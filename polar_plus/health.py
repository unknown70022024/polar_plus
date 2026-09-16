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

from polar_plus.config import (FLOOR_TS_ENV, HEALTH_DEAD_BLOCKS_MAX,
                               HEALTH_DEAD_SUBBLOCK_FRAC,
                               HEALTH_GLOBAL_INVALID_MAX, HEALTH_SUB_COLS,
                               HEALTH_SUB_ROWS, OUTPUT_DIR, ROOT_MARKER_KEY,
                               ROOT_MARKER_VALUE, ROOT_TS_TIMEOUT)

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

    Strictly validates the shape first and round-trips the result, so a
    truncated value like "20260915_1435" is rejected rather than silently
    read as 14:03:05 (see _TS_RE).
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
    """Site root of the GitHub Pages deployment, or None if underivable.

    Resolution order:

    1. ``PAGES_ROOT_URL`` — explicit override, so a local run can still see
       the published data (handy when experimenting off-CI).
    2. ``GH_PAGES_BASE`` — the workflow's own variable.
    3. ``GITHUB_REPOSITORY`` — always set on GitHub Actions, so CI resolves
       without any configuration.

    Mirrors the URL derivation in run.py but *without* the trailing
    ``/tiles/``, because root.json sits at the site root.
    """
    explicit = (os.environ.get("PAGES_ROOT_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    gh = (os.environ.get("GH_PAGES_BASE") or "").strip()
    if gh:
        return gh.rstrip("/")
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        owner, name = owner.strip(), name.strip()
        if owner and name:
            return f"https://{owner.lower()}.github.io/{name}"
    return None


def _ts_from_json(raw: bytes, source: str) -> tuple[datetime | None, bool]:
    """Decode a root.json payload → (timestamp, carries the publish marker).

    Never raises. The marker is read even when the timestamp is unusable, so
    a corrupt timestamp cannot by itself trigger a bootstrap.
    """
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:                       # noqa: BLE001 - defensive
        logger.warning(f"root.json 解析失败 ({source}): {exc}")
        return None, False
    if not isinstance(data, dict):
        logger.warning(f"root.json 顶层不是对象 ({source}): {type(data).__name__}")
        return None, False
    marked = data.get(ROOT_MARKER_KEY) == ROOT_MARKER_VALUE
    raw_ts = data.get("timestamp")
    ts = parse_ts(raw_ts)
    if ts is None:
        logger.warning(f"root.json 的 timestamp 缺失或格式非法 "
                       f"({source}): {raw_ts!r}")
        return None, marked
    return ts, marked


def _fetch_remote_ts(url: str) -> tuple[datetime | None, bool]:
    """GET a root.json over HTTP → (timestamp, marked). Failure → (None, False)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "polar_plus/1.0"})
        with urllib.request.urlopen(req, timeout=ROOT_TS_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                logger.warning(f"root.json HTTP {status} ({url})")
                return None, False
            raw = resp.read(256 * 1024)
    except Exception as exc:                       # noqa: BLE001 - defensive
        logger.info(f"root.json 获取失败 ({url}): {type(exc).__name__}: {exc}")
        return None, False
    return _ts_from_json(raw, url)


def _local_root_path():
    return OUTPUT_DIR / "latest" / "root.json"


def _read_local_ts() -> tuple[datetime | None, bool]:
    """Read the locally staged root.json → (timestamp, marked)."""
    path = _local_root_path()
    try:
        if not path.is_file():
            logger.info(f"本地 root.json 不存在: {path}")
            return None, False
        raw = path.read_bytes()
    except Exception as exc:                       # noqa: BLE001 - defensive
        logger.warning(f"本地 root.json 读取失败 ({path}): "
                       f"{type(exc).__name__}: {exc}")
        return None, False
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


def read_deployed_state() -> tuple[datetime | None, str, bool]:
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
        whether that source's root.json carries the publish marker).
        Never raises; on total failure returns (None, "none", False) and the
        caller runs without a lower bound.
    """
    found: list[tuple[datetime, str, bool]] = []

    root = _pages_root()
    if root:
        url = f"{root}/root.json"
        ts, marked = _fetch_remote_ts(url)
        if ts is not None:
            found.append((ts, f"pages:{url}", marked))
    else:
        logger.info("无法推导 Pages 地址（GH_PAGES_BASE 与 GITHUB_REPOSITORY "
                    "均未设置），跳过线上 root.json")

    local_ts, local_marked = _read_local_ts()
    if local_ts is not None:
        found.append((local_ts, f"local:{_local_root_path()}", local_marked))

    if not found:
        return None, "none", False

    if len(found) > 1:
        logger.info("root.json 多来源: " + "; ".join(
            f"{t:%Y-%m-%d %H:%M}Z({s}{'，已标记' if m else '，无标记'})"
            for t, s, m in found))

    ts, source, marked = max(found, key=lambda item: item[0])
    return _clamp_future(ts, "root.json"), source, marked


def resolve_floor_ts() -> tuple[datetime | None, str, bool]:
    """Lower bound for the backward search, plus the bootstrap flag.

    POLAR_FLOOR_TS is an experiment knob:

    * unset / empty      → auto-detect (max of the Pages and local root.json)
    * ``none``/``off``   → no bound at all
    * ``YYYYMMDD_HHMM``  → force this exact instant

    A malformed override is logged and ignored rather than trusted.

    Returns:
        (floor, source, bootstrap). ``bootstrap`` is True only when the live
        root.json exists but carries no publish marker, i.e. the version being
        served was produced by the old, ungated pipeline. In that case the
        caller ignores both bounds and force-publishes the newest healthy
        file — otherwise a bad live version would block the healthy older one
        forever. The run that does this writes the marker, so every later run
        goes back to normal bounded behaviour.
    """
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
    ts, source, marked = read_deployed_state()
    if ts is not None and not marked:
        logger.warning(
            f"线上 root.json（{source}）没有本版本标记 "
            f"{ROOT_MARKER_KEY}={ROOT_MARKER_VALUE} —— 判定为本次为初次运行，"
            f"忽略下界与搜索上限，强行发布最新健康文件")
        return None, source, True
    return ts, source, False


def describe_floor(ts: datetime | None, source: str, bootstrap: bool = False) -> str:
    """One-line log/console rendering of the resolved lower bound."""
    if bootstrap:
        return (f"未设下界（{source} 无本版本标记 → 初次运行，"
                f"忽略搜索上限，强行发布最新健康文件）")
    if ts is None:
        return f"未设回退下界（{source}）"
    return f"{ts:%Y-%m-%d %H:%M}Z（{source}）"
