"""
polar_plus/gcc_load.py — NASA SatCORPS Global Cloud Composite (GCC) v2a

Single-source global cloud cover, 90N-90S. Replaces GMGSI.
Remote partial-read via h5netcdf — only downloads compressed chunks
of BT_10.8um + cloud_phase (~96 MB), not the entire ~1.27 GB file.

Data:
  - Source: NASA Langley SatCORPS
  - URL: satcorps.larc.nasa.gov/prod/GCC-GEO-LEO/v2a/visst-pixel-netcdf/
  - Grid: 12960×6480 regular equirectangular (~3 km at equator), 90N to -90S
  - Variables: BT_10.8um (10.8μm brightness temp, uint16+scale_factor 0.01)
               cloud_phase (uint8, 0=clear, 1=liquid, 2=ice, 3+=other)
  - Latency: ~2h (GCC hourly updates, ~2h behind real-time)
  - Format: NetCDF4/HDF5 with chunked zlib compression
"""
import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np

from polar_plus.config import (GCC_V2A_BASE, BT_WARM, BT_COLD, SEARCH_HOURS,
                               MIN_AGE_HOURS)

logger = logging.getLogger(__name__)


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


def find_latest_gcc(max_hours_back: int = SEARCH_HOURS,
                    min_age_hours: int = MIN_AGE_HOURS) -> tuple:
    """Scan recent hours for the latest FINALISED GCC v2a file.

    GCC v2a files are published hourly (HH:00) but keep growing for ~2h
    as late-arriving LEO granules are appended (the file for hour H is
    finalised around H+2h). Reading a younger, still-assembling file
    returns corrupt chunks and large rectangular holes, so we skip any
    file younger than min_age_hours.

    Returns:
        (datetime, url) or (None, None) if nothing found.
    """
    now = datetime.now(timezone.utc)
    skipped = 0
    for hours_ago in range(min_age_hours, max_hours_back + 1):
        dt = now - timedelta(hours=hours_ago)
        dt = dt.replace(minute=0, second=0, microsecond=0)
        url = _gcc_url(dt)
        try:
            import urllib.request
            req = urllib.request.Request(url, method='HEAD')
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info(
                        f"Found GCC: {dt.strftime('%Y-%m-%d %H:%M')}Z "
                        f"(skipped {skipped} newer assembling slots, "
                        f"searched back {hours_ago}h)"
                    )
                    return dt, url
                else:
                    skipped += 1
        except Exception:
            skipped += 1
            continue
    logger.warning(f"No GCC data found in past {max_hours_back}h ({skipped} slots checked)")
    return None, None


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


def _wait_stable(url: str, max_wait: int = 180, interval: int = 8) -> tuple:
    """Wait until two consecutive HEAD signatures match.

    NASA regenerates the hourly composite in place (late-arriving LEO
    granules are appended), so the file grows while it is being written.
    Reading a mid-regeneration file produces corrupt chunks and large
    rectangular holes. This gate waits until the file stops changing
    before returning its signature.
    """
    prev = _head_gcc(url)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(interval)
        cur = _head_gcc(url)
        if cur is not None and cur == prev:
            return cur
        if cur is not None and prev is not None:
            logger.info(
                f"GCC file changing, waiting for stability... ({prev} -> {cur})"
            )
        prev = cur
    logger.warning(f"GCC file did not stabilise within {max_wait}s")
    return prev


def _read_needed_vars(url: str) -> tuple:
    """Read only BT_10.8um + cloud_phase + lat/lon via h5py.

    Uses h5py directly instead of xarray's open_dataset, which decodes
    every variable (including the variable-length string granule_name_list
    that triggers fragile global-heap reads over HTTP). Only the ~100 MB
    of compressed chunks for the two cloud fields are transferred.
    """
    import fsspec
    import h5py

    t0 = time.time()
    fs = fsspec.filesystem("http", block_size=1024 * 1024)  # 1 MB blocks
    with fs.open(url, "rb") as f:
        with h5py.File(f, "r") as h:
            bt_raw = h['BT_10.8um'][0, :, :]    # (6480, 12960) uint16
            cp_raw = h['cloud_phase'][0, :, :]  # (6480, 12960) uint8
            lat = h['lat'][:]                   # (6480,)
            lon = h['lon'][:]                   # (12960,)

    # scale_factor + _FillValue / valid_range -> float Kelvin + NaN
    bt = bt_raw.astype(np.float32) * 0.01
    bt[(bt_raw == 65535) | (bt_raw < 18000) | (bt_raw > 40000)] = np.nan
    cp = cp_raw.astype(np.float32)
    cp[(cp_raw == 127) | (cp_raw > 13)] = np.nan

    logger.info(
        f"Read BT_10.8um+phase: {bt.shape} in {time.time() - t0:.0f}s, "
        f"BT {np.nanmin(bt):.1f}~{np.nanmax(bt):.1f}K, "
        f"cloud={(cp >= 1).sum() / cp.size * 100:.0f}%"
    )
    return bt, cp, lat, lon


def read_gcc_bt(url: str, max_retries: int = 3) -> tuple:
    """Read BT_10.8um + cloud_phase with a stability gate + retry.

    Flow:
      1. Wait until the file stops changing (stability gate).
      2. Read only the needed variables (h5py direct).
      3. HEAD again: if the signature changed, the file was rewritten
         mid-read -> discard and retry.

    Returns:
        bt_k: (6480, 12960) float32 Kelvin (NaN where invalid)
        cloud_phase: (6480, 12960) float32, 0=clear, 1=liquid, 2=ice, ...
        lat: (6480,) float32
        lon: (12960,) float32
    """
    for attempt in range(max_retries):
        before = _wait_stable(url)
        try:
            bt, cp, lat, lon = _read_needed_vars(url)
        except Exception as e:
            logger.warning(f"Read attempt {attempt + 1}/{max_retries} failed: {e}")
            time.sleep(2 ** attempt)
            continue
        after = _head_gcc(url)
        if after == before:
            return bt, cp, lat, lon
        logger.warning(
            f"GCC file changed during read (attempt {attempt + 1}/{max_retries}), "
            f"retrying..."
        )
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


def load_gcc_density(target_w: int = 5000,
                     target_h: int = 2500,
                     max_hours_back: int = SEARCH_HOURS,
                     use_cloud_phase: bool = True) -> tuple:
    """Main entry: find latest GCC → read BT → convert to density → downsample.

    Args:
        target_w, target_h: Output equirectangular dimensions.
        max_hours_back: Search window in hours.
        use_cloud_phase: Whether to use cloud_phase to zero out clear pixels.

    Returns:
        (density, lat_grid, lon_grid, timestamp_str)
        density: (target_h, target_w) uint8 cloud density
        lat_grid: (target_h,) float64
        lon_grid: (target_w,) float64
        timestamp_str: "YYYYMMDD_HHMMSS" format
    """
    # 1. Find latest file
    dt, url = find_latest_gcc(max_hours_back)
    if dt is None:
        raise RuntimeError(
            f"No GCC v2a data found in past {max_hours_back}h")

    timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
    logger.info(f"Latest GCC: {dt.strftime('%Y-%m-%d %H:%M')}Z — {timestamp_str}")

    # 2. Remote-read BT + cloud_phase
    bt_k, cloud_phase, lat_src, lon_src = read_gcc_bt(url)

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
