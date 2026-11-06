"""Status through source, profile, historical revision and onboarding session reads."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.auth.aws_connection_authority import invalidate_connection
from tests.products.source_status import assert_status_surfaces
from tests.products.test_i04 import GatewayClient, cli
from tests.products.test_i08_api import mutation
from tests.products.test_i10 import setup_aws


@pytest.mark.asyncio
async def test_source_expiry_and_revocation_recheck_every_project_surface(
    test_client,
    db_session,
    app,
    source_member,
    monkeypatch,
):
    path, selection, credential, secret = await setup_aws(
        test_client,
        db_session,
        app,
        source_member,
        monkeypatch,
        verified_at=datetime.now(UTC),
    )
    root = path.removesuffix("/bindings")
    saved = await test_client.post(path, json=selection, headers=mutation(1, "v06_status_binding_12345"))
    assert saved.status_code == 201, saved.text
    session = await test_client.post(root + "/onboarding-sessions", json={}, headers=mutation(2, "v06_status_session_12345"))
    assert session.status_code == 201, session.text
    session_path = root + "/onboarding-sessions/" + session.json()["id"]
    expiry = datetime.fromisoformat(saved.json()["stale_after"].replace("Z", "+00:00"))
    observed_at = saved.json()["observed_at"]
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    scope = ("--product", root.split("/")[3], "--project", root.split("/")[5])

    def freeze(moment):
        for module in ("src.products.source_routes", "src.products.routes", "src.product_onboarding.routes"):
            monkeypatch.setattr(module + ".utcnow", lambda: moment)

    async def read_surfaces():
        sources = await test_client.get(root + "/sources", params={"connection_kind": "credential", "connection_id": credential.id})
        bindings = await test_client.get(path)
        profile = await test_client.get(root)
        revision = await test_client.get(root + "/revisions/2")
        onboarding = await test_client.get(session_path)
        for response in (sources, bindings, profile, onboarding):
            assert response.status_code == 200, response.text
        assert revision.status_code in (200, 404), revision.text
        return (
            sources.json()["items"][0],
            bindings.json()["items"][0],
            profile.json(),
            (revision.json() if revision.status_code == 200 else None),
            onboarding.json(),
            revision,
        )

    freeze(expiry - timedelta(seconds=1))
    source, binding, profile, revision, onboarding, revision_response = await read_surfaces()
    assert revision_response.status_code == 200
    assert_status_surfaces(
        source,
        binding,
        profile,
        revision,
        onboarding,
        phase="verified",
        observed_at=observed_at,
        native_id="eu-west-1",
        private_values=(secret.account, secret.role),
    )

    freeze(expiry)
    source, binding, profile, revision, onboarding, revision_response = await read_surfaces()
    assert revision_response.status_code == 200
    assert_status_surfaces(
        source,
        binding,
        profile,
        revision,
        onboarding,
        phase="stale",
        observed_at=observed_at,
        native_id="eu-west-1",
        private_values=(secret.account, secret.role),
    )
    assert binding["observed_at"] == observed_at

    invalidate_connection(credential)
    await db_session.commit()
    source, binding, profile, revision, onboarding, revision_response = await read_surfaces()
    assert_status_surfaces(
        source,
        binding,
        profile,
        revision,
        onboarding,
        phase="restricted",
        observed_at=None,
        native_id="eu-west-1",
        private_values=(secret.account, secret.role),
    )
    for value in (secret.account, secret.role):
        assert value not in revision_response.text
    cli_bindings = await asyncio.to_thread(cli.execute, cli.parser().parse_args(("source-bindings", *scope)), gateway)
    assert cli_bindings["detail"]["items"] == [binding]
    cli_sources = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(("sources", *scope, "--connection-kind", "credential", "--connection-id", credential.id)),
        gateway,
    )
    assert cli_sources["detail"]["items"][0]["coverage"] == "unavailable"
    assert secret.account not in json.dumps(cli_sources["detail"])


@pytest.mark.parametrize("surface", ("profile", "revision", "session"))
def test_revoked_surface_oracle_rejects_private_details(surface):
    from tests.products.source_status import assert_status_surfaces

    status = {"progress": {"checks": [{"id": "environment", "status": "missing"}]}}
    views = {key: json.loads(json.dumps(status)) for key in ("profile", "revision", "session")}
    views[surface]["configuration"] = {"verified_environment": {"role_ref": "private-role"}}
    with pytest.raises(AssertionError):
        assert_status_surfaces(
            {"coverage": "unavailable", "health": "unverified"},
            {"status": "restricted"},
            views["profile"],
            views["revision"],
            views["session"],
            phase="restricted",
            observed_at=None,
            native_id="us-east-1",
            private_values=("private-role",),
        )


def test_expired_surface_oracle_rejects_verified_readiness():
    from tests.products.source_status import assert_status_surfaces

    source = {
        "coverage": "partial",
        "health": "healthy",
        "verified_at": "2026-10-20T00:00:00Z",
        "resources": [{"native_id": "us-east-1", "observed_at": "2026-10-20T00:00:00Z"}],
    }
    binding = {"status": "degraded", "observed_at": "2026-10-20T00:00:01Z"}
    stale = {"progress": {"checks": [{"id": "environment", "status": "stale"}]}}
    falsely_complete = {"progress": {"checks": [{"id": "environment", "status": "complete"}]}}
    with pytest.raises(AssertionError):
        assert_status_surfaces(
            source,
            binding,
            stale,
            falsely_complete,
            stale,
            phase="stale",
            observed_at=binding["observed_at"],
            native_id="us-east-1",
            private_values=(),
        )
