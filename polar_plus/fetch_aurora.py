#!/usr/bin/env python3
"""
polar_plus/fetch_aurora.py — NOAA OVATION aurora oval contour pipeline.

Fetches the OVATION-Prime aurora probability grid (360×181, 1-degree
resolution) from NOAA SWPC, filters zero-probability entries, fits
oval contours per 5-degree longitude sector, and outputs a compact
JSON suitable for direct upload as GLSL uniform float arrays.

Output: {OUTPUT_DIR}/latest/aurora.json  (≈2.4 KB)
"""
import json
import logging
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("fetch_aurora")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CDN_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
SECTOR_RES_DEG = 5
SECTOR_COUNT = 360 // SECTOR_RES_DEG  # 72

REQUEST_HEADERS = {
    "User-Agent": "AuroraPipeline/1.0 (GitHub Actions)",
    "Accept": "application/json",
}
REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = REQUEST_TIMEOUT) -> dict:
    """Fetch JSON from a URL. Returns parsed data or raises."""
    req = Request(url, headers=REQUEST_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Oval contour fitting
# ---------------------------------------------------------------------------

def fit_ovals(coords: list) -> tuple:
    """Process raw OVATION probability grid into 72-sector oval contours.

    Returns (north_ovals, south_ovals) — each is a list of 72
    [centre_lat, half_width, peak_prob] triples.
    """
    # Normalise probabilities and filter zeros
    nonzero = [(float(lon), float(lat), prob / 100.0)
               for lon, lat, prob in coords if prob > 0]

    logger.info(
        "Grid: %s points, %s non-zero (%.1f%%)",
        f"{len(coords):,}", f"{len(nonzero):,}",
        100 * len(nonzero) / len(coords)
    )
    if nonzero:
        pmin = min(p[2] for p in nonzero)
        pmax = max(p[2] for p in nonzero)
        logger.info("Probability range: %.3f – %.3f", pmin, pmax)

    # Group by 5-degree longitude sector
    sectors_north = [[] for _ in range(SECTOR_COUNT)]
    sectors_south = [[] for _ in range(SECTOR_COUNT)]

    for lon, lat, prob in nonzero:
        s = int(lon // SECTOR_RES_DEG) % SECTOR_COUNT
        if lat >= 0:
            sectors_north[s].append((lat, prob))
        else:
            sectors_south[s].append((lat, prob))

    def fit_sector(points: list, min_peak: float = 0.03) -> list:
        """Fit one sector: [centre_lat, half_width, peak_prob].

        Uses 50%-of-peak FWHM threshold for half-width.
        Sectors with no data or peak < min_peak return [0, 0, 0].
        """
        if not points:
            return [0.0, 0.0, 0.0]
        peak_lat, peak_prob = max(points, key=lambda p: p[1])
        if peak_prob < min_peak:
            return [0.0, 0.0, 0.0]
        # Half-width at 50 % of peak probability
        threshold = peak_prob * 0.5
        in_oval = sorted([p[0] for p in points if p[1] >= threshold])
        if len(in_oval) >= 2:
            half_width = (in_oval[-1] - in_oval[0]) / 2.0
        elif in_oval:
            half_width = 1.5
        else:
            half_width = 1.5
        half_width = max(1.0, min(half_width, 12.0))
        return [round(peak_lat, 1),
                round(half_width, 1),
                round(peak_prob, 3)]

    north = [fit_sector(sectors_north[s]) for s in range(SECTOR_COUNT)]
    south = [fit_sector(sectors_south[s]) for s in range(SECTOR_COUNT)]
    return north, south


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_aurora(forecast_time: str, north: list, south: list,
                output_dir: Path):
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    output = {
        "forecast_time": forecast_time,
        "resolution_deg": SECTOR_RES_DEG,
        "sector_count": SECTOR_COUNT,
        "north": north,
        "south": south,
    }

    path = latest_dir / "aurora.json"
    json_text = json.dumps(output, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(json_text)

    size_kb = len(json_text.encode("utf-8")) / 1024
    active_n = sum(1 for s in north if s[2] > 0)
    active_s = sum(1 for s in south if s[2] > 0)
    logger.info(
        "Saved %s (%s KB) → %s | active sectors: N=%s/72 S=%s/72",
        path.name, f"{size_kb:.1f}", path, active_n, active_s
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    output_dir = Path(os.environ.get("OUTPUT_DIR", str(
        Path(__file__).resolve().parent / "output"
    )))

    logger.info("Fetching NOAA OVATION aurora data from %s", CDN_URL)

    try:
        data = fetch_json(CDN_URL)
        coords = data.get("coordinates")
        if not coords or len(coords) < 100:
            logger.error(
                "Unexpected data format, keys: %s",
                list(data.keys())[:5] if data else "None"
            )
            sys.exit(1)

        forecast_time = data.get("Forecast Time", "")
        logger.info("Fetched %s coordinates, forecast: %s",
                     f"{len(coords):,}", forecast_time)

        north_oval, south_oval = fit_ovals(coords)

    except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
        logger.error("Failed to fetch NOAA data: %s", e)
        sys.exit(1)

    save_aurora(forecast_time, north_oval, south_oval, output_dir)


if __name__ == "__main__":
    main()
