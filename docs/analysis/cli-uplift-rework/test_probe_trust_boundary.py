#!/usr/bin/env python3
"""Validation fixtures for the historical probe's trust boundary (issue #5608).

Two questions, both of which must hold:

  * the intended historical-analysis use still works -- the five counterexamples
    reproduce with byte-identical output; and
  * unsafe inputs are refused -- tampered content, abbreviated revisions and
    unpinned paths raise instead of executing.

Written against the standard library only (unittest, no pytest) because this
helper lives under docs/analysis/ and is run directly from a checkout rather
than through a module's test suite:

    python3 docs/analysis/cli-uplift-rework/test_probe_trust_boundary.py

Requires the pinned Git objects to be present; tests that need them skip with a
clear message on a shallow clone rather than failing misleadingly.
"""

import hashlib
import importlib
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported after the sys.path insert above so this file runs standalone from any
# working directory; importlib keeps that ordering explicit rather than relying
# on a linter directive.
probe = importlib.import_module("probe_historical_failures")

COMMON = ("3d22b4a039f782650965714aeecc2370b6919b99",
          "modules/gateway/cli/adp_common.py")


def objects_present():
    """True when the pinned objects are in this checkout (not a shallow clone)."""
    for revision, path in probe.PINNED_SOURCES:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell, read-only
            ["git", "cat-file", "-e", f"{revision}:{path}"],
            capture_output=True, check=False,  # absence is the signal, not an error
        )
        if result.returncode != 0:
            return False
    return True


HAVE_OBJECTS = objects_present()
needs_objects = unittest.skipUnless(
    HAVE_OBJECTS, "pinned Git objects absent (shallow clone?)")


class PinFormat(unittest.TestCase):
    """Refusals that do not depend on the object store being populated."""

    def test_abbreviated_revision_refused(self):
        """The original weakness: a short prefix is not a content identity."""
        with self.assertRaises(probe.UntrustedHistoricalSource) as caught:
            probe.pinned_source("3d22b4a039", COMMON[1])
        self.assertIn("40-hex", str(caught.exception))

    def test_unpinned_path_refused(self):
        """A path with no recorded digest cannot be executed."""
        with self.assertRaises(probe.UntrustedHistoricalSource) as caught:
            probe.pinned_source(COMMON[0], "modules/gateway/cli/does-not-exist.py")
        self.assertIn("PINNED_SOURCES", str(caught.exception))

    def test_right_path_wrong_pinned_revision_refused(self):
        """Pins are (revision, path) pairs; a real path under another real
        revision is still unpinned and must not slip through."""
        other_revision = "18ff18db70cc53e665f5fc100de85a95ab268cd0"
        self.assertIn((other_revision, "modules/gateway/cli/adp-github-admin.py"),
                      probe.PINNED_SOURCES)
        with self.assertRaises(probe.UntrustedHistoricalSource):
            probe.pinned_source(other_revision, COMMON[1])

    def test_every_pin_is_full_length_lowercase_hex(self):
        """Guards against a later edit shortening a pin for readability."""
        for revision, path in probe.PINNED_SOURCES:
            self.assertRegex(revision, r"^[0-9a-f]{40}$", f"{path} pin")
            self.assertRegex(probe.PINNED_SOURCES[(revision, path)],
                             r"^[0-9a-f]{64}$", f"{path} digest")


@needs_objects
class DigestVerification(unittest.TestCase):

    def test_pinned_source_returns_verified_bytes(self):
        content = probe.pinned_source(*COMMON)
        self.assertIsInstance(content, bytes)
        self.assertEqual(hashlib.sha256(content).hexdigest(),
                         probe.PINNED_SOURCES[COMMON])

    def test_tampered_content_is_refused(self):
        """The test that matters: if the bytes Git yields are not the pinned
        bytes, execution must stop.

        The recorded digest is swapped for the digest of attacker-chosen source,
        which models the real threat -- a checkout whose object store resolves
        the pin to different content -- without needing to forge an object ID.
        The failure must be the digest check, so the assertion is on the message
        and on the executing helper refusing too.
        """
        hostile = b'def save_session(tokens):\n    raise SystemExit("owned")\n'
        original = probe.PINNED_SOURCES[COMMON]
        probe.PINNED_SOURCES[COMMON] = hashlib.sha256(hostile).hexdigest()
        try:
            with self.assertRaises(probe.UntrustedHistoricalSource) as caught:
                probe.pinned_source(*COMMON)
            message = str(caught.exception)
            self.assertIn("refusing to execute", message)
            self.assertIn("expected", message)

            # and the compile/exec entry point refuses for the same reason
            with self.assertRaises(probe.UntrustedHistoricalSource):
                probe.historical_functions(*COMMON, ["save_session"])
        finally:
            probe.PINNED_SOURCES[COMMON] = original

        # digest restored, so the real source verifies again
        self.assertEqual(
            hashlib.sha256(probe.pinned_source(*COMMON)).hexdigest(), original)

    def test_missing_object_fails_closed(self):
        """A revision this checkout does not have must raise, not return empty.

        Uses a well-formed 40-hex ID that no object matches, pinned to an
        arbitrary digest so the lookup gets past the PINNED_SOURCES check and
        reaches Git.
        """
        absent = ("0" * 39 + "1", COMMON[1])
        probe.PINNED_SOURCES[absent] = "f" * 64
        try:
            with self.assertRaises(subprocess.CalledProcessError):
                probe.pinned_source(*absent)
        finally:
            del probe.PINNED_SOURCES[absent]


@needs_objects
class NamespaceContainment(unittest.TestCase):

    def test_module_globals_not_inherited(self):
        """Compiled definitions see only what the call site passed.

        subprocess is imported by the probe but is not passed to any call site,
        so it must not be reachable from the executed namespace.
        """
        loaded = probe.historical_functions(*COMMON, ["CliError"])
        self.assertFalse(hasattr(loaded, "subprocess"))
        self.assertFalse(hasattr(loaded, "PINNED_SOURCES"))
        self.assertTrue(hasattr(loaded, "CliError"))

    def test_requested_names_must_all_exist(self):
        with self.assertRaises(AssertionError):
            probe.historical_functions(*COMMON, ["no_such_function"])


@needs_objects
class IntendedUseStillWorks(unittest.TestCase):
    """The behaviour-preservation half of the acceptance criteria."""

    EXPECTED: ClassVar[dict] = {
        "historical_counterexamples_reproduced": 5,
        "observed": {
            "existing_identity_pool_after_login": "",
            "contradictory_owner_and_org": ["user", None],
            "infrastructure_503_classified_as_missing_app": True,
            "dry_run_reuse_state_writes": [{}],
            "codex_marker_selects_unrelated_claude_usage":
                "unrelated-claude-request",
        },
    }

    def test_five_counterexamples_still_reproduce(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            probe.main()
        self.assertEqual(json.loads(captured.getvalue()), self.EXPECTED)

    def test_verify_pins_reports_all_ok(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            probe.verify_pins()
        lines = captured.getvalue().strip().splitlines()
        self.assertEqual(len(lines), len(probe.PINNED_SOURCES))
        for line in lines:
            self.assertTrue(line.startswith("ok "), line)


if __name__ == "__main__":
    if not HAVE_OBJECTS:
        print("NOTE: pinned Git objects absent; object-dependent tests skip.",
              file=sys.stderr)
    unittest.main(verbosity=2)
