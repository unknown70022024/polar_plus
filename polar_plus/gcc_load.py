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
from datetime import datetime, timezone, timedelta

import numpy as np

from polar_plus.config import GCC_V2A_BASE, BT_WARM, BT_COLD, SEARCH_HOURS

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


def find_latest_gcc(max_hours_back: int = SEARCH_HOURS) -> tuple:
    """Scan recent hours for latest available GCC v2a file.

    GCC v2a data is published hourly (HH:00) with ~2h latency.

    Returns:
        (datetime, url) or (None, None) if nothing found.
    """
    now = datetime.now(timezone.utc)
    skipped = 0
    for hours_ago in range(max_hours_back + 1):
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
                        f"(skipped {skipped} newer unavailable slots, "
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


def read_gcc_bt(url: str) -> tuple:
    """Remote-read BT_10.8um + cloud_phase from GCC v2a NetCDF via h5netcdf.

    Only downloads compressed chunks (~96 MB total network transfer).

    Returns:
        bt_k: (6480, 12960) float32 Kelvin
        cloud_phase: (6480, 12960) float32, 0=clear, 1=liquid, 2=ice, ...
        lat: (6480,) float64, 89.99 ~ -89.99
        lon: (12960,) float64, -179.99 ~ 179.99
    """
    import xarray as xr
    from time import time

    logger.info(f"Opening GCC: {url}")
    ds = xr.open_dataset(url, decode_times=False, engine='h5netcdf')

    t0 = time()
    bt = ds['BT_10.8um'].values.squeeze()      # (6480, 12960)
    cp = ds['cloud_phase'].values.squeeze()     # (6480, 12960)
    lat = ds.coords['lat'].values               # (6480,)
    lon = ds.coords['lon'].values               # (12960,)
    elapsed = time() - t0

    logger.info(
        f"Read BT_10.8um+phase: {bt.shape} in {elapsed:.0f}s, "
        f"BT {np.nanmin(bt):.1f}~{np.nanmax(bt):.1f}K, "
        f"cloud={(cp >= 1).sum() / cp.size * 100:.0f}%"
    )
    ds.close()
    return bt, cp, lat, lon


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
