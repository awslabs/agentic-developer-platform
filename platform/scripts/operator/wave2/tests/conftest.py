#!/usr/bin/env python3
"""Stub-binary harness for the Wave 2 shell steps (issue #3968).

The creation script's value is in what it REFUSES to do, and a refusal is only
credible if it is executed. So these fixtures put fake `aws` and `kubectl`
binaries on PATH, drive the real script, and assert on two things:

  * the exit status, and
  * the call log -- specifically that no `create` call was ever issued.

Asserting "it printed an error" would pass against a script that printed an error
AND created the fixture anyway. Asserting the absence of the create call is what
actually distinguishes fail-closed from fail-noisy.

Scenarios are JSON rule lists, matched by substring against the joined argv, in
the same shape as tests/test_cleanup.py's ScriptedRunner so both layers read the
same way.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WAVE2 / "lib"))

import render_fixture  # noqa: E402

TARGET_ACCOUNT = "879318057152"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
argv = " ".join(sys.argv[1:])
tool = os.path.basename(sys.argv[0])
with open(os.environ["W2_TEST_CALLLOG"], "a") as fh:
    fh.write(f"{tool} {argv}\n")
rules = json.load(open(os.environ["W2_TEST_SCENARIO"]))
state_path = os.environ["W2_TEST_SCENARIO"] + ".consumed"
consumed = set()
if os.path.exists(state_path):
    consumed = set(json.load(open(state_path)))
for index, rule in enumerate(rules):
    if index in consumed:
        continue
    if rule.get("tool", tool) != tool:
        continue
    if all(fragment in argv for fragment in rule["match"]):
        if rule.get("once", True):
            consumed.add(index)
            with open(state_path, "w") as fh:
                json.dump(sorted(consumed), fh)
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("rc", 0))
sys.stderr.write(f"STUB: no scripted reply for: {tool} {argv}\n")
sys.exit(97)
'''


def live_deployment() -> dict:
    """A realistic live gateway Deployment, including all nine secret env refs.

    Built FROM render_fixture.EXPECTED_SECRET_ENV rather than hand-listed, so the
    fixture under test and the assertion about it cannot drift apart.
    """
    env: list[dict] = [
        {"name": name,
         "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        for name, (secret, key) in render_fixture.EXPECTED_SECRET_ENV.items()
    ]
    env.append({"name": "AGENT_CONTROL_PORT", "value": render_fixture.CONTROL_PORT})
    env.append({"name": "AGENT_CONTROL_CLUSTER_POD_CIDRS", "value": "10.0.0.0/16"})
    env.append({"name": "BG_DATABASE_URL",
                "valueFrom": {"secretKeyRef": {"name": "bedrockgateway-secrets",
                                               "key": "database-url"}}})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "bedrockgateway", "namespace": "adp-gateway",
                     "uid": "live-uid-0000", "resourceVersion": "12345"},
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "bedrockgateway"}},
            "template": {
                "metadata": {"labels": {"app": "bedrockgateway"}},
                "spec": {
                    "serviceAccountName": "bedrockgateway-sa",
                    "containers": [{
                        "name": "bedrockgateway",
                        "image": "123.dkr.ecr.us-east-1.amazonaws.com/adp-gateway:v1.2.3",
                        "ports": [{"containerPort": 8080}, {"containerPort": 8770}],
                        "envFrom": [
                            {"configMapRef": {"name": "bedrockgateway-config"}},
                            {"configMapRef": {"name": "bedrockgateway-feature-flags"}},
                        ],
                        "env": env,
                        "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}},
                        "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}},
                        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}},
                    }],
                },
            },
        },
    }


APPROVED_WORKER_DIGEST = "sha256:" + "cd" * 32
WORKER_CONTROL_ENDPOINT = (
    "https://abc123xyz0.execute-api.us-east-1.amazonaws.com/w2fixture/internal/v1/agent"
)


def live_worker_template(*, authority: bool = True) -> dict:
    """The live worker pod template, as the ScaledJob's jobTargetRef.template holds it.

    Composed from what `modules/agent-factory/webhook-ingress/infra/scaledjob.tf`
    actually renders when `agent_authority_enabled` and `agent_control_enabled` are
    true, rather than from what a fixture would find convenient: the whole point of
    render_worker_job is that it COPIES this, so a stub that is missing what the real
    one has would let a defect through and a stub that has what the real one lacks
    would test a composition nobody deploys.

    `authority=False` drops the projected token volume and its mounts, which is what
    the template looks like with agent authority off -- the case the renderer must
    refuse rather than paper over.
    """
    env: list[dict] = [
        {"name": "AWS_REGION", "value": "us-east-1"},
        {"name": "ENVIRONMENT", "value": "dev"},
        {"name": "QUEUE_URL",
         "value": "https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-agent-submit.fifo"},
        {"name": "ADP_GATEWAY_ENDPOINT",
         "value": "https://ordinary.execute-api.us-east-1.amazonaws.com/dev"},
        {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
        {"name": "FEATURE_AGENT_CONTROL_ENABLED", "value": "true"},
        {"name": "ADP_CONTROL_PORT", "value": render_fixture.CONTROL_PORT},
        {"name": "ADP_POD_DEADLINE_SECONDS", "value": "21600"},
    ]
    volumes: list[dict] = []
    mounts: list[dict] = []
    if authority:
        env += [
            {"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "true"},
            {"name": "ADP_AGENT_CONTROL_ENDPOINT",
             "value": "https://ordinary.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent"},
            {"name": "ADP_WORKLOAD_TOKEN_FILE", "value": render_fixture.WORKLOAD_TOKEN_PATH},
            {"name": "ADP_CONTROL_ENVELOPE_KEYS_FILE", "value": render_fixture.CONTROL_KEYS_PATH},
        ]
        mounts += [
            {"name": render_fixture.WORKLOAD_VOLUME,
             "mountPath": render_fixture.WORKLOAD_TOKEN_DIR, "readOnly": True},
            {"name": render_fixture.CONTROL_KEYS_VOLUME,
             "mountPath": render_fixture.CONTROL_KEYS_DIR, "readOnly": True},
        ]
        volumes += [
            {"name": render_fixture.WORKLOAD_VOLUME,
             "projected": {"sources": [{"serviceAccountToken": {
                 "audience": render_fixture.BOOTSTRAP_AUDIENCE,
                 "expirationSeconds": 3600, "path": "token"}}]}},
            {"name": render_fixture.CONTROL_KEYS_VOLUME,
             "configMap": {"name": render_fixture.CONTROL_KEYS_CONFIGMAP}},
        ]
    return {
        "metadata": {
            "annotations": {"karpenter.sh/do-not-disrupt": "true"},
            "labels": {"app.kubernetes.io/name": "agent-scaledjob",
                       "app.kubernetes.io/part-of": "adp-agent-factory"},
        },
        "spec": {
            "serviceAccountName": render_fixture.WORKER_SERVICE_ACCOUNT,
            "restartPolicy": "Never",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1001, "runAsGroup": 1001,
                                "fsGroup": 1001,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": render_fixture.WORKER_CONTAINER,
                "image": f"123.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@{APPROVED_WORKER_DIGEST}",
                "env": env,
                "volumeMounts": mounts,
                "ports": [{"name": "agent-control",
                           "containerPort": int(render_fixture.CONTROL_PORT),
                           "protocol": "TCP"}],
                "resources": {"requests": {"cpu": "1", "memory": "4Gi"},
                              "limits": {"cpu": "4", "memory": "8Gi"}},
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}},
            }],
            "volumes": volumes,
        },
    }


def not_found(kind: str, name: str) -> dict:
    return {"rc": 1, "stderr": f'Error from server (NotFound): {kind} "{name}" not found'}


def base_rules(*, digest: str = "sha256:" + "ab" * 32,
               secrets_present: bool = True,
               deployment: dict | None = None) -> list[dict]:
    """The rule set for a run that gets as far as the queue probe."""
    deployment = deployment if deployment is not None else live_deployment()
    rules: list[dict] = [
        {"tool": "aws", "match": ["sts", "get-caller-identity"], "once": False,
         "stdout": json.dumps({"Account": TARGET_ACCOUNT,
                               "Arn": f"arn:aws:sts::{TARGET_ACCOUNT}:assumed-role/op/w2",
                               "UserId": "AIDA:w2"})},
        # stage-gate observations: all four absent.
        #
        # The Job is observed HERE, before anything is created, rather than later next
        # to the worker creation. The staged lifecycle means one stage's creation target
        # is another's prerequisite, so the decision needs a reply about every object up
        # front -- and lib/stage_gate.py refuses a MISSING observation rather than
        # reading it as absent. A harness that omitted the Job reply would therefore
        # exercise the unreadable path, not the absent one.
        {"tool": "kubectl", "match": ["get", "Deployment", "w2-fixture-gateway"],
         **not_found("deployments.apps", "w2-fixture-gateway")},
        {"tool": "kubectl", "match": ["get", "Service", "w2-fixture-gateway"],
         **not_found("services", "w2-fixture-gateway")},
        # BOTH NetworkPolicies, matched on namespace as well as name. The fixture has a
        # gateway-side policy in adp-gateway and a worker-side one in adp-agents, and the
        # worker-side one is what confines the pod holding protected authority. While the
        # gate's expectation map was keyed by kind the two collided and only one was ever
        # checked; these replies are separated so a harness that answered for only one
        # would hit the gate's missing-observation refusal rather than silently pass.
        {"tool": "kubectl",
         "match": ["get", "NetworkPolicy", "w2-fixture-policy", "-n", "adp-gateway"],
         **not_found("networkpolicies.networking.k8s.io", "w2-fixture-policy")},
        {"tool": "kubectl",
         "match": ["get", "NetworkPolicy", "-worker", "-n", "adp-agents"],
         **not_found("networkpolicies.networking.k8s.io", "w2-fixture-policy-worker")},
        {"tool": "kubectl", "match": ["get", "Job", "w2-fixture-worker"],
         **not_found("jobs.batch", "w2-fixture-worker")},
        # the live composition to copy
        {"tool": "kubectl", "match": ["get", "deploy", "bedrockgateway", "-o", "json"],
         "stdout": json.dumps(deployment)},
        # running pods' imageID -> the digest actually serving
        {"tool": "kubectl", "match": ["get", "pods", "app=bedrockgateway"],
         "stdout": f"123.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@{digest}\n" if digest else ""},
    ]
    for secret, key in render_fixture.CONTROL_CRITICAL_SECRETS:
        rules.append({"tool": "kubectl", "match": ["get", "secret", secret, key],
                      "stdout": "eyJrZXkiOiAidmFsdWUifQ==" if secrets_present else ""})
    return rules


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, calls: list[str], tmp: Path) -> None:
        self.rc = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.calls = calls
        self.tmp = tmp

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def created(self, *fragments: str) -> bool:
        """Did any mutating call actually go out?"""
        mutators = ("kubectl create", "create-queue", "delete", "apply")
        for call in self.calls:
            if not any(m in call for m in mutators):
                continue
            if not fragments or all(f in call for f in fragments):
                return True
        return False

    def ledger(self) -> dict:
        path = self.tmp / "ledger.json"
        return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def run_create(tmp_path: Path):
    """Run 10-create-fixture.sh against stub binaries and a scenario."""

    def _run(rules: list[dict], *, args: list[str] | None = None,
             account: str = TARGET_ACCOUNT, env_extra: dict | None = None) -> Run:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        for tool in ("aws", "kubectl"):
            path = bindir / tool
            path.write_text(STUB)
            path.chmod(0o755)

        scenario = tmp_path / "scenario.json"
        scenario.write_text(json.dumps(rules))
        # The staged lifecycle is tested by invoking the script TWICE against one
        # ledger, so the harness has to be re-enterable: the consumed-rule set and
        # the call log belong to a single invocation, while the ledger and the
        # evidence directory deliberately survive between them (that persistence is
        # the thing under test).
        consumed = Path(str(scenario) + ".consumed")
        if consumed.exists():
            consumed.unlink()
        calllog = tmp_path / "calls.log"
        calllog.write_text("")
        kubeconfig = tmp_path / "kubeconfig"
        kubeconfig.write_text("apiVersion: v1\nkind: Config\n")

        env = dict(os.environ)
        env.update({
            "PATH": f"{bindir}:{env['PATH']}",
            "W2_TEST_SCENARIO": str(scenario),
            "W2_TEST_CALLLOG": str(calllog),
            "W2_CRED_MODE": "env",
            "W2_KUBECONFIG": str(kubeconfig),
            "W2_EXPECT_ACCOUNT": account,
        })
        for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            env.pop(key, None)
        env.update(env_extra or {})

        argv = [str(WAVE2 / "10-create-fixture.sh"),
                "--run-id", "w2-fixture-test",
                "--ledger", str(tmp_path / "ledger.json"),
                "--evidence-dir", str(tmp_path / "evidence")] + (args or [])
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
        calls = [line for line in calllog.read_text().splitlines() if line.strip()]
        return Run(proc, calls, tmp_path)

    return _run
