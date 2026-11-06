"""Native binding plans preserve unrelated authority and uncertain applies."""

import copy
import json
from types import SimpleNamespace

import pytest

from installation.config import Refusal, digest
from installation.runner import atomic
from installation import shared_binding_maintenance as maintenance


OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


@pytest.fixture
def before():
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": maintenance.NAME,
            "namespace": "adp-gateway",
            "uid": "retained-shared-configmap",
            "resourceVersion": "17",
        },
        "data": {
            "AGENT_WORKER_IMAGE_DIGESTS": "unrelated-shared-worker",
            "AGENT_AUTHORITY_ENABLED": "true",
            maintenance.KEY: json.dumps(
                [
                    {
                        "domain": "superplane",
                        "current_identity_enforced": True,
                        "org_id": "one-org",
                        "worker_namespace": "superplane",
                        "worker_image_digests": [OLD],
                        "database_secret_id": "retained-secret-reference",
                    }
                ]
            ),
        },
    }


@pytest.fixture
def plan(before):
    metadata = before["metadata"]
    old = {
        "id": "adp-gateway/" + maintenance.NAME,
        "data": before["data"],
        "binary_data": {},
        "immutable": False,
        "metadata": [
            {
                "name": metadata["name"],
                "namespace": metadata["namespace"],
                "uid": metadata["uid"],
                "resource_version": metadata["resourceVersion"],
                "generation": 0,
                "annotations": {},
                "labels": {},
                "generate_name": "",
            }
        ],
    }
    new = copy.deepcopy(old)
    new["data"] = maintenance.revised_data(old["data"], NEW)
    return {
        "format_version": "1.2",
        "complete": True,
        "errored": False,
        "resource_changes": [
            {
                "address": maintenance.TARGET,
                "mode": "managed",
                "change": {
                    "actions": ["update"],
                    "before": old,
                    "after": new,
                    "after_unknown": {},
                },
            }
        ],
    }


def test_only_enrolls_new_worker_without_changing_shared_authority(plan, before):
    desired = maintenance.check_plan(plan, before, NEW)
    assert desired["AGENT_WORKER_IMAGE_DIGESTS"] == "unrelated-shared-worker"
    assert desired["AGENT_AUTHORITY_ENABLED"] == "true"
    assert json.loads(desired[maintenance.KEY])[0]["worker_image_digests"] == [NEW]


def test_binding_override_retains_observed_settings_and_resource_guards(before):
    before["data"]["ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE"] = "retained-authorities"
    original = copy.deepcopy(before)
    override = maintenance.maintenance_override(before, NEW)
    target = override["resource"].pop("kubernetes_config_map")
    assert override == maintenance.PRESERVATION
    # No replacement of metadata, count, dependencies or lifecycle/preconditions.
    assert set(target) == {"worker_gateway"}
    assert set(target["worker_gateway"]) == {"data"}
    data = target["worker_gateway"]["data"]
    assert data["ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE"] == "retained-authorities"
    assert data["AGENT_AUTHORITY_ENABLED"] == "true"
    assert data["AGENT_WORKER_IMAGE_DIGESTS"] == "unrelated-shared-worker"
    assert json.loads(data[maintenance.KEY])[0]["worker_image_digests"] == [NEW]
    assert before == original


def test_accepts_computed_server_resource_version(plan, before):
    change = plan["resource_changes"][0]["change"]
    change["after"]["metadata"][0]["resource_version"] = None
    change["after_unknown"] = {"metadata": [{"resource_version": True}]}
    maintenance.check_plan(plan, before, NEW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "another-org"),
        ("worker_namespace", "another-namespace"),
        ("database_secret_id", "another-secret"),
        ("current_identity_enforced", False),
        ("worker_image_digests", [OLD, NEW]),
    ],
)
def test_refuses_binding_changes_beyond_exact_single_digest(plan, before, field, value):
    after = plan["resource_changes"][0]["change"]["after"]["data"]
    binding = json.loads(after[maintenance.KEY])
    binding[0][field] = value
    after[maintenance.KEY] = json.dumps(binding)
    with pytest.raises(Refusal, match="exceeds"):
        maintenance.check_plan(plan, before, NEW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("AGENT_AUTHORITY_ENABLED", "false"),
        ("AGENT_WORKER_IMAGE_DIGESTS", "another-shared-worker"),
        ("UNRELATED_NEW_SETTING", "value"),
    ],
)
def test_refuses_unrelated_configmap_settings(plan, before, field, value):
    plan["resource_changes"][0]["change"]["after"]["data"][field] = value
    with pytest.raises(Refusal, match="exceeds"):
        maintenance.check_plan(plan, before, NEW)


