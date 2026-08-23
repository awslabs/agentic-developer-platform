"""Tests for GitHub App configuration drift detection — Issue #4017.

The bug class under test: ADP validated the App's settings exactly once, at
registration, and never again. GitHub fires no event when an admin edits an
App's callback URL, permissions, events or webhook URL, so the platform's first
notice of a change was a user-visible failure — a dead login button, or agents
that silently never trigger.

Two invariants dominate these tests:

1. **Unknown is not broken.** Every check is tri-state; a GitHub outage, an
   absent expected value or an unregistered App must yield None (amber), never
   False (red). A false-negative red sends an operator to fix a non-problem.

2. **The callback URL is API-unverifiable and the repair action is read-only.**
   GitHub exposes no API to read an App's OAuth callback URL, so it is REPORTED,
   never diffed. And the repair action must never write a credential — a
   "re-validate config" button that clobbers a live client_secret is a worse
   outage than the drift it was fixing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.connections.routes import router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

SERVICE = "src.admin.connections.service"

# A well-formed App config: exactly what _build_app_manifest asks GitHub for.
GOOD_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "metadata": "read",
}
GOOD_EVENTS = [
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "label",
]

EXPECTED_WEBHOOK = "https://abc123.execute-api.us-east-1.amazonaws.com/dev/webhook"


@pytest.fixture(autouse=True)
def _clear_caches():
    """Drift state is module-level and per-pod; isolate every test."""
    from src.admin.connections.service import _invalidate_verification_cache

    _invalidate_verification_cache()
    yield
    _invalidate_verification_cache()


def _github_mocks(*, permissions=None, events=None, webhook_url=EXPECTED_WEBHOOK, app_status=200, hook_status=200, slug="my-app"):
    """Build the (app_response, hook_response) pair GET /app + /app/hook/config return."""
    app_resp = MagicMock()
    app_resp.status_code = app_status
    app_resp.json.return_value = {
        "slug": slug,
        "name": "My App",
        "permissions": GOOD_PERMISSIONS if permissions is None else permissions,
        "events": GOOD_EVENTS if events is None else events,
    }

    hook_resp = MagicMock()
    hook_resp.status_code = hook_status
    hook_resp.json.return_value = {"url": webhook_url}
    return app_resp, hook_resp


def _patch_github(app_resp, hook_resp):
    """Patch httpx.AsyncClient so /app and /app/hook/config route to their mocks."""

    async def _route_get(url, **kwargs):  # noqa: ARG001
        return hook_resp if "/app/hook/config" in url else app_resp

    client = MagicMock()
    client.get = _route_get
    ctx = MagicMock()
    ctx.__aenter__ = MagicMock(return_value=_awaitable(client))
    ctx.__aexit__ = MagicMock(return_value=_awaitable(None))
    return patch("httpx.AsyncClient", return_value=ctx)


def _awaitable(value):
    async def _coro():
        return value

    return _coro()


# ---------------------------------------------------------------------------
# diff_app_config — the pure comparison shared by registration and drift
# ---------------------------------------------------------------------------


class TestDiffAppConfig:
    def test_matching_config_is_all_true_with_no_warnings(self):
        from src.admin.connections.service import diff_app_config

        result = diff_app_config(
            actual_webhook_url=EXPECTED_WEBHOOK,
            actual_permissions=GOOD_PERMISSIONS,
            actual_events=GOOD_EVENTS,
            expected_webhook_url=EXPECTED_WEBHOOK,
        )

        assert result.webhook_url_matches is True
        assert result.permissions_match is True
        assert result.events_match is True
        assert result.warnings == []

    def test_webhook_url_drift_is_false_and_names_both_sides(self):
        from src.admin.connections.service import diff_app_config

        result = diff_app_config(
            actual_webhook_url="https://evil.example.com/hook",
            actual_permissions=GOOD_PERMISSIONS,
            actual_events=GOOD_EVENTS,
            expected_webhook_url=EXPECTED_WEBHOOK,
        )

        assert result.webhook_url_matches is False
        assert any("evil.example.com" in w and EXPECTED_WEBHOOK in w for w in result.warnings)

    def test_downgraded_permission_is_drift(self):
        from src.admin.connections.service import diff_app_config

        downgraded = {**GOOD_PERMISSIONS, "contents": "read"}
        result = diff_app_config(
            actual_webhook_url=EXPECTED_WEBHOOK,
            actual_permissions=downgraded,
            actual_events=GOOD_EVENTS,
            expected_webhook_url=EXPECTED_WEBHOOK,
        )

        assert result.permissions_match is False
        assert any("contents" in w for w in result.warnings)
        # Unrelated checks stay green — drift is per-field, not all-or-nothing.
        assert result.webhook_url_matches is True
        assert result.events_match is True

    def test_revoked_event_subscription_is_drift(self):
        from src.admin.connections.service import diff_app_config

        result = diff_app_config(
            actual_webhook_url=EXPECTED_WEBHOOK,
            actual_permissions=GOOD_PERMISSIONS,
            actual_events=[e for e in GOOD_EVENTS if e != "label"],
            expected_webhook_url=EXPECTED_WEBHOOK,
        )

        assert result.events_match is False
        assert any("label" in w for w in result.warnings)

    def test_unresolvable_expected_webhook_is_unknown_not_drift(self):
        """The core tri-state guarantee: no expected value means no verdict."""
        from src.admin.connections.service import diff_app_config

        result = diff_app_config(
            actual_webhook_url=EXPECTED_WEBHOOK,
            actual_permissions=GOOD_PERMISSIONS,
            actual_events=GOOD_EVENTS,
            expected_webhook_url="",
        )

        assert result.webhook_url_matches is None
        assert result.warnings == []

    def test_unreadable_webhook_from_github_is_unknown_with_hint(self):
        from src.admin.connections.service import diff_app_config

        result = diff_app_config(
            actual_webhook_url="",
            actual_permissions=GOOD_PERMISSIONS,
            actual_events=GOOD_EVENTS,
            expected_webhook_url=EXPECTED_WEBHOOK,
        )

        assert result.webhook_url_matches is None
        assert any("Could not verify webhook URL" in w for w in result.warnings)

    def test_unreachable_github_yields_no_verdicts_at_all(self):
        from src.admin.connections.service import diff_app_config

        result = diff_app_config(expected_webhook_url=EXPECTED_WEBHOOK, reachable=False)

        assert result.webhook_url_matches is None
        assert result.permissions_match is None
        assert result.events_match is None

    def test_manifest_and_diff_read_the_same_expected_config(self):
        """A drift checker built on a second copy of the expected config could
        report drift on an App that is exactly what we asked GitHub for."""
        from src.admin.connections.service import _build_app_manifest, diff_app_config

        manifest = _build_app_manifest(
            app_name="test-app",
            callback_url="https://example.com/cb",
            webhook_url=EXPECTED_WEBHOOK,
            setup_url="https://example.com/setup",
            public=False,
        )

        result = diff_app_config(
            actual_webhook_url=EXPECTED_WEBHOOK,
            actual_permissions=manifest["default_permissions"],
            actual_events=manifest["default_events"],
            expected_webhook_url=EXPECTED_WEBHOOK,
        )

        assert result.permissions_match is True
        assert result.events_match is True


# ---------------------------------------------------------------------------
# check_app_config — the GitHub read path
# ---------------------------------------------------------------------------


class TestCheckAppConfig:
    @pytest.mark.asyncio
    async def test_reads_webhook_url_from_the_hook_config_endpoint(self):
        """GET /app does NOT include the webhook URL; it lives at /app/hook/config."""
        from src.admin.connections.service import check_app_config

        app_resp, hook_resp = _github_mocks()
        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
        ):
            result = await check_app_config(app_id="123", pem="pem", expected_webhook_url=EXPECTED_WEBHOOK)

        assert result.reachable is True
        assert result.actual_webhook_url == EXPECTED_WEBHOOK
        assert result.webhook_url_matches is True

    @pytest.mark.asyncio
    async def test_unreadable_hook_config_leaves_only_webhook_unknown(self):
        """Two endpoints, two failure modes: permissions/events stay authoritative."""
        from src.admin.connections.service import check_app_config

        app_resp, hook_resp = _github_mocks(hook_status=403)
        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
        ):
            result = await check_app_config(app_id="123", pem="pem", expected_webhook_url=EXPECTED_WEBHOOK)

        assert result.webhook_url_matches is None
        assert result.permissions_match is True
        assert result.events_match is True

    @pytest.mark.asyncio
    async def test_github_5xx_degrades_to_unknown_without_raising(self):
        from src.admin.connections.service import check_app_config

        app_resp, hook_resp = _github_mocks(app_status=503)
        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
        ):
            result = await check_app_config(app_id="123", pem="pem", expected_webhook_url=EXPECTED_WEBHOOK)

        assert result.reachable is False
        assert result.permissions_match is None

    @pytest.mark.asyncio
    async def test_network_error_never_propagates(self):
        """This decorates a status page; it must not be able to break it."""
        from src.admin.connections.service import check_app_config

        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            patch("httpx.AsyncClient", side_effect=OSError("connection reset")),
        ):
            result = await check_app_config(app_id="123", pem="pem", expected_webhook_url=EXPECTED_WEBHOOK)

        assert result.reachable is False

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self):
        from src.admin.connections.service import check_app_config

        result = await check_app_config(app_id="", pem="", expected_webhook_url=EXPECTED_WEBHOOK)

        assert result.reachable is False
        assert result.permissions_match is None


# ---------------------------------------------------------------------------
# _compute_app_config_drift — throttle + expected-value precedence
# ---------------------------------------------------------------------------


class TestComputeAppConfigDrift:
    @pytest.mark.asyncio
    async def test_unregistered_app_reports_unknown_not_drift(self):
        from src.admin.connections.service import _compute_app_config_drift

        with patch(f"{SERVICE}._get_github_app_credentials", return_value=("", "")):
            result = await _compute_app_config_drift()

        assert result["app_webhook_url_matches"] is None
        assert result["app_permissions_match"] is None
        assert result["app_events_match"] is None

    @pytest.mark.asyncio
    async def test_cache_bounds_github_call_volume(self):
        """GitHub is rate-limited and the gateway serves this on a page load, so
        repeated reads must not mean repeated App-JWT calls."""
        from src.admin.connections.service import _compute_app_config_drift

        calls = []

        async def _counting_check(**kwargs):
            from src.admin.connections.service import AppConfigCheck

            calls.append(kwargs)
            return AppConfigCheck(reachable=True, app_slug="my-app", webhook_url_matches=True, permissions_match=True, events_match=True)

        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch(f"{SERVICE}._read_expected_app_config", return_value={}),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
            patch(f"{SERVICE}.check_app_config", _counting_check),
        ):
            first = await _compute_app_config_drift()
            second = await _compute_app_config_drift()

        assert len(calls) == 1, "second call should be served from the drift cache"
        assert first == second

    @pytest.mark.asyncio
    async def test_live_webhook_url_wins_over_the_recorded_one(self):
        """If webhook-ingress was redeployed, deliveries must go to the NEW
        endpoint — trusting the stored baseline would report a broken App as ok."""
        from src.admin.connections.service import _compute_app_config_drift

        seen = {}

        async def _capture(**kwargs):
            from src.admin.connections.service import AppConfigCheck

            seen.update(kwargs)
            return AppConfigCheck(reachable=True)

        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch(f"{SERVICE}._read_expected_app_config", return_value={"expected_webhook_url": "https://old.example.com/webhook"}),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
            patch(f"{SERVICE}.check_app_config", _capture),
        ):
            await _compute_app_config_drift()

        assert seen["expected_webhook_url"] == EXPECTED_WEBHOOK

    @pytest.mark.asyncio
    async def test_callback_url_is_reported_with_a_deep_link_never_diffed(self):
        from src.admin.connections.schemas import PlatformVerification
        from src.admin.connections.service import _compute_app_config_drift

        async def _check(**kwargs):  # noqa: ARG001
            from src.admin.connections.service import AppConfigCheck

            return AppConfigCheck(reachable=True, app_slug="my-app")

        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch(f"{SERVICE}._read_expected_app_config", return_value={}),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value="https://api.example.com/dev/auth/github/callback"),
            patch(f"{SERVICE}.check_app_config", _check),
        ):
            result = await _compute_app_config_drift()

        assert result["expected_callback_url"] == "https://api.example.com/dev/auth/github/callback"
        assert result["app_oauth_settings_url"] == "https://github.com/settings/apps/my-app/oauth"
        # There is no boolean callback field to render red — by construction.
        assert not [f for f in PlatformVerification.model_fields if "callback" in f and f != "expected_callback_url"]

    @pytest.mark.asyncio
    async def test_drift_failure_does_not_break_platform_verification(self):
        """The settings page must render even if the drift read explodes."""
        from src.admin.connections.service import _compute_platform_verification

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "whsec_real_value"}

        with (
            patch(f"{SERVICE}._compute_app_config_drift", side_effect=RuntimeError("boom")),
            patch(f"{SERVICE}._check_login_enabled", return_value=True),
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            result = await _compute_platform_verification()

        # The pre-existing #4016 checks still report, and the drift fields
        # degrade to unknown rather than taking the whole block down.
        assert result.login_credentials is True
        assert result.webhook_secret is True
        assert result.app_permissions_match is None
        assert result.app_webhook_url_matches is None
        assert result.app_events_match is None


# ---------------------------------------------------------------------------
# revalidate_app_config — the repair action
# ---------------------------------------------------------------------------


class TestRevalidateAppConfig:
    @pytest.mark.asyncio
    async def test_writes_zero_credential_secrets(self):
        """Review §6, the hard constraint: repair is read-only against GitHub and
        writes NOTHING but expected_* keys. A re-validate button that clobbers a
        live client_secret or private key is a worse outage than the drift."""
        from src.admin.connections.service import revalidate_app_config

        existing_meta = json.dumps(
            {
                "app_id": "123",
                "app_slug": "my-app",
                "client_id": "Iv1.live",
                "client_secret": "live_secret",
                "webhook_secret": "whsec_live",
            }
        )

        put_calls = []
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": existing_meta}
        mock_sm.put_secret_value.side_effect = lambda **kw: put_calls.append(kw)

        app_resp, hook_resp = _github_mocks()
        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
            patch("boto3.client", return_value=mock_sm),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value="https://api.example.com/dev/auth/github/callback"),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            result = await revalidate_app_config(actor="admin@example.com")

        assert result["checked"] is True

        # Every write must target the -meta secret only.
        written_ids = [kw["SecretId"] for kw in put_calls]
        for secret_id in written_ids:
            assert secret_id.endswith("-meta"), f"repair wrote a non-meta secret: {secret_id}"
        for forbidden in ("-id", "-key", "cognito/github-oauth-credentials", "webhook-ingress/github-webhook-secret"):
            assert not [s for s in written_ids if s.endswith(forbidden) or forbidden in s]

        # And every credential key in the meta blob must survive untouched.
        for kw in put_calls:
            blob = json.loads(kw["SecretString"])
            assert blob["client_id"] == "Iv1.live"
            assert blob["client_secret"] == "live_secret"
            assert blob["webhook_secret"] == "whsec_live"

    @pytest.mark.asyncio
    async def test_never_calls_store_app_credentials(self):
        """_store_app_credentials writes six secrets plus two write-throughs, so
        the repair path must not route through it (review §6)."""
        from src.admin.connections.service import revalidate_app_config

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"app_slug": "my-app"})}

        app_resp, hook_resp = _github_mocks()
        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
            patch("boto3.client", return_value=mock_sm),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
            patch(f"{SERVICE}._store_app_credentials") as mock_store,
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            await revalidate_app_config(actor="admin@example.com")

        mock_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_reports_drift_in_the_message(self):
        from src.admin.connections.service import revalidate_app_config

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"app_slug": "my-app"})}

        app_resp, hook_resp = _github_mocks(webhook_url="https://stale.example.com/webhook")
        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
            patch("boto3.client", return_value=mock_sm),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            result = await revalidate_app_config(actor="admin@example.com")

        assert result["app_webhook_url_matches"] is False
        assert "drifted" in result["message"]
        assert "webhook URL" in result["message"]

    @pytest.mark.asyncio
    async def test_unregistered_app_is_reported_not_an_error(self):
        from src.admin.connections.service import revalidate_app_config

        with patch(f"{SERVICE}._get_github_app_credentials", return_value=("", "")):
            result = await revalidate_app_config(actor="admin@example.com")

        assert result["checked"] is False
        assert result["expected_config_recorded"] is False
        assert result["warnings"]

    @pytest.mark.asyncio
    async def test_unreachable_github_is_unknown_not_drift(self):
        from src.admin.connections.service import revalidate_app_config

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"app_slug": "my-app"})}

        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            patch("httpx.AsyncClient", side_effect=OSError("connection reset")),
            patch("boto3.client", return_value=mock_sm),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            result = await revalidate_app_config(actor="admin@example.com")

        assert result["checked"] is False
        assert result["app_permissions_match"] is None
        assert result["app_webhook_url_matches"] is None
        assert any("unknown, not failed" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_clears_the_drift_cache_so_the_button_is_not_a_no_op(self):
        from src.admin.connections import service

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"app_slug": "my-app"})}

        service._app_config_drift_cache = (float("inf"), {"stale": True})

        app_resp, hook_resp = _github_mocks()
        with (
            patch(f"{SERVICE}._get_github_app_credentials", return_value=("123", "pem")),
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="jwt"),
            _patch_github(app_resp, hook_resp),
            patch("boto3.client", return_value=mock_sm),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            await service.revalidate_app_config(actor="admin@example.com")

        assert service._app_config_drift_cache is None

    @pytest.mark.asyncio
    async def test_logs_actor_and_changed_keys(self, caplog):
        """Every repair invocation is auditable (review §6)."""
        from src.admin.connections.service import _record_expected_app_config

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"app_slug": "my-app", "client_secret": "live"})}

        with (
            patch("boto3.client", return_value=mock_sm),
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value="https://api.example.com/dev/auth/github/callback"),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
            caplog.at_level("INFO"),
        ):
            recorded = _record_expected_app_config(actor="admin@example.com")

        assert recorded is True
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "admin@example.com" in logged
        assert "expected_webhook_url" in logged
        # The audit line must never carry a credential.
        assert "live" not in logged

    @pytest.mark.asyncio
    async def test_absent_app_metadata_is_not_invented(self):
        """Creating the meta secret here would fabricate App state that the
        registration flow owns."""
        from botocore.exceptions import ClientError

        from src.admin.connections.service import _record_expected_app_config

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}},
            "GetSecretValue",
        )

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            recorded = _record_expected_app_config(actor="admin@example.com")

        assert recorded is False
        mock_sm.put_secret_value.assert_not_called()
        mock_sm.create_secret.assert_not_called()


# ---------------------------------------------------------------------------
# Expected-config resolution
# ---------------------------------------------------------------------------


class TestExpectedConfigResolution:
    def test_record_never_contains_credentials(self):
        from src.admin.connections.service import _resolve_expected_app_config

        with (
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=EXPECTED_WEBHOOK),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value="https://api.example.com/dev/auth/github/callback"),
        ):
            record = _resolve_expected_app_config(
                existing={"client_secret": "live_secret", "webhook_secret": "whsec"},
            )

        assert all(k.startswith("expected_") for k in record)
        assert "live_secret" not in json.dumps(record)
        assert "whsec" not in json.dumps(record)

    def test_falls_back_to_previously_recorded_values(self):
        from src.admin.connections.service import _resolve_expected_app_config

        with (
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=""),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
        ):
            record = _resolve_expected_app_config(
                existing={"expected_webhook_url": EXPECTED_WEBHOOK, "expected_callback_url": "https://old/cb"},
            )

        assert record["expected_webhook_url"] == EXPECTED_WEBHOOK
        assert record["expected_callback_url"] == "https://old/cb"

    def test_unresolvable_values_are_omitted_not_empty(self):
        """An absent expected value reads as 'unknown' downstream; an empty
        string stored as a baseline could later be diffed against."""
        from src.admin.connections.service import _resolve_expected_app_config

        with (
            patch(f"{SERVICE}._resolve_expected_webhook_url", return_value=""),
            patch(f"{SERVICE}._resolve_expected_oauth_callback_url", return_value=""),
        ):
            record = _resolve_expected_app_config(existing={})

        assert "expected_webhook_url" not in record
        assert "expected_callback_url" not in record
        assert record["expected_permissions"] == GOOD_PERMISSIONS

    def test_non_string_ssm_value_does_not_break_json_storage(self):
        """A non-string from SSM used to raise inside json.dumps and fail the
        whole registration over an unresolvable optional hint."""
        from src.admin.connections.service import _read_ssm_string

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": MagicMock()}}

        with (
            patch("boto3.client", return_value=mock_ssm),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1"}),
        ):
            assert _read_ssm_string("/adp/dev/whatever") == ""

    def test_read_expected_config_never_returns_credential_keys(self):
        from src.admin.connections.service import _read_expected_app_config

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {
                    "app_slug": "my-app",
                    "client_secret": "live_secret",
                    "expected_webhook_url": EXPECTED_WEBHOOK,
                }
            )
        }

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            result = _read_expected_app_config()

        assert "client_secret" not in result
        assert "live_secret" not in json.dumps(result)
        assert result["expected_webhook_url"] == EXPECTED_WEBHOOK


# ---------------------------------------------------------------------------
# POST /admin/connections/github/app/revalidate — authorization
# ---------------------------------------------------------------------------


def _make_user(*, is_admin: bool) -> TokenContext:
    return TokenContext(
        user_id="user-001",
        org_id="org-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _make_client(*, is_admin: bool) -> TestClient:
    application = FastAPI()
    application.include_router(router)

    async def override_get_current_user():
        return _make_user(is_admin=is_admin)

    async def override_get_db():
        yield MagicMock()

    application.dependency_overrides[get_current_user] = override_get_current_user
    application.dependency_overrides[get_db] = override_get_db
    return TestClient(application, raise_server_exceptions=False)


class TestRevalidateRoute:
    def test_non_admin_returns_403(self):
        """This endpoint reads deployment-global App config, so it is admin-only."""
        client = _make_client(is_admin=False)

        resp = client.post("/admin/connections/github/app/revalidate")

        assert resp.status_code == 403
        assert "platform administrator" in resp.json()["detail"].lower()

    def test_non_admin_never_reaches_the_service(self):
        with patch("src.admin.connections.routes.revalidate_app_config", new=AsyncMock()) as mock_service:
            client = _make_client(is_admin=False)
            client.post("/admin/connections/github/app/revalidate")

        mock_service.assert_not_called()

    def test_admin_gets_the_drift_result(self):
        client = _make_client(is_admin=True)
        payload = {
            "checked": True,
            "app_webhook_url_matches": False,
            "app_permissions_match": True,
            "app_events_match": None,
            "expected_callback_url": "https://api.example.com/dev/auth/github/callback",
            "app_oauth_settings_url": "https://github.com/settings/apps/my-app/oauth",
            "warnings": ["Webhook URL mismatch"],
            "expected_config_recorded": True,
            "message": "App configuration has drifted: webhook URL.",
        }

        with patch("src.admin.connections.routes.revalidate_app_config", new=AsyncMock(return_value=payload)):
            resp = client.post("/admin/connections/github/app/revalidate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["app_webhook_url_matches"] is False
        assert body["app_events_match"] is None
        assert body["expected_callback_url"] == "https://api.example.com/dev/auth/github/callback"

    def test_passes_the_actor_for_the_audit_trail(self):
        client = _make_client(is_admin=True)
        payload = {"checked": True, "warnings": [], "expected_config_recorded": True, "message": "ok"}

        with patch("src.admin.connections.routes.revalidate_app_config", new=AsyncMock(return_value=payload)) as mock_service:
            client.post("/admin/connections/github/app/revalidate")

        assert mock_service.call_args.kwargs["actor"] == "user-001"

    def test_service_failure_is_a_500_not_a_stack_trace(self):
        client = _make_client(is_admin=True)

        with patch("src.admin.connections.routes.revalidate_app_config", new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = client.post("/admin/connections/github/app/revalidate")

        assert resp.status_code == 500
        assert "boom" not in resp.text
