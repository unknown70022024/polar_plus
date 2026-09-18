"""
tests/test_capfill_reproject.py — the Mercator → equirectangular resample.

``mercator_to_equirect`` was rewritten from a per-pixel Python loop (3.24 s for
the two polar bands, the largest CPU item in the pipeline) into vectorised
numpy (0.15 s, 21x). Speed was not the point though — the SSEC composite gets
blended into the GCC polar caps, so a one-LSB difference would surface as a
visible seam. The scalar loop is therefore kept here **as the reference** and
the vectorised version must match it bit for bit.

If this test ever fails, the fix is not to relax the tolerance. Byte equality
is the contract.

Run:  python -m pytest tests/test_capfill_reproject.py -q
      python tests/test_capfill_reproject.py      (no pytest needed)
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polar_plus import capfill                                    # noqa: E402


# ---------------------------------------------------------------------------
# the original implementation, verbatim, as the reference
# ---------------------------------------------------------------------------

def scalar_reference(merc: np.ndarray, lat_min: float, lat_max: float,
                     target_w: int) -> np.ndarray:
    """The per-pixel loop exactly as it shipped before vectorisation."""
    mh, mw = merc.shape
    total_px = capfill.N_TILES * capfill.TILE_SIZE
    band_deg = lat_max - lat_min
    target_h = max(1, int(round(band_deg / 180.0 * target_w * 0.5)))
    result = np.zeros((target_h, target_w), dtype=np.uint8)

    for ty in range(target_h):
        lat = lat_max - (ty + 0.5) / target_h * band_deg
        my = capfill._lat_to_my(lat)
        if my < 1 or my >= mh - 2:
            continue
        for tx in range(target_w):
            lon = -180.0 + (tx + 0.5) / target_w * 360.0
            mx = (lon + 180.0) / 360.0 * total_px
            ix = int(mx) % mw
            fx = mx - math.floor(mx)
            iy = int(my)
            fy = my - iy
            ix1 = (ix + 1) % mw
            iy1 = min(iy + 1, mh - 1)
            v00 = float(merc[iy, ix])
            v10 = float(merc[iy, ix1])
            v01 = float(merc[iy1, ix])
            v11 = float(merc[iy1, ix1])
            val = (v00 * (1.0 - fx) * (1.0 - fy) + v10 * fx * (1.0 - fy) +
                   v01 * (1.0 - fx) * fy + v11 * fx * fy)
            result[ty, tx] = max(0, min(255, int(round(val))))
    return result


SHAPES = [(1024, 1024), (1024, 512), (256, 256), (128, 128), (64, 64)]
BANDS = [(60.0, 85.0), (-85.0, -60.0), (45.0, 60.0), (-60.0, -45.0),
         (80.0, 89.9), (89.0, 90.0), (60.0, 60.5)]


class TestBitIdentical(unittest.TestCase):

    def test_matches_scalar_reference(self):
        rng = np.random.default_rng(7)
        checked = 0
        for shape in SHAPES:
            merc = rng.integers(0, 256, shape, dtype=np.uint8)
            for lat_min, lat_max in BANDS:
                with self.subTest(shape=shape, lat=(lat_min, lat_max)):
                    want = scalar_reference(merc, lat_min, lat_max, 5000)
                    got = capfill.mercator_to_equirect(merc, lat_min, lat_max,
                                                       5000)
                    self.assertEqual(got.shape, want.shape)
                    self.assertEqual(got.dtype, want.dtype)
                    if not np.array_equal(got, want):
                        diff = np.argwhere(got != want)
                        r, c = diff[0]
                        self.fail(f"{len(diff)} 处不同，首个 ({r},{c}): "
                                  f"参考 {want[r, c]} vs 向量化 {got[r, c]}")
                    checked += 1
        self.assertEqual(checked, len(SHAPES) * len(BANDS))

    def test_matches_on_a_realistic_mercator_tile(self):
        # Constant and ramped inputs catch an index transposition that uniform
        # noise can miss: with random bytes a swapped iy/ix still looks wrong
        # in many places, but a constant field would agree everywhere.
        rng = np.random.default_rng(11)
        # NB: np.arange(1024, dtype=np.uint8) wraps at 255 and is NOT a ramp —
        # it is a sawtooth. Build 0..255 spread over 1024 columns instead.
        ramp_1d = (np.arange(1024) // 4).astype(np.uint8)
        base = ramp_1d[None, :].repeat(1024, axis=0)     # varies with longitude
        ramp = ramp_1d[:, None].repeat(1024, axis=1)     # varies with latitude
        for name, merc in (("常数", np.full((1024, 1024), 137, np.uint8)),
                           ("经度渐变", base),
                           ("纬度渐变", ramp),
                           ("噪声", rng.integers(0, 256, (1024, 1024),
                                                 dtype=np.uint8))):
            with self.subTest(input=name):
                for lat_min, lat_max in ((60.0, 85.0), (-85.0, -60.0)):
                    want = scalar_reference(merc, lat_min, lat_max, 5000)
                    got = capfill.mercator_to_equirect(merc, lat_min, lat_max,
                                                       5000)
                    self.assertTrue(np.array_equal(got, want),
                                    f"{name} {lat_min}..{lat_max} 不一致")

    def test_small_mercator_does_not_raise(self):
        """A Mercator smaller than the z=2 space must be skipped, not crash.

        The loop `continue`d past rows whose ``my`` fell outside the image.
        The vectorised form computes all rows, so its indices have to be
        clamped or a 256x256 input raises IndexError. That regression was
        caught while writing this test.
        """
        merc = np.full((256, 256), 200, np.uint8)
        for lat_min, lat_max in BANDS:
            with self.subTest(lat=(lat_min, lat_max)):
                want = scalar_reference(merc, lat_min, lat_max, 5000)
                got = capfill.mercator_to_equirect(merc, lat_min, lat_max, 5000)
                self.assertTrue(np.array_equal(got, want))

    def test_skipped_rows_stay_zero(self):
        # Rows outside the Mercator range must remain 0 (the loop's `continue`
        # left them untouched), not carry a value from a clamped index.
        merc = np.full((1024, 1024), 255, np.uint8)
        got = capfill.mercator_to_equirect(merc, 89.0, 90.0, 5000)
        # band_deg = 1 -> round(1/180 * 5000 * 0.5) = 14 rows
        self.assertEqual(got.shape, (14, 5000))
        # whichever rows were skipped must be all-zero
        lats = 90.0 - (np.arange(got.shape[0]) + 0.5) / got.shape[0] * 1.0
        my = np.array([capfill._lat_to_my(float(v)) for v in lats])
        skipped = (my < 1) | (my >= 1024 - 2)
        self.assertTrue((got[skipped] == 0).all(),
                        "被跳过的行不应被写入")
        self.assertTrue((got[~skipped] == 255).all(),
                        "未跳过的行应全部为 255（常数输入）")


class TestContract(unittest.TestCase):

    def test_shape_and_dtype(self):
        for target_w in (1000, 5000):
            merc = np.zeros((1024, 1024), np.uint8)
            out = capfill.mercator_to_equirect(merc, 60.0, 85.0, target_w)
            expect_h = max(1, int(round(25.0 / 180.0 * target_w * 0.5)))
            self.assertEqual(out.shape, (expect_h, target_w))
            self.assertEqual(out.dtype, np.uint8)

    def test_horizontal_ramp_is_monotonic_except_at_the_wrap(self):
        # Sanity: one output row spans the whole Mercator width, so a horizontal
        # ramp comes back increasing — EXCEPT at the right edge, where ix1 wraps
        # from the last column back to column 0. That wrap is intentional: the
        # two ends of an equirect row are the same meridian, so blending across
        # it is what keeps the band continuous at lon 180. Asserting global
        # monotonicity here would be asserting away that behaviour.
        merc = (np.arange(1024) // 4).astype(np.uint8)[None, :].repeat(1024, axis=0)
        out = capfill.mercator_to_equirect(merc, 60.0, 85.0, 1024)
        row = out[out.shape[0] // 2].astype(int)
        self.assertTrue((np.diff(row[:1020]) >= 0).all(),
                        "除环绕处外，经度渐变应单调不减")
        self.assertLess(row[-1], row[-2],
                        "最右列应因环绕到第 0 列而下降")


if __name__ == "__main__":
    unittest.main(verbosity=2)
