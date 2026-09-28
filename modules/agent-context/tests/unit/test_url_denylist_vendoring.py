"""The vendored SSRF denylist must stay byte-identical to its source (#5658).

Why this test exists
--------------------
``images/ingestion/url_denylist.py`` is a copy of
``modules/domain-apps/cyber/agent/skills/url-analysis/denylist.py``. The ingestion
image is a separate Docker build context and cannot import across module
boundaries, so a copy is unavoidable.

What is avoidable is an *unguarded* copy. There is already a second, unguarded
copy of this file in ``.claude/skills/url-analysis/``, and it has drifted to
roughly 40% of the original: it kept the function names and lost alternate-IPv4
canonicalisation (so ``http://2130706433/`` is no longer recognised as
``127.0.0.1``) and ``check_connect_address`` entirely (so nothing re-validates the
address at connect time). It still imports, still returns ``DenylistResult``
objects, and still looks like a working SSRF guard at every call site. That is the
failure mode this test exists to prevent: security lost while the API survives.

So the copy is asserted byte-for-byte against the source of truth. If the
canonical module improves, this test fails until the copy is re-synced — which is
the point. If a fix is needed in the copy, it belongs upstream first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ingestion scripts are top-level modules in the image, imported via sys.path —
# the same convention as test_sqs_scope.py and test_s3_prefix_routing.py.
_INGESTION_DIR = str(Path(__file__).resolve().parents[2] / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

import url_denylist  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CANONICAL = (
    _REPO_ROOT / "modules/domain-apps/cyber/agent/skills/url-analysis/denylist.py"
)
_VENDORED = (
    _REPO_ROOT / "modules/agent-context/images/ingestion/url_denylist.py"
)

_BANNER_END = "# --- END VENDOR BANNER ---\n"


def _vendored_body() -> str:
    """The vendored file with its provenance banner removed."""
    text = _VENDORED.read_text()
    _, separator, body = text.partition(_BANNER_END)
    assert separator, (
        f"{_VENDORED} is missing the {_BANNER_END.strip()!r} marker. The banner "
        "records where this file comes from and that it must not be edited "
        "directly; without it the next reader has no way to know either."
    )
    return body


class TestVendoredCopyMatchesSource:
    def test_both_files_exist(self):
        """Neither side of the vendoring relationship has been moved or deleted."""
        assert _CANONICAL.is_file(), f"canonical source missing: {_CANONICAL}"
        assert _VENDORED.is_file(), f"vendored copy missing: {_VENDORED}"

    def test_vendored_body_is_byte_identical(self):
        """The copy matches the source exactly, modulo the banner."""
        if not _CANONICAL.is_file():
            pytest.skip("canonical source not present in this checkout")

        canonical = _CANONICAL.read_text()
        vendored = _vendored_body()

        assert vendored == canonical, (
            "images/ingestion/url_denylist.py has drifted from "
            "modules/domain-apps/cyber/agent/skills/url-analysis/denylist.py.\n"
            "Re-sync it (banner first, then the canonical file verbatim). Do not "
            "edit the vendored copy to fix a bug — fix it upstream and re-sync, or "
            "the two diverge silently the way .claude/skills/url-analysis/ did."
        )

    def test_banner_names_the_source_of_truth(self):
        """The banner points at the canonical path, not just 'somewhere else'."""
        banner = _VENDORED.read_text().partition(_BANNER_END)[0]
        assert "url-analysis/denylist.py" in banner
        assert "DO NOT EDIT" in banner.upper()


class TestVendoredCopyHasTheSecurityCriticalParts:
    """Independent of drift: the capabilities the weak fork lost must be present.

    The byte-identity test above subsumes these while the canonical file is
    healthy. They are asserted separately because they name the *specific*
    regressions observed in the drifted fork — if someone ever re-points the
    canonical path at a weaker implementation, byte-identity would still pass and
    these would not.
    """

    def test_alternate_ipv4_notations_are_canonicalised(self):
        """Integer, octal and hex IPv4 spellings of a blocked address are blocked."""
        denylist = url_denylist

        # All four of these are 127.0.0.1.
        for spelling in ("127.0.0.1", "2130706433", "0177.0.0.1", "0x7f.0.0.1"):
            result = denylist.check_url(f"http://{spelling}/")
            assert not result.allowed, (
                f"{spelling!r} is 127.0.0.1 written differently and must be blocked"
            )

    def test_connect_address_revalidation_exists(self):
        """check_connect_address is present and refuses an unapproved address.

        This is the TOCTOU/DNS-rebinding guard. The drifted fork dropped it, and a
        pinned transport cannot be built without it.
        """
        denylist = url_denylist

        approved = ["93.184.216.34"]
        # An address that is public and would pass classification on its own, but
        # was not the one approved, is still refused.
        verdict = denylist.check_connect_address("93.184.216.35", approved)
        assert not verdict.allowed
        assert verdict.reason_code == denylist.REASON_ADDRESS_NOT_APPROVED

        # And the approved one passes.
        assert denylist.check_connect_address("93.184.216.34", approved).allowed

    def test_imds_is_blocked_by_name_not_only_by_cidr(self):
        """The IMDS address is in an always-blocked list of its own.

        Relying only on the link-local CIDR means a config that narrows the CIDR
        list silently unblocks credential theft.
        """
        denylist = url_denylist

        assert "169.254.169.254" in denylist.ALWAYS_BLOCKED_IPS
        empty_cidrs = denylist.DenylistConfig(denied_cidrs=[])
        assert not denylist.check_url(
            "http://169.254.169.254/latest/meta-data/", empty_cidrs
        ).allowed
