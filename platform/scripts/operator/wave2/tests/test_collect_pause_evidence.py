#!/usr/bin/env python3
"""Shell-level tests for 20-collect-pause-evidence.sh (issue #3968, root's blocker 6).

WHY THESE ARE SUBPROCESS TESTS
------------------------------
tests/test_experiment_binding.py proves the binding LIBRARY refuses dishonest
reuse. That is necessary and not sufficient: every one of those guarantees is
reachable only if the script actually passes the arguments that engage them. A
library that rejects an unpinned image is worth nothing if the caller never
supplies the image, and the unit tests cannot see that -- they call the functions
directly.

The defects root found were in exactly that seam:

  * the runtime image was never read from the pod status, so `runtime_image` was
    always None -- the check existed and never fired;
  * `capture` was invoked without `--raw-output` or `--run-nonce`, so no binding
    ever carried the provenance `verify-reuse` demands;
  * `--reuse` copied $REUSE over the evidence dir's raw file while the digest was
    checked against the RECORDED path, so a verified artifact and a consumed
    artifact could differ.

So these drive the real script with stub `kubectl` / `node` / `npx` on PATH and
assert on the FILES it produced and the calls it made. Asserting on stdout alone
would pass against a script that printed the right thing and bound nothing.

Nothing here makes a model call, touches a cluster, or needs a credential: the
live-run branch is never taken (every case supplies --reuse, or is refused before
the experiment), which is also what keeps this suite honest about root's
constraint that no paid experiment runs from developer scope.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]
SCRIPT = WAVE2 / "20-collect-pause-evidence.sh"
sys.path.insert(0, str(WAVE2 / "lib"))

import experiment_binding as eb  # noqa: E402

PINNED = "docker-pullable://repo/agent@sha256:" + "a" * 64
POD_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# The expected-identity document the OPERATOR writes, outside the pod. It carries the
# resolved image digest bound to the pod uid, which is how the image axis reaches this
# process now that there is no in-pod kubectl.
EXPECTED_IDENTITY = {
    "run_id": "w2-20260924-011500",
    "account_id": "879318057152",
    "namespace": "adp-agents",
    "pod_name": "w2-fixture-agent-7c9f4b8d2-xk4lq",
    "pod_uid": POD_UID,
    "service_account": "w2-fixture-agent",
    "aws_role_arn": "arn:aws:iam::879318057152:role/w2-fixture-agent",
    "runtime_image": PINNED,
}

# kubectl is on PATH so that an in-pod query would be RECORDED rather than merely
# failing -- and it refuses, because the protected service account cannot get or list
# pods and is deliberately not granted it. So this stub reproduces what the real
# cluster would answer this pod, which makes any call a visible test failure instead of
# a silent behaviour change. The image axis no longer comes from here at all; it comes
# from the operator's recorded observation, keyed by pod uid.
KUBECTL_STUB = r'''#!/usr/bin/env python3
import os, sys
argv = " ".join(sys.argv[1:])
with open(os.environ["W2_TEST_CALLLOG"], "a") as fh:
    fh.write("kubectl " + argv + "\n")
sys.stderr.write(
    'Error from server (Forbidden): pods is forbidden: User '
    '"system:serviceaccount:adp-agents:agent-authority-worker-sa" cannot get resource '
    '"pods" in API group "" in the namespace "adp-agents"\n')
sys.exit(1)
'''

# `node -p` is how the script reads the installed SDK version.
NODE_STUB = r'''#!/usr/bin/env python3
import os, sys
with open(os.environ["W2_TEST_CALLLOG"], "a") as fh:
    fh.write("node " + " ".join(sys.argv[1:]) + "\n")
sys.stdout.write(os.environ.get("W2_STUB_SDK_VERSION", "0.1.5") + "\n")
'''

# Must never run in these tests: reaching it means a refusal did not hold and a
# paid model call would have been made.
NPX_STUB = r'''#!/usr/bin/env python3
import os, sys
with open(os.environ["W2_TEST_CALLLOG"], "a") as fh:
    fh.write("npx " + " ".join(sys.argv[1:]) + "\n")
sys.stderr.write("STUB npx: the live experiment must not run in these tests\n")
sys.exit(98)
'''


def sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class Run:
    def __init__(self, proc, calls: list[str], evidence: Path) -> None:
        self.rc = proc.returncode
        self.output = proc.stdout + proc.stderr
        self.calls = calls
        self.evidence = evidence

    @property
    def ran_experiment(self) -> bool:
        return any(c.startswith("npx") for c in self.calls)

    def binding(self, suffix: str = "") -> dict:
        path = self.evidence / "artifacts" / ("experiment-binding.json" + suffix)
        if not path.exists():
            return {}
        doc = json.loads(path.read_text())
        return doc.get("binding", doc)

    def verdict(self, name: str) -> dict:
        path = self.evidence / "artifacts" / name
        return json.loads(path.read_text()) if path.exists() else {}

    def refused_because(self, axis: str) -> bool:
        """Was the reuse refused for THIS reason specifically?

        Necessary because a refusal can have several simultaneous causes -- notably a
        dirty working tree, which refuses every reuse. Asserting only `rc != 0` would
        let these tests pass against a script that had lost the check under test.
        """
        verdict = self.verdict("reuse-verdict.json")
        problems = (verdict.get("drift") or []) + (verdict.get("provenance_problems") or [])
        return any(p.get("axis") == axis for p in problems)


@pytest.fixture
def run_collect(tmp_path: Path):
    """Drive the real 20- script with stubbed kubectl/node/npx."""

    def _run(*, args: list[str] | None = None, env_extra: dict | None = None) -> Run:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        for tool, body in (("kubectl", KUBECTL_STUB), ("node", NODE_STUB), ("npx", NPX_STUB)):
            path = bindir / tool
            path.write_text(body)
            path.chmod(0o755)

        calllog = tmp_path / "calls.log"
        calllog.write_text("")
        evidence = tmp_path / "ev"

        # A FIXTURE service-account directory, not the real one.
        #
        # Root's finding on the previous revision: "Five shell tests rely on
        # /var/run/secrets/kubernetes.io/serviceaccount/namespace existing on developer
        # host; outside your pod, no image query occurs and honest reuse fails. Make
        # tests hermetic with an injected reader/discovery seam or stub cat/file access,
        # not real host mounts. Do not create fake system credentials on root host."
        #
        # So the seam is W2_SA_DIR, pointed at tmp_path. Nothing is written outside the
        # test's own temporary directory, no real system path is touched or created, and
        # the token file below is a literal placeholder string -- these tests assert on
        # discovery and binding behaviour, never on authentication.
        sa_dir = tmp_path / "sa"
        sa_dir.mkdir(exist_ok=True)
        (sa_dir / "namespace").write_text("adp-agents")
        (sa_dir / "token").write_text("not-a-real-token-placeholder")

        # The downwardAPI projection, which replaced a `service-account.name` file that
        # no mount ever writes (a downwardAPI fieldRef cannot reference
        # spec.serviceAccountName). W2_POD_IDENTITY_DIR is the same kind of test seam as
        # W2_SA_DIR: nothing outside tmp_path is touched.
        identity_dir = tmp_path / "pod-identity"
        identity_dir.mkdir(exist_ok=True)
        (identity_dir / "pod-uid").write_text(POD_UID)

        # The operator's recorded observation. The image axis now comes from HERE rather
        # than from an in-pod `kubectl get pod`: the protected service account cannot get
        # or list pods and is deliberately not granted it. The document is written outside
        # the pod, and its pod_uid is what ties it to this container.
        doc = tmp_path / "expected-identity.json"
        if not doc.exists():
            doc.write_text(json.dumps({"expected_identity": dict(EXPECTED_IDENTITY)}))

        env = dict(os.environ)
        env.update({
            "PATH": f"{bindir}:{env['PATH']}",
            "W2_TEST_CALLLOG": str(calllog),
            "W2_SA_DIR": str(sa_dir),
            "W2_POD_IDENTITY_DIR": str(identity_dir),
            "W2_EXPECTED_IDENTITY": str(doc),
        })
        # A bypassPermissions host check must not see the test runner's own creds.
        for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_SESSION_TOKEN", "W2_RUNTIME_IMAGE"):
            env.pop(key, None)
        env.update(env_extra or {})

        argv = [str(SCRIPT), "--evidence-dir", str(evidence)] + (args or [])
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
        calls = [line for line in calllog.read_text().splitlines() if line.strip()]
        return Run(proc, calls, evidence)

    return _run


@pytest.fixture
def recorded_run(tmp_path: Path):
    """A previous experiment's raw output plus a binding that honestly describes it."""

    def _make(**over) -> tuple[Path, Path]:
        directory = tmp_path / "recorded"
        directory.mkdir(exist_ok=True)
        raw = directory / "raw-pause-experiments.json"
        # The shape control-runtime.integration.ts actually writes: a top-level
        # `reports` list of {name, ok, detail, artifact} plus `sdk_version`
        # (see its writeFileSync call). An earlier revision of this fixture wrote
        # `{"experiments": [...]}`, which no producer emits and the assembler reads
        # as "no reports" -- so the honest reuse path exited 2 for a reason that had
        # nothing to do with reuse, and the one clean-tree test of provenance
        # preservation could never pass. The fixture's job is to stand in for the
        # real producer, so it has to carry the real producer's contract.
        raw.write_text(json.dumps({
            "sdk_version": "0.1.5",
            "observed_models": ["claude-sonnet-4-5"],
            "reports": [
                {
                    "name": "a pause barrier blocks side effects (background_bash)",
                    "ok": True,
                    "detail": "recorded fixture",
                    "artifact": {
                        "adapter_id": "claude",
                        "sdk_version": "0.1.5",
                        "permission_mode": "bypassPermissions",
                        "requested_tool_shape": "background_bash",
                        "requested": {"admission_closed": True},
                        "confirmed": {"state": "paused", "active_tool_count": 0},
                        "held_interval": {
                            "duration_ms": 30000, "new_admissions": 0,
                            "fixture_writes": 0, "fixture_service_calls": 0,
                            "task_output_bytes": 0, "observed_by": "fixture",
                        },
                    },
                },
            ],
        }))

        revision = subprocess.run(["git", "-C", str(WAVE2), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
        doc = {
            "source_revision": revision,
            "source_dirty": False,
            "sdk_version": "0.1.5",
            "permission_mode": "bypassPermissions",
            "runtime_image": PINNED,
            "raw_output_path": str(raw),
            "raw_output_sha256": sha256_of(raw),
            "run_nonce": "originalrun0001",
            # The provenance a reuse must PRESERVE. Root's finding 4: bind the
            # evidence to the "actual fixture run/nonce/target", and on the reuse path
            # specifically "preserve original provenance on reuse; no relabeling".
            "fixture_run_id": "w2-20260924-011500",
            "fixture_target": {
                "k8s": [{"kind": "Deployment", "name": "w2-fixture-gateway",
                         "namespace": "adp-agents", "uid": "uid-fixture-0001"}],
                "queues": [],
            },
        }
        doc.update(over)
        (directory / "experiment-binding.json").write_text(json.dumps(doc, indent=2))
        return raw, directory / "experiment-binding.json"

    return _make


# ---------------------------------------------------------------------------
# the runtime image is OBSERVED, not assumed
# ---------------------------------------------------------------------------
def test_the_runtime_image_comes_from_the_operators_recorded_observation(
        run_collect, recorded_run) -> None:
    """Root's finding: "pass actual runtime image from the 20- script".

    The image must originate from the API server, because a process cannot read its own
    image digest and the question is what is ACTUALLY running. It still does -- one step
    earlier. lib/worker_observation.py resolves status.containerStatuses[].imageID and
    records it against the pod uid; this script reads it from that document.
    """
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)])
    assert PINNED in run.output


