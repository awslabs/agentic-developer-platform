"""Exercise the real release helper against modeled infrastructure boundaries."""

from copy import deepcopy
import json

import pytest

from tests.e2e.tools import atlassian_release_scenario as release
from modules.tools.adapters.tests.test_handoff_release import configuration, Transitions

ACCOUNT = "123456789012"
REGION = "us-east-1"
BINDING = {
    "source_revision": "a" * 40,
    "definition_revision": "e" * 40,
    "run_id": "1234",
    "run_attempt": "1",
    "release_sha256": "f" * 64,
}
ENV = {
    "ADP_TOOLS_ACCOUNT_ID": ACCOUNT,
    "ADP_TOOLS_AWS_REGION": REGION,
    "ADP_SOURCE_REVISION": BINDING["source_revision"],
    "ADP_DEFINITION_REVISION": BINDING["definition_revision"],
    "ADP_CORRELATION": "orch:00000000-0000-0000-0000-000000000001",
    "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-tools-handoff-trusted-deployment",
    "GITHUB_RUN_ID": "1233",
}


def config():
    return {
        "account": ACCOUNT,
        "region": REGION,
        "gateway_url": "https://gateway.example.test",
        "handoff": configuration(),
    }


def setup(tmp_path, monkeypatch):
    handoff, guard = release.helpers()
    execute = handoff.execute
    monkeypatch.setattr(
        handoff,
        "execute",
        lambda *a, **kw: execute(
            *a, **kw, clock=lambda: 120, sleep=lambda _: pytest.fail("unbounded drain")
        ),
    )
    monkeypatch.setattr(guard, "authorize", lambda _: None)
    tmp_path.chmod(0o700)
    selected = config()
    path, journal = tmp_path / "config.json", tmp_path / "journal.json"
    release._write(path, selected)
    deployment = {
        "metadata": {
            "name": "tool-adapters",
            "namespace": "adapters",
            "uid": "original-adapter",
            "resourceVersion": "1",
            "generation": 1,
        },
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "tool-adapters", "image": "example@sha256:" + "b" * 64}
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
        },
    }
    state = {
        "deployment": deployment,
        "stage": "old123",
        "patches": [],
        "configured": [],
        "switches": [],
        "health_reads": 0,
    }

    class Backend:
        def __init__(self, value):
            self.config, self.selectors = value, value["selectors"]

        def verify_prepared(self, plan):
            assert plan["before"]["deployment_id"] == "old123"
            assert plan["after"]["deployment_id"] == "new123"

        def observe(self, stage, expected):
            assert stage == "dev"
            if expected["deployment_id"] != state["stage"]:
                raise ValueError("actual stage differs")
            return deepcopy(expected)

        def configure_release(self, value, *, configure_adapter=True):
            state["configured"].append(deepcopy(value))

        def switch(self, plan, deployment):
            state["switches"].append(deployment)
            state["stage"] = deployment

        def kubectl(self, *args, mutation=False):
            if "get" in args:
                return json.dumps(state["deployment"])
            assert mutation and "patch" in args
            patch = json.loads(
                next(
                    arg.removeprefix("--patch=")
                    for arg in args
                    if arg.startswith("--patch=")
                )
            )
            assert (
                patch[0]["value"] == state["deployment"]["metadata"]["resourceVersion"]
            )
            replicas = patch[1]["value"]
            state["patches"].append(replicas)
            state["deployment"]["spec"]["replicas"] = replicas
            state["deployment"]["metadata"]["generation"] += 1
            state["deployment"]["metadata"]["resourceVersion"] = str(
                state["deployment"]["metadata"]["generation"]
            )
            state["deployment"]["status"] = {
                "observedGeneration": state["deployment"]["metadata"]["generation"],
                **dict.fromkeys(
                    (
                        "replicas",
                        "updatedReplicas",
                        "readyReplicas",
                        "availableReplicas",
                    ),
                    replicas,
                ),
            }
            return "{}"

    class Core(Transitions):
        def stale(self, plan):
            assert self.state["release"]["group_generation"] == 3
            return {"path": "/api/tools/adapter-release/transition", "status": 409}

    core = Core(handoff)

    def read(url, **kwargs):
        if url.endswith("/api/health"):
            state["health_reads"] += 1
            return {"status_code": 200, "body": {"status": "healthy"}}
        assert url.startswith(
            "http://internal-adapter.elb.amazonaws.com/internal/v1/tools/"
        )
        return {
            "status_code": 403,
            "body": {"detail": {"error": "adapter_authority_required"}},
        }

    return path, journal, selected, state, Backend, core, read


def execute(parts, **overrides):
    path, journal, _, _, factory, core, read = parts
    return release.rehearse_release(
        path,
        journal,
        environment=ENV,
        backend_factory=factory,
        transition=core,
        read=overrides.get("read", read),
    )


def collect(parts):
    path, journal, _, _, factory, _, read = parts
    return release.collect_release(
        path,
        binding=BINDING,
        account=ACCOUNT,
        region=REGION,
        environment=ENV,
        journal_path=journal,
        backend_factory=factory,
        read=read,
    )