@pytest.mark.parametrize(
    "actions", [["update"], ["create"], ["delete"], ["read"], ["delete", "create"]]
)
def test_refuses_any_unrelated_managed_mutation(plan, before, actions):
    plan["resource_changes"].append(
        {
            "address": "aws_iam_policy.agent_authority_boundary[0]",
            "mode": "managed",
            "change": {"actions": actions},
        }
    )
    with pytest.raises(Refusal, match="unrelated"):
        maintenance.check_plan(plan, before, NEW)


def test_accepts_observed_dependencies_without_rewriting_them(plan, before):
    plan["resource_changes"].extend(
        [
            {
                "address": "aws_iam_policy.agent_authority_boundary[0]",
                "mode": "managed",
                "change": {"actions": ["no-op"]},
            },
            {
                "address": "data.aws_iam_role.operator",
                "mode": "data",
                "change": {"actions": ["read"]},
            },
        ]
    )
    maintenance.check_plan(plan, before, NEW)


@pytest.fixture
def targeted_plan(plan):
    # Terraform 1.14 marks a successful -target plan incomplete globally.
    plan.update(terraform_version="1.14.0", complete=False, applyable=True)
    plan["resource_changes"].append(
        {
            "address": 'module.owner[0].terraform_data.registration["superplane"]',
            "mode": "managed",
            "change": {"actions": ["no-op"]},
        }
    )
    plan["checks"] = [
        {
            "address": {
                "kind": "resource",
                "to_display": "kubernetes_config_map.worker_gateway",
            },
            "status": "pass",
            "instances": [
                {"address": {"to_display": maintenance.TARGET}, "status": "pass"}
            ],
        },
        {
            "address": {
                "kind": "resource",
                "to_display": "module.owner.terraform_data.registration",
            },
            "status": "pass",
            "instances": [
                {
                    "address": {
                        "to_display": 'module.owner[0].terraform_data.registration["superplane"]'
                    },
                    "status": "pass",
                }
            ],
        },
        {
            "address": {
                "kind": "resource",
                "to_display": "aws_lambda_function.unselected",
            },
            "status": "unknown",
        },
        {
            "address": {"kind": "var", "to_display": "var.unselected"},
            "status": "unknown",
        },
    ]
    return plan


def test_applyable_targeted_plan_retains_native_apply_and_completion_rules(
    tmp_path, targeted_plan, before
):
    operation = NativeOperation(tmp_path, before, targeted_plan)
    assert operation.apply("c" * 64)["status"] == "binding-and-native-state-observed"
    assert operation.native_calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("applyable", False),
        ("applyable", None),
        ("applyable", "true"),
        ("errored", True),
        ("errored", None),
        ("deferred_changes", [{"reason": "resource_config_unknown"}]),
    ],
)
def test_incomplete_plan_requires_explicit_success_without_deferrals(
    targeted_plan, before, field, value
):
    targeted_plan[field] = value
    with pytest.raises(Refusal, match="incomplete"):
        maintenance.check_plan(targeted_plan, before, NEW)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("status", ["unknown", "fail", "error"])
@pytest.mark.parametrize("instance", [False, True])
def test_targeted_plan_requires_passing_selected_resource_and_instance_checks(
    targeted_plan, before, index, status, instance
):
    check = targeted_plan["checks"][index]
    (check["instances"][0] if instance else check)["status"] = status
    with pytest.raises(Refusal, match="selected check"):
        maintenance.check_plan(targeted_plan, before, NEW)


@pytest.mark.parametrize("status", ["fail", "error", "unexpected"])
def test_targeting_does_not_hide_failed_checks(targeted_plan, before, status):
    targeted_plan["checks"][2]["status"] = status
    with pytest.raises(Refusal, match="selected check"):
        maintenance.check_plan(targeted_plan, before, NEW)


