"""Provenance verification on POST /agent/trigger — Issue #4128.

#4073 findings #18 + #1b: the route accepted caller-supplied lineage
(``root_human_id``, ``is_human_rooted``, ``chain_depth``,
``parent_invocation_id``, ``target.repo``) and forwarded it into the spawned run
without verifying any of it. Because ``authorized_user_id`` for the spawned run
is computed SERVER-SIDE from that provenance, a forgery is a direct
privilege-escalation into another tenant's authority that leaves no audit signal
distinguishing it from a real human-rooted run.

Each test here asserts an OUTCOME (status code / value passed to spawn_persona),
not an implementation detail, and each fails on pre-fix code.

CI path: this file lives under ``lambda/`` so ``webhook-ingress-ci.yml``'s
``pytest lambda/ -m "not integration"`` actually executes it. Tests placed in
``webhook-ingress/tests/`` never run in CI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_trigger import handle_agent_trigger  # noqa: E402
from common.marker_verify import reset_key_cache  # noqa: E402

REAL_KEY = "a-real-generated-key-32-bytes!!!"
PLACEHOLDER = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
TEST_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:trigger-prov"


@pytest.fixture(autouse=True)
def _clean_env():
    """Reset the key cache and the rollout flag around every test."""
    reset_key_cache()
    saved = os.environ.pop("REQUIRE_SIGNED_PROVENANCE", None)
    yield
    os.environ.pop("REQUIRE_SIGNED_PROVENANCE", None)
    if saved is not None:
        os.environ["REQUIRE_SIGNED_PROVENANCE"] = saved
    reset_key_cache()


def _make_event(body: dict) -> dict:
    return {
        "resource": "/agent/trigger",
        "httpMethod": "POST",
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "identity": {
                "userArn": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-worker-role/s"
            }
        },
    }


def _valid_body(**overrides) -> dict:
    defaults = {
        "correlation_id": "corr-prov-001",
        "parent_invocation_id": "inv-parent-001",
        "persona": "developer",
        "target": {"repo": "org/repo", "issue": 42},
        "reason": "need a developer",
    }
    defaults.update(overrides)
    return defaults


def _chain_record(**overrides) -> dict:
    defaults = {
        "event_id": "inv-parent-001",
        "arrived_at": "2026-08-26T10:00:00Z",
        "tenant_id": "org",
        "correlation_id": "corr-prov-001",
        "repo": "org/repo",
        "root_human_id": "user-human-789",
        "is_human_rooted": True,
        "chain_depth": 1,
        "user_id": "user-human-789",
        "status": "webhook_received",
    }
    defaults.update(overrides)
    return defaults


def _sign(key: str, correlation_id, root_human_id, is_human_rooted, invocation_id, chain_depth):
    signing_input = (
        f"{correlation_id}:{root_human_id}:{is_human_rooted}:{invocation_id}:{chain_depth}"
    )
    sig = hmac.new(key.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _marker_text(
    *,
    correlation_id="corr-prov-001",
    root_human_id="user-human-789",
    is_human_rooted="true",
    invocation_id="inv-parent-001",
    chain_depth="1",
    key: str | None = None,
    signature: str | None = None,
) -> str:
    parts = [
        f"adp-correlation:{correlation_id}",
        f"adp-root-human:{root_human_id}",
        f"adp-is-human-rooted:{is_human_rooted}",
        f"adp-invocation:{invocation_id}",
        f"adp-chain-depth:{chain_depth}",
    ]
    if signature is None and key is not None:
        signature = _sign(
            key, correlation_id, root_human_id, is_human_rooted, invocation_id, chain_depth
        )
    if signature:
        parts.append(f"adp-sig:{signature}")
    return f"<!-- {' '.join(parts)} -->"


def _mock_sm(secret_value: str) -> MagicMock:
    client = MagicMock()

    def _get(**kwargs):
        if kwargs.get("VersionStage") == "AWSCURRENT":
            return {"SecretString": secret_value}
        raise Exception("no previous version")

    client.get_secret_value.side_effect = _get
    return client


def _ok_spawn():
    return MagicMock(success=True, message_id="msg-ok", block_reason=None)


# =============================================================================
# Marker signature verification
# =============================================================================


class TestMarkerVerification:
    """A body-supplied provenance marker is verified, not trusted."""

    @patch("agent_trigger._resolve_chain")
    def test_forged_marker_returns_403(self, mock_resolve):
        """Forged/tampered marker → 403, NOT stripped-and-continued.

        Signed with an attacker's key, verified against the real one. Rejecting
        outright is the point: strip-and-continue on a credential-bearing plane
        is the fail-open class this issue closes.
        """
        mock_resolve.return_value = _chain_record()
        body = _valid_body(
            provenance_marker=_marker_text(root_human_id="victim-human", key="attacker-key-xxxxxx")
        )

        with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 403
        assert json.loads(resp["body"])["error"] == "unverified_provenance"

    @patch("agent_trigger._resolve_chain")
    def test_tampered_field_returns_403(self, mock_resolve):
        """A validly-signed marker whose root_human_id was then swapped → 403.

        This is the actual attack shape: take a real signed marker off a public
        comment, change whose authority it claims, replay it here.
        """
        mock_resolve.return_value = _chain_record()
        good_sig = _sign(REAL_KEY, "corr-prov-001", "user-human-789", "true", "inv-parent-001", "1")
        body = _valid_body(
            provenance_marker=_marker_text(root_human_id="victim-human", signature=good_sig)
        )

        with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 403
        assert json.loads(resp["body"])["error"] == "unverified_provenance"

    @patch("agent_trigger._resolve_chain")
    def test_unsigned_marker_403_when_flag_enabled(self, mock_resolve):
        """Unsigned marker → 403 once REQUIRE_SIGNED_PROVENANCE is on.

        None means indeterminate, and on this plane indeterminate is a rejection
        — None must never be treated as "allow".
        """
        mock_resolve.return_value = _chain_record()
        body = _valid_body(provenance_marker=_marker_text())  # no adp-sig

        env = {
            "MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN,
            "REQUIRE_SIGNED_PROVENANCE": "true",
        }
        with patch.dict(os.environ, env):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 403
        assert json.loads(resp["body"])["error"] == "unverified_provenance"

    @patch("agent_trigger._resolve_chain")
    def test_no_provenance_403_when_flag_enabled(self, mock_resolve):
        """Omitting provenance entirely must not sidestep the requirement."""
        mock_resolve.return_value = _chain_record()

        env = {
            "MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN,
            "REQUIRE_SIGNED_PROVENANCE": "1",
        }
        with patch.dict(os.environ, env):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(_valid_body()), None)

        assert resp["statusCode"] == 403
        assert json.loads(resp["body"])["error"] == "unverified_provenance"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_placeholder_key_never_verifies_a_forgery(
        self, mock_resolve, mock_spawn, mock_install
    ):
        """The placeholder hole, seen from this route.

        An attacker signs with the repo-readable placeholder. Pre-fix,
        verify_marker returned True and this marker was VERIFIED provenance.
        Post-fix the verdict is None — so with the flag on it is rejected, and
        with the flag off it confers no authority.
        """
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(
            provenance_marker=_marker_text(root_human_id="victim-human", key=PLACEHOLDER)
        )

        env = {
            "MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN,
            "REQUIRE_SIGNED_PROVENANCE": "true",
        }
        with patch.dict(os.environ, env):
            with patch("common.secrets._get_client", return_value=_mock_sm(PLACEHOLDER)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 403, "placeholder-signed marker must not be accepted"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_marker_root_human_never_overrides_chain(self, mock_resolve, mock_spawn, mock_install):
        """Even a VALIDLY signed marker cannot rewrite whose authority is used.

        root_human_id stays server-resolved from the chain record. The marker
        proves the caller is legitimate; it does not get to name the human.
        """
        mock_resolve.return_value = _chain_record(root_human_id="real-human-999")
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(
            provenance_marker=_marker_text(root_human_id="attacker-claimed-human", key=REAL_KEY)
        )

        with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 202
        ctx = mock_spawn.call_args[1]["correlation_ctx"]
        assert ctx["root_human_id"] == "real-human-999"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_legitimate_signed_dispatch_succeeds(self, mock_resolve, mock_spawn, mock_install):
        """Regression: a real human-rooted run still dispatches end-to-end."""
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(provenance_marker=_marker_text(key=REAL_KEY))

        env = {
            "MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN,
            "REQUIRE_SIGNED_PROVENANCE": "true",
        }
        with patch.dict(os.environ, env):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 202

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_unsigned_dispatch_still_works_with_flag_off(
        self, mock_resolve, mock_spawn, mock_install
    ):
        """Rollout safety: flag OFF keeps today's callers working.

        The adp-trigger client does not sign yet. Rejecting unsigned provenance
        unconditionally would 403 every agent→agent dispatch — the
        ALLOW_OPEN_SIGNUP failure mode of code and config landing out of step.
        """
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = _ok_spawn()

        with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(_valid_body()), None)

        assert resp["statusCode"] == 202


# =============================================================================
# chain_depth
# =============================================================================


class TestChainDepth:
    """chain_depth must never silently reset to 0."""

    @patch("agent_trigger._resolve_chain")
    def test_malformed_chain_depth_returns_422(self, mock_resolve):
        """Malformed chain_depth → 422, not a silent reset to 0.

        A reset turns the depth counter into a reset button: every hop re-enters
        at depth 0, so the runaway-chain guard stops bounding recursion.
        """
        mock_resolve.return_value = _chain_record(chain_depth="not-a-number")
        resp = handle_agent_trigger(_make_event(_valid_body()), None)
        assert resp["statusCode"] == 422
        assert json.loads(resp["body"])["error"] == "invalid_chain_depth"

    @patch("agent_trigger._resolve_chain")
    def test_absent_chain_depth_returns_422(self, mock_resolve):
        """An absent chain_depth is equally not a licence to reset to 0."""
        record = _chain_record()
        del record["chain_depth"]
        mock_resolve.return_value = record
        resp = handle_agent_trigger(_make_event(_valid_body()), None)
        assert resp["statusCode"] == 422
        assert json.loads(resp["body"])["error"] == "invalid_chain_depth"

    @patch("agent_trigger._resolve_chain")
    def test_negative_chain_depth_returns_422(self, mock_resolve):
        """A negative depth would buy extra hops under MAX_CHAIN_DEPTH."""
        mock_resolve.return_value = _chain_record(chain_depth=-5)
        resp = handle_agent_trigger(_make_event(_valid_body()), None)
        assert resp["statusCode"] == 422
        assert json.loads(resp["body"])["error"] == "invalid_chain_depth"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_depth_taken_from_verified_marker(self, mock_resolve, mock_spawn, mock_install):
        """Depth comes from the VERIFIED marker when one is supplied.

        Issue #4268 moved the +1 into ``spawn_persona`` (mocked here), so the
        asserted value is the marker's depth as resolved, not depth+1. The subject
        of this test is unchanged: WHICH source the depth is read from.
        """
        mock_resolve.return_value = _chain_record(chain_depth=1)
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(provenance_marker=_marker_text(chain_depth="6", key=REAL_KEY))

        with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 202
        # The marker's 6 wins over the chain record's 1 (the point of the test).
        assert mock_spawn.call_args[1]["correlation_ctx"]["chain_depth"] == 6

    @patch("agent_trigger._resolve_chain")
    def test_forged_marker_cannot_reset_depth(self, mock_resolve):
        """A forged marker claiming depth 0 is rejected before depth is read."""
        mock_resolve.return_value = _chain_record(chain_depth=7)
        body = _valid_body(
            provenance_marker=_marker_text(chain_depth="0", key="attacker-key-xxxxxx")
        )

        with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                resp = handle_agent_trigger(_make_event(body), None)

        assert resp["statusCode"] == 403


# =============================================================================
# parent_invocation_id
# =============================================================================


class TestParentInvocation:
    """The claimed parent must belong to the claimed chain."""

    @patch("agent_trigger._query_event_row")
    @patch("agent_trigger._resolve_chain")
    def test_parent_from_another_chain_rejected(self, mock_resolve, mock_row):
        """parent_invocation_id from a different chain → rejected.

        Without this the field is free text that fabricates a lineage edge the
        Activity chain view then renders as real. The row EXISTS here (it is a
        real invocation) but carries a different correlation_id, which is the
        forged-lineage case rather than a nonexistent-row case.
        """
        mock_resolve.return_value = _chain_record(event_id="inv-real-latest")
        mock_row.return_value = [
            _chain_record(
                event_id="inv-from-someone-elses-chain",
                correlation_id="corr-SOMEONE-ELSE",
            )
        ]
        body = _valid_body(parent_invocation_id="inv-from-someone-elses-chain")
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 422
        assert json.loads(resp["body"])["error"] == "unknown_parent_invocation"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._query_event_row")
    @patch("agent_trigger._resolve_chain")
    def test_parent_is_an_older_row_of_the_chain_accepted(
        self, mock_resolve, mock_row, mock_spawn, mock_install
    ):
        """Regression #1828: cross-issue lineage points at a non-latest ancestor.

        Requiring "parent == the newest row" would fragment legitimate chains,
        so any row of the chain is a valid parent.
        """
        mock_resolve.return_value = _chain_record(event_id="inv-newest")
        mock_row.return_value = [_chain_record(event_id="inv-ancestor")]
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(parent_invocation_id="inv-ancestor")
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 202
        ctx = mock_spawn.call_args[1]["correlation_ctx"]
        assert ctx["parent_invocation_id"] == "inv-ancestor"

    @patch("agent_trigger._query_event_row")
    @patch("agent_trigger._resolve_chain")
    def test_chain_scan_failure_fails_closed(self, mock_resolve, mock_row):
        """An unverifiable lineage edge is a rejection, not a pass."""
        mock_resolve.return_value = _chain_record(event_id="inv-newest")
        mock_row.return_value = []  # row absent, or the query failed
        body = _valid_body(parent_invocation_id="inv-unknown")
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 422
        assert json.loads(resp["body"])["error"] == "unknown_parent_invocation"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._query_chain")
    @patch("agent_trigger._query_event_row")
    @patch("agent_trigger._resolve_chain")
    def test_parent_older_than_the_recency_window_accepted(
        self, mock_resolve, mock_row, mock_chain, mock_spawn, mock_install
    ):
        """Regression #4245: chain length must not decide lineage validity.

        A long-lived orchestrator dispatches several children; each child writes
        chain rows, so the orchestrator's own row is pushed arbitrarily far back.
        The old implementation read only the newest 50 GSI rows and rejected
        anything older, so a valid dispatch started failing with 422 purely
        because the chain got busy (observed at 477 rows, caller at 477/477).

        The parent is resolved by primary key, so a chain of ANY length works and
        no bounded chain scan is consulted at all.
        """
        mock_resolve.return_value = _chain_record(event_id="inv-newest-of-477")
        mock_row.return_value = [_chain_record(event_id="inv-the-oldest-row")]
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(parent_invocation_id="inv-the-oldest-row")
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 202
        mock_row.assert_called_once_with("inv-the-oldest-row")
        # The recency-window scan must not gate the decision any more.
        mock_chain.assert_not_called()


# =============================================================================
# target.repo
# =============================================================================


class TestTargetRepoTenant:
    """target.repo must resolve to the chain's tenant."""

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=None)
    @patch("agent_trigger._resolve_chain")
    def test_repo_outside_tenant_rejected(self, mock_resolve, mock_install):
        """target.repo in another org → 403.

        The channel key and the spawned run's repo are both built from this body
        value, so an unchecked repo points a legitimate chain at another tenant.
        """
        mock_resolve.return_value = _chain_record(tenant_id="org", repo="org/repo")
        body = _valid_body(target={"repo": "victim-org/secrets", "issue": 1})
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 403
        assert json.loads(resp["body"])["error"] == "cross_tenant_target"

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_sibling_repo_in_same_org_allowed(self, mock_resolve, mock_spawn, mock_install):
        """A different repo in the chain's own org is legitimate."""
        mock_resolve.return_value = _chain_record(tenant_id="org", repo="org/repo")
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(target={"repo": "org/another-repo", "issue": 7})
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 202

    @patch("common.spawn_persona.spawn_persona")
    @patch("common.installation_resolver.resolve_installation_for_tenant")
    @patch("agent_trigger._resolve_chain")
    def test_owner_resolving_to_same_installation_allowed(
        self, mock_resolve, mock_install, mock_spawn
    ):
        """A tenant id that is not literally the org login still resolves.

        Reuses installation_resolver: owner and tenant mapping to the SAME
        installation is the same tenant.
        """
        mock_resolve.return_value = _chain_record(tenant_id="tenant-uuid-abc", repo="org/repo")
        mock_install.return_value = 1247  # both owner and tenant → same install
        mock_spawn.return_value = _ok_spawn()
        body = _valid_body(target={"repo": "other-login/repo", "issue": 3})
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 202

    @patch("common.installation_resolver.resolve_installation_for_tenant")
    @patch("agent_trigger._resolve_chain")
    def test_owner_resolving_to_different_installation_rejected(self, mock_resolve, mock_install):
        """Different installation → different tenant → rejected."""
        mock_resolve.return_value = _chain_record(tenant_id="tenant-uuid-abc", repo="org/repo")
        mock_install.side_effect = lambda who: 9999 if who == "victim-org" else 1247
        body = _valid_body(target={"repo": "victim-org/repo", "issue": 3})
        resp = handle_agent_trigger(_make_event(body), None)
        assert resp["statusCode"] == 403
        assert json.loads(resp["body"])["error"] == "cross_tenant_target"


