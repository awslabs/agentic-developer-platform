"""Security regression checks against Terraform-rendered boundary JSON.

Fixture ARNs replace provider outputs. This is not a live IAM acceptance test.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def statements():
    if shutil.which("terraform") is None:
        pytest.skip("Terraform is needed to render policy expressions")
    result = subprocess.check_output(
        [sys.executable, str(Path(__file__).with_name("render_worker_boundary.py"))], text=True
    )
    return json.loads(result)["Statement"]


def test_worker_cannot_read_secrets_or_escalate_even_with_extra_identity_grants(statements):
    by_id = {statement["Sid"]: statement for statement in statements}
    assert by_id["DenyAllSecrets"] == {
        "Sid": "DenyAllSecrets",
        "Effect": "Deny",
        "Action": ["secretsmanager:*"],
        "Resource": "*",
    }
    permitted_actions = {
        action for s in statements if s["Effect"] == "Allow" for action in s["Action"]
    }
    assert not any(
        a.startswith(("iam:", "secretsmanager:", "eks:", "cognito-idp:", "bedrock:"))
        for a in permitted_actions
    )
    assert not (
        permitted_actions
        & {"sts:AssumeRole", "sts:AssumeRoleWithWebIdentity", "sts:GetFederationToken"}
    )
    assert by_id["DenyUnlistedActions"]["Effect"] == "Deny"
    assert set(by_id["DenyUnlistedActions"]["NotAction"]) == permitted_actions


def test_operational_resources_do_not_wildcard_other_environments_or_accounts(statements):
    allows = [s for s in statements if s["Effect"] == "Allow"]
    for statement in allows:
        resources = statement["Resource"]
        if isinstance(resources, str):
            resources = [resources]
        for resource in resources:
            if resource == "*":
                assert statement["Sid"] in {"Identity", "ProvenanceMetrics"}
            else:
                assert ":*:" not in resource
                assert "adp-*" not in resource
                assert "/adp/*/" not in resource
    metrics = next(s for s in allows if s["Sid"] == "ProvenanceMetrics")
    assert metrics["Condition"] == {"StringEquals": {"cloudwatch:namespace": "ADP/Provenance"}}


def test_all_credential_delivery_routes_are_allowed_without_admin_routes(statements):
    gateway = next(s for s in statements if s["Sid"] == "AuthenticatedGateway")
    for method, route in [
        ("POST", "github-installation-token"),
        ("POST", "credential-assume-role"),
        ("POST", "credential-raw-read"),
        ("POST", "worker-task-credentials"),
        ("POST", "proxy-request"),
        ("POST", "credential-materialize"),
        ("GET", "user-credentials"),
    ]:
        assert any(r.endswith(f"/{method}/internal/v1/{route}") for r in gateway["Resource"])
    assert not any("/admin" in resource for resource in gateway["Resource"])
    assert (
        next(s for s in statements if s["Sid"] == "DenyOtherGatewayRoutes")["NotResource"]
        == gateway["Resource"]
    )


def test_key_and_authority_data_denials_cover_resource_policy_grants(statements):
    by_id = {s["Sid"]: s for s in statements}
    assert by_id["DenyAuthorityData"]["NotResource"].endswith("/adp-dev-correlation-pointers")
    assert by_id["DenyOtherEncryptionKeys"]["NotResource"].endswith("key/dynamodb-key")
    assert by_id["DenyDirectKMS"]["Condition"] == {
        "StringNotEquals": {"kms:ViaService": "dynamodb.us-east-1.amazonaws.com"},
    }


def test_artifact_access_remains_explicitly_denied(statements):
    by_id = {s["Sid"]: s for s in statements}
    assert by_id["DenyDirectArtifacts"] == {
        "Sid": "DenyDirectArtifacts", "Effect": "Deny", "Action": ["s3:*"], "Resource": "*",
    }
    assert not any(action.startswith("s3:") for s in statements
                   if s["Effect"] == "Allow" for action in s["Action"])


def test_worker_consumes_only_its_input_queue_without_send_or_management(statements):
    by_id = {s["Sid"]: s for s in statements}
    queue = "arn:aws:sqs:us-east-1:879318057152:adp-dev-agent-submit.fifo"
    actions = {"sqs:ReceiveMessage", "sqs:ChangeMessageVisibility",
               "sqs:DeleteMessage", "sqs:GetQueueAttributes"}
    consumer = by_id["InputQueueConsumer"]
    assert consumer["Effect"] == "Allow"
    assert consumer["Resource"] == queue
    assert set(consumer["Action"]) == actions
    assert by_id["DenyOtherQueues"] == {
        "Sid": "DenyOtherQueues", "Effect": "Deny", "Action": ["sqs:*"], "NotResource": queue,
    }
    permitted_sqs = {a for s in statements if s["Effect"] == "Allow"
                     for a in s["Action"] if a.startswith("sqs:")}
    assert permitted_sqs == actions
    assert {a for a in by_id["DenyUnlistedActions"]["NotAction"]
            if a.startswith("sqs:")} == actions



def test_tool_exception_is_exact_and_preserves_existing_boundaries(statements):
    route = "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/POST/tools/cyber"
    updated = json.loads(subprocess.check_output(
        [sys.executable, str(Path(__file__).with_name("render_worker_boundary.py")), json.dumps([route])], text=True
    ))["Statement"]
    before = {s["Sid"]: s for s in statements}
    after = {s["Sid"]: s for s in updated}
    for sid in before:
        if sid in {"AuthenticatedGateway", "DenyOtherGatewayRoutes"}:
            key = "Resource" if sid == "AuthenticatedGateway" else "NotResource"
            assert after[sid][key] == before[sid][key] + [route]
            assert {k: v for k, v in after[sid].items() if k != key} == {k: v for k, v in before[sid].items() if k != key}
        else:
            assert after[sid] == before[sid]


@pytest.mark.parametrize("resource", [
    "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/*/POST/tools/cyber",
    "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/POST/internal/v1/anything",
    "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/GET/tools/cyber",
    "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/POST/tools/*",
])
def test_tool_exception_validation_refuses_broad_or_internal_routes(tmp_path, resource):
    if shutil.which("terraform") is None:
        pytest.skip("Terraform required")
    source = Path(__file__).resolve().parents[1] / "infra/task-tool-authority.tf"
    (tmp_path / "main.tf").write_text(source.read_text())
    result = subprocess.run(["terraform", "plan", "-input=false", "-lock=false", "-no-color", "-var", "task_tool_invoke_resources=" + json.dumps([resource])],
        input="var.task_tool_invoke_resources\n", cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Tool exceptions require" in result.stderr
