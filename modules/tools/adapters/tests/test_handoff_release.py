"""Prepared route transitions retain the original manifest through failures."""

import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def handoff(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("adapter_handoff_release", ROOT / "handoff_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def configuration():
    routes = {
        name: {
            "backend": "legacy" if name != "atlassian" else "disabled",
            "target_id": name + "target",
            "digest": "sha256:" + "c" * 64,
        }
        for name in ("github", "jira", "slack", "atlassian")
    }
    release = {
        "contract_version": "1.0",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "group": "tool-connectors",
        "group_generation": 1,
        "backend_generation": 1,
    }
    return {
        "plan": {
            "api_id": "api123",
            "stage": "dev",
            "before": {"status": "legacy", "release": None, "routes": routes, "deployment_id": "old123"},
            "after": {
                "status": "active",
                "release": release,
                "deployment_id": "new123",
                "routes": {name: {**route, "backend": "adapter"} for name, route in routes.items()},
            },
        },
        "selectors": {
            "account_id": "123456789012",
            "region": "us-east-1",
            "gateway_id": "gateway123",
            "candidate_stage": "candidate",
            "baseline_stage": "baseline",
            "adapter_host": "internal-adapter.elb.amazonaws.com",
            "legacy_host": "internal-gateway.elb.amazonaws.com",
            "adapter_namespace": "adapters",
            "runtime_before": {
                "maps": {name: {} for name in ("gateway", "facade")},
                "workloads": {name: {"env": [], "envFrom": []} for name in ("gateway", "facade")},
            },
            "workloads": {
                name: {"namespace": name, "deployment": name, "container": name} for name in ("gateway", "facade")
            },
        },
    }


class Transitions:
    def __init__(self, handoff):
        self.handoff, self.state, self.calls = handoff, None, []

    def __call__(self, *, action, plan, observed=None):
        self.calls.append(action)
        if action == "fence":
            self.state = {
                "status": "fenced",
                "release": plan["after"]["release"],
                "transition": {"phase": "fenced", "plan": plan, "drain_until": 120},
            }
        elif action == "rollback":
            self.state = {
                "status": "fenced",
                "release": self.handoff.restored(plan)["release"],
                "transition": {"phase": "rollback", "plan": plan, "drain_until": 120},
            }
        elif action in {"activate", "restore"}:
            self.state = {
                "status": observed["status"],
                "release": observed["release"],
                "transition": {
                    "phase": "active" if action == "activate" else "restored",
                    "plan": plan,
                    "drain_until": 120,
                },
            }
        return copy.deepcopy(self.state)


class Backend:
    def __init__(self):
        self.mutations, self.fail_forward, self.fail_restore = [], False, False

    def observe(self, stage, expected):
        return copy.deepcopy(expected)

    def verify_prepared(self, plan):
        pass

    def verify_configuration(self, release, *, active):
        pass

    def configure_release(self, release, *, configure_adapter=True):
        self.mutations.append(("configure", release))

    def switch(self, plan, deployment):
        self.mutations.append(("switch", deployment))
        if deployment == "new123" and self.fail_forward or deployment == "old123" and self.fail_restore:
            raise OSError("lost response")


def test_transition_fences_before_mutation_and_repeated_completion_does_not_write(handoff):
    state, backend, waits = Transitions(handoff), Backend(), []
    result = handoff.execute(configuration(), backend, state, clock=lambda: 100, sleep=waits.append)
    assert waits == [20]
    assert state.calls == ["read", "fence", "activate"]
    assert result["status"] == "active"
    before = copy.deepcopy(backend.mutations)
    assert handoff.execute(configuration(), backend, state) == result
    assert backend.mutations == before


def test_lost_apply_acknowledgment_restores_original_routes_but_still_fails_job(handoff):
    state, backend = Transitions(handoff), Backend()
    backend.fail_forward = True
    with pytest.raises(OSError):
        handoff.execute(configuration(), backend, state, clock=lambda: 150)
    assert state.calls == ["read", "fence", "rollback", "restore"]
    assert backend.mutations[-1] == ("switch", "old123")
    assert state.state["transition"]["phase"] == "restored"


def test_interrupted_rollback_resumes_original_state_without_reactivating(handoff):
    state, backend = Transitions(handoff), Backend()
    backend.fail_forward = backend.fail_restore = True
    with pytest.raises(OSError):
        handoff.execute(configuration(), backend, state, clock=lambda: 150)
    assert state.state["transition"]["phase"] == "rollback"
    backend.fail_restore = False
    backend.mutations.clear()
    result = handoff.execute(configuration(), backend, state, clock=lambda: 150)
    assert result["status"] == "legacy"
    assert ("switch", "new123") not in backend.mutations
    assert result["release"]["backend_generation"] == 2


def test_failed_preflight_never_fences_or_mutates(handoff):
    state, backend = Transitions(handoff), Backend()

    def refuse(plan):
        raise ValueError("unrelated route changed")

    backend.verify_prepared = refuse
    with pytest.raises(ValueError):
        handoff.execute(configuration(), backend, state)
    assert state.calls == ["read"] and backend.mutations == []


class Cloud:
    def __init__(self):
        self.document = {"securityDefinitions": {"sigv4": {"x-amazon-apigateway-authtype": "awsSigv4"}}, "paths": {}}
        for name in ("github", "jira", "slack", "atlassian"):
            path = "/internal/v1/tools/" + name + "/mcp"
            self.document["paths"][path] = {
                "post": {
                    "security": [{"sigv4": []}],
                    "x-amazon-apigateway-integration": {
                        "uri": "http://internal-adapter.elb.amazonaws.com" + path,
                        "connectionType": "VPC_LINK",
                        "httpMethod": "POST",
                        "type": "http_proxy",
                    },
                }
            }
        self.changes, self.deployment = [], "old123"

    def get_caller_identity(self):
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-trusted-deployment/run",
        }

    def describe_cluster(self, **kwargs):
        return {
            "cluster": {
                "arn": "arn:aws:eks:us-east-1:123456789012:cluster/cluster-one",
                "status": "ACTIVE",
                "endpoint": "https://cluster.example.test",
                "certificateAuthority": {"data": "certificate"},
            }
        }

    def get_stage(self, **kwargs):
        return {"deploymentId": self.deployment}

    def get_export(self, **kwargs):
        return {"body": io.BytesIO(json.dumps(self.document).encode())}

    def get_gateway_target(self, *, gatewayIdentifier, targetId):
        return {
            "targetId": targetId,
            "name": targetId.removesuffix("target"),
            "status": "READY",
            "targetConfiguration": {"mcp": {}},
            "credentialProviderConfigurations": [],
            "privateEndpoint": {},
        }

    def update_stage(self, **kwargs):
        self.changes.append(kwargs)
        self.deployment = kwargs["patchOperations"][0]["value"]


def backend(handoff, monkeypatch):
    config, cloud = configuration(), Cloud()
    environment = {
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "cluster-one",
        "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
    }

    def run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "clusters": [
                        {
                            "cluster": {
                                "server": "https://cluster.example.test",
                                "certificate-authority-data": "certificate",
                            }
                        }
                    ]
                }
            ),
        )

    monkeypatch.setattr(handoff.guard_release, "authorize", lambda env: None)
    return handoff.Backend(config, environment, clients=lambda service: cloud, run=run), cloud, config


