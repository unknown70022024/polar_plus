#!/usr/bin/env python3
"""
polar_plus/fetch_storms.py — Download lightning strikes from original proxy server.

Primary: weather-proxy.456544.xyz (protobuf format, still alive as of 2026-07)
Fallback: Default city locations if the proxy goes offline.

Protobuf wire format (manual parse, no library dependency):
  message StormLocations { repeated LatLng locations = 1; }
  message LatLng { float latDeg = 1; float lngDeg = 2; }

Output: {OUTPUT_DIR}/latest/storms.json  →  [{"lat":34.0,"lng":-118.0}, ...]
"""

import json
import logging
import math
import os
import random
import struct
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

# Original proxy server (still serving real Blitzortung data as of 2026-07)
ORIGINAL_STORMS_URL = "https://weather-proxy.456544.xyz/pixel/livewallpaper/myworld/storm_locations"

GRID_DEG = 1.0     # Dedup grid cell ≈ 111 km at equator
MAX_STRIKES = 2000

# Default locations if all sources are unreachable
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


def fetch_protobuf_storms(url: str) -> list[dict] | None:
    """
    Download and parse protobuf-encoded storm locations from the original proxy.

    Wire format (no protobuf library needed):
      - Repeated field 1 (locations): tag=0x0A, wire_type=2 (length-delimited)
        - field 1 (latDeg): tag=0x0D, wire_type=5 (fixed32/float, 4 bytes)
        - field 2 (lngDeg): tag=0x15, wire_type=5 (fixed32/float, 4 bytes)
    """
    try:
        req = Request(url)
        req.add_header("User-Agent", "polar-plus/1.0 (Android live wallpaper)")
        with urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                logger.warning(f"Original storms URL returned {resp.status}")
                return None
            data = resp.read()
    except (HTTPError, URLError, OSError) as e:
        logger.warning(f"Failed to download from original storms URL: {e}")
        return None

    strikes = []
    seen_grids = set()
    pos = 0

    try:
        while pos < len(data):
            tag = data[pos]
            pos += 1
            field = tag >> 3
            wtype = tag & 0x07

            if field == 1 and wtype == 2:
                # Read varint length
                length = 0
                shift = 0
                while True:
                    b = data[pos]
                    pos += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7

                end = pos + length
                lat = lng = None

                while pos < end:
                    stag = data[pos]
                    pos += 1
                    sfield = stag >> 3
                    swtype = stag & 0x07

                    if sfield == 1 and swtype == 5:
                        lat = struct.unpack('<f', data[pos:pos + 4])[0]
                        pos += 4
                    elif sfield == 2 and swtype == 5:
                        lng = struct.unpack('<f', data[pos:pos + 4])[0]
                        pos += 4
                    else:
                        # Unknown field — skip (shouldn't happen)
                        break

                if lat is not None and lng is not None:
                    gk = grid_key(lat, lng)
                    if gk not in seen_grids:
                        seen_grids.add(gk)
                        strikes.append({"lat": round(lat, 4), "lng": round(lng, 4)})
            else:
                break  # End of locations
    except (IndexError, struct.error) as e:
        logger.warning(f"Protobuf parse error: {e}")

    logger.info(f"Original proxy: {len(strikes)} strikes parsed from {len(data)} bytes")
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

    # Primary: original proxy server (real Blitzortung data in protobuf)
    logger.info("Fetching lightning strikes from original proxy...")
    strikes = fetch_protobuf_storms(ORIGINAL_STORMS_URL)

    # Fallback: default city locations
    if strikes is None or len(strikes) == 0:
        logger.warning("Original proxy unavailable, using default locations")
        strikes = DEFAULT_LOCATIONS

    save_storms(strikes, output_dir)


if __name__ == "__main__":
    main()
