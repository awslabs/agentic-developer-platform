"""Self-writable Cognito attributes and authorization inputs must be disjoint — #5666 (A11).

The application-side hardening (``src/admin/onboarding/trusted_identity.py``) is only
half the control. The other half lives in Terraform: which attributes an app client
may write about itself. If those two drift — a new attribute added to
``write_attributes``, or a new attribute consulted by identity resolution — the
fallback this issue closed reopens silently and for every tenant.

So this asserts the boundary against the Terraform that actually defines it, rather
than against a Python copy of it. A future edit to either side fails the build.

Reads the ``.tf`` files as text (the established pattern in this directory, e.g.
``test_gateway_log_retention.py``) because Terraform is not importable and no HCL
parser is a dependency of this module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.admin.onboarding.trusted_identity import (
    SELF_WRITABLE_ATTRIBUTES,
    TRUSTED_LOGIN_ATTRIBUTE,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_COGNITO_MAIN = _REPO_ROOT / "modules" / "gateway" / "infra" / "modules" / "cognito" / "main.tf"

# Human-facing app clients. The SPA client is the one a browser session holds, so
# it is the client that matters for self-assertion; the CLI client shares the same
# attribute policy and is included so the two cannot diverge unnoticed.
_HUMAN_CLIENTS = ("main", "cli")

# Every attribute that reaches an identity, membership or role decision. Sourced
# from the application: the trusted GitHub login, plus the claims the
# pre-token-generation Lambda copies into the token and access_control resolves
# authority from.
AUTHORIZATION_INPUT_ATTRIBUTES = frozenset(
    {
        TRUSTED_LOGIN_ATTRIBUTE,
        "custom:role",
        "custom:org_id",
        "custom:team_id",
        "custom:department_id",
        "custom:account_type",
    }
)


def _tf_text() -> str:
    assert _COGNITO_MAIN.is_file(), (
        f"The gateway Cognito module is the only user pool definition in this repo and must exist at {_COGNITO_MAIN}. "
        "Note the path `infra/modules/cognito/` named in older records is relative to modules/gateway/, "
        "not the repo root — no such tree exists at the root."
    )
    return _COGNITO_MAIN.read_text(encoding="utf-8")


def _client_block(tf_text: str, client_name: str) -> str:
    """Return the body of ``resource "aws_cognito_user_pool_client" "<name>"``."""
    match = re.search(
        r'resource\s+"aws_cognito_user_pool_client"\s+"' + re.escape(client_name) + r'"\s*\{',
        tf_text,
    )
    assert match, f"app client {client_name!r} not found in {_COGNITO_MAIN}"
    start = match.end() - 1
    depth = 0
    for index in range(start, len(tf_text)):
        if tf_text[index] == "{":
            depth += 1
        elif tf_text[index] == "}":
            depth -= 1
            if depth == 0:
                return tf_text[start : index + 1]
    raise AssertionError(f"app client {client_name!r} block is unterminated")


def _attribute_list(client_body: str, argument: str) -> set[str]:
    """Parse ``write_attributes = [...]`` / ``read_attributes = [...]`` into a set."""
    match = re.search(re.escape(argument) + r"\s*=\s*\[(.*?)\]", client_body, re.DOTALL)
    assert match, f"{argument} not declared on this app client"
    return {value for value in re.findall(r'"([^"]+)"', match.group(1))}


@pytest.fixture(scope="module")
def cognito_tf() -> str:
    return _tf_text()


class TestTrustBoundaryIsDisjoint:
    """The load-bearing assertion of this file."""

    @pytest.mark.parametrize("client", _HUMAN_CLIENTS)
    def test_no_self_writable_attribute_feeds_an_authorization_decision(self, cognito_tf: str, client: str):
        writable = _attribute_list(_client_block(cognito_tf, client), "write_attributes")
        overlap = writable & AUTHORIZATION_INPUT_ATTRIBUTES
        assert not overlap, (
            f"App client {client!r} may self-write {sorted(overlap)}, and those attributes reach an identity "
            "or role decision. A user could then assert their own authority. Either remove the attribute from "
            "write_attributes, or stop consulting it in identity/role resolution — do not weaken this test."
        )

    @pytest.mark.parametrize("client", _HUMAN_CLIENTS)
    def test_trusted_github_login_is_never_self_writable(self, cognito_tf: str, client: str):
        """The specific attribute the onboarding resolver trusts."""
        writable = _attribute_list(_client_block(cognito_tf, client), "write_attributes")
        assert TRUSTED_LOGIN_ATTRIBUTE not in writable

    @pytest.mark.parametrize("client", _HUMAN_CLIENTS)
    def test_python_mirror_matches_terraform(self, cognito_tf: str, client: str):
        """``SELF_WRITABLE_ATTRIBUTES`` must track the Terraform, not drift from it.

        The Python constant is what the application-side tests parametrize over. If
        Terraform widens ``write_attributes`` and this mirror is not updated, those
        tests would keep passing while no longer covering the new attribute — so the
        mirror is pinned here rather than trusted.
        """
        writable = _attribute_list(_client_block(cognito_tf, client), "write_attributes")
        assert writable == set(SELF_WRITABLE_ATTRIBUTES), (
            f"write_attributes on client {client!r} is {sorted(writable)} but "
            f"SELF_WRITABLE_ATTRIBUTES is {sorted(SELF_WRITABLE_ATTRIBUTES)}. "
            "Update src/admin/onboarding/trusted_identity.py to match, and check whether the new "
            "attribute reaches an authorization decision."
        )


class TestPrivilegeAttributesStayReadOnly:
    """Regression cover for the attributes that already were excluded."""

    @pytest.mark.parametrize(
        "attribute",
        ["custom:role", "custom:org_id", "custom:team_id", "custom:department_id"],
    )
    @pytest.mark.parametrize("client", _HUMAN_CLIENTS)
    def test_privilege_claims_are_not_self_writable(self, cognito_tf: str, client: str, attribute: str):
        """These gate the approval middleware's fast path and the admin RBAC resolver.

        A self-writable ``custom:org_id`` would satisfy
        ``ApprovalEnforcementMiddleware._is_approved``'s claim fast path without any
        approval ever happening.
        """
        writable = _attribute_list(_client_block(cognito_tf, client), "write_attributes")
        assert attribute not in writable
