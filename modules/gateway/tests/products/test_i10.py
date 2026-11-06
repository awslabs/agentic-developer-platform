"""Independent, run-owned evidence for version-bound AWS environment selection."""

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.aws_connection_authority import connection_binding, invalidate_connection
from src.auth.dependencies import get_current_user
from src.auth.vault_routes import get_secrets_manager
from src.products.models import Product, ProductOperation, ProductSourceBinding, Project
from src.shared.models.organization import User
from src.shared.models.vault import UserCredential
from tests.products.test_i04 import GatewayClient, cli
from tests.products.test_i08_api import mutation, setup_source


class SecretFixture:
    def __init__(self):
        self.version = "fixture-version"
        self.account = "123456789012"
        self.role = "arn:aws:iam::123456789012:role/ADP-Agent-Role"
        self.secret = json.dumps(
            {
                "role_arn": self.role,
                "account_id": self.account,
                "external_id": "run-owned-external-id",
                "default_region": "eu-west-1",
            }
        )
        self.reads = []

    def current_version_id(self, secret_arn):
        self.reads.append(("version", secret_arn))
        return self.version

    def get_secret_at_version(self, secret_arn, version):
        self.reads.append(("secret", secret_arn, version))
        return self.secret, version


async def setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=None):
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    path, _ = await setup_source(test_client, db_session, source_member)
    secret = SecretFixture()
    app.dependency_overrides[get_secrets_manager] = lambda: secret
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="aws",
        credential_type="aws_role",
        label="Verified test AWS",
        secret_arn="run-owned-secret",
        aws_external_id="run-owned-external-id",
        scopes={"account_id": secret.account, "role_arn": secret.role, "status": "verified"},
        aws_verified_at=verified_at or datetime.now(UTC),
        aws_verified_version_id=secret.version,
        aws_verification_attempt="run-owned-attempt",
    )
    db_session.add(credential)
    await db_session.flush()
    credential.aws_verified_binding = connection_binding(credential)
    await db_session.commit()
    selection = {
        "kind": "environment",
        "provider": "aws",
        "namespace": secret.account,
        "native_id": "eu-west-1",
        "connection_ref": {"kind": "credential", "id": credential.id},
        "environment": {"name": "Staging", "environment_class": "staging", "production": False},
    }
    return path, selection, credential, secret


@pytest.mark.asyncio
async def environment_connection_identity(test_client, db_session, test_engine, app, source_member, monkeypatch, record_property):
    verified_at = datetime.now(UTC).replace(microsecond=0)
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=verified_at)
    root = path.removesuffix("/bindings")
    discovered = await test_client.get(
        root + "/sources",
        params={
            "connection_kind": "credential",
            "connection_id": credential.id,
        },
    )
    assert discovered.status_code == 200, discovered.text
    choice = discovered.json()["items"][0]
    assert choice["coverage"] == "partial"
    assert choice["resources"][0]["observed_at"] == verified_at.isoformat().replace("+00:00", "Z")
    assert choice["resources"][0]["verified_environment"] == {
        "provider": "aws",
        "account_id": secret.account,
        "regions": ["eu-west-1"],
        "role_ref": secret.role,
        "permission_tier": "unknown",
        "verified_at": choice["resources"][0]["observed_at"],
        "source_revision": secret.version,
    }
    created = await test_client.post(path, json=selection, headers=mutation(1, "i10_environment_create_123"))
    assert created.status_code == 201, created.text
    assert created.json()["verified_environment"]["role_ref"] == secret.role
    assert created.json()["status"] == "healthy"
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        project = await independent.get(Project, (source_member.org_id, root.split("/")[5]))
        product = await independent.get(Product, (source_member.org_id, root.split("/")[3]))
        binding = await independent.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == project.id))
        assert project.configuration["advanced"]["environments"] == [
            {
                "tenant_id": source_member.org_id,
                "project_id": project.id,
                "source_connection_id": credential.id,
                "record_type": "environment",
                "record_id": "eu-west-1",
                "record_revision": secret.version,
            }
        ]
        assert (binding.namespace, binding.native_id, binding.project_id) == (secret.account, "eu-west-1", project.id)
        assert product.revision == 2
    assert secret.reads.count(("secret", "run-owned-secret", secret.version)) == 2
    record_property("scenario_id", "SCN-01")
    record_property("criterion", "AC-01")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property("provider_source_revision", secret.version)
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("fixture_clock", verified_at.isoformat())
    record_property("observation", "Pinned connection version and role match discovered native account/region and committed Project ref")
    record_property("attempt_report", "Gateway read and create plus independent persisted binding, Project and Product revision readback")


