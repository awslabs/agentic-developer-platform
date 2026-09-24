#!/usr/bin/env python3
"""Tests for 00-verify-target.sh (issue #3968, root's blocker 5).

Why this script needed rewriting, and why these tests exist:

  The previous version refused on line 29 unless `adp-cred` was on PATH ("the
  vault connection is the only supported credential path"). Root has valid
  embark1/instance credentials but NO adp-cred binary -- so the gate that every
  later step depends on could not run on the host that has to run it. A guard
  that cannot execute in its intended environment protects nothing.

The account assertion is the security property and is mode-independent, so the
rewrite makes it stronger rather than weaker: all three credential modes are now
covered by the single check in lib/session.sh.

These tests also pin a defect found while rewriting: a `valueFrom` reference for
FEATURE_AGENT_CONTROL_ENABLED has no inline `value`, so the old parser printed
"absent" -- which PASSED the DP-INV-1 gate. An indirect flag whose resolved value
is unknown must not be graded as a flag that is off.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]
SCRIPT = WAVE2 / "00-verify-target.sh"
GOOD_ACCOUNT = "879318057152"
WRONG_ACCOUNT = "605440105851"

GOOD_ROLE = f"arn:aws:sts::{GOOD_ACCOUNT}:assumed-role/ADP-Agent-adp-embark1/s"


def deploy_json(env_entry: dict | None) -> str:
    env = [env_entry] if env_entry else []
    return json.dumps({
        "spec": {"template": {"spec": {"containers": [
            {"name": "bedrockgateway", "env": env}]}}}
    })


@pytest.fixture(scope="session")
def path_without_adp_cred(tmp_path_factory) -> str:
    """A PATH that genuinely does NOT contain `adp-cred`.

    This exists because the obvious version of the test was vacuous. The dev host
    HAS adp-cred installed, so "assert the script runs" passed without ever
    exercising the binary's absence -- it would have passed just as happily against
    the old script that hard-required it. A test that cannot fail for the reason it
    names is the same defect class this PR removes from the scripts themselves.

    So: symlink every executable on the real PATH into one directory, minus
    adp-cred, and point the script at that. Now absence is a fact of the
    environment rather than an assumption in a docstring.
    """
    shim = tmp_path_factory.mktemp("no-adp-cred-path")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isdir(entry):
            continue
        for name in sorted(os.listdir(entry)):
            if name == "adp-cred" or (shim / name).exists():
                continue
            try:
                (shim / name).symlink_to(os.path.join(entry, name))
            except OSError:
                pass
    assert not (shim / "adp-cred").exists()
    assert (shim / "python3").exists(), "the shim PATH must still be usable"
    return str(shim)


@pytest.fixture
def target(tmp_path: Path, path_without_adp_cred: str):
    """Run the real script with aws/kubectl stubbed. No cluster, no credentials.

    Runs on a PATH with no `adp-cred`, which is root's actual host condition.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calllog = tmp_path / "calls.log"
    calllog.touch()

    aws = bindir / "aws"
    aws.write_text(f"""#!/usr/bin/env bash
echo "aws $*" >> "$W2_TEST_CALLLOG"
acct="${{STUB_ACCOUNT:-{GOOD_ACCOUNT}}}"
arn="${{STUB_ARN:-{GOOD_ROLE}}}"
case "$*" in
  *get-caller-identity*)
    printf '{{"Account":"%s","Arn":"%s","UserId":"u"}}\\n' "$acct" "$arn" ;;
  *describe-table*KeySchema*)
    printf '%s\\n' "${{STUB_SCHEMA:-$(printf 'event_id\\tHASH\\narrived_at\\tRANGE')}}" ;;
  *describe-table*ItemCount*) echo "123456" ;;
  *update-kubeconfig*) : ;;
  *get-queue-url*) exit "${{STUB_QUEUE_RC:-1}}" ;;
  *) : ;;
esac
""")
    aws.chmod(0o755)

    kubectl = bindir / "kubectl"
    kubectl.write_text("""#!/usr/bin/env bash
echo "kubectl $*" >> "$W2_TEST_CALLLOG"
case "$*" in
  *"get deploy bedrockgateway"*) printf '%s\\n' "$STUB_DEPLOY" ;;
  *"get deploy authority-probe"*) exit "${STUB_PROBE_RC:-1}" ;;
  # %b so the \\n become real newlines: the script counts lines with `wc -l`, and a
  # literal backslash-n would read as ZERO policies and refuse. `-` not `:-` so an
  # explicitly-empty STUB_NETPOL stays empty (the no-policies case) instead of
  # falling back to the default.
  *networkpolicy*) printf '%b' "${STUB_NETPOL-np-a x\\nnp-b y\\n}" ;;
  *"get pods"*) echo "docker.io/x@sha256:abc123" ;;
  *) : ;;
esac
""")
    kubectl.chmod(0o755)

    def run(env=None):
        environment = dict(os.environ)
        environment.update({
            # NOT os.environ["PATH"]: that has adp-cred on it, and the whole point
            # of blocker 5 is the host that does not.
            "PATH": f"{bindir}:{path_without_adp_cred}",
            "W2_TEST_CALLLOG": str(calllog),
            "AWS_PROFILE": "adp-embark1",   # the mode the old script could not use
            "W2_KUBECONFIG": str(tmp_path / "kubeconfig"),
            "STUB_DEPLOY": deploy_json({"name": "FEATURE_AGENT_CONTROL_ENABLED",
                                        "value": "false"}),
        })
        for key in ("W2_CRED_MODE", "KUBERNETES_SERVICE_HOST"):
            environment.pop(key, None)
        if env:
            environment.update(env)
        (tmp_path / "kubeconfig").write_text("stub")
        proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                              env=environment, timeout=60)
        return proc, calllog.read_text()

    return run