def test_the_experiment_never_runs_an_in_pod_kubectl(run_collect, recorded_run) -> None:
    """THE boundary root required: "no in-worker kubectl assumption".

    The protected service account can neither get nor list pods, and must NOT be granted
    it "just to make a test helper work". An in-pod query would fail on RBAC and, because
    an unreadable pod leaves the image unobserved, would refuse every protected run --
    so this is a correctness property, not only a security one.
    """
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)])
    assert not any(c.startswith("kubectl") for c in run.calls), \
        f"the experiment queried the API server from inside the pod: {run.calls}"


def test_a_document_for_a_different_pod_uid_yields_no_image(run_collect, recorded_run,
                                                           tmp_path) -> None:
    """Reading the document is only an observation about THIS pod if the uid matches.

    Without that tie it would be an inherited claim: a document describing some other
    pod would supply this run's image axis. The uid is projected by the kubelet, so it
    is the one value this process could not have written.
    """
    doc = tmp_path / "expected-identity.json"
    doc.write_text(json.dumps({"expected_identity": {
        **EXPECTED_IDENTITY, "pod_uid": "99999999-9999-9999-9999-999999999999"}}))
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)])
    assert "records no image for this pod uid" in run.output
    assert run.rc != 0, "an unmatched document must not supply the image axis"
    assert run.refused_because("runtime_image"), run.output
    assert not run.ran_experiment