@pytest.mark.asyncio
async def test_environment_rejects_mismatch_and_changed_version(test_client, db_session, app, source_member, monkeypatch):
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    wrong = {**selection, "namespace": "000000000000"}
    response = await test_client.post(path, json=wrong, headers=mutation(1, "i10_wrong_account_1234"))
    assert response.status_code == 422
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0
    secret.version = "changed-version"
    response = await test_client.post(path, json=selection, headers=mutation(1, "i10_rotated_secret_123"))
    assert response.status_code == 409
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0
    secret.version = "fixture-version"
    invalidate_connection(credential)
    await db_session.commit()
    assert (
        await test_client.get(
            path.removesuffix("/bindings") + "/sources",
            params={
                "connection_kind": "credential",
                "connection_id": credential.id,
            },
        )
    ).json()["items"][0]["coverage"] == "unavailable"
    assert (await test_client.post(path, json=selection, headers=mutation(1, "i10_revoked_secret_1234"))).status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0


@pytest.mark.asyncio
async def source_picker_coverage(test_client, db_session, test_engine, app, source_member, monkeypatch, tmp_path, record_property):
    verified_at = datetime.now(UTC).replace(microsecond=0)
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=verified_at)
    root = path.removesuffix("/bindings")
    baseline = await test_client.get(root + "/sources")
    assert baseline.status_code == 200
    assert any(item["provider"] == "aws" and item["capabilities"] == ["environment"] for item in baseline.json()["items"])
    assert all("pipeline" not in item["capabilities"] for item in baseline.json()["items"] if item["provider"] == "aws")
    assert all("support" not in item["capabilities"] for item in baseline.json()["items"])
    query = {"connection_kind": "credential", "connection_id": credential.id}
    aws = (await test_client.get(root + "/sources", params=query)).json()["items"][0]
    assert aws["coverage"] == "partial" and aws["health"] == "healthy"
    assert aws["resources"][0]["observed_at"] == verified_at.isoformat().replace("+00:00", "Z")
    assert [(item["kind"], item["namespace"], item["native_id"]) for item in aws["resources"]] == [("environment", secret.account, "eu-west-1")]
    assert not any(item["kind"] in ("pipeline", "support") for item in aws["resources"])
    gateway = GatewayClient(test_client, asyncio.get_running_loop())

    async def invoke(*arguments):
        return await asyncio.to_thread(cli.execute, cli.parser().parse_args(arguments), gateway)

    scope = ("--product", root.split("/")[3], "--project", root.split("/")[5])
    listed = (await invoke("sources", *scope, "--connection-kind", "credential", "--connection-id", credential.id))["detail"]
    assert listed["items"][0]["resources"] == aws["resources"]
    assert "other regions" in listed["items"][0]["next_prerequisite"]
    assert len(listed["next_prerequisites"]) == 2
    saved_input = tmp_path / "environment.json"
    saved_input.write_text(json.dumps(selection), encoding="utf-8")
    saved = (await invoke("source-bind", *scope, "--revision", "1", "--idempotency-key", "i10_cli_source_bind_123", "--input", str(saved_input)))[
        "detail"
    ]
    assert saved["project_id"] == scope[3] and saved["selection"] == selection
    secret.version = "rotated-version"
    unavailable = (await invoke("sources", *scope, "--connection-kind", "credential", "--connection-id", credential.id))["detail"]
    assert unavailable["items"][0]["coverage"] == "unavailable"
    assert "Verify" in unavailable["items"][0]["next_prerequisite"]
    assert "resources" not in unavailable["items"][0]
    assert (
        await test_client.patch(root, json={"name": "Saved without adapter"}, headers=mutation(2, "i10_product_save_partial_123"))
    ).status_code == 200
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        project = await independent.get(Project, (source_member.org_id, scope[3]))
        product = await independent.get(Product, (source_member.org_id, scope[1]))
        binding = await independent.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == project.id))
        assert product.name == "Saved without adapter" and product.revision == 3
        assert binding.kind == "environment" and binding.status == "healthy"
        assert (binding.namespace, binding.native_id, binding.observed["source_revision"]) == (secret.account, "eu-west-1", "fixture-version")
        assert project.configuration["advanced"]["environments"][0]["record_revision"] == "fixture-version"
    record_property("scenario_id", "SCN-02")
    record_property("criterion", "AC-02")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property("provider_source_revision", "fixture-version")
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("fixture_clock", verified_at.isoformat())
    record_property("observation", "Partial AWS scope yielded one native region; AWS has no pipeline/support adapter")
    record_property(
        "attempt_report", "Separate gateway/CLI coverage reads and independent persisted Project/Product readback after unavailable source"
    )