# =============================================================================
# is_human_rooted default
# =============================================================================


class TestIsHumanRootedDefault:
    """A missing is_human_rooted attribute must not confer human authority."""

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_missing_is_human_rooted_treated_as_false(
        self, mock_resolve, mock_spawn, mock_install
    ):
        """Missing attribute → False.

        Pre-fix this defaulted to True, so a chain row that never established
        human rooting handed the spawned run a human's authority on the strength
        of an ABSENT field — and authorized_user_id is computed from it.
        """
        record = _chain_record()
        del record["is_human_rooted"]
        mock_resolve.return_value = record
        mock_spawn.return_value = _ok_spawn()

        resp = handle_agent_trigger(_make_event(_valid_body()), None)

        assert resp["statusCode"] == 202
        ctx = mock_spawn.call_args[1]["correlation_ctx"]
        assert ctx["is_human_rooted"] is False

    @patch("common.installation_resolver.resolve_installation_for_tenant", return_value=1247)
    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_explicit_true_is_preserved(self, mock_resolve, mock_spawn, mock_install):
        """Control: a genuinely human-rooted chain still is one."""
        mock_resolve.return_value = _chain_record(is_human_rooted=True)
        mock_spawn.return_value = _ok_spawn()
        resp = handle_agent_trigger(_make_event(_valid_body()), None)
        assert resp["statusCode"] == 202
        assert mock_spawn.call_args[1]["correlation_ctx"]["is_human_rooted"] is True
