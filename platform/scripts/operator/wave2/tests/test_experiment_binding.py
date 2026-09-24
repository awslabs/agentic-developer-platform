#!/usr/bin/env python3
"""Tests for experiment host refusal and reuse binding (issue #3968, blocker 6).

Two properties, and the second is the subtle one:

  (a) a bypassPermissions experiment must refuse to run on an administrator host,
      and must fail CLOSED on a host it cannot identify;

  (b) --reuse must not bind old experiment output to a new revision. This is more
      dangerous than a missing artifact, because a falsely-bound artifact is
      indistinguishable from a real measurement: the harness cannot detect it and
      the result looks like a pass.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

import experiment_binding as eb  # noqa: E402

# Sentinel so a test can pass identity=None ("the document is missing") distinctly
# from not overriding it at all. `None` cannot serve as its own default here.
_UNSET = object()

SHA_A = "a" * 40
SHA_B = "b" * 40


SCOPED_ARN = "arn:aws:sts::879318057152:assumed-role/w2-fixture-agent/pod"
ADMIN_ARN = "arn:aws:sts::879318057152:assumed-role/AdministratorAccess/session"
PINNED_IMAGE = "img@sha256:" + "a" * 64


def runner_for(revision: str = SHA_A, dirty: bool = False, arn: str | None = SCOPED_ARN):
    """A git + STS stub.

    `arn=None` models an STS call that fails, which classify_host must treat as an
    unknown blast radius rather than as an absence of privilege.
    """
    def run(argv):
        joined = " ".join(argv)
        if "rev-parse" in joined:
            return eb.CommandResult(0, revision + "\n")
        if "status --porcelain" in joined:
            return eb.CommandResult(0, " M file.py\n" if dirty else "")
        if "get-caller-identity" in joined:
            if arn is None:
                return eb.CommandResult(255, "", "unable to locate credentials")
            return eb.CommandResult(0, arn + "\n")
        return eb.CommandResult(1, "", "unexpected")
    return run


RUN_ID = "w2-20260924-011500"
POD_NAME = "w2-fixture-agent-7c9f4b8d2-xk4lq"
POD_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SA_NAME = "w2-fixture-agent"
NAMESPACE = "adp-agents"
LEDGER_NONCE = "6f1d2e3c4b5a6978"

# The provisioning record: written OUTSIDE the SDK pod by the root-owned launcher
# that created the fixture. It is the reference every identity check compares
# against, which is what makes those checks about identity rather than about shape.
EXPECTED_IDENTITY = {
    "run_id": RUN_ID,
    "account_id": "879318057152",
    "namespace": NAMESPACE,
    "pod_name": POD_NAME,
    # The server-assigned uid, and the only axis the experiment can prove with
    # something it did not author: kubelet writes it into the downwardAPI volume.
    # It replaced a `service-account.name` file that no mount ever provides -- a
    # downwardAPI fieldRef cannot reference spec.serviceAccountName.
    "pod_uid": POD_UID,
    "service_account": SA_NAME,
    "aws_role_arn": "arn:aws:iam::879318057152:role/w2-fixture-agent",
    "runtime_image": PINNED_IMAGE,
    # The Job uid the operator observed on the pod's controller ownerReference. It is
    # compared against the uid recorded when this run CREATED the Job, which is how a
    # same-named replacement between creation and observation is detected.
    "job_uid": "99999999-8888-7777-6666-555555555555",
}

IDENTITY_PATH = "/w2/expected-identity.json"
LEDGER_PATH = "/w2/ledger.json"

# The Job this run created, and the Pod it created in turn. Both are recorded at
# CREATION time from the server's response, which is what makes a later observation
# comparable to them: a same-named replacement carries a different uid.
JOB_UID = "99999999-8888-7777-6666-555555555555"

LEDGER = {
    "run_id": RUN_ID,
    "run_nonce": LEDGER_NONCE,
    "account_id": "879318057152",
    "k8s": [{"kind": "Deployment", "name": "w2-fixture-gateway", "namespace": NAMESPACE,
             "uid": "11111111-2222-3333-4444-555555555555"},
            {"kind": "Job", "name": "w2-fixture-worker", "namespace": NAMESPACE,
             "uid": JOB_UID},
            # The workload the experiment actually ran in. Without this entry the
            # ledger describes the run without naming the thing measured.
            {"kind": "Pod", "name": POD_NAME, "namespace": NAMESPACE, "uid": POD_UID}],
    "queues": [{"name": "adp-dev-w2-fixture.fifo", "url": "https://sqs/q"}],
}


def reader_for(token: str | None = "not-a-real-token-placeholder", namespace: str | None = NAMESPACE,
               *, pod_uid: str | None = POD_UID, pod_name: str | None = POD_NAME,
               identity: dict | None = _UNSET, ledger: dict | None = _UNSET,
               extra: dict[str, str | None] | None = None):
    """Stands in for every file classify_host reads. HERMETIC BY CONSTRUCTION.

    Root's finding on the previous revision: five shell tests "rely on
    /var/run/secrets/kubernetes.io/serviceaccount/namespace existing on developer
    host", and the fix must be "an injected reader/discovery seam or stub cat/file
    access, not real host mounts" -- explicitly NOT fabricated system credentials on
    root's host. So this reader answers from a mapping and returns None for anything
    it was not given. No test in this file touches a real path, and the fake token
    below is a literal placeholder, never a credential.

    The mounted files are the authoritative identity source because the KUBELET
    mounted them: unlike an environment variable, a process cannot forge them for
    itself. But presence alone is not identity, which is why the expected-identity
    document is part of this fixture rather than an optional extra.
    """
    table: dict[str, str | None] = {
        eb.SA_TOKEN_PATH: token,
        eb.SA_NAMESPACE_PATH: namespace,
        # The downwardAPI projection, NOT a service-account file. The pod's uid is
        # what the kubelet can actually give a container about itself.
        eb.POD_UID_PATH: pod_uid,
        eb.HOSTNAME_PATH: pod_name,
    }
    doc = EXPECTED_IDENTITY if identity is _UNSET else identity
    table[IDENTITY_PATH] = None if doc is None else json.dumps({"expected_identity": doc})
    led = LEDGER if ledger is _UNSET else ledger
    table[LEDGER_PATH] = None if led is None else json.dumps(led)
    table.update(extra or {})

    def read_file(path: str) -> str | None:
        return table.get(path)
    return read_file


# A host that satisfies every admission requirement: pod-mounted identity MATCHING
# the provisioning record, the provisioned digest-pinned image, the provisioned
# scoped ARN and no ambient credentials.
FIXTURE_ENV = {"W2_RUNTIME_IMAGE": PINNED_IMAGE, "KUBERNETES_SERVICE_HOST": "10.100.0.1",
               "W2_EXPECTED_IDENTITY": IDENTITY_PATH}


def allowed_host(**over):
    """The single admissible shape, so each test can move exactly one thing."""
    kwargs = {"env": dict(FIXTURE_ENV), "run": runner_for(), "read_file": reader_for()}
    kwargs.update(over)
    env = kwargs.pop("env")
    return eb.classify_host(env, **kwargs)


def binding(**over):
    """A COMPLETE binding: identity axes plus the provenance a reuse must carry.

    raw_output_sha256, run_nonce, fixture_run_id and fixture_target are included in
    the baseline because a binding lacking them is refused outright -- so omitting
    them here would make every reuse test pass for that reason instead of the one
    under test.
    """
    base = {"source_revision": SHA_A, "source_dirty": False, "sdk_version": "0.1.5",
            "permission_mode": "bypassPermissions", "runtime_image": PINNED_IMAGE,
            "raw_output_path": "/ev/artifacts/raw-pause-experiments.json",
            "raw_output_sha256": "sha256:" + "c" * 64,
            "run_nonce": "6f1d2e3c4b5a6978",
            "fixture_run_id": RUN_ID,
            "fixture_target": {"k8s": [{"kind": "Deployment", "name": "w2-fixture",
                                        "namespace": NAMESPACE, "uid": "uid-0001"}],
                               "queues": []}}
    base.update(over)
    return base


def digester(mapping: dict[str, str | None] | None = None, default: str | None = None):
    """A stub for sha256_file, so provenance tests need no real files.

    Defaults to returning the digest the baseline binding records, i.e. "the bytes on
    disk are unchanged" -- the honest case.
    """
    table = dict(mapping or {})
    fallback = default if default is not None else binding()["raw_output_sha256"]

    def digest_of(path: str) -> str | None:
        return table.get(path, fallback)
    return digest_of


# ---------------------------------------------------------------------------
# (a) host refusal
# ---------------------------------------------------------------------------
def test_operator_host_is_refused() -> None:
    """Root's host: real credentials, not in a cluster. This is the case that matters.

    bypassPermissions disables the tool-permission prompts, so an agent that
    reaches for the AWS CLI here performs an unreviewed administrator action.
    """
    verdict = eb.classify_host({"AWS_PROFILE": "adp-embark1"},
                               run=runner_for(arn=ADMIN_ARN), read_file=reader_for(None, None))
    assert verdict.allowed is False
    assert any("service-account token" in r for r in verdict.reasons)


def test_unrecognised_host_fails_closed() -> None:
    """An empty environment is not evidence of safety.

    A denylist would pass anything nobody enumerated, and the failure mode is an
    unsupervised agent on an admin host -- so the default must be refusal.
    """
    assert eb.classify_host({}).allowed is False


def test_a_fully_identified_scoped_fixture_is_allowed() -> None:
    """The fix must not be a blanket refusal.

    A guard that never admits anything is indistinguishable from a broken one, and
    the pressure to delete it is the same. Exactly one shape passes: pod-mounted
    identity, digest-pinned image, scoped ARN, no ambient credentials.
    """
    verdict = allowed_host()
    assert verdict.allowed is True, verdict.reasons
    assert verdict.signals["pod_namespace"] == "adp-agents"
    assert verdict.signals["runtime_image"] == PINNED_IMAGE


def test_two_env_vars_are_not_an_identity() -> None:
    """Root's finding, quoted: "two env vars are not identity".

    The previous revision admitted any host presenting W2_EXPERIMENT_FIXTURE=1 and
    KUBERNETES_SERVICE_HOST. Both are ordinary environment variables that the
    operator's own shell can set one `export` away -- so the check could be satisfied
    by the very host root prohibited, while reporting a verified fixture.
    """
    verdict = eb.classify_host(
        {"W2_EXPERIMENT_FIXTURE": "1", "KUBERNETES_SERVICE_HOST": "10.100.0.1"},
        run=runner_for(arn=ADMIN_ARN), read_file=reader_for(None, None))
    assert verdict.allowed is False
    assert verdict.signals["declared_fixture"] is True, (
        "the declaration is still recorded as context...")
    assert any("service-account token" in r for r in verdict.reasons), (
        "...but it must not be what admits the host")


def test_the_unsafe_host_override_no_longer_exists() -> None:
    """The removed escape hatch, pinned so it cannot return.

    The previous revision accepted W2_ALLOW_UNSAFE_EXPERIMENT_HOST=i-understand-the-risk
    and then ALLOWED the run, which is precisely what root prohibited: an unsupervised
    bypassPermissions agent on an administrator host. A guard against one catastrophic
    mistake cannot ship with an approved way to make it.
    """
    for value in ("i-understand-the-risk", "1", "true", "I-UNDERSTAND-THE-RISK"):
        verdict = eb.classify_host(
            {"AWS_PROFILE": "adp-embark1", "W2_ALLOW_UNSAFE_EXPERIMENT_HOST": value},
            run=runner_for(arn=ADMIN_ARN), read_file=reader_for(None, None))
        assert verdict.allowed is False, f"{value!r} must not admit an administrator host"
    assert "explicit_override" not in verdict.signals
    # And the refusal must not advertise a way around itself.
    assert not any("W2_ALLOW_UNSAFE" in r for r in verdict.reasons)


def test_even_a_perfect_fixture_cannot_be_overridden_into_a_bad_host() -> None:
    """The override is gone in both directions: it cannot rescue an admin host."""
    verdict = allowed_host(
        env=dict(FIXTURE_ENV, W2_ALLOW_UNSAFE_EXPERIMENT_HOST="i-understand-the-risk"),
        run=runner_for(arn=ADMIN_ARN))
    assert verdict.allowed is False
    assert any("administrative" in r for r in verdict.reasons)


def test_fixture_with_ambient_credentials_is_still_refused() -> None:
    """Being in a pod is not enough if the pod can call AWS as an administrator.

    This is the #5195 shape again: ambient credentials nobody accounted for.
    """
    verdict = allowed_host(env=dict(FIXTURE_ENV, AWS_ACCESS_KEY_ID="AKIAEXAMPLE"))
    assert verdict.allowed is False
    assert any("administrator-shaped credentials" in r for r in verdict.reasons)


@pytest.mark.parametrize("var", ["AWS_PROFILE", "AWS_ACCESS_KEY_ID",
                                 "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"])
def test_each_credential_shape_is_detected(var: str) -> None:
    verdict = allowed_host(env=dict(FIXTURE_ENV, **{var: "x"}))
    assert verdict.allowed is False
    assert var in verdict.signals["privileged_credential_env"]


@pytest.mark.parametrize("token,namespace,expected", [
    (None, "adp-agents", "service-account token"),
    ("tok", None, "no namespace"),
])
def test_pod_identity_must_come_from_the_mount_namespace(token, namespace, expected) -> None:
    """Identity has to come from files the kubelet mounted, not from the environment.

    A process can set any env var for itself; it cannot fabricate a projected
    service-account token mount.
    """
    verdict = allowed_host(read_file=reader_for(token, namespace))
    assert verdict.allowed is False
    assert any(expected in r for r in verdict.reasons)


def test_the_service_account_token_value_is_never_recorded() -> None:
    """Root: "Keep raw credentials out of source/logs/artifacts."

    The verdict is written to host-verdict.json, so a token echoed into signals would
    be a bearer credential committed to an evidence directory.
    """
    secret = "eyJhbGciOiJSUzI1NiJ9.SUPERSECRETPAYLOAD.sig"
    verdict = allowed_host(read_file=reader_for(secret, "adp-agents"))
    assert secret not in repr(verdict.signals)
    assert secret not in " ".join(verdict.reasons)


@pytest.mark.parametrize("image,why", [
    ("", "an unset image identifies nothing"),
    ("myrepo/agent:latest", "a floating tag resolves to different bytes over time"),
    ("myrepo/agent:v1.2.3", "even a version tag is mutable and can be re-pushed"),
])
def test_an_image_that_is_not_digest_pinned_is_refused(image, why) -> None:
    """Root's finding: "reject unknown SDK/source/image"."""
    verdict = allowed_host(env=dict(FIXTURE_ENV, W2_RUNTIME_IMAGE=image))
    assert verdict.allowed is False, f"admitted despite {why}"


