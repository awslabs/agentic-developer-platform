"""The identity assertion is only authority when the edge vouched for it — #5653 (A01).

``X-Caller-Identity`` is *proof of identity* to this service: it names an IAM
principal, and the agent registry resolves that to a privileged ``TokenContext``
(``scope="internal"``, ``allowed_models=["*"]``, platform org, no tenant binding).
Three separate call sites read the header, and none of them checked who wrote it.

What these tests assert, and why in this shape
----------------------------------------------
The property under test is the **invariant**, not an exploit string: *an assertion
the edge did not write is not an identity*. That distinction matters, because a
test that only feeds malformed junk passes against the vulnerable code — the old
code rejected unparseable ARNs too. So the payload used throughout is a
**well-formed assumed-role ARN naming the genuinely privileged principal that is
actually seeded in the registry** (``scaledjob-worker``, from
modules/agent-factory/infra/agent-registry-seed.tf). If provenance were skipped,
these requests would succeed with real platform-scope authority. Every test here
therefore fails against the pre-fix code, which is the only reason it is evidence.

All three consumers are covered, because partial fixes in this area have left doors
open before: closing ``get_current_user`` alone leaves the middleware and the
internal-endpoint guard reachable, and the same unauthenticated access remains
through a narrower door.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request

from src.auth.caller_provenance import (
    CALLER_IDENTITY_HEADER,
    EDGE_PROVENANCE_HEADER,
    has_caller_identity_assertion,
    verified_caller_identity,
)
from src.auth.dependencies import get_current_user
from src.auth.middleware import extract_iam_identity_from_headers
from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.config import Settings

# The real seeded privileged principal — NOT a synthetic placeholder. An assumed-role
# ARN for adp-dev-agent-scaledjob-role, whose registry entry carries scope="internal",
# allowed_models=["*"] and org __platform__. This is the value an attacker would pick.
PRIVILEGED_ARN = "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-scaledjob-role/session"

_INTERNAL_KEY = "test-internal-api-key"
_EDGE_SECRET = "test-edge-provenance-secret"


def _settings(*, trust: bool) -> MagicMock:
    settings = MagicMock()
    settings.trust_apigw_headers = trust
    settings.apigw_provenance_secret = _EDGE_SECRET
    settings.internal_api_key = _INTERNAL_KEY
    return settings


def _verified_headers() -> dict[str, str]:
    return {CALLER_IDENTITY_HEADER: PRIVILEGED_ARN, EDGE_PROVENANCE_HEADER: _EDGE_SECRET}


def _request(headers: dict[str, str], path: str = "/v1/messages") -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "method": "POST",
            "path": path,
        }
    )


def _privileged_registry_entry() -> dict:
    """The registry row the ARN above resolves to, as seeded in production."""
    return {
        "agent_id": "scaledjob-worker",
        "role_arn": "arn:aws:iam::123456789012:role/adp-dev-agent-scaledjob-role",
        "agent_name": "scaledjob-worker",
        "org_id": "__platform__",
        "team_id": "__agents__",
        "owner": "platform",
        "scope": "internal",
        "budget_config_id": "",
        "allowed_models": ["*"],
        "credential_scopes": ["credential:raw-read"],
        "status": "active",
        "description": "Hosted agent worker pods",
        "image_uri": "",
        "code_repo": "",
        "workflow_name": "",
        "created_at": "2026-05-12T00:00:00Z",
        "updated_at": "2026-05-12T00:00:00Z",
    }


class TestHelperContract:
    """The shared helper itself."""

    def test_unvouched_privileged_assertion_yields_no_identity(self):
        """The core invariant, stated once on the helper.

        A perfectly well-formed ARN naming the most privileged principal in the
        registry is not an identity when the edge did not vouch for it.
        """
        assert verified_caller_identity(_request({CALLER_IDENTITY_HEADER: PRIVILEGED_ARN}), settings=_settings(trust=False)) is None

    def test_vouched_assertion_is_returned_verbatim(self):
        """The legitimate path: an edge-written assertion is honoured unchanged."""
        assert verified_caller_identity(_request(_verified_headers()), settings=_settings(trust=True)) == PRIVILEGED_ARN

    def test_direct_cluster_assertion_is_rejected_when_edge_trust_is_enabled(self):
        request = _request({CALLER_IDENTITY_HEADER: PRIVILEGED_ARN})

        assert verified_caller_identity(request, settings=_settings(trust=True)) is None

    def test_forged_edge_proof_is_rejected(self):
        request = _request({CALLER_IDENTITY_HEADER: PRIVILEGED_ARN, EDGE_PROVENANCE_HEADER: "attacker-value"})

        assert verified_caller_identity(request, settings=_settings(trust=True)) is None

    def test_unconfigured_edge_secret_fails_closed(self):
        settings = _settings(trust=True)
        settings.apigw_provenance_secret = ""

        assert verified_caller_identity(_request(_verified_headers()), settings=settings) is None

    def test_absent_assertion_is_not_a_rejection(self):
        """No header means "no identity from this mechanism", not "deny".

        Ordinary bearer-token clients send no assertion at all; they must fall
        through to JWT validation rather than being rejected here.
        """
        assert verified_caller_identity(_request({}), settings=_settings(trust=True)) is None
        assert has_caller_identity_assertion(_request({})) is False

    def test_whitespace_only_assertion_is_not_an_identity(self):
        """Blank is what API Gateway can actually guarantee.

        The edge cannot *drop* a mapped integration header, only set it to the empty
        string, so "blank" is the shape a stripped assertion arrives in. It must be
        indistinguishable from absent — otherwise the edge control does not hold.
        """
        for blank in ("", "   ", "\t"):
            req = _request({CALLER_IDENTITY_HEADER: blank})
            assert has_caller_identity_assertion(req) is False
            assert verified_caller_identity(req, settings=_settings(trust=True)) is None

    def test_asserted_but_unvouched_is_distinguishable_from_silent(self):
        """The two states must not collapse.

        "Asserted nothing" falls through to bearer-token auth; "asserted something
        the edge did not write" is a forgery attempt the internal guard rejects
        outright. Conflating them would either reject honest clients or silently
        accept forgeries.
        """
        forged = _request({CALLER_IDENTITY_HEADER: PRIVILEGED_ARN})
        assert has_caller_identity_assertion(forged) is True
        assert verified_caller_identity(forged, settings=_settings(trust=False)) is None


class TestSafeDefaultInCode:
    """The safe value must not depend on deployment configuration."""

    def test_trust_defaults_off(self):
        """An environment that has not deployed the edge fix honours no assertion.

        This is what makes the two-stage rollout safe in either order: until the
        Terraform blanking is applied and the flag deliberately turned on, the
        application believes no assertion at all.
        """
        assert Settings(cognito_user_pool_id="", database_url="").trust_apigw_headers is False


class TestConsumerOneGetCurrentUser:
    """~18 routers sit behind this dependency (admin, usage, budget, knowledge...)."""

    @pytest.mark.asyncio
    async def test_unvouched_privileged_assertion_does_not_authenticate(self):
        """Falls through to JWT auth and fails there — never 200 as the agent.

        401 (not 403) is the point: the assertion was ignored entirely rather than
        treated as a failed identity claim, so it did not pre-empt bearer-token
        validation. A registry lookup must never happen for an unvouched ARN.
        """
        registry = MagicMock()

        with (
            patch("src.auth.dependencies.get_settings", return_value=_settings(trust=True)),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=registry),
        ):
            request = MagicMock(spec=Request)
            request.headers = Headers({CALLER_IDENTITY_HEADER.lower(): PRIVILEGED_ARN})
            request.state = type("S", (), {})()

            with pytest.raises(HTTPException) as exc:
                await get_current_user(request, authorization=None)

        assert exc.value.status_code == 401
        registry.get_agent_by_role_arn.assert_not_called()


class TestConsumerTwoMiddleware:
    """Guards the metered inference prefixes, where unmetered spend lives."""

    def test_unvouched_privileged_assertion_yields_no_context(self):
        registry = MagicMock()

        with (
            patch("src.auth.middleware.get_settings", return_value=_settings(trust=True)),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=registry),
        ):
            assert extract_iam_identity_from_headers(_request({CALLER_IDENTITY_HEADER: PRIVILEGED_ARN})) is None

        registry.get_agent_by_role_arn.assert_not_called()

    def test_vouched_privileged_assertion_still_authenticates(self):
        """Regression guard: the legitimate agent path is unchanged by this fix.

        Without this, "reject everything" would pass the whole file — and rejecting
        every agent workload is itself the outage this issue warns about.
        """
        service = MagicMock()
        service.get_agent_by_role_arn.return_value = _privileged_registry_entry()

        with (
            patch("src.auth.middleware.get_settings", return_value=_settings(trust=True)),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=service),
        ):
            context = extract_iam_identity_from_headers(_request(_verified_headers()))

        assert context is not None
        assert context.user_id == "scaledjob-worker"
        assert context.auth_source == "iam"


class TestConsumerThreeInternalGuard:
    """/internal/v1/* — credential issuance and platform control."""

    @staticmethod
    def _client() -> TestClient:
        app = FastAPI()

        @app.get("/internal/v1/probe")
        async def _probe(_: None = Depends(verify_internal_or_irsa)):
            return {"ok": True}

        return TestClient(app)

    def test_unvouched_privileged_assertion_rejected(self):
        with patch("src.internal.auth_deps.get_settings", return_value=_settings(trust=True)):
            resp = self._client().get("/internal/v1/probe", headers={CALLER_IDENTITY_HEADER: PRIVILEGED_ARN})

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "invalid_caller_identity"

    def test_shared_secret_required_when_no_vouched_identity(self):
        """An identity assertion is not a substitute for the shared secret.

        The issue's acceptance keeps a proven SigV4/IAM path (so a *vouched* caller
        needs no secret), but an unvouched assertion must not buy access either —
        it is rejected outright rather than falling through to the secret check.
        """
        with patch("src.internal.auth_deps.get_settings", return_value=_settings(trust=True)):
            client = self._client()
            # No credential at all.
            assert client.get("/internal/v1/probe").status_code == 403
            # A forged identity instead of the secret.
            assert client.get("/internal/v1/probe", headers={CALLER_IDENTITY_HEADER: PRIVILEGED_ARN}).status_code == 403

    def test_unvouched_assertion_does_not_mask_itself_behind_a_valid_secret(self):
        """Presence stays terminal (#3985), so a forgery attempt is never a 200.

        A caller holding the shared secret AND sending a forged identity must be
        rejected, not quietly served on the legacy path. Otherwise the attempt is
        invisible in the logs this issue exists to make trustworthy.
        """
        with patch("src.internal.auth_deps.get_settings", return_value=_settings(trust=False)):
            resp = self._client().get(
                "/internal/v1/probe",
                headers={CALLER_IDENTITY_HEADER: PRIVILEGED_ARN, "X-Internal-Api-Key": _INTERNAL_KEY},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "invalid_caller_identity"

    def test_legitimate_shared_secret_caller_unaffected(self):
        """The ClusterIP ingestion callback sends no assertion and must keep working."""
        with patch("src.internal.auth_deps.get_settings", return_value=_settings(trust=False)):
            resp = self._client().get("/internal/v1/probe", headers={"X-Internal-Api-Key": _INTERNAL_KEY})

        assert resp.status_code == 200
