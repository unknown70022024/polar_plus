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
# Public site root — where the pipeline publishes and reads itself back
# ---------------------------------------------------------------------------
# One variable covers both directions:
#   * run.py publishes root.json at {root}/root.json and points the tiles at
#     {root}/tiles/
#   * health.py reads the live {root}/root.json to work out the backward-search
#     floor, so the pipeline can never publish data older than what is live
#
# The name is deliberately host-neutral: the same pipeline runs against
# GitHub Pages, Azure Static Web Apps, Azure Blob static website or a local
# directory without any code change.
#
# Resolution order (first non-empty wins):
#   1. POLAR_PUBLIC_BASE_URL  — the value to set on Azure
#   2. PAGES_ROOT_URL         — kept so a local run can point at the live site
#   3. GH_PAGES_BASE          — legacy GitHub Actions variable
#   4. GITHUB_REPOSITORY      — legacy, always set on Actions, derives
#                               https://<owner>.github.io/<repo>
# Steps 3-4 exist only so the existing Actions workflow keeps working during
# the migration; drop them once the Azure job is the only producer.
PUBLIC_BASE_URL_ENV = "POLAR_PUBLIC_BASE_URL"

# Subdirectory (under the site root) that holds the cubemap faces. root.json
# advertises this as `baseUrl`, so the client only ever appends "<face>.jpg".
TILES_SUBPATH = "tiles"

# Written into root.json when no public base URL is configured at all. This is
# intentionally obviously-local rather than a plausible github.io URL: a wrong
# but realistic host would be published to clients and silently break them,
# whereas this one fails visibly.
LOCAL_BASE_URL_FALLBACK = "http://localhost:8080"


def _derive_legacy_github_pages() -> str | None:
    """Derive the Pages site root from GITHUB_REPOSITORY, or None."""
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    owner, name = owner.strip(), name.strip()
    if not owner or not name:
        return None
    return f"https://{owner.lower()}.github.io/{name}"