def test_an_unresolvable_aws_identity_is_refused_not_assumed_harmless() -> None:
    """A failed STS call means the blast radius is UNKNOWN, not that it is zero.

    The pod may still hold an IRSA role; a credential lookup that errors proves
    nothing about what a tool call could do.
    """
    verdict = allowed_host(run=runner_for(arn=None))
    assert verdict.allowed is False
    assert any("could not be resolved" in r for r in verdict.reasons)


@pytest.mark.parametrize("marker", ["Administrator", "admin", "PowerUser", "root", "breakglass"])
def test_an_administrative_role_is_refused(marker: str) -> None:
    arn = f"arn:aws:sts::879318057152:assumed-role/{marker}-role/session"
    verdict = allowed_host(run=runner_for(arn=arn))
    assert verdict.allowed is False
    assert any("administrative" in r for r in verdict.reasons)


def test_host_verdict_is_carried_into_the_binding() -> None:
    """So the artifact itself shows where the experiment ran."""
    result = eb.current_binding(
        run=runner_for(), repo_root="/repo", sdk_version="0.1.5",
        permission_mode="bypassPermissions", runtime_image=PINNED_IMAGE,
        env=dict(FIXTURE_ENV))
    assert result["host_signals"]["runtime_image"] == PINNED_IMAGE
    assert "host_allowed" in result