def test_a_document_with_no_pod_uid_yields_no_image(run_collect, recorded_run,
                                                    tmp_path) -> None:
    """An absent recorded uid must not compare equal to anything.

    `"" == ""` would read as agreement about an identity neither side stated -- the
    vacuous pass, at the point of measurement.
    """
    doc = tmp_path / "expected-identity.json"
    doc.write_text(json.dumps({"expected_identity": {
        k: v for k, v in EXPECTED_IDENTITY.items() if k != "pod_uid"}}))
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)])
    assert "records no image for this pod uid" in run.output
    assert run.refused_because("runtime_image"), run.output


def test_an_unobservable_image_is_left_unset_rather_than_guessed(run_collect,
                                                                recorded_run,
                                                                tmp_path) -> None:
    """A failed lookup must read as "not observed", never as a default.

    If discovery substituted a placeholder on failure, the binding would present an
    unidentified image as an identified one -- the false-absence defect this whole PR
    is about, reintroduced at the point of measurement.
    """
    # No projected uid at all: this process cannot show the document is about itself.
    empty_identity = tmp_path / "no-identity"
    empty_identity.mkdir(exist_ok=True)
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)],
                      env_extra={"W2_POD_IDENTITY_DIR": str(empty_identity)})
    assert run.rc != 0, "a reuse with no observed image must not be permitted"
    assert "NOT observed" in run.output
    # The image axis itself must be what objects, and the recorded side is pinned --
    # so the drift can only come from the current side being unobserved.
    assert run.refused_because("runtime_image"), run.output
    # The drift entry legitimately quotes the RECORDED image, so the assertion is on
    # the recorded binding's current-side value: it must be empty, not a stand-in.
    drift = next(d for d in run.verdict("reuse-verdict.json")["drift"]
                 if d["axis"] == "runtime_image")
    assert not drift["current"], f"an image was invented when none was observed: {drift}"
    assert not run.ran_experiment


