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

The verdict is the **area-weighted invalid fraction over the region SSEC
cannot repair** (|lat| < 60): reject when it exceeds
``HEALTH_UNFILLABLE_MAX_PCT`` (1.05%).

    unfillable_pct = Σ over rows with |lat| < 60 of
                     (row invalid fraction × row area share) × 100

Rows are weighted by their exact spherical area, ``sin(hi) - sin(lo)``, not by
the midpoint approximation. There is deliberately **no cap** on loss inside
|lat| > 60, because capfill repaints that region from SSEC.

What this replaced, and why
---------------------------
Until 2026-09-18 the gate counted 405x810 sub-blocks that were >=95% invalid
and rejected at 4 of them. Measured over 45 real files (full sweep in the
gcc-probe repository) that count was wrong in both directions simultaneously:

  * **count != area.** A block is a fixed pixel count, so it covers 0.060% of
    the globe at the pole and 0.610% at the equator — 10.2x. "Four dead
    blocks" meant 0.24% of the globe at high latitude but 2.44% near it.
  * **>=95% is a cliff.** 94% invalid contributed nothing, 95% contributed a
    whole unit, so the verdict tracked the *shape* of the damage rather than
    its size. 09-17 16:00Z measured 47.6% invalid across the whole 70-80N band
    yet scored zero dead blocks, and was published with black holes over the
    equatorial faces — the exact defect the gate exists to prevent.
  * **position was ignored.** Loss inside capfill's |lat| > 60 zone is
    recoverable, loss outside is not, and the count treated them alike.

The result was that accept/reject was not even monotone in data loss: a file
missing 5.69% of the globe was accepted while one missing 3.87% was rejected,
and files whose only defect was a fixable polar cap were thrown away.

Measured separation after the change, same 45 files:

    accepted, worst   0.760% unfillable
    rejected, best    1.037% unfillable

Nothing sat near 1.0%, so the threshold inside that gap changes no observed
decision; 1.05% takes the loose end and admits the 1.037% file.

The dead-block count survives as a **diagnostic only** (see
``count_dead_blocks``): it no longer decides anything, but it appears in years
of logs and comparing it across the change is how the transition is reviewed.

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

from functools import lru_cache

from polar_plus.config import (BT_N_ROWS, DEV_VERSION, FLOOR_TS_ENV,
                               HEALTH_DEAD_SUBBLOCK_FRAC, HEALTH_FILL_LAT,
                               HEALTH_SUB_COLS, HEALTH_SUB_ROWS,
                               HEALTH_UNFILLABLE_MAX_PCT, LEGACY_MARKER_KEY,
                               OUTPUT_DIR, ROOT_TS_TIMEOUT, ROOT_VERSION_KEY,
                               VERSION_ENV, pipeline_version,
                               public_base_url)

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
# The verdict is an AREA measurement over the region SSEC cannot repair. See
# the "BT_10.8um completeness gate" section of config.py for why the previous
# dead-block *count* was replaced — briefly, count is not area (10.2x spread
# between a polar and an equatorial block), the 95% rule is a cliff that made
# the verdict depend on the shape of the damage rather than its size, and it
# ignored whether the loss fell inside capfill's |lat| > 60 zone.

@dataclass(frozen=True)
class BtHealth:
    """Verdict for one candidate file's BT_10.8um coverage.

    ``unfillable_pct`` is the decisive figure: the area-weighted invalid
    fraction, in percent of the globe, over |lat| <= HEALTH_FILL_LAT (the part
    SSEC cannot patch). ``fillable_pct`` is the same measure over the polar
    caps, reported but never used to decide. Dead blocks are kept as
    diagnostics for continuity with the historical log record.
    """

    global_invalid: float     # fraction of the whole grid that is fill/invalid
    dead_blocks: int          # 16x16 sub-blocks at/above the dead threshold
    worst_block: float        # invalid fraction of the worst single sub-block
    ok: bool
    reason: str = ""
    unfillable_pct: float = 0.0
    fillable_pct: float = 0.0

    def summary(self) -> str:
        return (f"不可补缺失={self.unfillable_pct:.3f}% "
                f"(上限 {HEALTH_UNFILLABLE_MAX_PCT}%) "
                f"可补缺失={self.fillable_pct:.3f}% "
                f"| 诊断: 全局无效={self.global_invalid * 100:.1f}% "
                f"死块={self.dead_blocks} 最差块={self.worst_block * 100:.1f}%")


@lru_cache(maxsize=4)
def row_area_weights(n_rows: int) -> tuple:
    """Exact spherical area of each row, as a fraction of the globe summing to 1.

    Uses the exact band area ``sin(lat_hi) - sin(lat_lo)`` per row.

    The more obvious ``cos(lat_centre)`` midpoint form is *not* used, but the
    honest reason is robustness rather than a bug: measured on this 6480-row
    grid the two agree to machine precision (largest relative difference
    0.000% across every row, and identical band totals), because a midpoint
    quadrature of cos over a 0.028-degree cell is already exact to well below
    float64 display precision. The exact form is kept because it is right by
    construction at any resolution — a coarser grid, or a future change to
    BT_N_ROWS, would start to separate them, and nothing here should depend on
    the grid staying this fine.
    """
    edges = np.linspace(90.0, -90.0, n_rows + 1)
    w = np.sin(np.deg2rad(edges[:-1])) - np.sin(np.deg2rad(edges[1:]))
    return tuple((w / w.sum()).tolist())


