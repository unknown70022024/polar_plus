"""
polar_plus/health.py — BT_10.8um completeness gate + backward-search bound.

Why this exists
---------------
NASA's hourly GCC NetCDF files are published at HH:00Z but keep being
rewritten for many hours afterwards as late granules arrive (measured
Last-Modified advances of +2h up to +8h). Every variable in the file carries
data of its own, and the pipeline only needs ``BT_10.8um``. So the
file-level signals people normally reach for —

    Content-Length, Last-Modified, granule counts, total size

— tell us nothing useful: the file can still be growing for reasons that
have nothing to do with BT_10.8um, and it can already look "finalised"
while BT_10.8um is still missing entire latitude bands.

We therefore check the one variable we actually consume, directly, while
reading it, and we check it *before* committing to the rest of the file.

The signal
----------
``BT_10.8um`` is (1, 6480, 12960) uint16 with chunks (1, 1620, 3240) — a 4x4
grid of chunks, each 45 deg of latitude by 90 deg of longitude. Missing data
is ``_FillValue`` (65535) or outside ``valid_range`` [18000, 40000].

We split the field into 405x810 sub-blocks (256 of them) and count how many
are at least 95% invalid. Measured on real files, 2026-09-15:

    file                    global invalid   dead_blocks   verdict
    09-14 14:00 (reproc.)       1.3%              0        usable
    09-15 09:00                 8.2%              1        usable
    09-15 10:00                 8.2%              1        usable
    09-15 11:00                 5.6%              1        usable
    09-15 12:00                 5.3%              1        usable
    09-15 13:00                 5.3%              1        usable
    09-15 14:00 (writing)      33.2%             43        REJECT

Healthy files sit at 0-1 dead blocks (the 1 is one fixed high-latitude gap
present in every file) and still-writing ones at 43, so the threshold in
HEALTH_DEAD_BLOCKS_MAX keeps a wide margin on both sides. The separate
global-invalid backstop catches a file that is uniformly sparse without any
single dead block.

Everything in this module is defensive: it is called before any download has
happened, it runs on every pipeline invocation including the very first one,
and no failure here may ever abort the run. Every entry point returns a
usable value (``None`` / 0 / an ``ok=False`` report) instead of raising.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from polar_plus.config import (DEV_VERSION, FLOOR_TS_ENV,
                               HEALTH_DEAD_BLOCKS_MAX,
                               HEALTH_DEAD_SUBBLOCK_FRAC,
                               HEALTH_GLOBAL_INVALID_MAX, HEALTH_SUB_COLS,
                               HEALTH_SUB_ROWS, LEGACY_MARKER_KEY, OUTPUT_DIR,
                               ROOT_TS_TIMEOUT, ROOT_VERSION_KEY, VERSION_ENV,
                               pipeline_version, public_base_url)

logger = logging.getLogger(__name__)

# root.json timestamps look like "20260914_140000" (seconds optional).
#
# The shape is pinned by a regex *before* strptime because strptime is far
# too lenient here: %M and %S both accept a single digit, so
# strptime("20260915_1435", "%Y%m%d_%H%M%S") happily returns 14:03:05
# (H=14, M=3, S=5) instead of failing. A truncated or hand-edited timestamp
# would therefore be silently misread rather than rejected.
_TS_RE = re.compile(r"^(\d{8})_(\d{4}|\d{6})$")


# ---------------------------------------------------------------------------
# BT_10.8um completeness
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BtHealth:
    """Verdict for one candidate file's BT_10.8um coverage."""

    global_invalid: float     # fraction of the whole grid that is fill/invalid
    dead_blocks: int          # 16x16 sub-blocks at/above the dead threshold
    worst_block: float        # invalid fraction of the worst single sub-block
    ok: bool
    reason: str = ""

    def summary(self) -> str:
        return (f"global_invalid={self.global_invalid * 100:.1f}% "
                f"dead_blocks={self.dead_blocks} "
                f"worst_block={self.worst_block * 100:.1f}%")