@pytest.mark.parametrize(
    "fault",
    ["unknown-target", "unrelated-change", "replace-target", "import", "drift-delete"],
)
def test_targeted_incomplete_plan_preserves_closed_mutation_guards(
    targeted_plan, before, fault
):
    target = targeted_plan["resource_changes"][0]
    if fault == "unknown-target":
        target["change"]["after_unknown"] = {"data": True}
    elif fault == "unrelated-change":
        targeted_plan["resource_changes"][1]["change"]["actions"] = ["update"]
    elif fault == "replace-target":
        target["change"]["actions"] = ["delete", "create"]
    elif fault == "import":
        target["change"]["importing"] = {"id": "another-object"}
    else:
        targeted_plan["resource_drift"] = [{"change": {"actions": ["delete"]}}]
    with pytest.raises(Refusal):
        maintenance.check_plan(targeted_plan, before, NEW)


@pytest.mark.parametrize("field", ["uid", "name", "namespace", "resource_version"])
def test_refuses_changed_configmap_identity(plan, before, field):
    change = plan["resource_changes"][0]["change"]
    change["before"]["metadata"][0][field] = "another-value"
    change["after"]["metadata"][0][field] = "another-value"
    with pytest.raises(Refusal, match="observed shared binding"):
        maintenance.check_plan(plan, before, NEW)


@pytest.mark.parametrize("unknown", [{"data": True}, {"metadata": [{"uid": True}]}])
def test_refuses_unknown_configuration_or_identity(plan, before, unknown):
    plan["resource_changes"][0]["change"]["after_unknown"] = unknown
    with pytest.raises(Refusal, match="unresolved"):
        maintenance.check_plan(plan, before, NEW)


def test_refuses_state_moves_and_missing_resources(plan, before):
    plan["resource_changes"][0]["previous_address"] = "old.address"
    with pytest.raises(Refusal, match="move or import"):
        maintenance.check_plan(plan, before, NEW)
    del plan["resource_changes"][0]["previous_address"]
    plan["resource_drift"] = [{"change": {"actions": ["delete"]}}]
    with pytest.raises(Refusal, match="missing resources"):
        maintenance.check_plan(plan, before, NEW)


def test_refuses_absent_target_or_duplicate_address(plan, before):
    plan["resource_changes"] *= 2
    with pytest.raises(Refusal, match="Duplicate"):
        maintenance.check_plan(plan, before, NEW)
    plan["resource_changes"] = []
    with pytest.raises(Refusal, match="omitted"):
        maintenance.check_plan(plan, before, NEW)


