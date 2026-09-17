"""
tests/test_version_marker.py — build identity + first-run (bootstrap) logic.

The publish marker decides whether a run is allowed to ignore the
backward-search floor. Getting it wrong in one direction freezes the pipeline
on data the new code exists to replace; getting it wrong in the other
direction lets it republish stale data on every single run. Both failure modes
are silent in production, so the rules are pinned here.

Run:  python -m pytest tests/test_version_marker.py -q
      python tests/test_version_marker.py          (no pytest needed)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polar_plus import config                                    # noqa: E402
from polar_plus import health                                    # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _EnvGuard:
    """Set/clear env vars for the duration of one test."""

    def __init__(self, **values):
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.pipeline_version.cache_clear()
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.pipeline_version.cache_clear()
        return False


def _write_root_json(directory: Path, payload: dict) -> Path:
    latest = directory / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    path = latest / "root.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# Vars that would otherwise let a unit test reach the real network or pick up
# the developer's own environment. Anything the *caller* has already set wins,
# so a test can do `with _EnvGuard(POLAR_VERSION=...)` outside `_LocalRoot`.
_ISOLATED_ENV = dict(POLAR_FLOOR_TS=None, POLAR_VERSION=None,
                     POLAR_VERSION_FILE=None, GITHUB_SHA=None,
                     POLAR_PUBLIC_BASE_URL=None, PAGES_ROOT_URL=None,
                     GH_PAGES_BASE=None, GITHUB_REPOSITORY=None)


class _LocalRoot:
    """Point OUTPUT_DIR at a temp tree for one test."""

    def __init__(self, payload: dict | None):
        self.payload = payload
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        if self.payload is not None:
            _write_root_json(root, self.payload)
        self.saved = os.environ.get("OUTPUT_DIR")
        os.environ["OUTPUT_DIR"] = str(root)
        # config.OUTPUT_DIR is read at import time; health reads it through
        # that module attribute, so patch the attribute itself.
        self.health_saved = health.OUTPUT_DIR
        health.OUTPUT_DIR = root
        overrides = {k: v for k, v in _ISOLATED_ENV.items()
                     if k not in os.environ}
        self.env = _EnvGuard(**overrides)
        self.env.__enter__()
        # pipeline_version() is cached; clear it *after* the env is in place so
        # a value cached by an earlier test cannot leak into this one.
        config.pipeline_version.cache_clear()
        return self

    def __exit__(self, *exc):
        self.env.__exit__(*exc)
        health.OUTPUT_DIR = self.health_saved
        if self.saved is None:
            os.environ.pop("OUTPUT_DIR", None)
        else:
            os.environ["OUTPUT_DIR"] = self.saved
        self.tmp.cleanup()
        return False


# ---------------------------------------------------------------------------
# version normalisation
# ---------------------------------------------------------------------------

class TestNormalise(unittest.TestCase):

    def test_full_sha_is_shortened_to_twelve(self):
        full = "de1b6a809493be28c2f9c50bdee2990ee17763cc"
        self.assertEqual(config._normalise_version(full), "de1b6a809493")

    def test_short_sha_is_kept_verbatim(self):
        self.assertEqual(config._normalise_version("de1b6a8"), "de1b6a8")

    def test_sha_is_case_folded(self):
        self.assertEqual(config._normalise_version("DE1B6A8"), "de1b6a8")

    def test_surrounding_whitespace_and_newline_are_stripped(self):
        # version.txt is written with a trailing newline.
        self.assertEqual(config._normalise_version("de1b6a8\n"), "de1b6a8")

    def test_empty_and_none_are_unusable(self):
        for value in (None, "", "   ", "\n"):
            self.assertIsNone(config._normalise_version(value))

    def test_free_string_passes_through(self):
        # A legacy hand-typed marker still has to be readable back, if only so
        # the log can say what the live version was.
        self.assertEqual(config._normalise_version("2.0.0"), "2.0.0")
        self.assertEqual(config._normalise_version("legacy:1"), "legacy:1")

    def test_is_commit_version(self):
        self.assertTrue(config.is_commit_version("de1b6a8"))
        self.assertTrue(config.is_commit_version("de1b6a809493"))
        self.assertFalse(config.is_commit_version("2.0.0"))
        self.assertFalse(config.is_commit_version("v6"))
        self.assertFalse(config.is_commit_version("legacy:1"))
        self.assertFalse(config.is_commit_version(None))
        # 6 hex chars is shorter than git's own minimum abbreviation.
        self.assertFalse(config.is_commit_version("de1b6a"))
        # a hex-looking string that is too long is not a sha
        self.assertFalse(config.is_commit_version("a" * 41))


# ---------------------------------------------------------------------------
# version resolution
# ---------------------------------------------------------------------------

class TestResolution(unittest.TestCase):

    def test_env_override_wins_over_everything(self):
        with _EnvGuard(POLAR_VERSION="cafebabe1234",
                       GITHUB_SHA="f" * 40):
            self.assertEqual(config.pipeline_version(),
                             ("cafebabe1234", "POLAR_VERSION"))

    def test_github_sha_is_used_when_nothing_else_identifies_the_build(self):
        with _EnvGuard(POLAR_VERSION=None, POLAR_VERSION_FILE=None,
                       GITHUB_SHA="de1b6a809493be28c2f9c50bdee2990ee17763cc"):
            self.assertEqual(config.pipeline_version(),
                             ("de1b6a809493", "GITHUB_SHA"))

    def test_version_file_outranks_github_sha(self):
        # The image was built from a specific commit; the env of the run that
        # invokes it must not be able to misrepresent that.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.txt"
            path.write_text("aaaaaaaaaaaa\n", encoding="utf-8")
            with _EnvGuard(POLAR_VERSION=None, POLAR_VERSION_FILE=str(path),
                           GITHUB_SHA="b" * 40):
                self.assertEqual(config.pipeline_version(),
                                 ("aaaaaaaaaaaa", str(path)))

    def test_missing_version_file_falls_through(self):
        with _EnvGuard(POLAR_VERSION=None,
                       POLAR_VERSION_FILE="/nonexistent/version.txt",
                       GITHUB_SHA="c" * 40):
            self.assertEqual(config.pipeline_version(), ("cccccccccccc",
                                                         "GITHUB_SHA"))

    def test_unidentified_build_reports_dev(self):
        # A source checkout always has .git, so simulate the container case by
        # pointing the repo root somewhere without one.
        with _EnvGuard(POLAR_VERSION=None, POLAR_VERSION_FILE="/nonexistent",
                       GITHUB_SHA=None):
            saved = config._version_from_git
            config._version_from_git = lambda: (None, False)
            try:
                self.assertEqual(config.pipeline_version(),
                                 (config.DEV_VERSION, "未识别的构建"))
            finally:
                config._version_from_git = saved

    def test_resolution_is_cached_within_a_run(self):
        with _EnvGuard(POLAR_VERSION="cafebabe1234"):
            first = config.pipeline_version()
            os.environ["POLAR_VERSION"] = "deadbeef0000"
            self.assertEqual(config.pipeline_version(), first)


# ---------------------------------------------------------------------------
# bootstrap decision
# ---------------------------------------------------------------------------

class TestBootstrap(unittest.TestCase):

    def _floor(self, live: dict | None):
        with _LocalRoot(live):
            return health.resolve_floor_ts()

    def test_same_version_applies_the_floor(self):
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            floor, source, bootstrap = self._floor(
                {"timestamp": "20260916_160000", "version": "de1b6a8"})
        self.assertFalse(bootstrap)
        self.assertIsNotNone(floor)
        self.assertEqual(floor.strftime("%Y%m%d_%H%M%S"), "20260916_160000")

    def test_different_version_bootstraps(self):
        with _EnvGuard(POLAR_VERSION="aaaaaaa"):
            floor, _source, bootstrap = self._floor(
                {"timestamp": "20260916_160000", "version": "de1b6a8"})
        self.assertTrue(bootstrap)
        self.assertIsNone(floor)

    def test_full_sha_in_root_json_matches_short_local_sha(self):
        # root.json may carry either length; both must compare equal.
        with _EnvGuard(POLAR_VERSION="de1b6a809493be28c2f9c50bdee2990ee17763cc"):
            floor, _source, bootstrap = self._floor(
                {"timestamp": "20260916_160000", "version": "de1b6a809493"})
        self.assertFalse(bootstrap)
        self.assertIsNotNone(floor)

    def test_legacy_gate_marker_bootstraps(self):
        # Exactly the payload the currently-deployed, unversioned pipeline
        # writes. This is the transition case that must fire once.
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            floor, _source, bootstrap = self._floor(
                {"baseUrl": "http://x/tiles/", "timestamp": "20260916_160000",
                 "gate": 1})
        self.assertTrue(bootstrap)
        self.assertIsNone(floor)

    def test_missing_version_bootstraps(self):
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            floor, _source, bootstrap = self._floor(
                {"timestamp": "20260916_160000"})
        self.assertTrue(bootstrap)
        self.assertIsNone(floor)

    def test_hand_typed_version_bootstraps_once(self):
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            floor, _source, bootstrap = self._floor(
                {"timestamp": "20260916_160000", "version": "2.0.0"})
        self.assertTrue(bootstrap)
        self.assertIsNone(floor)

    def test_no_live_root_json_bootstraps(self):
        # Nothing published yet: there is no floor to apply either way, and
        # bootstrap must still be reported so the caller's logging is honest.
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            floor, _source, bootstrap = self._floor(None)
        self.assertIsNone(floor)

    def test_corrupt_timestamp_does_not_bootstrap(self):
        # A broken timestamp must not, by itself, be read as a version change.
        # (Note "20260915_1435" is NOT corrupt: the 4-digit H%M form is legal.)
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            floor, source, bootstrap = self._floor(
                {"timestamp": "not-a-timestamp", "version": "de1b6a8"})
        self.assertIsNone(floor)          # unparsable → no bound
        self.assertFalse(bootstrap)       # but *not* a version change

    def test_dev_build_bootstraps_against_a_real_marker(self):
        # The container case with no stamping: every run must bootstrap.
        with _EnvGuard(POLAR_VERSION=None, POLAR_VERSION_FILE="/nonexistent",
                       GITHUB_SHA=None):
            saved = config._version_from_git
            config._version_from_git = lambda: (None, False)
            config.pipeline_version.cache_clear()
            try:
                floor, _source, bootstrap = self._floor(
                    {"timestamp": "20260916_160000", "version": "de1b6a8"})
            finally:
                config._version_from_git = saved
        self.assertTrue(bootstrap)
        self.assertIsNone(floor)

    def test_forced_floor_is_never_treated_as_bootstrap(self):
        with _EnvGuard(POLAR_VERSION="de1b6a8", POLAR_FLOOR_TS="20260916_120000"):
            floor, _source, bootstrap = self._floor(
                {"timestamp": "20260916_160000", "version": "aaa"})
        self.assertFalse(bootstrap)
        self.assertEqual(floor.strftime("%Y%m%d_%H%M%S"), "20260916_120000")

    def test_describe_floor_mentions_the_running_version(self):
        with _EnvGuard(POLAR_VERSION="de1b6a8"):
            text = health.describe_floor(None, "local:x", bootstrap=True)
        self.assertIn("de1b6a8", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
