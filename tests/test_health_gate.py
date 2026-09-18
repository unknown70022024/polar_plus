"""
tests/test_health_gate.py — the BT_10.8um completeness gate.

The gate decides whether a GCC file is published. Its failure modes are
silent in production: a false reject just keeps serving older data, and a
false accept publishes black holes that stay live for hours. So the rules are
pinned here, including the two real cases the criterion change was made to
fix.

Run:  python -m pytest tests/test_health_gate.py -q
      python tests/test_health_gate.py          (no pytest needed)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polar_plus import config, health                          # noqa: E402

ROWS = config.BT_N_ROWS
COLS = config.BT_N_COLS
BAND = config.BT_N_ROWS // config.BT_N_BANDS
FILL_LAT = config.HEALTH_FILL_LAT
LIMIT = config.HEALTH_UNFILLABLE_MAX_PCT


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def blank() -> np.ndarray:
    return np.zeros((ROWS, COLS), dtype=bool)


def bandify(full: np.ndarray):
    """Yield (band_mask, row0) the way gcc_load reads a file."""
    for b in range(config.BT_N_BANDS):
        yield full[b * BAND:(b + 1) * BAND], b * BAND


def stream_verdict(full: np.ndarray) -> tuple[health.BtHealth, int]:
    """Verdict via the production banded path; also returns bands read.

    Mirrors ``gcc_load._read_bt_banded``: feed bands in order and stop as soon
    as the running unfillable loss has already crossed the line. The band count
    matters because it is what an early abort actually saves.

    Note that on an early abort the returned ``BtHealth`` carries only the
    **partial** accumulated totals — that is exactly what the pipeline logs
    when it rejects. Use ``stream_verdict_all_bands`` to compare against a
    whole-array verdict.
    """
    acc = health.BtHealthAccumulator()
    read = 0
    for band, row0 in bandify(full):
        read += 1
        acc.add_band(band, row0)
        if acc.exceeded:
            return acc.finalize(), read
    return acc.finalize(), read


def stream_verdict_all_bands(full: np.ndarray) -> health.BtHealth:
    """Same accumulation with the abort suppressed, for equivalence checks."""
    acc = health.BtHealthAccumulator()
    for band, row0 in bandify(full):
        acc.add_band(band, row0)
    return acc.finalize()


def mask_with(unfillable_pct: float = 0.0,
              fillable_pct: float = 0.0) -> np.ndarray:
    """Build a mask whose area losses are as close as row granularity allows.

    Fills whole rows (all longitudes) in descending area order, which is the
    cheapest way to hit a target loss without solving a subset-sum. The result
    is quantised by one row's area share (~0.024% near the equator), so callers
    must compare with a tolerance rather than for equality.
    """
    w = np.asarray(health.row_area_weights(ROWS))
    mid = health.unfillable_row_mask()
    full = blank()

    def fill(target_pct: float, allowed: np.ndarray) -> None:
        remaining = target_pct / 100.0
        order = np.argsort(-w)
        for r in order:
            if remaining <= 0:
                break
            if not allowed[r] or full[r].any():
                continue
            full[r, :] = True
            remaining -= w[r]

    fill(unfillable_pct, mid)
    fill(fillable_pct, ~mid)
    return full


def loss_of(full: np.ndarray) -> tuple[float, float]:
    w = np.asarray(health.row_area_weights(ROWS))
    mid = health.unfillable_row_mask()
    per_row = full.mean(axis=1)
    return (float((per_row[mid] * w[mid]).sum()) * 100,
            float((per_row[~mid] * w[~mid]).sum()) * 100)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

class TestGeometry(unittest.TestCase):

    def test_weights_sum_to_one(self):
        w = np.asarray(health.row_area_weights(ROWS))
        self.assertAlmostEqual(w.sum(), 1.0, places=12)

    def test_no_zero_weight_rows(self):
        # A zero weight would silently drop a row from the area accounting.
        w = np.asarray(health.row_area_weights(ROWS))
        self.assertTrue((w > 0).all(), f"min weight {w.min():.3e}")

    def test_band_areas(self):
        w = np.asarray(health.row_area_weights(ROWS))
        B = config.BT_N_BANDS
        areas = [w[b * BAND:(b + 1) * BAND].sum() * 100 for b in range(B)]
        for got, want in zip(areas, (14.6447, 35.3553, 35.3553, 14.6447)):
            self.assertAlmostEqual(got, want, places=3)
        self.assertAlmostEqual(sum(areas), 100.0, places=9)

    def test_unfillable_region_is_866_percent_of_globe(self):
        w = np.asarray(health.row_area_weights(ROWS))
        mid = health.unfillable_row_mask()
        self.assertEqual(int(mid.sum()), 4320)
        self.assertAlmostEqual(float(w[mid].sum()) * 100, 86.6025, places=3)
        self.assertAlmostEqual(float(w[~mid].sum()) * 100, 13.3975, places=3)

    def test_no_row_centre_lands_on_the_boundary(self):
        # The mask is strict (|lat| < 60) and the complement is |lat| > 60. That
        # is unambiguous only because no cell centre sits exactly on +/-60; if
        # BT_N_ROWS ever changes, this test is the thing that notices.
        c = health.row_centres(ROWS)
        self.assertEqual(int(np.sum(np.abs(c) == FILL_LAT)), 0)
        mid = health.unfillable_row_mask()
        self.assertEqual(int(mid.sum() + (~mid).sum()), ROWS)

    def test_per_band_unfillable_rows(self):
        mid = health.unfillable_row_mask()
        w = np.asarray(health.row_area_weights(ROWS))
        got = [(int(mid[b * BAND:(b + 1) * BAND].sum()),
                round(float(w[b * BAND:(b + 1) * BAND][
                    mid[b * BAND:(b + 1) * BAND]].sum()) * 100, 2))
               for b in range(config.BT_N_BANDS)]
        self.assertEqual(got, [(540, 7.95), (1620, 35.36),
                               (1620, 35.36), (540, 7.95)])


# ---------------------------------------------------------------------------
# the criterion
# ---------------------------------------------------------------------------

class TestCriterion(unittest.TestCase):

    def test_clean_file_is_accepted(self):
        h, read = stream_verdict(blank())
        self.assertTrue(h.ok)
        self.assertEqual(h.unfillable_pct, 0.0)
        self.assertEqual(read, config.BT_N_BANDS)

    def test_polar_cap_is_not_counted_as_loss(self):
        """The whole north polar cap empty must still be ACCEPTED.

        Rows 0..1080 are |lat| > 60, i.e. exactly what capfill repaints. The
        old count criterion saw 32 dead blocks here and threw the file away.
        """
        full = blank()
        full[:1080, :] = True
        h, _read = stream_verdict(full)
        self.assertAlmostEqual(h.unfillable_pct, 0.0, places=6)
        self.assertGreater(h.fillable_pct, 6.0)
        self.assertTrue(h.ok, f"极冠空洞不应导致拒绝: {h.reason}")

    def test_old_criterion_would_have_rejected_that_file(self):
        # Guards the premise of the test above: the same mask is well past the
        # dead-block line, so this is a real behaviour change and not a case
        # that was never rejected.
        full = blank()
        full[:1080, :] = True
        h, _ = stream_verdict(full)
        self.assertGreaterEqual(h.dead_blocks, 4,
                                "前提不成立：这个掩码本就没被旧判据拒过")

    def test_equatorial_hole_beyond_limit_is_rejected(self):
        full = mask_with(unfillable_pct=3.0)
        u, f = loss_of(full)
        self.assertAlmostEqual(u, 3.0, delta=0.1)
        h, _read = stream_verdict(full)
        self.assertFalse(h.ok)
        self.assertIn("不可补缺失", h.reason)

    def test_threshold_is_strict_greater_than(self):
        # At exactly the limit the file passes; the comparison is `>`.
        acc = health.BtHealthAccumulator()
        acc.unfillable_pct = LIMIT
        self.assertFalse(acc.exceeded)
        self.assertTrue(acc.finalize().ok)

        acc2 = health.BtHealthAccumulator()
        acc2.unfillable_pct = LIMIT + 1e-9
        self.assertTrue(acc2.exceeded)
        self.assertFalse(acc2.finalize().ok)

    def test_loss_just_under_limit_is_accepted(self):
        full = mask_with(unfillable_pct=LIMIT - 0.1)
        h, _ = stream_verdict(full)
        self.assertLessEqual(h.unfillable_pct, LIMIT)
        self.assertTrue(h.ok, f"{h.unfillable_pct:.3f}% 应被接受: {h.reason}")

    def test_empty_file_is_rejected_in_band_zero(self):
        """An all-fill placeholder must be rejected, and early.

        The unfillable rows of band 0 alone are 7.95% of the globe, so a file
        that is entirely fill crosses the line on the first band — which is
        what keeps the 4 MB placeholder cheap to reject. The reported figure on
        abort is the *partial* sum (7.95%, band 0 only), not the file's total
        86.6%; that is what the pipeline logs at the moment it gives up.
        """
        full = np.ones((ROWS, COLS), dtype=bool)
        h, read = stream_verdict(full)
        self.assertFalse(h.ok)
        self.assertEqual(read, 1)
        self.assertAlmostEqual(h.unfillable_pct, 7.9459, places=3)
        self.assertGreater(h.unfillable_pct, LIMIT)
        # Suppressing the abort gives the whole-file figure.
        self.assertGreater(stream_verdict_all_bands(full).unfillable_pct, 86.0)

    def test_band_zero_unfillable_share_is_the_reason_early_abort_works(self):
        w = np.asarray(health.row_area_weights(ROWS))
        mid = health.unfillable_row_mask()
        share = float(w[:BAND][mid[:BAND]].sum()) * 100
        self.assertAlmostEqual(share, 7.95, places=2)
        self.assertGreater(share, LIMIT)

    def test_dead_blocks_no_longer_decide(self):
        """Many dead blocks plus a small unfillable patch is still ACCEPTED.

        This is the shape of the false rejects the change was made for: most of
        the damage sits inside the SSEC zone (repaintable however many blocks
        the old counter flagged) and the rest is well under the limit.

        Note the polar cap is rows 0..1080, NOT the whole north *band* — the
        band runs down to lat 45, and its rows below 60N are unfillable, so
        using the band here would silently be testing an equatorial hole.
        """
        full = blank()
        full[:1080, :] = True                 # whole north polar cap
        full[3000:3020, 0:2000] = True        # ~0.07% unfillable, tiny
        h, _ = stream_verdict(full)
        self.assertGreaterEqual(h.dead_blocks, 4)
        self.assertLessEqual(h.unfillable_pct, LIMIT)
        self.assertTrue(h.ok, f"死块 {h.dead_blocks} 但不可补仅 "
                              f"{h.unfillable_pct:.3f}%，应接受: {h.reason}")

    def test_north_band_below_60n_is_unfillable(self):
        # The inverse: rows 1080..1620 of the north band are lat 60..45, i.e.
        # outside the SSEC zone, so emptying them *is* a real loss. Pinned as a
        # test because confusing the band with the polar cap is exactly the
        # mistake the case above invites.
        full = blank()
        full[1080:1620, :] = True
        h, _ = stream_verdict(full)
        self.assertAlmostEqual(h.fillable_pct, 0.0, places=6)
        self.assertGreater(h.unfillable_pct, LIMIT)
        self.assertFalse(h.ok)


# ---------------------------------------------------------------------------
# streaming vs whole-array
# ---------------------------------------------------------------------------

class TestStreamingEquivalence(unittest.TestCase):

    def _compare(self, full: np.ndarray) -> None:
        # All bands, abort suppressed: an early abort deliberately reports a
        # partial sum and would not be comparable with the whole-array result.
        streamed = stream_verdict_all_bands(full)
        whole = health.evaluate_bt(full)
        self.assertAlmostEqual(streamed.unfillable_pct, whole.unfillable_pct,
                               places=9)
        self.assertAlmostEqual(streamed.fillable_pct, whole.fillable_pct,
                               places=9)
        self.assertAlmostEqual(streamed.global_invalid, whole.global_invalid,
                               places=9)
        self.assertEqual(streamed.dead_blocks, whole.dead_blocks)
        self.assertAlmostEqual(streamed.worst_block, whole.worst_block,
                               places=9)
        self.assertEqual(streamed.ok, whole.ok)

    def test_clean(self):
        self._compare(blank())

    def test_polar_only(self):
        f = blank()
        f[:1080, :] = True
        self._compare(f)

    def test_mixed_damage(self):
        f = blank()
        f[100:900, 200:900] = True          # polar, partial
        f[3000:3600, 5000:9000] = True      # equatorial block hole
        f[5000:5400, :] = True              # southern, straddling 60S
        self._compare(f)

    def test_everything(self):
        self._compare(np.ones((ROWS, COLS), dtype=bool))

    def test_accumulator_rejects_out_of_order_bands(self):
        acc = health.BtHealthAccumulator()
        with self.assertRaises(ValueError):
            acc.add_band(blank()[:BAND], BAND)


# ---------------------------------------------------------------------------
# the two real cases the change was made for
# ---------------------------------------------------------------------------

class TestRealCases(unittest.TestCase):
    """Measured on 2026-09-18 with the gcc-probe sweep (45 files).

    The (fillable, unfillable) pairs are the probe's independent per-row
    measurements; the file at 1.037% is the closest reject in the whole set and
    is the reason the limit sits at 1.05 rather than 1.0.
    """

    CASES = [
        # (label, fillable %, unfillable %, expected ok)
        ("09-17 06:00 纯极区损害 13 死块", 4.623, 0.055, True),
        ("09-17 13:00 极区+中纬 3 死块", 2.854, 0.883, True),
        ("09-18 03:00 严重损坏", 8.029, 15.477, False),
        ("09-16 20:00 极区+不可补小洞", 2.400, 1.470, False),
        ("09-17 16:00 弥散赤道损害 0 死块", 2.601, 3.087, False),
        ("最近的拒绝样本 1.037%", 2.9, 1.037, True),
        ("刚过线 1.06%", 2.9, 1.060, False),
    ]

    def test_verdicts(self):
        for label, f_pct, u_pct, expect_ok in self.CASES:
            with self.subTest(label=label):
                full = mask_with(unfillable_pct=u_pct, fillable_pct=f_pct)
                got_u, got_f = loss_of(full)
                h, _ = stream_verdict(full)
                # Row granularity means the target cannot be hit exactly.
                self.assertAlmostEqual(got_u, u_pct, delta=0.1,
                                       msg=f"{label}: 构造出的不可补缺失偏离过多")
                self.assertAlmostEqual(got_f, f_pct, delta=0.1)
                self.assertEqual(
                    h.ok, expect_ok,
                    f"{label}: 得到 ok={h.ok} 不可补={h.unfillable_pct:.3f}% "
                    f"可补={h.fillable_pct:.3f}% 死块={h.dead_blocks}")

    def test_the_1600z_wrong_accept_is_now_caught(self):
        """09-17 16:00Z was published by the old criterion despite 3.087%.

        It had ZERO dead blocks — the damage was spread across blocks rather
        than concentrated — which is why a count-based rule could not see it.
        """
        full = mask_with(unfillable_pct=3.087, fillable_pct=2.601)
        h, _ = stream_verdict(full)
        self.assertFalse(h.ok)
        self.assertGreater(h.unfillable_pct, LIMIT)

    def test_the_0600z_false_reject_is_now_accepted(self):
        """09-17 06:00Z was rejected by the old criterion with 13 dead blocks.

        Its only damage was inside the SSEC zone (0.055% unfillable), so it was
        cleaner than files the same rule happily published.
        """
        full = mask_with(unfillable_pct=0.055, fillable_pct=4.623)
        h, _ = stream_verdict(full)
        self.assertTrue(h.ok, f"{h.reason}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
