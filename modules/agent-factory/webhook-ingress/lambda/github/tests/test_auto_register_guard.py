"""Tests for the auto-register write-guard (Issue #2769).

Postgres is the single source of truth for the installation → tenant mapping.
_auto_register_installation() must:
  1. No-op when a Postgres-owned row (no auto_registered flag) exists.
  2. Write + tag auto_registered when no row exists and the installation resolves
     to a known Postgres tenant — writing the POSTGRES tenant, not the raw login.
  3. Emit InstallationTenantDrift when a Postgres-owned row's org differs from
     the webhook org login.

Issue #4046 (#2724 slice A): the gateway client returns three states
(resolved / not_found / error) instead of ``None`` for every non-success.

Issue #2724 (slice B): the deny has now landed, and the two states are no longer
treated alike:

  * ``not_found`` (authoritative gateway 404) → **DENY**: no rows written, no
    tenant returned, caller 403s ``unknown_installation``. This is
    ``TestTenantGate``, and it is the behavior the module docstring of
    ``_auto_register_installation`` promised since #2769 without implementing.
  * ``error`` (gateway unreachable) → fail OPEN but LOUD: the ``org_login``
    fallback row is still written so an outage does not reject every new
    install, but the result is marked NON-authoritative so the caller skips
    per-tenant credential provisioning.

``_auto_register_installation`` now returns an ``AutoRegisterResult(tenant_id,
authoritative)`` rather than a bare string, because "we wrote a routable row" and
"we know this org is a real tenant" are different facts — conflating them is what
let the platform App's private key be copied for any org that installed the App.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


def _mock_table_with(forward_item=None, reverse_item=None):
    """Return a mock DDB table whose get_item returns the given rows by identity_type."""
    table = MagicMock()

    def get_item(Key=None):  # noqa: N803
        itype = Key["identity_type"]
        if itype == "github_installation_id":
            return {"Item": forward_item} if forward_item else {}
        if itype == "org_installation":
            return {"Item": reverse_item} if reverse_item else {}
        return {}

    table.get_item = get_item
    return table


class TestAutoRegisterGuard:
    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_no_op_when_postgres_owned_row_exists(self, mock_resolver, mock_gw, mock_metric):
        """A row without auto_registered is Postgres-owned → no write, returns stored tenant."""
        from handler import _auto_register_installation

        # Postgres-owned row, same org login → no drift
        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144082554",
            "org_id": "pranavsharma1000",
        }
        table = _mock_table_with(forward_item=forward)
        mock_resolver.return_value._get_table.return_value = table

        result = _auto_register_installation(144082554, "pranavsharma1000")

        assert result.tenant_id == "pranavsharma1000"
        # Issue #2724: a Postgres-owned row IS the authoritative answer, so
        # downstream credential provisioning stays permitted.
        assert result.authoritative is True
        table.put_item.assert_not_called()
        # gateway not consulted — row already exists
        mock_gw.return_value.resolve_installation_by_id.assert_not_called()
        mock_metric.assert_not_called()

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_drift_metric_when_postgres_row_org_differs(self, mock_resolver, mock_gw, mock_metric):
        """Postgres-owned row whose org differs from webhook login → keep PG, emit drift."""
        from handler import _auto_register_installation

        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144082554",
            "org_id": "pranavsharma1000",
        }
        table = _mock_table_with(forward_item=forward)
        mock_resolver.return_value._get_table.return_value = table

        result = _auto_register_installation(144082554, "aws-innovate")

        assert result.tenant_id == "pranavsharma1000"  # Postgres tenant kept
        assert result.authoritative is True
        table.put_item.assert_not_called()
        mock_metric.assert_called_once_with("InstallationTenantDrift")

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_denies_when_gateway_says_not_a_known_tenant(
        self, mock_resolver, mock_gw, mock_metric
    ):
        """THE GATE (Issue #2724 slice B): an authoritative 404 writes NOTHING.

        Formerly ``test_falls_back_to_org_login_when_gateway_unknown``, which
        pinned the vulnerable fallthrough (and before that
        ``test_skips_when_not_known_tenant``, whose name claimed a skip its body
        contradicted — the mismatch that let the gap survive a merge and a
        security review). The deny has landed, so the expectation flips here
        deliberately, exactly as slice A promised.

        A gateway 404 means the gateway looked and no organization claims this
        installation. No identity rows, no tenant returned → the caller 403s
        ``unknown_installation``, which is the contract
        ``_auto_register_installation``'s docstring promised since #2769.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {"state": "not_found"}

        result = _auto_register_installation(555, "some-random-org")

        # No tenant → caller 403s. The org_login NEVER becomes a tenant_id.
        assert result.tenant_id is None
        assert result.authoritative is False
        # Neither the forward nor the reverse row is written.
        table.put_item.assert_not_called()
        mock_metric.assert_called_once_with("AutoRegisterDenied")

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_denies_self_created_shell_when_open_onboarding_off(
        self, mock_resolver, mock_gw, mock_metric
    ):
        """A tenant the installer created themselves is not a known tenant.

        Issue #2724: ``install_autocreate`` provenance means the only reason a
        Postgres tenant row exists is that the *unauthenticated* no-nonce install
        callback created it when this same party clicked Install. Trusting it
        would make the gate self-satisfiable — the whole reason the gate keys on
        provenance rather than existence.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "attacker-org",
            "created_via": "install_autocreate",
        }

        with patch.dict(os.environ, {"ORG_TENANT_AUTO_CREATE": "false"}):
            result = _auto_register_installation(555, "attacker-org")

        assert result.tenant_id is None
        assert result.authoritative is False
        table.put_item.assert_not_called()
        mock_metric.assert_called_once_with("AutoRegisterDenied")

    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_allows_self_created_shell_when_open_onboarding_on(self, mock_resolver, mock_gw):
        """The escape hatch: ORG_TENANT_AUTO_CREATE=true restores open onboarding.

        Issue #2724: deliberately-open deployments (hackathons, demos) opt in via
        the SAME single flag the gateway reads — there is no second Lambda-only
        flag. This is also the documented env-only instant rollback.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "hackathon-org",
            "created_via": "install_autocreate",
        }

        with patch.dict(os.environ, {"ORG_TENANT_AUTO_CREATE": "true"}):
            result = _auto_register_installation(555, "hackathon-org")

        assert result.tenant_id == "hackathon-org"
        # Resolved via the gateway, so provisioning is permitted.
        assert result.authoritative is True
        assert table.put_item.call_count == 2

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_fails_open_but_loud_when_gateway_errors(self, mock_resolver, mock_gw, mock_metric):
        """An ``error`` state must NOT deny — but must not provision either.

        Issue #2724 §3: a gateway outage cannot become "reject all new customer
        installations" (the top row of this issue's own blast-radius table), so
        the org_login fallback row is still written. But the result is marked
        NON-authoritative so the caller skips the per-tenant secret — that seed
        copies the platform App's private key, and we do not know whose org this
        is. Loud: ``AutoRegisterGateUnavailable``.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "error",
            "reason": "http_500",
        }

        result = _auto_register_installation(555, "some-random-org")

        # Fails OPEN: routing still works.
        assert result.tenant_id == "some-random-org"
        # But NOT authoritative: no credential provisioning.
        assert result.authoritative is False
        assert table.put_item.call_count == 2
        forward_item = table.put_item.call_args_list[0].kwargs["Item"]
        assert forward_item["org_id"] == "some-random-org"
        assert forward_item["auto_registered"] is True
        assert mock_metric.call_args_list[0].args[0] == "AutoRegisterGateUnavailable"

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_fails_open_when_gateway_provenance_absent(self, mock_resolver, mock_gw, mock_metric):
        """A gateway not yet redeployed with created_via must not brick onboarding.

        Issue #2724: unknown provenance is not untrusted. The Lambda and the
        gateway deploy independently, so during the rollout window a resolved
        result can legitimately carry no provenance. Fail open, loudly, and
        without provisioning credentials.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "acme",
            "created_via": "",
        }

        result = _auto_register_installation(555, "acme")

        # Allowed, but via the non-authoritative fallback path.
        assert result.tenant_id == "acme"
        assert result.authoritative is False
        assert mock_metric.call_args_list[0].args[0] == "AutoRegisterGateUnavailable"

    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_writes_postgres_tenant_when_known(self, mock_resolver, mock_gw):
        """No row + gateway resolves a TRUSTED tenant → write the POSTGRES tenant.

        Issue #2724: the no-over-tightening guard. An org an operator or an
        authenticated ADP flow onboarded must keep working exactly as before.
        """
        from handler import _auto_register_installation

        table = _mock_table_with()  # no existing rows
        mock_resolver.return_value._get_table.return_value = table
        # Gateway maps installation → Postgres tenant (which differs from the login)
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "pranavsharma1000",
            "created_via": "register_flow",
        }

        result = _auto_register_installation(144082554, "pranav-login")

        assert result.tenant_id == "pranavsharma1000"
        assert result.authoritative is True
        # Two writes: forward + reverse
        assert table.put_item.call_count == 2
        forward_call = table.put_item.call_args_list[0]
        forward_item = forward_call.kwargs["Item"]
        assert forward_item["org_id"] == "pranavsharma1000"  # NOT the raw login
        assert forward_item["auto_registered"] is True
        # Non-clobber condition on the fresh write
        assert forward_call.kwargs["ConditionExpression"] == "attribute_not_exists(auto_registered)"
        # Reverse row keyed on the Postgres tenant
        reverse_item = table.put_item.call_args_list[1].kwargs["Item"]
        assert reverse_item["identity_type"] == "org_installation"
        assert reverse_item["identity_value"] == "pranavsharma1000"
        assert reverse_item["installation_id"] == 144082554

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_812_ui_register_then_webhook_does_not_clobber(
        self, mock_resolver, mock_gw, mock_metric
    ):
        """Regression for the account-812447483903 split-brain (2026-07-04).

        Sequence reproduced live: the UI register flow writes the Postgres
        installation → tenant mapping (identity-index forward row, NO
        ``auto_registered`` flag) for org ``pranavsharma1000``; ~35 minutes
        later a GitHub webhook fires auto-register with the org *login*
        ``aws-innovate``. The webhook MUST NOT overwrite the Postgres-owned row
        with the login (that clobber is what rolled usage up to the phantom
        ``aws-innovate`` tenant, which has no Postgres org and no admin).

        Row shapes below are the real 812 artifacts captured before the account
        was wiped (per #2400: never invent fixture shapes).
        """
        from handler import _auto_register_installation

        # Real DDB adp-dev-identity-index forward row written by the UI register
        # flow on 812 — Postgres-owned (no auto_registered flag).
        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144240027",
            "org_id": "pranavsharma1000",
        }
        table = _mock_table_with(forward_item=forward)
        mock_resolver.return_value._get_table.return_value = table

        # Webhook auto-register fires later with the GitHub org login.
        result = _auto_register_installation(144240027, "aws-innovate")

        # Postgres tenant is kept; the phantom login never becomes the tenant.
        assert result.tenant_id == "pranavsharma1000"
        assert result.authoritative is True
        # No write at all — the Postgres-owned row is untouched.
        table.put_item.assert_not_called()
        # Gateway is not consulted: an existing row short-circuits the resolve.
        mock_gw.return_value.resolve_installation_by_id.assert_not_called()
        # Drift is surfaced for observability (org login != stored tenant).
        mock_metric.assert_called_once_with("InstallationTenantDrift")

    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_idempotent_refresh_of_auto_registered_row(self, mock_resolver, mock_gw):
        """An existing auto_registered row refreshes idempotently without a gateway call."""
        from handler import _auto_register_installation

        forward = {
            "identity_type": "github_installation_id",
            "identity_value": "144082554",
            "org_id": "pranavsharma1000",
            "auto_registered": True,
        }
        reverse = {
            "identity_type": "org_installation",
            "identity_value": "pranavsharma1000",
            "installation_id": 144082554,
            "auto_registered": True,
        }
        table = _mock_table_with(forward_item=forward, reverse_item=reverse)
        mock_resolver.return_value._get_table.return_value = table

        result = _auto_register_installation(144082554, "pranavsharma1000")

        assert result.tenant_id == "pranavsharma1000"
        # Issue #2724 grandfathering: the refresh path neither consults the gateway
        # nor checks provenance, so rows written before the gate existed keep
        # routing. It is NOT authoritative though — an auto_registered row may
        # itself be a pre-gate org_login fallback, so we do not re-seed on it.
        assert result.authoritative is False
        # Refresh path does not consult the gateway
        mock_gw.return_value.resolve_installation_by_id.assert_not_called()
        # Forward write has no ConditionExpression (idempotent overwrite)
        forward_call = table.put_item.call_args_list[0]
        assert "ConditionExpression" not in forward_call.kwargs


