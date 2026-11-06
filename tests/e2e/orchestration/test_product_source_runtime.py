"""Product source deployment observations use distinct authenticated provider paths."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.scenarios import release, runtime
from tests.e2e.orchestration.scenarios.delivery import LiveSession
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.manifest import RuntimeTarget
from tests.e2e.orchestration.scenarios.definitions import fixture_test

pytest_plugins = (
    "tests.e2e.orchestration.test_product_epic_wiring",
    "tests.e2e.orchestration.test_release",
)

FIRST = "a" * 40
SECOND = "b" * 40
ENGINE = "c" * 40
DIGEST = "sha256:" + "d" * 64
WORKFLOWS = {
    "first": ".github/workflows/api-deploy.yml",
    "second": ".github/workflows/client-deploy.yml",
}


def source_manifest(config):
    engine = RuntimeTarget(
        cluster="fixture",
        namespace="fixture",
        deployment="engine",
        container="engine",
        ecr_repository="engine",
    )
    api = engine.model_copy(update={"deployment": "api", "container": "api"})
    client = engine.model_copy(
        update={"kind": "job", "deployment": None, "container": "client"}
    )
    return NS(
        runtime={"engine": engine, "worker": engine, "api": api, "client": client},
        source_workflows=WORKFLOWS,
        deployment_workflows=[".github/workflows/gateway-deploy.yml"],
    )


def source_prs():
    return [{"pr": {"merge_commit_sha": sha}} for sha in (FIRST, SECOND)]


def source_runs(config):
    return {
        target.repository: [
            {
                "path": WORKFLOWS[ref],
                "head_sha": sha,
                "conclusion": "success",
                "status": "completed",
                "repository": {"full_name": target.repository},
                "id": number,
                "run_attempt": 1,
            }
        ]
        for (ref, sha, number), target in zip(
            (("first", FIRST, 101), ("second", SECOND, 102)),
            config.product_epic.stories,
        )
    }


def test_source_deployment_observations_reject_wrong_repo_revision_or_job(
    wired, tmp_path, monkeypatch
):
    config, _ = wired
    config = replace(config, versions={**config.versions, "engine": ENGINE})
    manifest = source_manifest(config)
    session = LiveSession(
        config, manifest, NS(path=tmp_path, qualification_id="q-0123456789abcdef"), {}
    )
    runs = source_runs(config)
    session.client.pages = Mock(
        side_effect=lambda path, **_: runs[
            path.split("/actions/runs", 1)[0].removeprefix("/repos/")
        ]
    )
    session.runtime_observations = {
        "first": {"kind": "deployment", "actual_revision": FIRST, "digest": DIGEST},
        "second": {
            "kind": "job",
            "actual_revision": SECOND,
            "digest": DIGEST,
            "workflow_run_id": 102,
            "workflow_run_attempt": 1,
            "job_uid": "job-uid",
            "pods": ["pod-uid"],
        },
    }
    from tests.e2e.orchestration.scenarios import runtime

    current = deepcopy(session.runtime_observations)

    def read_source(client, target, **source):
        if target is manifest.runtime["client"]:
            assert source == {
                "repository": config.story_repository(2),
                "source_revision": SECOND,
                "workflow_path": WORKFLOWS["second"],
            }
            return current["second"]
        assert not source
        return (
            {"actual_revision": ENGINE}
            if target is manifest.runtime["engine"]
            else current["first"]
        )

    monkeypatch.setattr(runtime, "read_runtime", read_source)
    evidence = session.deployments(source_prs())
    assert [row["repository"] for row in evidence] == [
        target.repository for target in config.product_epic.stories
    ]
    assert [row["runtime"]["actual_revision"] for row in evidence] == [FIRST, SECOND]
    assert session.client.pages.call_count == 2
    current["second"]["workflow_run_id"] = 999
    with pytest.raises(AssertionError):
        session.deployments(source_prs())
    current["second"]["workflow_run_id"] = 102
    for key, value in (
        ("job_uid", "replacement-job"),
        ("pods", ["replacement-pod"]),
        ("workflow_run_attempt", 2),
    ):
        original = current["second"][key]
        current["second"][key] = value
        with pytest.raises(AssertionError):
            session.deployments(source_prs())
        current["second"][key] = original
    runs[config.product_epic.stories[1].repository][0]["repository"]["full_name"] = (
        config.product_epic.stories[0].repository
    )
    with pytest.raises(AssertionError):
        session.deployments(source_prs())
    runs[config.product_epic.stories[1].repository][0]["repository"]["full_name"] = (
        config.product_epic.stories[1].repository
    )
    current["first"]["digest"] = "sha256:" + "e" * 64
    with pytest.raises(AssertionError):
        session.deployments(source_prs())
    current["first"]["digest"] = DIGEST
    runs[config.product_epic.stories[0].repository][0]["path"] = WORKFLOWS["second"]
    with pytest.raises(AssertionError):
        session.deployments(source_prs())
    runs[config.product_epic.stories[0].repository][0]["path"] = WORKFLOWS["first"]
    current["first"]["actual_revision"] = ENGINE
    with pytest.raises(AssertionError):
        session.deployments(source_prs())


def test_missing_product_runtime_or_workflow_is_not_a_deployment(wired, tmp_path):
    config, _ = wired
    manifest = source_manifest(config)
    session = LiveSession(
        config, manifest, NS(path=tmp_path, qualification_id="q-0123456789abcdef"), {}
    )
    with pytest.raises(Unsupported, match="runtime not observed"):
        session.deployments(source_prs())
    manifest.runtime.pop("api")
    with pytest.raises(Unsupported, match="source runtime"):
        session.deployments(source_prs())


@pytest.fixture
def completed_job(monkeypatch):
    target = RuntimeTarget(
        kind="job",
        cluster="fixture",
        namespace="test",
        container="client",
        ecr_repository="repo",
    )
    image = "000000000000.dkr.ecr.us-east-1.amazonaws.com/repo@" + DIGEST
    job = {
        "metadata": {
            "uid": "job-uid",
            "name": "client-102-1",
            "namespace": "test",
            "annotations": {
                "adp/source-revision": SECOND,
                "adp/workflow-run-id": "102",
                "adp/workflow-run-attempt": "1",
            },
        },
        "status": {
            "succeeded": 1,
            "conditions": [{"type": "Complete", "status": "True"}],
        },
        "spec": {
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [{"name": "client", "image": image}],
                }
            }
        },
    }
    pod = {
        "metadata": {
            "uid": "pod-uid",
            "namespace": "test",
            "ownerReferences": [
                {
                    "uid": "job-uid",
                    "controller": True,
                    "kind": "Job",
                    "name": "client-102-1",
                }
            ],
        },
        "spec": {"containers": [{"name": "client", "image": image}]},
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "client",
                    "imageID": "docker-pullable://" + image,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ],
        },
    }
    ecr = Mock()
    ecr.describe_images.return_value = {
        "imageDetails": [{"imageTags": [SECOND], "imageDigest": DIGEST}]
    }
    kube = NS(
        identity={"Account": "000000000000"},
        role="arn:aws:iam::000000000000:role/fixture",
        cluster={"arn": "arn:aws:eks:us-east-1:000000000000:cluster/fixture"},
        bounded=NS(),
        session=NS(client=Mock(return_value=ecr)),
    )
    kube.get = Mock(
        side_effect=lambda path: job if "/jobs/" in path else {"items": [pod]}
    )
    client = Mock()
    workflow = {
        "path": WORKFLOWS["second"],
        "head_sha": SECOND,
        "conclusion": "success",
        "status": "completed",
        "repository": {"full_name": "example/client"},
        "id": 102,
        "run_attempt": 1,
    }
    client.pages.return_value = [workflow]

    def authenticated_kube(actual, cluster):
        assert actual is client and cluster == target.cluster
        return kube

    monkeypatch.setattr(runtime, "ScopedKubernetes", authenticated_kube)
    return NS(
        client=client, target=target, job=job, pod=pod, kube=kube, workflow=workflow
    )


def observe_job(fixture):
    return runtime.read_runtime(
        fixture.client,
        fixture.target,
        repository="example/client",
        source_revision=SECOND,
        workflow_path=WORKFLOWS["second"],
    )


def test_completed_client_job_reads_bound_role_workflow_and_ecr_revision(completed_job):
    observed = observe_job(completed_job)
    assert observed["actual_revision"] == SECOND
    assert observed["workflow_run_id"] == 102
    assert observed["digest"] == DIGEST
    assert observed["deployment"] == "client-102-1"
    assert observed["job_uid"] == "job-uid"
    assert observed["pods"] == ["pod-uid"]
    completed_job.client.pages.assert_called_once_with(
        f"/repos/example/client/actions/runs?head_sha={SECOND}", key="workflow_runs"
    )
    completed_job.kube.get.assert_any_call(
        "/apis/batch/v1/namespaces/test/jobs/client-102-1"
    )
    assert all(
        call.args[0].startswith(("/apis/batch/", "/api/v1/"))
        for call in completed_job.kube.get.call_args_list
    )
    assert (
        completed_job.kube.session.client.return_value.describe_images.call_args.kwargs[
            "repositoryName"
        ]
        == "repo"
    )
    completed_job.job["metadata"]["annotations"]["adp/source-revision"] = FIRST
    with pytest.raises(Unsupported, match="workflow attribution"):
        observe_job(completed_job)
    completed_job.job["metadata"]["annotations"]["adp/source-revision"] = SECOND
    completed_job.pod["status"]["phase"] = "Running"
    with pytest.raises(Unsupported, match="Pod or image"):
        observe_job(completed_job)


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "ambiguous",
        "wrong-repo",
        "wrong-revision",
        "wrong-workflow",
        "failed",
        "running",
        "bad-id",
        "bad-attempt",
    ],
)
def test_job_requires_unique_authenticated_successful_workflow(completed_job, failure):
    workflow = completed_job.workflow
    if failure == "missing":
        completed_job.client.pages.return_value = []
    elif failure == "ambiguous":
        completed_job.client.pages.return_value = [workflow, {**workflow, "id": 103}]
    elif failure == "wrong-repo":
        workflow["repository"]["full_name"] = "example/foreign"
    elif failure == "wrong-revision":
        workflow["head_sha"] = FIRST
    elif failure == "wrong-workflow":
        workflow["path"] = WORKFLOWS["first"]
    elif failure == "failed":
        workflow["conclusion"] = "failure"
    elif failure == "running":
        workflow["status"] = "in_progress"
    elif failure == "bad-id":
        workflow["id"] = True
    else:
        workflow["run_attempt"] = 0
    with pytest.raises(Unsupported, match="workflow"):
        observe_job(completed_job)
    completed_job.kube.get.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        "old-name",
        "run",
        "attempt",
        "revision",
        "owner",
        "owner-kind",
        "owner-name",
        "pod-uid",
        "missing-pod",
        "ambiguous-pod",
        "continued-pods",
        "missing-job",
    ],
)
def test_dynamic_job_rejects_wrong_identity_or_ambiguous_pods(completed_job, failure):
    job, pod = completed_job.job, completed_job.pod
    if failure == "old-name":
        job["metadata"]["name"] = "client"
    elif failure in {"run", "attempt", "revision"}:
        annotation = {
            "run": "workflow-run-id",
            "attempt": "workflow-run-attempt",
            "revision": "source-revision",
        }[failure]
        job["metadata"]["annotations"]["adp/" + annotation] = "999"
    elif failure in {"owner", "owner-kind", "owner-name"}:
        key = {"owner": "uid", "owner-kind": "kind", "owner-name": "name"}[failure]
        pod["metadata"]["ownerReferences"][0][key] = "foreign"
    elif failure == "pod-uid":
        pod["metadata"].pop("uid")
    elif failure == "missing-job":
        from tests.e2e.orchestration.scenarios.kubernetes import KubernetesUnavailable

        completed_job.kube.get.side_effect = KubernetesUnavailable(404)
    else:
        pods = {"items": [] if failure == "missing-pod" else [pod, deepcopy(pod)]}
        if failure == "continued-pods":
            pods = {"items": [pod], "metadata": {"continue": "next"}}
        completed_job.kube.get.side_effect = (
            lambda path: job if "/jobs/" in path else pods
        )
    with pytest.raises(Unsupported):
        observe_job(completed_job)


def test_job_rerun_resolves_exact_new_attempt(completed_job):
    completed_job.workflow["run_attempt"] = 2
    completed_job.job["metadata"]["name"] = "client-102-2"
    completed_job.job["metadata"]["annotations"]["adp/workflow-run-attempt"] = "2"
    completed_job.pod["metadata"]["ownerReferences"][0]["name"] = "client-102-2"
    assert observe_job(completed_job)["workflow_run_attempt"] == 2
    completed_job.kube.get.assert_any_call(
        "/apis/batch/v1/namespaces/test/jobs/client-102-2"
    )


def test_job_manifest_rejects_fixed_name_and_nonjob_requires_name():
    target = dict(
        cluster="fixture", namespace="test", container="client", ecr_repository="repo"
    )
    with pytest.raises(ValueError, match="resolved from its workflow run"):
        RuntimeTarget(kind="job", deployment="client", **target)
    with pytest.raises(ValueError, match="require a workload name"):
        RuntimeTarget(**target)
    assert RuntimeTarget(kind="job", **target).deployment is None


@pytest.mark.parametrize(
    "failure", [None, "foreign-repository", "unmerged", "wrong-revision"]
)
def test_delivery_barrier_resolves_job_from_its_bound_source_pr(
    wired, completed_job, tmp_path, failure
):
    config, _ = wired
    manifest = source_manifest(config)
    manifest.runtime["client"] = completed_job.target
    session = LiveSession(
        config, manifest, NS(path=tmp_path, qualification_id="q-0123456789abcdef"), {}
    )
    session.client = completed_job.client
    repository = config.story_repository(2)
    completed_job.workflow["repository"]["full_name"] = repository
    session.client.get.return_value = {
        "merged": failure != "unmerged",
        "merge_commit_sha": FIRST if failure == "wrong-revision" else SECOND,
    }
    data = {
        "graph": {
            "nodes": [
                {"node_ref": "first", "state": "passed"},
                {
                    "node_ref": "second",
                    "state": "passed",
                    "bound_pull_request": {
                        "repo": "example/foreign"
                        if failure == "foreign-repository"
                        else repository,
                        "pr_number": 10,
                    },
                },
                {"node_ref": "verify", "state": "passed"},
                {"node_ref": "release", "state": "passed"},
            ]
        },
        "cost": {"status": "known", "partial": False, "amount_usd": 0},
    }
    session.snapshot = Mock(return_value=data)
    session.observe_faults = Mock()
    session.observe_worker_loss = Mock()
    session.observe_wait_and_ci = Mock()
    session.runtime_observations["first"] = {"actual_revision": FIRST}
    assert session.await_completion() is data
    if failure is None:
        observed = session.runtime_observations["second"]
        assert observed["deployment"] == "client-102-1"
        assert observed["actual_revision"] == SECOND
        assert observed["job_uid"] == "job-uid" and observed["pods"] == ["pod-uid"]
        session.client.get.assert_called_once_with(
            f"/repos/{repository}/pulls/10", github=True
        )
    else:
        assert "second" not in session.runtime_observations
        assert session.observation_errors["second"] == "Unsupported"
        completed_job.kube.get.assert_not_called()


@pytest.mark.parametrize("failure", ["digest", "revision", "ambiguous-revision"])
def test_job_requires_exact_ecr_digest_and_source(completed_job, failure):
    image = completed_job.kube.session.client.return_value.describe_images.return_value[
        "imageDetails"
    ][0]
    if failure == "digest":
        image["imageDigest"] = "sha256:" + "e" * 64
    elif failure == "revision":
        image["imageTags"] = [FIRST]
    else:
        image["imageTags"].append(FIRST)
    with pytest.raises(Unsupported, match="ECR"):
        observe_job(completed_job)


def test_product_e1_separates_engine_target_from_api_source(
    release_fixture, wired, monkeypatch
):
    context = release_fixture
    config, _ = wired
    config = replace(config, versions={**config.versions, "engine": ENGINE})
    context.context.actual_revision = FIRST
    engine = NS(namespace="fixture")
    api = NS(namespace="fixture", deployment="api")
    monkeypatch.setattr(
        release,
        "load_manifest",
        lambda config: (NS(runtime={"engine": engine, "api": api}), "hash"),
    )
    engine_runtime = {**context.runtime, "actual_revision": ENGINE}
    api_runtime = {**context.runtime, "actual_revision": FIRST, "digest": DIGEST}
    release.read_runtime.side_effect = lambda client, target: (
        engine_runtime if target is engine else api_runtime
    )
    observed = release.evaluate_release(
        config=config, inventory=context.inventory, context=context.context
    )
    assert observed["actual_revision"] == FIRST
    assert {row["outcome"] for row in observed["criteria"]} == {"pass"}
    request = release.execute_source.call_args.args[-1]
    assert request == {
        "qualification_id": "q-release0123456789",
        "tests": fixture_test(0, "q-release0123456789", product=True),
        "product": True,
    }
    assert release.execute_source.call_args.args[1] is api
    engine_runtime["actual_revision"] = FIRST
    with pytest.raises(Unsupported, match="release/target differs"):
        release.evaluate_release(
            config=config, inventory=context.inventory, context=context.context
        )


def test_product_manifest_requires_source_targets_and_distinct_workflows(
    wired, tmp_path, monkeypatch
):
    import json
    from tests.e2e.orchestration.scenarios import manifest as loader

    config, _ = wired
    document = json.loads(
        (loader.ROOT / "tests/e2e/orchestration/manifest.example.json").read_text()
    )
    document["execution_policy"]["repository_ids"] = [
        config.repository,
        *(target.repository for target in config.product_epic.stories),
    ]
    document["execution_policy"]["environment_connection_ids"] = [
        config.connection_ref,
        *(target.connection_ref for target in config.product_epic.stories),
    ]
    document["execution_policy"]["limits"]["max_wall_clock_seconds"] = (
        config.max_duration_seconds
    )
    document["execution_policy"]["limits"]["max_attempts_per_node"] = config.max_runs
    config = replace(
        config,
        scenario_manifest="manifest.json",
        versions={"engine": ENGINE, "worker": FIRST, "harness": SECOND},
    )
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="requires reviewed API and client source"):
        loader.load_manifest(config)
    targets = source_manifest(config).runtime
    document["runtime"]["api"] = targets["api"].model_dump()
    document["runtime"]["client"] = targets["client"].model_dump()
    document["source_workflows"] = WORKFLOWS
    path.write_text(json.dumps(document))
    verified, _ = loader.load_manifest(config)
    assert verified.runtime["api"].kind == "deployment"
    assert verified.runtime["client"].kind == "job"
    document["runtime"]["api"] = document["runtime"]["engine"]
    with pytest.raises(ValueError, match="must not alias engine or worker"):
        loader.Manifest.model_validate(document)
    document["runtime"]["api"] = targets["api"].model_dump()
    document["source_workflows"]["second"] = WORKFLOWS["first"]
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="distinct reviewed API and client workflows"):
        loader.load_manifest(config)


def test_product_deployed_code_probe_runs_api_repo_tests_not_gateway(
    tmp_path, monkeypatch
):
    import sys
    from pathlib import Path
    from tests.e2e.orchestration.scenarios import release_process

    qualification_id = "q-source0123456789"
    source = (
        tmp_path / "qualification-api/qualification" / qualification_id / "pricing.py"
    )
    source.parent.mkdir(parents=True)
    source.write_text(
        "def discounted_cents(cents, basis_points):\n"
        "    if cents < 0 or not 0 <= basis_points <= 10000:\n"
        "        raise ValueError('invalid discount')\n"
        "    return (cents * (10000 - basis_points) + 5000) // 10000\n"
    )
    original_path = Path
    monkeypatch.setattr(
        release_process,
        "Path",
        lambda value: (
            tmp_path / value.removeprefix("/app/")
            if isinstance(value, str) and value.startswith("/app/")
            else original_path(value)
        ),
    )
    before = list(sys.path)
    try:
        observed = release_process.run(
            {
                "qualification_id": qualification_id,
                "product": True,
                "tests": fixture_test(0, qualification_id, product=True),
            }
        )
        assert observed["successful"] and observed["tests_run"] == 3
        source.unlink()
        with pytest.raises(ValueError, match="fixture code is absent"):
            release_process.run(
                {
                    "qualification_id": qualification_id,
                    "product": True,
                    "tests": fixture_test(0, qualification_id, product=True),
                }
            )
    finally:
        sys.path[:] = before
        for key in list(sys.modules):
            if key.startswith("qualification." + qualification_id):
                sys.modules.pop(key)


def test_product_native_probe_does_not_accept_source_revision_as_engine(monkeypatch):
    import time
    from tests.e2e.orchestration.scenarios import native_probe

    session = NS(
        started=time.monotonic(),
        manifest=NS(native_faults=True, runtime={"engine": object()}),
        config=NS(
            product_epic=object(),
            versions={"engine": ENGINE},
            max_duration_seconds=60,
        ),
        runtime_observations={"first": {"actual_revision": FIRST}},
        client=Mock(),
    )
    monkeypatch.setattr(
        native_probe, "read_runtime", lambda client, target: {"actual_revision": FIRST}
    )
    with pytest.raises(Unsupported, match="native probe target changed"):
        native_probe.NativeProbe(session).request("execution", "tick-restart")
