"""Only reviewed origins may read this API — issue #5682 (A02).

## What was wrong

`app/config.py` shipped `cors_origins: list[str] = ["*"]` while `app/main.py`
added `CORSMiddleware` with `allow_credentials=True`. A deployment that set
nothing inherited a wildcard on a credentialed API.

With a wildcard and allow_credentials=True, Starlette reflects the requesting
origin on preflight responses. This test establishes that middleware behavior;
it does not establish that an arbitrary site can acquire a user's bearer token
or read an authenticated application response. The application must separately
verify credentials and tenant ownership on every protected request.

## What these tests pin

1. The shipped default is empty, not `["*"]` — the value-level claim, asserted
   on a fresh `Settings` rather than the configured singleton, because what a
   deployment inherits is the class default.
2. A wildcard is refused at startup wherever it comes from — as the whole value
   and as one member of an otherwise-reviewed list, which is the case likeliest
   to survive review.
3. An origin that is not on the list gets no allowance, and one that is does.
   The point of an allowlist is both halves.
4. No shipped deploy configuration carries a wildcard, so the refusal in (2)
   cannot be triggered by this repository's own files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# `app` is first-party, but this package configures no isort known-first-party,
# so ruff groups it as third-party and reports I001 on this block. Every sibling
# test file carries the same finding for the same reason; grouping correctly and
# matching them is preferred over letting `--fix` file `app.config` under
# third-party imports, which would be untrue.
from app.config import (
    Settings,
    WildcardCORSWithCredentials,
    resolve_cors_origins,
    settings,
)

DEPLOY_ROOT = Path(__file__).resolve().parents[1] / "deploy"

REVIEWED = "https://superplane.example.com"
UNREVIEWED = "https://attacker.example"


class TestTheShippedDefaultAllowsNoCrossOriginReader:
    """The value-level half: an unset variable must not produce a wildcard."""

    def test_the_default_is_empty(self) -> None:
        """`Settings()`, not the singleton: the class default is what deploys inherit."""
        assert Settings(_env_file=None).cors_origins == [], (
            "cors_origins must default to []. A wildcard default is a credentialed "
            "cross-origin read granted to every site, inherited by any deployment "
            "that sets nothing."
        )

    def test_the_default_resolves_to_no_allowance_rather_than_raising(
        self, monkeypatch
    ) -> None:
        """Empty is a valid, safe configuration — the same-origin SPA topology.

        This matters because the app must still start when CORS_ORIGINS is unset.
        If "unset" were itself a startup failure, operators would be pushed to set
        *something*, and the something people reach for is "*".
        """
        monkeypatch.setattr(settings, "cors_origins", [])
        assert resolve_cors_origins() == []


class TestAWildcardIsRefusedAtStartup:
    """The runtime half, including the member case that review tends to miss."""

    def test_the_whole_value_being_a_wildcard_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "cors_origins", ["*"])
        with pytest.raises(WildcardCORSWithCredentials):
            resolve_cors_origins()

    def test_a_wildcard_hidden_among_reviewed_origins_is_refused(
        self, monkeypatch
    ) -> None:
        """["https://legit", "*"] is exactly as permissive as ["*"].

        And far likelier to pass a skim, which is why it is checked separately
        rather than assumed to follow from the case above.
        """
        monkeypatch.setattr(settings, "cors_origins", [REVIEWED, "*"])
        with pytest.raises(WildcardCORSWithCredentials):
            resolve_cors_origins()

    def test_a_padded_wildcard_is_refused(self, monkeypatch) -> None:
        """Whitespace must not smuggle it past the check.

        A comma-separated env value is a normal way to supply this, and ' * '
        is what that produces.
        """
        monkeypatch.setattr(settings, "cors_origins", [" * "])
        with pytest.raises(WildcardCORSWithCredentials):
            resolve_cors_origins()

    def test_the_refusal_names_the_variable_and_explains_the_exposure(
        self, monkeypatch
    ) -> None:
        """An operator hitting this at 3am should not need to read the source."""
        monkeypatch.setattr(settings, "cors_origins", ["*"])
        with pytest.raises(WildcardCORSWithCredentials) as raised:
            resolve_cors_origins()
        message = str(raised.value)
        assert "CORS_ORIGINS" in message
        assert "credential" in message.lower()

    def test_reviewed_origins_survive_resolution(self, monkeypatch) -> None:
        """The check rejects the wildcard, not cross-origin use in general."""
        monkeypatch.setattr(settings, "cors_origins", [REVIEWED, UNREVIEWED])
        assert resolve_cors_origins() == [REVIEWED, UNREVIEWED]


class TestOnlyListedOriginsReceiveAnAllowance:
    """End-to-end through the middleware, which is what a browser actually sees."""

    @staticmethod
    async def _preflight(origins: list[str], origin: str):
        """Build an app with the given allowlist and preflight it from `origin`.

        A separate FastAPI instance rather than the real app: `add_middleware`
        runs at import, so the shipped app's allowlist is fixed by the time any
        test could monkeypatch it. What is under test is the middleware
        configuration this change produces, which is reproducible here exactly.
        """
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware

        probe = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        probe.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @probe.get("/probe")
        async def _probe():
            return {"ok": True}

        transport = ASGITransport(app=probe)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.options(
                "/probe",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                },
            )

    @pytest.mark.asyncio
    async def test_an_unlisted_origin_gets_no_allowance(self) -> None:
        response = await self._preflight([REVIEWED], UNREVIEWED)
        assert "access-control-allow-origin" not in response.headers, (
            "an origin outside the reviewed allowlist was granted a credentialed "
            "allowance"
        )

    @pytest.mark.asyncio
    async def test_a_listed_origin_gets_its_allowance(self) -> None:
        response = await self._preflight([REVIEWED], REVIEWED)
        assert response.headers["access-control-allow-origin"] == REVIEWED
        assert response.headers["access-control-allow-credentials"] == "true"

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_grants_nothing_to_anyone(self) -> None:
        response = await self._preflight([], REVIEWED)
        assert "access-control-allow-origin" not in response.headers

    @pytest.mark.asyncio
    async def test_wildcard_credentialed_preflight_reflects_unreviewed_origin(
        self,
    ) -> None:
        """A successful preflight is not evidence of application authentication."""
        response = await self._preflight(["*"], UNREVIEWED)
        assert response.headers["access-control-allow-origin"] == UNREVIEWED
        assert response.headers["access-control-allow-credentials"] == "true"


class TestNoShippedConfigurationCarriesAWildcard:
    """The refusal must not be reachable from this repository's own deploy files."""

    def test_the_deploy_env_template_has_no_wildcard(self) -> None:
        for line in (
            (DEPLOY_ROOT / "config.env").read_text(encoding="utf-8").splitlines()
        ):
            code = line.split("#", 1)[0].strip()
            if not code.startswith("CORS_ORIGINS"):
                continue
            value = code.split("=", 1)[1].strip()
            assert "*" not in value, (
                f"deploy/config.env would fail startup: {line.strip()!r}"
            )

    def test_the_integration_manifest_has_no_wildcard(self) -> None:
        """Integration tests call the API in-cluster, so they need no allowance.

        Pinned because a wildcard here would not just be a bad example — it would
        break the integration run itself now that startup refuses it, and the
        tempting repair is to weaken the refusal.
        """
        for line in (
            (DEPLOY_ROOT / "integration-test.yaml")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            code = line.split("#", 1)[0]
            if "CORS_ORIGINS" not in code:
                continue
            value = code.split(":", 1)[1].strip().strip("'\"")
            assert "*" not in json.loads(value), (
                f"the integration manifest would fail startup: {line.strip()!r}"
            )
