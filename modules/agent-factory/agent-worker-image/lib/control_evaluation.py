"""Run the live control acceptance harness in an authenticated fixture worker.

This is selected only by a protected dispatch and a matching projected fixture
nonce. Task acquisition, envelope-digest verification and credential refresh are
the normal entrypoint's responsibility and happen before this function is called.
The operator supplies a Git bundle and observed pod identity after the pod exists.
No repository credential or customer credential is needed for this task.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

HANDOFF = Path("/work/control-evaluation")
IDENTITY = Path("/var/run/adp-w2-identity")


def evaluation_request(envelope: dict, *, authenticated: bool, env: dict,
                       identity_dir: Path = IDENTITY) -> dict | None:
    payload = envelope.get("payload") or {}
    request = payload.get("control_evaluation")
    if request is None:
        return None
    if not isinstance(request, dict) or not authenticated or env.get("ADP_AGENT_AUTHORITY_ENABLED") != "true":
        raise ValueError("control evaluation requires authenticated fixture dispatch")
    labels = {}
    for line in (identity_dir / "pod-labels").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            labels[key] = json.loads(value)
    nonce = labels.get("adp.io/w2-nonce", "")
    run_id = labels.get("adp.io/w2-fixture", "")
    if (not re.fullmatch(r"[a-f0-9]{16,64}", nonce)
            or not run_id.startswith("w2-")
            or request.get("run_nonce") != nonce
            or request.get("run_id") != run_id
            or env.get("W2_FIXTURE_RUN_ID") != run_id):
        raise ValueError("control evaluation does not belong to this fixture")
    if not re.fullmatch(r"[a-f0-9]{40}", str(request.get("source_revision", ""))):
        raise ValueError("control evaluation requires an exact source revision")
    if not re.fullmatch(r"[a-f0-9]{64}", str(request.get("bundle_sha256", ""))):
        raise ValueError("control evaluation requires an exact source bundle digest")
    endpoint = env.get("ADP_AGENT_CONTROL_ENDPOINT", "")
    if not endpoint.endswith("/internal/v1/agent") or env.get("SIGV4_PROXY_TARGET") != endpoint.removesuffix("/internal/v1/agent") + "/agent":
        raise ValueError("control and model calls must use the same fixture edge")
    return request


def _wait_for(path: Path, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"operator handoff timed out: {path.name}")
        time.sleep(1)


def verify_handoff(request: dict, *, handoff: Path, pod_uid: str) -> None:
    """Bind operator-supplied bytes to the authenticated dispatch and this pod."""
    bundle = handoff / "source.bundle"
    with bundle.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != request["bundle_sha256"]:
        raise ValueError("control evaluation source bundle digest differs")
    document = json.loads((handoff / "expected-identity.json").read_text())
    expected = document.get("expected_identity", document)
    if not pod_uid or expected.get("pod_uid") != pod_uid:
        raise ValueError("control evaluation identity names a different pod")
    ledger = json.loads((handoff / "cleanup-ledger.json").read_text())
    if ledger.get("run_id") != request["run_id"] or ledger.get("run_nonce") != request["run_nonce"]:
        raise ValueError("control evaluation cleanup ledger names a different run")


def run_evaluation(request: dict, envelope: dict, *, start_proxy: Callable,
                   stop_proxy: Callable, handoff: Path = HANDOFF,
                   identity_dir: Path = IDENTITY, control_env: dict | None = None) -> int:
    """Run the evidence-collection subprocess with the production control channel open.

    ``control_env`` carries whatever ``_setup_agent_control`` placed into a
    fresh child-env dict for this run (issue #5891, LF-01) — the same
    ``ADP_CONTROL_*`` values the ordinary path places into its own agent
    subprocess's environment. Empty when control registration failed or the
    feature flag is off, in which case the evidence collector runs exactly as
    it did before this parameter existed. Never merged into ``os.environ``:
    like the ordinary path, only the *evaluation subprocess's* environment
    carries the token.
    """
    handoff.mkdir(mode=0o700, parents=True, exist_ok=True)
    pod_uid = (identity_dir / "pod-uid").read_text().strip()
    (handoff / "bootstrap-ready.json").write_text(json.dumps({
        "pod_uid": pod_uid, "run_id": request["run_id"],
        "run_nonce": request["run_nonce"], "invocation_id": envelope["message_id"],
    }))
    # ready is copied LAST. Partial transfers cannot start a paid experiment.
    _wait_for(handoff / "ready", 300)
    verify_handoff(request, handoff=handoff, pod_uid=pod_uid)
    checkout = handoff / "source"
    subprocess.run(["git", "clone", "--no-checkout", str(handoff / "source.bundle"), str(checkout)], check=True, timeout=120)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", request["source_revision"]], check=True, timeout=120)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if revision != request["source_revision"]:
        raise ValueError("control evaluation checkout revision differs")
    os.environ["ADP_MESSAGE_ID"] = envelope["message_id"]
    os.environ["ADP_CORRELATION_ID"] = (envelope.get("correlation") or {}).get("correlation_id", envelope["message_id"])
    env = os.environ.copy()
    env.update({
        "ADP_TENANT_ID": envelope["tenant_id"], "TENANT_ID": envelope["tenant_id"],
        "W2_EXPECTED_IDENTITY": str(handoff / "expected-identity.json"),
        "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "http://127.0.0.1:9090", "SIGV4_PROXY_PORT": "9090",
    })
    if control_env:
        env.update(control_env)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(key, None)
    proxy = start_proxy(env, envelope["tenant_id"])
    if proxy is None:
        raise RuntimeError("control evaluation model proxy did not start")
    result = 1
    try:
        with (handoff / "experiment.log").open("w") as log:
            process = subprocess.Popen([
                "bash", str(checkout / "platform/scripts/operator/wave2/20-collect-pause-evidence.sh"),
                "--evidence-dir", str(handoff / "evidence"),
                "--expected-identity", str(handoff / "expected-identity.json"),
                "--ledger", str(handoff / "cleanup-ledger.json"),
            ], env=env, cwd=checkout, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                result = process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                result = 124
            finally:
                # Bash timing out must not leave its SDK/tool descendants alive.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    finally:
        stop_proxy(proxy)
        (handoff / "result.json").write_text(json.dumps({"exit_code": result, "pod_uid": pod_uid,
                                                       "run_nonce": request["run_nonce"]}))
    # Keep the pod available for evidence extraction; the normal task heartbeat
    # continues throughout. An unread result never becomes claimed acceptance.
    _wait_for(handoff / "collected", 180)
    return result
