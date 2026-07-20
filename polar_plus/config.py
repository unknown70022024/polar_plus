"""
polar_plus/config.py — GCC pipeline configuration.
"""
import os
from pathlib import Path

_default_output = Path(__file__).resolve().parent / "output"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(_default_output)))
FACE_SIZE = 1024
LON_OFFSET = 0.0
FACES = ['px', 'nx', 'py', 'ny', 'pz', 'nz']

# ---------------------------------------------------------------------------
# NASA GCC v2a (Global Cloud Composite) — primary data source
# ---------------------------------------------------------------------------
GCC_V2A_BASE = ("https://satcorps.larc.nasa.gov/prod/GCC-GEO-LEO/v2a/"
                "visst-pixel-netcdf")

BT_WARM = 285.0       # Kelvin — above this → clear sky (density=0)
BT_COLD = 200.0       # Kelvin — below this → thick cloud (density=255)
SEARCH_HOURS = 96     # Look-back window for latest GCC file

# ---------------------------------------------------------------------------
# SSEC RealEarth — polar gap-fill backup
# ---------------------------------------------------------------------------
SSEC_WMS_URL = ("https://realearth.ssec.wisc.edu/cgi-bin/mapserv"
                "?map=globalir.map&SERVICE=WMS&VERSION=1.1.1"
                "&REQUEST=GetMap&LAYERS=globalir&FORMAT=image/png"
                "&SRS=EPSG:4326")

SSEC_API_BASE = "http://re.ssec.wisc.edu/api/image"

GAP_LAT_THRESHOLD = 60.0    # |lat| > threshold → allow SSEC gap-fill
FEATHER_WIDTH = 2.0         # degrees — cosine feather at gap edges
MERCATOR_LAT_HIGH = 85.0    # Mercator reprojection upper bound
BBOX_LAT_TOP = 89.9         # bbox patch top (near-pole)

# Mercator tile params
TILE_Z = 2
TILE_SIZE = 256
N_TILES = 2 ** TILE_Z       # 4

# Target equirectangular size
TARGET_W = 5000
TARGET_H = 2500
