"""Protected dispatch and real manager evidence are required before unpausing."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import manager_integration as integration

OPERATION = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    config = {"account_id": "123456789012", "expected_registrar_role_arn": "arn:aws:iam::123456789012:role/registrar",
              "region": "us-east-1", "source_revision": "a" * 40, "group_id": 42,
              "operation_id": OPERATION, "evidence_path": str(tmp_path / "operation.json"),
              "adp_correlation": "accepted-attempt", "adp_definition_revision": "a" * 40,
              "adp_contract_digest": "b" * 64, "approved_plan_sha256": "c" * 64}
    now = datetime.now(timezone.utc)
    clients = {name: Mock() for name in ("sts", "ssm", "ec2", "autoscaling")}
    clients["sts"].get_caller_identity.return_value = {"Account": config["account_id"],
        "Arn": "arn:aws:sts::123456789012:assumed-role/registrar/test"}
    clients["autoscaling"].describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [{
        "AutoScalingGroupName": "adp-dev-gitlab-job-42", "Instances": [], "DesiredCapacity": 0,
        "Tags": [{"Key": key, "Value": value} for key, value in
                 (("Component", "gitlab-job"), ("Environment", "dev"), ("GroupId", "42"))]}]}
    clients["ec2"].describe_instances.return_value = {"Reservations": []}
    clients["ssm"].get_parameter.side_effect = lambda **kwargs: {"Parameter": {
        "LastModifiedDate": datetime.now(timezone.utc),
        "Value": "active" if kwargs["Name"].endswith("manager-alive") else json.dumps({"runner_id": 101, "system_id": "r_" + "a" * 12})}}
    admin = AsyncMock()
    admin.request.return_value = {"id": 101, "paused": True, "runner_type": "group_type", "locked": True,
                                  "run_untagged": False, "tag_list": ["adp-group-42"]}
    managers = [{"status": "online", "system_id": "r_" + "a" * 12, "version": "19.5.2",
                 "platform": "linux", "architecture": "amd64", "contacted_at": now.isoformat()}]
    original_runner = admin.request.return_value
    admin.request.side_effect = lambda method, path, **kwargs: managers if path.endswith("/managers") else original_runner
    admin.pages.side_effect = lambda path, **kwargs: ([{"id": 101}] if path.startswith("groups/") else
                                                    managers if path.endswith("/managers") else [])
    state = {"run": None, "posts": 0, "ambiguous": False}

    def gh(method, path, payload=None):
        if path.endswith("git/ref/heads/main"):
            return {"object": {"sha": "a" * 40}}
        if method == "POST":
            state["posts"] += 1
            assert payload["inputs"]["activation_operation_id"] == OPERATION
            assert payload["inputs"]["activation_group_id"] == "42"
            assert payload["inputs"]["approved_plan_sha256"] == config["approved_plan_sha256"]
            assert payload["inputs"]["adp_correlation"] == config["adp_correlation"]
            assert payload["inputs"]["adp_source_revision"] == config["source_revision"]
            assert payload["inputs"]["adp_definition_revision"] == config["adp_definition_revision"]
            assert payload["inputs"]["adp_contract_digest"] == config["adp_contract_digest"]
            state["run"] = {"id": 77, "head_sha": "a" * 40, "head_branch": "main", "event": "workflow_dispatch",
                            "display_title": f"ADP deployment {config['adp_correlation']}", "status": "completed", "conclusion": "success",
                            "created_at": datetime.now(timezone.utc).isoformat(), "run_started_at": datetime.now(timezone.utc).isoformat()}
            if state["ambiguous"]:
                raise integration.IntegrationError("lost response")
            return None
        if path.endswith("/runs/77"):
            return copy.deepcopy(state["run"])
        return {"workflow_runs": [copy.deepcopy(state["run"])] if state["run"] else []}

    sdk = SimpleNamespace(client=lambda name: clients[name])
    adapter = integration.ManagerIntegration(config, sdk, admin, group_id=42, operation_id=OPERATION,
                                             region="us-east-1", runner_version="19.5.2", github=gh)
    yield SimpleNamespace(adapter=adapter, config=config, clients=clients, admin=admin, state=state, managers=managers)
    adapter.close()


def test_success_requires_a_matching_workflow_and_manager_not_only_dispatch(setup):
    asyncio.run(setup.adapter.switch(101))
    assert asyncio.run(setup.adapter.manager_uses(101)) is True
    receipt = json.loads(Path(setup.config["evidence_path"]).read_text())
    assert receipt["workflow_run_id"] == 77 and receipt["verified_manager"]["runner_id"] == 101
    assert setup.state["posts"] == 1
    assert all(call.args[0] != "PUT" for call in setup.admin.request.call_args_list)


def test_lost_dispatch_response_reconciles_without_a_second_submission(setup):
    setup.state["ambiguous"] = True
    with pytest.raises(integration.IntegrationError):
        asyncio.run(setup.adapter.switch(101))
    assert json.loads(Path(setup.config["evidence_path"]).read_text())["status"] == "dispatch_intent"
    asyncio.run(setup.adapter.switch(101))
    assert setup.state["posts"] == 1


@pytest.mark.parametrize("change", [{"conclusion": "failure"}, {"head_sha": "b" * 40}, {"event": "pull_request"}])
def test_failed_or_different_source_workflow_never_qualifies(setup, change):
    asyncio.run(setup.adapter.switch(101))
    setup.state["run"].update(change)
    with pytest.raises(integration.IntegrationError):
        asyncio.run(setup.adapter.switch(101))
    assert setup.state["posts"] == 1


@pytest.mark.parametrize("change", [{"system_id": "r_" + "b" * 12}, {"version": "19.4.0"}, {"status": "offline"},
                                  {"contacted_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}])
def test_wrong_stale_or_unqualified_gitlab_manager_never_qualifies(setup, change):
    asyncio.run(setup.adapter.switch(101))
    setup.managers[0].update(change)
    with pytest.raises(integration.IntegrationError):
        asyncio.run(setup.adapter.manager_uses(101))


def test_drain_requires_zero_jobs_attached_and_detached_capacity(setup):
    assert asyncio.run(setup.adapter.drained(100)) is True
    setup.clients["ec2"].describe_instances.return_value = {"Reservations": [{"Instances": [{"InstanceId": "i-00000000"}]}]}
    times = iter([0, 1, 241])
    setup.adapter.clock = lambda: next(times)
    setup.adapter.pause = AsyncMock()
    assert asyncio.run(setup.adapter.drained(100)) is False
    setup.clients["ec2"].terminate_instances.assert_not_called()


def test_private_operation_lock_prevents_concurrent_qualifiers(setup):
    with pytest.raises(integration.IntegrationError, match="already active"):
        integration.ManagerIntegration(setup.config, setup.adapter.sdk, setup.admin, group_id=42,
                                       operation_id=OPERATION, region="us-east-1", runner_version="19.5.2")