def test_a_missing_projected_uid_is_diagnosed_as_not_being_the_fixture_pod(
        run_collect, recorded_run, tmp_path) -> None:
    """The diagnosis has to name the real cause.

    An operator reading "no projected pod uid" knows the pod was not created by the
    fixture renderer. An earlier revision reported this class of failure as a
    multi-container ambiguity, sending the reader to look for a sidecar that does not
    exist -- the outcome looked right while the explanation was wrong.
    """
    empty_identity = tmp_path / "no-identity"
    empty_identity.mkdir(exist_ok=True)
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)],
                      env_extra={"W2_POD_IDENTITY_DIR": str(empty_identity)})
    assert "no projected pod uid" in run.output
    assert "not running as the provisioned fixture pod" in run.output
    assert "W2_EXPERIMENT_CONTAINER" not in run.output, \
        "a missing projection was misdiagnosed as an ambiguous multi-container pod"


# ---------------------------------------------------------------------------
# --reuse verifies the bytes it will actually consume
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    bool(subprocess.run(["git", "-C", str(WAVE2), "status", "--porcelain"],
                        capture_output=True, text=True).stdout.strip()),
    reason="the working tree is dirty, which correctly refuses ANY reuse: a recorded "
           "SHA does not identify uncommitted code. The honest-path assertion is only "
           "meaningful on a clean tree (as in CI), so it is skipped rather than "
           "weakened to accommodate a local edit.")
def test_an_honest_reuse_is_permitted(run_collect, recorded_run) -> None:
    """The fix must not be a blanket refusal.

    --reuse exists so artifacts can be re-assembled without re-spending model calls.
    If the honest path stopped working, operators would route around the check, so
    this test is load-bearing for the others being credible: refusals only mean
    something if something can also be accepted.
    """
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)])
    verdict = run.verdict("reuse-verdict.json")
    assert verdict.get("reuse_permitted") is True, run.output
    assert not run.ran_experiment, "a reuse must never spend model calls"
    # And the reused bytes are the ones now standing as the evidence.
    assert (run.evidence / "artifacts" / "raw-pause-experiments.json").read_bytes() \
        == raw.read_bytes()


