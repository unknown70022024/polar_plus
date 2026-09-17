"""
polar_plus/gcc_load.py — NASA SatCORPS Global Cloud Composite (GCC) v2a

Single-source global cloud cover, 90N-90S. Replaces GMGSI.
Remote partial-read via h5py — only the compressed chunks of BT_10.8um and
cloud_phase are transferred (~100 MB), never the entire ~1.3 GB file.

Data:
  - Source: NASA Langley SatCORPS
  - URL: satcorps.larc.nasa.gov/prod/GCC-GEO-LEO/v2a/visst-pixel-netcdf/
  - Grid: 12960×6480 regular equirectangular (~3 km at equator), 90N to -90S
  - Variables: BT_10.8um (10.8μm brightness temp, uint16+scale_factor 0.01)
               cloud_phase (uint8, 0=clear, 1=liquid, 2=ice, 3+=other)
  - Format: NetCDF4/HDF5 with chunked zlib compression

Completeness — read this before changing the search logic:
  NASA publishes each hourly file at HH:00Z but keeps rewriting it for hours
  afterwards as late granules arrive (measured Last-Modified up to +8h). The
  file also carries ~37 other science variables, so its size, Last-Modified
  and granule counts say nothing about whether *our* variable is complete:
  a file can look finalised while BT_10.8um is still missing entire latitude
  bands, and a hole that big survives the SSEC polar gap-fill.
  We therefore validate BT_10.8um itself while reading it (see health.py) and
  walk back an hour at a time until one passes, never going older than the
  timestamp already published (see health.resolve_floor_ts). If nothing
  passes, load_gcc_density raises NoHealthyGCCError and the caller must abort
  rather than publish partial data.
"""
import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np

from polar_plus.config import (GCC_V2A_BASE, BT_WARM, BT_COLD, SEARCH_HOURS,
                               MIN_AGE_HOURS, HEALTH_ENFORCE,
                               HEALTH_DEAD_BLOCKS_MAX,
                               GCC_SIZE_FILTER_RATIO)
from polar_plus.health import BtHealth, count_dead_blocks, evaluate_bt

logger = logging.getLogger(__name__)

# BT_10.8um geometry: (1, 6480, 12960) uint16, chunks (1, 1620, 3240).
# One 1620-row band costs exactly 4 HDF5 chunks (the 4 longitude quadrants),
# so a band is the cheapest unit that still spans every longitude. The
# north band (lat 90..45N) is read first because every incomplete file we
# have measured is missing high-latitude data first.
BT_ROWS = 6480
BT_COLS = 12960
BT_BANDS = 4
BT_BAND_ROWS = BT_ROWS // BT_BANDS      # 1620 == one HDF5 chunk row


class NoHealthyGCCError(RuntimeError):
    """No candidate file in the allowed window had a complete BT_10.8um."""


class IncompleteGCCError(RuntimeError):
    """A candidate file's BT_10.8um is incomplete — the file is still being
    written. Raised instead of returning partial data so the caller can move
    on to the previous hour."""


def _gcc_url(dt: datetime) -> str:
    """Build HTTP URL for a GCC v2a file at a given datetime."""
    yyyy = dt.year
    mm = f"{dt.month:02d}"
    dd = f"{dt.day:02d}"
    doy = f"{dt.timetuple().tm_yday:03d}"
    hhmm = f"{dt.hour:02d}00"
    return (f"{GCC_V2A_BASE}/{yyyy}/{mm}/{dd}/"
            f"satcorps-gcc.v02a.geoleo.glob-comp."
            f"{yyyy}{doy}.{hhmm}.3km.nc")


