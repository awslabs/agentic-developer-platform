"""Offline failure/recovery tests; synthetic results are never live evidence."""
import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registration_workflow as workflow
from registration_delivery import check_reviewed_instance_plan, expected_observations


def sample():
    account, region, environment = "123456789012", "us-west-2", "staging"
    secret = f"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}"
    operation = lambda index: f"00000000-0000-4000-8000-{index:012}"
    config = {
        "schema_version": 1, "account_id": account, "region": region, "environment": environment,
        "source_revision": "a" * 40, "correlation": "synthetic-accepted-operation",
        "registrar_role_arn": f"arn:aws:iam::{account}:role/registrar",
        "exporter_role_arn": f"arn:aws:iam::{account}:role/exporter", "instance_id": "i-1234567890abcdef0",
        "instance_url": "https://portal.example.invalid/gitlab",
        "targets": {"tenant_parent": 12, "product_subgroup": 24, "project": 7, "qualification_parent": 60,
                    "approved_root_sha256": "d" * 64, "reviewed_plan_sha256": "e" * 64},
        "backend": {"bucket": f"adp-terraform-state-{account}", "region": region, "encrypt": True,
                    "key": "staging/modules/gitlab/terraform.tfstate", "dynamodb_table": "adp-terraform-locks"},
        "artifacts": {name: {"bucket": f"adp-staging-deployment-evidence-{account}", "key": f"gitlab-registration/inputs/review/{name}",
                             "version_id": "synthetic-version", "sha256": "d" * 64 if name == "approved_root" else "a" * 64}
                      for name in ("tfplan", "variables", "approved_root")},
        "product": {"tenant_id": "tenant-a", "group_id": 12, "product_group_id": 24, "project_id": 7,
                    "product_id": "product-a", "tag_list": ["adp-group-12"], "access_level": "ref_protected",
                    "operation_id": operation(1), "token_secret_arn": f"{secret}/gitlab-runner/12-abcdef"},
        "qualification": {"tenant_id": "tenant-b", "group_id": 60, "tag_list": ["adp-group-60"],
                          "access_level": "not_protected", "operation_id": operation(2),
                          "token_secret_arn": f"{secret}/gitlab-runner/60-abcdef"},
        "probe_operation_id": operation(3), "admin_secret_arn": f"{secret}/gitlab-admin-token-abcdef", "admin_user_id": 4,
        "ca": {"certificate_arn": f"arn:aws:acm:{region}:{account}:certificate/{operation(4)}",
               "runner_url": "https://gitlab.internal.example.invalid/gitlab", "ca_version": operation(5),
               "ca_secret_arn": f"{secret}/gitlab/ca-chain-abcdef"}, "secret_policy_sha256": {}, "instance_change_approval": None,
    }
    config["secret_policy_sha256"] = {arn: "f" * 64 for arn in (config["admin_secret_arn"], config["product"]["token_secret_arn"], config["qualification"]["token_secret_arn"])}
    env = {"ACCOUNT_ID": account, "AWS_REGION": region, "ENVIRONMENT": environment, "GITHUB_SHA": "b" * 40, "ADP_SOURCE_REVISION": "a" * 40,
           "ADP_CORRELATION": config["correlation"], "ADP_DEPLOY_ROLE_ARN": config["registrar_role_arn"],
           "GITHUB_RUN_ID": "987", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REPOSITORY_ID": "123"}
    return config, env


def test_protected_configuration_is_portable_and_does_not_imply_instance_authority():
    config, env = sample()
    assert workflow.validate_config(config, env) == config
    assert workflow.instance_gate(config) is False


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(account_id="999999999999"), lambda c: c.update(source_revision="b" * 40),
    lambda c: c.update(correlation="unaccepted"), lambda c: c["backend"].update(key="another/state"),
    lambda c: c["backend"].update(encrypt=False), lambda c: c["product"].update(group_id=60),
    lambda c: c["qualification"].update(tenant_id="tenant-a"), lambda c: c.update(probe_operation_id=c["product"]["operation_id"]),
    lambda c: c["artifacts"]["tfplan"].update(version_id="null"), lambda c: c["artifacts"]["tfplan"].update(key="gitlab-registration/inputs/../unreviewed"),
    lambda c: c.update(exporter_role_arn=c["registrar_role_arn"]), lambda c: c["ca"].update(runner_url="http://example.invalid"),
    lambda c: c["product"].update(tag_list=["adp-group-12", "adp-group-60"]), lambda c: c["secret_policy_sha256"].clear(),
    lambda c: c["artifacts"]["approved_root"].update(sha256="f" * 64),
])
def test_wrong_scope_or_artifacts_fail_before_provider_calls(mutation):
    config, env = sample()
    mutation(config)
    with pytest.raises(ValueError): workflow.validate_config(config, env)


