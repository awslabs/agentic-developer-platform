"""Exercise the maintained observer at the provider and clean-checkout boundaries."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from modules.tools.scripts.release_window import verify_manifest, verify_window
from tests.e2e.tools import release_observer as observer

ROOT = Path(__file__).resolve().parents[3]
ACCOUNT = "123456789012"
REGION = "us-east-1"
SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-tools-facade@{DIGEST}"
NOW = datetime(2026, 1, 2, 3, 4, 30, tzinfo=UTC)
TIME = "2026-01-02T03:04:00Z"


def owned(kind, name):
    return {"kind": kind, "name": name, "uid": name + "-uid", "controller": True}


def metadata(name, namespace, *owners):
    return {
        "name": name,
        "namespace": namespace,
        "uid": name + "-uid",
        "generation": 1,
        "ownerReferences": list(owners),
    }


def pod(name, namespace, container, parent, *, completed=False):
    return {
        "metadata": metadata(name, namespace, parent),
        "spec": {"containers": [{"name": container, "image": IMAGE}]},
        "status": {
            "phase": "Succeeded" if completed else "Running",
            "containerStatuses": [
                {
                    "name": container,
                    "image": IMAGE,
                    "imageID": "docker-pullable://" + IMAGE,
                    "ready": not completed,
                    "state": {"terminated": {"exitCode": 0, "finishedAt": TIME}}
                    if completed
                    else {"running": {}},
                }
            ],
        },
    }


def fixtures():
    selectors = {
        "version": 1,
        "account": ACCOUNT,
        "region": REGION,
        "cluster": "fictional-cluster",
        "components": {
            "gateway": {
                "namespace": "adp-gateway",
                "deployment": "bedrockgateway",
                "container": "bedrockgateway",
            },
            "facade and reconciler": {
                "namespace": "adp-gateway",
                "deployment": "tools-facade",
                "container": "tools-facade",
                "cronjob": "tools-facade-reconciler",
                "reconciler_container": "reconciler",
            },
            "shared Lambda and Browser": {
                "function_arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:fictional-shared",
                "namespace": "fictional-agents",
                "deployment": "fictional-browser",
                "container": "browser",
            },
            "platform targets": {
                "gateway_id": "fictional-gateway",
                "target_ids": ["target-one", "target-two"],
            },
        },
    }
    responses = {}
    for selected in list(selectors["components"].values())[:3]:
        name, namespace, container = (selected[key] for key in ("deployment", "namespace", "container"))
        spec = {"containers": [{"name": container, "image": IMAGE}]}
        responses[f"deployment/{namespace}/{name}"] = {
            "metadata": metadata(name, namespace),
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": {"spec": spec}},
            "status": dict(
                observedGeneration=1, replicas=1, readyReplicas=1, updatedReplicas=1, availableReplicas=1
            ),
        }
        responses[f"replicaset/{namespace}/{name}-rs"] = {
            "metadata": metadata(name + "-rs", namespace, owned("Deployment", name))
        }
        responses[f"pods/{namespace}/app={name}"] = {
            "items": [pod(name + "-pod", namespace, container, owned("ReplicaSet", name + "-rs"))]
        }
    cron, namespace, job = "tools-facade-reconciler", "adp-gateway", "tools-facade-reconciler-123"
    spec = {"containers": [{"name": "reconciler", "image": IMAGE}]}
    responses[f"cronjob/{namespace}/{cron}"] = {
        "metadata": metadata(cron, namespace),
        "spec": {"suspend": False, "jobTemplate": {"spec": {"template": {"spec": spec}}}},
    }
    responses[f"jobs/{namespace}/"] = {
        "items": [
            {
                "metadata": metadata(job, namespace, owned("CronJob", cron)),
                "spec": {"template": {"spec": spec}},
                "status": {"succeeded": 1, "completionTime": TIME},
            }
        ]
    }
    responses[f"pods/{namespace}/batch.kubernetes.io/job-name={job}"] = {
        "items": [pod(job + "-pod", namespace, "reconciler", owned("Job", job), completed=True)]
    }
    responses["sts/get-caller-identity"] = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/adp-tools-trusted-deployment/deploy-51-1",
    }
    responses["lambda/get-function"] = {
        "Configuration": {
            "FunctionArn": selectors["components"]["shared Lambda and Browser"]["function_arn"],
            "State": "Active",
            "LastUpdateStatus": "Successful",
        },
        "Code": {"ResolvedImageUri": IMAGE},
    }
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/fictional-gateway"
    responses["bedrock-agentcore-control/get-gateway"] = {
        "gatewayId": "fictional-gateway",
        "gatewayArn": gateway_arn,
        "status": "READY",
    }
    responses["bedrock-agentcore-control/list-gateway-targets"] = {
        "items": [{"targetId": target} for target in ("target-one", "target-two")]
    }
    for target in ("target-one", "target-two"):
        responses["bedrock-agentcore-control/get-gateway-target/" + target] = {
            "gatewayArn": gateway_arn,
            "targetId": target,
            "status": "READY",
            "lastSynchronizedAt": TIME,
        }
    expected = {
        "gateway": {"image": DIGEST},
        "facade and reconciler": {"image": DIGEST, "reconciler": DIGEST},
        "shared Lambda and Browser": {"lambda": DIGEST, "browser": DIGEST},
        "platform targets": {
            "targets": "sha256:"
            + hashlib.sha256(
                b'[["target-one","2026-01-02T03:04:00+00:00"],["target-two","2026-01-02T03:04:00+00:00"]]'
            ).hexdigest()
        },
    }
    return selectors, responses, expected


def response_key(arguments):
    if arguments[0] == "aws":
        assert arguments[1:3] in [
            ("sts", "get-caller-identity"),
            ("lambda", "get-function"),
            ("bedrock-agentcore-control", "get-gateway"),
            ("bedrock-agentcore-control", "list-gateway-targets"),
            ("bedrock-agentcore-control", "get-gateway-target"),
        ]
        key = "/".join(arguments[1:3])
        if "--target-id" in arguments:
            key += "/" + arguments[arguments.index("--target-id") + 1]
        return key
    assert arguments[0] == "kubectl" and "get" in arguments and arguments[1] == "--kubeconfig"
    kind = arguments[arguments.index("get") + 1]
    namespace = arguments[arguments.index("-n") + 1]
    name = (
        arguments[arguments.index("-l") + 1] if "-l" in arguments else arguments[arguments.index("get") + 2]
    )
    return f"{kind}/{namespace}/{'' if name == '-n' else name}"


class Provider:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, *arguments):
        key = response_key(arguments)
        self.calls.append(key)
        return copy.deepcopy(self.responses[key])


def configured(tmp_path, selectors, provider):
    return observer.Observer(
        selectors,
        account=ACCOUNT,
        region=REGION,
        cluster="fictional-cluster",
        kubeconfig=tmp_path / "kubeconfig",
        evidence=tmp_path / "evidence",
        read=provider,
        now=lambda: NOW,
    )


def manifest(expected):
    workflows = {
        "gateway": "gateway-deploy.yml",
        "tool-adapters": "tools-platform-deploy.yml",
        "facade and reconciler": "tools-facade-deploy.yml",
        "shared Lambda and Browser": "shared-agentcore-tools-deploy.yml",
        "platform targets": "tools-platform-deploy.yml",
    }
    components = {}
    for name, values in expected.items():
        receipt = {
            "component": name,
            "workflow": workflows[name],
            "source_revision": SOURCE,
            "workflow_revision": SOURCE,
            "environment": "dev",
            "account": ACCOUNT,
            "run_id": 18,
            "run_attempt": 1,
            "mode": "apply",
            "evidence_type": "independent-runtime",
            "component_digests": values,
            "runtime_digests": values,
        }
        if name == "shared Lambda and Browser":
            receipt.update(
                images={key: {"built": IMAGE, "runtime": IMAGE} for key in values},
                state_sha256="c" * 64,
                config={
                    key: "d" * 64 for key in ["backend_sha256", "tfvars_sha256", "state_identity_sha256"]
                },
            )
        components[name] = {"source_revision": SOURCE, "expected_runtime": values, "receipt": receipt}
    return {
        "source_revision": SOURCE,
        "account": ACCOUNT,
        "environment": "dev",
        "paths": ["modules/tools/agentcore/agentcore_tools/handler.py"] +
                 (["modules/tools/adapters/deploy.py"] if "tool-adapters" in expected else []),
        "components": components,
    }


def test_both_phases_read_each_provider_again_without_manifest_access(tmp_path):
    selectors, responses, expected = fixtures()
    provider = Provider(responses)
    bound = configured(tmp_path, selectors, provider)
    assert bound.readback("before") == expected
    calls = list(provider.calls)
    assert bound.readback("after") == expected
    assert provider.calls == calls + calls
    assert {
        "lambda/get-function",
        "jobs/adp-gateway/",
        "bedrock-agentcore-control/get-gateway-target/target-one",
        "replicaset/adp-gateway/bedrockgateway-rs",
        "replicaset/fictional-agents/fictional-browser-rs",
    } <= set(calls)
    for phase in ("before", "after"):
        evidence = tmp_path / "evidence" / f"runtime-{phase}.json"
        assert evidence.stat().st_mode & 0o077 == 0
        assert json.loads(evidence.read_text())["runtime"] == expected


@pytest.mark.parametrize(
    "case",
    [
        "foreign-pod-owner",
        "wrong-runtime-image",
        "unready-pod",
        "mixed-replicas",
        "terminating-pod",
        "missing-jobs",
        "old-reconciler",
        "foreign-job-owner",
        "cron-image-mismatch",
        "lambda-updating",
        "wrong-account",
        "missing-target",
        "extra-target",
        "duplicate-target",
        "target-not-ready",
        "incomplete-page",
    ],
)
def test_unverifiable_runtime_is_rejected(tmp_path, case):
    selectors, responses, _ = fixtures()
    runtime_pod = responses["pods/adp-gateway/app=bedrockgateway"]["items"][0]
    completed = responses["pods/adp-gateway/batch.kubernetes.io/job-name=tools-facade-reconciler-123"][
        "items"
    ][0]
    if case == "foreign-pod-owner":
        runtime_pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif case == "wrong-runtime-image":
        runtime_pod["status"]["containerStatuses"][0]["imageID"] = IMAGE.replace("b" * 64, "e" * 64)
    elif case == "unready-pod":
        runtime_pod["status"]["containerStatuses"][0]["ready"] = False
    elif case == "mixed-replicas":
        responses["deployment/adp-gateway/bedrockgateway"]["status"]["replicas"] = 2
    elif case == "terminating-pod":
        runtime_pod["metadata"]["deletionTimestamp"] = TIME
    elif case == "missing-jobs":
        responses["jobs/adp-gateway/"]["items"] = []
    elif case == "old-reconciler":
        completed["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = (
            "2025-01-01T00:00:00Z"
        )
    elif case == "foreign-job-owner":
        completed["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif case == "cron-image-mismatch":
        responses["cronjob/adp-gateway/tools-facade-reconciler"]["spec"]["jobTemplate"]["spec"]["template"][
            "spec"
        ]["containers"][0]["image"] = IMAGE.replace("b" * 64, "e" * 64)
    elif case == "lambda-updating":
        responses["lambda/get-function"]["Configuration"]["LastUpdateStatus"] = "InProgress"
    elif case == "wrong-account":
        responses["sts/get-caller-identity"]["Account"] = "000000000000"
    elif case == "missing-target":
        responses["bedrock-agentcore-control/list-gateway-targets"]["items"].pop()
    elif case == "extra-target":
        responses["bedrock-agentcore-control/list-gateway-targets"]["items"].append({"targetId": "foreign"})
    elif case == "duplicate-target":
        responses["bedrock-agentcore-control/list-gateway-targets"]["items"].append(
            {"targetId": "target-one"}
        )
    elif case == "target-not-ready":
        responses["bedrock-agentcore-control/get-gateway-target/target-one"]["status"] = "UPDATING"
    else:
        responses["bedrock-agentcore-control/list-gateway-targets"]["nextToken"] = "unread"
    with pytest.raises(ValueError):
        configured(tmp_path, selectors, Provider(responses)).readback("before")


def test_second_phase_observes_changed_lambda_and_fails_manifest_comparison(tmp_path):
    selectors, responses, expected = fixtures()
    provider = Provider(responses)
    bound = configured(tmp_path, selectors, provider)

    def changing(phase):
        if phase == "after":
            responses["lambda/get-function"]["Code"]["ResolvedImageUri"] = IMAGE.replace("b" * 64, "e" * 64)
        return bound.readback(phase)

    with pytest.raises(ValueError, match="after independent runtime"):
        verify_window(
            manifest(expected),
            changing,
            source=SOURCE,
            account=ACCOUNT,
            gateway_image=IMAGE,
            facade_image=IMAGE,
        )
    assert provider.calls.count("lambda/get-function") == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(account="000000000000"),
        lambda x: x["components"]["gateway"].update(expected_runtime=DIGEST),
        lambda x: x["components"].update(unknown={}),
        lambda x: x["components"]["platform targets"].update(target_ids=["target-one", "target-one"]),
    ],
)
def test_selectors_cannot_supply_observations_or_escape_target_binding(tmp_path, change):
    selectors, responses, _ = fixtures()
    change(selectors)
    provider = Provider(responses)
    with pytest.raises(ValueError):
        configured(tmp_path, selectors, provider)
    assert not provider.calls


def test_permission_failure_is_closed_and_does_not_disclose_provider_stderr(monkeypatch):
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="AccessDenied private-fixture-credential")

    monkeypatch.setattr(observer.subprocess, "run", denied)
    with pytest.raises(ValueError, match="read permissions") as caught:
        observer.command("aws", "lambda", "get-function")
    assert "private-fixture-credential" not in str(caught.value)


# A separate executable process implements only the observed read calls. It is
# used by the unmodified workflow shell and runner in a fresh exported checkout.
FAKE_CLI = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['TEST_CHECKOUT'])
from tests.e2e.tools.test_release_observer import response_key
args = (Path(sys.argv[0]).name, *sys.argv[1:])
with open(os.environ['TEST_CALLS'], 'a') as f: f.write(json.dumps(args) + '\n')
responses = json.loads(Path(os.environ['TEST_RESPONSES']).read_text())
if args[:3] == ('aws', 'eks', 'update-kubeconfig'):
    path = Path(args[args.index('--kubeconfig') + 1]); path.write_text('owned fixture config'); path.chmod(0o600); print('{}'); sys.exit()
if args[:3] == ('aws', 'ecr', 'describe-images'):
    print('sha256:' + 'b' * 64); sys.exit()
if args[0] == 'kubectl' and '--kubeconfig' not in args:
    assert os.environ['KUBECONFIG'].endswith('/tools-eval-kubeconfig')
    args = ('kubectl', '--kubeconfig', os.environ['KUBECONFIG'], *args[1:])
key = response_key(args)
if key == os.environ.get('TEST_DENY'):
    print('AccessDenied private-fixture-credential', file=sys.stderr); sys.exit(1)
value = responses[key]
if '--query' in args: value = value[args[args.index('--query') + 1]]
print(json.dumps(value) if '--query' not in args else value)
"""