def test_concrete_stage_writer_changes_only_deployment_under_current_guard(handoff, monkeypatch):
    actual, cloud, config = backend(handoff, monkeypatch)
    actual.switch(config["plan"], "new123")
    assert cloud.changes == [
        {
            "restApiId": "api123",
            "stageName": "dev",
            "patchOperations": [{"op": "replace", "path": "/deploymentId", "value": "new123"}],
        }
    ]
    cloud.deployment = "foreign123"
    with pytest.raises(ValueError):
        actual.switch(config["plan"], "new123")
    assert len(cloud.changes) == 1


@pytest.mark.parametrize("field", ["source_revision", "image_digest"])
def test_prepared_handoff_refuses_artifact_change_before_observation(handoff, monkeypatch, field):
    actual, cloud, config = backend(handoff, monkeypatch)
    plan = config["plan"]
    plan["before"]["status"] = "active"
    plan["before"]["release"] = copy.deepcopy(plan["after"]["release"])
    plan["before"]["release"][field] = "f" * 40 if field == "source_revision" else "sha256:" + "f" * 64
    observations = []

    def observe(stage, expected):
        observations.append(stage)
        return copy.deepcopy(expected)

    monkeypatch.setattr(actual, "observe", observe)
    monkeypatch.setattr(actual, "export", lambda stage: (stage, {"paths": {}}))
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        actual.verify_prepared(plan)
    assert observations == []
    assert cloud.changes == []


@pytest.mark.parametrize("baseline", ["initial", "active", "restored"])
def test_prepared_handoff_keeps_initial_and_same_artifact_transitions(handoff, monkeypatch, baseline):
    actual, cloud, config = backend(handoff, monkeypatch)
    plan = config["plan"]
    if baseline != "initial":
        plan["before"]["release"] = copy.deepcopy(plan["after"]["release"])
        if baseline == "active":
            plan["before"] = copy.deepcopy(plan["after"])
        for field in ("group_generation", "backend_generation"):
            plan["after"]["release"][field] += 1
    observations = []

    def observe(stage, expected):
        observations.append(stage)
        return copy.deepcopy(expected)

    monkeypatch.setattr(actual, "observe", observe)
    monkeypatch.setattr(actual, "export", lambda stage: (stage, {"basePath": stage, "paths": {}}))
    actual.verify_prepared(plan)
    assert observations == [config["selectors"]["baseline_stage"], config["selectors"]["candidate_stage"]]
    assert cloud.changes == []


