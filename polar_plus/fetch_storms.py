#!/usr/bin/env python3
"""
polar_plus/fetch_storms.py — Fetch real-time lightning strikes via MQTT relay.

Connects to the public Blitzortung MQTT broker, subscribes to global
geo/# topics, collects strikes for a configurable duration, grid-dedup,
and outputs storms.json for the Android live wallpaper.

MQTT broker: blitzortung.ha.sed.pl:1883 (public, no auth required)

Fallback: uses DEFAULT_LOCATIONS if MQTT broker is unreachable.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("fetch_storms")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_BROKER = os.environ.get("MQTT_BROKER", "blitzortung.ha.sed.pl")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
COLLECT_SECONDS = int(os.environ.get("STORM_COLLECT_SECS", "60"))
GRID_DEG = 1.0  # Dedup grid cell size in degrees (≈ 111 km at equator)
MAX_STRIKES = 2000  # Cap output for mobile app memory

# Default locations when MQTT is unavailable (globally distributed major cities)
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


def fetch_via_mqtt(broker: str, port: int, collect_secs: int) -> list[dict]:
    """
    Connect to MQTT broker, subscribe to global geo/# topics,
    collect strikes for collect_secs, return deduped list.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.error("paho-mqtt not installed. Run: pip install paho-mqtt")
        return None

    strikes = []
    seen = set()

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", errors="replace"))
            lat = float(data.get("lat", 0))
            lng = float(data.get("lon", 0))
            gk = grid_key(lat, lng)
            if gk not in seen:
                seen.add(gk)
                strikes.append({"lat": lat, "lng": lng})
        except (ValueError, TypeError, json.JSONDecodeError):
            pass  # Skip malformed messages

    client = mqtt.Client()
    client.on_message = on_message
    client.connect_async(broker, port, 10)

    logger.info(f"Connected to MQTT broker {broker}:{port}, subscribing geo/+/# ...")
    client.subscribe("geo/+/#", qos=0)
    client.loop_start()

    deadline = time.time() + collect_secs
    while time.time() < deadline:
        time.sleep(0.5)
        if len(strikes) % 100 == 0 and len(strikes) > 0:
            logger.info(f"  Collected {len(strikes)} strikes so far...")

    client.loop_stop()
    client.disconnect()
    logger.info(f"MQTT collection done: {len(strikes)} unique strikes in {collect_secs}s")
    return strikes


def save_storms(strikes: list[dict], output_dir: Path):
    """Write storms.json to output_dir/latest/."""
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    if len(strikes) > MAX_STRIKES:
        import random
        strikes = random.sample(strikes, MAX_STRIKES)

    path = latest_dir / "storms.json"
    with open(path, "w") as f:
        json.dump(strikes, f, separators=(",", ":"))
    logger.info(f"Saved {len(strikes)} storms -> {path} ({path.stat().st_size:,} bytes)")


def main():
    output_dir = Path(os.environ.get("OUTPUT_DIR", str(
        Path(__file__).resolve().parent / "output"
    )))

    logger.info(f"Fetching lightning strikes via MQTT ({MQTT_BROKER}:{MQTT_PORT})...")
    strikes = fetch_via_mqtt(MQTT_BROKER, MQTT_PORT, COLLECT_SECONDS)

    if strikes is None or len(strikes) == 0:
        logger.warning("MQTT fetch returned no data, using default locations")
        strikes = DEFAULT_LOCATIONS

    save_storms(strikes, output_dir)


if __name__ == "__main__":
    main()