@pytest.mark.parametrize("deny", ["", "lambda/get-function", "jobs/adp-gateway/"])
def test_clean_checkout_workflow_materializes_config_runs_observer_and_cleans_up(tmp_path, deny):
    checkout = tmp_path / "checkout"
    # Export source without local modules, bytecode, site packages or private files.
    files = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "tests/__init__.py",
            "tests/e2e/__init__.py",
            "tests/e2e/tools",
            "modules/tools/scripts",
            "modules/tools/platform/scripts",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in files:
        dest = checkout / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    assert not list((checkout / "tests/e2e/tools").glob("private_release_*"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("aws", "kubectl"):
        path = bin_dir / name
        path.write_text(FAKE_CLI)
        path.chmod(0o700)
    selectors, responses, expected = fixtures()
    # Use a current completion timestamp for the real subprocess clock.
    completion = datetime.now(UTC).isoformat()
    responses["pods/adp-gateway/batch.kubernetes.io/job-name=tools-facade-reconciler-123"]["items"][0][
        "status"
    ]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = completion
    gateway = responses["deployment/adp-gateway/bedrockgateway"]["spec"]["template"]["spec"]["containers"][0]
    gateway["image"] = IMAGE.replace("/adp-tools-facade@", "/adp-gateway@")
    gateway_pod = responses["pods/adp-gateway/app=bedrockgateway"]["items"][0]
    gateway_pod["spec"]["containers"][0]["image"] = gateway["image"]
    gateway_pod["status"]["containerStatuses"][0].update(image=gateway["image"], imageID=gateway["image"])
    response_file = tmp_path / "responses.json"
    response_file.write_text(json.dumps(responses))
    env_file = tmp_path / "github-env"
    env_file.touch()
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": "",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(env_file),
        "EXPECTED_ACCOUNT": ACCOUNT,
        "EXPECTED_ROLE": f"arn:aws:iam::{ACCOUNT}:role/adp-tools-trusted-deployment",
        "AWS_REGION": REGION,
        "EKS_CLUSTER": "fictional-cluster",
        "TOOLS_NAMESPACE": "adp-gateway",
        "GITHUB_RUN_ID": "51",
        "GITHUB_RUN_ATTEMPT": "1",
        "SOURCE_REVISION": SOURCE,
        "DEFINITION_REVISION": SOURCE,
        "GATEWAY_REVISION": SOURCE,
        "GITHUB_SHA": SOURCE,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "SCENARIO_FILTER": "SCN-8068-04",
        "GATEWAY_URL": "https://gateway.example.invalid",
        "PRIVATE_RELEASE_MANIFEST": json.dumps(manifest(expected)),
        "PRIVATE_RELEASE_SELECTORS": json.dumps(selectors),
        "TEST_RESPONSES": str(response_file),
        "TEST_CHECKOUT": str(checkout),
        "TEST_CALLS": str(tmp_path / "calls.jsonl"),
        "TEST_DENY": deny,
    }
    workflow = yaml.load(
        (ROOT / ".github/workflows/tools-live-evaluation.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["evaluate"]["steps"]

    def step(name):
        body = next(item["run"] for item in steps if item.get("name") == name)
        result = subprocess.run(["bash", "-c", body], cwd=checkout, env=env, capture_output=True, text=True)
        for line in env_file.read_text().splitlines():
            key, value = line.split("=", 1)
            env[key] = value
        return result

    assert step("Verify the deployed container images against immutable source tags").returncode == 0
    assert step("Bind the approved private release window").returncode == 0
    for name in ("tools-eval-kubeconfig", "tools-release-window.json", "tools-release-selectors.json"):
        assert (tmp_path / name).stat().st_mode & 0o077 == 0
    result = step("Exercise deployed gateway through the MCP client")
    report = json.loads((tmp_path / "tools-eval-evidence/evidence.json").read_text())
    assert result.returncode == (1 if deny else 0), (result.stdout, result.stderr, report)
    assert report["live_attestation"] is (not bool(deny))
    assert report["scenarios"][0]["status"] == ("fail" if deny else "pass")
    assert "private-fixture-credential" not in json.dumps(report)
    if not deny:
        for phase in ("before", "after"):
            assert (
                json.loads((tmp_path / f"tools-eval-evidence/runtime-{phase}.json").read_text())["runtime"]
                == expected
            )
        calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert sum(call[:3] == ["aws", "lambda", "get-function"] for call in calls) == 2
    assert step("Remove owned evaluation configuration and test-user session").returncode == 0
    assert all(
        not (tmp_path / name).exists()
        for name in ("tools-eval-kubeconfig", "tools-release-window.json", "tools-release-selectors.json")
    )


@pytest.mark.parametrize("source", ["reconciler", "target"])
def test_invalid_provider_timestamps_do_not_leak_into_evaluation_report(tmp_path, source):
    from tests.e2e.tools import runner

    selectors, responses, expected = fixtures()
    marker = "synthetic-provider-response-secret"
    if source == "reconciler":
        value = responses["pods/adp-gateway/batch.kubernetes.io/job-name=tools-facade-reconciler-123"][
            "items"
        ][0]
        value["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = marker
    else:
        responses["bedrock-agentcore-control/get-gateway-target/target-one"]["lastSynchronizedAt"] = marker
    bound = configured(tmp_path, selectors, Provider(responses))
    output = tmp_path / "report"
    assert not runner.evaluate(
        "SCN-8068-04",
        output,
        "https://gateway.example.invalid",
        SOURCE,
        IMAGE,
        IMAGE,
        command=[],
        live=True,
        release_manifest=manifest(expected),
        release_observer=bound.readback,
        release_source_revision=SOURCE,
        release_account=ACCOUNT,
    )
    report = (output / "evidence.json").read_text()
    assert marker not in report
    assert json.loads(report)["scenarios"][0]["status"] == "fail"


def test_containerd_status_image_may_be_a_config_digest(tmp_path):
    selectors, responses, expected = fixtures()
    for key, response in responses.items():
        if key.startswith("pods/"):
            for value in response["items"]:
                value["status"]["containerStatuses"][0]["image"] = "sha256:" + "c" * 64
    # The immutable spec and resolved imageID still match; status.image is not
    # required to be a repository-qualified manifest reference under containerd.
    assert configured(tmp_path, selectors, Provider(responses)).readback("before") == expected


def adapter_fixture():
    selectors, responses, expected = fixtures()
    selectors["components"]["tool-adapters"] = {
        "namespace": "adp-tools", "deployment": "tool-adapters", "container": "tool-adapters",
    }
    deployment = copy.deepcopy(responses["deployment/adp-gateway/bedrockgateway"])
    deployment["metadata"] = metadata("tool-adapters", "adp-tools")
    deployment["spec"]["selector"]["matchLabels"] = {"app": "tool-adapters"}
    deployment["spec"]["template"]["spec"]["containers"][0]["name"] = "tool-adapters"
    responses["deployment/adp-tools/tool-adapters"] = deployment
    responses["replicaset/adp-tools/tool-adapters-rs"] = {
        "metadata": metadata("tool-adapters-rs", "adp-tools", owned("Deployment", "tool-adapters")),
    }
    responses["pods/adp-tools/app=tool-adapters"] = {"items": [
        pod("tool-adapters-pod", "adp-tools", "tool-adapters", owned("ReplicaSet", "tool-adapters-rs")),
    ]}
    content = json.dumps({"schema_version": "1.0", "release": {"image_digest": DIGEST}})
    responses["configmap/adp-tools/tool-adapters-contract"] = {
        "metadata": metadata("tool-adapters-contract", "adp-tools"), "data": {"config.json": content},
    }
    expected["tool-adapters"] = {
        "image": DIGEST, "configuration": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
    }
    return selectors, responses, expected


def test_adapter_release_selection_reads_separate_workload_and_configuration(tmp_path):
    selectors, responses, expected = adapter_fixture()
    provider = Provider(responses)
    bound = configured(tmp_path, selectors, provider)
    receipt = verify_window(manifest(expected), bound.readback, source=SOURCE,
                            account=ACCOUNT, gateway_image=IMAGE, facade_image=IMAGE)
    assert receipt["components"] == sorted(expected)
    assert provider.calls.count("configmap/adp-tools/tool-adapters-contract") == 2


def test_adapter_release_cannot_infer_four_route_fence_from_aggregate_targets(tmp_path):
    selectors, responses, expected = adapter_fixture()
    release = manifest(expected)
    release["route_group"] = {
        "group": "tool-connectors", "generation": 2, "status": "active",
        "routes": {name: {"target_id": name + "-target", "backend": "adapter", "digest": DIGEST}
                   for name in ("atlassian", "github", "slack", "jira")},
    }
    provider = Provider(responses)
    bound = configured(tmp_path, selectors, provider)
    with pytest.raises(ValueError, match="independent runtime readback changed"):
        verify_window(release, bound.readback, source=SOURCE, account=ACCOUNT,
                      gateway_image=IMAGE, facade_image=IMAGE)
    assert provider.calls.count("bedrock-agentcore-control/list-gateway-targets") == 1


@pytest.mark.parametrize("phase", ("before", "after"))
@pytest.mark.parametrize("stale", ("image", "configuration"))
def test_green_adapter_receipt_rejects_independently_observed_stale_target(tmp_path, phase, stale):
    selectors, responses, expected = adapter_fixture()
    release = manifest(expected)
    assert verify_manifest(release, source=SOURCE, account=ACCOUNT,
                           gateway_image=IMAGE, facade_image=IMAGE) == expected
    provider = Provider(responses)
    bound = configured(tmp_path, selectors, provider)

    def readback(observed_phase):
        if observed_phase == phase:
            configmap = responses["configmap/adp-tools/tool-adapters-contract"]
            configuration = json.loads(configmap["data"]["config.json"])
            if stale == "image":
                old_image = IMAGE.replace("b" * 64, "e" * 64)
                deployment = responses["deployment/adp-tools/tool-adapters"]
                deployment["spec"]["template"]["spec"]["containers"][0]["image"] = old_image
                adapter_pod = responses["pods/adp-tools/app=tool-adapters"]["items"][0]
                adapter_pod["spec"]["containers"][0]["image"] = old_image
                status = adapter_pod["status"]["containerStatuses"][0]
                status["image"] = old_image
                status["imageID"] = "docker-pullable://" + old_image
                configuration["release"]["image_digest"] = old_image.rsplit("@", 1)[-1]
            else:
                configuration["release"]["group_generation"] = 2
            configmap["data"]["config.json"] = json.dumps(configuration)
        return bound.readback(observed_phase)

    with pytest.raises(ValueError, match=f"{phase} independent runtime readback changed"):
        verify_window(release, readback, source=SOURCE, account=ACCOUNT,
                      gateway_image=IMAGE, facade_image=IMAGE)
    assert provider.calls.count("configmap/adp-tools/tool-adapters-contract") == (1 if phase == "before" else 2)