@pytest.mark.asyncio
async def test_unavailable_adapters_do_not_fabricate_resources(test_client, db_session, app, source_member, monkeypatch):
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    root = path.removesuffix("/bindings")
    invalidate_connection(credential)
    await db_session.commit()
    response = await test_client.get(root + "/sources", params={"connection_kind": "credential", "connection_id": credential.id})
    assert response.status_code == 200
    assert response.json()["items"][0]["coverage"] == "unavailable"
    assert response.json()["items"][0]["health"] == "unverified"
    assert "resources" not in response.json()["items"][0]
    assert (
        await test_client.post(
            path,
            json={**selection, "kind": "pipeline", "environment": None, "repository_id": "35", "source_revision": "rev"},
            headers=mutation(1, "i10_pipeline_unverified_123"),
        )
    ).status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0


@pytest.mark.asyncio
async def source_picker_scope(test_client, db_session, test_engine, app, source_member, monkeypatch, record_property):
    verified_at = datetime.now(UTC).replace(microsecond=0)
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch, verified_at=verified_at)
    product_id, first_id = path.split("/")[3], path.split("/")[5]
    second = await test_client.post(
        f"/v1/products/{product_id}/projects", json={"mode": "create", "name": "Second Project"}, headers=mutation(1, "i10_create_second_scope_123")
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["project_id"]
    first = await test_client.post(path, json=selection, headers=mutation(2, "i10_first_scope_source_123"))
    assert first.status_code == 201, first.text
    root = f"/v1/products/{product_id}/projects"
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        one = await independent.get(Project, (source_member.org_id, first_id))
        two = await independent.get(Project, (source_member.org_id, second_id))
        assert len(one.configuration["advanced"]["environments"]) == 1
        assert two.configuration == {}
    assert (await test_client.get(f"{root}/{second_id}/bindings")).json()["items"] == []
    assert (await test_client.get(f"{root}/{first_id}/bindings")).json()["items"][0]["id"] == first.json()["id"]
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    first_cli_bindings = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(
            [
                "source-bindings",
                "--product",
                product_id,
                "--project",
                first_id,
            ]
        ),
        gateway,
    )
    assert first_cli_bindings["detail"]["items"] == (await test_client.get(f"{root}/{first_id}/bindings")).json()["items"]
    one_cli = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(
            [
                "sources",
                "--product",
                product_id,
                "--project",
                first_id,
                "--connection-kind",
                "credential",
                "--connection-id",
                credential.id,
            ]
        ),
        gateway,
    )
    two_cli = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(
            [
                "sources",
                "--product",
                product_id,
                "--project",
                second_id,
                "--connection-kind",
                "credential",
                "--connection-id",
                credential.id,
            ]
        ),
        gateway,
    )
    assert one_cli["detail"]["items"][0]["resources"] == two_cli["detail"]["items"][0]["resources"]
    assert one_cli["detail"]["items"][0]["coverage"] == "partial"
    second_selection = {
        **selection,
        "environment": {**selection["environment"], "name": "Production", "environment_class": "production", "production": True},
    }
    added = await test_client.post(f"{root}/{second_id}/bindings", json=second_selection, headers=mutation(3, "i10_second_scope_source_123"))
    assert added.status_code == 201, added.text
    assert added.json()["id"] != first.json()["id"]
    assert (await test_client.get(f"{root}/{first_id}/bindings")).json()["items"][0]["selection"]["environment"]["name"] == "Staging"
    assert (await test_client.get(f"{root}/{second_id}/bindings")).json()["items"][0]["selection"]["environment"]["name"] == "Production"
    assert (
        await test_client.post(f"{root}/{second_id}/bindings/{first.json()['id']}/verify", headers=mutation(4, "i10_foreign_scope_verify_123"))
    ).status_code == 404

    invalidate_connection(credential)
    await db_session.commit()
    for project_id in (first_id, second_id):
        visible = (await test_client.get(f"{root}/{project_id}/bindings")).json()["items"]
        assert len(visible) == 1 and visible[0]["status"] == "restricted" and "selection" not in visible[0]
        cli_bindings = await asyncio.to_thread(
            cli.execute,
            cli.parser().parse_args(
                [
                    "source-bindings",
                    "--product",
                    product_id,
                    "--project",
                    project_id,
                ]
            ),
            gateway,
        )
        assert cli_bindings["detail"]["items"] == visible
        profile = (await test_client.get(f"{root}/{project_id}")).json()
        assert not profile["configuration"].get("basic", {}).get("environments")
        checks = {item["id"]: item["status"] for item in profile["progress"]["checks"]}
        assert checks["environment"] != "complete"
        assert secret.role not in json.dumps(profile)
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        first_project = await independent.get(Project, (source_member.org_id, first_id))
        second_project = await independent.get(Project, (source_member.org_id, second_id))
        assert first_project.configuration["advanced"]["environments"][0]["record_revision"] == "fixture-version"
        assert second_project.configuration["advanced"]["environments"][0]["record_revision"] == "fixture-version"
        assert first_project.configuration["advanced"]["environments"][0]["project_id"] == first_id
        assert second_project.configuration["advanced"]["environments"][0]["project_id"] == second_id
    record_property("scenario_id", "SCN-03")
    record_property("criterion", "AC-03")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property("provider_source_revision", secret.version)
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("fixture_clock", verified_at.isoformat())
    record_property("observation", "Both Projects retain separate native refs; revoked connection redacts both and drops readiness")
    record_property("attempt_report", "Independent persisted Project readback and separate API/CLI redacted status reads after revocation")


