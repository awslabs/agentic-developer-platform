"""Gateway capacity survives rendering, apply preparation and concurrent writes."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "modules/gateway/scripts/apply-gateway-deployment.py"
IMAGE = "example.test/gateway@sha256:" + "a" * 64
spec = importlib.util.spec_from_file_location("gateway_deployment_apply", SCRIPT)
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


def workflow_step(name):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    )
    return next(
        s["run"]
        for j in workflow["jobs"].values()
        for s in j.get("steps", [])
        if s.get("name") == name
    )


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Execute the maintained hosted renderer with no AWS or Kubernetes access."""
    manifest = tmp_path_factory.mktemp("gateway-render") / "deployment.yaml"
    run = workflow_step("Migrate schema then roll out new image")
    block = run[run.index("get_ssm() {") : run.index('python3 "$RUNNER_TEMP/adp-gateway-control/capacity-lock.py" verify', run.index("get_ssm() {"))]
    control = tmp_path_factory.mktemp("gateway-capacity-control")
    (control / "adp-gateway-control").mkdir()
    (control / "adp-gateway-control/render-capacity.py").write_bytes(
        (ROOT / "platform/scripts/render-gateway-capacity.py").read_bytes()
    )
    capacity = tmp_path_factory.mktemp("gateway-capacity-bundle")
    aws = control / "aws"
    aws.write_text('#!/bin/sh\necho "ParameterNotFound" >&2\nexit 255\n')
    aws.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\naws() { return 1; }\ndeclare -A ADP_SSM_VALUES=()\n" + block],
        cwd=ROOT,
        env=dict(os.environ, ENVIRONMENT="test", RELEASE_IMAGE=IMAGE, RENDERED_DEPLOYMENT=str(manifest),
                 CAPACITY_BUNDLE=str(capacity), RUNNER_TEMP=str(control), GITHUB_OUTPUT=os.devnull,
                 PATH=f"{control}:{os.environ['PATH']}"),
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.returncode == 0
    document = yaml.safe_load(manifest.read_text())
    assert document["spec"]["replicas"] == 2
    assert document["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
    return document


class API:
    """Stateful API boundary: dry-run never persists; create and update are fenced."""

    def __init__(self, manifest, replicas=12):
        self.manifest = copy.deepcopy(manifest)
        self.live = copy.deepcopy(manifest)
        self.live["metadata"].update(uid="gateway-uid", resourceVersion="31")
        self.live["spec"]["replicas"] = replicas
        self.live["metadata"]["annotations"] = {"operator.example/keep": "unmanaged"}
        self.calls = []
        self.writes = []
        self.after_get = self.after_dry_run = self.before_create = lambda: None
        self.get_error = self.apply_error = self.replace_error = None
        self.get_payload = None
        self.prepared = None

    def scale(self, count):
        self.live["spec"]["replicas"] = count
        self.live["metadata"]["resourceVersion"] = str(
            int(self.live["metadata"]["resourceVersion"]) + 1
        )

    def command(self, namespace, args, document=None):
        assert namespace == "adp-gateway"
        self.calls.append((args, copy.deepcopy(document)))
        operation = args[0]
        if operation == "create" and "--dry-run=client" in args:
            return json.dumps(self.manifest)
        if operation == "get":
            assert "--ignore-not-found" in args
            if self.get_error:
                raise RuntimeError(self.get_error)
            observed = (
                self.get_payload
                if self.get_payload is not None
                else json.dumps(self.live)
                if self.live
                else ""
            )
            self.after_get()
            return observed
        if operation == "create":
            assert "--save-config" in args
            self.before_create()
            if self.live:
                raise RuntimeError("AlreadyExists")
            self.live = copy.deepcopy(document)
        elif operation == "apply":
            assert "--dry-run=server" in args
            if self.apply_error:
                raise RuntimeError(self.apply_error)
            # This server result deliberately includes fields absent from the
            # manifest and omits a formerly managed field. The helper must use
            # it intact, not rebuild a partial update from its input manifest.
            candidate = copy.deepcopy(self.live)
            candidate["spec"].update(copy.deepcopy(document["spec"]))
            candidate["spec"]["revisionHistoryLimit"] = 10
            candidate["metadata"]["annotations"][
                "kubectl.kubernetes.io/last-applied-configuration"
            ] = json.dumps(document)
            candidate["metadata"]["managedFields"] = [{"manager": "server-fixture"}]
            candidate["status"] = {"observedGeneration": 7}
            self.prepared = copy.deepcopy(candidate)
            self.after_dry_run()
            return json.dumps(candidate)
        elif operation == "replace":
            assert args[1:3] == [
                "--raw",
                "/apis/apps/v1/namespaces/adp-gateway/deployments/bedrockgateway?fieldManager=kubectl-client-side-apply",
            ]
            assert "--force" not in args
            if self.replace_error:
                raise RuntimeError(self.replace_error)
            if any(
                document["metadata"][k] != self.live["metadata"][k]
                for k in ["uid", "resourceVersion"]
            ):
                raise RuntimeError("Conflict")
            self.live = copy.deepcopy(document)
        else:
            raise AssertionError(args)
        self.writes.append(copy.deepcopy(document))
        return "deployment.apps/bedrockgateway updated"


@pytest.fixture
def api(monkeypatch, rendered):
    fixture = API(rendered)
    monkeypatch.setattr(apply, "command", fixture.command)
    return fixture


def release():
    apply.apply_deployment("rendered.yaml", IMAGE, "adp-gateway")


@pytest.mark.parametrize("replicas", [0, 4, 12])
def test_actual_hosted_render_keeps_current_capacity_and_server_apply_result(
    api, replicas
):
    api.live["spec"]["replicas"] = replicas
    release()
    assert api.live["spec"]["replicas"] == replicas
    assert len(api.writes) == 1
    expected = copy.deepcopy(api.prepared)
    expected.pop("status")
    assert api.writes[0] == expected
    assert api.live["metadata"]["annotations"]["operator.example/keep"] == "unmanaged"
    saved = json.loads(
        api.live["metadata"]["annotations"][
            "kubectl.kubernetes.io/last-applied-configuration"
        ]
    )
    assert saved["spec"]["replicas"] == replicas
    assert "resourceVersion" not in saved["metadata"] and "uid" not in saved["metadata"]
    container = next(
        c
        for c in api.live["spec"]["template"]["spec"]["containers"]
        if c["name"] == "bedrockgateway"
    )
    assert container["image"] == IMAGE


def test_unchanged_template_allows_configmap_restart_without_extra_image_set(api, capsys):
    api.live["spec"]["template"] = copy.deepcopy(api.manifest["spec"]["template"])
    api.live["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE
    release()
    assert "unchanged" in capsys.readouterr().out
    release()
    assert "unchanged" in capsys.readouterr().out
    api.manifest["spec"]["template"]["metadata"]["annotations"]["new-release"] = "yes"
    release()
    assert "replaced" in capsys.readouterr().out


def test_omitting_manifest_replicas_cannot_trigger_apply_default_to_one(api):
    api.manifest["spec"].pop("replicas")
    release()
    desired = next(document for args, document in api.calls if args[0] == "apply")
    assert desired["spec"]["replicas"] == api.live["spec"]["replicas"] == 12


def test_terminating_gateway_is_not_updated(api):
    api.live["metadata"]["deletionTimestamp"] = "2026-10-01T00:00:00Z"
    with pytest.raises(RuntimeError, match="terminating"):
        release()
    assert not api.writes and len(api.calls) == 2


def test_namespace_cannot_change_raw_update_resource(api):
    with pytest.raises(RuntimeError, match="valid Kubernetes namespace"):
        apply.apply_deployment("rendered.yaml", IMAGE, "adp-gateway/../../other")
    assert not api.calls


def test_confirmed_absence_uses_create_at_two(api):
    api.live = None
    api.manifest["spec"].pop("replicas")
    release()
    assert api.live["spec"]["replicas"] == 2 and len(api.writes) == 1
    assert not any(args[0] in {"apply", "replace"} for args, _ in api.calls)


def test_concurrent_first_install_is_not_overwritten(api):
    existing = copy.deepcopy(api.live)
    api.live = None
    api.before_create = lambda: setattr(api, "live", existing)
    with pytest.raises(RuntimeError, match="AlreadyExists"):
        release()
    assert api.live == existing and not api.writes


@pytest.mark.parametrize("moment", ["after_get", "after_dry_run"])
@pytest.mark.parametrize("change", ["scale", "replacement", "other_update"])
def test_concurrent_change_never_overwrites_newer_deployment(api, moment, change):
    def mutate():
        if change == "scale":
            api.scale(7)
        elif change == "replacement":
            api.live["metadata"].update(uid="replacement-uid", resourceVersion="99")
        else:
            api.live["metadata"]["resourceVersion"] = "32"
            api.live["metadata"]["annotations"]["operator.example/new"] = "retain"

    setattr(api, moment, mutate)
    with pytest.raises(RuntimeError, match="Gateway changed|Conflict"):
        release()
    assert not api.writes
    assert sum(args[0] == "replace" for args, _ in api.calls) == (
        moment == "after_dry_run"
    )


@pytest.mark.parametrize("value", [None, True, "12", -1])
def test_unreadable_replica_count_never_defaults(api, value):
    api.live["spec"]["replicas"] = value
    with pytest.raises(RuntimeError, match="replicas are unreadable"):
        release()
    assert not api.writes and not any(args[0] == "apply" for args, _ in api.calls)


@pytest.mark.parametrize("error", ["Forbidden", "Timeout", "Unavailable"])
def test_failed_read_is_never_treated_as_absence(api, error):
    api.get_error = error
    with pytest.raises(RuntimeError, match=error):
        release()
    assert not api.writes and len(api.calls) == 2


@pytest.mark.parametrize("payload", ["{}", "{invalid", '{"kind":"List","items":[]}'])
def test_malformed_read_stops_before_any_update(api, payload):
    api.get_payload = payload
    with pytest.raises((RuntimeError, ValueError)):
        release()
    assert not api.writes and len(api.calls) == 2


@pytest.mark.parametrize("stage", ["apply_error", "replace_error"])
def test_failed_preparation_or_commit_has_no_unfenced_retry(api, stage):
    setattr(api, stage, "operation failed")
    with pytest.raises(RuntimeError, match="operation failed"):
        release()
    assert not api.writes
    assert sum(args[0] == "get" for args, _ in api.calls) == 1


def test_workflow_uses_maintained_helper_after_migration_before_readiness():
    run = workflow_step("Migrate schema then roll out new image")
    assert (
        run.index("scripts/migrate-before-rollout.py")
        < run.index("adp-gateway-control/apply-deployment.py")
        < run.index("kubectl rollout status")
    )
    assert '"$RENDERED_DEPLOYMENT" | kubectl apply' not in run
    assert "kubectl set image deployment/bedrockgateway" not in run
    guard = workflow_step("Load maintained deployment guards")
    assert (
        'git show "$ADP_WORKFLOW_REVISION:modules/gateway/scripts/apply-gateway-deployment.py"'
        in guard
    )


def test_real_command_refuses_nonzero_without_disclosing_provider_output(monkeypatch):
    def failed(*args, **kwargs):
        assert "--request-timeout=30s" in args[0] and kwargs["timeout"] == 45
        return subprocess.CompletedProcess(
            args[0], 1, "secret value", "provider secret"
        )

    monkeypatch.setattr(apply.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="get failed; no unfenced fallback") as error:
        apply.command("adp-gateway", ["get"])
    assert "secret" not in str(error.value)