# ---------------------------------------------------------------------------
# (b) reuse binding
# ---------------------------------------------------------------------------
def test_reuse_across_a_revision_change_is_refused() -> None:
    """THE defect. Yesterday's output reused after changing the PauseGate produces
    an artifact claiming to measure today's code.

    Not a warning: a warning scrolls past in a long script, and the artifact it
    produces outlives the terminal it was printed in.
    """
    outcome = eb.evaluate_reuse(binding(source_revision=SHA_A), binding(source_revision=SHA_B))
    assert outcome["reuse_permitted"] is False
    assert outcome["drift"][0]["axis"] == "source_revision"
    assert "never ran against" in outcome["explanation"]


def test_reuse_with_identical_identity_is_permitted() -> None:
    """--reuse must still work, or operators will bypass it to avoid paying twice.

    The honest case, and the one that keeps the other refusals meaningful: every
    identity axis matches, both sides observed them, and the raw bytes still hash to
    the digest recorded when they were measured.
    """
    outcome = eb.evaluate_reuse(binding(), binding(), digest_of=digester())
    assert outcome["reuse_permitted"] is True, outcome["explanation"]
    assert outcome["provenance_problems"] == []


def test_a_reuse_carries_the_ORIGINAL_run_nonce_not_a_new_one() -> None:
    """Root's finding: "bind raw artifact digest plus original run nonce".

    The nonce names the run that actually produced the evidence. A reuse happens in a
    NEW run, so the nonce is deliberately NOT an equality axis -- comparing nonces
    would refuse every reuse, including the honest ones the flag exists for. What must
    hold is that the recorded nonce is present, so the artifact states which run the
    measurement came from instead of implying it was this one.
    """
    assert "run_nonce" not in dict(eb.BINDING_AXES)
    recorded = binding(run_nonce="original-run-aaaa")
    current = binding(run_nonce="a-brand-new-nonce-bbbb")
    outcome = eb.evaluate_reuse(recorded, current, digest_of=digester())
    assert outcome["reuse_permitted"] is True, outcome["explanation"]
    assert "original-run-aaaa" in outcome["explanation"], (
        "the verdict must name the run that produced the evidence")


@pytest.mark.parametrize("axis", ["raw_output_sha256", "run_nonce"])
def test_a_binding_without_provenance_cannot_be_reused(axis: str) -> None:
    """A binding naming no digest and no run vouches for any file presented to it.

    This is the defect: identity axes could all match while the binding identified no
    particular OUTPUT -- so --reuse could attach a raw file from a different run, and
    the assembled artifact would be indistinguishable from a real measurement.
    """
    outcome = eb.evaluate_reuse(binding(**{axis: None}), binding(), digest_of=digester())
    assert outcome["reuse_permitted"] is False
    assert any(p["axis"] == axis for p in outcome["provenance_problems"])


def test_raw_output_edited_after_measurement_is_refused() -> None:
    """The bytes changed after they were measured, so they are not that evidence."""
    recorded = binding()
    outcome = eb.evaluate_reuse(
        recorded, binding(),
        digest_of=digester({recorded["raw_output_path"]: "sha256:" + "d" * 64}))
    assert outcome["reuse_permitted"] is False
    problem = next(p for p in outcome["provenance_problems"]
                   if p["axis"] == "raw_output_sha256")
    assert "does not hash to the digest recorded" in problem["reason"]


def test_a_missing_raw_output_file_is_refused_not_skipped() -> None:
    """An unreadable artifact is not a verified one.

    If an unreadable file skipped the digest check, deleting the raw output would
    make the reuse PASS -- the strongest claim from the least evidence.
    """
    recorded = binding()
    outcome = eb.evaluate_reuse(
        recorded, binding(), digest_of=digester({recorded["raw_output_path"]: None}))
    assert outcome["reuse_permitted"] is False
    assert any("not present to verify" in p["reason"]
               for p in outcome["provenance_problems"])


