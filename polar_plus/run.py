#!/usr/bin/env python3
"""
polar_plus/run.py — NASA GCC v2a + SSEC polar gap-fill pipeline

Flow:
  1. Find latest GCC v2a file (96h search, 30-min slots)
  2. Remote-read BT_10.8um + cloud_phase via h5netcdf
  3. BT → cloud density + gamma correction
  4. Downsample to target equirectangular
  5. SSEC gap-fill: only fill pixels where GCC density==0 in polar regions
  6. Post-process (threshold + linear stretch)
  7. Cubemap projection → 6-face JPG
  8. Deploy to GitHub Pages (via GitHub Actions)
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from polar_plus.config import (OUTPUT_DIR, FACE_SIZE, LON_OFFSET,
                                TARGET_W, TARGET_H)
from polar_plus.cubemap import equirect_to_cubemap
from polar_plus.gcc_load import load_gcc_density
from polar_plus.capfill import fill_gcc_gaps


def _post_process(density: np.ndarray, threshold: int = 45) -> np.ndarray:
    """Remove low-density noise and apply linear contrast stretch.

    threshold: pixel values below this become 0 (clear sky).
    Linear stretch: [threshold, max] → [0, 255] preserves cloud feature
    geometry without the centroid shift that gamma correction causes.
    """
    density = np.where(density < threshold, 0, density)
    valid = density[density > 0]
    if len(valid) > 1:
        vmin, vmax = valid.min(), valid.max()
        if vmax > vmin:
            density = np.clip(
                (density.astype(np.float32) - vmin) / (vmax - vmin) * 255.0,
                0, 255
            ).astype(np.uint8)
    logger.info(
        f"Post-process: threshold={threshold}, linear stretch, "
        f"zeros={np.sum(density == 0) / density.size * 100:.1f}%"
    )
    return density

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("run")


def fix_edges_copy(density: np.ndarray, n: int = 3) -> np.ndarray:
    """Replace edge columns with copies of interior columns.

    PIL LANCZOS downsampling truncates the kernel at image edges,
    producing anomalous values in the first few columns. The left and
    right edges represent the same longitude (180°) and should match,
    but LANCZOS computes them independently → mismatch at the dateline.

    The nx cubemap face centre row maps entirely to col 0, so any
    anomaly there becomes a visible north-south seam on the globe.

    Fix: overwrite the first/last N columns with copies from N columns
    deeper into the interior, bypassing the LANCZOS edge artifact.
    """
    for i in range(n):
        density[:, i] = density[:, n + i]           # col 0 = col n
        density[:, -(i + 1)] = density[:, -(n + i + 1)]  # col -1 = col -(n+1)
    logger.info(f"Edge fix: replaced {n} edge cols with interior copies")
    return density


def save_faces(faces: dict, tiles_dir: Path):
    tiles_dir.mkdir(parents=True, exist_ok=True)
    for face_name, face_img in faces.items():
        path = tiles_dir / f"{face_name}.jpg"
        face_img.convert('RGB').save(path, quality=95, optimize=True, subsampling=0)
        size_kb = path.stat().st_size / 1024
        print(f"  [SAVE] {path.name} ({size_kb:.0f} KB)")


def run_pipeline(api_key: str = ""):
    """GCC v2a + SSEC gap-fill pipeline."""
    print("=== NASA GCC v2a + SSEC Polar Gap-Fill Pipeline ===\n")

    t0 = time.time()

    # Step 1: GCC global data
    print("[1/5] Loading GCC v2a global cloud composite...")
    density, lat_grid, lon_grid, ts = load_gcc_density(
        target_w=TARGET_W, target_h=TARGET_H)
    print(f"  Density: {density.shape}, "
          f"zeros={np.sum(density==0)/density.size*100:.1f}%")
    print(f"  Lat: {lat_grid[0]:.1f} to {lat_grid[-1]:.1f}")
    print(f"  Time: {time.time()-t0:.0f}s")

    # Save GCC source before gap-fill
    raw_path = OUTPUT_DIR / f"gcc_source_{ts}.png"
    Image.fromarray(density, mode='L').save(raw_path)
    print(f"\n  Saved GCC source: {raw_path}")

    # Step 2: SSEC polar gap-fill
    print(f"\n[2/5] SSEC polar gap-fill (matching timestamp {ts})...")
    t1 = time.time()
    density = fill_gcc_gaps(density, lat_grid, lon_grid, ts, api_key)
    print(f"  Time: {time.time()-t1:.0f}s")

    # Save post-gap-fill debug image
    gapfill_path = OUTPUT_DIR / f"gapfill_{ts}.png"
    Image.fromarray(density, mode='L').save(gapfill_path)
    print(f"  Saved gap-filled: {gapfill_path}")

    # Step 3: Post-process
    print(f"\n[3/5] Post-processing density (threshold + linear stretch)...")
    density = _post_process(density, threshold=45)

    print(f"\n  Density stats: "
          f"min={density.min()}, max={density.max()}, "
          f"mean={density.mean():.1f}, "
          f"zeros={np.sum(density==0)/density.size*100:.1f}%")

    # Step 3.5: Fix dateline edge artifact
    density = fix_edges_copy(density)

    # Step 4: Cubemap projection
    print(f"\n[4/5] Equirectangular to cubemap...")
    ts_dir = OUTPUT_DIR / ts
    tiles_dir = ts_dir / "tiles"
    h, w = density.shape[:2]
    faces = equirect_to_cubemap(density, w, h, FACE_SIZE, LON_OFFSET)

    # Step 5: Save
    print(f"\n[5/5] Saving faces...")
    save_faces(faces, tiles_dir)

    # Build root.json with GitHub Pages URL
    gh_pages_base = os.environ.get("GH_PAGES_BASE", "")
    if gh_pages_base:
        base_url = f"{gh_pages_base.rstrip('/')}/tiles/"
    else:
        repo = os.environ.get("GITHUB_REPOSITORY", "owner/polar_plus")
        owner = repo.split("/")[0].lower()
        repo_name = repo.split("/")[1] if "/" in repo else "polar_plus"
        base_url = f"https://{owner}.github.io/{repo_name}/tiles/"

    root_data = {"baseUrl": base_url, "timestamp": ts}

    # root.json → timestamped dir
    root_path = ts_dir / "root.json"
    with open(root_path, 'w') as f:
        json.dump(root_data, f)
    print(f"  [SAVE] root.json -> {root_path}")

    # Stage to latest/ for stable app URL
    latest_dir = OUTPUT_DIR / "latest"
    latest_tiles = latest_dir / "tiles"
    latest_tiles.mkdir(parents=True, exist_ok=True)
    for face_name in faces:
        src = tiles_dir / f"{face_name}.jpg"
        dst = latest_tiles / f"{face_name}.jpg"
        shutil.copy2(src, dst)
    latest_root = latest_dir / "root.json"
    with open(latest_root, 'w') as f:
        json.dump(root_data, f)
    print(f"  [SAVE] latest/ staged -> {latest_dir}")

    print(f"\nDone! Total: {time.time()-t0:.0f}s")
    print(f"Output: {tiles_dir}")
    print(f"Latest: {latest_dir}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("SSEC_API_KEY", "")
    run_pipeline(api_key)
    print(f"\n[DONE] Files in {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