@pytest.mark.asyncio
async def test_revocation_during_source_read_redacts_native_metadata(test_client, db_session, app, source_member, monkeypatch):
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    from src.product_onboarding.source_discovery import aws_environment

    async def revoked_while_reading(connection, secrets_manager):
        resource = await aws_environment(connection, secrets_manager)
        assert resource is not None
        invalidate_connection(credential)
        await db_session.commit()
        return resource

    monkeypatch.setattr("src.product_onboarding.source_discovery.aws_environment", revoked_while_reading)
    response = await test_client.get(
        path.removesuffix("/bindings") + "/sources",
        params={
            "connection_kind": "credential",
            "connection_id": credential.id,
        },
    )
    assert response.status_code == 200, response.text
    source = response.json()["items"][0]
    assert source["coverage"] == "unavailable" and source["health"] == "unverified"
    assert "resources" not in source and "verified_at" not in source
    assert secret.account not in response.text and secret.role not in response.text
    assert (await test_client.post(path, json=selection, headers=mutation(1, "i10_revoked_race_binding_123"))).status_code == 404


@pytest.mark.asyncio
async def test_login_subject_resolves_credential_owner_across_source_reads(test_client, db_session, app, source_member, monkeypatch):
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    member = await db_session.get(User, source_member.user_id)
    member.cognito_sub = "run-owned-login-subject"
    await db_session.commit()
    actor = source_member.model_copy(update={"user_id": member.cognito_sub})
    app.dependency_overrides[get_current_user] = lambda: actor
    root = path.removesuffix("/bindings")
    assert (await test_client.get(root)).status_code == 200
    listed = await test_client.get(root + "/sources")
    assert listed.status_code == 200
    assert credential.id in [source["connection_ref"]["id"] for source in listed.json()["items"]]
    discovered = await test_client.get(root + "/sources", params={"connection_kind": "credential", "connection_id": credential.id})
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["items"][0]["resources"][0]["verified_environment"]["role_ref"] == secret.role
    receipt = mutation(1, "i10_login_subject_binding_123")
    saved = await test_client.post(path, json=selection, headers=receipt)
    assert saved.status_code == 201, saved.text
    replay = await test_client.post(path, json=selection, headers=receipt)
    assert replay.status_code == 200 and replay.json()["status"] == "healthy"
    bindings = (await test_client.get(path)).json()["items"]
    assert bindings[0]["status"] == "healthy" and bindings[0]["selection"] == selection
    profile = (await test_client.get(root)).json()
    assert profile["configuration"]["advanced"]["environments"][0]["source_connection_id"] == credential.id
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    cli_bindings = await asyncio.to_thread(
        cli.execute,
        cli.parser().parse_args(
            [
                "source-bindings",
                "--product",
                root.split("/")[3],
                "--project",
                root.split("/")[5],
            ]
        ),
        gateway,
    )
    assert cli_bindings["detail"]["items"] == bindings
    invalidate_connection(credential)
    await db_session.commit()
    restricted = (await test_client.get(path)).json()["items"][0]
    assert restricted["status"] == "restricted" and "selection" not in restricted
    assert (await test_client.get(root)).json()["configuration"]["advanced"]["environments"] == []
    assert actor.user_id == member.cognito_sub and actor.org_id == source_member.org_id


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "access_removed", "reverified", "version_changed", "authority_changed"])
async def test_binding_rechecks_authority_after_discovery(test_client, db_session, test_engine, app, source_member, monkeypatch, change):
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    from src.product_onboarding.source_discovery import aws_environment

    async def changed_while_reading(connection, secrets_manager):
        resource = await aws_environment(connection, secrets_manager)
        assert resource is not None
        if change == "revoked":
            invalidate_connection(credential)
        elif change == "access_removed":
            other = User(
                org_id=source_member.org_id,
                team_id=source_member.team_id,
                email="other-member@example.test",
                user_kind="human",
            )
            db_session.add(other)
            await db_session.flush()
            credential.user_id = other.id
            credential.aws_verified_binding = connection_binding(credential)
        elif change == "reverified":
            credential.aws_verification_attempt = "replacement-attempt"
        elif change == "version_changed":
            credential.aws_verified_version_id = "replacement-version"
        else:
            credential.label = "Changed authority"
            credential.aws_verified_binding = connection_binding(credential)
        await db_session.commit()
        return resource

    monkeypatch.setattr("src.product_onboarding.source_discovery.aws_environment", changed_while_reading)
    response = await test_client.post(path, json=selection, headers=mutation(1, "i10_binding_authority_race_123"))
    assert response.status_code == (404 if change in ("revoked", "access_removed") else 409), response.text
    assert secret.account not in response.text and secret.role not in response.text
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        assert await independent.scalar(select(func.count()).select_from(ProductSourceBinding)) == 0
        assert await independent.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "createBinding")) == 0
        product = await independent.get(Product, (source_member.org_id, path.split("/")[3]))
        project = await independent.get(Project, (source_member.org_id, path.split("/")[5]))
        assert product.revision == 1 and project.configuration == {}


@pytest.mark.asyncio
async def test_secrets_access_denial_never_exposes_identity(test_client, db_session, app, source_member, monkeypatch):
    path, _, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)

    def access_denied(secret_arn):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "DescribeSecret")

    monkeypatch.setattr(secret, "current_version_id", access_denied)
    response = await test_client.get(
        path.removesuffix("/bindings") + "/sources",
        params={
            "connection_kind": "credential",
            "connection_id": credential.id,
        },
    )
    assert response.status_code == 200
    source = response.json()["items"][0]
    assert source["coverage"] == "unavailable" and "resources" not in source
    assert secret.account not in response.text and secret.role not in response.text