def test_the_file_actually_being_reused_is_the_one_digested() -> None:
    """--reuse names a file that need not be the recorded path.

    20-collect-pause-evidence.sh copies $REUSE over the evidence dir's raw file. If
    the digest were checked against the RECORDED path while a different file was
    consumed, the check would verify one artifact and ship another -- the same
    substitution the digest exists to catch, one level up. verify_path must therefore
    be digested INSTEAD of the recorded path.
    """
    recorded = binding()
    substitute = "/tmp/some-other-run/raw-pause-experiments.json"
    outcome = eb.evaluate_reuse(
        recorded, binding(), verify_path=substitute,
        # The recorded path still holds the right bytes; the substituted file does not.
        digest_of=digester({recorded["raw_output_path"]: recorded["raw_output_sha256"],
                            substitute: "sha256:" + "e" * 64}))
    assert outcome["reuse_permitted"] is False, (
        "the substituted file was accepted because a DIFFERENT file verified")
    problem = next(p for p in outcome["provenance_problems"]
                   if p["axis"] == "raw_output_sha256")
    assert substitute in problem["reason"], "the refusal must name the file it digested"


@pytest.mark.parametrize("axis", ["source_revision", "sdk_version", "runtime_image",
                                  "permission_mode", "raw_output_sha256", "run_nonce"])
def test_two_unknowns_are_not_an_agreement(axis: str) -> None:
    """Root's finding: "reject unknown SDK/source/image".

    With `source_revision: None` on BOTH sides the old comparison found no drift and
    reported "every identity axis matches", permitting the reuse. That is the
    strongest possible claim assembled from the complete absence of evidence -- the
    vacuous pass this PR exists to remove.
    """
    outcome = eb.evaluate_reuse(binding(**{axis: None}), binding(**{axis: None}),
                                digest_of=digester())
    assert outcome["reuse_permitted"] is False, f"two unknown {axis} values agreed"


@pytest.mark.parametrize("placeholder", ["unknown", "", "none", "n/a", "latest", "  UNKNOWN  "])
def test_placeholder_values_do_not_count_as_observations(placeholder: str) -> None:
    """Strings that LOOK like a value but record no observation.

    "unknown" matters because current_binding itself produces it when `git rev-parse`
    fails; "latest" matters because a floating tag identifies no particular bytes.
    """
    assert eb.is_unknown(placeholder) is True
    outcome = eb.evaluate_reuse(binding(runtime_image=placeholder),
                                binding(runtime_image=placeholder), digest_of=digester())
    assert outcome["reuse_permitted"] is False


def test_a_mutable_tag_on_both_sides_is_refused() -> None:
    """The image axis matching is not enough if the value cannot identify bytes.

    `latest == latest` is agreement about a POINTER, not about code. The same tag
    resolves to different images between the experiment and the reuse.
    """
    outcome = eb.evaluate_reuse(binding(runtime_image="myrepo/agent:latest"),
                                binding(runtime_image="myrepo/agent:latest"),
                                digest_of=digester())
    assert outcome["reuse_permitted"] is False


@pytest.mark.parametrize("axis,value", [
    ("sdk_version", "0.2.0"),
    ("permission_mode", "default"),
    ("runtime_image", "img@sha256:bb"),
    ("source_dirty", True),
])
def test_every_identity_axis_blocks_reuse(axis: str, value) -> None:
    """Each axis independently invalidates the binding.

    permission_mode matters because evidence gathered WITHOUT bypassPermissions
    does not demonstrate the behaviour under that mode at all.
    """
    outcome = eb.evaluate_reuse(binding(), binding(**{axis: value}))
    assert outcome["reuse_permitted"] is False
    assert any(d["axis"] == axis for d in outcome["drift"])


def test_missing_recorded_axis_is_a_mismatch_not_a_match() -> None:
    """Output from before an axis was tracked cannot be shown to agree with it.

    Treating absent-as-equal is the same defect as treating an error as absence:
    it manufactures agreement from missing data.
    """
    recorded = binding()
    del recorded["runtime_image"]
    outcome = eb.evaluate_reuse(recorded, binding())
    assert outcome["reuse_permitted"] is False
    drift = next(d for d in outcome["drift"] if d["axis"] == "runtime_image")
    assert "predates this axis" in drift["reason"]


def test_empty_recorded_binding_is_refused() -> None:
    """Old output with no binding at all must not reuse freely."""
    assert eb.evaluate_reuse({}, binding())["reuse_permitted"] is False


def test_dirty_tree_blocks_reuse_even_when_everything_matches() -> None:
    """A recorded SHA does not identify uncommitted code.

    Both sides agree on the SHA here, so a naive comparison permits the reuse --
    but neither run is reproducible from that SHA, so the binding names code
    nobody can recover.
    """
    outcome = eb.evaluate_reuse(binding(source_dirty=True), binding(source_dirty=True),
                                digest_of=digester())
    assert outcome["reuse_permitted"] is False
    assert "uncommitted changes" in outcome["explanation"]


def test_a_dirty_tree_at_MEASUREMENT_time_also_blocks_reuse() -> None:
    """Both sides are checked, not just the current one.

    The recorded run is the one that produced the evidence. If ITS tree was dirty, the
    SHA in the binding does not identify the code that was measured, however clean the
    tree is now.
    """
    outcome = eb.evaluate_reuse(binding(source_dirty=True), binding(source_dirty=False),
                                digest_of=digester())
    assert outcome["reuse_permitted"] is False


def test_drift_names_what_moved_in_operator_language() -> None:
    """The refusal has to be actionable without reading the source."""
    outcome = eb.evaluate_reuse(binding(), binding(sdk_version="0.9.9"))
    drift = outcome["drift"][0]
    assert drift["label"] == "the Claude Agent SDK version"
    assert drift["recorded"] == "0.1.5" and drift["current"] == "0.9.9"


def test_partial_match_is_not_a_partial_pass() -> None:
    """Two axes moved; there is no meaningful "80% about the current code"."""
    outcome = eb.evaluate_reuse(
        binding(), binding(source_revision=SHA_B, sdk_version="0.9.9"))
    assert outcome["reuse_permitted"] is False
    assert len(outcome["drift"]) == 2


# ---------------------------------------------------------------------------
# binding capture
# ---------------------------------------------------------------------------
def test_dirty_tree_is_recorded_as_an_axis() -> None:
    result = eb.current_binding(
        run=runner_for(dirty=True), repo_root="/repo", sdk_version="0.1.5",
        permission_mode="bypassPermissions", runtime_image=None, env=dict(FIXTURE_ENV))
    assert result["source_dirty"] is True


def test_unavailable_git_does_not_silently_report_a_revision() -> None:
    """"unknown" then fails the binding comparison, which is the correct outcome."""
    def run(argv):
        return eb.CommandResult(128, "", "fatal: not a git repository")
    result = eb.current_binding(
        run=run, repo_root="/nope", sdk_version="0.1.5",
        permission_mode="bypassPermissions", runtime_image=None, env=dict(FIXTURE_ENV))
    assert result["source_revision"] == "unknown"
    assert result["source_dirty"] is None
    assert eb.evaluate_reuse(binding(), result)["reuse_permitted"] is False