def public_base_url() -> str | None:
    """Site root the pipeline publishes to, or None when unconfigured.

    Never raises. Callers that must publish should treat None as fatal; the
    caller in run.py only needs a value to embed in root.json.
    """
    for var in (PUBLIC_BASE_URL_ENV, "PAGES_ROOT_URL", "GH_PAGES_BASE"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value.rstrip("/")
    return _derive_legacy_github_pages()


def describe_base_url() -> str:
    """One-line description of where this run will publish, for the log."""
    explicit = (os.environ.get(PUBLIC_BASE_URL_ENV) or "").strip()
    if explicit:
        return f"{explicit.rstrip('/')} (from {PUBLIC_BASE_URL_ENV})"
    value = public_base_url()
    if value:
        return f"{value} (derived from a legacy GitHub variable)"
    return (f"{LOCAL_BASE_URL_FALLBACK} (NO public base URL configured — "
            f"set {PUBLIC_BASE_URL_ENV} before publishing)")


# ---------------------------------------------------------------------------
# NASA GCC v2a (Global Cloud Composite) — primary data source
# ---------------------------------------------------------------------------
GCC_V2A_BASE = ("https://satcorps.larc.nasa.gov/prod/GCC-GEO-LEO/v2a/"
                "visst-pixel-netcdf")

BT_WARM = 285.0       # Kelvin — above this → clear sky (density=0)
BT_COLD = 200.0       # Kelvin — below this → thick cloud (density=255)
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
# (There used to be a stability probe here — _wait_stable, which slept for a
# few seconds and compared two HEAD signatures before reading, to avoid reading
# a file NASA was still rewriting. It was removed: the correctness guarantee
# comes from read_gcc_bt comparing the HEAD signature taken before the read
# with the one taken after it, and a half-written file fails anyway — either
# decompression raises, or the completeness gate sees a band of _FillValue.
# The probe cost one HEAD plus a fixed sleep per candidate, which on a bad day
# was 17% of the whole run.)

# ---------------------------------------------------------------------------
# Truncated-file pre-filter (size heuristic)
# ---------------------------------------------------------------------------
# NASA never finishes writing some hourly files. The completeness gate detects
# that by reading BT_10.8um, but doing so costs 4 chunk fetches plus several
# HEAD round-trips PER CANDIDATE — measured ~33s each, and on 2026-09-16 ten
# candidates in a row were damaged, so the gate alone took 338s of a 484s run.
#
# Content-Length is already in hand from the HEAD the walk does anyway, and
# measured sizes separate cleanly:
#
#     healthy  1313-1418 MB
#     damaged   204-1273 MB   (the north band is missing, so the file is short)
#
# So candidates below a fraction of the last PUBLISHED file's size are assumed
# truncated and skipped without being downloaded. The reference is the live
# file because it is known to have passed the gate, which makes the threshold
# adapt automatically if NASA ever changes resolution or compression.
#
# This is a HEURISTIC and can only ever SKIP — never accept. Publication is
# still decided solely by the gate reading BT_10.8um. And because a wrong
# threshold would silently starve the pipeline, load_gcc_density falls back to
# a full un-filtered walk whenever the filter skipped everything.
GCC_SIZE_FILTER_RATIO = 0.75    # 0 disables the filter entirely

# Set POLAR_HEALTH_ENFORCE=0 to run the gate in log-only (shadow) mode.
HEALTH_ENFORCE = os.environ.get("POLAR_HEALTH_ENFORCE", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# ---------------------------------------------------------------------------
# Backward-search window
# ---------------------------------------------------------------------------
# The walk starts at the most recent eligible hour (MIN_AGE_HOURS ago — younger
# files are still being written) and steps back one hour at a time, newest
# first, keeping the first file whose BT_10.8um passes the completeness gate.
# It stops early once the candidate is no longer newer than the already
# published floor (see ROOT_MARKER_KEY below), and hard-stops at this window.
#
# Why 48 hours rather than something tighter: NASA's archive produces runs of
# damaged files lasting a day or more. On 2026-09-15/16, 15:00Z and 14:00Z were
# incomplete and every 09-16 hour up to 14:00Z was too — a 12h window meant
# publishing nothing at all for over a day even though healthy files (09-15
# 13:00Z and earlier) sat just outside it. Widening the window costs a few
# HEAD requests and, at worst, one extra rejected read.
#
# This is the ONLY window knob: the old pair (SEARCH_HOURS=96 capped by
# MAX_FALLBACK_HOURS=12) is gone, because the effective bound was always
# min(the two) and having two numbers made it easy to misread.
#
# POLAR_SEARCH_HOURS overrides it, so the window can be widened or narrowed for
# a single run without rebuilding the image.
SEARCH_HOURS = int(os.environ.get("POLAR_SEARCH_HOURS", "48"))

# ---------------------------------------------------------------------------
# Publish floor (never publish data older than what is already live)
# ---------------------------------------------------------------------------
ROOT_TS_TIMEOUT = 15      # seconds — per root.json fetch

# POLAR_FLOOR_TS overrides the auto-detected lower bound, for experiments:
#   ""                  → auto (max of the Pages and local root.json)
#   "none"              → disable the bound entirely
#   "YYYYMMDD_HHMM"     → use this exact instant
FLOOR_TS_ENV = "POLAR_FLOOR_TS"

# ---------------------------------------------------------------------------
# Publish marker — how a run recognises that it is the first of this version
# ---------------------------------------------------------------------------
# Every root.json this pipeline writes carries the pipeline version that
# produced it. The marker is not a boolean but a version string, because
# "is this the first run?" is really the question "was the live data produced
# by a pipeline whose behaviour matches mine?".
#
#   live root.json WITHOUT a version        → first run of a versioned pipeline
#   live root.json WITH a DIFFERENT version → first run after a behaviour change
#   live root.json WITH THE SAME version    → normal, bounded operation
#
# Why this matters: the backward search refuses to publish anything older than
# what is live. That is right when the code is unchanged, but after a change
# that alters which data we would pick — a new completeness gate, a new data
# source, a new search window — the live version may itself be exactly the
# thing the new code exists to replace, and the bound would block the fix
# forever. So the first run of a new version ignores the floor, picks the
# newest healthy file and force-publishes it; that run writes the new version
# and every run after it applies the bounds normally.
#
# The search window (SEARCH_HOURS) still applies during bootstrap, so this can
# never publish genuinely stale data.
#
# IMPORTANT — bump PIPELINE_VERSION by hand, deliberately. Do NOT derive it
# from git, an image tag or a content hash: every commit or rebuild would then
# look like a new version, the floor would never apply, and the pipeline could
# republish older data on every single run.
#
# Bump it when a change makes the already-published data something the new
# code would not have chosen. Examples:
#   2.0.0  completeness gate redesign + 48h window + three-source lightning
PIPELINE_VERSION = os.environ.get("POLAR_PIPELINE_VERSION",
                                  "2.0.0").strip() or "2.0.0"
ROOT_VERSION_KEY = "version"

# The pre-version key, kept only so a run can say *why* it is bootstrapping.
LEGACY_MARKER_KEY = "gate"
ROOT_MARKER_KEY = ROOT_VERSION_KEY      # backwards-compatible alias
ROOT_MARKER_VALUE = PIPELINE_VERSION    # ditto

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
