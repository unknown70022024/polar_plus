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
MIN_AGE_HOURS = 2     # Skip files younger than this (still being assembled)

# ---------------------------------------------------------------------------
# BT_10.8um completeness gate
# ---------------------------------------------------------------------------
# The GCC NetCDF files carry ~37 science variables and NASA keeps appending
# data to them for many hours after the nominal hour, so file-level signals
# (Content-Length, Last-Modified, granule counts) say nothing about whether
# the one variable this pipeline needs — BT_10.8um — is actually complete.
# We therefore validate BT_10.8um itself, band by band, while reading it.
#
# Measured on real files (BT_10.8um, invalid = _FillValue or outside
# valid_range), 2026-09-15, sub-blocks of 405x810 (405 = 6480/16 rows,
# 810 = 3240/4 cols), out of 256 blocks total:
#
#     file                       band invalid N->S      dead_blocks   verdict
#     09-14 14:00 (reprocessed)  ~1%                     0            usable
#     09-15 09:00 .. 13:00       5-8%                    1            usable
#     09-15 14:00 (12h later)    24/12/8/31%             7            REJECT
#     09-15 15:00                63% in band 0 alone    30 (band 0)  REJECT
#     09-15 14:00 (mid-write)    --                      43           REJECT
#
# Healthy files sit at 0-1 dead blocks (the 1 is one fixed high-latitude gap
# that appears in every file). The 14:00 and 15:00 rows above are not merely
# "less good" — they are files NASA never finished writing, and they stayed
# that way hours later. 15:00 is the one a scheduled run actually published
# before this gate existed: the resulting tiles were 67-94% pure black on
# every equatorial face, and the app served that for hours.
#
# The threshold is 4: four times the worst healthy file observed, and well
# below both damaged ones. Erring strict is deliberate. A false reject only
# means "keep serving the previous good data"; a false accept publishes
# garbage that stays live until some later run happens to succeed.
HEALTH_SUB_ROWS = 405           # 6480 / 16
HEALTH_SUB_COLS = 810           # 3240 / 4
HEALTH_DEAD_SUBBLOCK_FRAC = 0.95  # sub-block at/above this → "dead"
HEALTH_DEAD_BLOCKS_MAX = 4      # >= this many dead blocks → file incomplete
HEALTH_GLOBAL_INVALID_MAX = 0.50  # separate backstop: catches a uniformly
                                  # sparse file that has no single dead block
HEALTH_STABLE_WAIT = 24         # seconds — short stability probe per candidate

# Set POLAR_HEALTH_ENFORCE=0 to run the gate in log-only (shadow) mode.
HEALTH_ENFORCE = os.environ.get("POLAR_HEALTH_ENFORCE", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# ---------------------------------------------------------------------------
# Backward-search bound (never publish data older than what is already live)
# ---------------------------------------------------------------------------
MAX_FALLBACK_HOURS = 12   # absolute cap on how far back we will search
ROOT_TS_TIMEOUT = 15      # seconds — per root.json fetch

# POLAR_FLOOR_TS overrides the auto-detected lower bound, for experiments:
#   ""                  → auto (max of the Pages and local root.json)
#   "none"              → disable the bound entirely
#   "YYYYMMDD_HHMM"     → use this exact instant
FLOOR_TS_ENV = "POLAR_FLOOR_TS"

# ---------------------------------------------------------------------------
# Publish marker — how a run recognises that it is the first of this version
# ---------------------------------------------------------------------------
# Every root.json this pipeline writes carries these two keys. They are what
# lets a run tell whether the data currently live was produced by a pipeline
# that applies the completeness gate:
#
#   live root.json WITHOUT the marker
#       The live data came from the old pipeline, which would publish a
#       half-written file (that is how the broken 2026-09-15 15:00Z got out).
#       No bound can repair that on its own, because the bound says "never
#       publish older than what is live" — so a bad live version blocks the
#       healthy older one forever. The FIRST run of this pipeline therefore
#       ignores BOTH bounds (the floor and MAX_FALLBACK_HOURS), picks the
#       newest healthy file and force-publishes it. That run writes the
#       marker, and every run after it applies the bounds normally.
#
#   live root.json WITH the marker
#       Normal operation.
#
# Bump ROOT_MARKER_VALUE only if a change makes already-published data
# untrustworthy, so the next run re-bootstraps.
ROOT_MARKER_KEY = "gate"
ROOT_MARKER_VALUE = 1

# ---------------------------------------------------------------------------
# SSEC RealEarth — polar gap-fill backup
# ---------------------------------------------------------------------------
SSEC_WMS_URL = ("https://realearth.ssec.wisc.edu/cgi-bin/mapserv"
                "?map=globalir.map&SERVICE=WMS&VERSION=1.1.1"
                "&REQUEST=GetMap&LAYERS=globalir&FORMAT=image/png"
                "&SRS=EPSG:4326")

SSEC_API_BASE = "http://re.ssec.wisc.edu/api/image"

GAP_LAT_THRESHOLD = 60.0    # |lat| > threshold → allow SSEC gap-fill
FEATHER_WIDTH = 2.0         # degrees — cosine fade-in at the 60° cut-off

# A pixel counts as a hole when its density is below the value that
# run._post_process() zeroes out anyway. Using `density == 0` instead left the
# 1-px anti-aliasing/ringing ramp that LANCZOS downsampling (12960 → 5000)
# leaves along *every* hole boundary unfilled: those pixels are 1..44, not 0,
# so the fill skipped them and the threshold then turned the ramp into a crisp
# black outline around each filled area. Must stay equal to the threshold
# passed to run._post_process().
GAP_DENSITY_THRESHOLD = 45
# Width, in pixels, of the cosine ramp applied just OUTSIDE the fill boundary
# so GCC and SSEC meet without a step. The outermost ring gets weight 0.
GAP_FEATHER_PX = 4
MERCATOR_LAT_HIGH = 85.0    # Mercator reprojection upper bound
BBOX_LAT_TOP = 89.9         # bbox patch top (near-pole)

# Mercator tile params
TILE_Z = 2
TILE_SIZE = 256
N_TILES = 2 ** TILE_Z       # 4

# Target equirectangular size
TARGET_W = 5000
TARGET_H = 2500