def test_a_substituted_raw_file_cannot_ride_in_on_a_valid_binding(run_collect,
                                                                 recorded_run,
                                                                 tmp_path) -> None:
    """THE seam defect: verify one artifact, consume another.

    The script copies $REUSE over the evidence dir's raw file. If the digest were
    checked against the path RECORDED at measurement time -- which still holds the
    correct bytes -- then pointing --reuse at a different file would pass the check
    and then install the substitute as the evidence. The assembled artifact would be
    indistinguishable from a real measurement of a run that never happened.
    """
    raw, binding_path = recorded_run()
    # The substitute sits BESIDE the genuine binding, because the script locates the
    # binding via dirname($REUSE). Placing it elsewhere would be refused earlier for
    # having no binding at all -- which is a different check, and would make this test
    # pass without exercising the digest at all. The recorded raw file is left intact,
    # so the recorded path still hashes correctly: that is precisely the condition
    # under which digesting the wrong file yields a false pass.
    substitute = binding_path.parent / "other-raw.json"
    substitute.write_text(json.dumps({"experiments": [{"id": "FABRICATED"}]}))
    assert raw.exists() and sha256_of(raw) == json.loads(
        binding_path.read_text())["raw_output_sha256"], "the recorded bytes must still be valid"

    run = run_collect(args=["--reuse", str(substitute)])
    assert run.rc != 0, "a substituted raw file was accepted as the recorded evidence"
    # Specifically the DIGEST must be what objects -- not some incidental refusal.
    assert run.refused_because("raw_output_sha256"), (
        f"refused, but not because the bytes differ: {run.output}")
    assert str(substitute) in run.output, \
        "the refusal must name the file it digested, i.e. the one being consumed"
    installed = run.evidence / "artifacts" / "raw-pause-experiments.json"
    assert not installed.exists() or "FABRICATED" not in installed.read_text(), \
        "the substituted bytes were installed as the evidence anyway"
    assert not run.ran_experiment


def test_raw_output_edited_after_the_fact_is_refused(run_collect, recorded_run) -> None:
    """Same bytes, same path, changed content: the digest is what notices."""
    raw, _ = recorded_run()
    raw.write_text(json.dumps({"experiments": [{"id": "pause_boundary", "edited": True}]}))
    run = run_collect(args=["--reuse", str(raw)])
    assert run.rc != 0
    assert run.refused_because("raw_output_sha256"), run.output
    assert "does not hash to the digest recorded" in run.output


@pytest.mark.parametrize("axis", ["raw_output_sha256", "run_nonce"])
def test_a_recorded_binding_without_provenance_is_refused(run_collect, recorded_run,
                                                          axis) -> None:
    """Root: "bind raw artifact digest plus original run nonce".

    Output recorded before provenance was tracked names no particular artifact and no
    particular run, so it vouches for any file presented to it.
    """
    raw, _ = recorded_run(**{axis: None})
    run = run_collect(args=["--reuse", str(raw)])
    assert run.rc != 0
    assert run.refused_because(axis), (
        f"refused, but not because {axis} was missing: {run.output}")
    assert not run.ran_experiment


def test_reuse_across_a_revision_change_is_refused(run_collect, recorded_run) -> None:
    """Yesterday's measurement does not describe today's PauseGate."""
    raw, _ = recorded_run(source_revision="0" * 40)
    run = run_collect(args=["--reuse", str(raw)])
    assert run.rc != 0
    assert run.refused_because("source_revision"), run.output


def test_a_reuse_with_no_binding_beside_it_is_refused(run_collect, tmp_path) -> None:
    """Untracked output cannot be shown to describe any revision."""
    orphan = tmp_path / "orphan" / "raw-pause-experiments.json"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("{}")
    run = run_collect(args=["--reuse", str(orphan)])
    assert run.rc != 0
    assert "no experiment-binding.json" in run.output


def test_a_refused_reuse_writes_no_harness_artifacts(run_collect, recorded_run) -> None:
    """A refusal must leave nothing behind that a later step could read as evidence.

    If the pause_* artifacts were written anyway, the harness would consume them and
    the refusal would be cosmetic.
    """
    raw, _ = recorded_run(source_revision="0" * 40)
    run = run_collect(args=["--reuse", str(raw)])
    assert run.rc != 0
    for name in ("pause_boundary.json", "pause_resume.json", "pause_expiry.json"):
        assert not (run.evidence / "artifacts" / name).exists(), \
            f"{name} was written despite the reuse being refused"