def iter_candidate_files(floor_dt: datetime = None,
                         max_hours_back: int = SEARCH_HOURS,
                         min_age_hours: int = MIN_AGE_HOURS):
    """Yield (datetime, url) candidates newest-first, bounded on both sides.

    The walk starts at the most recent eligible hour and steps back one hour
    at a time. The near bound is ``min_age_hours`` (files younger than this are
    still being written, so they are skipped). The walk stops as soon as a
    candidate is no longer **newer** than ``floor_dt`` — the newest timestamp
    already published — so live data can never be replaced by something older,
    and hard-stops after ``max_hours_back`` hours regardless.

    Args:
        floor_dt: exclusive lower bound. None disables it (first run, or a
            live site with no root.json yet), leaving max_hours_back as the
            only limit.
        max_hours_back: search window, in hours.
        min_age_hours: skip files younger than this.

    Yields:
        (dt, url) tuples, possibly none when the bounds leave no room; the
        caller turns that into NoHealthyGCCError.
    """
    if max_hours_back < min_age_hours:
        logger.warning(f"搜索窗口 {max_hours_back}h < 最小文件年龄 "
                       f"{min_age_hours}h —— 没有可搜索的候选")
        return

    now = datetime.now(timezone.utc)
    for hours_ago in range(min_age_hours, max_hours_back + 1):
        dt = (now - timedelta(hours=hours_ago)).replace(
            minute=0, second=0, microsecond=0)
        if floor_dt is not None and dt <= floor_dt:
            logger.info(f"候选 {dt:%Y-%m-%d %H:%M}Z 已不晚于线上时间戳 "
                        f"{floor_dt:%Y-%m-%d %H:%M}Z —— 停止向前搜索")
            return
        yield dt, _gcc_url(dt)


def _head_gcc(url: str) -> tuple:
    """HEAD the GCC file and return a stability signature.

    Returns (Content-Length, Last-Modified, ETag), or None if the HEAD
    request fails.
    """
    import urllib.request

    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (
                resp.headers.get('Content-Length'),
                resp.headers.get('Last-Modified'),
                resp.headers.get('ETag'),
            )
    except Exception as e:
        logger.warning(f"GCC HEAD failed: {e}")
        return None


def _read_bt_banded(h, enforce: bool = True) -> tuple:
    """Read BT_10.8um band by band from an open h5py file, validating as we go.

    Why banded: BT_10.8um is chunked (1, 1620, 3240), so one 1620-row band
    costs exactly 4 HDF5 chunks. Reading a band at a time lets us detect a
    still-being-written file after 4 of the 16 chunks instead of all 16 —
    and on a healthy file it costs exactly the same as reading the whole
    variable in one go, because the chunk set is identical.

    The north band (lat 90..45N) is read first: every incomplete file we have
    measured loses high-latitude data first, so that ordering maximises the
    early-abort saving.

    Args:
        h: an open h5py.File for the remote file (shared with the caller so
           the HDF5 handle is opened exactly once).
        enforce: when False the gate only logs (shadow mode).

    Returns:
        (bt_float32_with_nan, health)

    Raises:
        IncompleteGCCError: when the accumulated dead-block count crosses
            HEALTH_DEAD_BLOCKS_MAX and ``enforce`` is True. The caller never
            reaches the cloud_phase read in that case.
    """
    t0 = time.time()
    bt = np.empty((BT_ROWS, BT_COLS), dtype=np.float32)
    dead_total = 0
    for band in range(BT_BANDS):
        r0 = band * BT_BAND_ROWS
        raw = h['BT_10.8um'][0, r0:r0 + BT_BAND_ROWS, :]
        invalid = (raw == 65535) | (raw < 18000) | (raw > 40000)
        # Per-band contribution to the dead-block tally; the running total is
        # monotone, so it is safe to abort on the partial value.
        dead_band, worst = count_dead_blocks(invalid)
        dead_total += dead_band
        part = raw.astype(np.float32) * 0.01
        part[invalid] = np.nan
        bt[r0:r0 + BT_BAND_ROWS] = part
        del raw, part
        logger.info(
            f"    BT band {band} (lat {90 - band * 45}..{90 - (band + 1) * 45}): "
            f"无效率 {invalid.mean() * 100:5.1f}%, 死块 {dead_band:4d}, "
            f"累计 {dead_total}")
        if enforce and dead_total >= HEALTH_DEAD_BLOCKS_MAX:
            raise IncompleteGCCError(
                f"BT_10.8um 在第 {band} 带（lat "
                f"{90 - band * 45}..{90 - (band + 1) * 45}）即累计 "
                f"{dead_total} 个死块，判定文件未写完"
                f"（已读 {(band + 1) * 4}/16 chunk）")

    health = evaluate_bt(np.isnan(bt))
    logger.info(f"    BT_10.8um 读毕 {bt.shape} in {time.time() - t0:.0f}s, "
                f"BT {np.nanmin(bt):.1f}~{np.nanmax(bt):.1f}K, "
                f"{health.summary()}")
    return bt, health


