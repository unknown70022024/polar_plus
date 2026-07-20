"""
polar_plus/capfill.py — API Mercator 瓦片 (z=2) + bbox 极顶三级拼接

下载全 1024×1024 Mercator（16 瓦片）重投影 55-85°，
再用 bbox 等距柱面填充 85-89° 极顶。
"""
import logging
import io
import math
import urllib.request
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

API_BASE = "http://re.ssec.wisc.edu/api/image"
TILE_Z = 2
TS = 256
N_TILES = 2 ** TILE_Z  # 4


def _tile_url(x: int, y: int, api_key: str = "") -> str:
    url = f"{API_BASE}?products=globalir&z={TILE_Z}&x={x}&y={y}&width={TS}&height={TS}"
    if api_key:
        url += f"&accesskey={api_key}"
    return url


def download_tile(x: int, y: int, api_key: str = "") -> np.ndarray:
    """下载 API 瓦片 → (256, 256) uint8"""
    req = urllib.request.Request(_tile_url(x, y, api_key))
    with urllib.request.urlopen(req, timeout=30) as resp:
        img = Image.open(io.BytesIO(resp.read())).convert('L')
    return np.array(img, dtype=np.uint8)


def download_full_mercator(api_key: str = "") -> np.ndarray:
    """下载全部 4×4 = 16 瓦片 → 1024×1024 全 Mercator"""
    rows = []
    for y in range(N_TILES):
        row = [download_tile(x, y, api_key) for x in range(N_TILES)]
        rows.append(np.concatenate(row, axis=1))
    return np.concatenate(rows, axis=0)


def download_cap_equirect(lat_min: float, lat_max: float,
                          api_key: str = "") -> np.ndarray:
    """下载 bbox 等距柱面极顶补丁 → (h, 1024) uint8"""
    span = lat_max - lat_min
    w = 1024
    h = max(1, int(round(w * span / 360)))
    url = (f"{API_BASE}?products=globalir"
           f"&bbox=-180,{lat_min},180,{lat_max}&width={w}&height={h}")
    if api_key:
        url += f"&accesskey={api_key}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        img = Image.open(io.BytesIO(resp.read())).convert('L')
    logger.info(f"  Cap bbox [{lat_min:.0f}, {lat_max:.0f}] → {w}×{h}, "
                f"mean={np.mean(img):.0f}")
    return np.array(img, dtype=np.uint8)


def _lat_to_my(lat: float) -> float:
    """纬度 → Mercator y 像素坐标 (z=2, 全 1024px 空间)"""
    n = float(N_TILES)
    phi = math.radians(lat)
    return (1 - math.log(math.tan(math.pi / 4 + phi / 2)) / math.pi) / 2 * n * TS