def test_instance_plan_supports_compressed_userdata_but_refuses_other_unknown_changes():
    config, _ = sample()
    before = {"id": config["instance_id"], "user_data": "old", "user_data_base64": None, "ami": "ami-old"}
    plan = {"resource_changes": [{"mode": "managed", "type": "aws_instance", "change": {
        "actions": ["update"], "before": before, "after": {**before, "user_data": None, "user_data_base64": "new"},
        "after_unknown": {"user_data": True}}}]}
    config["targets"]["reviewed_plan_sha256"] = workflow.digest(plan)
    with pytest.raises(ValueError): check_reviewed_instance_plan(plan, config["targets"], config["instance_id"])
    assert check_reviewed_instance_plan(plan, config["targets"], config["instance_id"], instance_gate=True)
    plan["resource_changes"][0]["change"]["after_unknown"]["ami"] = True
    config["targets"]["reviewed_plan_sha256"] = workflow.digest(plan)
    with pytest.raises(ValueError): check_reviewed_instance_plan(plan, config["targets"], config["instance_id"], instance_gate=True)
    config["instance_change_approval"] = {"account_id": config["account_id"], "instance_id": config["instance_id"],
        "source_revision": config["source_revision"], "plan_sha256": "b" * 64, "approval_ref": "private-approval"}
    with pytest.raises(ValueError): workflow.instance_gate(config)


@pytest.mark.parametrize("mismatch", ["version", "body", "size"])
def test_versioned_input_readback_refuses_substitution(mismatch):
    config, _ = sample()
    artifact = config["artifacts"]["tfplan"]
    import hashlib
    artifact["sha256"] = hashlib.sha256(b"reviewed").hexdigest()
    response = {"VersionId": artifact["version_id"], "ContentLength": 8, "Body": io.BytesIO(b"reviewed")}
    if mismatch == "version": response["VersionId"] = "other"
    if mismatch == "body": response["Body"] = io.BytesIO(b"tampered")
    if mismatch == "size": response["ContentLength"] = workflow.MAX_OBJECT + 1
    with pytest.raises(ValueError): workflow.download(SimpleNamespace(get_object=lambda **kwargs: response), artifact, config["account_id"])


@pytest.mark.parametrize("bad", [None, "missing", "oversize", "different_existing", "link"])
def test_provider_lock_comes_from_reviewed_plan_not_latest_provider_selection(tmp_path, bad):
    import zipfile
    with zipfile.ZipFile(tmp_path / "tfplan", "w") as archive:
        archive.writestr("different" if bad == "missing" else ".terraform.lock.hcl", b"x" * 65537 if bad == "oversize" else b"reviewed-lock")
    infra = tmp_path / "infra"; infra.mkdir()
    if bad == "different_existing": (infra / ".terraform.lock.hcl").write_bytes(b"other-lock")
    if bad == "link": (infra / ".terraform.lock.hcl").symlink_to(tmp_path / "absent")
    if bad:
        with pytest.raises(ValueError): workflow.restore_plan_lock(tmp_path, infra)
    else:
        workflow.restore_plan_lock(tmp_path, infra)
        assert (infra / ".terraform.lock.hcl").read_bytes() == b"reviewed-lock"
        workflow.restore_plan_lock(tmp_path, infra)


def test_apply_rechecks_exact_plan_and_refuses_drift_without_receipt(tmp_path, monkeypatch):
    config, _ = sample()
    calls = []
    monkeypatch.setattr(workflow, "guard", lambda *args: calls.append("guard"))
    def terraform(directory, *args, **kwargs):
        calls.append(args)
        if args[0] == "plan": return SimpleNamespace(returncode=2, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=b'{"resource_changes":[]}')
    monkeypatch.setattr(workflow, "terraform", terraform)
    with pytest.raises(ValueError, match="post_apply_drift"): workflow.apply_plan(config, tmp_path)
    assert calls[0] == "guard" and calls[1][0] == "apply" and calls[2][0] == "plan"
    assert not (tmp_path / "apply.json").exists()
    assert not any("-target" in str(call) or "-refresh-only" in str(call) for call in calls)


