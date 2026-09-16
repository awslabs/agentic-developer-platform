"""
Unit tests for GET /.well-known/cognito-config (Issue #4145).

`cli/bg-cognito-auth.sh` fetches this document before it holds any token — both
to bootstrap the interactive `login` flow and to learn the `client_id`/`region`
that `import` needs in order to refresh a browser-issued refresh token. So the
route must:
- be public (no Authorization header) — auth would break the bootstrap,
- return exactly the four keys the CLI reads,
- return 503 (not a JSON config body) when Cognito is unconfigured, because the
  CLI only checks that the response is valid JSON before persisting it,
- never return a client secret or user-specific data.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.routes import well_known_router
from src.shared.config import Settings

test_app = FastAPI()
test_app.include_router(well_known_router)
client = TestClient(test_app)

ROUTE = "/.well-known/cognito-config"


def _settings(**overrides) -> Settings:
    base = {
        "cognito_user_pool_id": "us-east-1_abc123XYZ",
        "cognito_client_id": "1h57kf5cpq17m0eml12EXAMPLE",
        "cognito_cli_client_id": "cli-public-client",
        "aws_region": "us-east-1",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.mark.unit
class TestCognitoConfigRoute:
    def test_returns_settings_values(self):
        with patch("src.auth.routes.get_settings", return_value=_settings()):
            resp = client.get(ROUTE)

        assert resp.status_code == 200
        assert resp.json() == {
            "user_pool_id": "us-east-1_abc123XYZ",
            "client_id": "1h57kf5cpq17m0eml12EXAMPLE",
            "cli_client_id": "cli-public-client",
            "identity_pool_id": "",
            "region": "us-east-1",
        }

    def test_identity_pool_id_is_always_empty(self):
        """The gateway does not know it, and neither `token` nor `import` uses it."""
        with patch("src.auth.routes.get_settings", return_value=_settings()):
            resp = client.get(ROUTE)

        assert resp.json()["identity_pool_id"] == ""

    def test_region_follows_settings(self):
        with patch("src.auth.routes.get_settings", return_value=_settings(aws_region="eu-west-1")):
            resp = client.get(ROUTE)

        assert resp.status_code == 200
        assert resp.json()["region"] == "eu-west-1"

    def test_requires_no_authorization_header(self):
        """Bootstrap path: the CLI has no token when it calls this."""
        with patch("src.auth.routes.get_settings", return_value=_settings()):
            resp = client.get(ROUTE)

        assert resp.status_code == 200
        assert "WWW-Authenticate" not in resp.headers

    def test_returns_503_when_cognito_unconfigured(self):
        """Must NOT return a 200 JSON body the CLI would persist as valid config."""
        with patch("src.auth.routes.get_settings", return_value=_settings(cognito_user_pool_id="")):
            resp = client.get(ROUTE)

        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "auth_not_configured"
        # No config keys leak through the error body.
        assert "client_id" not in resp.text

    def test_body_contains_no_secret_or_user_data(self):
        with patch("src.auth.routes.get_settings", return_value=_settings()):
            resp = client.get(ROUTE)

        body = resp.json()
        assert set(body) == {"user_pool_id", "client_id", "cli_client_id", "identity_pool_id", "region"}
        lowered = resp.text.lower()
        for forbidden in ("secret", "password", "email", "sub", "token"):
            assert forbidden not in lowered

    def test_route_is_not_under_the_auth_prefix(self):
        """The CLI fetches <gateway_url>/.well-known/cognito-config, not /auth/...."""
        from src.auth.routes import router

        auth_paths = {route.path for route in router.routes}
        assert ROUTE not in auth_paths
        assert ROUTE in {route.path for route in well_known_router.routes}
