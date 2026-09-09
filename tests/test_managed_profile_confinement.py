"""A `profile` query parameter must confine a managed-file download.

The download endpoint accepts `?profile=<name>` and the client sends it. Before
this, the parameter was decorative: the resolved target was not checked against
the named profile, so a symlink at `profiles/<a>/report.pdf` pointing into
`profiles/<b>/` was served — a cross-profile read dressed as an ordinary
download. And an invalid or reserved profile silently disabled the check.

These tests exercise the confinement directly, against real temp profile
directories including a cross-profile symlink.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402
from hermes_cli import web_server_files as web_server  # noqa: E402


class RequestedProfileConfinementTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profiles_root = Path(self._tmp.name) / "profiles"
        self.a = self.profiles_root / "abu-saleh"
        self.b = self.profiles_root / "majed"
        for d in (self.a, self.b):
            d.mkdir(parents=True, exist_ok=True)
        (self.a / "report.pdf").write_text("A's report\n", encoding="utf-8")
        (self.b / "private.pdf").write_text("B's private file\n", encoding="utf-8")
        self._patches = [
            mock.patch("hermes_cli.profiles._get_profiles_root", return_value=self.profiles_root),
            mock.patch("hermes_cli.profiles.get_profile_dir",
                       side_effect=lambda name: (self.profiles_root.parent if name == "default"
                                                 else self.profiles_root / name)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _enforce(self, profile, target):
        web_server._enforce_requested_profile(profile, web_server._canonical_path(Path(target)))

    def test_a_file_in_the_named_profile_is_allowed(self):
        self._enforce("abu-saleh", self.a / "report.pdf")  # no raise

    def test_a_file_in_another_profile_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            self._enforce("abu-saleh", self.b / "private.pdf")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_a_cross_profile_symlink_is_refused(self):
        link = self.a / "leak.pdf"
        try:
            os.symlink(self.b / "private.pdf", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        # Canonicalisation resolves the link into profile B, so confinement to A
        # must reject it.
        with self.assertRaises(HTTPException) as ctx:
            self._enforce("abu-saleh", link)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_an_invalid_profile_is_rejected_not_skipped(self):
        # Rejected by Hermes' own validator: traversal, separators, spaces,
        # over-length, reserved names, and (after normalize) their mixed-case
        # spellings. An empty value names no profile and enforces nothing.
        for bad in ["..", ".", "a/b", "../majed", " ", "a b", "x" * 80,
                    "root", "hermes", "test", "Root", "HERMES", "with.dot"]:
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as ctx:
                    self._enforce(bad, self.a / "report.pdf")
                self.assertEqual(ctx.exception.status_code, 400)
        # Empty string: falsy, so nothing to enforce.
        self._enforce("", self.a / "report.pdf")

    def test_a_mixed_case_named_profile_normalizes_then_confines(self):
        # "Abu-Saleh" normalizes to "abu-saleh" and confines to A's directory.
        self._enforce("Abu-Saleh", self.a / "report.pdf")  # no raise
        with self.assertRaises(HTTPException) as ctx:
            self._enforce("Abu-Saleh", self.b / "private.pdf")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_default_cannot_reach_a_named_profile(self):
        # `profile=default` is the home, but must not be a way into a named
        # profile's subtree.
        with self.assertRaises(HTTPException) as ctx:
            self._enforce("default", self.b / "private.pdf")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_default_allows_a_file_outside_the_profiles_tree(self):
        loose = self.profiles_root.parent / "notes.md"
        loose.write_text("x\n", encoding="utf-8")
        self._enforce("default", loose)  # no raise

    def test_no_profile_named_enforces_nothing(self):
        self._enforce(None, self.b / "private.pdf")  # no raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