# ---------------------------------------------------------------------------
# identity is MATCHED against a provisioning record, not inferred from shape
# ---------------------------------------------------------------------------
# Root executed these reproductions at c474c23ac with no live calls, and they are
# the reason the previous revision was not accepted:
#
#   "classify_host with injected nonempty token file, ordinary-namespace, env image
#    repo@sha256:<64a>, and STS arn:aws:sts::111111111111:assumed-role/Worker/session
#    => host_allowed:true despite wrong account and no fixture UID/SA/role proof."
#
# Every check that fell for that was a SHAPE check: a token file exists, the ARN does
# not contain "admin". Neither names WHICH pod or WHICH role. A denylist of role names
# cannot be repaired either -- the set of privileged names is open (root used
# `Worker`), while the set of identities permitted to run an unsupervised
# bypassPermissions experiment has exactly one member.

WORKER_ARN = "arn:aws:sts::111111111111:assumed-role/Worker/session"


def test_roots_reproduction_is_refused() -> None:
    """The exact shape root reported as host_allowed:true.

    Nonempty token, an ordinary namespace, a digest-pinned image, and an
    unremarkably-named role in the WRONG account. Every individual signal looks
    healthy; none of them says this is the fixture that was provisioned.
    """
    verdict = eb.classify_host(
        {"W2_RUNTIME_IMAGE": PINNED_IMAGE, "W2_EXPECTED_IDENTITY": IDENTITY_PATH},
        run=runner_for(arn=WORKER_ARN),
        read_file=reader_for(namespace="default",
                            pod_uid="ffffffff-0000-0000-0000-000000000000",
                            pod_name="some-other-pod"))
    assert verdict.allowed is False
    # And specifically for identity reasons, not incidentally for something else.
    joined = " ".join(verdict.reasons)
    assert "not the scoped fixture role" in joined, joined
    assert "provisioned in" in joined or "account" in joined, joined


def test_a_privileged_role_with_an_unremarkable_name_is_refused() -> None:
    """Root: "A privileged role with another name (e.g. Worker) passes".

    This is why the denylist had to become an allowlist of one. `Worker` contains
    none of admin/administrator/poweruser/root/breakglass, so the old check admitted
    it -- and nothing about the name bounds what the role can actually do.
    """
    verdict = allowed_host(run=runner_for(
        arn="arn:aws:sts::879318057152:assumed-role/Worker/session"))
    assert verdict.allowed is False
    assert any("not the scoped fixture role" in r for r in verdict.reasons), verdict.reasons


def test_the_right_role_name_in_the_wrong_account_is_refused() -> None:
    """Root: "wrong-account/ordinary pod role also passes".

    The same role name in another account is a different principal with different
    permissions, so the account is part of the identity rather than context.
    """
    verdict = allowed_host(run=runner_for(
        arn="arn:aws:sts::111111111111:assumed-role/w2-fixture-agent/pod"))
    assert verdict.allowed is False
    assert any("account" in r for r in verdict.reasons), verdict.reasons


def test_a_mounted_token_in_an_ordinary_namespace_is_refused() -> None:
    """Root: "Reading any nonempty token file is not API authentication of its subject."

    Presence proves a mount. It does not prove WHICH service account was mounted, and
    it certainly does not prove this pod was authorised to run the experiment.
    """
    verdict = allowed_host(read_file=reader_for(namespace="default"))
    assert verdict.allowed is False
    assert any("authorised" in r for r in verdict.reasons), verdict.reasons


def test_another_pod_in_the_right_namespace_is_refused_on_its_uid() -> None:
    """Being in the fixture's namespace is not being the fixture's pod.

    Previously this was asserted against a projected `service-account.name` file. That
    check could never have run: a downwardAPI fieldRef cannot reference
    spec.serviceAccountName, and no service-account mount writes such a file. The uid
    is what the kubelet can actually give a container about itself, so the same defect
    class is pinned against the mechanism that exists.
    """
    verdict = allowed_host(read_file=reader_for(
        pod_uid="99999999-9999-9999-9999-999999999999"))
    assert verdict.allowed is False
    assert any("but the run provisioned" in r for r in verdict.reasons), verdict.reasons


def test_an_unreadable_pod_uid_denies_rather_than_skips() -> None:
    """A check that cannot be performed must not be treated as passed.

    With no projected uid there is nothing unforgeable to compare, so the guard must
    deny. Skipping it would be the vacuous pass at the level of the guard itself
    rather than of the artifact.
    """
    verdict = allowed_host(read_file=reader_for(pod_uid=None))
    assert verdict.allowed is False
    assert any("nothing unforgeable to match" in r for r in verdict.reasons), verdict.reasons


def test_an_expected_document_without_a_pod_uid_refuses() -> None:
    """The other side of the same gap: nothing to match AGAINST.

    load_expected_identity requires pod_uid, so this shape only arrives when a caller
    injects a document directly. It must not pass: comparing an observed uid against
    an absent expected one is `"" != ""` -> False, which would read as agreement.
    """
    without_uid = {k: v for k, v in EXPECTED_IDENTITY.items() if k != "pod_uid"}
    verdict = allowed_host(expected_identity=without_uid)
    assert verdict.allowed is False
    assert any("names no pod_uid" in r for r in verdict.reasons), verdict.reasons


def test_the_service_account_is_reported_as_operator_verified_not_self_read() -> None:
    """The boundary that must not quietly move.

    The protected SA cannot get or list pods, so the pod cannot read its own
    spec.serviceAccountName and is not granted permission to. The signal must say who
    established it, so a reader cannot mistake it for something the pod proved.
    """
    verdict = allowed_host()
    assert verdict.allowed is True, verdict.reasons
    assert verdict.signals["service_account_name"] == SA_NAME
    assert "operator" in verdict.signals["service_account_verified_by"]
    assert "cannot get or list pods" in verdict.signals["service_account_verified_by"]


def test_the_wrong_pod_in_the_right_namespace_and_service_account_is_refused() -> None:
    """Two pods can share a service account; only one was provisioned for this run."""
    verdict = allowed_host(read_file=reader_for(pod_name="w2-fixture-agent-OTHER"))
    assert verdict.allowed is False
    assert any("not the provisioned" in r for r in verdict.reasons), verdict.reasons


def test_a_pod_selector_pointing_elsewhere_is_refused_not_followed() -> None:
    """Root: "W2_EXPERIMENT_POD/container can currently select an unrelated pod".

    Evidence read from another pod describes another workload. The selector is an
    env var -- the class of input root already rejected as identity -- so pointing it
    away from the provisioned pod is refused rather than obeyed.
    """
    verdict = allowed_host(env={**FIXTURE_ENV, "W2_EXPERIMENT_POD": "unrelated-pod-abc"})
    assert verdict.allowed is False
    assert any("refused rather than followed" in r for r in verdict.reasons), verdict.reasons