def test_policy_digest_verifies_real_resource_document_not_policy_presence():
    config, _ = sample()
    policy = {"Statement": [{"Effect": "Deny", "Action": "secretsmanager:GetSecretValue", "Resource": "*"}]}
    config["secret_policy_sha256"] = {config["admin_secret_arn"]: workflow.digest(policy)}
    secrets = Mock()
    secrets.get_resource_policy.return_value = {"ARN": config["admin_secret_arn"], "ResourcePolicy": json.dumps(policy)}
    workflow.verify_secret_policies(secrets, config)
    policy["Statement"][0]["Effect"] = "Allow"
    secrets.get_resource_policy.return_value["ResourcePolicy"] = json.dumps(policy)
    with pytest.raises(ValueError): workflow.verify_secret_policies(secrets, config)


class ProbeAdmin:
    """Stateful API double catches broad deletion and lost create responses."""
    owner_id = 7
    def __init__(self, *, lose_create=False):
        self.rows = {8: {"id": 8, "description": "permanent runner"}}
        self.deleted = []
        self.lose_create = lose_create

    async def request(self, method, endpoint, **kwargs):
        if endpoint == "groups/42":
            import hashlib
            key = hashlib.sha256(b"synthetic-tenant").hexdigest()[:24]
            return {"id": 42, "path": f"tenant-{key}", "full_path": f"tenant-{key}", "description": f"ADP tenant {key}", "parent_id": None}
        if method == "POST" and endpoint == "user/runners":
            self.rows[9] = {**kwargs["json"], "id": 9}
            if self.lose_create: raise TimeoutError("synthetic lost response")
            return {"id": 9, "token": "glrt-synthetic-secret"}
        runner_id = int(endpoint.split("/")[1])
        if method == "DELETE":
            assert runner_id != 8
            self.deleted.append(runner_id)
            self.rows.pop(runner_id, None)
            return None
        assert method == "GET"
        return self.rows.get(runner_id)

    async def pages(self, endpoint, **kwargs):
        if endpoint.endswith("members") or endpoint.endswith("members/all"):
            return [{"id": 7, "access_level": 50}]
        if endpoint == "runners/all":
            return [row for row in self.rows.values() if row["description"] == kwargs["search"]]
        assert endpoint == "groups/42/runners"
        return list(self.rows.values())


@pytest.mark.parametrize("failure", [None, "baseline", "after", "transport", "lost_create"])
def test_revocation_cleans_only_owned_probe_and_never_group_token_store(failure):
    registrar = workflow.registrar_module()
    admin = ProbeAdmin(lose_create=failure == "lost_create")
    store = Mock()
    probe = registrar.RunnerLifecycle(admin, store, "synthetic-tenant", 42, ["adp-group-42"], "ref_protected")
    seen = []
    async def verify_token(token, runner_id, operation_id):
        assert token == "glrt-synthetic-secret" and runner_id == 9
        seen.append(runner_id in admin.rows)
        if failure == "transport": raise httpx.ConnectError("synthetic transport failure")
        return "rejected" if failure == "baseline" else "accepted" if failure == "after" else "accepted" if runner_id in admin.rows else "rejected"
    call = probe.probe_revocation("00000000-0000-4000-8000-000000000003", verify_token)
    if failure:
        with pytest.raises((registrar.RegistrationError, TimeoutError, httpx.ConnectError)): asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert result["token_before"] == "accepted" and result["token_after"] == "rejected"
        assert seen == [True, False]
    assert set(admin.rows) == {8} and admin.deleted == [9]
    assert not store.mock_calls


@pytest.mark.parametrize("status,expected", [(200, "accepted"), (403, "rejected"), (401, None), (404, None), (302, None), (500, None)])
def test_token_rejection_requires_real_authentication_response(status, expected):
    async def run():
        def handle(request):
            assert request.url.path == "/gitlab/api/v4/runners/verify"
            assert "private-token" not in request.headers
            assert json.loads(request.content)["system_id"].startswith("s_")
            return httpx.Response(status, json={"id": 9})
        async with httpx.AsyncClient(base_url="https://example.invalid/gitlab/api/v4/", transport=httpx.MockTransport(handle)) as client:
            return await workflow.token_verifier(client, "glrt-synthetic", 9, "synthetic-operation")
    if expected: assert asyncio.run(run()) == expected
    else:
        with pytest.raises(ValueError): asyncio.run(run())


