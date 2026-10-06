"""Actual TypeScript artifact downloads over gateway HTTP and Moto storage."""

from pathlib import Path

import pytest

from tests.agentauth.test_chat_artifact import artifacts as artifacts_fixture
from tests.agentauth.test_chat_artifact import capability as capability_fixture
from tests.agentauth.test_chat_artifact import client as client_fixture
from tests.agentauth.test_chat_artifact import create
from tests.agentauth.test_chat_artifact import runtime as runtime_fixture
from tests.agentauth.test_chat_artifact import store as store_fixture
from tests.agentauth.test_chat_artifact import sts as sts_fixture
from tests.agentauth.test_chat_memory_port_integration import gateway_http as gateway_http_fixture
from tests.agentauth.test_chat_memory_port_integration import node_tools

pytestmark = [pytest.mark.integration, pytest.mark.chat_ports]
artifacts = artifacts_fixture
capability = capability_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
gateway_http = gateway_http_fixture
DRIVER = Path(__file__).with_name("fixtures") / "chat_artifact_port.cjs"


@pytest.mark.parametrize("field,value", [("ownerUserId", "other-user"), ("tenantId", "other-tenant")])
async def test_artifact_port_distinguishes_missing_content_from_scope_denial(
    client, capability, artifacts, runtime, gateway_http, tmp_path, field, value
):
    reference = await create(client, capability)

    async def fetch(destination):
        return await node_tools(gateway_http, runtime[-1], driver=DRIVER, workspace=str(tmp_path), id=reference["id"], destination=destination)

    assert await fetch("owner.txt") == {"content": "A durable artifact"}
    row = artifacts[0].scan()["Items"][0]
    artifacts[1].delete_object(Bucket=artifacts[2], Key=row["s3Key"])
    assert await fetch("missing.txt") == {"error": "missing", "status": 404}
    assert not (tmp_path / "missing.txt").exists()

    artifacts[0].put_item(Item={**row, field: value})
    assert await fetch("denied-absent.txt") == {"error": "denied", "status": 404}
    assert not (tmp_path / "denied-absent.txt").exists()
    artifacts[1].put_object(Bucket=artifacts[2], Key=row["s3Key"], Body=b"A durable artifact", ContentType="text/plain")
    assert await fetch("denied-present.txt") == {"error": "denied", "status": 404}
    assert not (tmp_path / "denied-present.txt").exists()
