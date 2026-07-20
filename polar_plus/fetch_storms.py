#!/usr/bin/env python3
"""
polar_plus/fetch_storms.py — Fetch lightning strike data from Blitzortung.

Fetches recent strikes from data.blitzortung.org (requires operator credentials
set as BLITZORTUNG_AUTH env var) and converts them to a simple JSON array for
the Android live wallpaper to consume.

Output: {OUTPUT_DIR}/latest/storms.json  →  [{"lat":34.0,"lng":-118.0}, ...]

If BLITZORTUNG_AUTH is not set, generates a placeholder file with hardcoded
default locations so the pipeline does not fail while waiting for credentials.
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("fetch_storms")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Blitzortung protected data endpoint
BLITZORTUNG_URL = (
    "https://data.blitzortung.org/Data/Protected/last_strikes.php"
    "?number=50000&sig=0"
)

# Grid dedup: merge strikes within GRID_DEG degrees into one
GRID_DEG = 1.0  # ~111 km at equator

# How far back to look (hours)
LOOKBACK_HOURS = 3

# Default locations when Blitzortung data is unavailable
# (major cities roughly distributed around the globe)
DEFAULT_LOCATIONS = [
    {"lat": 34.05, "lng": -118.24},
    {"lat": -33.87, "lng": 151.21},
    {"lat": 51.51, "lng": -0.13},
    {"lat": 35.68, "lng": 139.76},
    {"lat": -34.60, "lng": -58.38},
    {"lat": 41.01, "lng": 28.98},
    {"lat": 19.08, "lng": 72.88},
    {"lat": -1.29, "lng": 36.82},
    {"lat": 55.75, "lng": 37.62},
    {"lat": -22.91, "lng": -43.20},
    {"lat": 30.04, "lng": 31.24},
    {"lat": -6.21, "lng": 106.85},
    {"lat": 48.86, "lng": 2.35},
    {"lat": -37.81, "lng": 144.96},
    {"lat": 37.57, "lng": 126.98},
    {"lat": 14.60, "lng": 120.98},
    {"lat": -4.33, "lng": 15.31},
    {"lat": 25.20, "lng": 55.27},
    {"lat": 40.42, "lng": -3.70},
    {"lat": 52.52, "lng": 13.41},
    {"lat": 59.33, "lng": 18.07},
    {"lat": 33.89, "lng": 35.50},
    {"lat": -26.20, "lng": 28.05},
    {"lat": 53.55, "lng": -113.49},
    {"lat": 43.65, "lng": -79.38},
]


def grid_key(lat: float, lng: float) -> tuple:
    """Return grid cell (row, col) for 1-degree dedup."""
    return (
        int(math.floor(lat / GRID_DEG)),
        int(math.floor(lng / GRID_DEG)),
    )


def fetch_blitzortung(auth: str) -> list[dict] | None:
    """
    Fetch recent strikes from Blitzortung protected API.

    Returns list of {"lat": float, "lng": float} dicts, or None on failure.
    """
    try:
        # Use URL with embedded credentials for HTTP Basic Auth
        url = BLITZORTUNG_URL
        if auth and "://" not in auth:
            # auth is just "user:pass"
            url = url.replace(
                "https://data.blitzortung.org/",
                f"https://{auth}@data.blitzortung.org/",
            )

        req = Request(url)
        req.add_header("User-Agent", "polar-plus/1.0 (Android live wallpaper)")

        logger.info(f"Fetching: {BLITZORTUNG_URL}")
        t0 = time.time()
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        logger.info(f"Downloaded {len(body):,} bytes in {time.time() - t0:.0f}s")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        cutoff_ns = int(cutoff.timestamp() * 1e9)

        strikes = []
        seen_grids = set()
        for line in body.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = obj.get("time", 0)
                lat = float(obj.get("lat", 0))
                lng = float(obj.get("lon", 0))

                # Skip old strikes
                if ts < cutoff_ns:
                    continue

                # Grid dedup
                gk = grid_key(lat, lng)
                if gk in seen_grids:
                    continue
                seen_grids.add(gk)

                strikes.append({"lat": lat, "lng": lng})
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

        logger.info(
            f"Parsed {len(strikes)} unique strikes in last {LOOKBACK_HOURS}h "
            f"(cutoff: {cutoff.isoformat()})"
        )
        return strikes if strikes else None

    except HTTPError as e:
        logger.error(f"HTTP {e.code}: {e.reason}")
        return None
    except URLError as e:
        logger.error(f"Connection failed: {e.reason}")
        return None
    except Exception as e:
        logger.exception("Unexpected error fetching strikes")
        return None


def save_storms(strikes: list[dict], output_dir: Path):
    """Write storms.json to output_dir/latest/."""
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    path = latest_dir / "storms.json"
    with open(path, "w") as f:
        json.dump(strikes, f, separators=(",", ":"))
    logger.info(f"Saved {len(strikes)} storms → {path} ({path.stat().st_size:,} bytes)")


def main():
    output_dir = Path(os.environ.get("OUTPUT_DIR", str(
        Path(__file__).resolve().parent / "output"
    )))

    auth = os.environ.get("BLITZORTUNG_AUTH", "").strip()

    strikes = None
    if auth:
        logger.info("BLITZORTUNG_AUTH set, attempting live fetch...")
        strikes = fetch_blitzortung(auth)
    else:
        logger.warning(
            "BLITZORTUNG_AUTH not set — "
            "using default placeholder locations. "
            "Set the secret in GitHub Actions for live lightning data."
        )

    if strikes is None:
        strikes = DEFAULT_LOCATIONS
        logger.info(f"Using {len(strikes)} default locations as placeholder.")

    # Limit to reasonable count for mobile app memory
    if len(strikes) > 2000:
        # Random sample to cap
        import random
        strikes = random.sample(strikes, 2000)
        logger.info(f"Capped to 2000 strikes.")

    save_storms(strikes, output_dir)


if __name__ == "__main__":
    main()
