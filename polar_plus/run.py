#!/usr/bin/env python3
"""
polar_plus/run.py — NASA GCC v2a + SSEC polar gap-fill pipeline

Flow:
  0. Read the timestamp already published (root.json) — the backward-search
     lower bound, so we never replace live data with something older
  1. Find the newest GCC v2a file whose BT_10.8um is actually complete,
     walking back an hour at a time (48h window, min age 2h)
  2. Remote-read BT_10.8um + cloud_phase via h5py
  3. BT → cloud density + gamma correction
  4. Downsample to target equirectangular
  5. SSEC gap-fill: only fill pixels where GCC density==0 in polar regions
  6. Post-process (threshold + linear stretch)
  7. Cubemap projection → 6-face JPG
  8. Stage output/latest for publishing (the publisher uploads that tree; the
     site root comes from POLAR_PUBLIC_BASE_URL, so the target host is not
     baked into this module)

If no complete GCC file can be found inside the allowed window, the process
exits non-zero *without* publishing anything, so the previous deployment
stays live rather than being replaced by partial data.
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

from polar_plus.config import (OUTPUT_DIR, FACE_SIZE, LON_OFFSET,
                                TARGET_W, TARGET_H,
                                ROOT_VERSION_KEY,
                                TILES_SUBPATH, LOCAL_BASE_URL_FALLBACK,
                                DEFAULT_VERSION_FILE, DEV_VERSION, VERSION_ENV,
                                describe_base_url, pipeline_version,
                                public_base_url)
from polar_plus.cubemap import equirect_to_cubemap
from polar_plus.gcc_load import NoHealthyGCCError, load_gcc_density
from polar_plus.health import describe_floor, resolve_floor_ts
from polar_plus.capfill import fill_gcc_gaps

# Exit code used when no complete GCC file is available. Non-zero on purpose:
# it fails the run so that nothing is published, leaving the live site
# serving the previous good data.
EXIT_NO_HEALTHY_GCC = 2


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


def repair_dateline_seam(density: np.ndarray) -> np.ndarray:
    """Rebuild the dateline columns so the equirectangular wrap is seamless.

    Two separate defects meet at lon 180:

    1. **The GCC composite's own grid is defective there.** Its outermost
       4 columns on each side carry ~37% missing values against ~0% in the
       interior (measured on the raw BT_10.8um: cols 0-3 and 12956-12959),
       so after downsampling column 0 is ~67% zero and its neighbour ~27%.

       This is *not* a PIL LANCZOS edge artifact -- feeding a clean
       synthetic 12960-wide image through the same resize leaves the edge
       columns indistinguishable from the interior, and zeroing a whole
       source column perturbs exactly one output column without spreading.
       An earlier version of this function blamed LANCZOS; that diagnosis
       was wrong.

    2. **Column 0 and column W-1 are the same meridian.**
       ``lon_grid = linspace(-180, 180, W)`` includes both endpoints, so
       sample_bilinear's horizontal wrap (``x1 = (x0 + 1) % W``) is only
       seamless when those two columns hold identical values.

    The previous fix copied columns 3/4/5 onto 0/1/2 (and mirrored on the
    right). That did remove the bad data, but because the copy source sits
    *inside* the overwritten range the content ran
    ``c3, c4, c5, c3, c4, c5, c6`` -- six columns that repeat once and jump
    two columns backwards in the middle. Each row of the nx cubemap face
    is one whole equirect column stretched across the full 1024 px width,
    so that 0.43 degree band became a ~4-row stripe spanning the entire
    face: a wide seam running from the north pole to the south pole
    (nx's rows 511/512 are exactly lon 180, and the polar caps pz/nz
    continue it into both poles).

    Fix: rebuild the band {W-3, W-2, W-1, 0, 1, 2} by linear interpolation
    in longitude between the last clean columns on either side (col 3 at
    lon -179.784 and col W-4 at lon +179.784, 0.43 degrees apart), then
    force column 0 and column W-1 to be identical. No duplication, no
    backward jump, and the wrap is exactly periodic.

    Must keep `col 0 == col W-1`: cubemap.sample_bilinear wraps with
    ``x1 = (x0 + 1) % w``, so a mismatch between the two ends of the row
    reappears as a seam at lon 180 in every face that straddles it.
    """
    w = density.shape[1]
    # The last clean columns flanking the defective band, ordered west -> east.
    # The band straddles lon 180 ({W-3, W-2, W-1} are +179.856..+180 and
    # {0, 1, 2} are -180..-179.856), so its WESTERN neighbour is W-4
    # (lon +179.784) and its EASTERN neighbour is col 3 (lon -179.784).
    west = density[:, w - 4].astype(np.float32)      # lon +179.784
    east = density[:, 3].astype(np.float32)          # lon -179.784
    # Fractional longitude of each rebuilt column between the two anchors.
    # W-1 and 0 are the same meridian (lon 180), so both take t = 0.5 and
    # come out identical by construction.
    for col, t in ((w - 3, 1.0 / 6), (w - 2, 2.0 / 6), (w - 1, 3.0 / 6),
                   (0, 3.0 / 6), (1, 4.0 / 6), (2, 5.0 / 6)):
        density[:, col] = np.clip(
            np.rint((1.0 - t) * west + t * east), 0, 255).astype(np.uint8)
    density[:, 0] = density[:, w - 1]
    logger.info(f"Dateline repair: rebuilt 6 cols across lon 180 "
                f"(col0==col{w - 1})")
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
    print("=== NASA GCC v2a + SSEC Polar Gap-Fill Pipeline ===")
    # Printed before any work: which build is running decides whether the
    # backward-search floor applies at all, so it belongs at the top of the log
    # rather than buried at the end.
    _version, _version_src = pipeline_version()
    print(f"    build: {_version}  (来源: {_version_src})\n")

    t0 = time.time()

    # Step 0: lower bound for the backward search — the newest timestamp
    # already published. Read before anything is downloaded. Never fatal:
    # on a first run there is nothing to read and we simply run unbounded
    # (bounded in practice by SEARCH_HOURS).
    #
    # `bootstrap` is True when the live root.json exists but carries no
    # publish marker, i.e. it was written by the old, ungated pipeline. That
    # version can itself be a half-written file, and the bound would then
    # block every healthy older candidate forever — so this first run drops
    # both bounds, publishes the newest healthy file and writes the marker.
    floor_ts, floor_src, bootstrap = resolve_floor_ts()
    print(f"[0/5] 回退下界（线上已发布的时间戳）: "
          f"{describe_floor(floor_ts, floor_src, bootstrap)}")

    # Step 1: GCC global data
    print("\n[1/5] Loading GCC v2a global cloud composite...")
    try:
        density, lat_grid, lon_grid, ts = load_gcc_density(
            target_w=TARGET_W, target_h=TARGET_H, floor_ts=floor_ts,
            bootstrap=bootstrap)
    except NoHealthyGCCError as exc:
        logger.error(f"未找到 BT_10.8um 完整健康的 GCC 文件：{exc}")
        logger.error("本次不产出、不发布；线上保持上一版数据")
        print(f"\n[ABORT] {exc}")
        print("本次不发布，线上数据保持不变。")
        sys.exit(EXIT_NO_HEALTHY_GCC)
    print(f"  Density: {density.shape}, "
          f"zeros={np.sum(density==0)/density.size*100:.1f}%")
    print(f"  Lat: {lat_grid[0]:.1f} to {lat_grid[-1]:.1f}")
    print(f"  Time: {time.time()-t0:.0f}s")

    # Save GCC source before gap-fill
    raw_path = OUTPUT_DIR / f"gcc_source_{ts}.png"
    Image.fromarray(density, mode='L').save(raw_path)
    print(f"\n  Saved GCC source: {raw_path}")

    # Emit GCC timestamp for the downstream lightning fusion (the container
    # entrypoint parses this, or reads it back out of root.json)
    print(f"GCC_TS={ts}")

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

    # Step 3.5: Rebuild the defective dateline columns (replaces the old
    # copy-based edge fix, which duplicated 3 columns and left a
    # pole-to-pole seam). Called exactly once.
    density = repair_dateline_seam(density)

    # Step 4: Cubemap projection
    print(f"\n[4/5] Equirectangular to cubemap...")
    ts_dir = OUTPUT_DIR / ts
    tiles_dir = ts_dir / "tiles"
    h, w = density.shape[:2]
    faces = equirect_to_cubemap(density, w, h, FACE_SIZE, LON_OFFSET)

    # Step 5: Save
    print(f"\n[5/5] Saving faces...")
    save_faces(faces, tiles_dir)

    # Build root.json. `baseUrl` is the only field that has to change when
    # the hosting moves, which is why the client (which reads it from
    # root.json) needs no rebuild to follow the tiles to a new host.
    site_base = public_base_url()
    if site_base is None:
        site_base = LOCAL_BASE_URL_FALLBACK
        logger.warning(
            "没有配置公开站点地址，root.json 将写入占位地址 %s —— "
            "请在发布前设置 %s", LOCAL_BASE_URL_FALLBACK,
            describe_base_url())
    base_url = f"{site_base}/{TILES_SUBPATH}/"
    print(f"  [PUBLISH TARGET] {describe_base_url()}")

    # Record which build produced this data. The next run compares it against
    # its own version: a mismatch (or no version at all) means "first run of a
    # new build", which drops the backward-search floor once. The value is the
    # git commit the code was built from — see pipeline_version() in config.py.
    running_version, _ = pipeline_version()
    if running_version == DEV_VERSION:
        logger.warning(
            "无法识别本次运行的构建版本（既无 %s，也无 %s，且没有 git 仓库）"
            "—— root.json 将写入 %r，此后每次运行都会被判为初次运行",
            VERSION_ENV, DEFAULT_VERSION_FILE, DEV_VERSION)
    root_data = {"baseUrl": base_url, "timestamp": ts,
                 ROOT_VERSION_KEY: running_version}

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