# ---------------------------------------------------------------------------
# the live path refuses BEFORE spending, and only against a real reference
# ---------------------------------------------------------------------------
# Root's findings 1-4 at c474c23ac were all about the live path, and all of them
# survived the previous revision because the seam never engaged the checks:
#
#   * `check-host` was invoked with no expected-identity, so it could only compare
#     shapes -- "a token file exists", "the ARN lacks the substring admin" -- which
#     root defeated with an ordinary pod in the wrong account;
#   * `capture` exited 0 on source_revision=unknown, source_dirty=true and even
#     host_allowed=false, so the money was spent and the artifact still looked real;
#   * the run nonce was minted here, naming nothing.
#
# These assert the script now refuses first. Every case must reach npx NEVER: the npx
# stub exits 98 precisely so that "a paid model call would have happened" is a test
# failure rather than an invisible cost.

def test_a_live_run_without_a_provisioning_record_is_refused_before_spending(
        run_collect) -> None:
    """No reference means the host check could only ever compare shapes.

    This is also the state every pre-existing invocation is in, so it must deny by
    omission and not merely when defeated.
    """
    # The harness normally supplies the document through W2_EXPECTED_IDENTITY, because
    # that is what a provisioned pod has. Cleared HERE specifically, since the state
    # under test is its ABSENCE -- and an absent document is what every pre-existing
    # invocation has.
    run = run_collect(args=[], env_extra={"W2_EXPECTED_IDENTITY": ""})
    assert run.rc != 0
    assert "--expected-identity is required" in run.output
    assert "self-description is not authorisation" in run.output
    assert not run.ran_experiment, "a paid model call was made despite the refusal"


def test_a_live_run_without_a_fixture_ledger_is_refused_before_spending(
        run_collect, tmp_path) -> None:
    """Root: bind to "actual fixture run/nonce/target, not independently minted".

    Without the ledger the script would be back to minting a nonce that names nothing.
    """
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"expected_identity": {"run_id": "w2-x"}}))
    run = run_collect(args=["--expected-identity", str(identity)])
    assert run.rc != 0
    assert "--ledger is required" in run.output
    assert "names nothing" in run.output
    assert not run.ran_experiment


def test_a_live_run_on_an_unidentified_host_never_reaches_the_model(
        run_collect, tmp_path) -> None:
    """The host check runs before anything is spent, and a refusal ends the run.

    The identity document here names a pod this process is not -- by UID, the one axis
    that cannot be reused or asserted -- so the refusal is an identity refusal rather
    than an incidental one, and it must arrive before npx.
    """
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"expected_identity": {
        "run_id": "w2-x", "account_id": "879318057152", "namespace": "adp-agents",
        "pod_name": "some-pod-this-is-not",
        "pod_uid": "99999999-9999-9999-9999-999999999999",
        "service_account": "w2-fixture-agent",
        "aws_role_arn": "arn:aws:iam::879318057152:role/w2-fixture-agent",
        "runtime_image": PINNED}}))
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"run_id": "w2-x", "run_nonce": "aaaabbbbccccdddd",
                                  "k8s": [{"kind": "Deployment", "name": "d",
                                           "namespace": "adp-agents", "uid": "u"}],
                                  "queues": []}))
    # The SDK stub must report the LOCKFILE's pinned version, or the run is refused by
    # the earlier lockfile-mismatch check -- a real refusal, but not the one under test,
    # which would let this pass against a script that had lost the host check entirely.
    pinned = json.loads(
        (WAVE2.parents[3] / "modules/agent-factory/agent/package-lock.json").read_text()
    )["packages"]["node_modules/@anthropic-ai/claude-agent-sdk"]["version"]
    run = run_collect(args=["--expected-identity", str(identity), "--ledger", str(ledger)],
                      env_extra={"W2_STUB_SDK_VERSION": pinned})
    assert run.rc != 0
    assert "Nothing was spent" in run.output, run.output
    # Specifically an IDENTITY refusal, on the uid: the pod we are is not the pod
    # provisioned. Asserting on the uid rather than on any refusal keeps this from
    # passing against a script that had lost the host check and refused for some
    # incidental reason instead.
    assert "but the run provisioned '99999999-9999-9999-9999-999999999999'" in run.output, \
        run.output
    assert not run.ran_experiment, "a paid model call was made on an unidentified host"