def test_an_image_pinned_to_the_wrong_digest_is_refused() -> None:
    """Pinned to SOME digest is not pinned to the RIGHT digest.

    The earlier fix established that a mutable tag cannot identify bytes. This is the
    other half: a digest that identifies bytes nobody provisioned is a different
    workload wearing the fixture's name.
    """
    verdict = allowed_host(env={**FIXTURE_ENV,
                                "W2_RUNTIME_IMAGE": "img@sha256:" + "b" * 64})
    assert verdict.allowed is False
    assert any("wrong code" in r for r in verdict.reasons), verdict.reasons


def test_the_kubelet_transport_prefix_does_not_defeat_the_image_match() -> None:
    """The fix must not be a blanket refusal on a cosmetic difference.

    kubelet reports `docker-pullable://repo@sha256:...`; the provisioner records the
    reference it built. Same bytes, different presentation -- so the comparison is on
    the digest. Without this, the honest path fails in the real cluster and the
    pressure is to weaken the check.
    """
    verdict = allowed_host(env={**FIXTURE_ENV,
                                "W2_RUNTIME_IMAGE": "docker-pullable://" + PINNED_IMAGE})
    assert verdict.allowed is True, verdict.reasons


def test_an_unpinned_expected_image_cannot_admit_anything() -> None:
    """A reference that does not pin content cannot authorise a match against it."""
    assert eb.same_image(PINNED_IMAGE, "repo/agent:latest") is False
    assert eb.same_image("repo/agent:latest", "repo/agent:latest") is False


def test_an_unreadable_provisioning_record_refuses() -> None:
    """With no reference there is nothing to match against, so nothing may be admitted.

    This is the load-bearing default: the experiment cannot author its own
    authorisation, so a missing reference is a refusal rather than a fallback to
    self-description.
    """
    verdict = allowed_host(read_file=reader_for(identity=None))
    assert verdict.allowed is False
    assert any("Refusing rather than falling back to self-description" in r
               for r in verdict.reasons), verdict.reasons


def test_no_provisioning_record_path_at_all_refuses() -> None:
    """Distinct from unreadable: nothing was even NOMINATED as the reference.

    Worth its own test because it is the state a caller reaches by simply not passing
    the new argument -- i.e. the state every existing invocation is in. If this did
    not deny, the whole check could be bypassed by omission rather than by defeat.
    """
    verdict = allowed_host(env={k: v for k, v in FIXTURE_ENV.items()
                                if k != "W2_EXPECTED_IDENTITY"})
    assert verdict.allowed is False
    assert any("cannot establish its own authorisation" in r for r in verdict.reasons), \
        verdict.reasons


@pytest.mark.parametrize("field", ["run_id", "account_id", "namespace", "pod_name",
                                   "service_account", "aws_role_arn", "runtime_image"])
def test_an_incomplete_provisioning_record_refuses(field: str) -> None:
    """An unstated field would be matched against nothing and would admit anything.

    Parametrised because a partially-filled reference is the realistic failure: each
    omission silently disables exactly one check, and a guard with one disabled check
    reports the same verdict as a guard with none.
    """
    partial = {k: v for k, v in EXPECTED_IDENTITY.items() if k != field}
    identity, error = eb.load_expected_identity(
        IDENTITY_PATH, read_file=reader_for(identity=partial))
    assert identity is None
    assert field in (error or ""), error


def test_a_corrupt_provisioning_record_refuses() -> None:
    """Unparseable is unverifiable, which must deny rather than default."""
    identity, error = eb.load_expected_identity(
        IDENTITY_PATH, read_file=reader_for(extra={IDENTITY_PATH: "{not json"}))
    assert identity is None and "not valid JSON" in (error or "")


def test_the_admin_substring_check_can_still_deny_but_never_admits() -> None:
    """Belt and braces, in the safe direction only.

    An ARN that matches the provisioned role AND reads as administrative means the
    provisioning itself is wrong. Trusting the reference over that signal would make
    a mistake in the root-owned record silently authorise the thing it exists to
    prevent -- so the subordinate check is retained, and it can only deny.
    """
    admin_identity = {**EXPECTED_IDENTITY,
                      "aws_role_arn": "arn:aws:iam::879318057152:role/AdministratorAccess"}
    verdict = allowed_host(run=runner_for(arn=ADMIN_ARN),
                           read_file=reader_for(identity=admin_identity))
    assert verdict.allowed is False
    assert any("looks administrative" in r for r in verdict.reasons), verdict.reasons


# ---------------------------------------------------------------------------
# capture must REFUSE, not merely record
# ---------------------------------------------------------------------------
# Root: "capture ALWAYS writes binding and returns0, including
# source_revision=unknown, source_dirty=true/None, host_allowed=false, empty/raw
# digest... Reject unknown/dirty identity before running and verify post-run
# axes/bytes before admitting output. The current unknown refusal exists for reuse
# only."
#
# That last sentence is the defect: all the rigour about unknown axes lived in
# compare_bindings, which only runs on --reuse. The LIVE path -- the one that spends
# money and produces the evidence everything else derives from -- wrote whatever it
# found and exited 0.

def admissible_binding(**over):
    base = {"source_revision": SHA_A, "source_dirty": False, "sdk_version": "0.1.5",
            "permission_mode": "bypassPermissions", "runtime_image": PINNED_IMAGE,
            "host_allowed": True, "run_nonce": LEDGER_NONCE,
            # The target, bound to the pod the experiment ran in. A binding without
            # it names no workload, which is the shape root's `k8s: [{}]` produced.
            "fixture_target": {
                "k8s": [{"kind": "Pod", "name": POD_NAME, "namespace": NAMESPACE,
                         "uid": POD_UID}],
                "queues": [{"name": "adp-dev-w2-fixture.fifo", "url": "https://sqs/q"}],
                "bound_pod_uid": POD_UID,
                "bound_job_uid": JOB_UID,
            },
            "raw_output_path": "/ev/raw.json", "raw_output_sha256": "sha256:" + "c" * 64}
    base.update(over)
    return base


def test_a_clean_identified_binding_is_admissible() -> None:
    """Again: refusals only mean something if something can also be accepted."""
    assert eb.binding_problems(admissible_binding(), require_output=True) == []


@pytest.mark.parametrize("over,expected", [
    ({"source_revision": "unknown"}, "never identified"),
    ({"source_dirty": True}, "nobody can reproduce"),
    ({"source_dirty": None}, "never determined"),
    ({"sdk_version": None}, "sdk_version was not observed"),
    ({"runtime_image": None}, "runtime_image was not observed"),
    ({"runtime_image": "repo/agent:latest"}, "mutable tag"),
    ({"host_allowed": False}, "host was not admitted"),
    ({"run_nonce": None}, "cannot name the run"),
])
def test_an_unidentified_binding_is_refused_before_the_run(over, expected) -> None:
    """Each axis, refused BEFORE anything is spent.

    Pre-run is where this matters most: the refusal is free, whereas discovering
    afterwards that the evidence cannot be attributed to a revision means the money
    is already gone and the artifact still looks like a real measurement.
    """
    problems = eb.binding_problems(admissible_binding(**over), require_output=False)
    assert any(expected in p for p in problems), problems


