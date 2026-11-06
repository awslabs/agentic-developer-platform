"""Fail-closed tenant group runner registration checks with fake GitLab responses."""

import asyncio
import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


spec = importlib.util.spec_from_file_location("register_runner", Path(__file__).parents[1] / "register-runner.py")
register_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(register_runner)

TENANT = "synthetic-tenant"
GROUP_ID = 42
KEY = hashlib.sha256(TENANT.encode()).hexdigest()[:24]
OPERATION = "00000000-0000-4000-8000-000000000001"
TAG = "adp-group-42"


def admin_fixture(*, group_changes=None, detail_changes=None, members=None):
    group = {
        "id": GROUP_ID, "path": f"tenant-{KEY}", "full_path": f"tenant-{KEY}",
        "description": f"ADP tenant {KEY}", "parent_id": None,
        "shared_with_groups": [],
    }
    group.update(group_changes or {})
    detail = {
        "id": 9, "runner_type": "group_type", "run_untagged": False,
        "locked": True, "paused": True, "tag_list": [TAG],
        "access_level": "ref_protected", "description": f"ADP runner {OPERATION}",
    }
    detail.update(detail_changes or {})
    admin = AsyncMock()
    admin.owner_id = 7
    admin.request.side_effect = [group, {"id": 9, "token": "glrt-synthetic-runner-token"}, detail]
    admin.pages.side_effect = [
        members if members is not None else [{"id": 7, "access_level": 50}],
        members if members is not None else [{"id": 7, "access_level": 50}],
        [{"id": 9}],
    ]
    return admin


def create(admin):
    return asyncio.run(register_runner.create_paused_runner(
        admin, TENANT, GROUP_ID, [TAG], "ref_protected", OPERATION
    ))


def test_group_runner_created_only_after_group_and_policy_readback():
    admin = admin_fixture()
    assert create(admin)["id"] == 9
    assert admin.request.call_args_list[1].args == ("POST", "user/runners")
    payload = admin.request.call_args_list[1].kwargs["json"]
    assert payload == {
        "runner_type": "group_type", "group_id": GROUP_ID, "run_untagged": False,
        "locked": True, "paused": True, "tag_list": [TAG],
        "access_level": "ref_protected", "description": f"ADP runner {OPERATION}",
    }
    assert admin.pages.call_args_list[-1].args == ("groups/42/runners",)


@pytest.mark.parametrize("changed", [
    {"id": 43}, {"path": "tenant-other"}, {"full_path": "tenant-other/subgroup"},
    {"description": "unmanaged"}, {"parent_id": 8},
    {"shared_with_groups": [{"group_id": 12}]},
])
def test_wrong_group_or_parent_never_creates_runner(changed):
    admin = admin_fixture(group_changes=changed)
    with pytest.raises(register_runner.RegistrationError):
        create(admin)
    admin.request.assert_awaited_once()


@pytest.mark.parametrize("changed", [
    {"runner_type": "instance_type"}, {"runner_type": "project_type"},
    {"locked": False}, {"paused": False}, {"run_untagged": True},
    {"tag_list": []}, {"tag_list": ["adp-group-43"]},
    {"access_level": "not_protected"},
])
def test_shared_or_unrestricted_runner_is_never_accepted(changed):
    admin = admin_fixture(detail_changes=changed)
    with pytest.raises(register_runner.RegistrationError, match="runner_scope_not_verified"):
        create(admin)


def test_human_member_rejected_before_creation():
    admin = admin_fixture(members=[{"id": 7, "access_level": 50}, {"id": 8, "access_level": 30}])
    with pytest.raises(register_runner.RegistrationError, match="tenant_group_members_not_verified"):
        create(admin)
    admin.request.assert_awaited_once()


def test_missing_group_association_rejected():
    admin = admin_fixture()
    admin.pages.side_effect = [[{"id": 7, "access_level": 50}]] * 2 + [[]]
    with pytest.raises(register_runner.RegistrationError, match="runner_scope_not_verified"):
        create(admin)


class FakeStore:
    def __init__(self, current=None):
        self.record = None
        self.secret = current
        self.pending_value = None
        self.deleted = False
        self.fail_stage = False

    def read_record(self):
        return dict(self.record) if self.record else None

    def write_record(self, record):
        self.record = dict(record)

    def current(self, *, allow_expired=False):
        return self.secret

    def pending(self, operation_id):
        return self.pending_value

    def stage(self, record, token, token_expires_at=None):
        if self.fail_stage:
            raise RuntimeError("synthetic failure")
        self.pending_value = {"runner_id": record["new_runner_id"], "group_id": GROUP_ID, "generation": record["generation"], "version_id": record["operation_id"]}

    def promote(self, operation_id):
        self.secret = self.pending_value
        self.pending_value = None

    def schedule_deletion(self):
        self.deleted = True


def lifecycle(admin, store):
    return register_runner.RunnerLifecycle(admin, store, TENANT, GROUP_ID, [TAG], "ref_protected")


def test_begin_persists_intent_before_create_and_stages_paused_candidate():
    admin = begin_admin()
    store = FakeStore()
    result = asyncio.run(lifecycle(admin, store).begin(OPERATION))
    assert result == {"phase": "staged", "runner_id": 9}
    assert store.record["generation"] == 1
    assert store.pending_value["runner_id"] == 9
    with pytest.raises(register_runner.RegistrationError, match="operation_in_progress"):
        asyncio.run(lifecycle(admin, store).begin("00000000-0000-4000-8000-000000000002"))


def test_failed_storage_keeps_candidate_paused_and_reconciles_orphan():
    admin = begin_admin()
    store = FakeStore()
    store.fail_stage = True
    with pytest.raises(RuntimeError, match="synthetic failure"):
        asyncio.run(lifecycle(admin, store).begin(OPERATION))
    assert store.record["phase"] == "candidate"
    assert admin.request.call_args_list[2].kwargs["json"]["paused"] is True
    store.fail_stage = False
    recovery = admin_fixture()
    recovery.request.side_effect = [None, None, {"id": 10, "token": "glrt-replacement"},
                                    {"id": 10, "runner_type": "group_type", "run_untagged": False,
                                     "locked": True, "paused": True, "tag_list": [TAG],
                                     "access_level": "ref_protected", "description": f"ADP runner {OPERATION}"}]
    recovery.pages.side_effect = [
        [{"id": 9}], [{"id": 9}], [{"id": 7, "access_level": 50}],
        [{"id": 7, "access_level": 50}], [{"id": 10}],
    ]
    recovery.request.side_effect = [
        {"id": 9, "runner_type": "group_type", "paused": True, "description": f"ADP runner {OPERATION}", "locked": True,
         "run_untagged": False, "access_level": "ref_protected", "tag_list": [TAG]},
        None, None,
        {"id": GROUP_ID, "path": f"tenant-{KEY}", "full_path": f"tenant-{KEY}",
         "description": f"ADP tenant {KEY}", "parent_id": None},
        {"id": 10, "token": "glrt-replacement"},
        {"id": 10, "runner_type": "group_type", "run_untagged": False,
         "locked": True, "paused": True, "tag_list": [TAG],
         "access_level": "ref_protected", "description": f"ADP runner {OPERATION}"},
    ]
    assert asyncio.run(lifecycle(recovery, store).reconcile(OPERATION))["runner_id"] == 10
    assert recovery.request.call_args_list[1].args == ("DELETE", "runners/9")


