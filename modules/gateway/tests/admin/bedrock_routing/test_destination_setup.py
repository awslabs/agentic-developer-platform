"""Saved setup survives administrator handoff without rotating trust or enabling routing."""

import base64
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa

from src.shared.models.bedrock_routing import BedrockDestinationRegistry

from .conftest import ORG_ID, PERSONAL_DEST, PLATFORM_DEST, client_for, fake_secrets, platform_admin_context


@pytest.fixture
def setup_urls(monkeypatch):
    monkeypatch.setattr("src.auth.cfn_template.build_template_url", lambda *args: "https://templates.example/aws_role_v2.yaml?fresh=1")
    template = (Path(__file__).parents[3] / "src/auth/cfn_templates/aws_role_v2.yaml").read_text()
    monkeypatch.setattr("src.admin.bedrock_routing.routes.read_routing_template", lambda: template)


def test_template_download_honors_the_configured_s3_object(monkeypatch):
    from src.auth.cfn_template import read_routing_template

    monkeypatch.setenv("ADP_CFN_TEMPLATE_BUCKET", "configured-template-bucket")
    monkeypatch.setenv("ADP_CFN_TEMPLATE_KEY_V2", "custom/approved-routing.yaml")
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(b"AWSTemplateFormatVersion: '2010-09-09'\n")}
    monkeypatch.setattr("src.auth.cfn_template.boto3.client", lambda *args, **kwargs: s3)
    assert read_routing_template() == "AWSTemplateFormatVersion: '2010-09-09'\n"
    s3.get_object.assert_called_once_with(Bucket="configured-template-bucket", Key="custom/approved-routing.yaml")


async def create_pending(client, secrets):
    response = await client.post(
        "/admin/bedrock-routing/destinations",
        json={
            "source": "new_account",
            "account_id": "123456789012",
            "label": "sophos-it",
            "link_to_org_id": ORG_ID,
        },
    )
    assert response.status_code == 201, response.text
    # Model the real secret store: setup must retrieve the original generated value.
    secrets.get_secret.return_value = secrets.create_secret.call_args.args[2]
    return response.json()


async def test_download_and_resume_preserve_destination_and_external_id(session, seeded, setup_urls):
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        created = await create_pending(client, secrets)
        destination_id = created["destination"]["id"]
        count_before = await session.scalar(sa.select(sa.func.count()).select_from(BedrockDestinationRegistry))
        first = await client.get(f"/admin/bedrock-routing/destinations/{destination_id}/setup")
        second = await client.get(f"/admin/bedrock-routing/destinations/{destination_id}/setup")
        listed = await client.get("/admin/bedrock-routing/destinations")

    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    details = second.json()
    assert details["account_id"] == "123456789012"
    assert details["role_arn"] == "arn:aws:iam::123456789012:role/ADP-Agent-sophos-it"
    original_query = parse_qs(urlsplit(created["launch_url"]).fragment.split("?", 1)[1])
    query = parse_qs(urlsplit(details["launch_url"]).fragment.split("?", 1)[1])
    assert original_query == query
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(details["download_base64"]))) as bundle:
        assert set(bundle.namelist()) == {"template.yaml", "parameters.json", "README.md"}
        template = bundle.read("template.yaml").decode()
        assert template == (Path(__file__).parents[3] / "src/auth/cfn_templates/aws_role_v2.yaml").read_text()
        assert "project/default" in template  # OpenAI, alongside Claude's model/profile grants.
        params = {item["ParameterKey"]: item["ParameterValue"] for item in json.loads(bundle.read("parameters.json"))}
        assert params == {key.removeprefix("param_"): value[0] for key, value in query.items() if key.startswith("param_")}
        assert params["ExternalId"] == json.loads(secrets.get_secret.return_value)["external_id"]
        assert "UserSessionTag" not in params
        assert "CAPABILITY_NAMED_IAM" in bundle.read("README.md").decode()
    assert await session.scalar(sa.select(sa.func.count()).select_from(BedrockDestinationRegistry)) == count_before
    secrets.create_secret.assert_called_once()
    row = next(row for row in listed.json() if row["id"] == destination_id)
    assert not row["usable_for_routing"]
    assert row["verified_at"] is None
    assert params["ExternalId"] not in listed.text
    assert "download_base64" not in listed.text


async def test_resume_then_verify_uses_the_saved_role_and_probe(session, seeded, setup_urls, probe_ok):
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        created = await create_pending(client, secrets)
        destination_id = created["destination"]["id"]
        await client.get(f"/admin/bedrock-routing/destinations/{destination_id}/setup")
        probe_ok.assert_not_awaited()
        result = await client.post(f"/admin/bedrock-routing/destinations/{destination_id}/verify")
    assert result.json()["verified"] is True
    assert result.json()["destination"]["usable_for_routing"] is True
    probe_ok.assert_awaited_once()
    assert "arn:aws:iam::123456789012:role/ADP-Agent-sophos-it" in str(probe_ok.call_args)


async def test_unreadable_secret_cannot_generate_a_different_trust_policy(session, seeded, setup_urls):
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        created = await create_pending(client, secrets)
        secrets.get_secret.return_value = "{}"
        result = await client.get(f"/admin/bedrock-routing/destinations/{created['destination']['id']}/setup")
    assert result.status_code == 409
    assert "download_base64" not in result.text
    secrets.create_secret.assert_called_once()


@pytest.mark.parametrize("destination_id, status", [("missing", 404), (PERSONAL_DEST, 409), (PLATFORM_DEST, 409)])
async def test_setup_does_not_repurpose_existing_personal_connections(session, seeded, destination_id, status):
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        result = await client.get(f"/admin/bedrock-routing/destinations/{destination_id}/setup")
    assert result.status_code == status
    secrets.get_secret.assert_not_called()


@pytest.mark.parametrize("changes", [{"label": "bad name"}, {"label": "a" * 55}, {"region": "us-east-1;echo bad"}])
async def test_invalid_cloudformation_inputs_are_rejected_before_creating_secrets(session, seeded, changes):
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        result = await client.post(
            "/admin/bedrock-routing/destinations",
            json={
                "source": "new_account",
                "account_id": "123456789012",
                "label": "sophos-it",
                "link_to_org_id": ORG_ID,
                **changes,
            },
        )
    assert result.status_code == 422
    secrets.create_secret.assert_not_called()
