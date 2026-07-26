#!/usr/bin/env python3
"""
polar_plus/fetch_storms.py — Download global lightning strikes from Blitzortung.

Connects to the public JSON-RPC service (bo-service.tryb.de) maintained by
the wuan/bo-android project.  Returns grid-aggregated strike data which
we then sample into individual (lat, lng) locations.

Output: {OUTPUT_DIR}/latest/storms.json  →  [{"lat":34.0,"lng":-118.0}, ...]
"""
import json
import logging
import math
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("fetch_storms")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BO_SERVICE_URL = "http://bo-service.tryb.de/"
BO_METHOD = "get_global_strikes_grid"
WINDOW_MINUTES = 60  # time window in minutes to fetch
BO_GRID_BASE = 10000
BO_THRESHOLD = 0

REQUEST_HEADERS = {
    "Content-Type": "text/json",
    "User-Agent": "bo-android-170",
}

REQUEST_TIMEOUT = 30

GRID_DEG = 1.0          # Dedup grid cell ≈ 111 km at equator
MAX_STRIKES = 500

# Fallback locations when the service is unreachable
DEFAULT_LOCATIONS = [
    {"lat": 34.05, "lng": -118.24}, {"lat": -33.87, "lng": 151.21},
    {"lat": 51.51, "lng": -0.13},   {"lat": 35.68, "lng": 139.76},
    {"lat": -34.60, "lng": -58.38}, {"lat": 41.01, "lng": 28.98},
    {"lat": 19.08, "lng": 72.88},   {"lat": -1.29, "lng": 36.82},
    {"lat": 55.75, "lng": 37.62},   {"lat": -22.91, "lng": -43.20},
    {"lat": 30.04, "lng": 31.24},   {"lat": -6.21, "lng": 106.85},
    {"lat": 48.86, "lng": 2.35},    {"lat": -37.81, "lng": 144.96},
    {"lat": 37.57, "lng": 126.98},  {"lat": 14.60, "lng": 120.98},
    {"lat": -4.33, "lng": 15.31},   {"lat": 25.20, "lng": 55.27},
    {"lat": 40.42, "lng": -3.70},   {"lat": 52.52, "lng": 13.41},
    {"lat": 59.33, "lng": 18.07},   {"lat": 33.89, "lng": 35.50},
    {"lat": -26.20, "lng": 28.05},  {"lat": 53.55, "lng": -113.49},
    {"lat": 43.65, "lng": -79.38},  {"lat": -12.05, "lng": -77.04},
    {"lat": 39.90, "lng": 116.41},  {"lat": -31.95, "lng": 115.86},
    {"lat": 47.38, "lng": 8.54},    {"lat": 60.17, "lng": 24.94},
    {"lat": 38.72, "lng": -9.14},   {"lat": 50.85, "lng": 4.35},
    {"lat": 52.37, "lng": 4.89},    {"lat": 45.44, "lng": 9.19},
    {"lat": 17.39, "lng": 78.49},   {"lat": 29.56, "lng": 106.55},
    {"lat": 44.80, "lng": 20.47},   {"lat": -23.55, "lng": -46.63},
    {"lat": 28.61, "lng": 77.23},   {"lat": 13.75, "lng": 100.50},
]


def grid_key(lat: float, lng: float) -> tuple:
    return (int(math.floor(lat / GRID_DEG)), int(math.floor(lng / GRID_DEG)))


# ---------------------------------------------------------------------------
# Fetch from bo-service JSON-RPC
# ---------------------------------------------------------------------------

def fetch_bo_service(minute_offset: int = 0) -> list[dict] | None:
    """Call the bo-service JSON-RPC and return a list of (lat, lng) strikes.

    Args:
        minute_offset: Minutes from now for the window end (negative = past).
                       e.g. -60 means "1 hour ago → 2 hours ago".
    """
    params = [WINDOW_MINUTES, BO_GRID_BASE, minute_offset, BO_THRESHOLD]
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": BO_METHOD,
        "params": params,
        "id": 1,
    }).encode()

    try:
        req = Request(BO_SERVICE_URL, data=payload, headers=REQUEST_HEADERS)
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
        logger.warning(f"bo-service request failed: {e}")
        return None

    # Validate JSON-RPC response
    if "result" not in data or data.get("result") is None:
        logger.warning(f"bo-service returned no result: {data.get('error', 'unknown')}")
        return None

    result = data["result"]
    if "r" not in result or "xd" not in result or "yd" not in result:
        logger.warning("bo-service response missing grid fields")
        return None

    grid_cells = result["r"]       # [[x_idx, y_idx, count, time_offset], ...]
    xd = result["xd"]               # longitude delta per cell (degrees)
    yd = result["yd"]               # latitude delta per cell (degrees)

    if not grid_cells:
        logger.warning("bo-service returned empty grid")
        return None

    total_count = sum(cell[2] for cell in grid_cells)
    logger.info(f"bo-service: {len(grid_cells)} cells, {total_count} total strikes, "
                f"cell={xd:.4f}°×{yd:.4f}°")

    # Weighted sampling by count
    if total_count == 0:
        return None

    weights = [max(cell[2], 1) for cell in grid_cells]
    sample_size = min(MAX_STRIKES, len(grid_cells))
    sampled = random.choices(grid_cells, weights=weights, k=sample_size)

    # Convert grid indices → (lat, lng) with in-cell jitter.
    # The grid cells (~0.13°×0.09°) already provide natural spacing,
    # so no post-dedup needed — just jitter within each cell.
    strikes = []
    for x_idx, y_idx, _, _ in sampled:
        lon = (x_idx + 0.5) * xd + random.uniform(-xd / 2, xd / 2)
        lat = -(y_idx + 0.5) * yd + random.uniform(-yd / 2, yd / 2)

        # Normalise longitude to [-180, 180)
        lon = ((lon + 180) % 360) - 180

        strikes.append({"lat": round(lat, 4), "lng": round(lon, 4)})

    logger.info(f"bo-service: {len(strikes)} strikes after sampling")
    return strikes if strikes else None


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------

def save_storms(strikes: list[dict], output_dir: Path):
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    path = latest_dir / "storms.json"
    with open(path, "w") as f:
        json.dump(strikes, f, separators=(",", ":"))
    logger.info(f"Saved {len(strikes)} storms -> {path} ({path.stat().st_size:,} bytes)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    output_dir = Path(os.environ.get("OUTPUT_DIR", str(
        Path(__file__).resolve().parent / "output"
    )))

    # Try to align with GCC timestamp
    minute_offset = 0  # default: most recent data
    ts_str = os.environ.get("GCC_TIMESTAMP")
    if ts_str:
        logger.info(f"Using GCC_TIMESTAMP env var: {ts_str}")
    else:
        # Fallback: read from file (local development)
        ts_path = output_dir / "latest" / "gcc_timestamp.txt"
        if ts_path.exists():
            ts_str = ts_path.read_text().strip()
    if ts_str:
        try:
            gcc_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            minute_offset = int((gcc_dt - now).total_seconds() / 60)
            logger.info(f"Aligned to GCC timestamp {ts_str} (offset={minute_offset} min)")
        except (ValueError, OSError) as e:
            logger.warning(f"Failed to parse GCC timestamp: {e}")

    logger.info(f"Fetching lightning strikes from bo-service (offset={minute_offset} min)...")
    strikes = fetch_bo_service(minute_offset=minute_offset)

    if strikes is None or len(strikes) == 0:
        logger.warning("bo-service unavailable, using default locations")
        strikes = DEFAULT_LOCATIONS

    save_storms(strikes, output_dir)


if __name__ == "__main__":
    main()