def test_post_run_additionally_requires_the_bytes_to_have_been_observed() -> None:
    """A binding with no digest names no artifact, so it vouches for any file.

    Pre-run this is expected -- the output does not exist yet -- which is exactly why
    the two phases have different requirements rather than one lenient standard.
    """
    without = admissible_binding(raw_output_sha256=None, raw_output_path=None)
    assert eb.binding_problems(without, require_output=False) == []
    assert eb.binding_problems(without, require_output=True) != []


# ---------------------------------------------------------------------------
# the run nonce comes from the FIXTURE, not from a minted number
# ---------------------------------------------------------------------------
# Root: "Bind experiment evidence to actual fixture run/nonce/target, not
# independently minted W2_EXPERIMENT_NONCE with no ledger/target relation."
#
# The minted nonce was unique, which made it look like provenance. It named nothing.
# A nonce is provenance only if it identifies something independently known -- here,
# the fixture run whose target the experiment measured.

def test_the_run_nonce_is_taken_from_the_fixture_ledger() -> None:
    fields, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for())
    assert problems == [], problems
    assert fields["run_nonce"] == LEDGER_NONCE
    assert fields["fixture_run_id"] == RUN_ID


def test_the_evidence_names_the_target_it_measured() -> None:
    """"...with no ledger/target relation". A run id says WHEN; the target says WHAT.

    Carried as identities rather than bare names: a name can be reused by a later
    object, a uid cannot.
    """
    fields, _ = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for())
    target = fields["fixture_target"]
    assert target["k8s"][0]["uid"] == "11111111-2222-3333-4444-555555555555"
    assert target["queues"][0]["name"] == "adp-dev-w2-fixture.fifo"


def test_a_minted_nonce_cannot_override_the_fixtures_own() -> None:
    """The defect, precisely: the caller's number winning over the fixture's record.

    Silently preferring the argument would restore it, so a conflict is refused
    rather than resolved.
    """
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY,
        supplied_nonce="ffffffffffffffff", read_file=reader_for())
    assert any("must be bound to the run that created the target" in p for p in problems), \
        problems


def test_no_ledger_means_no_provenance() -> None:
    """A unique label with no referent is decoration, not evidence."""
    _, problems = eb.resolve_run_binding(
        ledger_path=None, expected=EXPECTED_IDENTITY, supplied_nonce="ffffffffffffffff",
        read_file=reader_for())
    assert any("names nothing" in p for p in problems), problems


def test_a_ledger_from_a_different_fixture_run_is_refused() -> None:
    """Evidence from one fixture run does not describe another run's target."""
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected={**EXPECTED_IDENTITY, "run_id": "w2-OTHER"},
        supplied_nonce=None, read_file=reader_for())
    assert any("different fixture runs" in p for p in problems), problems


def test_an_empty_ledger_has_no_target_to_have_measured() -> None:
    """A fixture that created nothing cannot be what the experiment observed."""
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for(ledger={**LEDGER, "k8s": [], "queues": []}))
    assert any("no target" in p for p in problems), problems


# ---------------------------------------------------------------------------
# the target must be a NAMED WORKLOAD, not merely agreement about the run
# ---------------------------------------------------------------------------
# Root's three executed counter-examples. Each satisfied every check that existed --
# the ledger agreed about run id and nonce -- while naming nothing the experiment
# measured. Agreement about labels is not a binding to a workload.

def test_an_anonymous_ledger_entry_is_not_a_target() -> None:
    """Root's counter-example: `k8s: [{}]`.

    One empty entry made the list non-empty, so "records no created resources" passed
    while the target identified nothing. A non-empty list of anonymous entries is the
    same evidence as an empty list, dressed to look like more.
    """
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for(ledger={**LEDGER, "k8s": [{}], "queues": []}))
    assert any("does not identify an object" in p for p in problems), problems


def test_a_partially_identified_entry_is_still_anonymous() -> None:
    """Every axis is required: a uid with no kind names an object of unknown type.

    `delete_k8s` needs kind, name and namespace to address the object at teardown, so
    an entry missing any of them is also unremovable.
    """
    for hole in ("kind", "name", "namespace", "uid"):
        entry = {"kind": "Pod", "name": POD_NAME, "namespace": NAMESPACE, "uid": POD_UID}
        del entry[hole]
        _, problems = eb.resolve_run_binding(
            ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
            read_file=reader_for(ledger={**LEDGER, "k8s": [entry]}))
        assert any("does not identify an object" in p for p in problems), (hole, problems)


def test_a_ledger_of_unrelated_objects_does_not_name_the_observed_pod() -> None:
    """Root's counter-example: an unrelated ConfigMap.

    Same run, same nonce, same account, fully identified entry -- and still no
    statement about the workload the experiment ran in. This is the case that kept
    passing: the evidence claimed to have measured run X's target while naming only
    objects the experiment never touched.
    """
    unrelated = {"kind": "ConfigMap", "name": "some-other-config",
                 "namespace": "adp-gateway",
                 "uid": "cccccccc-cccc-cccc-cccc-cccccccccccc"}
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for(ledger={**LEDGER, "k8s": [unrelated]}))
    assert any("does not record the Pod the experiment ran in" in p for p in problems), \
        problems


def test_a_pod_uid_matching_another_kind_is_not_the_pod() -> None:
    """The match is on kind AND uid, so a uid recorded under another kind is not it.

    Otherwise a ledger could satisfy the pod binding with a Deployment that happened
    to be recorded with the same uid string.
    """
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for(ledger={**LEDGER, "k8s": [
            {"kind": "Deployment", "name": "w2-fixture-gateway",
             "namespace": NAMESPACE, "uid": POD_UID}]}))
    assert any("does not record the Pod the experiment ran in" in p for p in problems), \
        problems


def test_a_replacement_job_between_creation_and_observation_is_refused() -> None:
    """Root's counter-example: a same-named Job recreated after this run created it.

    The uid the observer read off the pod's controller ownerReference must be the uid
    recorded when THIS run created the Job. The two were independent reads that nothing
    compared, so a Job deleted and recreated under the same name passed -- and the pod
    observed under it was never the one this run admitted.
    """
    observed_replacement = {**EXPECTED_IDENTITY,
                            "job_uid": "dddddddd-dddd-dddd-dddd-dddddddddddd"}
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=observed_replacement, supplied_nonce=None,
        read_file=reader_for())
    assert any("not the uid this run recorded when it CREATED" in p for p in problems), \
        problems


def test_a_ledger_from_a_foreign_account_is_refused() -> None:
    """Root: "Refuse ... foreign accounts".

    The same run id and the same resource names exist in any account. ownership.py
    refuses to LOAD a ledger from another account, but that runs in the operator's
    session; nothing re-established it here, where the ledger arrives as a path from
    outside.
    """
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for(ledger={**LEDGER, "account_id": "210987654321"}))
    assert any("different objects" in p for p in problems), problems


