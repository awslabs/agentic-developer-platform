"""Tests for TokenContext's authenticated/attributed org split.

Issue #4132: ``org_id`` is authenticated-only and is the sole field any
authorization path may read. ``attributed_org_id`` is caller-influenced (via the
X-Agent-OrgId header for internal-scope agents) and must never gate access.

These tests pin the field's default behaviour, which is what keeps the rename
transparent for every non-internal caller.
"""

from datetime import UTC, datetime, timedelta

from src.shared.schemas.auth import TokenContext


def _context(**overrides) -> TokenContext:
    kwargs = {
        "user_id": "user-1",
        "org_id": "org-authenticated",
        "team_id": "team-1",
        "department_id": "dept-1",
        "account_type": "human",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    kwargs.update(overrides)
    return TokenContext(**kwargs)


class TestAttributedOrgIdDefault:
    """attributed_org_id defaults to org_id so existing callers are unaffected."""

    def test_defaults_to_org_id_when_omitted(self):
        context = _context()

        assert context.attributed_org_id == "org-authenticated"

    def test_defaults_to_org_id_when_explicitly_empty(self):
        context = _context(attributed_org_id="")

        assert context.attributed_org_id == "org-authenticated"

    def test_explicit_attribution_is_preserved(self):
        context = _context(attributed_org_id="org-attributed")

        assert context.attributed_org_id == "org-attributed"
        # The authenticated field is untouched by attribution.
        assert context.org_id == "org-authenticated"

    def test_empty_org_id_yields_empty_attribution(self):
        """A caller with no org assignment (#600) attributes to nothing, not to a default."""
        context = _context(org_id="")

        assert context.attributed_org_id == ""
