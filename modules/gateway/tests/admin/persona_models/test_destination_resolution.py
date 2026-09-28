"""Destination resolution in catalogue routes — Issue #5420 (PMM-03).

Evidence is keyed on the destination, so what the route resolves decides
which evidence is even reachable.  These tests cover the resolution outcomes
and the fail-closed degradation when a destination cannot be determined.

**These tests deliberately do not fabricate a ``src.proxy.bedrock_routing``
module.**  An earlier version injected a stub into ``sys.modules`` and, on
cleanup, ``pop``-ed the key instead of restoring the original entry.  Any test
that had already imported the real module then found it evicted, and the next
importer silently got a fresh copy with fresh module-level state.  The
observable damage: running this file before
``tests/proxy/test_bedrock_routing_shadow.py::TestFlagIsARealOffSwitch``
turned that file's off-switch regression guard red (2 failed, 6 passed) — a
test suite disabling another suite's safety check.

The justification given for the stub (that ``src.proxy`` "may not have all
dependencies" under test) does not hold: ``tests/proxy/`` imports the real
module and passes.  So these tests patch the attribute on the real module
with ``unittest.mock.patch``, which restores the original automatically and
is both simpler and honest about what it replaces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.admin.persona_models.catalogue_routes import router as persona_models_router
from src.auth.dependencies import get_current_user
from src.proxy.bedrock_routing import BedrockTarget
from src.shared.database import get_db

from .conftest import member_context

PLATFORM_ACCOUNT = "555555555555"
RUNTIME_REGION = "us-east-1"


def _build_app(session, context):
    app = FastAPI()
    app.include_router(persona_models_router)

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: context
    return app


async def _get_catalogue(session, *, persona_key: str | None = "developer"):
    app = _build_app(session, member_context())
    params = {"persona_key": persona_key} if persona_key else None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/me/persona-models/catalog", params=params)


def _patch_resolver(target=None, *, side_effect=None):
    """Patch ``resolve`` on the real module singleton.

    Patching the attribute (rather than swapping the module) keeps the
    singleton identity intact, which is what the route depends on for its
    existence-gate cache.
    """
    mock_resolve = AsyncMock(return_value=target, side_effect=side_effect)
    return patch(
        "src.proxy.bedrock_routing.bedrock_routing_resolver.resolve",
        mock_resolve,
    ), mock_resolve


def _patch_settings(*, platform_account: str | None = PLATFORM_ACCOUNT, region: str = RUNTIME_REGION):
    settings = MagicMock()
    settings.platform_bedrock_account_id = platform_account or ""
    settings.aws_region = region
    return patch("src.admin.persona_models.catalogue_routes.get_settings", return_value=settings)


class TestResolverIdentity:
    """Operator round-4 finding 5: use the shared instance."""

    @pytest.mark.asyncio
    async def test_route_uses_the_module_singleton(self, session):
        """A per-request resolver instance defeats the existence-gate cache.

        ``bedrock_routing.py`` states the cache "lives on the instance and a
        per-request instance would defeat it entirely — the gate would then
        cost one query per call".  Constructing ``BedrockRoutingResolver()``
        in the route reintroduced that hot-path regression, so this asserts
        the class is never instantiated by the request.
        """
        target = BedrockTarget(account_id="111111111111", rung="user", region="eu-west-1")
        resolver_patch, mock_resolve = _patch_resolver(target)

        with _patch_settings(), resolver_patch, patch("src.proxy.bedrock_routing.BedrockRoutingResolver") as mock_cls:
            resp = await _get_catalogue(session)

        assert resp.status_code == 200
        mock_resolve.assert_awaited_once()
        mock_cls.assert_not_called()


class TestDestinationPreserved:
    """Operator round-4 item 3: do not discard a usable destination."""

    @pytest.mark.asyncio
    async def test_platform_target_keeps_account_and_uses_runtime_region(self, session):
        """The default path must reach destination-specific evidence.

        ``_platform_target()`` returns the configured account with
        ``region=None``.  Requiring both parts discarded the account entirely,
        so the platform rung — the path most deployments are on — could never
        match an evidence row, regardless of what had been probed.
        """
        target = BedrockTarget(account_id=None, rung="platform", region=None)
        resolver_patch, _ = _patch_resolver(target)

        with (
            _patch_settings(),
            resolver_patch,
            patch(
                "src.admin.persona_models.catalogue_routes.service.build_model_catalogue",
                new=AsyncMock(return_value=[]),
            ) as mock_build,
        ):
            resp = await _get_catalogue(session)

        assert resp.status_code == 200
        kwargs = mock_build.await_args.kwargs
        assert kwargs["account_id"] == PLATFORM_ACCOUNT, "configured platform account was discarded"
        assert kwargs["region"] == RUNTIME_REGION, "runtime region was not substituted"

    @pytest.mark.asyncio
    async def test_mapped_region_outranks_runtime_default(self, session):
        """An explicitly mapped region is authoritative."""
        target = BedrockTarget(account_id="111111111111", rung="team", region="ap-southeast-2")
        resolver_patch, _ = _patch_resolver(target)

        with (
            _patch_settings(),
            resolver_patch,
            patch(
                "src.admin.persona_models.catalogue_routes.service.build_model_catalogue",
                new=AsyncMock(return_value=[]),
            ) as mock_build,
        ):
            resp = await _get_catalogue(session)

        assert resp.status_code == 200
        kwargs = mock_build.await_args.kwargs
        assert kwargs["account_id"] == "111111111111"
        assert kwargs["region"] == "ap-southeast-2"


class TestFailuresAreDistinguishable:
    """Operator round-4 item 3: a fault and a config gap are different."""

    @pytest.mark.asyncio
    async def test_resolver_fault_degrades_without_500(self, session, caplog):
        """A resolver exception must not surface as a 500 (§6.3)."""
        resolver_patch, _ = _patch_resolver(side_effect=RuntimeError("DB connection failed"))

        with _patch_settings(), resolver_patch, caplog.at_level("WARNING"):
            resp = await _get_catalogue(session)

        assert resp.status_code == 200
        for model in resp.json()["models"]:
            assert model["reason"] == "probing_disabled"
        assert "catalogue_destination_resolver_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_unconfigured_platform_account_logs_distinctly(self, session, caplog):
        """No configured account is a config gap, not a resolver fault.

        The two need different operator action, so they must not share one
        log event — the previous catch-all made a real misconfiguration
        indistinguishable from normal operation.
        """
        target = BedrockTarget(account_id=None, rung="platform", region=None)
        resolver_patch, _ = _patch_resolver(target)

        with _patch_settings(platform_account=None), resolver_patch, caplog.at_level("WARNING"):
            resp = await _get_catalogue(session)

        assert resp.status_code == 200
        for model in resp.json()["models"]:
            assert model["reason"] == "probing_disabled"
        assert "catalogue_destination_unconfigured" in caplog.text
        assert "catalogue_destination_resolver_failed" not in caplog.text


class TestPersonaCatalogueSkipsResolution:
    @pytest.mark.asyncio
    async def test_persona_catalogue_does_not_resolve_destination(self, session):
        """The persona catalogue is destination-independent."""
        resolver_patch, mock_resolve = _patch_resolver(BedrockTarget(account_id=None, rung="platform"))

        with _patch_settings(), resolver_patch:
            resp = await _get_catalogue(session, persona_key=None)

        assert resp.status_code == 200
        assert "personas" in resp.json()
        mock_resolve.assert_not_awaited()
