#!/usr/bin/env python3
"""
polar_plus/fetch_storms.py — Download lightning strikes from limap.org archive.

Fetches recent strike data (last 3 hours) from Blitzortung's official
data archive at limap.org via plain HTTP GET. No credentials required.

Data is organized by container (region) and 10-minute intervals:
    https://limap.org/{container}/{year}/{month}/{day}/{hour}/{10min}.json

Containers covering the globe: C1-C7, C10, C18, C19

Output: {OUTPUT_DIR}/latest/storms.json  →  [{"lat":34.0,"lng":-118.0}, ...]
"""

import json
import logging
import math
import os
import random
import time
from datetime import datetime, timezone, timedelta
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

ARCHIVE_BASE = "https://limap.org"

# Geographic containers (Blitzortung regions)
CONTAINERS = [
    "C1",   # Europe 1 (Germany server)
    "C2",   # Oceania
    "C3",   # North America 1 (Germany server)
    "C4",   # Asia
    "C5",   # Africa
    "C6",   # South America
    "C7",   # Japan
    "C10",  # North America 2 (Finland server)
    "C18",  # Europe 2 (Finland server)
    "C19",  # Europe 3 (Finland server)
]

LOOKBACK_HOURS = 3
GRID_DEG = 1.0     # Dedup grid cell ≈ 111 km at equator
MAX_STRIKES = 2000

# Default locations if archive is unreachable
DEFAULT_LOCATIONS = [
    {"lat": 34.05, "lng": -118.24}, {"lat": -33.87, "lng": 151.21},
    {"lat": 51.51, "lng": -0.13}, {"lat": 35.68, "lng": 139.76},
    {"lat": -34.60, "lng": -58.38}, {"lat": 41.01, "lng": 28.98},
    {"lat": 19.08, "lng": 72.88}, {"lat": -1.29, "lng": 36.82},
    {"lat": 55.75, "lng": 37.62}, {"lat": -22.91, "lng": -43.20},
    {"lat": 30.04, "lng": 31.24}, {"lat": -6.21, "lng": 106.85},
    {"lat": 48.86, "lng": 2.35}, {"lat": -37.81, "lng": 144.96},
    {"lat": 37.57, "lng": 126.98}, {"lat": 14.60, "lng": 120.98},
    {"lat": -4.33, "lng": 15.31}, {"lat": 25.20, "lng": 55.27},
    {"lat": 40.42, "lng": -3.70}, {"lat": 52.52, "lng": 13.41},
    {"lat": 59.33, "lng": 18.07}, {"lat": 33.89, "lng": 35.50},
    {"lat": -26.20, "lng": 28.05}, {"lat": 53.55, "lng": -113.49},
    {"lat": 43.65, "lng": -79.38}, {"lat": -12.05, "lng": -77.04},
    {"lat": 39.90, "lng": 116.41}, {"lat": -31.95, "lng": 115.86},
    {"lat": 47.38, "lng": 8.54}, {"lat": 60.17, "lng": 24.94},
    {"lat": 38.72, "lng": -9.14}, {"lat": 50.85, "lng": 4.35},
    {"lat": 52.37, "lng": 4.89}, {"lat": 45.44, "lng": 9.19},
    {"lat": 17.39, "lng": 78.49}, {"lat": 29.56, "lng": 106.55},
    {"lat": 44.80, "lng": 20.47}, {"lat": -23.55, "lng": -46.63},
    {"lat": 28.61, "lng": 77.23}, {"lat": 13.75, "lng": 100.50},
]


def grid_key(lat: float, lng: float) -> tuple:
    return (int(math.floor(lat / GRID_DEG)), int(math.floor(lng / GRID_DEG)))


def fetch_archive() -> list[dict] | None:
    """
    Download recent strike data from limap.org for all containers.
    Returns list of {"lat": float, "lng": float} dicts, or None on failure.
    """
    now = datetime.now(timezone.utc)
    # Get the most recent completed 10-minute interval
    latest_minute = (now.minute // 10) * 10
    latest_slot = now.replace(minute=latest_minute, second=0, microsecond=0)

    strikes = []
    seen_grids = set()
    total_bytes = 0

    # Fetch only the latest interval for each container (10 files total)
    urls = []
    for container in CONTAINERS:
        url = (
            f"{ARCHIVE_BASE}/{container}/"
            f"{latest_slot.year}/{latest_slot.month:02d}/{latest_slot.day:02d}/"
            f"{latest_slot.hour:02d}/{latest_slot.minute:02d}.json"
        )
        urls.append((container, url))

    logger.info(f"Fetching {len(urls)} archive files from limap.org...")
    t0 = time.time()

    for container, url in urls:
        try:
            req = Request(url)
            req.add_header("User-Agent", "polar-plus/1.0 (Android live wallpaper)")
            with urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    continue
                body = resp.read()
                total_bytes += len(body)
                text = body.decode("utf-8", errors="replace")

            for line in text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    lat = float(obj.get("lat", 0))
                    lng = float(obj.get("lon", 0))
                    gk = grid_key(lat, lng)
                    if gk in seen_grids:
                        continue
                    seen_grids.add(gk)
                    strikes.append({"lat": lat, "lng": lng})
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        except HTTPError as e:
            if e.code != 404:  # 404 is normal — many 10-min slots are empty
                logger.debug(f"  HTTP {e.code} for {container}")
        except URLError:
            pass  # Container server may be down
        except Exception:
            pass

    elapsed = time.time() - t0
    logger.info(
        f"Downloaded {total_bytes / 1024:.0f} KB in {elapsed:.0f}s, "
        f"{len(strikes)} unique strikes from {len(urls)} files"
    )
    return strikes if strikes else None


def save_storms(strikes: list[dict], output_dir: Path):
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    if len(strikes) > MAX_STRIKES:
        strikes = random.sample(strikes, MAX_STRIKES)

    path = latest_dir / "storms.json"
    with open(path, "w") as f:
        json.dump(strikes, f, separators=(",", ":"))
    logger.info(f"Saved {len(strikes)} storms -> {path} ({path.stat().st_size:,} bytes)")


def main():
    output_dir = Path(os.environ.get("OUTPUT_DIR", str(
        Path(__file__).resolve().parent / "output"
    )))

    logger.info("Fetching lightning strikes from limap.org archive...")
    strikes = fetch_archive()

    if strikes is None or len(strikes) == 0:
        logger.warning("Archive fetch returned no data, using default locations")
        strikes = DEFAULT_LOCATIONS

    save_storms(strikes, output_dir)


if __name__ == "__main__":
    main()