def count_dead_blocks(invalid: np.ndarray,
                      sub_rows: int = None,
                      sub_cols: int = None) -> tuple[int, float]:
    """Count sub-blocks whose invalid fraction is at/above the dead threshold.

    The sub-block geometry is fixed (HEALTH_SUB_ROWS x HEALTH_SUB_COLS, i.e.
    405x810 = 1/16 of a latitude band by 1/4 of a chunk width), **not** a
    fixed grid size. That matters because this is called both on the whole
    BT_10.8um array (6480x12960 → a 16x16 grid, 256 blocks) and on a single
    1620-row band while streaming (1620x12960 → a 4x16 grid, 64 blocks).
    Both use the same block size, so the counts are directly comparable and
    a band's contribution is exactly its rows of the full grid.

    Args:
        invalid: 2-D boolean array whose height is a multiple of
            ``sub_rows`` and width a multiple of ``sub_cols``.
        sub_rows, sub_cols: override the block size (tests).

    Returns:
        (dead_block_count, worst_block_fraction)

    Raises:
        ValueError: if the array is not 2-D or does not tile evenly. Callers
            in the pipeline only ever pass BT_10.8um geometry, so this is a
            programming-error guard rather than a runtime condition.
    """
    sub_rows = sub_rows or HEALTH_SUB_ROWS
    sub_cols = sub_cols or HEALTH_SUB_COLS
    if invalid.ndim != 2:
        raise ValueError(f"expected a 2-D array, got shape {invalid.shape}")
    h, w = invalid.shape
    if h % sub_rows or w % sub_cols:
        raise ValueError(
            f"shape {invalid.shape} is not a multiple of the "
            f"{sub_rows}x{sub_cols} sub-block size")
    sub = invalid.reshape(h // sub_rows, sub_rows,
                          w // sub_cols, sub_cols).mean(axis=(1, 3))
    return int((sub >= HEALTH_DEAD_SUBBLOCK_FRAC).sum()), float(sub.max())


def evaluate_bt(invalid: np.ndarray) -> BtHealth:
    """Full verdict from an invalid-mask for BT_10.8um.

    ``ok`` is driven by the dead-block count, which is the measured
    discriminator. The global-invalid threshold is a loose backstop only —
    a still-writing file measured 33% global (below the 50% backstop) yet
    thousands of dead blocks, so the backstop is not what does the work.
    """
    dead, worst = count_dead_blocks(invalid)
    glob = float(invalid.mean())
    reasons = []
    if dead >= HEALTH_DEAD_BLOCKS_MAX:
        reasons.append(f"dead_blocks={dead}>={HEALTH_DEAD_BLOCKS_MAX}")
    if glob >= HEALTH_GLOBAL_INVALID_MAX:
        reasons.append(
            f"global_invalid={glob:.1%}>={HEALTH_GLOBAL_INVALID_MAX:.0%}")
    return BtHealth(global_invalid=glob, dead_blocks=dead, worst_block=worst,
                    ok=not reasons, reason=", ".join(reasons))


# ---------------------------------------------------------------------------
# Backward-search lower bound (never publish older data than what is live)
# ---------------------------------------------------------------------------

def parse_ts(value) -> datetime | None:
    """Parse a root.json timestamp. Returns None for anything unusable.

    Two legal shapes, both of which the pipeline has actually written at some
    point: ``YYYYMMDD_HHMMSS`` and ``YYYYMMDD_HHMM``.

    The shape is pinned by a regex rather than left to strptime, which is far
    too lenient here: ``%M`` and ``%S`` both accept a single digit, so
    ``strptime("20260915_1435", "%Y%m%d_%H%M%S")`` returns 14:03:05 (H=14,
    M=3, S=5) instead of failing. The format is therefore chosen by the
    matched length, and the result is round-tripped back to text as a final
    guard. A value that is neither shape — truncated, hand-edited, with
    separators — is rejected rather than silently misread.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _TS_RE.match(text)
    if match is None:
        return None
    fmt = "%Y%m%d_%H%M%S" if len(match.group(2)) == 6 else "%Y%m%d_%H%M"
    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError:
        return None
    if parsed.strftime(fmt) != text:      # belt and braces against leniency
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _pages_root() -> str | None:
    """Site root of the public deployment, or None if underivable.

    Delegates to ``config.public_base_url()`` so the publish side (run.py) and
    the read-back side (this module) can never disagree about where the live
    root.json lives — that agreement is what makes the backward-search floor
    trustworthy.

    The site root is host-neutral: GitHub Pages, Azure Static Web Apps, Azure
    Blob static website or a local directory all work unchanged.
    """
    return public_base_url()


def _ts_from_json(raw: bytes, source: str) -> tuple[datetime | None, str | None]:
    """Decode a root.json payload → (timestamp, pipeline version or None).

    Never raises. The version is read even when the timestamp is unusable, so
    a corrupt timestamp cannot by itself trigger a bootstrap.
    """
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:                       # noqa: BLE001 - defensive
        logger.warning(f"root.json 解析失败 ({source}): {exc}")
        return None, None
    if not isinstance(data, dict):
        logger.warning(f"root.json 顶层不是对象 ({source}): {type(data).__name__}")
        return None, None
    if ROOT_VERSION_KEY in data:
        version = str(data.get(ROOT_VERSION_KEY) or "").strip() or None
    elif LEGACY_MARKER_KEY in data:
        # 旧版写的是 gate=1 —— 数据由未版本化的管线产生，一律当作"版本不同"
        version = f"legacy:{data.get(LEGACY_MARKER_KEY)}"
    else:
        version = None
    raw_ts = data.get("timestamp")
    ts = parse_ts(raw_ts)
    if ts is None:
        logger.warning(f"root.json 的 timestamp 缺失或格式非法 "
                       f"({source}): {raw_ts!r}")
        return None, version
    return ts, version


def _fetch_remote_ts(url: str) -> tuple[datetime | None, str | None]:
    """GET a root.json over HTTP → (timestamp, version). Failure → (None, None)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "polar_plus/1.0"})
        with urllib.request.urlopen(req, timeout=ROOT_TS_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                logger.warning(f"root.json HTTP {status} ({url})")
                return None, None
            raw = resp.read(256 * 1024)
    except Exception as exc:                       # noqa: BLE001 - defensive
        logger.info(f"root.json 获取失败 ({url}): {type(exc).__name__}: {exc}")
        return None, None
    return _ts_from_json(raw, url)


def _local_root_path():
    return OUTPUT_DIR / "latest" / "root.json"


def _read_local_ts() -> tuple[datetime | None, str | None]:
    """Read the locally staged root.json → (timestamp, version)."""
    path = _local_root_path()
    try:
        if not path.is_file():
            logger.info(f"本地 root.json 不存在: {path}")
            return None, None
        raw = path.read_bytes()
    except Exception as exc:                       # noqa: BLE001 - defensive
        logger.warning(f"本地 root.json 读取失败 ({path}): "
                       f"{type(exc).__name__}: {exc}")
        return None, None
    return _ts_from_json(raw, str(path))


def _clamp_future(ts: datetime, label: str) -> datetime:
    """Pull a future timestamp back to the current hour.

    A timestamp ahead of now (clock skew, or a hand-edited value) would make
    the backward search reject *every* candidate and abort the run forever,
    so it is clamped rather than trusted.
    """
    now = datetime.now(timezone.utc)
    if ts > now:
        floored = now.replace(minute=0, second=0, microsecond=0)
        logger.warning(f"{label} 时间戳 {ts:%Y-%m-%d %H:%M}Z 晚于当前时刻，"
                       f"已夹到 {floored:%Y-%m-%d %H:%M}Z")
        return floored
    return ts


def read_deployed_state() -> tuple[datetime | None, str, str | None]:
    """Latest timestamp already published, plus whether it carries the marker.

    Both the GitHub Pages copy and the locally staged copy are consulted and
    the **later** one wins. That makes the behaviour correct without having to
    detect the environment:

    * on GitHub Actions the local file does not exist, so this is the Pages
      timestamp — the data that is actually live;
    * locally it is normally the local file, which is the freshest output;
    * when both exist the later one wins, so we can never regress.

    Returns:
        (timestamp or None, human-readable description of where it came from,
        the pipeline version recorded there, or None).
        Never raises; on total failure returns (None, "none", None) and the
        caller runs without a lower bound.
    """
    found: list[tuple[datetime, str, str | None]] = []

    root = _pages_root()
    if root:
        url = f"{root}/root.json"
        ts, version = _fetch_remote_ts(url)
        if ts is not None:
            found.append((ts, f"pages:{url}", version))
    else:
        logger.info("无法推导 Pages 地址（GH_PAGES_BASE 与 GITHUB_REPOSITORY "
                    "均未设置），跳过线上 root.json")

    local_ts, local_version = _read_local_ts()
    if local_ts is not None:
        found.append((local_ts, f"local:{_local_root_path()}", local_version))

    if not found:
        return None, "none", None

    if len(found) > 1:
        logger.info("root.json 多来源: " + "; ".join(
            f"{t:%Y-%m-%d %H:%M}Z({s}，版本={m or '未标记'})"
            for t, s, m in found))

    ts, source, version = max(found, key=lambda item: item[0])
    return _clamp_future(ts, "root.json"), source, version


def resolve_floor_ts() -> tuple[datetime | None, str, bool]:
    """Lower bound for the backward search, plus the bootstrap flag.

    POLAR_FLOOR_TS is an experiment knob:

    * unset / empty      → auto-detect (max of the Pages and local root.json)
    * ``none``/``off``   → no bound at all
    * ``YYYYMMDD_HHMM``  → force this exact instant

    A malformed override is logged and ignored rather than trusted.

    Returns:
        (floor, source, bootstrap).

        ``bootstrap`` is True when the live root.json was produced by a
        *different* build than the one running now — including the case where
        it carries no version at all (data written by the old, unversioned
        pipeline). In that case the floor is dropped: the live data may itself
        be exactly what this build exists to replace, and keeping the bound
        would block the fix forever. The run then picks the newest healthy
        file, force-publishes it, and writes its own version, so every later
        run goes back to normal bounded operation.

        The search window (SEARCH_HOURS) still applies during bootstrap, so
        this can never publish genuinely stale data.
    """
    running, version_src = pipeline_version()
    raw = (os.environ.get(FLOOR_TS_ENV) or "").strip()
    if raw:
        if raw.lower() in ("none", "off", "disable", "disabled"):
            return None, f"{FLOOR_TS_ENV}=none (下界已禁用)", False
        forced = parse_ts(raw)
        if forced is None:
            logger.warning(f"{FLOOR_TS_ENV}={raw!r} 无法解析"
                           f"（应为 YYYYMMDD_HHMMSS 或 YYYYMMDD_HHMM），"
                           f"改用自动检测")
        else:
            return _clamp_future(forced, FLOOR_TS_ENV), f"{FLOOR_TS_ENV}={raw}", False
    ts, source, version = read_deployed_state()
    if ts is not None and version != running:
        if version is None:
            reason = ("线上 root.json 没有版本标记（数据由未版本化的"
                      "旧管线产生）")
        elif version.startswith("legacy:"):
            reason = (f"线上 root.json 用的是旧的 gate 标记 "
                      f"({version.split(':', 1)[1]})")
        else:
            reason = f"线上版本 {version} ≠ 本次运行版本 {running}"
        hint = ""
        if running == DEV_VERSION:
            hint = (f"；本次运行也没能识别自己的构建"
                    f"（可设 {VERSION_ENV} 或重新构建镜像）")
        logger.warning(
            f"{reason}{hint} —— 判定为本构建的初次运行，"
            f"忽略回退下界（搜索窗口仍然生效），"
            f"将发布窗口内最新的健康文件")
        return None, source, True
    return ts, source, False


def describe_floor(ts: datetime | None, source: str, bootstrap: bool = False) -> str:
    """One-line log/console rendering of the resolved lower bound."""
    running, version_src = pipeline_version()
    if bootstrap:
        return (f"未设下界（{source} → 版本 {running} 的初次运行，"
                f"忽略回退下界，发布窗口内最新健康文件）")
    if ts is None:
        return f"未设回退下界（{source}）"
    return f"{ts:%Y-%m-%d %H:%M}Z（{source}，版本 {running}）"