def test_a_ledger_without_an_account_cannot_place_its_objects() -> None:
    _, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for(ledger={k: v for k, v in LEDGER.items()
                                     if k != "account_id"}))
    assert any("records no account_id" in p for p in problems), problems


def test_the_bound_uids_are_published_on_the_target() -> None:
    """The accepting case must record WHAT it bound to, not just that it agreed.

    Downstream evidence cites these, and `binding_problems` refuses a target with no
    bound_pod_uid -- so a check that passed without publishing them would leave the
    later refusal unsatisfiable.
    """
    fields, problems = eb.resolve_run_binding(
        ledger_path=LEDGER_PATH, expected=EXPECTED_IDENTITY, supplied_nonce=None,
        read_file=reader_for())
    assert problems == [], problems
    assert fields["fixture_target"]["bound_pod_uid"] == POD_UID
    assert fields["fixture_target"]["bound_job_uid"] == JOB_UID
    assert fields["fixture_account_id"] == "879318057152"


def test_a_binding_with_no_target_is_inadmissible() -> None:
    """The second half: `resolve_run_binding` refuses, and so does the assembler.

    A binding assembled by another path could omit fixture_target entirely and reach
    binding_problems with every other axis satisfied. An absent target compared against
    nothing is the vacuous pass this module exists to remove.
    """
    without = {k: v for k, v in admissible_binding().items() if k != "fixture_target"}
    assert any("names no fixture target" in p
               for p in eb.binding_problems(without, require_output=True))

    anonymous = admissible_binding(fixture_target={"k8s": [{}], "queues": []})
    assert eb.binding_problems(anonymous, require_output=True) != []

    unbound = admissible_binding(fixture_target={
        "k8s": [{"kind": "Pod", "name": POD_NAME, "namespace": NAMESPACE, "uid": POD_UID}],
        "queues": []})
    assert any("no bound_pod_uid" in p
               for p in eb.binding_problems(unbound, require_output=True))


# ---------------------------------------------------------------------------
# (g) a reuse must preserve provenance, and there must be provenance to preserve
#
# Root's finding 4, second half: "Preserve original provenance on reuse; no
# relabeling." Preservation only means something if the recorded binding actually
# names the fixture run and the target it measured -- otherwise the reuse faithfully
# carries forward nothing at all, which looks identical to carrying forward evidence.
# ---------------------------------------------------------------------------
def test_a_recorded_binding_with_no_fixture_run_cannot_be_reused() -> None:
    """A nonce with no fixture run behind it is the minted-nonce defect, recorded.

    Output measured before the nonce came from the ledger carries a unique number and
    no referent. Reusing it preserves that number honestly and still produces an
    artifact that names no run anyone can look up.
    """
    outcome = eb.evaluate_reuse(binding(fixture_run_id=None), binding(),
                                digest_of=digester())
    assert outcome["reuse_permitted"] is False
    axes = [p["axis"] for p in outcome["provenance_problems"]]
    assert "fixture_run_id" in axes, outcome


def test_a_recorded_binding_with_no_target_cannot_be_reused() -> None:
    """Evidence that names no target states when it was taken and nothing about what."""
    outcome = eb.evaluate_reuse(binding(fixture_target=None), binding(),
                                digest_of=digester())
    assert outcome["reuse_permitted"] is False
    assert "fixture_target" in [p["axis"] for p in outcome["provenance_problems"]], outcome


def test_a_target_with_the_right_shape_and_no_entries_is_not_a_target() -> None:
    """The subtler case: the field is present, the dict is non-empty, the lists are empty.

    `{"k8s": [], "queues": []}` passes any is-it-there check and identifies nothing.
    Root's own distinction, applied one level in: shape is not identity.
    """
    outcome = eb.evaluate_reuse(binding(fixture_target={"k8s": [], "queues": []}),
                                binding(), digest_of=digester())
    assert outcome["reuse_permitted"] is False
    problem = next(p for p in outcome["provenance_problems"]
                   if p["axis"] == "fixture_target")
    assert "names no resource" in problem["reason"], problem


def test_the_fixture_run_and_target_are_not_equality_axes() -> None:
    """They are carried, not compared -- the same reason the nonce is not compared.

    A reuse runs in a new process, and the point is that the ORIGINAL provenance
    survives. If these were equality axes the honest reuse would still work (both
    sides describe the same fixture), but the requirement being encoded is
    preservation, so it is stated where preservation is checked.
    """
    axes = dict(eb.BINDING_AXES)
    assert "fixture_run_id" not in axes
    assert "fixture_target" not in axes
    assert "fixture_run_id" in dict(eb.PROVENANCE_AXES)
    assert "fixture_target" in dict(eb.PROVENANCE_AXES)


@pytest.mark.parametrize("value", [None, {}, [], "", {"k8s": [], "queues": []}])
def test_an_empty_container_is_not_an_observation(value) -> None:
    """`{} == {}` would otherwise read as agreement about a target neither side has."""
    assert eb.names_a_target(value) is False


def test_a_populated_target_is_recognised() -> None:
    """The accepting case, so the refusals above are not a blanket rejection."""
    assert eb.names_a_target(binding()["fixture_target"]) is True


def test_a_reuse_that_rewrites_the_nonce_is_caught() -> None:
    """Root: "Preserve original provenance on reuse; no relabeling."

    The relabeling is not hypothetical bookkeeping: capturing a fresh binding beside
    reused bytes produces an artifact whose nonce names THIS run, while the
    measurement came from another. Nothing downstream can tell the difference.
    """
    recorded = binding(run_nonce="original-run-aaaa")
    written = binding(run_nonce="todays-run-bbbb")
    problems = eb.relabeling_problems(recorded, written)
    assert any("run_nonce was relabeled" in p for p in problems), problems
    assert any("did not produce it" in p for p in problems), problems


@pytest.mark.parametrize("axis", eb.PRESERVED_AXES)
def test_every_preserved_axis_is_actually_compared(axis: str) -> None:
    """Each axis is named individually, so losing one from the loop fails a test.

    Parametrized over PRESERVED_AXES rather than a hand-written list: adding an axis
    to the tuple without comparing it would otherwise go unnoticed.
    """
    problems = eb.relabeling_problems(binding(), binding(**{axis: "something-else"}))
    assert any(axis in p for p in problems), (axis, problems)


def test_a_faithful_reuse_reports_no_relabeling() -> None:
    """The accepting case: the recorded binding installed verbatim."""
    assert eb.relabeling_problems(binding(), binding()) == []


def test_a_dropped_axis_counts_as_relabeling() -> None:
    """Losing the provenance is not more honest than rewriting it.

    A binding with no nonce at all states nothing about which run measured the bytes,
    so it is the same defect with a different value: `None != "original-run-aaaa"`.
    """
    written = binding()
    del written["fixture_target"]
    problems = eb.relabeling_problems(binding(), written)
    assert any("fixture_target" in p for p in problems), problems
