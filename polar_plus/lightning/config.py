"""
light.config — 三源闪电融合的参数与覆盖模型。

覆盖范围不是猜的，是从各仪器的真实元数据里量出来的：

  GOES-18 (GOES-West, 137.2°W)  GLM FOV: lon 158.8°E .. 72.8°W, lat ±57.6°
       —— 读自 GLM-L2-LCFA 文件的 lon_field_of_view_bounds / lat_field_of_view_bounds

  GOES-19 (GOES-East,  75.2°W)  GLM FOV: lon 139.4°W .. 11.0°W, lat ±57.6°
       —— 同上

  MTG-I1 LI (0°)  全圆盘，约 ±80° 可见盘
       —— CEOS EO Handbook: "84% of visible earth disc"

三者并集覆盖 158.8°E → 11.0°W，全球只剩两个盲区交给 Blitzortung：

  1. 亚太带   约 81°E .. 158.8°E
  2. 高纬带   |lat| > 57.6°（GLM 区）/ 更高（LI 区）

按用户决定，Blitzortung 只保留亚太区（BLITZ_BBOX），高纬带不再补。
"""
from __future__ import annotations

import math
import os
import pathlib


def _load_env_files() -> None:
    """从 ~/.lightning.env 和仓库根的 .env 读环境变量。

    已存在的环境变量优先，不会被覆盖。密钥建议放仓库外
    （~/.lightning.env）—— 那样即使误操作 git add -A 也提交不上去。
    """
    # config.py 在 polar_plus/lightning/ 下，上三层才是仓库根
    here = pathlib.Path(__file__).resolve().parent.parent.parent
    for path in (pathlib.Path.home() / ".lightning.env", here / ".env"):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env_files()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

# 用户指定：不再局限 500 点，用 2000-3000。
MAX_POINTS = _env_int("LIGHT_MAX_POINTS", 2500)

# 每个 1° 网格最多出几个点 —— 保证地理铺开，防止欧洲/北美密度淹没亚太
POINTS_PER_CELL = _env_int("LIGHT_POINTS_PER_CELL", 3)

# 融合网格（度）
GRID_DEG = _env_float("LIGHT_GRID_DEG", 1.0)


# ---------------------------------------------------------------------------
# 时间窗
# ---------------------------------------------------------------------------

# 闪电窗口长度（分钟），默认与现有管线一致
WINDOW_MINUTES = _env_int("LIGHT_WINDOW_MINUTES", 60)


# ---------------------------------------------------------------------------
# 卫星覆盖模型
# ---------------------------------------------------------------------------

GLM18_SUB_LON = -137.2      # GOES-West
GLM19_SUB_LON = -75.2       # GOES-East
LI_SUB_LON = 0.0            # MTG-I1

# 实测的 GLM 仪器视场（度）
GLM_LAT_LIMIT = 57.6
GLM18_LON = (158.8, -72.8)  # 跨日界线：158.8°E → 180 → -180 → 72.8°W
GLM19_LON = (-139.4, -11.0)

# LI 可见盘半径（度，地心角）。
# CEOS EO Handbook: "Fixed view of 84% of visible earth disc"。
# 地球盘的地心角半径是 81.3°，按面积取 84% 反推半径：
#     r = 81.3 * sqrt(0.84) = 74.5°
# 用 80° 会高估——那样连印度东部 (20°N,78°E, 地心角 78.7°) 都会被判成覆盖，
# 而实际上那已经在 MTG 视盘之外了。
LI_VIEW_ANGLE = 74.5


def _in_lon_span(lon: float, lo: float, hi: float) -> bool:
    """经度是否落在 [lo, hi] 内，支持跨日界线的区间（lo > hi）。"""
    if lo <= hi:
        return lo <= lon <= hi
    return lon >= lo or lon <= hi


def satellite_for(lat: float, lon: float) -> str | None:
    """返回覆盖该点的卫星名，没有则 None。"""
    if abs(lat) <= GLM_LAT_LIMIT:
        if _in_lon_span(lon, *GLM18_LON):
            return "goes18"
        if _in_lon_span(lon, *GLM19_LON):
            return "goes19"
    # LI：以 (0°, 0°) 为中心的地心角
    p = math.radians(lat)
    dl = math.radians(lon - LI_SUB_LON)
    cos_g = max(-1.0, min(1.0, math.cos(p) * math.cos(dl)))
    if math.degrees(math.acos(cos_g)) <= LI_VIEW_ANGLE:
        return "mtg-li"
    return None


# ---------------------------------------------------------------------------
# Blitzortung
# ---------------------------------------------------------------------------