def test_preservation_overrides_cannot_erase_future_lifecycle_guards(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(maintenance, "WEBHOOK", tmp_path)
    (tmp_path / "agent-authority-boundary.tf").write_text(
        'resource "aws_iam_policy" "agent_authority_boundary" {\n  name = "fixed"\n}\n'
    )
    (tmp_path / "secrets.tf").write_text(
        'resource "aws_secretsmanager_secret" "marker_signing_key" {\n  name = "fixed"\n}\n'
    )
    assert len(maintenance.preservation_sources()) == 2
    (tmp_path / "secrets.tf").write_text(
        'resource "aws_secretsmanager_secret" "marker_signing_key" {\n  lifecycle { prevent_destroy = true }\n}\n'
    )
    with pytest.raises(Refusal, match="gained lifecycle"):
        maintenance.preservation_sources()


class NativeOperation(maintenance.Maintenance):
    def __init__(self, directory, before, plan):
        self.directory = directory
        self.env, self.inputs = {"account_id": "123456789012"}, {"retained": True}
        self.lock = {"images": {"superplane-paid-worker": NEW}}
        self.baseline = {
            "configmap": copy.deepcopy(before),
            "domain_objects": [{"paused": True}],
            "operator_role_id": "immutable-owner",
            "state_lineage": "retained-lineage",
            "state_serial": 42,
            "preserved_dependencies": {"boundary": "unchanged", "marker": "unchanged"},
        }
        self.current = copy.deepcopy(self.baseline)
        self.document = copy.deepcopy(plan)
        self.native_calls = 0
        self.fail_native = False
        self.recorded = copy.deepcopy(plan["resource_changes"][0]["change"]["before"])
        atomic(directory / "desired-inputs.tfvars.json", {"desired": NEW})
        atomic(
            directory / "receipt.json",
            {
                "kind": "shared-worker-binding-maintenance-v1",
                "status": "planned",
                "plan_sha256": self.plan_hash(),
                "environment_sha256": digest(self.env),
                "release_sha256": digest(self.lock),
                "inputs_sha256": digest(self.inputs),
                "desired_inputs_sha256": digest({"desired": NEW}),
                "source": self.source(),
                "override": maintenance.maintenance_override(before, NEW),
                "before": self.baseline,
            },
        )

    def plan_hash(self):
        return "c" * 64

    def source(self):
        return {"revision": "d" * 40}

    def inspect(self):
        return copy.deepcopy(self.current)

    def native_plan(self):
        return self.document

    def state(self):
        return {
            "resources": [
                {
                    "type": "kubernetes_config_map",
                    "name": "worker_gateway",
                    "instances": [{"attributes": self.recorded}],
                }
            ]
        }

    def run(self, args, **kwargs):
        assert args[:2] == ["terraform", "apply"]
        assert "-lock-timeout=30s" in args
        assert "-lock=true" in args
        self.native_calls += 1
        self.current["configmap"]["data"] = maintenance.revised_data(
            self.current["configmap"]["data"], NEW
        )
        self.current["configmap"]["metadata"]["resourceVersion"] = "18"
        self.current["state_serial"] += 1
        if self.fail_native:
            raise Refusal("native result unavailable")
        self.recorded = copy.deepcopy(
            self.document["resource_changes"][0]["change"]["after"]
        )
        return ""


def test_native_apply_keeps_owner_state_and_receipt_together(tmp_path, before, plan):
    operation = NativeOperation(tmp_path, before, plan)
    receipt = operation.apply("c" * 64)
    assert operation.native_calls == 1
    assert receipt["status"] == "binding-and-native-state-observed"
    with pytest.raises(Refusal, match="observed, never"):
        operation.apply("c" * 64)


def test_modified_binding_override_refuses_before_native_apply(tmp_path, before, plan):
    operation = NativeOperation(tmp_path, before, plan)
    path = tmp_path / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["override"]["resource"]["kubernetes_config_map"]["worker_gateway"]["data"][
        "AGENT_AUTHORITY_ENABLED"
    ] = "false"
    atomic(path, receipt)
    with pytest.raises(Refusal, match="inputs or source changed"):
        operation.apply("c" * 64)
    assert operation.native_calls == 0


def test_lost_native_reply_is_not_repeated_or_claimed_complete(tmp_path, before, plan):
    operation = NativeOperation(tmp_path, before, plan)
    operation.fail_native = True
    with pytest.raises(Refusal, match="unavailable"):
        operation.apply("c" * 64)
    assert (
        json.loads((tmp_path / "receipt.json").read_text())["status"]
        == "native-apply-pending"
    )
    with pytest.raises(Refusal, match="observed, never"):
        operation.apply("c" * 64)
    assert operation.native_calls == 1
    with pytest.raises(Refusal, match="both live binding and owner state"):
        operation.observe()
    operation.recorded = copy.deepcopy(plan["resource_changes"][0]["change"]["after"])
    assert operation.observe()["status"] == "binding-and-native-state-observed"
    assert operation.native_calls == 1


def test_stale_plan_refuses_before_native_apply(tmp_path, before, plan):
    operation = NativeOperation(tmp_path, before, plan)
    operation.current["configmap"]["metadata"]["resourceVersion"] = "changed"
    with pytest.raises(Refusal, match="changed after planning"):
        operation.apply("c" * 64)
    assert operation.native_calls == 0


def test_wrong_approval_and_modified_overlay_refuse(tmp_path, before, plan):
    operation = NativeOperation(tmp_path, before, plan)
    with pytest.raises(Refusal, match="approval"):
        operation.apply("f" * 64)
    atomic(tmp_path / "desired-inputs.tfvars.json", {"desired": OLD})
    with pytest.raises(Refusal, match="inputs or source changed"):
        operation.apply("c" * 64)
    assert operation.native_calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("operator_role_id", "new-owner"),
        ("state_lineage", "new-lineage"),
        ("domain_objects", [{"paused": False}]),
        ("preserved_dependencies", {"boundary": "changed", "marker": "unchanged"}),
    ],
)
def test_observation_refuses_changed_ownership_or_quiescence(
    tmp_path, before, plan, field, value
):
    operation = NativeOperation(tmp_path, before, plan)
    operation.apply("c" * 64)
    operation.current[field] = value
    with pytest.raises(Refusal, match="Ownership or paused domain"):
        operation.observe()


