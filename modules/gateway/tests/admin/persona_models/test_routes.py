"""HTTP endpoint tests for persona-model catalogue — Issue #5420 (PMM-03).

Integration tests that exercise the FastAPI routes through HTTPX/ASGI.

Operator item 8: tests prove the fail-closed contract, not incomplete behavior.
"""

from __future__ import annotations

import pytest

from .conftest import client_for, member_context


class TestPersonaCatalogueEndpoint:
    """GET /me/persona-models/catalog (no persona_key)."""

    @pytest.mark.asyncio
    async def test_returns_personas(self, session):
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog")
            assert resp.status_code == 200
            body = resp.json()
            assert "personas" in body
            assert len(body["personas"]) == 20

    @pytest.mark.asyncio
    async def test_persona_row_shape(self, session):
        """Each persona row has the required fields."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog")
            body = resp.json()
            for persona in body["personas"]:
                assert "key" in persona
                assert "display_name" in persona
                assert "purpose" in persona
                assert "configurable" in persona
                assert "compatibility_class" in persona

    @pytest.mark.asyncio
    async def test_pt_superpower_in_response(self, session):
        """pt-superpower appears but is not configurable."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog")
            body = resp.json()
            superpower = next(p for p in body["personas"] if p["key"] == "pt-superpower")
            assert superpower["configurable"] is False
            assert superpower["not_configurable_reason"] == "dispatches_without_persona_identity"


class TestModelCatalogueEndpoint:
    """GET /me/persona-models/catalog?persona_key=..."""

    @pytest.mark.asyncio
    async def test_returns_models_for_persona(self, session):
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog", params={"persona_key": "developer"})
            assert resp.status_code == 200
            body = resp.json()
            assert "models" in body
            assert body["persona_key"] == "developer"
            assert body["compatibility_class"] == "claude-agent-sdk"
            sonnet5 = next(model for model in body["models"] if model["canonical_model_id"] == "global.anthropic.claude-sonnet-5")
            assert sonnet5["aliases"] == ["sonnet5"]

    @pytest.mark.asyncio
    async def test_codex_reviewer_never_receives_claude_catalogue_fallback(self, session):
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get(
                "/me/persona-models/catalog",
                params={"persona_key": "agent-codex-reviewer"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["compatibility_class"] == "codex-sdk"
            assert {model["canonical_model_id"] for model in body["models"]} == {
                "openai.gpt-6-astra",
                "openai.gpt-6-sol",
                "openai.gpt-6-luna",
            }
            assert all(model["compatibility_class"] == "codex-sdk" for model in body["models"])

    @pytest.mark.asyncio
    async def test_model_row_shape(self, session):
        """Each model row has the required fields."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog", params={"persona_key": "developer"})
            body = resp.json()
            for model in body["models"]:
                assert "canonical_model_id" in model
                assert "aliases" in model
                assert "model_family" in model
                assert "canonical_version" in model
                assert "selectable" in model
                assert "compatibility_class" in model
                assert "harness_contract_revision" in model
                assert "retired" in model

    @pytest.mark.asyncio
    async def test_all_models_probing_disabled_without_destination(self, session):
        """Without destination resolution, all models are not selectable."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog", params={"persona_key": "developer"})
            body = resp.json()
            for model in body["models"]:
                assert model["selectable"] is False
                assert model["reason"] == "probing_disabled"
                assert model["invocable"] is None

    @pytest.mark.asyncio
    async def test_unknown_persona_422(self, session):
        """An unknown persona returns 422."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog", params={"persona_key": "nonexistent"})
            assert resp.status_code == 422
            body = resp.json()
            assert body["detail"]["reason"] == "unknown_persona"

    @pytest.mark.asyncio
    async def test_no_fable_in_models(self, session):
        """Fable 5.1 does not appear in the model catalogue (§7, #2300)."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/catalog", params={"persona_key": "developer"})
            body = resp.json()
            model_ids = [m["canonical_model_id"] for m in body["models"]]
            fable_ids = [mid for mid in model_ids if "fable" in mid.lower()]
            assert fable_ids == [], f"Fable models found in catalogue: {fable_ids}"

    @pytest.mark.asyncio
    async def test_no_resolve_alias_route(self, session):
        """Operator item 6: the /resolve-alias route was removed."""
        ctx = member_context()
        async with client_for(session, ctx) as client:
            resp = await client.get("/me/persona-models/resolve-alias", params={"alias": "opus46"})
            # Should be 404 or 405 since the route does not exist
            assert resp.status_code in (404, 405)