def row_centres(n_rows: int) -> np.ndarray:
    """Latitude of each row's centre, north to south."""
    edges = np.linspace(90.0, -90.0, n_rows + 1)
    return (edges[:-1] + edges[1:]) / 2.0


def unfillable_row_mask(n_rows: int = None,
                        fill_lat: float = None) -> np.ndarray:
    """Boolean mask of rows SSEC cannot repair (``|lat| < fill_lat``).

    Strict on both sides. For the real 6480-row grid no cell centre lands
    exactly on +/-60, so the boundary convention cannot change a verdict; a
    test pins that, because it stops being true if the grid ever changes.
    """
    n_rows = n_rows or BT_N_ROWS
    fill_lat = HEALTH_FILL_LAT if fill_lat is None else fill_lat
    c = row_centres(n_rows)
    return (c > -fill_lat) & (c < fill_lat)


class BtHealthAccumulator:
    """Running unfillable-area tally, fed one latitude band at a time.

    Exists so the banded reader can reach a verdict without a second pass over
    a full 6480x12960 mask. Everything is accumulated from per-band
    ``invalid.mean(axis=1)`` plus the row weights, so memory stays at one band
    (about 21 MB of bool) instead of a full float64 intermediate (~670 MB).

    The diagnostic dead-block count accumulates the same way. That is exact,
    not an approximation: the sub-block size is fixed at 405x810, so a band's
    4x16 grid contributes precisely its rows of the full 16x16 grid, and the
    totals agree — the gcc-probe sweep verified ``band_sum == full_count`` on
    all 45 measured files.
    """

    def __init__(self, n_rows: int = None, fill_lat: float = None):
        self.n_rows = n_rows or BT_N_ROWS
        self._w = np.asarray(row_area_weights(self.n_rows))
        self._mid = unfillable_row_mask(self.n_rows, fill_lat)
        self.unfillable_pct = 0.0
        self.fillable_pct = 0.0
        self.dead_blocks = 0
        self.worst_block = 0.0
        self._invalid_px = 0
        self._total_px = 0
        self._rows_seen = 0

    def add_band(self, invalid_band: np.ndarray, row0: int) -> None:
        """Fold one band's invalid mask in. ``row0`` is its first row index."""
        n = invalid_band.shape[0]
        if row0 != self._rows_seen:
            raise ValueError(
                f"bands must be added in order and without gaps: got row0="
                f"{row0}, expected {self._rows_seen}")
        w = self._w[row0:row0 + n]
        # Contribution of each row to the global invalid area, in percent.
        contrib = invalid_band.mean(axis=1) * w * 100.0
        mid = self._mid[row0:row0 + n]
        self.unfillable_pct += float(contrib[mid].sum())
        self.fillable_pct += float(contrib[~mid].sum())

        dead, worst = count_dead_blocks(invalid_band)
        self.dead_blocks += dead
        self.worst_block = max(self.worst_block, worst)

        self._invalid_px += int(invalid_band.sum())
        self._total_px += int(invalid_band.size)
        self._rows_seen += n

    @property
    def exceeded(self) -> bool:
        """True once the running unfillable loss has already failed the gate.

        Monotone, so it is safe to abort on the partial sum — the same
        property the old cumulative dead-block count relied on.
        """
        return self.unfillable_pct > HEALTH_UNFILLABLE_MAX_PCT

    @property
    def global_invalid(self) -> float:
        return (self._invalid_px / self._total_px) if self._total_px else 0.0

    def finalize(self) -> "BtHealth":
        """Verdict for everything accumulated so far."""
        reasons = []
        if self.unfillable_pct > HEALTH_UNFILLABLE_MAX_PCT:
            reasons.append(f"不可补缺失={self.unfillable_pct:.3f}%>"
                           f"{HEALTH_UNFILLABLE_MAX_PCT}%")
        return BtHealth(
            global_invalid=self.global_invalid,
            dead_blocks=self.dead_blocks,
            worst_block=self.worst_block,
            unfillable_pct=self.unfillable_pct,
            fillable_pct=self.fillable_pct,
            ok=not reasons,
            reason=", ".join(reasons),
        )


def count_dead_blocks(invalid: np.ndarray,
                      sub_rows: int = None,
                      sub_cols: int = None) -> tuple[int, float]:
    """Count sub-blocks whose invalid fraction is at/above the dead threshold.

    DIAGNOSTIC ONLY — this no longer decides anything. It is kept because the
    number appears in years of logs and comparing it across the change is how
    the transition gets reviewed.

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
    """Full verdict from a complete invalid mask for BT_10.8um.

    A thin wrapper over BtHealthAccumulator with a single band, so the
    streaming path in gcc_load and this whole-array path cannot drift apart —
    they are literally the same arithmetic.

    The old global-invalid backstop (50%) is gone: it is subsumed. A file that
    is entirely fill has an unfillable loss of 86.6% of the globe, because
    |lat| <= 60 covers that much of the sphere, so the area criterion rejects
    it with room to spare. Keeping two thresholds would only invite the
    "which one actually decides?" confusion the old SEARCH_HOURS /
    MAX_FALLBACK_HOURS pair caused.
    """
    acc = BtHealthAccumulator(n_rows=invalid.shape[0])
    acc.add_band(invalid, 0)
    return acc.finalize()


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
