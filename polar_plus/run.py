#!/usr/bin/env python3
"""
polar_plus/run.py — GMGSI + SSEC WMS 极区拼接管线

流程：
  1. 从 AWS S3 下载最新 GMGSI (60N-60S 核心)
  2. BT -> 云密度
  3. 从 SSEC RealEarth WMS 下载南北极区渲染图
  4. 直方图匹配 + 羽化拼接 -> 全球密度图
  5. Cubemap 投影 -> 6 面 JPG
"""
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from polar_plus.config import OUTPUT_DIR, FACE_SIZE, LON_OFFSET, TARGET_W, TARGET_H
from polar_plus.cubemap import equirect_to_cubemap
from polar_plus.gmgsi_load import load_core_density, post_process_density
from polar_plus.capfill import fill_polar_caps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("run")


def save_faces(faces: dict, tiles_dir: Path):
    tiles_dir.mkdir(parents=True, exist_ok=True)
    for face_name, face_img in faces.items():
        path = tiles_dir / f"{face_name}.jpg"
        face_img.convert('RGB').save(path, quality=95, optimize=True, subsampling=0)
        size_kb = path.stat().st_size / 1024
        print(f"  [SAVE] {path.name} ({size_kb:.0f} KB)")


def run_pipeline(api_key: str = ""):
    """完整管线"""
    print("=== GMGSI + SSEC WMS Polar Fill Pipeline ===\n")

    # Step 1: GMGSI 核心
    print("[1/4] Loading GMGSI core (60N-60S)...")
    t0 = time.time()
    core_density, lat_grid, lon_grid, ts = load_core_density(
        target_w=TARGET_W, max_days_back=14)
    print(f"  Core: {core_density.shape}, zeros={np.sum(core_density==0)/core_density.size*100:.1f}%")
    print(f"  Lat: {lat_grid[0]:.1f} to {lat_grid[-1]:.1f}")
    print(f"  Time: {time.time()-t0:.0f}s")

    # Step 2: 极区填充
    print(f"\n[2/4] Filling polar caps via SSEC WMS...")
    t1 = time.time()
    full_density = fill_polar_caps(core_density, lat_grid, lon_grid, api_key)
    print(f"  Full: {full_density.shape}")
    print(f"  Time: {time.time()-t1:.0f}s")

    # 保存调试图
    raw_path = OUTPUT_DIR / f"polar_plus_source_{ts}.png"
    Image.fromarray(full_density, mode='L').save(raw_path)
    print(f"\n  Saved source: {raw_path}")

    print(f"\n  Density stats: "
          f"min={full_density.min()}, max={full_density.max()}, "
          f"mean={full_density.mean():.1f}, "
          f"zeros={np.sum(full_density==0)/full_density.size*100:.1f}%")

    # Step 2.5: 密度后处理（阈值去噪 + Gamma 校正）
    print(f"\n[2.5/4] Post-processing density (threshold + gamma)...")
    full_density = post_process_density(full_density, threshold=45)

    # Step 3: Cubemap
    print(f"\n[3/4] Equirect to cubemap...")
    ts_dir = OUTPUT_DIR / ts
    tiles_dir = ts_dir / "tiles"
    h, w = full_density.shape[:2]
    faces = equirect_to_cubemap(full_density, w, h,
                                 FACE_SIZE, LON_OFFSET)

    # Step 4: 保存
    print(f"\n[4/4] Saving faces...")
    save_faces(faces, tiles_dir)

    # Use GH_PAGES_BASE env var if set (recommended), otherwise fall back
    # GitHub Pages URL format: https://<username>.github.io/<repo>/
    gh_pages_base = os.environ.get("GH_PAGES_BASE", "")
    if gh_pages_base:
        base_url = f"{gh_pages_base.rstrip('/')}/tiles/"
    else:
        # Fallback: construct from GITHUB_REPOSITORY
        repo = os.environ.get("GITHUB_REPOSITORY", "owner/polar_plus")
        owner = repo.split("/")[0].lower()
        repo_name = repo.split("/")[1] if "/" in repo else "polar_plus"
        base_url = f"https://{owner}.github.io/{repo_name}/tiles/"

    root_data = {"baseUrl": base_url, "timestamp": ts}

    # Save root.json to timestamped directory
    root_path = ts_dir / "root.json"
    with open(root_path, 'w') as f:
        json.dump(root_data, f)
    print(f"  [SAVE] root.json -> {root_path}")

    # Also stage to latest/ directory for stable app URL
    latest_dir = OUTPUT_DIR / "latest"
    latest_tiles = latest_dir / "tiles"
    latest_tiles.mkdir(parents=True, exist_ok=True)
    for face_name, face_img in faces.items():
        src = tiles_dir / f"{face_name}.jpg"
        dst = latest_tiles / f"{face_name}.jpg"
        shutil.copy2(src, dst)
    # Save root.json to latest/
    latest_root = latest_dir / "root.json"
    with open(latest_root, 'w') as f:
        json.dump(root_data, f)
    print(f"  [SAVE] latest/ staged -> {latest_dir}")

    print(f"\nDone! Total: {time.time()-t0:.0f}s")
    print(f"Output: {tiles_dir}")
    print(f"Latest: {latest_dir}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # API key 可选，从环境变量读取
    api_key = ""
    run_pipeline(api_key)
    print(f"\n[DONE] Files in {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