def test_real_handoff_helper_interrupts_rolls_back_redeploys_and_restores_adapter(
    tmp_path, monkeypatch
):
    parts = setup(tmp_path, monkeypatch)
    _, _, selected, state, _, core, _ = parts
    journal = execute(parts)
    assert journal["cleanup"] == "confirmed"
    assert state["patches"] == [0, 2]
    assert state["switches"] == ["new123", "old123", "new123"]
    assert core.state["release"]["group_generation"] == 3
    report = collect(parts)
    qualified = release.qualify_release(report, manifest=selected, binding=BINDING)
    assert [row["status"] for row in qualified["cases"]] == ["pass"] * 6 + [
        "pending"
    ] * 3
    assert qualified["full_ac06"] == "pending"
    mutations = deepcopy((state["patches"], state["switches"], state["configured"]))
    execute(parts)
    collect(parts)
    assert (state["patches"], state["switches"], state["configured"]) == mutations


def test_health_failure_during_outage_still_restores_then_resumes_without_another_outage(
    tmp_path, monkeypatch
):
    parts = setup(tmp_path, monkeypatch)
    state, read = parts[3], parts[6]

    def fail_during_outage(url, **kwargs):
        if url.endswith("/api/health") and state["deployment"]["spec"]["replicas"] == 0:
            raise TimeoutError("health response lost")
        return read(url, **kwargs)

    with pytest.raises(TimeoutError):
        execute(parts, read=fail_during_outage)
    assert state["patches"] == [0, 2]
    execute(parts)
    assert state["patches"] == [0, 2]
    report = collect(parts)
    row = next(
        item for item in report["cases"] if item["case"] == "core-health-through-outage"
    )
    assert row["status"] == "pending"


def test_lost_restoration_resumes_exact_original_replicas_before_continuing(
    tmp_path, monkeypatch
):
    parts = setup(tmp_path, monkeypatch)
    original = release.set_replicas
    failed = []

    def interrupted(backend, snapshot, count, **kwargs):
        if count == 2 and not failed:
            failed.append(True)
            raise OSError("lost process before restoration")
        return original(backend, snapshot, count, **kwargs)

    monkeypatch.setattr(release, "set_replicas", interrupted)
    with pytest.raises(OSError):
        execute(parts)
    assert parts[3]["deployment"]["spec"]["replicas"] == 0
    result = execute(parts)
    assert result["cleanup"] == "confirmed" and parts[3]["patches"] == [0, 2]


def test_refuses_changed_template_without_overwriting_another_deployment(
    tmp_path, monkeypatch
):
    parts = setup(tmp_path, monkeypatch)
    state, factory = parts[3], parts[4]
    backend = factory(parts[2]["handoff"])
    original = release.replica_snapshot(state["deployment"])
    state["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
        "foreign"
    )
    with pytest.raises(ValueError, match="identity or template changed"):
        release.set_replicas(backend, original, 0)
    assert state["patches"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "route",
        "partial",
        "outage",
        "rollback",
        "stale",
        "direct",
        "promotion",
        "binding",
    ],
)
def test_independent_reader_rejects_promoted_or_substituted_release_evidence(
    tmp_path, monkeypatch, fault
):
    parts = setup(tmp_path, monkeypatch)
    execute(parts)
    report = collect(parts)
    events = report["rehearsal"]["observations"]
    if fault == "route":
        report["observations"]["route"]["routes"]["jira"]["digest"] = (
            "sha256:" + "0" * 64
        )
    elif fault == "partial":
        events["interruption"]["state"]["status"] = "active"
    elif fault == "outage":
        events["outage_down"]["deployment"]["status"]["readyReplicas"] = 2
    elif fault == "rollback":
        events["rollback"]["route"]["routes"]["atlassian"]["backend"] = "legacy"
    elif fault == "stale":
        events["stale"]["status"] = 503
    elif fault == "direct":
        report["observations"]["denials"][0]["status_code"] = 404
    elif fault == "binding":
        report["binding"] = {**BINDING, "run_id": "999"}
    else:
        report["cases"][-1].update(status="pass", reason=None)
    with pytest.raises(ValueError):
        release.qualify_release(report, manifest=parts[2], binding=BINDING)


def test_manifest_rejects_arbitrary_host_and_atlassian_legacy_fallback():
    selected = config()
    selected["handoff"]["selectors"]["adapter_host"] = "attacker.test"
    with pytest.raises(ValueError):
        release.manifest(selected, account=ACCOUNT, region=REGION)
    selected = config()
    selected["handoff"]["plan"]["before"]["routes"]["atlassian"]["backend"] = "legacy"
    with pytest.raises(ValueError):
        release.manifest(selected, account=ACCOUNT, region=REGION)


def test_existing_release_guard_runs_before_any_probe_or_mutation(
    tmp_path, monkeypatch
):
    parts = setup(tmp_path, monkeypatch)
    _, guard = release.helpers()

    def refuse(_):
        raise guard.GuardRefused("no current release authority")

    monkeypatch.setattr(guard, "authorize", refuse)
    with pytest.raises(guard.GuardRefused):
        execute(parts)
    assert parts[3]["patches"] == [] and parts[3]["health_reads"] == 0
    assert not parts[1].exists()