def test_lost_stage_response_resumes_pending_without_creating_another_runner():
    admin = begin_admin()
    store = FakeStore()
    asyncio.run(lifecycle(admin, store).begin(OPERATION))
    store.record["phase"] = "candidate"
    recovery = admin_fixture()
    recovery.pages.side_effect = [[{"id": 9}], [{"id": 9}]]
    recovery.request.side_effect = [{"id": 9, "runner_type": "group_type", "paused": True,
                                     "locked": True, "run_untagged": False, "access_level": "ref_protected",
                                     "tag_list": [TAG], "description": f"ADP runner {OPERATION}"}]
    assert asyncio.run(lifecycle(recovery, store).reconcile(OPERATION))["phase"] == "staged"
    assert not any(call.args[0] == "POST" for call in recovery.request.call_args_list)


def begin_admin():
    admin = admin_fixture()
    group = {"id": GROUP_ID, "path": f"tenant-{KEY}", "full_path": f"tenant-{KEY}",
             "description": f"ADP tenant {KEY}", "parent_id": None}
    members = [{"id": 7, "access_level": 50}]
    admin.request.side_effect = [group, group, {"id": 9, "token": "glrt-synthetic-runner-token"},
                                 {"id": 9, "runner_type": "group_type", "run_untagged": False,
                                  "locked": True, "paused": True, "tag_list": [TAG],
                                  "access_level": "ref_protected", "description": f"ADP runner {OPERATION}"}]
    admin.pages.side_effect = [members, members, [], [], members, members, [{"id": 9}]]
    return admin


def test_rotation_waits_for_drain_and_manager_qualification():
    from types import SimpleNamespace

    admin = begin_admin()
    store = FakeStore(current={"runner_id": 5, "group_id": GROUP_ID, "tenant_id": TENANT, "generation": 1})
    manager = lifecycle(admin, store)
    assert asyncio.run(manager.begin(OPERATION, rotate=True))["phase"] == "staged"
    assert store.record["old_runner_id"] == 5
    assert store.record["generation"] == 2
    admin.request.side_effect = [None, None, None, {"paused": False}, None, None]
    with pytest.raises(register_runner.RegistrationError, match="runner_not_drained"):
        asyncio.run(manager.qualify(OPERATION, SimpleNamespace(drained=lambda runner_id: False)))
    assert store.record["phase"] == "staged"
    assert store.secret["runner_id"] == 5
    with pytest.raises(register_runner.RegistrationError, match="manager_not_qualified"):
        asyncio.run(manager.qualify(OPERATION, SimpleNamespace(drained=lambda runner_id: True, switch=lambda runner_id: None,
                                                                 manager_uses=lambda runner_id: False)))
    assert store.record["phase"] == "promoted"
    assert not any(call.args == ("PUT", "runners/9") for call in admin.request.call_args_list)
    assert asyncio.run(manager.qualify(OPERATION, SimpleNamespace(switch=lambda runner_id: None, manager_uses=lambda runner_id: runner_id == 9))) == {
        "phase": "finished", "runner_id": 9,
    }
    assert store.record["phase"] == "finished"
    assert admin.request.call_args_list[-2].args == ("DELETE", "runners/5")


def test_revoke_keeps_record_until_runner_absence_and_secret_deletion():
    admin = AsyncMock()
    admin.request.side_effect = [{"id": 5}, None, None, None]
    store = FakeStore(current={"runner_id": 5, "group_id": GROUP_ID, "tenant_id": TENANT, "generation": 1})
    manager = lifecycle(admin, store)
    assert asyncio.run(manager.revoke(OPERATION))["phase"] == "finished"
    assert [call.args for call in admin.request.call_args_list] == [
        ("GET", "runners/5"), ("PUT", "runners/5"),
        ("DELETE", "runners/5"), ("GET", "runners/5"),
    ]
    assert store.deleted


def test_async_manager_callbacks_are_awaited_before_any_unpause():
    from types import SimpleNamespace

    admin = begin_admin()
    store = FakeStore()
    manager = lifecycle(admin, store)
    asyncio.run(manager.begin(OPERATION))
    switch, uses = AsyncMock(), AsyncMock(return_value=False)
    with pytest.raises(register_runner.RegistrationError, match="manager_not_qualified"):
        asyncio.run(manager.qualify(OPERATION, SimpleNamespace(switch=switch, manager_uses=uses)))
    switch.assert_awaited_once_with(9)
    uses.assert_awaited_once_with(9)
    assert store.record["phase"] == "promoted"
    assert not any(call.args == ("PUT", "runners/9") for call in admin.request.call_args_list)
    uses.return_value = True
    admin.request.side_effect = [None, {"paused": False}]
    assert asyncio.run(manager.qualify(OPERATION, SimpleNamespace(switch=switch, manager_uses=uses)))["phase"] == "finished"


def test_previously_qualified_operation_requires_fresh_manager_evidence():
    from types import SimpleNamespace

    admin = begin_admin()
    store = FakeStore()
    manager = lifecycle(admin, store)
    asyncio.run(manager.begin(OPERATION))
    store.record["phase"] = "qualified"
    uses = AsyncMock(return_value=False)
    with pytest.raises(register_runner.RegistrationError, match="manager_not_qualified"):
        asyncio.run(manager.qualify(OPERATION, SimpleNamespace(manager_uses=uses)))
    uses.assert_awaited_once_with(9)
    assert not any(call.args == ("PUT", "runners/9") for call in admin.request.call_args_list)


def test_failed_runner_deletion_preserves_revocation_intent():
    admin = AsyncMock()
    admin.request.side_effect = [{"id": 5}, None, None, {"id": 5}]
    store = FakeStore(current={"runner_id": 5, "group_id": GROUP_ID, "tenant_id": TENANT, "generation": 1})
    with pytest.raises(register_runner.RegistrationError, match="runner_removal_not_verified"):
        asyncio.run(lifecycle(admin, store).revoke(OPERATION))
    assert store.record["phase"] == "revoking"
    assert not store.deleted


def test_wrong_group_secret_rejected_without_create():
    admin = begin_admin()
    store = FakeStore(current={"runner_id": 5, "group_id": 43, "tenant_id": TENANT, "generation": 1})
    with pytest.raises(register_runner.RegistrationError, match="runner_secret_not_verified"):
        asyncio.run(lifecycle(admin, store).begin(OPERATION, rotate=True))
    assert store.record is None
    assert not any(call.args[0] == "POST" for call in admin.request.call_args_list)


