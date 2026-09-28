#!/usr/bin/env python3
"""Tests for worker_observation -- the operator's runtime binding (issue #3968).

Two questions are being pinned here, and they are different questions:

  1. Is this pod the one this run CREATED? Settled from ownerReferences, on the
     server-assigned Job uid. A label match is not an answer -- anything can wear a
     label, and root's reproductions of the ledger defects were all of this shape:
     a populated structure with no identity in it.

  2. Given that it is, does the gateway's verifier accept what it has become? Those
     are the conditions no template can promise (phase, resolved imageID, podIP,
     exactly one container status), so they are read from the live object.

The order matters and is tested: ownership is settled before phase or image is
reported, because reporting facts about somebody else's pod is how acting on it
starts.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import copy

import pytest

from worker_observation import (
    ObservationError,
    digest_of,
    expected_identity_document,
    observe_worker_pod,
)

RUN_ID = "w2-20260924-0130"
NONCE = "deadbeefcafe0123"
JOB_UID = "11111111-2222-3333-4444-555555555555"
POD_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SA = "agent-authority-worker-sa"
DIGEST = "sha256:" + "cd" * 32
OTHER_DIGEST = "sha256:" + "ef" * 32
ROLE_ARN = "arn:aws:iam::879318057152:role/adp-dev-w2-fixture-worker"


def live_pod(**over) -> dict:
    """A pod as the API server returns it for a running protected worker."""
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "w2-fixture-worker-20260924-0130-x7k2p",
            "namespace": "adp-agents",
            "uid": POD_UID,
            "labels": {
                "adp.io/w2-fixture": RUN_ID,
                "adp.io/w2-nonce": NONCE,
                "app": "w2-fixture-worker-20260924-0130",
                "job-name": "w2-fixture-worker-20260924-0130",
            },
            "ownerReferences": [{
                "apiVersion": "batch/v1", "kind": "Job",
                "name": "w2-fixture-worker-20260924-0130",
                "uid": JOB_UID, "controller": True, "blockOwnerDeletion": True,
            }],
        },
        "spec": {
            "serviceAccountName": SA,
            "containers": [{"name": "agent-worker"}],
        },
        "status": {
            "phase": "Running",
            "podIP": "10.0.42.17",
            "containerStatuses": [{
                "name": "agent-worker",
                "ready": True,
                "imageID": f"docker-pullable://123.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@{DIGEST}",
                "state": {"running": {"startedAt": "2026-09-24T01:31:07Z"}},
            }],
        },
    }
    pod.update(over)
    return pod


def observe(pod: dict | None = None, **over):
    kwargs = dict(run_id=RUN_ID, nonce=NONCE, job_uid=JOB_UID,
                  service_account=SA, approved_digests=[DIGEST])
    kwargs.update(over)
    return observe_worker_pod(pod if pod is not None else live_pod(), **kwargs)


# ---------------------------------------------------------------------------
# digest_of -- the comparison every other check rests on
# ---------------------------------------------------------------------------
def test_a_kubelet_transport_prefix_is_not_part_of_the_identity() -> None:
    """kubelet reports `docker-pullable://repo@sha256:...`; the digest is the identity."""
    assert digest_of(f"docker-pullable://repo/name@{DIGEST}") == DIGEST
    assert digest_of(f"repo/name@{DIGEST}") == DIGEST


def test_a_registry_alias_does_not_change_the_digest() -> None:
    """The same bytes can be reported under a different registry host.

    Comparing full references would refuse a correct pod because ECR answered with
    an alias the operator did not record.
    """
    assert digest_of(f"111.dkr.ecr.us-east-1.amazonaws.com/x@{DIGEST}") == \
        digest_of(f"222.dkr.ecr.eu-west-1.amazonaws.com/y@{DIGEST}")


@pytest.mark.parametrize("value", [
    "repo/name:latest",              # a tag names no bytes
    "repo/name",                     # nothing at all
    "repo/name@sha256:abc",          # truncated
    "repo/name@sha256:" + "zz" * 32,  # not hex
    "repo/name@sha512:" + "cd" * 32,  # wrong algorithm
    "",
    None,
])
def test_anything_that_is_not_a_full_digest_yields_none(value) -> None:
    """None is a refusal at every call site, never a wildcard."""
    assert digest_of(value) is None


# ---------------------------------------------------------------------------
# ownership: is this pod ours?
# ---------------------------------------------------------------------------
def test_the_happy_path_reports_what_the_pod_actually_is() -> None:
    result = observe()
    assert result["pod_uid"] == POD_UID
    assert result["runtime_image_digest"] == DIGEST
    assert result["service_account"] == SA
    assert result["pod_ip"] == "10.0.42.17"
    assert result["job_uid"] == JOB_UID


def test_a_pod_owned_by_a_different_job_is_refused_even_wearing_our_labels() -> None:
    """THE defect class root reproduced, in its pod form.

    A same-named Job from an earlier run has a different uid. Its pods carry the same
    labels, the same name prefix, the same everything a selector can see -- and
    deleting one at teardown would destroy a resource this run did not create.
    """
    pod = live_pod()
    pod["metadata"]["ownerReferences"][0]["uid"] = "99999999-9999-9999-9999-999999999999"
    with pytest.raises(ObservationError, match="not controlled by the Job this run created"):
        observe(pod)


def test_a_pod_with_no_owner_references_is_refused() -> None:
    """A bare pod someone created by hand can wear any label it likes."""
    pod = live_pod()
    pod["metadata"]["ownerReferences"] = []
    with pytest.raises(ObservationError, match="not controlled by the Job"):
        observe(pod)


def test_a_non_controller_owner_reference_is_not_ownership() -> None:
    """Referencing the Job among your owners is not being its child."""
    pod = live_pod()
    pod["metadata"]["ownerReferences"][0]["controller"] = False
    with pytest.raises(ObservationError, match="not controlled by the Job"):
        observe(pod)


def test_an_owner_reference_to_a_different_kind_with_our_uid_is_refused() -> None:
    """Uid equality across kinds is not descent.

    Contrived, but the check is `kind == Job AND uid == AND controller` precisely so
    that no single coincidence is sufficient.
    """
    pod = live_pod()
    pod["metadata"]["ownerReferences"] = [{
        "kind": "ReplicaSet", "name": "x", "uid": JOB_UID, "controller": True}]
    with pytest.raises(ObservationError, match="not controlled by the Job"):
        observe(pod)


def test_a_pod_without_a_uid_is_refused() -> None:
    """No uid means nothing can be proven ours at teardown."""
    pod = live_pod()
    pod["metadata"].pop("uid")
    with pytest.raises(ObservationError, match="no metadata.uid"):
        observe(pod)


def test_an_absent_job_uid_cannot_be_treated_as_matching_anything() -> None:
    """An empty binding value must refuse, not vacuously accept.

    This is the shape of the ledger defect root executed: a structure that looks
    populated and relates nothing. An empty job_uid compared with `==` against an
    absent ownerReference uid could otherwise 'match'.
    """
    with pytest.raises(ObservationError, match="no Job uid was supplied"):
        observe(job_uid="")


def test_a_pod_from_another_run_is_refused_on_the_run_label() -> None:
    pod = live_pod()
    pod["metadata"]["labels"]["adp.io/w2-fixture"] = "w2-someone-elses-run"
    with pytest.raises(ObservationError, match="not this run's"):
        observe(pod)


def test_a_pod_with_the_wrong_nonce_is_refused() -> None:
    """The run id shape is guessable; the nonce is what separates two runs."""
    pod = live_pod()
    pod["metadata"]["labels"]["adp.io/w2-nonce"] = "0000000000000000"
    with pytest.raises(ObservationError, match="nonce"):
        observe(pod)


def test_ownership_is_settled_before_the_image_is_reported() -> None:
    """Order of refusals, asserted deliberately.

    A foreign pod running an unapproved image must be refused as FOREIGN. Reporting
    its image first would mean the operator's first fact about somebody else's
    workload is a finding about its contents.
    """
    pod = live_pod()
    pod["metadata"]["ownerReferences"][0]["uid"] = "foreign-uid"
    pod["status"]["containerStatuses"][0]["imageID"] = f"repo@{OTHER_DIGEST}"
    with pytest.raises(ObservationError, match="not controlled by the Job"):
        observe(pod)


# ---------------------------------------------------------------------------
# the verifier conditions no template can promise
# ---------------------------------------------------------------------------
def test_a_terminating_pod_is_refused() -> None:
    """workload.py refuses a pod with a deletionTimestamp.

    It is still Running and still has an IP, so every other check passes; the pod is
    simply going away, and evidence bound to it describes a workload that will not
    exist.
    """
    pod = live_pod()
    pod["metadata"]["deletionTimestamp"] = "2026-09-24T01:40:00Z"
    with pytest.raises(ObservationError, match="terminating"):
        observe(pod)


@pytest.mark.parametrize("phase", ["Pending", "Succeeded", "Failed", "Unknown"])
def test_only_a_running_pod_is_bound(phase: str) -> None:
    pod = live_pod()
    pod["status"]["phase"] = phase
    with pytest.raises(ObservationError, match="not 'Running'"):
        observe(pod)


def test_a_terminated_worker_in_a_running_pod_is_refused() -> None:
    """Root's executed regression (5808699406).

    `status.phase: Running` with the agent-worker container `{terminated: {exitCode: 1}}`
    and `ready: false` was returned as an ACCEPTED observation, `started_at` silently
    None. Pod phase is not the container's state: `Running` means at least one container
    runs, which stays true while the worker itself has exited and a sidecar lives on --
    and the worker is what the gateway admits.

    workload.py:151 refuses on this exact axis, so accepting it would claim an admission
    condition the real verifier rejects.
    """
    pod = live_pod()
    pod["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 1}}
    pod["status"]["containerStatuses"][0]["ready"] = False
    with pytest.raises(ObservationError, match="not running"):
        observe(pod)


def test_a_waiting_worker_in_a_running_pod_is_refused() -> None:
    """The other non-running state, e.g. a crash-loop backoff between attempts."""
    pod = live_pod()
    pod["status"]["containerStatuses"][0]["state"] = {
        "waiting": {"reason": "CrashLoopBackOff"}}
    pod["status"]["containerStatuses"][0]["ready"] = False
    with pytest.raises(ObservationError, match="not running"):
        observe(pod)


def test_a_terminated_worker_is_refused_even_when_reported_ready() -> None:
    """`ready` is the kubelet's probe verdict, not the container's state.

    The refusal must rest on the state the verifier reads, so a stale or contradictory
    `ready: true` cannot make a terminated container look admissible.
    """
    pod = live_pod()
    pod["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
    pod["status"]["containerStatuses"][0]["ready"] = True
    with pytest.raises(ObservationError, match="not running"):
        observe(pod)


def test_an_absent_or_empty_container_state_is_refused() -> None:
    """A state that says nothing cannot establish running.

    Unprovable reads as refused, not as optimistically true -- the same
    refusal-to-guess applied on every other axis here.
    """
    for state in ({}, None):
        pod = live_pod()
        pod["status"]["containerStatuses"][0]["state"] = state
        with pytest.raises(ObservationError, match="not running"):
            observe(pod)

    missing = live_pod()
    del missing["status"]["containerStatuses"][0]["state"]
    with pytest.raises(ObservationError, match="not running"):
        observe(missing)


def test_a_running_image_id_does_not_imply_a_running_container() -> None:
    """imageID is populated at pull time, before start, and survives exit.

    Named explicitly because root's directive forbids inferring running from imageID
    or pod phase: the terminated container below carries a fully resolved, APPROVED
    digest, which is the one thing that might otherwise look like evidence of running.
    """
    pod = live_pod()
    pod["status"]["containerStatuses"][0]["state"] = {
        "terminated": {"exitCode": 137, "reason": "OOMKilled"}}
    with pytest.raises(ObservationError, match="not running"):
        observe(pod)


def test_the_running_state_condition_is_reported_as_observed() -> None:
    """An accepted observation must claim this condition, having actually checked it.

    The condition list is the document's account of what was verified; a condition
    enforced but unclaimed understates the evidence, and one claimed but unenforced
    overstates it.
    """
    observation = observe()
    assert "containerStatus agent-worker state is running" in \
        observation["verifier_conditions_observed"]
    assert observation["started_at"] == "2026-09-24T01:31:07Z"


def test_a_pod_with_no_ip_yet_is_refused() -> None:
    """Running is not the same as reachable; the listener binds to POD_IP."""
    pod = live_pod()
    pod["status"]["podIP"] = ""
    with pytest.raises(ObservationError, match="no status.podIP"):
        observe(pod)


def test_a_whitespace_ip_is_not_an_ip() -> None:
    pod = live_pod()
    pod["status"]["podIP"] = "   "
    with pytest.raises(ObservationError, match="no status.podIP"):
        observe(pod)


def test_the_wrong_service_account_is_refused() -> None:
    """Read from the live spec, because this is what the verifier compares."""
    pod = live_pod()
    pod["spec"]["serviceAccountName"] = "agent-scaledjob-sa"
    with pytest.raises(ObservationError, match="not the protected"):
        observe(pod)


def test_two_container_statuses_under_the_expected_name_are_refused() -> None:
    pod = live_pod()
    pod["status"]["containerStatuses"].append(
        copy.deepcopy(pod["status"]["containerStatuses"][0]))
    with pytest.raises(ObservationError, match="exactly one"):
        observe(pod)


def test_a_missing_container_status_is_refused() -> None:
    """A sidecar-only status list names no agent-worker to attribute the image to."""
    pod = live_pod()
    pod["status"]["containerStatuses"] = [{"name": "istio-proxy", "imageID": f"x@{DIGEST}"}]
    with pytest.raises(ObservationError, match="exactly one"):
        observe(pod)


# ---------------------------------------------------------------------------
# the resolved image
# ---------------------------------------------------------------------------
def test_the_resolved_digest_is_taken_from_status_not_from_spec() -> None:
    """A tag in the spec can have moved since admission; imageID cannot.

    The spec here names a tag and the status names the digest actually running, which
    is the situation the check exists for.
    """
    pod = live_pod()
    pod["spec"]["containers"][0]["image"] = "repo/adp-agent-runtime:latest"
    assert observe(pod)["runtime_image_digest"] == DIGEST


def test_an_unresolvable_image_id_is_refused() -> None:
    pod = live_pod()
    pod["status"]["containerStatuses"][0]["imageID"] = "repo/adp-agent-runtime:latest"
    with pytest.raises(ObservationError, match="names no sha256 digest"):
        observe(pod)


def test_a_running_digest_that_is_not_approved_is_refused() -> None:
    """The gateway would refuse this worker at bootstrap; better to know now."""
    pod = live_pod()
    pod["status"]["containerStatuses"][0]["imageID"] = f"repo@{OTHER_DIGEST}"
    with pytest.raises(ObservationError, match="NOT on the gateway's approved list"):
        observe(pod)


def test_an_empty_approved_list_refuses_rather_than_admitting_everything() -> None:
    with pytest.raises(ObservationError, match="approved worker image digest list is empty"):
        observe(approved_digests=[])


def test_a_whitespace_only_approved_list_is_empty() -> None:
    """`"".split(",")` is `['']`, which is how an unset env var arrives."""
    with pytest.raises(ObservationError, match="approved worker image digest list is empty"):
        observe(approved_digests=["", "  "])


def test_approved_entries_are_compared_after_stripping() -> None:
    """A comma-separated ConfigMap value carries incidental whitespace."""
    assert observe(approved_digests=[f" {DIGEST} ", OTHER_DIGEST])["runtime_image_digest"] == DIGEST


# ---------------------------------------------------------------------------
# the expected-identity document
# ---------------------------------------------------------------------------
def build_doc(**over):
    kwargs = dict(account_id="879318057152", region="us-east-1", aws_role_arn=ROLE_ARN,
                  ledger_path="/tmp/ledger.json",
                  control_endpoint="https://x.execute-api.us-east-1.amazonaws.com/s/internal/v1/agent")
    kwargs.update(over)
    return expected_identity_document(observe(), **kwargs)


def test_the_document_names_the_pod_uid_not_only_the_name() -> None:
    """A Job can produce a same-named pod after a deletion.

    The experiment proves which pod it is by reading its own projected uid, so a
    document that named only the pod NAME would accept the replacement -- which is
    the gap that made `service-account.name` matching unworkable in the first place.
    """
    identity = build_doc()["expected_identity"]
    assert identity["pod_uid"] == POD_UID
    assert identity["pod_name"]
    assert identity["job_uid"] == JOB_UID


def test_the_document_carries_every_axis_the_experiment_matches_on() -> None:
    """Missing any one field means it is matched against nothing and admits anything."""
    identity = build_doc()["expected_identity"]
    for field_name in ("run_id", "account_id", "namespace", "pod_name", "pod_uid",
                       "service_account", "aws_role_arn", "runtime_image", "container",
                       "nonce", "ledger"):
        assert identity.get(field_name), field_name


def test_the_recorded_image_is_digest_pinned() -> None:
    """classify_host refuses a tag, so a document carrying one could never admit."""
    identity = build_doc()["expected_identity"]
    assert "@sha256:" in identity["runtime_image"]
    assert identity["runtime_image_digest"] == DIGEST


def test_an_unresolved_scoped_role_is_refused() -> None:
    """The blast radius of an unsupervised run IS that role.

    Absent credentials are not automatically safe -- the pod may hold an IRSA role --
    so an unresolved role is a refusal rather than an assumption of harmlessness.
    """
    with pytest.raises(ObservationError, match="no scoped AWS role ARN"):
        build_doc(aws_role_arn="")


def test_a_document_without_an_account_is_refused() -> None:
    """The same role name in another account is a different principal."""
    with pytest.raises(ObservationError, match="account and region"):
        build_doc(account_id="")


def test_a_document_without_a_ledger_is_refused() -> None:
    """Root's finding 4: evidence must name WHAT it measured, not merely when."""
    with pytest.raises(ObservationError, match="no ledger path"):
        build_doc(ledger_path="")


def test_the_document_carries_no_credential_material() -> None:
    """It lands in the evidence directory, so every VALUE in it must be safe to keep.

    The identity is expressed as references -- a role ARN, an image digest, a uid --
    never as anything that could authenticate. Asserted on the field names and values
    rather than on the whole blob, so this does not become a test about prose: a
    future comment mentioning the word 'token' is not a leak, whereas a field holding
    one is.
    """
    identity = build_doc()["expected_identity"]
    for key, value in identity.items():
        assert "token" not in key.lower(), key
        assert "password" not in key.lower(), key
        if isinstance(value, str):
            # The two shapes that would actually be credentials if present.
            assert not value.startswith("eyJ"), f"{key} looks like a JWT"
            assert not value.startswith("AKIA"), f"{key} looks like an access key id"
    assert identity["aws_role_arn"].startswith("arn:aws:iam::")
