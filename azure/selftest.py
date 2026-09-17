#!/usr/bin/env python3
"""
azure/selftest.py — prove the container computes correctly WITHOUT
downloading a gigabyte from NASA.

Why this exists
---------------
A true end-to-end run needs ~1 GB of Range reads from satcorps.larc.nasa.gov.
On a slow link that is many hours (measured: 0.10 Mbit/s from one dev machine
= 22 hours for 1 GB), which makes "does the container work?" untestable.

But the download is only the *input* side. Everything else — post-processing,
the dateline repair, the cubemap projection, the JPEG encode — is pure local
compute, and it is exactly the part a container migration can break
(different numpy/Pillow version, missing native lib, wrong working directory).

So this script feeds a REAL density map (the gap-filled PNG the pipeline
already wrote) through the same functions the pipeline calls, and compares the
result against the tiles that same run actually published. If they match, the
compute path in this container is equivalent to the one that produced the live
data.

Usage (inside the container):
    python azure/selftest.py --from-output /data/output --timestamp 20260915_130000

    # or let it pick the newest timestamp that has both a gapfill PNG and tiles
    python azure/selftest.py --from-output /data/output

Exit code 0 = parity within tolerance, 1 = mismatch.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Running this as a script puts azure/ (not the repo root) on sys.path, so
# "import polar_plus" would fail outside the container. The image sets
# PYTHONPATH=/app, but make it work from a bare checkout too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polar_plus.config import FACE_SIZE, LON_OFFSET, TARGET_H, TARGET_W, FACES
from polar_plus.cubemap import equirect_to_cubemap
from polar_plus.run import _post_process, repair_dateline_seam

# Compare JPEG against JPEG. The reference tiles are JPEGs, so comparing them
# with raw computed pixels measures JPEG rounding rather than compute error —
# that alone produces mean ~0.5-1.0 and max ~11 on cloud edges, which looks
# like a regression and is not. Re-encoding the generated face with the same
# settings the pipeline uses (quality=95, optimize=True, subsampling=0) and
# decoding both sides isolates the actual compute difference.
#
# With identical pixels the two encodes should be byte-identical, so the
# tolerances only need to absorb JPEG encoder drift between Pillow versions.
MEAN_TOLERANCE = 0.5
MAX_TOLERANCE = 8


def _reencode_jpeg(img: Image.Image) -> Image.Image:
    """Round-trip an image through JPEG exactly as save_faces() writes it."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95,
                            optimize=True, subsampling=0)
    buf.seek(0)
    return Image.open(buf).convert("L")


def find_timestamp(output_dir: Path, requested: str | None) -> str:
    if requested:
        return requested
    candidates = []
    for png in sorted(output_dir.glob("gapfill_*.png")):
        ts = png.stem.replace("gapfill_", "")
        if (output_dir / ts / "tiles").is_dir():
            candidates.append(ts)
    if not candidates:
        raise SystemExit(
            f"no gapfill_*.png with a matching tiles/ directory in {output_dir}.\n"
            f"Pass --from-output pointing at a directory the pipeline has "
            f"written, or run the pipeline once first.")
    return candidates[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-output", default="/data/output",
                    help="directory holding gapfill_<ts>.png and <ts>/tiles/")
    ap.add_argument("--timestamp", default=None,
                    help="which run to verify (default: newest available)")
    ap.add_argument("--out", default=None,
                    help="write the regenerated tiles here for eyeballing")
    ap.add_argument("--face-size", type=int, default=FACE_SIZE)
    args = ap.parse_args()

    output_dir = Path(args.from_output)
    ts = find_timestamp(output_dir, args.timestamp)
    gapfill = output_dir / f"gapfill_{ts}.png"
    published = output_dir / ts / "tiles"

    print(f"timestamp : {ts}")
    print(f"input     : {gapfill}")
    print(f"reference : {published}")

    if not gapfill.is_file():
        raise SystemExit(f"missing input image: {gapfill}")

    density = np.asarray(Image.open(gapfill).convert("L"))
    print(f"input size: {density.shape} (expected {(TARGET_H, TARGET_W)})")
    if density.shape != (TARGET_H, TARGET_W):
        print(f"  WARNING: shape differs from TARGET_H x TARGET_W "
              f"({TARGET_H}x{TARGET_W})")

    # Exactly the pipeline's own sequence, from run.py.
    density = _post_process(density, threshold=45)
    density = repair_dateline_seam(density)
    faces = equirect_to_cubemap(density, TARGET_W, TARGET_H,
                                args.face_size, LON_OFFSET)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    print()
    print(f"{'face':6} {'mean|d|':>8} {'max|d|':>7} {'pct>2':>7}  verdict")
    for name in FACES:
        ref_path = published / f"{name}.jpg"
        if not ref_path.is_file():
            print(f"{name:6} {'-':>8} {'-':>7} {'-':>7}  MISSING reference")
            failures += 1
            continue

        gen = faces[name]
        if out_dir:
            gen.convert("RGB").save(out_dir / f"{name}.jpg", quality=95,
                                    optimize=True, subsampling=0)

        ref = np.asarray(Image.open(ref_path).convert("L"), dtype=np.int16)
        got = np.asarray(_reencode_jpeg(gen), dtype=np.int16)
        if ref.shape != got.shape:
            print(f"{name:6} {'-':>8} {'-':>7} {'-':>7}  SHAPE {ref.shape} vs "
                  f"{got.shape}")
            failures += 1
            continue

        diff = np.abs(ref - got)
        mean_d = float(diff.mean())
        max_d = int(diff.max())
        pct = float((diff > 2).mean() * 100)

        ok = mean_d <= MEAN_TOLERANCE and max_d <= MAX_TOLERANCE
        if not ok:
            failures += 1
        print(f"{name:6} {mean_d:8.3f} {max_d:7d} {pct:6.2f}%  "
              f"{'ok' if ok else 'MISMATCH'}")

    print()
    if failures:
        print(f"FAIL: {failures} face(s) outside tolerance "
              f"(mean<={MEAN_TOLERANCE}, max<={MAX_TOLERANCE})")
        return 1
    print("PASS: container compute matches the published tiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