# ---------------------------------------------------------------------------
# the image query is not bypassable by the environment
#
# Root's remaining finding 1: "discover_runtime_image is BYPASSED if W2_RUNTIME_IMAGE
# is already set, then logs it as observed from pod status -- still a writable
# environment assertion." The env var is a claim any parent process can make; the pod
# status is a measurement. Collapsing the two let the one axis that was supposed to
# come from the API server be supplied by whoever launched the script.
# ---------------------------------------------------------------------------
def test_a_preset_image_does_not_skip_the_recorded_observation(run_collect, recorded_run,
                                                              tmp_path) -> None:
    """The recorded observation is consulted even when the environment claims an answer.

    Proven by making the two disagree: the environment names the pinned digest and the
    document names another. If the pre-set variable short-circuited discovery the run
    would proceed on the environment's word, which is the bypass. It must instead be
    refused, which is only possible if the document was read.
    """
    doc = tmp_path / "expected-identity.json"
    doc.write_text(json.dumps({"expected_identity": {
        **EXPECTED_IDENTITY, "runtime_image": "repo/agent@sha256:" + "c" * 64}}))
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)],
                      env_extra={"W2_RUNTIME_IMAGE": PINNED})
    assert "is not the image the" in run.output, (
        "a pre-set W2_RUNTIME_IMAGE skipped the recorded observation, so the image axis "
        f"was satisfied by an environment variable: {run.output}")
    assert "already present in the environment" not in run.output


def test_an_environment_image_disagreeing_with_the_pod_is_refused(run_collect,
                                                                 recorded_run) -> None:
    """Where the claim and the measurement disagree, neither wins -- the run is refused.

    Resolving in favour of the environment would attribute the evidence to bytes that
    did not execute it. Resolving in favour of the observation would silently discard a
    disagreement that means something is wrong with the launch.
    """
    raw, _ = recorded_run()
    other = "repo/agent@sha256:" + "b" * 64
    run = run_collect(args=["--reuse", str(raw)], env_extra={"W2_RUNTIME_IMAGE": other})
    assert run.rc != 0
    assert "is not the image the" in run.output, run.output
    assert not run.ran_experiment


def test_a_matching_environment_image_is_accepted_via_the_observation(run_collect,
                                                                    recorded_run) -> None:
    """The accepting case: same digest, different presentation.

    kubelet reports `docker-pullable://repo/agent@sha256:...`; a registry reference has
    no transport prefix. The digest is the content identity, so only it is compared --
    otherwise an honest launch would be refused for cosmetic reasons and operators
    would stop passing the variable at all.
    """
    raw, _ = recorded_run()
    registry_form = "repo/agent@sha256:" + "a" * 64
    run = run_collect(args=["--reuse", str(raw)],
                      env_extra={"W2_RUNTIME_IMAGE": registry_form})
    assert "confirmed by the environment" in run.output, run.output
    assert not run.refused_because("runtime_image"), run.output


def test_an_unverifiable_environment_image_is_not_treated_as_observed(run_collect,
                                                                    recorded_run,
                                                                    tmp_path) -> None:
    """The bypass itself: env var set, no observation available to corroborate it.

    Previously this path announced the variable as "observed from the pod status" and
    the image axis was satisfied without the API server ever being asked. It must now
    read as NOT observed -- a refusal, because an unidentified image cannot be bound to
    the evidence. The unobservable side is produced by removing the projected uid, which
    is the live equivalent of running somewhere the operator never provisioned.
    """
    empty_identity = tmp_path / "no-identity"
    empty_identity.mkdir(exist_ok=True)
    raw, _ = recorded_run()
    run = run_collect(args=["--reuse", str(raw)],
                      env_extra={"W2_RUNTIME_IMAGE": PINNED,
                                 "W2_POD_IDENTITY_DIR": str(empty_identity)})
    assert "observed from the pod status" not in run.output, (
        f"an unverified environment variable was announced as an observation: {run.output}")
    assert "NOT treated as observed" in run.output, run.output
    assert run.rc != 0
    assert run.refused_because("runtime_image"), run.output
    assert not run.ran_experiment


# ---------------------------------------------------------------------------
# a reuse preserves the original provenance
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    bool(subprocess.run(["git", "-C", str(WAVE2), "status", "--porcelain"],
                        capture_output=True, text=True).stdout.strip()),
    reason="a dirty tree correctly refuses ANY reuse, so the honest path cannot be "
           "exercised locally. Skipped rather than weakened: asserting a nonzero exit "
           "here would pass against a script that had lost the preservation entirely.")