def test_concrete_observer_hashes_provider_reads_and_rejects_direct_or_cross_connector_routes(handoff, monkeypatch):
    actual, cloud, config = backend(handoff, monkeypatch)
    cloud.deployment = "new123"
    observed = actual.observe("dev", config["plan"]["after"])
    assert observed["routes"]["jira"]["digest"] != config["plan"]["after"]["routes"]["jira"]["digest"]
    path = "/internal/v1/tools/atlassian/mcp"
    cloud.document["paths"][path]["post"]["x-amazon-apigateway-integration"]["uri"] = (
        "http://internal-adapter.elb.amazonaws.com/internal/v1/tools/jira/mcp"
    )
    with pytest.raises(ValueError):
        actual.observe("dev", config["plan"]["after"])


def test_handoff_contract_patch_names_the_release_field_manager(handoff, monkeypatch):
    """The handoff must not leave /data/config.json owned by kubectl-patch.

    A default-managed field is refused with a conflict when the adapter
    replacement is applied server-side under adp-tool-adapters, so the fenced
    patch names that manager itself while still changing only this one field.
    """
    actual, cloud, config = backend(handoff, monkeypatch)
    release = config["plan"]["after"]["release"]
    from test_prepare_handoff import runtime_configuration

    serialized = json.dumps(release, sort_keys=True, separators=(",", ":"))
    original_settings = runtime_configuration({**release, "group": "atlassian-foundation"})
    current = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "tool-adapters-contract", "namespace": "adapters", "resourceVersion": "8800"},
        "data": {
            "config.json": json.dumps(original_settings),
            "legacy.json": "retained",
        },
    }
    calls = []

    def kubectl(*args, payload=None, mutation=False):
        calls.append((args, mutation))
        if args[2] == "get" and args[3].startswith("deployment/"):
            container = {
                "name": args[3].removeprefix("deployment/"),
                "env": [
                    {"name": "ADP_TOOL_ADAPTER_RELEASE", "value": serialized},
                    {"name": "ADP_TOOL_ADAPTER_LEGACY_ENABLED", "value": "true"},
                ],
            }
            return json.dumps({"spec": {"template": {"spec": {"containers": [container]}}}})
        return ""

    monkeypatch.setattr(handoff.catalogue_binding, "install", lambda *_: None)
    monkeypatch.setattr(handoff.catalogue_binding, "verify", lambda *_: None)
    monkeypatch.setattr(actual, "adapter_contract", lambda selected: current)
    monkeypatch.setattr(actual, "kubectl", kubectl)
    actual.configure_release(release)
    patches = [(args, mutation) for args, mutation in calls if args[2] == "patch"]
    assert len(patches) == 1
    args, mutation = patches[0]
    assert mutation is True
    assert "configmap/tool-adapters-contract" in args
    assert "--type=json" in args
    assert "--field-manager=adp-tool-adapters" in args
    assert "--force-conflicts" not in args
    operations = json.loads(next(value for value in args if value.startswith("--patch=")).removeprefix("--patch="))
    assert operations[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "8800"}
    assert [operation["path"] for operation in operations[1:]] == ["/data/config.json"]
    settings = json.loads(operations[1]["value"])
    assert settings == {**original_settings, "release": release}
    assert "legacy.json" not in "".join(args)
    assert cloud.changes == []


def test_workflow_keeps_handoff_out_of_evaluation_and_uses_native_guard():
    workflow = (ROOT.parents[2] / ".github/workflows/tools-platform-deploy.yml").read_text()
    section = workflow.split("  adapter-handoff:", 1)[1].split("  adapter-foundation:", 1)[0]
    assert "environment: adp-tools-handoff-dev" in section
    assert "github.run_attempt == 1" in section
    assert "python3 modules/tools/adapters/guard_release.py" in section
    assert "ADP_TOOLS_ADAPTER_PREPARATION_JSON" in section
    assert "ADP_TOOLS_ADAPTER_HANDOFF_JSON" not in section
    assert (
        "if: inputs.adapter_action == 'handoff'\n        run: bash modules/tools/adapters/deploy.sh prepare" in section
    )
    assert (
        "if: inputs.adapter_action == 'rollback'\n        run: bash modules/tools/adapters/deploy.sh rollback-plan"
        in section
    )
    assert (
        section.index("deploy.sh handoff-context")
        < section.index("deploy.sh prepare")
        < section.index('deploy.sh "$ADAPTER_ACTION"')
    )
    assert "deploy.sh handoff-receipt" in section
    assert "ADP_DELIVERY_EVIDENCE_STORE: s3-oidc-v1" in workflow
    assert 'delivery delivery-evidence "$RUNNER_TEMP/adp-adapter-handoff/release/delivery.json"' in section
    assert "actions/upload-artifact" not in section
    assert "src/orchestration/delivery_contract.py" in section
    assert '"$RUNNER_TEMP/adp-deployment-evidence/runtime/bin/python" -m pip install --quiet pydantic==' in section
    assert (
        "uses: ./.github/actions/upload-ci-evidence-s3\n        with:\n"
        "          name: handoff-readback-${{ github.run_attempt }}" in section
    )
    assert "path: ${{ runner.temp }}/adp-adapter-handoff/release/handoff-readback.json" in section
    assert "./.github/actions/trusted-deployment" in section
    evaluation = (ROOT.parents[2] / ".github/workflows/tools-live-evaluation.yml").read_text()
    assert "handoff_release.py" not in evaluation