def _read_candidate(url: str, enforce: bool = True) -> tuple:
    """Read BT_10.8um (validated) + cloud_phase + lat/lon via h5py.

    Uses h5py directly instead of xarray's open_dataset, which decodes every
    variable (including the variable-length string granule_name_list that
    triggers fragile global-heap reads over HTTP).

    BT_10.8um is read and validated first; cloud_phase is only fetched once
    BT has passed, so an incomplete file never pays for cloud_phase's 9
    chunks. Transfer stays partial (~100 MB of compressed chunks for a good
    file); the full ~1.3 GB file is never downloaded.

    Raises:
        IncompleteGCCError: BT_10.8um is incomplete (see _read_bt_banded).
    """
    import fsspec
    import h5py

    # 8 MB blocks: the 1 MB setting was needed only while we might read a
    # file mid-write; candidates are now content-validated, so fewer, larger
    # requests cut the round-trip count on a high-latency link.
    fs = fsspec.filesystem("http", block_size=8 * 1024 * 1024)
    with fs.open(url, "rb") as f:
        with h5py.File(f, "r") as h:          # opened exactly once
            bt, health = _read_bt_banded(h, enforce=enforce)
            cp_raw = h['cloud_phase'][0, :, :]  # (6480, 12960) uint8
            lat = h['lat'][:]                   # (6480,)
            lon = h['lon'][:]                   # (12960,)

    cp = cp_raw.astype(np.float32)
    cp[(cp_raw == 127) | (cp_raw > 13)] = np.nan

    logger.info(f"    cloud_phase: cloud={(cp >= 1).sum() / cp.size * 100:.0f}%")
    return bt, cp, lat, lon, health


def read_gcc_bt(url: str, max_retries: int = 3, enforce: bool = None,
                 before_sig: tuple = None) -> tuple:
    """Read BT_10.8um + cloud_phase, verifying the file did not change mid-read.

    Flow:
      1. Read BT_10.8um band by band, validating as we go.
      2. Read cloud_phase + lat/lon only once BT has passed.
      3. HEAD again: if the signature moved, the file was rewritten during the
         read -> discard and retry.

    Step 3 is the whole correctness mechanism, and it is why there is no
    "wait for the file to stop growing" probe any more. NASA does rewrite each
    hourly file in place for hours, but the write is caught by comparing the
    HEAD signature taken before the read with the one taken after it: if they
    differ, nothing from that read is used. A half-written file also fails on
    its own — either decompression errors out, or BT_10.8um is full of
    _FillValue and the completeness gate rejects it.

    IncompleteGCCError is deliberately **not** retried: retrying the same hour
    cannot help, so it propagates to the caller, which walks back an hour.

    Args:
        before_sig: HEAD signature already taken by the caller (the candidate
            walk HEADs every file anyway to read Content-Length). Reusing it
            saves a round trip on the first attempt; retries take a fresh one,
            since the point of a retry is that the file may have moved.

    Returns:
        (bt_k, cloud_phase, lat, lon, health) where bt_k is
        (6480, 12960) float32 Kelvin with NaN where invalid.
    """
    if enforce is None:
        enforce = HEALTH_ENFORCE
    for attempt in range(max_retries):
        before = before_sig if (attempt == 0 and before_sig is not None) \
            else _head_gcc(url)
        try:
            bt, cp, lat, lon, health = _read_candidate(url, enforce=enforce)
        except IncompleteGCCError:
            raise
        except Exception as e:
            logger.warning(f"    Read attempt {attempt + 1}/{max_retries} "
                           f"failed: {type(e).__name__}: {e}")
            time.sleep(2 ** attempt)
            continue
        after = _head_gcc(url)
        if after == before:
            return bt, cp, lat, lon, health
        logger.warning(
            f"    GCC file changed during read (attempt {attempt + 1}/"
            f"{max_retries}), retrying...")
    raise RuntimeError(
        f"GCC file could not be read stably after {max_retries} attempts: {url}"
    )