class TestPartialWriteSplit:
    """Forward/reverse error-scope split (Issue #4030).

    The two identity-index writes are not equivalent. Dispatch routes on the
    FORWARD row; the reverse row only serves ``adp-trigger`` resolution (#3860).
    A single ``except`` used to cover both, so a reverse-row failure returned
    None *after* the forward row was already persisted — and a None return makes
    the caller skip secret provisioning. Because the mapping now exists, every
    later webhook resolves fine and the ``unknown_installation`` self-heal branch
    never fires again, so the seed is never retried. That is the self-sustaining
    state the Acme PoV hit: dispatch worked, workers died on a missing secret.
    """

    @staticmethod
    def _table_failing_on(identity_type: str):
        """Mock table whose put_item raises only for the given identity_type."""
        table = MagicMock()

        def get_item(Key=None):
            return {}

        def put_item(Item=None, **kwargs):
            if Item["identity_type"] == identity_type:
                raise RuntimeError(f"DDB unavailable writing {identity_type}")
            return {}

        table.get_item = get_item
        table.put_item = MagicMock(side_effect=put_item)
        return table

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_reverse_write_failure_returns_tenant_and_emits_metric(
        self, mock_resolver, mock_gw, mock_metric
    ):
        """Reverse-row failure → tenant STILL returned + PartialWrite metric.

        This is the regression guard for the swallow gap. The forward row is
        persisted, so the tenant genuinely routes and the caller must be allowed
        to continue with its provisioning.
        """
        from handler import _auto_register_installation

        table = self._table_failing_on("org_installation")
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "acme-internal",
            "created_via": "operator",
        }

        result = _auto_register_installation(144082554, "acme-internal")

        # The key assertion: NOT None. Pre-#4030 this returned None.
        assert result.tenant_id == "acme-internal"
        assert result.authoritative is True
        mock_metric.assert_called_once_with("AutoRegister.PartialWrite")
        # Forward row was written before the reverse row blew up.
        forward_item = table.put_item.call_args_list[0].kwargs["Item"]
        assert forward_item["identity_type"] == "github_installation_id"
        assert forward_item["org_id"] == "acme-internal"

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_forward_write_failure_returns_none(self, mock_resolver, mock_gw, mock_metric):
        """Forward-row failure → None, and NO PartialWrite metric.

        Nothing routes to this installation, so returning a tenant would invert
        the bug: the caller would provision a secret for a tenant that cannot
        receive dispatch.
        """
        from handler import _auto_register_installation

        table = self._table_failing_on("github_installation_id")
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "acme-internal",
            "created_via": "operator",
        }

        result = _auto_register_installation(144082554, "acme-internal")

        assert result.tenant_id is None
        assert result.authoritative is False
        # PartialWrite is specifically "forward succeeded, reverse didn't".
        assert "AutoRegister.PartialWrite" not in [
            c.args[0] for c in mock_metric.call_args_list
        ]
        # Reverse write never attempted.
        assert table.put_item.call_count == 1

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_reverse_get_item_failure_also_returns_tenant(
        self, mock_resolver, mock_gw, mock_metric
    ):
        """The reverse-row READ is inside the partial-write scope too.

        The original bug report pointed at the reverse put_item, but the guard
        read that precedes it is equally past the point of no return — it must
        not be able to discard an already-persisted mapping either.
        """
        from handler import _auto_register_installation

        table = MagicMock()
        calls = {"n": 0}

        def get_item(Key=None):
            calls["n"] += 1
            if Key["identity_type"] == "org_installation":
                raise RuntimeError("DDB throttled on reverse read")
            return {}

        table.get_item = get_item
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "acme",
            "created_via": "operator",
        }

        result = _auto_register_installation(999, "acme")

        assert result.tenant_id == "acme"
        assert result.authoritative is True
        mock_metric.assert_called_once_with("AutoRegister.PartialWrite")

    @patch("handler._emit_metric")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    def test_conditional_check_failure_still_no_ops(self, mock_resolver, mock_gw, mock_metric):
        """Regression: the ConditionalCheckFailed race path is unchanged.

        A Postgres-owned row winning the read→write race returns the tenant and
        writes nothing further. Restructuring the error scopes must not turn this
        into a PartialWrite.
        """
        from handler import _auto_register_installation

        table = MagicMock()
        table.get_item = MagicMock(return_value={})

        class ConditionalCheckFailedException(Exception):
            pass

        table.put_item = MagicMock(side_effect=ConditionalCheckFailedException("race lost"))
        mock_resolver.return_value._get_table.return_value = table
        mock_gw.return_value.resolve_installation_by_id.return_value = {
            "state": "resolved",
            "tenant_id": "acme",
            "created_via": "operator",
        }

        result = _auto_register_installation(999, "acme")

        assert result.tenant_id == "acme"
        # A Postgres-owned row won the race, so the answer is authoritative.
        assert result.authoritative is True
        mock_metric.assert_not_called()
        # Only the forward attempt; the reverse row is not written on a lost race.
        assert table.put_item.call_count == 1