def test_reconcile_rejects_unverified_orphan_scope_without_deletion():
    admin = AsyncMock()
    admin.pages.side_effect = [[{"id": 9}], [{"id": 9}]]
    admin.request.side_effect = [{"id": 9, "runner_type": "instance_type", "paused": True,
                                  "description": f"ADP runner {OPERATION}"}]
    store = FakeStore()
    store.write_record({"operation_id": OPERATION, "group_id": GROUP_ID,
                        "generation": 1, "phase": "intent", "new_runner_id": None})
    with pytest.raises(register_runner.RegistrationError, match="runner_scope_not_verified"):
        asyncio.run(lifecycle(admin, store).reconcile(OPERATION))
    assert store.record["phase"] == "intent"
    admin.request.assert_awaited_once()


class FakeAwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeSecrets:
    def __init__(self):
        self.versions = {}
        self.stages = {}
        self.deleted = False

    def get_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == "adp/dev/gitlab-runner/42"
        version = kwargs.get("VersionId") or next(
            (version_id for version_id, stages in self.stages.items() if kwargs["VersionStage"] in stages), None
        )
        if version not in self.versions:
            raise FakeAwsError("ResourceNotFoundException")
        return {"VersionId": version, "SecretString": self.versions[version]}

    def put_secret_value(self, **kwargs):
        assert kwargs["VersionStages"] == ["AWSPENDING"]
        version = kwargs["ClientRequestToken"]
        self.versions[version] = kwargs["SecretString"]
        self.stages[version] = ["AWSPENDING"]

    def describe_secret(self, **kwargs):
        return {"VersionIdsToStages": dict(self.stages)}

    def update_secret_version_stage(self, **kwargs):
        version = kwargs["MoveToVersionId"]
        old = kwargs.get("RemoveFromVersionId")
        if old:
            self.stages[old].remove("AWSCURRENT")
        self.stages[version].append("AWSCURRENT")

    def delete_secret(self, **kwargs):
        assert kwargs["RecoveryWindowInDays"] == 30
        self.deleted = True


class FakeSsm:
    def __init__(self):
        self.value = "{}"

    def get_parameter(self, **kwargs):
        assert kwargs["Name"] == "/adp/dev/gitlab-runner-operations/42"
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, **kwargs):
        assert kwargs["Type"] == "String" and kwargs["Overwrite"] is True
        self.value = kwargs["Value"]


def test_aws_store_keeps_token_only_in_versioned_secret_not_operation_record():
    import json

    secrets = FakeSecrets()
    ssm = FakeSsm()
    store = register_runner.AwsRunnerStore(
        secrets, ssm, "dev", TENANT, GROUP_ID, "https://gitlab.example.test/gitlab",
        "https://runner.example.test/gitlab", [TAG], "ref_protected",
    )
    assert store.read_record() is None
    assert store.current() is None
    record = {"phase": "candidate", "operation_id": OPERATION, "secret_version_id": OPERATION,
              "new_runner_id": 9, "old_runner_id": None, "group_id": GROUP_ID, "generation": 1}
    store.write_record(record)
    store.stage(record, "glrt-synthetic-runner-token", "2030-01-01T00:00:00Z")
    assert store.pending(OPERATION)["runner_id"] == 9
    assert store.current() is None
    assert "glrt-synthetic-runner-token" not in ssm.value
    value = json.loads(secrets.versions[OPERATION])
    assert value == {
        "schema_version": 1, "tenant_id": TENANT, "group_id": GROUP_ID,
        "runner_id": 9, "instance_url": "https://gitlab.example.test/gitlab",
        "runner_url": "https://runner.example.test/gitlab", "tag_list": [TAG],
        "access_level": "ref_protected", "token": "glrt-synthetic-runner-token",
        "token_expires_at": "2030-01-01T00:00:00Z", "operation_id": OPERATION, "generation": 1,
    }
    store.promote(OPERATION)
    assert store.current()["version_id"] == OPERATION
    store.promote(OPERATION)
    store.schedule_deletion()
    assert secrets.deleted


def test_aws_store_rejects_cross_group_or_mismatched_version():
    import json

    secrets = FakeSecrets()
    store = register_runner.AwsRunnerStore(
        secrets, FakeSsm(), "dev", TENANT, GROUP_ID, "https://gitlab.example.test/gitlab",
        "https://runner.example.test/gitlab", [TAG], "ref_protected",
    )
    record = {"new_runner_id": 9, "operation_id": OPERATION, "generation": 1}
    store.stage(record, "glrt-synthetic-runner-token")
    value = json.loads(secrets.versions[OPERATION])
    value["group_id"] = 43
    secrets.versions[OPERATION] = json.dumps(value)
    with pytest.raises(register_runner.RegistrationError, match="runner_secret_not_verified"):
        store.pending(OPERATION)
    value["group_id"] = GROUP_ID
    value["operation_id"] = "00000000-0000-4000-8000-000000000002"
    secrets.versions[OPERATION] = json.dumps(value)
    with pytest.raises(register_runner.RegistrationError, match="runner_secret_not_verified"):
        store.pending(OPERATION)


def test_nonsecret_operation_record_rejects_token_field():
    store = register_runner.AwsRunnerStore(
        FakeSecrets(), FakeSsm(), "dev", TENANT, GROUP_ID,
        "https://gitlab.example.test/gitlab", "https://runner.example.test/gitlab", [TAG], "ref_protected",
    )
    with pytest.raises(register_runner.RegistrationError, match="operation_record_invalid"):
        store.write_record({"operation_id": OPERATION, "token": "glrt-synthetic-runner-token"})


def test_aws_store_rejects_expired_or_non_runner_token():
    import json

    secrets = FakeSecrets()
    store = register_runner.AwsRunnerStore(
        secrets, FakeSsm(), "dev", TENANT, GROUP_ID,
        "https://gitlab.example.test/gitlab", "https://runner.example.test/gitlab", [TAG], "ref_protected",
    )
    record = {"new_runner_id": 9, "operation_id": OPERATION, "generation": 1}
    with pytest.raises(register_runner.RegistrationError, match="runner_token_missing"):
        store.stage(record, "glpat-admin-canary")
    store.stage(record, "glrt-runner-canary", "2020-01-01T00:00:00Z")
    with pytest.raises(register_runner.RegistrationError, match="runner_secret_not_verified"):
        store.pending(OPERATION)
    value = json.loads(secrets.versions[OPERATION])
    value["token_expires_at"] = "not-a-date"
    secrets.versions[OPERATION] = json.dumps(value)
    with pytest.raises(register_runner.RegistrationError, match="runner_secret_not_verified"):
        store.pending(OPERATION)


