"""
polar_plus/capfill.py — SSEC polar gap-fill for GCC.

Downloads SSEC RealEarth Mercator tiles + bbox near-pole patches
matching the GCC timestamp, then fills only pixels where GCC has
missing data (density == 0) in polar regions (|lat| > threshold).
"""
import io
import logging
import math
import urllib.request
from datetime import datetime

import numpy as np
from PIL import Image

from polar_plus.config import (
    SSEC_API_BASE, SSEC_WMS_URL,
    GAP_LAT_THRESHOLD, FEATHER_WIDTH,
    GAP_DENSITY_THRESHOLD, GAP_FEATHER_PX,
    MERCATOR_LAT_HIGH, BBOX_LAT_TOP,
    TILE_Z, TILE_SIZE, N_TILES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSEC tile download helpers
# ---------------------------------------------------------------------------

def _iso_time(timestamp_str: str) -> str:
    """Convert 'YYYYMMDD_HHMMSS' → 'YYYY-MM-DDTHH:MM:SSZ'."""
    dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tile_url(x: int, y: int, iso_time: str = "", api_key: str = "") -> str:
    """Build SSEC API tile URL, optionally with time parameter."""
    url = (f"{SSEC_API_BASE}?products=globalir"
           f"&z={TILE_Z}&x={x}&y={y}&width={TILE_SIZE}&height={TILE_SIZE}")
    if iso_time:
        url += f"&time={iso_time}"
    if api_key:
        url += f"&accesskey={api_key}"
    return url


def download_tile(x: int, y: int, iso_time: str = "",
                  api_key: str = "") -> np.ndarray:
    """Download one SSEC API tile → (256, 256) uint8."""
    req = urllib.request.Request(_tile_url(x, y, iso_time, api_key))
    with urllib.request.urlopen(req, timeout=30) as resp:
        img = Image.open(io.BytesIO(resp.read())).convert('L')
    return np.array(img, dtype=np.uint8)


def download_full_mercator(iso_time: str = "",
                           api_key: str = "") -> np.ndarray:
    """Download 4×4 = 16 Mercator tiles → 1024×1024."""
    rows = []
    for y in range(N_TILES):
        row = [download_tile(x, y, iso_time, api_key) for x in range(N_TILES)]
        rows.append(np.concatenate(row, axis=1))
    return np.concatenate(rows, axis=0)


def _bbox_url(lat_min: float, lat_max: float, iso_time: str = "",
              api_key: str = "", w: int = 1024) -> str:
    """Build SSEC WMS bbox URL, optionally with time parameter."""
    span = lat_max - lat_min
    h = max(1, int(round(w * span / 360.0)))
    url = (f"{SSEC_WMS_URL}"
           f"&BBOX=-180,{lat_min},180,{lat_max}"
           f"&WIDTH={w}&HEIGHT={h}")
    if iso_time:
        url += f"&TIME={iso_time}"
    if api_key:
        url += f"&accesskey={api_key}"
    return url


def download_bbox_cap(lat_min: float, lat_max: float,
                      iso_time: str = "", api_key: str = "",
                      w: int = 1024) -> np.ndarray:
    """Download SSEC WMS bbox equirectangular polar cap patch → (h, w) uint8."""
    req = urllib.request.Request(_bbox_url(lat_min, lat_max, iso_time, api_key, w))
    with urllib.request.urlopen(req, timeout=30) as resp:
        img = Image.open(io.BytesIO(resp.read())).convert('L')
    logger.info(f"  Bbox [{lat_min:.0f}, {lat_max:.0f}] → {img.size[0]}×{img.size[1]}, "
                f"mean={np.array(img).mean():.0f}")
    return np.array(img, dtype=np.uint8)


# ---------------------------------------------------------------------------
# Mercator → equirectangular reprojection
# ---------------------------------------------------------------------------

def _lat_to_my(lat: float) -> float:
    """Latitude → Mercator y pixel (z=2, 1024 px full space)."""
    phi = math.radians(lat)
    return (1.0 - math.log(math.tan(math.pi / 4.0 + phi / 2.0)) / math.pi) / 2.0 * N_TILES * TILE_SIZE


def mercator_to_equirect(merc: np.ndarray, lat_min: float, lat_max: float,
                         target_w: int) -> np.ndarray:
    """Reproject Mercator strip → equirectangular band."""
    mh, mw = merc.shape
    total_px = N_TILES * TILE_SIZE  # 1024
    band_deg = lat_max - lat_min
    target_h = max(1, int(round(band_deg / 180.0 * target_w * 0.5)))
    result = np.zeros((target_h, target_w), dtype=np.uint8)

    for ty in range(target_h):
        lat = lat_max - (ty + 0.5) / target_h * band_deg
        my = _lat_to_my(lat)
        if my < 1 or my >= mh - 2:
            continue
        for tx in range(target_w):
            lon = -180.0 + (tx + 0.5) / target_w * 360.0
            mx = (lon + 180.0) / 360.0 * total_px
            ix = int(mx) % mw
            fx = mx - math.floor(mx)
            iy = int(my)
            fy = my - iy
            ix1 = (ix + 1) % mw
            iy1 = min(iy + 1, mh - 1)
            v00 = float(merc[iy, ix])
            v10 = float(merc[iy, ix1])
            v01 = float(merc[iy1, ix])
            v11 = float(merc[iy1, ix1])
            val = (v00 * (1.0 - fx) * (1.0 - fy) + v10 * fx * (1.0 - fy) +
                   v01 * (1.0 - fx) * fy + v11 * fx * fy)
            result[ty, tx] = max(0, min(255, int(round(val))))
    return result


# ---------------------------------------------------------------------------
# SSEC composite builder (polar bands only)
# ---------------------------------------------------------------------------

def _row_for_lat(lat: float, target_h: int) -> int:
    """Row index for a given latitude in an equirectangular image."""
    return int(round((90.0 - lat) / 180.0 * target_h))


def build_ssec_polar(gcc_timestamp: str, target_w: int, target_h: int,
                     api_key: str = "") -> np.ndarray:
    """Build full-resolution SSEC polar composite (60°–89.9° N+S).

    Returns:
        ssec: (target_h, target_w) uint8 — SSEC data in polar bands, 0 elsewhere.
    """
    iso_time = _iso_time(gcc_timestamp)
    logger.info(f"Building SSEC polar composite for {iso_time}...")

    # 1. Full Mercator (used for 60-85° bands)
    logger.info("Downloading full Mercator (z=2, 16 tiles)...")
    try:
        full_merc = download_full_mercator(iso_time, api_key)
    except Exception as e:
        logger.warning(f"Mercator download failed: {e}, trying without time param")
        full_merc = download_full_mercator("", api_key)
    logger.info(f"Full Mercator: {full_merc.shape}")

    # 2. Reproject north + south 60-85°
    north_eq = mercator_to_equirect(full_merc, GAP_LAT_THRESHOLD, MERCATOR_LAT_HIGH, target_w)
    south_eq = mercator_to_equirect(full_merc, -MERCATOR_LAT_HIGH, -GAP_LAT_THRESHOLD, target_w)

    # 3. Bbox near-pole patches 85°–89.9°
    logger.info("Downloading north bbox cap 85-89.9°...")
    try:
        north_cap = download_bbox_cap(MERCATOR_LAT_HIGH, BBOX_LAT_TOP, iso_time, api_key, target_w)
    except Exception as e:
        logger.warning(f"North cap download failed: {e}, trying without time param")
        north_cap = download_bbox_cap(MERCATOR_LAT_HIGH, BBOX_LAT_TOP, "", api_key, target_w)

    logger.info("Downloading south bbox cap -89.9° to -85°...")
    try:
        south_cap = download_bbox_cap(-BBOX_LAT_TOP, -MERCATOR_LAT_HIGH, iso_time, api_key, target_w)
    except Exception as e:
        logger.warning(f"South cap download failed: {e}, trying without time param")
        south_cap = download_bbox_cap(-BBOX_LAT_TOP, -MERCATOR_LAT_HIGH, "", api_key, target_w)

    # 4. Scale to target grid
    north_rows = _row_for_lat(GAP_LAT_THRESHOLD, target_h) - _row_for_lat(MERCATOR_LAT_HIGH, target_h)
    south_rows = _row_for_lat(-MERCATOR_LAT_HIGH, target_h) - _row_for_lat(-GAP_LAT_THRESHOLD, target_h)
    cap_n_rows = _row_for_lat(MERCATOR_LAT_HIGH, target_h) - _row_for_lat(BBOX_LAT_TOP, target_h)
    # Anchor each polar cap to its image edge (the pole) and derive its height
    # from the 85° row, so cap and Mercator band are always adjacent. The old
    # code anchored the north cap one row down ("89.9°" → row 1) and derived
    # the south cap height by subtraction; both left a one-row gap that stayed
    # permanently zero in the composite -- row 0 (+90.0°, the pole itself) and
    # row 2431 (-85.03°, exactly where the two southern sources meet). A GCC
    # hole landing on either row was "filled" with 0, i.e. a black line at the
    # pole and at -85°S.
    r_n = 0
    cap_n_rows = _row_for_lat(MERCATOR_LAT_HIGH, target_h)
    r_s = _row_for_lat(-MERCATOR_LAT_HIGH, target_h)
    cap_s_rows = target_h - r_s
    # The four bands must tile [0, target_h) with no gap and no overlap.
    assert r_n == 0 and r_s + cap_s_rows == target_h
    assert r_n + cap_n_rows == _row_for_lat(MERCATOR_LAT_HIGH, target_h)
    assert _row_for_lat(-GAP_LAT_THRESHOLD, target_h) + south_rows == r_s

    north_big = np.array(Image.fromarray(north_eq).resize((target_w, north_rows), Image.LANCZOS))
    south_big = np.array(Image.fromarray(south_eq).resize((target_w, south_rows), Image.LANCZOS))
    cap_n_big = np.array(Image.fromarray(north_cap).resize((target_w, cap_n_rows), Image.LANCZOS))
    cap_s_big = np.array(Image.fromarray(south_cap).resize((target_w, cap_s_rows), Image.LANCZOS))

    ssec = np.zeros((target_h, target_w), dtype=np.uint8)

    # North: 85-89.9° cap + 60-85° Mercator
    ssec[r_n:r_n + cap_n_rows, :] = cap_n_big
    r_m_n = _row_for_lat(MERCATOR_LAT_HIGH, target_h)
    ssec[r_m_n:r_m_n + north_rows, :] = north_big

    # South: -89.9°~-85° cap (bottom-aligned) + -85°~-60° Mercator
    ssec[r_s:r_s + cap_s_rows, :] = cap_s_big
    r_m_s = _row_for_lat(-GAP_LAT_THRESHOLD, target_h)
    ssec[r_m_s:r_m_s + south_rows, :] = south_big

    logger.info(f"SSEC polar composite: mean={ssec[ssec > 0].mean():.1f}, "
                f"nonzero={(ssec > 0).sum() / ssec.size * 100:.1f}%")
    return ssec


# ---------------------------------------------------------------------------
# Gap-fill with feathering
# ---------------------------------------------------------------------------

def _dilate8(mask: np.ndarray) -> np.ndarray:
    """8-connected dilation by one pixel (no wrap-around at the poles)."""
    h, w = mask.shape
    p = np.pad(mask, 1, mode='edge')
    out = mask.copy()
    for dy in range(3):
        for dx in range(3):
            out |= p[dy:dy + h, dx:dx + w]
    return out


def _fill_alpha(mask: np.ndarray, px: int) -> np.ndarray:
    """Blend weight map for the gap-fill.

    1.0 everywhere inside ``mask`` (full SSEC), then a cosine ramp decaying
    outwards, reaching exactly 0.0 at ``px`` pixels out. Without this the
    fill ends on a hard step: inside is bright SSEC, outside is GCC, and the
    two meet with no transition. The ramp drags a little SSEC into the GCC
    side so the junction is a slope rather than an edge.

    Deliberately asymmetric -- the ramp only ever goes OUTWARDS. Ramping
    inwards would blend the hole's own near-zero GCC values (1..44, the very
    ramp this fix exists for) back into the fill and re-create the dark ring.
    """
    alpha = np.zeros(mask.shape, dtype=np.float32)
    alpha[mask] = 1.0
    covered = mask.copy()
    frontier = mask
    for k in range(1, px + 1):
        frontier = _dilate8(frontier) & ~covered
        if not frontier.any():
            break
        alpha[frontier] = 0.5 * (1.0 + math.cos(math.pi * k / px))
        covered |= frontier
    return alpha


def fill_gcc_gaps(gcc_density: np.ndarray,
                  lat_grid: np.ndarray,
                  lon_grid: np.ndarray,
                  gcc_timestamp: str,
                  api_key: str = "") -> np.ndarray:
    """Fill holes in GCC polar regions with time-matched SSEC data.

    Algorithm:
      1. Identify holes: (density < GAP_DENSITY_THRESHOLD) AND (|lat| > 60°).
         The threshold matches the one run._post_process() zeroes out, so the
         fill covers exactly the pixels that would otherwise render black.
      2. Build SSEC polar composite matching the GCC timestamp
      3. Fill the hole pixels with SSEC data, with a cosine ramp just outside
         the hole boundary so the two sources meet without a step
      4. Fade the whole fill in over FEATHER_WIDTH degrees past 60°

    If SSEC download fails entirely, returns GCC unchanged (with holes).

    Args:
        gcc_density: (H, W) uint8 GCC cloud density
        lat_grid: (H,) float64
        lon_grid: (W,) float64
        gcc_timestamp: "YYYYMMDD_HHMMSS"
        api_key: optional SSEC API key

    Returns:
        density: (H, W) uint8 with polar gaps filled
    """
    target_h, target_w = gcc_density.shape

    # 1. Identify polar holes. `< GAP_DENSITY_THRESHOLD` (not `== 0`) on
    #    purpose: it is the same predicate run._post_process() uses to decide
    #    what is black, so the fill boundary lands exactly on the visible hole
    #    boundary. See GAP_DENSITY_THRESHOLD in config.py.
    lat_abs = np.abs(lat_grid)
    polar_mask = lat_abs > GAP_LAT_THRESHOLD         # (H,) boolean
    gap_mask = (gcc_density < GAP_DENSITY_THRESHOLD) & polar_mask[:, np.newaxis]

    gap_count = gap_mask.sum()
    polar_total = polar_mask.sum() * target_w
    logger.info(f"GCC polar holes (<{GAP_DENSITY_THRESHOLD}, >|{GAP_LAT_THRESHOLD:.0f}|°): "
                f"{gap_count}/{polar_total} pixels ({gap_count / max(polar_total, 1) * 100:.1f}%)")

    if gap_count == 0:
        logger.info("No polar gaps — skipping SSEC")
        return gcc_density

    # 2. Build SSEC composite
    try:
        ssec = build_ssec_polar(gcc_timestamp, target_w, target_h, api_key)
    except Exception as e:
        logger.warning(f"SSEC build failed: {e} — returning GCC with polar holes")
        return gcc_density

    # 3. Background subtraction on SSEC (remove non-cloud floor)
    ssec_nonzero = ssec[ssec > 0]
    if len(ssec_nonzero) > 0:
        bg = int(np.percentile(ssec_nonzero, 5))
        if bg > 0:
            ssec = np.clip(ssec.astype(np.int16) - bg, 0, 255).astype(np.uint8)
            logger.info(f"SSEC bg subtract: floor={bg}")

    # 4. LUT histogram match SSEC → GCC in overlap zone (55-65°)
    #    Use the region where both GCC and SSEC have data
    overlap_mask = (lat_abs >= 55) & (lat_abs <= 65)
    ssec_overlap = ssec[overlap_mask]
    gcc_overlap = gcc_density[overlap_mask]
    valid = (ssec_overlap > 0) & (gcc_overlap > 0)
    if valid.sum() > 100:
        # Build CDF-matching LUT
        bins = 256
        sh = np.histogram(ssec_overlap[valid], bins=bins, range=(0, 255))[0].astype(np.float32)
        gh = np.histogram(gcc_overlap[valid], bins=bins, range=(0, 255))[0].astype(np.float32)
        sc = np.cumsum(sh) / sh.sum()
        gc = np.cumsum(gh) / gh.sum()
        lut = np.array([np.argmin(np.abs(gc - sc[i])) for i in range(bins)], dtype=np.uint8)
        ssec = lut[ssec]
        logger.info(f"LUT match: ssec {ssec_overlap[valid].mean():.0f}→{gcc_overlap[valid].mean():.0f}")
    else:
        logger.warning(f"LUT skip: only {valid.sum()} overlap samples")

    # 5. Composite SSEC into GCC over the holes, with a cosine ramp just
    #    outside the hole boundary so the two sources meet in a slope.
    alpha = _fill_alpha(gap_mask, GAP_FEATHER_PX)

    #    The SSEC composite only exists for |lat| > 60°, so an outward ramp
    #    crossing that line would multiply real mid-latitude GCC data by
    #    (1 - alpha) and darken it into a band. The latitude ramp keeps the
    #    blend at zero until the fill is genuinely inside SSEC coverage and
    #    fades it in over FEATHER_WIDTH degrees (the role the old row-wise
    #    feather played).
    ramp = np.clip((lat_abs - GAP_LAT_THRESHOLD) / FEATHER_WIDTH, 0.0, 1.0)
    ramp = (1.0 - np.cos(ramp * np.pi)) / 2.0
    alpha *= ramp[:, np.newaxis]

    result = np.rint(gcc_density.astype(np.float32) * (1.0 - alpha)
                     + ssec.astype(np.float32) * alpha)
    result = np.clip(result, 0, 255).astype(np.uint8)
    logger.info(f"Filled {gap_count} hole pixels with SSEC "
                f"({GAP_FEATHER_PX}px cosine feather at the fill edge)")

    logger.info(f"Final: zeros={(result == 0).sum() / result.size * 100:.1f}% "
                f"(was {(gcc_density == 0).sum() / gcc_density.size * 100:.1f}%)")
    return result