class EvidenceStore:
    def __init__(self): self.objects = {}
    def get_bucket_versioning(self, **kwargs): return {"Status": "Enabled"}
    def get_public_access_block(self, **kwargs):
        return {"PublicAccessBlockConfiguration": dict.fromkeys(("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"), True)}
    def put_object(self, **kwargs):
        assert kwargs["IfNoneMatch"] == "*" and kwargs["ServerSideEncryption"] == "AES256"
        assert kwargs["Key"] not in self.objects
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"VersionId": "synthetic-immutable-version"}
    def get_object(self, **kwargs):
        assert kwargs["VersionId"] == "synthetic-immutable-version"
        return {"VersionId": kwargs["VersionId"], "Body": io.BytesIO(self.objects[kwargs["Key"]])}


def test_six_separate_versioned_readbacks_bind_run_attempt_and_exact_observations():
    config, env = sample()
    store = EvidenceStore()
    receipt = workflow.publish_readbacks(store, config, env, expected_observations(config["targets"]), {"probe_runner_id": 9})
    assert len(store.objects) == len(receipt["checks"]) == 6
    for name, row in receipt["checks"].items():
        assert f"/runs/987/attempts/2/{name}.json?versionId=" in row["evidence_ref"]
    for body in store.objects.values():
        assert json.loads(body)["source_revision"] == "a" * 40
        assert b"glrt-" not in body


def test_partial_observations_cannot_publish_success():
    config, env = sample()
    store = EvidenceStore()
    observed = expected_observations(config["targets"])
    del observed["runner-revocation"]
    with pytest.raises(ValueError): workflow.publish_readbacks(store, config, env, observed, {})
    assert not store.objects