def test_observation_refuses_unrelated_metadata_drift(tmp_path, before, plan):
    operation = NativeOperation(tmp_path, before, plan)
    operation.apply("c" * 64)
    operation.current["configmap"]["metadata"]["labels"] = {"foreign": "change"}
    with pytest.raises(Refusal, match="metadata or binary data"):
        operation.observe()


def test_namespace_mismatch_refuses_before_cloud_inspection(
    environment, release, tmp_path
):
    environment["gateway_namespace"] = "expected-gateway"
    with pytest.raises(Refusal, match="Gateway namespace differ"):
        maintenance.Maintenance(
            environment, release, {"gateway_namespace": "other-gateway"}, tmp_path
        )


@pytest.fixture
def source_reader(tmp_path, monkeypatch):
    runtime, tooling = "a" * 40, "b" * 40
    webhook = tmp_path / "webhook"
    webhook.mkdir()
    (webhook / "owner.tf").write_text("# retained Terraform source\n")
    monkeypatch.setattr(maintenance, "ROOT", tmp_path)
    monkeypatch.setattr(maintenance, "WEBHOOK", webhook)
    monkeypatch.setattr(
        maintenance, "preservation_sources", lambda: {"owner.tf": "unchanged"}
    )
    operation = object.__new__(maintenance.Maintenance)
    operation.lock = {
        "source_revision": runtime,
        "images": {"superplane-paid-worker": OLD},
    }
    operation.maintenance_source_revision = tooling
    state = {
        "head": tooling,
        "dirty": "",
        "ancestor": runtime,
        "changes": [
            "modules/domain-apps/superplane/installation/shared_binding_maintenance.py"
        ],
        "merged": {runtime, tooling},
        "merged_reads": [],
    }

    def run(args):
        if args[:2] == ["git", "rev-parse"]:
            return state["head"]
        if args[:2] == ["git", "status"]:
            return state["dirty"]
        if args[:2] == ["git", "ls-files"]:
            return "webhook/owner.tf\n"
        if args[:2] == ["git", "merge-base"]:
            assert args[2:] == [runtime, state["head"]]
            return state["ancestor"]
        if args[:2] == ["git", "diff"]:
            assert args[2:4] == ["--no-renames", "--name-only"]
            return "\n".join(state["changes"])
        assert args[:2] == ["gh", "api"]
        candidate = args[2].split("/")[-1].split("...")[0]
        state["merged_reads"].append(candidate)
        return json.dumps(
            {
                "status": "ahead" if candidate in state["merged"] else "diverged",
                "merge_base_commit": {"sha": candidate},
            }
        )

    operation.run = run
    return operation, state, runtime, tooling, webhook


def test_separately_pinned_merged_tooling_preserves_runtime_release(source_reader):
    operation, state, runtime, tooling, _ = source_reader
    original = copy.deepcopy(operation.lock)
    result = operation.source()
    assert result["revision"] == result["maintenance_source_revision"] == tooling
    assert result["runtime_revision"] == runtime
    assert result["maintenance_source_changes"] == state["changes"]
    assert set(state["merged_reads"]) == {runtime, tooling}
    assert operation.lock == original


def test_without_explicit_tooling_pin_source_must_match_runtime(source_reader):
    operation, state, runtime, _, _ = source_reader
    operation.maintenance_source_revision = None
    with pytest.raises(Refusal, match="exact clean release"):
        operation.source()
    state["head"] = runtime
    assert operation.source() == {
        "revision": runtime,
        "preservation_sources": {"owner.tf": "unchanged"},
    }


@pytest.mark.parametrize(
    "fault",
    [
        "pin",
        "head",
        "dirty",
        "ancestor",
        "runtime-unmerged",
        "tooling-unmerged",
        "untracked-terraform",
    ],
)
def test_tooling_source_refuses_unpinned_unmerged_or_changed_checkout(
    source_reader, fault
):
    operation, state, runtime, tooling, webhook = source_reader
    if fault == "pin":
        operation.maintenance_source_revision = "main"
    elif fault == "head":
        state["head"] = "c" * 40
    elif fault == "dirty":
        state["dirty"] = " M installation/shared_binding_maintenance.py"
    elif fault == "ancestor":
        state["ancestor"] = "c" * 40
    elif fault == "runtime-unmerged":
        state["merged"].remove(runtime)
    elif fault == "tooling-unmerged":
        state["merged"].remove(tooling)
    else:
        (webhook / "unexpected.tf").write_text("# refuse untracked configuration\n")
    with pytest.raises(Refusal):
        operation.source()