def _bt_to_density(bt_k: np.ndarray,
                   cloud_phase: np.ndarray = None,
                   use_cloud_phase: bool = True,
                   lat: np.ndarray = None) -> np.ndarray:
    """BT Kelvin → cloud density 0-255 uint8.

    Formula: density = (BT_WARM - BT) / (BT_WARM - BT_COLD) * 255
    BT_WARM=285K → clear/warm. BT_COLD=200K → thick cold cloud.

    If use_cloud_phase=True and cloud_phase is provided:
      - Cloud pixels (phase >= 1): use BT → density
      - Clear pixels (phase < 1 or NaN): set to 0
    """
    arr = bt_k.astype(np.float32)
    bt_range = BT_WARM - BT_COLD  # 85 K
    density = np.clip((BT_WARM - arr) / bt_range, 0.0, 1.0) * 255.0

    if cloud_phase is not None and use_cloud_phase:
        clear = (cloud_phase < 1) | np.isnan(cloud_phase)
        density[clear] = 0.0

    # Diagnostic: density by latitude band
    if lat is not None:
        lat_abs = np.abs(lat)
        bands = [(90, 80, "Polar(80-90°)"), (80, 70, "High(70-80°)"),
                 (70, 60, "Mid-High(60-70°)"), (60, 45, "Mid(45-60°)"),
                 (45, 30, "Mid-Low(30-45°)"), (30, 0, "Low(0-30°)")]
        for hi, lo, name in bands:
            mask = (lat_abs <= hi) & (lat_abs > lo)
            band = density[mask]
            logger.info(f"  Band [{name}]: mean={band.mean():.1f}, "
                        f"zeros={(band == 0).sum() / band.size * 100:.1f}%")

    density = np.nan_to_num(density, nan=0.0).astype(np.uint8)
    return density


def _gamma_correct(density: np.ndarray, gamma: float = 0.45) -> np.ndarray:
    """Gamma correction: gamma < 1 brightens dark areas (thin clouds).
    Zero values stay zero.
    """
    return np.where(
        density > 0,
        (255.0 * ((density.astype(np.float32) / 255.0) ** gamma)).astype(np.uint8),
        np.uint8(0)
    )