@pytest.mark.parametrize("shared_runners", [True, False])
def test_collector_stages_both_groups_retries_without_duplicates_and_revokes_probe(tmp_path, monkeypatch, shared_runners):
    """Use the real registrar/store/admin adapter against stateful fake providers."""
    config, _ = sample()
    import hashlib
    import ssl
    from botocore.exceptions import ClientError

    observed_ca = expected_observations(config["targets"])["ca-root-verified"]
    monkeypatch.setattr(workflow, "observe_ca_root", lambda *args, **kwargs: observed_ca)
    monkeypatch.setattr(workflow, "verify_secret_policies", lambda *args: None)
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: True)
    workflow.write_private(tmp_path / "apply.json", workflow.canonical(expected_observations(config["targets"])["instance-no-unreviewed-change"]))

    class Secrets:
        def __init__(self): self.versions = {}
        def get_secret_value(self, SecretId, **kwargs):
            if SecretId == config["admin_secret_arn"]: return {"SecretString": "glpat-synthetic-admin"}
            key = SecretId, kwargs.get("VersionId")
            if key not in self.versions:
                raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")
            return {"SecretString": self.versions[key], "VersionId": key[1]}
        def put_secret_value(self, SecretId, ClientRequestToken, SecretString, VersionStages):
            assert VersionStages == ["AWSPENDING"]
            self.versions[SecretId, ClientRequestToken] = SecretString

    class Ssm:
        def __init__(self): self.records = {}
        def get_parameter(self, Name):
            return {"Parameter": {"Value": config["ca"]["runner_url"] if Name.endswith("/runner-url") else
                                  config["instance_url"] if Name.endswith("/url") else self.records.get(Name, "{}")}}
        def put_parameter(self, Name, Value, Type, Overwrite): self.records[Name] = Value

    secrets, ssm, rows, deleted = Secrets(), Ssm(), {}, []
    shared, project_writes = [shared_runners], []
    groups = {item["group_id"]: item for item in (config["product"], config["qualification"])}
    keys = {group: hashlib.sha256(item["tenant_id"].encode()).hexdigest()[:24] for group, item in groups.items()}
    def handle(request):
        endpoint = request.url.path.split("/api/v4/")[1]
        method = request.method
        data = json.loads(request.content) if request.content else {}
        if endpoint == "user": return httpx.Response(200, json={"id": 4, "is_admin": True})
        if endpoint == "user/runners":
            runner_id = max([100] + list(rows)) + 1
            rows[runner_id] = {**data, "id": runner_id}
            return httpx.Response(201, json={"id": runner_id, "token": f"glrt-synthetic-{runner_id}"})
        if endpoint == "runners/verify":
            assert "PRIVATE-TOKEN" not in request.headers
            runner_id = int(data["token"].split("-")[-1])
            return httpx.Response(200 if runner_id in rows else 403, json={"id": runner_id})
        if endpoint == "runners/all":
            return httpx.Response(200, json=[row for row in rows.values() if row["description"] == request.url.params["search"]])
        if endpoint.startswith("runners/"):
            runner_id = int(endpoint.split("/")[1])
            if method == "DELETE":
                deleted.append(runner_id); rows.pop(runner_id, None)
                return httpx.Response(204)
            return httpx.Response(200, json=rows[runner_id]) if runner_id in rows else httpx.Response(404)
        if endpoint.startswith("groups/"):
            group_id = int(endpoint.split("/")[1])
            if endpoint.endswith("members") or endpoint.endswith("members/all"):
                return httpx.Response(200, json=[{"id": 4, "access_level": 50}])
            if endpoint.endswith("runners"):
                return httpx.Response(200, json=[row for row in rows.values() if row["group_id"] == group_id])
            if group_id == 24:
                return httpx.Response(200, json={"id": 24, "parent_id": 12, "path": "product-product-a",
                    "full_path": f"tenant-{keys[12]}/product-product-a", "description": "ADP product product-a"})
            return httpx.Response(200, json={"id": group_id, "parent_id": None, "path": f"tenant-{keys[group_id]}",
                "full_path": f"tenant-{keys[group_id]}", "description": f"ADP tenant {keys[group_id]}"})
        assert endpoint == "projects/7"
        if method == "PUT":
            assert data == {"shared_runners_enabled": False}
            project_writes.append(data)
            shared[0] = False
        return httpx.Response(200, json={"id": 7, "namespace": {"id": 24, "kind": "group"}, "path": "demo",
            "path_with_namespace": f"tenant-{keys[12]}/product-product-a/demo", "visibility": "private", "shared_runners_enabled": shared[0]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(**{**kwargs, "transport": httpx.MockTransport(handle)}))
    session = SimpleNamespace(client=lambda service: {"secretsmanager": secrets, "ssm": ssm}.get(service))
    asyncio.run(workflow.preflight(config, tmp_path, session, session))
    assert not rows and not secrets.versions and not project_writes
    observations, details = asyncio.run(workflow.collect(config, tmp_path, session, session))
    assert len(project_writes) == int(shared_runners)
    assert shared[0] is False
    assert observations == expected_observations(config["targets"])
    assert len(rows) == len(secrets.versions) == 2 and len(deleted) == 1
    assert all(row["paused"] is True for row in rows.values())
    for raw in secrets.versions.values():
        value = json.loads(raw)
        assert value["instance_url"] == config["instance_url"] and value["runner_url"] == config["ca"]["runner_url"]
    first_ids = details["runner_ids"]
    observations, details = asyncio.run(workflow.collect(config, tmp_path, session, session))
    assert details["runner_ids"] == first_ids and len(rows) == len(secrets.versions) == 2
    assert len(deleted) == 2
    # Drift in a staged runner's access policy is rejected on retry, before a
    # new revocation probe can be created.
    rows[first_ids["product"]]["access_level"] = "not_protected"
    with pytest.raises(Exception, match="runner_scope_not_verified"):
        asyncio.run(workflow.collect(config, tmp_path, session, session))
    assert len(deleted) == 2


def test_workflow_wires_protected_config_saved_plan_live_collection_and_publisher():
    root = Path(__file__).resolve().parents[5]
    path = root / ".github/workflows/gitlab-infra-apply.yml"
    doc = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    steps = doc["jobs"]["apply"]["steps"]
    commands = [(i, step["run"]) for i, step in enumerate(steps) if "registration_workflow.py" in step.get("run", "")]
    assert [command.split()[-1] for _, command in commands] == ["prepare", "guard", "apply", "collect"]
    for index, _ in commands:
        assert steps[index]["env"]["ADP_REGISTRATION_CONFIG_JSON"] == "${{ vars.GITLAB_REGISTRATION_CONFIG_JSON }}"
        assert not steps[index].get("continue-on-error") and not steps[index].get("if")
    publisher = next(i for i, step in enumerate(steps) if "publish-deployment-evidence.py delivery" in step.get("run", ""))
    assert commands[-1][0] < publisher
    assert steps[-1]["if"] == "always()" and "rm -rf" in steps[-1]["run"]
    assert "terraform apply" not in path.read_text()


def test_project_isolation_requires_real_post_write_readback(monkeypatch):
    config, _ = sample()
    calls = []
    async def project(*args):
        return {"shared_runners_enabled": True}
    async def tenant(*args):
        return None
    monkeypatch.setattr(workflow, 'registrar_module', lambda: SimpleNamespace(
        verify_product_scope=project, verify_tenant_group=tenant))
    class Admin:
        async def request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            return {"shared_runners_enabled": False}
    with pytest.raises(ValueError, match='shared_project_runners_enabled'):
        asyncio.run(workflow.verify_project_bindings(Admin(), config, configure=True))
    assert calls == [('PUT', 'projects/7', {'json': {'shared_runners_enabled': False}})]


@pytest.mark.parametrize('during', ['stopped', 'stopping', 'running'])
def test_failed_apply_restores_only_the_same_previously_running_instance(tmp_path, monkeypatch, during):
    config, _ = sample(); client = Mock()
    states = ['running', during] + (['stopped'] if during == 'stopping' else []) + ['running']
    client.describe_instances.side_effect = [{'Reservations': [{'Instances': [{'InstanceId': config['instance_id'], 'State': {'Name': state}}]}]} for state in states]
    session = SimpleNamespace(client=lambda name: client)
    monkeypatch.setattr(workflow, 'guard', lambda *args: None)
    monkeypatch.setattr(workflow, 'apply_plan', Mock(side_effect=ValueError('original_apply_failure')))
    with pytest.raises(ValueError, match='original_apply_failure'):
        workflow.apply(config, tmp_path, session)
    if during == 'running':client.start_instances.assert_not_called()
    else:client.start_instances.assert_called_once_with(InstanceIds=[config['instance_id']])
    client.modify_instance_attribute.assert_not_called()
    client.terminate_instances.assert_not_called()


def test_unexpected_initial_stopped_state_never_applies_or_starts(tmp_path, monkeypatch):
    config, _ = sample(); client = Mock()
    client.describe_instances.return_value = {'Reservations': [{'Instances': [{'InstanceId': config['instance_id'], 'State': {'Name': 'stopped'}}]}]}
    monkeypatch.setattr(workflow, 'guard', lambda *args: None)
    apply = Mock(); monkeypatch.setattr(workflow, 'apply_plan', apply)
    with pytest.raises(ValueError, match='reviewed_instance_not_running'):
        workflow.apply(config, tmp_path, SimpleNamespace(client=lambda name: client))
    apply.assert_not_called(); client.start_instances.assert_not_called()


def test_full_provider_failure_is_private_and_persisted_without_console_output(tmp_path, monkeypatch, capsys):
    import base64
    output = b'complete provider stdout glrt-secret-canary'
    diagnostic = b'Encoded authorization failure message: ' + b'x' * 4096
    monkeypatch.setattr(workflow.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=1, stdout=output, stderr=diagnostic)))
    with pytest.raises(ValueError, match='registration_terraform_operation_failed'):
        workflow.terraform(tmp_path, 'apply', 'reviewed-plan')
    path = tmp_path / 'terraform-failure.json'; saved = json.loads(path.read_bytes())
    assert path.stat().st_mode & 0o777 == 0o600
    assert base64.b64decode(saved['stdout_base64']) == output and base64.b64decode(saved['stderr_base64']) == diagnostic
    assert not capsys.readouterr().out and not capsys.readouterr().err
    config, _ = sample(); s3 = Mock(); s3.put_object.return_value = {'VersionId': 'retained-version'}
    workflow.retain_failure(config, tmp_path, SimpleNamespace(client=lambda name: s3),
                            {'GITHUB_REPOSITORY_ID': '123', 'GITHUB_RUN_ID': '456', 'GITHUB_RUN_ATTEMPT': '1'})
    args = s3.put_object.call_args.kwargs
    assert args['Key'] == 'gitlab-registration-diagnostics/repos/123/runs/456/attempts/1/terraform-failure.json'
    assert args['Body'] == path.read_bytes() and args['ServerSideEncryption'] == 'AES256' and args['IfNoneMatch'] == '*'