# 服务是 wuan/bo-android 的公开 JSON-RPC 镜像。
# 实测：它**不支持按经纬度取数** ——
#   get_global_strikes_grid(win, base, offset, thr)  全球
#   get_strikes_grid(win, base)                      硬编码欧洲 lon -25..57 / lat 27..72
# 第二参数只影响分辨率，区域恒定。所以亚太只能在客户端过滤。
BO_SERVICE_URL = os.environ.get("BO_SERVICE_URL", "http://bo-service.tryb.de/")
BO_METHOD = "get_global_strikes_grid"
BO_GRID_BASE = _env_int("BO_GRID_BASE", 10000)
BO_THRESHOLD = _env_int("BO_THRESHOLD", 0)
BO_TIMEOUT = _env_int("BO_TIMEOUT", 45)

# 必须的请求头，否则服务拒绝（照搬现有 polar_plus/fetch_storms.py）
BO_HEADERS = {
    "Content-Type": "text/json",
    "User-Agent": "bo-android-170",
}

# Blitzortung 只保留亚太区：lon_min, lon_max, lat_min, lat_max
# 全球响应只有约 64 KB，客户端过滤成本可忽略。
BLITZ_BBOX = (
    _env_float("LIGHT_BLITZ_LON_MIN", 70.0),
    _env_float("LIGHT_BLITZ_LON_MAX", 180.0),
    _env_float("LIGHT_BLITZ_LAT_MIN", -55.0),
    _env_float("LIGHT_BLITZ_LAT_MAX", 60.0),
)


def blitz_in_bbox(lat: float, lon: float) -> bool:
    lo0, lo1, la0, la1 = BLITZ_BBOX
    return la0 <= lat <= la1 and lo0 <= lon <= lo1


# ---------------------------------------------------------------------------
# NOAA GOES GLM
# ---------------------------------------------------------------------------

GOES_BUCKETS = {"goes18": "noaa-goes18", "goes19": "noaa-goes19"}
GLM_PREFIX = "GLM-L2-LCFA"
GLM_CONCURRENCY = _env_int("GLM_CONCURRENCY", 32)
GLM_TIMEOUT = _env_int("GLM_TIMEOUT", 30)   # 单文件总时长上限（文件仅约 300 KB）
GLM_MAX_FILES = _env_int("GLM_MAX_FILES", 400)   # 60min/20s = 180/星，留余量

# 文件抽样步长：GLM 是 20 秒一个 granule，60 分钟双星就是 360 个文件（约 110 MB），
# 但输出上限只有 2500 点 —— 也就是采了 10 倍于所需的量。
# stride=3 表示每 3 个文件取 1 个（等效 1 分钟时间分辨率），能砍掉 2/3 的流量。
# stride=1 表示不抽样。
GLM_FILE_STRIDE = _env_int("GLM_FILE_STRIDE", 1)


# ---------------------------------------------------------------------------
# EUMETSAT MTG LI
# ---------------------------------------------------------------------------

EUMETSAT_TOKEN_URL = "https://api.eumetsat.int/token"
EUMETSAT_SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
EUMETSAT_DOWNLOAD_URL = "https://api.eumetsat.int/data/download/1.0.0"
EUMETSAT_COLLECTION = os.environ.get("EUMETSAT_COLLECTION", "EO:EUM:DAT:0691")  # LI L2 LFL
EUMETSAT_TIMEOUT = _env_int("EUMETSAT_TIMEOUT", 60)   # 单产品总时长上限（仅约 500 KB）
EUMETSAT_CONCURRENCY = _env_int("EUMETSAT_CONCURRENCY", 8)
EUMETSAT_MAX_PRODUCTS = _env_int("EUMETSAT_MAX_PRODUCTS", 12)   # 60min/10min = 6，留余量

# 闪击置信度下限（flash_filter_confidence 已展开到 0..1）。
# 实测一个 10 分钟的全圆盘产品有 3.4 万个闪击 —— 折合约 56 次/秒，
# 而全球平均只有约 44 次/秒，说明其中有大量低置信度的误检。
# 设 0 表示不过滤，便于先看原始分布再定阈值。
EUMETSAT_MIN_CONFIDENCE = _env_float("EUMETSAT_MIN_CONFIDENCE", 0.5)

EUMETSAT_KEY = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
EUMETSAT_SECRET = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()


def eumetsat_enabled() -> bool:
    return bool(EUMETSAT_KEY and EUMETSAT_SECRET)


# ---------------------------------------------------------------------------
# 源开关：LIGHT_SOURCES=blitzortung,glm,eumetsat
# 便于分步验证 —— 出问题时能立刻定位到是哪个源。
# ---------------------------------------------------------------------------

DEFAULT_SOURCES = "blitzortung,glm,eumetsat"


def enabled_sources() -> list[str]:
    raw = os.environ.get("LIGHT_SOURCES", DEFAULT_SOURCES)
    out = []
    for s in raw.split(","):
        s = s.strip().lower()
        if s in ("blitzortung", "glm", "eumetsat") and s not in out:
            out.append(s)
    return out
