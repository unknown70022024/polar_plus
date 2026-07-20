"""
polar_plus/config.py — 独立配置
"""
import os
from pathlib import Path

_default_output = Path(__file__).resolve().parent / "output"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(_default_output)))
FACE_SIZE = 1024
LON_OFFSET = 0.0
FACES = ['px', 'nx', 'py', 'ny', 'pz', 'nz']

# GMGSI S3
GMGSI_BUCKET = "noaa-gmgsi-pds"
GMGSI_REGION = "us-east-1"
BT_WARM = 280.0
BT_COLD = 200.0
BT_FILL = -999.0

# SSEC RealEarth WMS (mapserv CGI, 支持 BBOX 分块)
SSEC_WMS_URL = ("https://realearth.ssec.wisc.edu/cgi-bin/mapserv"
                "?map=globalir.map&SERVICE=WMS&VERSION=1.1.1"
                "&REQUEST=GetMap&LAYERS=globalir&FORMAT=image/png"
                "&SRS=EPSG:4326")

# 分块参数（免费 WMS 最大边长 512px）
TILE_W = 512         # 每块宽度
TILE_H = 512         # 每块高度
LON_TILES = 10       # 经度方向分块数（360° / 10 = 36° 每块）

# Core latitude range
CORE_LAT_NORTH = 60.0
CORE_LAT_SOUTH = -60.0

# Target equirectangular size
TARGET_W = 5000
TARGET_H = 2500