# ---------------------------------------------------------------------------
# blocker 5: it must RUN without adp-cred
# ---------------------------------------------------------------------------
def test_runs_to_completion_without_adp_cred(target) -> None:
    """THE blocker. Root has no adp-cred binary; the old script refused at line 29."""
    proc, _ = target()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "TARGET VERIFIED" in proc.stdout


def test_adp_cred_is_not_a_hard_requirement() -> None:
    source = SCRIPT.read_text()
    assert "command -v adp-cred >/dev/null || fail" not in source, \
        "adp-cred must not be a hard gate: root's host does not have it"


def test_profile_mode_is_reported(target) -> None:
    proc, _ = target()
    assert "profile" in proc.stdout


# ---------------------------------------------------------------------------
# the account assertion still gates, in this mode too
# ---------------------------------------------------------------------------
def test_wrong_account_refuses_before_reading_anything(target) -> None:
    """The #5195 failure mode. Relaxing the credential MODE must not relax this."""
    proc, calls = target(env={"STUB_ACCOUNT": WRONG_ACCOUNT})
    assert proc.returncode != 0
    assert "TARGET VERIFIED" not in proc.stdout
    assert "describe-table" not in calls, "no observations after an account refusal"


def test_an_unexpected_role_is_recorded_not_refused(target) -> None:
    """Several legitimate credentials reach the target account. The ACCOUNT is the
    security property; hard-failing on the role name rejects a correct credential
    for the right account -- which is what blocked root."""
    proc, _ = target(env={"STUB_ARN": f"arn:aws:sts::{GOOD_ACCOUNT}:assumed-role/Other/s"})
    assert proc.returncode == 0
    assert "not ADP-Agent-adp-embark1" in proc.stdout
    assert "TARGET VERIFIED" in proc.stdout


# ---------------------------------------------------------------------------
# DP-INV-1
# ---------------------------------------------------------------------------
def test_control_flag_false_passes(target) -> None:
    proc, _ = target()
    assert "DP-INV-1 intact" in proc.stdout
    assert proc.returncode == 0


def test_control_flag_true_refuses(target) -> None:
    """The ordinary flag must stay OFF: ON only in the disposable fixture."""
    proc, _ = target(env={"STUB_DEPLOY": deploy_json(
        {"name": "FEATURE_AGENT_CONTROL_ENABLED", "value": "true"})})
    assert proc.returncode != 0
    assert "DP-INV-1" in proc.stdout + proc.stderr


def test_absent_flag_passes(target) -> None:
    proc, _ = target(env={"STUB_DEPLOY": deploy_json(None)})
    assert proc.returncode == 0


def test_valueFrom_flag_is_refused_not_treated_as_absent(target) -> None:
    """Defect found while rewriting.

    A secret/configmap-backed flag has no inline `value`, so the old parser fell
    through to "absent" -- which PASSED the gate. An indirect reference whose
    resolved value is unknown must not grade as a flag that is off. Same shape as
    every other defect in this PR: an unobserved value read as a satisfied one.
    """
    proc, _ = target(env={"STUB_DEPLOY": deploy_json({
        "name": "FEATURE_AGENT_CONTROL_ENABLED",
        "valueFrom": {"configMapKeyRef": {"name": "cm", "key": "k"}}})})
    assert proc.returncode != 0
    assert "valueFrom" in proc.stdout + proc.stderr


def test_unreadable_deployment_refuses(target) -> None:
    """An invariant that cannot be verified must not be assumed intact."""
    proc, _ = target(env={"STUB_DEPLOY": ""})
    assert proc.returncode != 0


# ---------------------------------------------------------------------------
# table schema
# ---------------------------------------------------------------------------
def test_wrong_key_schema_refuses(target) -> None:
    """Cleanup deletes synthetic rows by BOTH keys; another schema breaks that."""
    proc, _ = target(env={"STUB_SCHEMA": "event_id\tHASH"})
    assert proc.returncode != 0
    assert "key schema" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# network policy
# ---------------------------------------------------------------------------
def test_no_network_policies_refuses(target) -> None:
    proc, _ = target(env={"STUB_NETPOL": ""})
    assert proc.returncode != 0
    assert "ingress isolation" in proc.stdout + proc.stderr


def test_presence_is_not_claimed_as_enforcement(target) -> None:
    """Root's blocker 2: "an applied NetworkPolicy is not isolation proof."."""
    proc, _ = target()
    assert "presence is not enforcement" in proc.stdout


# ---------------------------------------------------------------------------
# do-not-touch inventory
# ---------------------------------------------------------------------------
def test_preexisting_probe_resources_are_flagged_do_not_touch(target) -> None:
    """Unknown ownership: never reuse, mutate or remove."""
    proc, _ = target(env={"STUB_PROBE_RC": "0", "STUB_QUEUE_RC": "0"})
    assert "DO NOT reuse/mutate/delete" in proc.stdout
    assert "DO NOT reuse/purge/delete" in proc.stdout


def test_script_is_read_only(target) -> None:
    """It must observe and never mutate: no create/apply/delete/put anywhere."""
    _, calls = target(env={"STUB_PROBE_RC": "0", "STUB_QUEUE_RC": "0"})
    for mutating in ("create", "apply", "delete", "put-item", "set-queue",
                     "patch", "scale", "purge"):
        assert mutating not in calls, f"step 00 must be read-only, saw {mutating!r}"