@pytest.mark.parametrize(
    "changed",
    [
        "modules/domain-apps/superplane/installation/runner.py",
        "modules/domain-apps/superplane/installation/config.py",
        "modules/domain-apps/superplane/installation/new_module.py",
        "modules/domain-apps/superplane/tests/installer_checks/test_other.py",
        "modules/domain-apps/superplane/src/superplane-api/app/installation.py",
        "modules/agent-factory/webhook-ingress/infra/main.tf",
        "modules/domain-apps/superplane/releases/superplane.lock.yaml",
        "unrelated.md",
    ],
)
def test_tooling_source_allowlist_cannot_change_runtime_terraform_or_other_files(
    source_reader, changed
):
    operation, state, *_ = source_reader
    state["changes"].append(changed)
    with pytest.raises(Refusal, match="runtime, Terraform or unrelated"):
        operation.source()


@pytest.fixture
def dependency_reader():
    policy = {
        "Arn": "arn:aws:iam::123456789012:policy/boundary",
        "PolicyId": "retained-policy",
        "DefaultVersionId": "v4",
    }
    version = {
        "VersionId": "v4",
        "IsDefaultVersion": True,
        "Document": {"Version": "2012-10-17", "Statement": []},
    }
    secret = {
        "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:marker",
        "Name": "marker",
        "Tags": [{"Key": "Purpose", "Value": "marker"}],
    }
    tags = {"Tags": [{"Key": "Owner", "Value": "shared"}], "IsTruncated": False}
    calls = []

    def response(name, value):
        def call(**kwargs):
            calls.append(name)
            return copy.deepcopy(value)

        return call

    clients = {
        "iam": SimpleNamespace(
            get_policy=response("get_policy", {"Policy": policy}),
            get_policy_version=response(
                "get_policy_version", {"PolicyVersion": version}
            ),
            list_policy_tags=response("list_policy_tags", tags),
        ),
        "secretsmanager": SimpleNamespace(
            describe_secret=response("describe_secret", secret)
        ),
    }
    operation = object.__new__(maintenance.Maintenance)
    operation.session = SimpleNamespace(client=lambda name: clients[name])
    state = {
        "resources": [
            {
                "type": "aws_iam_policy",
                "name": "agent_authority_boundary",
                "instances": [{"attributes": {"arn": policy["Arn"]}}],
            },
            {
                "type": "aws_secretsmanager_secret",
                "name": "marker_signing_key",
                "instances": [
                    {"attributes": {"arn": secret["ARN"], "recovery_window_in_days": 7}}
                ],
            },
        ]
    }
    return operation, state, policy, version, secret, tags, calls


def test_dependencies_observe_live_versions_tags_and_record_retention_truthfully(
    dependency_reader,
):
    operation, state, _, _, _, _, calls = dependency_reader
    result = operation.dependencies(state)
    assert result["worker_boundary"]["default_version"] == "v4"
    assert result["worker_boundary"]["tags"] == [{"Key": "Owner", "Value": "shared"}]
    assert result["marker_secret"]["terraform_recovery_window_in_days"] == 7
    assert "get_secret_value" not in calls
    assert calls == [
        "get_policy",
        "get_policy_version",
        "list_policy_tags",
        "describe_secret",
    ]


@pytest.mark.parametrize(
    "fault", ["default-version", "truncated-tags", "deleted-secret"]
)
def test_dependencies_refuse_ambiguous_current_state(dependency_reader, fault):
    operation, state, policy, version, secret, tags, _ = dependency_reader
    if fault == "default-version":
        policy["DefaultVersionId"] = "v5"
    elif fault == "truncated-tags":
        tags["IsTruncated"] = True
    else:
        secret["DeletedDate"] = "pending-deletion"
    with pytest.raises(Refusal, match="dependency identity or live deletion"):
        operation.dependencies(state)