def test_a_reuse_installs_the_recorded_binding_not_a_fresh_one(run_collect,
                                                              recorded_run) -> None:
    """Root's finding 4: "Preserve original provenance on reuse; no relabeling."

    Capturing a new binding on the reuse path would stamp this run's identity onto
    yesterday's measurement -- the artifact would name a run that did not produce it,
    which is the relabeling. So the recorded binding is installed verbatim and the
    script verifies that is what landed.
    """
    raw, recorded_path = recorded_run()
    run = run_collect(args=["--reuse", str(raw)])

    # The subject of this test is the BINDING the reuse installed. Asserted field by
    # field, because that is what relabeling would change.
    recorded = json.loads(recorded_path.read_text())
    written = run.binding()
    assert written, f"the reuse wrote no binding at all: {run.output}"
    for axis in ("run_nonce", "fixture_run_id", "fixture_target", "raw_output_sha256"):
        assert written.get(axis) == recorded.get(axis), (
            f"{axis} was relabeled on reuse: {recorded.get(axis)!r} -> {written.get(axis)!r}")
    assert "provenance preserved" in run.output

    # And the reuse itself must have been ACCEPTED, not merely have written
    # something on its way to a refusal. Both the verification and the preservation
    # step have to have passed.
    assert "reuse permitted" in run.output, run.output
    assert "the reuse preserved the recorded run's provenance" in run.output, run.output

    # The exit status belongs to the ASSEMBLER, not to the reuse.
    #
    # It is deliberately NOT asserted to be 0 here. `21-assemble-pause-artifacts.py`
    # exits 1 whenever a field the harness requires was never measured, and the
    # pause_expiry fields (auto-resume, the single neutral annotation, heartbeats,
    # the deadline clamp, cancellation-without-admission) are measured by no
    # experiment that exists yet -- the assembler says so itself. So on any honest
    # input recorded from today's producer, a successful reuse still exits 1, and
    # demanding 0 here would only be satisfiable by a fabricated raw file carrying
    # measurements no producer emits.
    #
    # What IS asserted: the exit is the assembler's unmeasured-fields status and not
    # a malformed-input or refusal status, and the reason is named. 2 is the
    # assembler's "this input is not usable" code -- which is what a fixture writing
    # a shape no producer emits used to produce, and is the defect this test lost
    # its coverage to.
    assert run.rc == 1, (
        f"expected the assembler's unmeasured-fields exit (1), got {run.rc}:\n{run.output}")
    assert "UNMEASURED REQUIRED FIELDS" in run.output, run.output
    assert "contains no reports" not in run.output, (
        "the reused raw file was not recognised as experiment output at all, so this "
        f"test exercised input handling rather than provenance:\n{run.output}")


def test_a_recorded_binding_naming_no_fixture_run_cannot_be_reused(run_collect,
                                                                 recorded_run) -> None:
    """Preservation is only meaningful if there is provenance to preserve.

    A binding carrying a minted nonce and no fixture run names nothing, so reusing it
    faithfully carries nothing forward -- which is indistinguishable from carrying
    evidence forward.
    """
    raw, _ = recorded_run(fixture_run_id=None)
    run = run_collect(args=["--reuse", str(raw)])
    assert run.rc != 0
    assert run.refused_because("fixture_run_id"), run.output


def test_installed_sdk_version_uses_exported_entry_point(tmp_path: Path) -> None:
    """The pinned SDK exports its entry point, not its package.json subpath."""
    import re
    import shutil

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the installed-package resolution regression')
    package = tmp_path / 'node_modules/@anthropic-ai/claude-agent-sdk'
    package.mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'name': '@anthropic-ai/claude-agent-sdk', 'version': '0.3.220',
        'type': 'module', 'exports': {'.': './sdk.mjs'},
    }))
    (package / 'sdk.mjs').write_text('export const query = () => {};\n')
    expression = re.search(r'INSTALLED="\$\(node -p "([^"]+)"', SCRIPT.read_text())
    assert expression, 'run the actual preflight expression, not a test-only substitute'
    observed = subprocess.run([node, '-p', expression.group(1)], cwd=tmp_path,
                              capture_output=True, text=True, check=False)
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == '0.3.220'