def test_aws_store_reconciles_lost_stage_response_with_persisted_record():
    secrets = FakeSecrets()
    ssm = FakeSsm()
    store = register_runner.AwsRunnerStore(
        secrets, ssm, "dev", TENANT, GROUP_ID,
        "https://gitlab.example.test/gitlab", "https://runner.example.test/gitlab", [TAG], "ref_protected",
    )
    admin = begin_admin()
    assert asyncio.run(lifecycle(admin, store).begin(OPERATION))["phase"] == "staged"
    assert store.read_record()["secret_version_id"] == OPERATION
    record = store.read_record()
    record["phase"] = "candidate"
    store.write_record(record)
    recovery = admin_fixture()
    recovery.pages.side_effect = [[{"id": 9}], [{"id": 9}]]
    recovery.request.side_effect = [{"id": 9, "runner_type": "group_type", "paused": True,
                                     "locked": True, "run_untagged": False, "access_level": "ref_protected",
                                     "tag_list": [TAG], "description": f"ADP runner {OPERATION}"}]
    assert asyncio.run(lifecycle(recovery, store).reconcile(OPERATION))["phase"] == "staged"
    assert not any(call.args[0] == "POST" for call in recovery.request.call_args_list)


class FakeCliSecrets(FakeSecrets):
    def __init__(self, *, failure=False):
        super().__init__()
        self.failure = failure

    def get_secret_value(self, **kwargs):
        if kwargs["SecretId"] == "adp/dev/gitlab-admin-token":
            if self.failure:
                import logging
                logging.getLogger("botocore").debug("glpat-admin-canary glrt-runner-canary")
                raise RuntimeError("glpat-admin-canary provider exception glrt-runner-canary")
            return {"SecretString": "glpat-admin-canary"}
        return super().get_secret_value(**kwargs)


def cli_fixture(tmp_path, *, failure=False):
    import json
    from types import SimpleNamespace

    config_file = tmp_path / "operator-config.json"
    config_file.write_text(json.dumps({
        "tenant_id": TENANT, "group_id": GROUP_ID, "tag_list": [TAG],
        "access_level": "ref_protected", "instance_url": "https://gitlab.example.test/gitlab",
        "runner_url": "https://runner.example.test/gitlab", "ca_file": "/private/operator-root.pem",
    }))
    config_file.chmod(0o600)
    secrets = FakeCliSecrets(failure=failure)
    ssm = FakeSsm()
    sdk = SimpleNamespace(client=lambda name: {"ssm": ssm, "secretsmanager": secrets}[name])
    admin = begin_admin()
    admin.__aenter__.return_value = admin
    def factory(*args, **kwargs):
        return admin
    args = ["--environment", "dev", "--region", "us-east-1", "--config", str(config_file),
            "--operation-id", OPERATION, "--create"]
    return args, sdk, factory, secrets, ssm, admin


def test_real_cli_create_prints_only_safe_identifiers(tmp_path, capsys, caplog):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, factory, secrets, ssm, admin = cli_fixture(tmp_path)
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "phase": "staged", "operation_id": OPERATION, "runner_id": 9, "secret_version_id": OPERATION,
    }
    assert captured.err == ""
    assert all(canary not in captured.out + captured.err + caplog.text + ssm.value
               for canary in ("glpat-admin-canary", "glrt-synthetic-runner-token"))
    assert secrets.versions[OPERATION]


@pytest.mark.parametrize("failure", [False, True])
def test_cli_suppresses_actual_sdk_request_response_and_late_child_logs(tmp_path, capsys, caplog, monkeypatch, failure):
    import json
    import logging
    from types import SimpleNamespace

    import boto3
    from botocore.awsrequest import AWSResponse

    caplog.set_level(logging.DEBUG)
    args, sdk, factory, secrets, ssm, _ = cli_fixture(tmp_path)
    client = boto3.client("secretsmanager", region_name="us-east-1",
                          aws_access_key_id="synthetic-access-key", aws_secret_access_key="synthetic-secret-key")
    requests = []

    def send(request):
        requests.append(request)
        body = json.dumps({"SecretString": "glpat-admin-canary", "VersionId": OPERATION}).encode()
        return AWSResponse(request.url, 200, {"content-type": "application/x-amz-json-1.1"},
                           SimpleNamespace(stream=lambda: iter([body])))

    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    client.get_secret_value(SecretId="adp/dev/gitlab-admin-token")
    client.put_secret_value(SecretId="adp/dev/gitlab-runner/42", SecretString="glrt-synthetic-runner-token")
    assert "glpat-admin-canary" in caplog.text and "glrt-synthetic-runner-token" in caplog.text
    caplog.clear()
    requests.clear()
    original_read = secrets.get_secret_value
    original_write = secrets.put_secret_value
    previous_threshold = logging.root.manager.disable
    logger = logging.getLogger("botocore.parsers")
    previous_settings = (logger.disabled, logger.level, logger.propagate, list(logger.handlers))
    late_name = f"botocore.endpoint.{tmp_path.name}"
    assert late_name not in logging.root.manager.loggerDict

    def read(**kwargs):
        if kwargs["SecretId"] == "adp/dev/gitlab-admin-token":
            logging.getLogger(late_name).debug("glpat-admin-canary")
            value = client.get_secret_value(**kwargs)
            if failure:
                raise RuntimeError("glpat-admin-canary")
            return value
        return original_read(**kwargs)

    def write(**kwargs):
        client.put_secret_value(**kwargs)
        return original_write(**kwargs)

    monkeypatch.setattr(secrets, "get_secret_value", read)
    monkeypatch.setattr(secrets, "put_secret_value", write)
    try:
        assert register_runner.main(args, sdk=sdk, admin_factory=factory) == (3 if failure else 0)
        output = capsys.readouterr()
        assert len(requests) == (1 if failure else 2)
        assert all(canary not in output.out + output.err + caplog.text + ssm.value
                   for canary in ("glpat-admin-canary", "glrt-synthetic-runner-token"))
        assert logging.root.manager.disable == previous_threshold
        assert (logger.disabled, logger.level, logger.propagate, logger.handlers) == previous_settings
        logger.debug("sdk-logging-restored")
        logging.getLogger(late_name).debug("late-logging-restored")
        assert "sdk-logging-restored" in caplog.text and "late-logging-restored" in caplog.text
    finally:
        client.close()


@pytest.mark.parametrize("threshold", [0, 30, 51])
@pytest.mark.parametrize("failure", [False, True])
def test_cli_preserves_existing_logging_suppression(tmp_path, capsys, threshold, failure):
    import logging

    args, sdk, factory, _, _, _ = cli_fixture(tmp_path, failure=failure)
    previous = logging.root.manager.disable
    try:
        logging.disable(threshold)
        assert register_runner.main(args, sdk=sdk, admin_factory=factory) == (3 if failure else 0)
        assert logging.root.manager.disable == threshold
    finally:
        logging.disable(previous)
    capsys.readouterr()