def mercator_to_equirect(merc: np.ndarray, lat_min: float, lat_max: float,
                         target_w: int) -> np.ndarray:
    """
    Mercator 极区 → 等距柱面条带
    merc: 全 1024×1024 Mercator（总像素 = n*TS）
    输出: (h, target_w) 等距柱面，覆盖 lat_min~lat_max
    """
    mh, mw = merc.shape
    n = float(N_TILES)
    total_px = n * TS  # 1024
    band_deg = lat_max - lat_min
    target_h = max(1, int(round(band_deg / 180.0 * target_w * 0.5)))
    result = np.zeros((target_h, target_w), dtype=np.uint8)

    for ty in range(target_h):
        lat = lat_max - (ty + 0.5) / target_h * band_deg
        for tx in range(target_w):
            lon = -180.0 + (tx + 0.5) / target_w * 360.0
            my = _lat_to_my(lat)
            # 全 Mercator: mh=1024, 所有有效纬度 my 在 0~1023
            if my < 1 or my >= mh - 2:
                continue
            # mx 水平环绕（Mercator ±180° 无缝拼接）
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
            val = (v00 * (1 - fx) * (1 - fy) + v10 * fx * (1 - fy) +
                   v01 * (1 - fx) * fy + v11 * fx * fy)
            result[ty, tx] = max(0, min(255, int(round(val))))
        if ty % max(1, target_h // 5) == 0:
            logger.info(f"  Repro {ty}/{target_h}")
    return result


def _row_for_lat(lat: float, target_h: int) -> int:
    """等距柱面中给定纬度对应的行号"""
    return int(round((90.0 - lat) / 180.0 * target_h))


def fill_polar_caps(core_density: np.ndarray,
                    lat_grid: np.ndarray,
                    lon_grid: np.ndarray,
                    api_key: str = "") -> np.ndarray:
    target_h, target_w = core_density.shape

    # --- 1. 全 Mercator ---
    logger.info("Downloading full Mercator (z=2, 16 tiles)...")
    full_merc = download_full_mercator(api_key)
    logger.info(f"Full Mercator: {full_merc.shape}")

    # --- 2. 重投影 55-85° ---
    logger.info("Reprojecting north band 55-85°...")
    north_eq = mercator_to_equirect(full_merc, 55.0, 85.0, target_w)
    logger.info(f"North eq: {north_eq.shape}")

    logger.info("Reprojecting south band -85° to -55°...")
    south_eq = mercator_to_equirect(full_merc, -85.0, -55.0, target_w)
    logger.info(f"South eq: {south_eq.shape}")

    # --- 3. bbox 极顶 85-89° ---
    logger.info("Downloading north cap 85-89.9°...")
    north_cap = download_cap_equirect(85.0, 89.9, api_key)
    logger.info(f"North cap: {north_cap.shape}")

    logger.info("Downloading south cap -89.9° to -85°...")
    south_cap = download_cap_equirect(-89.9, -85.0, api_key)
    logger.info(f"South cap: {south_cap.shape}")

    # --- 4. 合并到 ssec 中间图像 ---
    # 将各条带缩放到目标等距柱面尺寸
    north_rows = _row_for_lat(55.0, target_h) - _row_for_lat(85.0, target_h)
    south_rows = _row_for_lat(-85.0, target_h) - _row_for_lat(-55.0, target_h)
    cap_n_rows = _row_for_lat(85.0, target_h) - _row_for_lat(89.0, target_h)
    cap_s_rows = _row_for_lat(-89.0, target_h) - _row_for_lat(-85.0, target_h)

    north_big = np.array(Image.fromarray(north_eq).resize(
        (target_w, north_rows), Image.LANCZOS))
    south_big = np.array(Image.fromarray(south_eq).resize(
        (target_w, south_rows), Image.LANCZOS))
    cap_n_big = np.array(Image.fromarray(north_cap).resize(
        (target_w, cap_n_rows), Image.LANCZOS))
    cap_s_big = np.array(Image.fromarray(south_cap).resize(
        (target_w, cap_s_rows), Image.LANCZOS))

    ssec = np.zeros((target_h, target_w), dtype=np.uint8)

    # 极顶 85-89°
    r_n = _row_for_lat(89.0, target_h)
    ssec[r_n:r_n + cap_n_rows, :] = cap_n_big
    # Mercator 55-85°
    r_m_n = _row_for_lat(85.0, target_h)
    ssec[r_m_n:r_m_n + north_rows, :] = north_big
    # 南半球 Mercator -85°~-55°
    r_m_s = _row_for_lat(-55.0, target_h)
    ssec[r_m_s:r_m_s + south_rows, :] = south_big
    # 极顶 -89°~-85°
    r_s = _row_for_lat(-85.0, target_h)
    ssec[r_s:r_s + cap_s_rows, :] = cap_s_big

    logger.info(f"SSEC composite: mean={ssec.mean():.1f}, "
                f"zeros={(ssec==0).mean()*100:.1f}%")

    # --- 5. LUT 直方图匹配（55-65° 重叠带） ---
    m = ((lat_grid >= 55) & (lat_grid <= 65)) | \
        ((lat_grid >= -65) & (lat_grid <= -55))
    s, t = ssec[m], core_density[m]
    v = (s > 0) & (t > 0)
    sv, tv = s[v], t[v]
    if len(sv) > 100:
        bins = 256
        sh = np.histogram(sv, bins=bins, range=(0, 255))[0].astype(np.float32)
        th = np.histogram(tv, bins=bins, range=(0, 255))[0].astype(np.float32)
        sc = np.cumsum(sh) / sh.sum()
        tc = np.cumsum(th) / th.sum()
        lut = np.array([np.argmin(np.abs(tc - sc[i])) for i in range(bins)],
                       dtype=np.uint8)
        ssec = lut[ssec]
        logger.info(f"LUT: {sv.mean():.0f}->{tv.mean():.0f}")
    else:
        logger.warning(f"Skip LUT ({len(sv)} samples)")

    # --- 5b. 背景减法：估算并移除 SSEC 的非零背景偏移 ---
    overlap_mask = (lat_grid >= 55) & (lat_grid <= 65)
    ssec_bg = int(np.percentile(ssec[overlap_mask], 5))
    if ssec_bg > 0:
        ssec = np.clip(ssec.astype(np.int16) - ssec_bg, 0, 255).astype(np.uint8)
        logger.info(f"SSEC bg subtract: floor={ssec_bg}, zeros={(ssec==0).mean()*100:.1f}%")

    # --- 6. 羽化融合 ---
    logger.info("Feathering...")
    r = core_density.copy()
    for y in range(target_h):
        lat = lat_grid[y]
        if lat > 65:
            r[y] = ssec[y]
        elif 55 <= lat <= 65:
            w = (1 - math.cos((lat - 55) / 10 * math.pi)) / 2
            r[y] = (core_density[y] * (1 - w) + ssec[y] * w).astype(np.uint8)
        elif -65 <= lat <= -55:
            w = (1 - math.cos((-55 - lat) / 10 * math.pi)) / 2
            r[y] = (core_density[y] * (1 - w) + ssec[y] * w).astype(np.uint8)
        elif lat < -65:
            r[y] = ssec[y]

    logger.info(f"Final: mean={r.mean():.1f}, "
                f"zeros={np.sum(r==0)/r.size*100:.1f}%")
    return r