def _downsample(density: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Downsample density to target equirectangular size via LANCZOS."""
    from PIL import Image
    src_h, src_w = density.shape
    if src_w == target_w and src_h == target_h:
        return density
    img = Image.fromarray(density, mode='L')
    img = img.resize((target_w, target_h), Image.LANCZOS)
    logger.info(f"Downsampled: {src_w}×{src_h} → {target_w}×{target_h}")
    return np.array(img, dtype=np.uint8)


def _reference_size(floor_ts: datetime) -> int:
    """Content-Length of the currently published file, or 0 if unknown.

    The live file is the right reference for the size heuristic because it is
    known to have passed the completeness gate, so the derived threshold
    tracks any change in NASA's output size instead of hard-coding one.
    """
    if floor_ts is None:
        return 0
    sig = _head_gcc(_gcc_url(floor_ts))
    if sig is None or sig[0] is None:
        return 0
    try:
        return int(sig[0])
    except (TypeError, ValueError):
        return 0


def _size_floor(floor_ts: datetime) -> int:
    """Byte threshold below which a candidate is assumed truncated (0 = off)."""
    if GCC_SIZE_FILTER_RATIO <= 0:
        return 0
    ref = _reference_size(floor_ts)
    if ref <= 0:
        logger.info("  没有可用的参考大小（线上文件未知），本次不做大小预筛")
        return 0
    return int(ref * GCC_SIZE_FILTER_RATIO)


def _walk_candidates(candidates, size_floor: int):
    """Try candidates newest-first until one passes the gate.

    Returns (chosen, tried, skipped_by_size, n_seen, n_existing). ``chosen`` is
    the tuple the caller wants, or None. ``n_existing`` counts candidates that
    actually had a file (a successful HEAD), which is what the caller needs to
    tell "the size filter rejected everything" from "everything really was
    damaged".
    """
    chosen = None
    tried = []
    skipped = []
    n_seen = 0
    n_existing = 0
    for dt, url in candidates:
        n_seen += 1
        sig = _head_gcc(url)
        if sig is None or sig[0] is None:
            logger.info(f"  {dt:%Y-%m-%d %H:%M}Z 不存在，继续向前搜索")
            continue
        n_existing += 1
        try:
            size = int(sig[0])
        except (TypeError, ValueError):
            size = 0
        if size_floor and size and size < size_floor:
            logger.info(
                f"  {dt:%Y-%m-%d %H:%M}Z 只有 {size / 1e6:.0f} MB，低于阈值 "
                f"{size_floor / 1e6:.0f} MB（参考线上文件），判定截断，跳过")
            skipped.append(dt)
            continue
        logger.info(f"  → 尝试 {dt:%Y-%m-%d %H:%M}Z ({size / 1e6:.0f} MB)")
        try:
            bt_k, cloud_phase, lat_src, lon_src, health = read_gcc_bt(
                url, enforce=HEALTH_ENFORCE, before_sig=sig)
        except IncompleteGCCError as e:
            logger.warning(f"  ✗ {dt:%Y-%m-%d %H:%M}Z BT_10.8um 不完整：{e}")
            tried.append(dt)
            continue
        except Exception as e:
            logger.warning(f"  ✗ {dt:%Y-%m-%d %H:%M}Z 读取失败："
                           f"{type(e).__name__}: {e}")
            tried.append(dt)
            continue
        chosen = (dt, bt_k, cloud_phase, lat_src, lon_src, health)
        break
    return chosen, tried, skipped, n_seen, n_existing


def load_gcc_density(target_w: int = 5000,
                     target_h: int = 2500,
                     max_hours_back: int = SEARCH_HOURS,
                     use_cloud_phase: bool = True,
                     floor_ts: datetime = None,
                     bootstrap: bool = False) -> tuple:
    """Main entry: find newest *complete* GCC → BT → density → downsample.

    Walks candidate hours newest-first, and for each one validates
    BT_10.8um's spatial coverage before accepting it. A candidate that is
    still being written (large regions of _FillValue) is rejected and the
    walk continues to the previous hour, stopping once the candidate is no
    longer newer than ``floor_ts`` (the newest data already published).

    Args:
        target_w, target_h: Output equirectangular dimensions.
        max_hours_back: Search window in hours.
        use_cloud_phase: Whether to use cloud_phase to zero out clear pixels.
        floor_ts: Exclusive lower bound for the backward walk; pass the
            timestamp read from root.json. None disables the bound.
        bootstrap: First run of this pipeline against data published by the
            old one. Drops the floor — the live version is known to be ungated
            (it may itself be a half-written file, which would otherwise block
            every healthy older candidate forever), so pick the newest file
            that passes the gate and publish it. Still bounded by
            ``max_hours_back``, which is what stops it publishing genuinely
            stale data.

    Returns:
        (density, lat_grid, lon_grid, timestamp_str)

    Raises:
        NoHealthyGCCError: no candidate in the allowed window had a complete
            BT_10.8um. The caller must abort without publishing anything.
    """
    # 1. Walk candidate hours newest-first until one validates.
    if bootstrap:
        logger.warning(
            f"初次运行（线上数据由旧版管线发布，无本版本标记）：忽略已发布下界，"
            f"在最近 {max_hours_back}h 内取最新的健康文件强行发布 —— "
            f"此后恢复正常的下界约束")
    # bootstrap only drops the floor; the window is the same either way.
    walk_floor = None if bootstrap else floor_ts

    def _candidates():
        # Fresh generator per pass: iter_candidate_files yields lazily and a
        # consumed generator cannot be replayed.
        return iter_candidate_files(walk_floor, max_hours_back)

    size_floor = _size_floor(walk_floor)
    if size_floor:
        logger.info(f"  大小预筛开启：低于 {size_floor / 1e6:.0f} MB 的候选"
                    f"（参考线上文件）直接跳过，不下载")
    chosen, tried, skipped, n_candidates, n_existing = _walk_candidates(
        _candidates(), size_floor)

    # Safety valve. The size threshold is a heuristic; if NASA ever changes its
    # output size, a stale threshold would skip every candidate and the
    # pipeline would silently stop publishing.
    #
    # The trigger is deliberately narrow: only when the filter skipped EVERY
    # candidate that existed. If even one file was downloaded and rejected on
    # its content, the gate is doing its job and the filter was not the thing
    # standing in the way — re-walking then would just double the run for
    # nothing (measured: it did exactly that on 2026-09-16 17:30).
    #
    # So the filter may delay a publish, never prevent one.
    if chosen is None and skipped and len(skipped) == n_existing:
        logger.warning(
            f"  大小预筛把 {n_existing} 个存在的候选全部跳过了且没有找到健康文件 "
            f"—— 关闭预筛重新完整校验（阈值可能已过时）")
        chosen, tried2, _, n2, _ = _walk_candidates(_candidates(), 0)
        tried = tried + tried2
        n_candidates += n2

    if chosen is None:
        bound = (floor_ts.strftime('%Y-%m-%d %H:%M') + 'Z') if floor_ts else '无'
        if n_candidates == 0:
            # Benign: the published timestamp already covers the whole
            # searchable window, so there is simply nothing newer to publish.
            raise NoHealthyGCCError(
                f"没有可搜索的候选：线上已发布的时间戳 {bound} 已经覆盖了整个"
                f"搜索窗口，没有更新的文件可发布 —— 本次无需更新")
        detail = ", ".join(f"{d:%m-%d %H:%M}Z" for d in tried) or "（均无法读取）"
        raise NoHealthyGCCError(
            f"在允许范围内找不到 BT_10.8um 完整的 GCC 文件。"
            f"下界={bound}，共 {n_candidates} 个候选，其中 "
            f"{len(tried)} 个不完整/不可用：{detail}")

    dt, bt_k, cloud_phase, lat_src, lon_src, health = chosen
    timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if tried:
        logger.info(f"选中 GCC {dt:%Y-%m-%d %H:%M}Z（{timestamp_str}，"
                    f"数据龄 {age_h:.1f}h）—— 向前回退跳过了 "
                    f"{len(tried)} 个不完整/不可用文件")
    else:
        logger.info(f"选中 GCC {dt:%Y-%m-%d %H:%M}Z（{timestamp_str}，"
                    f"数据龄 {age_h:.1f}h）—— 最新定稿文件即完整")

    # 3. BT → density (with cloud_phase filtering)
    density = _bt_to_density(bt_k, cloud_phase,
                             use_cloud_phase=use_cloud_phase,
                             lat=lat_src)
    logger.info(f"Density: {density.min()}~{density.max()}, "
                f"zeros={(density == 0).sum() / density.size * 100:.1f}%")

    # 4. Gamma correction
    density = _gamma_correct(density, gamma=0.45)
    logger.info(f"Gamma (0.45): non-zero mean={density[density > 0].mean():.1f}")

    # 5. Downsample
    if target_w and target_h:
        density = _downsample(density, target_w, target_h)

    # 6. Build lat/lon grids for downsampled output
    lat_grid = np.linspace(90.0, -90.0, target_h, dtype=np.float64)
    lon_grid = np.linspace(-180.0, 180.0, target_w, dtype=np.float64)

    return density, lat_grid, lon_grid, timestamp_str