def test_real_cli_provider_failure_never_prints_canaries(tmp_path, capsys, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, factory, _, ssm, _ = cli_fixture(tmp_path, failure=True)
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 3
    output = capsys.readouterr()
    assert output.out == "" and output.err.strip() == "runner_provider_unavailable"
    assert "glpat-admin-canary" not in output.err + caplog.text + ssm.value
    assert "glrt-runner-canary" not in output.err + caplog.text + ssm.value


def test_real_cli_rejects_world_readable_config_before_fetching_secret(tmp_path, capsys):
    args, sdk, factory, _, _, _ = cli_fixture(tmp_path)
    from pathlib import Path
    Path(args[args.index("--config") + 1]).chmod(0o644)
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 2
    assert capsys.readouterr().err.strip() == "unsafe_operator_config"


def test_real_cli_failed_secret_write_keeps_resumable_record_without_canary_output(tmp_path, capsys, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, factory, secrets, ssm, _ = cli_fixture(tmp_path)

    def failed_write(**kwargs):
        logging.getLogger("botocore").debug("glrt-synthetic-runner-token glpat-admin-canary")
        raise RuntimeError("glrt-synthetic-runner-token glpat-admin-canary")

    secrets.put_secret_value = failed_write
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() == "runner_provider_unavailable"
    assert "glrt-synthetic-runner-token" not in captured.err + caplog.text + ssm.value
    assert "glpat-admin-canary" not in captured.err + caplog.text + ssm.value
    assert "candidate" in ssm.value
    assert logging.getLogger("botocore").disabled is False


@pytest.mark.parametrize("bad_url", ["http://gitlab.example.test/gitlab", "https://elsewhere.example.test@evil.example.test/gitlab",
                                       "https://gitlab.example.test:0/gitlab", "https://gitlab.example.test:99999/gitlab",
                                       "https://gitlab.example.test/../gitlab",
                                       "https://gitlab.example.test/gitlab?token=glpat-admin-canary"])
def test_real_cli_rejects_invalid_urls_before_secret_fetch(tmp_path, capsys, bad_url):
    import json

    args, sdk, factory, secrets, _, _ = cli_fixture(tmp_path)
    from pathlib import Path
    config_file = Path(args[args.index("--config") + 1])
    config = json.loads(config_file.read_text())
    config["runner_url"] = bad_url
    config_file.write_text(json.dumps(config))
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 2
    assert capsys.readouterr().err.strip() == "invalid_operator_url"
    assert not secrets.versions


def test_real_cli_api_failure_keeps_canary_out_of_output_and_record(tmp_path, capsys, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, factory, _, ssm, admin = cli_fixture(tmp_path)
    group = {"id": GROUP_ID, "path": f"tenant-{KEY}", "full_path": f"tenant-{KEY}",
             "description": f"ADP tenant {KEY}", "parent_id": None}
    admin.request.side_effect = [group, group, RuntimeError("glpat-admin-canary glrt-runner-canary")]
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 3
    output = capsys.readouterr()
    assert output.out == "" and output.err.strip() == "runner_provider_unavailable"
    assert "glpat-admin-canary" not in output.err + caplog.text + ssm.value
    assert "glrt-runner-canary" not in output.err + caplog.text + ssm.value
    assert '"phase": "intent"' in ssm.value


def test_terraform_runner_resources_cannot_write_secret_versions_to_state():
    import re

    source = (Path(__file__).resolve().parents[2] / "infra" / "runner_registration.tf").read_text()
    assert not re.search(r'resource\s+"aws_secretsmanager_secret_version"', source)
    assert not re.search(r'resource\s+"aws_ssm_parameter"[^}]*type\s*=\s*"SecureString"', source, re.DOTALL)
    assert 'value       = "{}"' in source
    assert 'ignore_changes = [value]' in source


class StatefulCliAdmin:
    owner_id = 7

    def __init__(self, ssm):
        self.ssm = ssm
        self.runners = {}
        self.next_runner_id = 9
        self.calls = []
        self.fail_create = False
        self.fail_delete = False
        self.keep_deleted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def request(self, method, path, **kwargs):
        import json
        import logging

        self.calls.append((method, path))
        if (method, path) == ("GET", f"groups/{GROUP_ID}"):
            return {"id": GROUP_ID, "path": f"tenant-{KEY}", "full_path": f"tenant-{KEY}",
                    "description": f"ADP tenant {KEY}", "parent_id": None, "shared_with_groups": []}
        if (method, path) == ("POST", "user/runners"):
            assert json.loads(self.ssm.value)["phase"] in ("intent", "candidate")
            payload = kwargs["json"]
            assert payload["runner_type"] == "group_type" and payload["group_id"] == GROUP_ID
            assert payload["locked"] is True and payload["run_untagged"] is False
            assert payload["paused"] is True and payload["access_level"] == "ref_protected"
            runner_id = self.next_runner_id
            self.next_runner_id += 1
            self.runners[runner_id] = dict(payload, id=runner_id)
            if self.fail_create:
                self.fail_create = False
                logging.getLogger("botocore").debug("glpat-admin-canary glrt-synthetic-runner-token")
                raise RuntimeError("glpat-admin-canary glrt-synthetic-runner-token")
            return {"id": runner_id, "token": "glrt-synthetic-runner-token",
                    "token_expires_at": "2030-01-01T00:00:00Z"}
        prefix = "runners/"
        assert path.startswith(prefix)
        runner_id = int(path[len(prefix):])
        if method == "GET":
            return dict(self.runners[runner_id]) if runner_id in self.runners else None
        if method == "PUT":
            self.runners[runner_id].update(kwargs["json"])
            return dict(self.runners[runner_id])
        if method == "DELETE":
            if not self.keep_deleted:
                self.runners.pop(runner_id, None)
            else:
                self.keep_deleted = False
            if self.fail_delete:
                self.fail_delete = False
                raise RuntimeError("glpat-admin-canary glrt-synthetic-runner-token")
            return None
        raise AssertionError((method, path))

    async def pages(self, path, **kwargs):
        if path in (f"groups/{GROUP_ID}/members", f"groups/{GROUP_ID}/members/all"):
            return [{"id": self.owner_id, "access_level": 50}]
        if path == "runners/all":
            return [{"id": runner_id} for runner_id, runner in self.runners.items()
                    if kwargs["search"] in runner["description"]]
        if path == f"groups/{GROUP_ID}/runners":
            return [{"id": runner_id} for runner_id in self.runners]
        raise AssertionError(path)


def stateful_cli_fixture(tmp_path):
    args, sdk, _, secrets, ssm, _ = cli_fixture(tmp_path)
    admin = StatefulCliAdmin(ssm)
    return args, sdk, secrets, ssm, admin


def call_cli(args, sdk, secrets, ssm, admin, capsys, caplog):
    import json
    from datetime import datetime, timezone

    def clock():
        return datetime(2026, 11, 1, tzinfo=timezone.utc)

    result = register_runner.main(args, sdk=sdk, admin_factory=lambda *_args, **_kwargs: admin, clock=clock)
    captured = capsys.readouterr()
    assert all(canary not in captured.out + captured.err + caplog.text + ssm.value
               for canary in ("glpat-admin-canary", "glrt-synthetic-runner-token"))
    if result == 0:
        assert captured.err == ""
        assert set(json.loads(captured.out)) == {"phase", "operation_id", "runner_id", "secret_version_id"}
    else:
        assert captured.out == ""
        assert captured.err.strip() in {"runner_provider_unavailable", "runner_removal_not_verified",
                                        "operation_in_progress", "operation_ownership_conflict", "invalid_arguments",
                                        "operation_id_required", "runner_secret_not_verified"}
    return result, json.loads(captured.out) if captured.out else captured.err.strip()


def seed_cli_current(secrets, ssm, admin):
    from datetime import datetime, timezone

    previous_operation = "00000000-0000-4000-8000-000000000000"
    store = register_runner.AwsRunnerStore(
        secrets, ssm, "dev", TENANT, GROUP_ID, "https://gitlab.example.test/gitlab",
        "https://runner.example.test/gitlab", [TAG], "ref_protected",
        clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    store.stage({"operation_id": previous_operation, "new_runner_id": 5, "generation": 1},
                "glrt-synthetic-runner-token", "2030-01-01T00:00:00Z")
    store.promote(previous_operation)
    admin.runners[5] = {"id": 5, "description": "preexisting", "runner_type": "group_type",
                        "locked": True, "paused": False, "run_untagged": False,
                        "tag_list": [TAG], "access_level": "ref_protected"}
    return previous_operation


def test_cli_reconciles_lost_create_response_without_activating_orphan(tmp_path, capsys, caplog):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    admin.fail_create = True
    assert call_cli(args, sdk, secrets, ssm, admin, capsys, caplog) == (3, "runner_provider_unavailable")
    assert json.loads(ssm.value)["phase"] == "intent" and admin.runners[9]["paused"] is True
    assert call_cli(args[:-1] + ["--reconcile"], sdk, secrets, ssm, admin, capsys, caplog) == (
        0, {"phase": "staged", "operation_id": OPERATION, "runner_id": 10, "secret_version_id": OPERATION},
    )
    assert list(admin.runners) == [10] and admin.runners[10]["paused"] is True
    assert admin.calls.count(("DELETE", "runners/9")) == 1
    assert json.loads(secrets.versions[OPERATION])["runner_id"] == 10
    assert json.loads(ssm.value)["phase"] == "staged"


def test_cli_reconciles_lost_secret_write_response_without_duplicate_runner(tmp_path, capsys, caplog):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    put_secret = secrets.put_secret_value

    def lost_write(**kwargs):
        put_secret(**kwargs)
        secrets.put_secret_value = put_secret
        raise RuntimeError("glpat-admin-canary glrt-synthetic-runner-token")

    secrets.put_secret_value = lost_write
    assert call_cli(args, sdk, secrets, ssm, admin, capsys, caplog) == (3, "runner_provider_unavailable")
    assert json.loads(ssm.value)["phase"] == "candidate"
    assert call_cli(args[:-1] + ["--reconcile"], sdk, secrets, ssm, admin, capsys, caplog)[0] == 0
    assert admin.calls.count(("POST", "user/runners")) == 1
    assert list(admin.runners) == [9] and admin.runners[9]["paused"] is True
    assert secrets.stages[OPERATION] == ["AWSPENDING"]
    assert json.loads(ssm.value)["phase"] == "staged"


@pytest.mark.parametrize("lost_response", [True, False])
def test_cli_revocation_reconcile_confirms_absence_before_deleting_secret(tmp_path, capsys, caplog, lost_response):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    seed_cli_current(secrets, ssm, admin)
    admin.fail_delete = lost_response
    admin.keep_deleted = not lost_response
    revoke = args[:-1] + ["--revoke"]
    expected = (3, "runner_provider_unavailable") if lost_response else (2, "runner_removal_not_verified")
    assert call_cli(revoke, sdk, secrets, ssm, admin, capsys, caplog) == expected
    assert json.loads(ssm.value)["phase"] == "revoking" and not secrets.deleted
    assert call_cli(args[:-1] + ["--reconcile"], sdk, secrets, ssm, admin, capsys, caplog) == (
        0, {"phase": "finished", "operation_id": OPERATION, "runner_id": 5, "secret_version_id": None},
    )
    assert not admin.runners and secrets.deleted and json.loads(ssm.value)["phase"] == "finished"


def test_cli_rotate_stages_replacement_without_touching_active_token(tmp_path, capsys, caplog):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    previous_operation = seed_cli_current(secrets, ssm, admin)
    assert call_cli(args[:-1] + ["--rotate"], sdk, secrets, ssm, admin, capsys, caplog)[0] == 0
    assert "AWSCURRENT" in secrets.stages[previous_operation]
    assert secrets.stages[OPERATION] == ["AWSPENDING"]
    assert admin.runners[5]["paused"] is False and admin.runners[9]["paused"] is True
    assert json.loads(ssm.value)["old_runner_id"] == 5
    assert call_cli(args[:-1] + ["--reconcile"], sdk, secrets, ssm, admin, capsys, caplog)[0] == 0
    assert call_cli(args[:-1] + ["--revoke"], sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "operation_in_progress",
    )
    assert list(admin.runners) == [5, 9] and not secrets.deleted


def test_cli_rejects_unsafe_arguments_without_echoing_credentials(tmp_path, capsys, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    assert call_cli(args + ["--credential", "glpat-admin-canary"], sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "invalid_arguments",
    )
    assert call_cli(args[:args.index("--operation-id")] + ["--reconcile"], sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "operation_id_required",
    )
    assert not admin.calls and not secrets.versions and ssm.value == "{}"


def test_cli_fails_closed_until_orphan_cleanup_is_verified(tmp_path, capsys, caplog):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    put_secret = secrets.put_secret_value

    def failed_write(**kwargs):
        secrets.put_secret_value = put_secret
        raise RuntimeError("glpat-admin-canary glrt-synthetic-runner-token")

    secrets.put_secret_value = failed_write
    assert call_cli(args, sdk, secrets, ssm, admin, capsys, caplog) == (3, "runner_provider_unavailable")
    assert json.loads(ssm.value)["phase"] == "candidate" and list(admin.runners) == [9]
    admin.keep_deleted = True
    reconcile = args[:-1] + ["--reconcile"]
    assert call_cli(reconcile, sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "runner_removal_not_verified",
    )
    assert admin.calls.count(("POST", "user/runners")) == 1
    assert json.loads(ssm.value)["phase"] == "candidate" and not secrets.versions
    assert call_cli(reconcile, sdk, secrets, ssm, admin, capsys, caplog)[0] == 0
    assert admin.calls.count(("POST", "user/runners")) == 2
    assert list(admin.runners) == [10] and admin.runners[10]["paused"] is True
    assert json.loads(secrets.versions[OPERATION])["runner_id"] == 10
    assert call_cli(reconcile, sdk, secrets, ssm, admin, capsys, caplog)[0] == 0
    assert admin.calls.count(("POST", "user/runners")) == 2


def test_cli_wrong_operation_cannot_reconcile_staged_runner(tmp_path, capsys, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    assert call_cli(args, sdk, secrets, ssm, admin, capsys, caplog)[0] == 0
    wrong = "00000000-0000-4000-8000-000000000002"
    reconcile = args[:-3] + ["--operation-id", wrong, "--reconcile"]
    assert call_cli(reconcile, sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "operation_ownership_conflict",
    )
    assert list(admin.runners) == [9] and secrets.stages[OPERATION] == ["AWSPENDING"]


def test_cli_fixed_clock_rejects_expired_current_before_rotation(tmp_path, capsys, caplog):
    import json
    import logging

    caplog.set_level(logging.DEBUG)
    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    previous_operation = seed_cli_current(secrets, ssm, admin)
    previous = json.loads(secrets.versions[previous_operation])
    previous["token_expires_at"] = "2026-11-01T00:00:00Z"
    secrets.versions[previous_operation] = json.dumps(previous)
    assert call_cli(args[:-1] + ["--rotate"], sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "runner_secret_not_verified",
    )
    assert ssm.value == "{}" and admin.calls == [("GET", "groups/42")]
    assert list(admin.runners) == [5] and secrets.stages[previous_operation].count("AWSCURRENT") == 1


def test_cli_fixed_clock_revokes_expired_current_with_durable_intent(tmp_path, capsys, caplog, monkeypatch):
    import json

    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    previous_operation = seed_cli_current(secrets, ssm, admin)
    previous = json.loads(secrets.versions[previous_operation])
    previous["token_expires_at"] = "2026-11-01T00:00:00Z"
    secrets.versions[previous_operation] = json.dumps(previous)
    original_request = admin.request

    async def request(method, path, **kwargs):
        assert json.loads(ssm.value) == {
            "operation_id": OPERATION, "group_id": GROUP_ID, "phase": "revoking", "old_runner_id": 5,
        }
        return await original_request(method, path, **kwargs)

    monkeypatch.setattr(admin, "request", request)
    assert call_cli(args[:-1] + ["--revoke"], sdk, secrets, ssm, admin, capsys, caplog) == (
        0, {"phase": "finished", "operation_id": OPERATION, "runner_id": 5, "secret_version_id": None},
    )
    assert admin.calls == [("GET", "runners/5"), ("PUT", "runners/5"), ("DELETE", "runners/5"), ("GET", "runners/5")]
    assert not admin.runners and secrets.deleted and json.loads(ssm.value)["phase"] == "finished"


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "another-tenant"), ("group_id", 43), ("runner_id", 0),
    ("operation_id", OPERATION), ("schema_version", 2),
    ("token_expires_at", "not-a-date"), ("token_expires_at", "2020-01-01T00:00:00"),
])
def test_cli_revocation_still_rejects_invalid_expired_record(tmp_path, capsys, caplog, field, value):
    import json

    args, sdk, secrets, ssm, admin = stateful_cli_fixture(tmp_path)
    previous_operation = seed_cli_current(secrets, ssm, admin)
    previous = json.loads(secrets.versions[previous_operation])
    previous["token_expires_at"] = "2026-11-01T00:00:00Z"
    previous[field] = value
    secrets.versions[previous_operation] = json.dumps(previous)
    assert call_cli(args[:-1] + ["--revoke"], sdk, secrets, ssm, admin, capsys, caplog) == (
        2, "runner_secret_not_verified",
    )
    assert ssm.value == "{}" and not admin.calls and not secrets.deleted
    assert list(admin.runners) == [5]


def product_scope_rows():
    parent = {"id": GROUP_ID, "path": f"tenant-{KEY}", "full_path": f"tenant-{KEY}",
              "description": f"ADP tenant {KEY}", "parent_id": None, "shared_with_groups": []}
    subgroup = {"id": 43, "path": "product-synthetic-product", "parent_id": GROUP_ID,
                "full_path": f"tenant-{KEY}/product-synthetic-product",
                "description": "ADP product synthetic-product", "shared_with_groups": []}
    project = {"id": 5, "path": "example", "namespace": {"id": 43, "kind": "group"},
               "path_with_namespace": f"{subgroup['full_path']}/example",
               "visibility": "private", "shared_with_groups": []}
    return parent, subgroup, project


def test_product_scope_uses_tenant_parent_and_separate_product_project_readback():
    parent, subgroup, project = product_scope_rows()
    admin = admin_fixture()
    admin.request.side_effect = [parent, subgroup, project]
    admin.pages.side_effect = [[{"id": 7, "access_level": 50}]] * 2
    assert asyncio.run(register_runner.verify_product_scope(
        admin, TENANT, GROUP_ID, "synthetic-product", 43, 5)) == project
    assert [call.args for call in admin.request.call_args_list] == [
        ("GET", "groups/42"), ("GET", "groups/43"), ("GET", "projects/5")]


@pytest.mark.parametrize("wrong", [
    ("parent", {"parent_id": 7}),
    ("parent", {"path": "product-synthetic-product", "full_path": "product-synthetic-product"}),
    ("subgroup", {"parent_id": 7}),
    ("subgroup", {"full_path": "tenant-other/product-synthetic-product"}),
    ("subgroup", {"shared_with_groups": [{"group_id": 99}]}),
    ("project", {"namespace": {"id": GROUP_ID, "kind": "group"}}),
    ("project", {"path_with_namespace": "tenant-other/example"}),
    ("project", {"shared_with_groups": [{"group_id": 99}]}),
])
def test_product_root_or_cross_product_project_refused(wrong):
    parent, subgroup, project = product_scope_rows()
    rows = {"parent": parent, "subgroup": subgroup, "project": project}
    rows[wrong[0]].update(wrong[1])
    admin = admin_fixture()
    admin.request.side_effect = [parent, subgroup, project]
    admin.pages.side_effect = [[{"id": 7, "access_level": 50}]] * 2
    with pytest.raises(register_runner.RegistrationError):
        asyncio.run(register_runner.verify_product_scope(admin, TENANT, GROUP_ID, "synthetic-product", 43, 5))
    assert all(call.args[0] == "GET" for call in admin.request.call_args_list)


def test_qualification_tenant_root_stays_independent_of_product_scope():
    other_tenant = "synthetic-qualification-tenant"
    other_key = hashlib.sha256(other_tenant.encode()).hexdigest()[:24]
    admin = admin_fixture()
    admin.request.side_effect = [{"id": 106, "path": f"tenant-{other_key}", "full_path": f"tenant-{other_key}",
                                  "description": f"ADP tenant {other_key}", "parent_id": None, "shared_with_groups": []}]
    admin.pages.side_effect = [[{"id": 7, "access_level": 50}]] * 2
    assert asyncio.run(register_runner.verify_tenant_group(admin, other_tenant, 106)) == "adp-group-106"
    admin.request.assert_awaited_once_with("GET", "groups/106", missing=True)


def test_operator_config_with_product_scope_checks_before_mutation(tmp_path, capsys):
    import json

    args, sdk, factory, _, _, admin = cli_fixture(tmp_path)
    config_path = Path(args[args.index("--config") + 1])
    config = json.loads(config_path.read_text())
    config.update(product_id="synthetic-product", product_group_id=43, project_id=5)
    config_path.write_text(json.dumps(config))
    parent, subgroup, project = product_scope_rows()
    admin.request.side_effect = [parent, subgroup, {**project, "namespace": {"id": 99, "kind": "group"}}]
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 2
    assert capsys.readouterr().err.strip() == "project_scope_not_verified"
    assert all(call.args[0] == "GET" for call in admin.request.call_args_list)


def test_operator_product_scope_proceeds_only_with_parent_group(tmp_path, capsys):
    import json

    args, sdk, factory, _, _, admin = cli_fixture(tmp_path)
    config_path = Path(args[args.index("--config") + 1])
    config = json.loads(config_path.read_text())
    config.update(product_id="synthetic-product", product_group_id=43, project_id=5)
    config_path.write_text(json.dumps(config))
    parent, subgroup, project = product_scope_rows()
    admin.request.side_effect = [parent, subgroup, project, *admin.request.side_effect]
    admin.pages.side_effect = [[{"id": 7, "access_level": 50}]] * 2 + list(admin.pages.side_effect)
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 0
    assert capsys.readouterr().err == ""
    assert next(call.kwargs["json"] for call in admin.request.call_args_list if call.args[0] == "POST")["group_id"] == GROUP_ID


def test_partial_product_scope_config_rejected_before_gitlab_call(tmp_path, capsys):
    import json

    args, sdk, factory, _, _, admin = cli_fixture(tmp_path)
    config_path = Path(args[args.index("--config") + 1])
    config = json.loads(config_path.read_text())
    config.update(product_group_id=43)
    config_path.write_text(json.dumps(config))
    assert register_runner.main(args, sdk=sdk, admin_factory=factory) == 2
    assert capsys.readouterr().err.strip() == "invalid_operator_config"
    admin.request.assert_not_awaited()


def ci_admin(*, builds="disabled", project_changes=None, developer_access=30, readback=True):
    parent, subgroup, project = product_scope_rows()
    project.update({"builds_access_level": builds, "shared_runners_enabled": False, "group_runners_enabled": True})
    project.update(project_changes or {})
    admin = AsyncMock()
    admin.owner_id = 7
    members = [{"id": 7, "access_level": 50}, {"id": 8, "access_level": developer_access}]

    async def request(method, path, **kwargs):
        if (method, path) == ("GET", "groups/42"):
            return parent
        if (method, path) == ("GET", "groups/43"):
            return subgroup
        if (method, path) == ("GET", "projects/5"):
            return project
        if (method, path) == ("PUT", "projects/5"):
            assert kwargs["json"] == {"builds_access_level": "enabled", "group_runners_enabled": True}
            if readback:
                project["builds_access_level"] = "enabled"
                project["group_runners_enabled"] = True
            return project
        raise AssertionError((method, path))

    admin.request.side_effect = request
    admin.pages.side_effect = lambda path: members if path == "projects/5/members/all" else (
        [{"id": 7, "access_level": 50}] if path in ("groups/42/members", "groups/42/members/all") else None)
    return admin


@pytest.mark.parametrize("already_enabled", [False, True])
def test_operator_enables_project_ci_without_changing_developer_or_runner_access(already_enabled):
    admin = ci_admin(builds="enabled" if already_enabled else "disabled")
    result = asyncio.run(register_runner.enable_project_ci(admin, TENANT, GROUP_ID, "synthetic-product", 43, 5, 8))
    assert result["builds_access_level"] == "enabled"
    writes = [call for call in admin.request.call_args_list if call.args[0] == "PUT"]
    assert len(writes) == (0 if already_enabled else 1)
    assert all(call.args[1] == "projects/5" for call in writes)
    assert admin.pages.call_args_list[-1].args == ("projects/5/members/all",)


@pytest.mark.parametrize("project_changes,developer_id,developer_access", [
    ({"namespace": {"id": 99, "kind": "group"}}, 8, 30),
    ({"shared_runners_enabled": True}, 8, 30),
    ({"builds_access_level": "private"}, 8, 30),
    ({}, 8, 40),
    ({}, 7, 30),
    ({}, 999, 30),
])
def test_operator_refuses_wrong_project_or_developer_before_ci_mutation(project_changes, developer_id, developer_access):
    admin = ci_admin(project_changes=project_changes, developer_access=developer_access)
    with pytest.raises(register_runner.RegistrationError):
        asyncio.run(register_runner.enable_project_ci(admin, TENANT, GROUP_ID, "synthetic-product", 43, 5, developer_id))
    assert all(call.args[0] == "GET" for call in admin.request.call_args_list)


def test_ci_enablement_refuses_missing_post_write_readback():
    admin = ci_admin(readback=False)
    with pytest.raises(register_runner.RegistrationError, match="project_ci_enablement_not_verified"):
        asyncio.run(register_runner.enable_project_ci(admin, TENANT, GROUP_ID, "synthetic-product", 43, 5, 8))
    assert len([call for call in admin.request.call_args_list if call.args[0] == "PUT"]) == 1


def test_operator_cli_enables_ci_using_owned_product_binding(tmp_path, capsys):
    import json

    args, sdk, _, _, _, _ = cli_fixture(tmp_path)
    config_path = Path(args[args.index("--config") + 1])
    config = json.loads(config_path.read_text())
    config.update(product_id="synthetic-product", product_group_id=43, project_id=5, developer_user_id=8)
    config_path.write_text(json.dumps(config))
    admin = ci_admin()
    admin.__aenter__.return_value = admin
    args[-1] = "--enable-ci"
    assert register_runner.main(args, sdk=sdk, admin_factory=lambda *_args, **_kwargs: admin) == 0
    assert json.loads(capsys.readouterr().out) == {"phase": "ci_enabled", "project_id": 5}
    assert all(call.args[1] != "user/runners" for call in admin.request.call_args_list)


def test_operator_enables_only_verified_project_group_runners():
    admin = ci_admin(project_changes={"group_runners_enabled": False})
    project = asyncio.run(register_runner.enable_project_ci(admin, TENANT, GROUP_ID, "synthetic-product", 43, 5, 8))
    assert project["group_runners_enabled"] is True
    assert [call.args[:2] for call in admin.request.call_args_list if call.args[0] == "PUT"] == [("PUT", "projects/5")]
