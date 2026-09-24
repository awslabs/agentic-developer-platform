#!/usr/bin/env python3
"""Bind the protected fixture worker's RUNTIME identity, from the operator's side.

WHY THIS EXISTS AS A SEPARATE STEP
----------------------------------
``render_fixture.render_worker_job`` can assert everything the gateway's verifier
decides from a pod SPEC. It cannot assert the rest, because the rest does not exist
until the pod is scheduled:

  * ``status.phase == "Running"``
  * exactly one ``containerStatus``, named ``agent-worker``
  * that status's RESOLVED ``imageID`` digest is on ``AGENT_WORKER_IMAGE_DIGESTS``
  * non-empty ``status.podIP``
  * no ``deletionTimestamp``

Those are the conditions ``modules/gateway/src/agentauth/workload.py`` evaluates
against the live object, and a template cannot promise any of them. Asserting them
before creation would be a guess dressed as a check, so they are OBSERVED here,
after creation, from the API server's own response.

WHO DOES THE READING, AND WHY IT IS NOT THE WORKER
--------------------------------------------------
The operator. Root verified the protected service account can neither ``get`` nor
``list`` pods, and that boundary is preserved rather than widened: there is no
in-worker ``kubectl`` here and no RBAC grant to make one work. The split is

  * the CONTAINER receives its own ``metadata.uid`` through a downwardAPI volume --
    the one identity a process cannot rewrite for itself, and one of the five
    fieldRefs a downwardAPI volume actually supports (annotations, labels, name,
    namespace, uid; NOT ``spec.serviceAccountName``, which is why an earlier
    revision's dependence on a ``service-account.name`` file could never work);
  * the OPERATOR, which can read the API server, resolves what that uid's pod
    really is -- service account, container, resolved image digest, address -- and
    writes it into the expected-identity document the experiment is measured
    against.

The experiment therefore compares a value it cannot forge (its own projected uid)
against a reference it did not author (this document). Neither half alone is an
identity. That is the whole point: a subject that writes its own reference has
asserted nothing, and a reference with nothing to compare against admits everything.

WHY THE POD IS TIED TO THE JOB'S UID
------------------------------------
A label selector finds pods that CARRY a label. It does not establish that this run
created them -- anything can wear a label. ``ownerReferences`` carries the
server-assigned uid of the controlling Job, so a pod is accepted here only if the
API server itself says it descends from the exact Job object this run created. A
same-named Job from an earlier run has a different uid and is refused.

This is also why the Job is rendered with ``backoffLimit: 0``. A replacement pod is
a different uid; if retries were allowed, this document would describe a pod that no
longer exists while another ran unobserved.

Every function here is pure -- pod JSON in, verdict out -- so the refusals are
tested against real object shapes without a cluster, and without fabricating
credentials on any host.
"""

from __future__ import annotations

import json
import sys
from typing import Any

WORKER_CONTAINER = "agent-worker"


class ObservationError(Exception):
    """The live pod is not the protected worker this run created.

    Raised rather than returned because every caller must stop: an unverified pod
    cannot be recorded as this run's, cannot be cleaned up by uid, and must never
    become the reference an unsupervised experiment measures itself against.
    """


def digest_of(image_ref: str) -> str | None:
    """The ``sha256:`` digest an image reference names, or None if it names none.

    kubelet reports a resolved image with a transport prefix
    (``docker-pullable://repo@sha256:...``) and may report a different registry
    alias for the same content than the operator recorded, so the digest is compared
    and the prefix and repository are treated as presentation. A reference without
    one identifies no particular bytes and yields None, which every caller treats as
    a refusal rather than a match.
    """
    if not isinstance(image_ref, str) or "@sha256:" not in image_ref:
        return None
    tail = image_ref.split("@sha256:", 1)[1].strip()
    # Trailing junk after a digest is not a digest. 64 lowercase hex, exactly.
    candidate = tail.split()[0] if tail.split() else ""
    if len(candidate) != 64 or any(c not in "0123456789abcdef" for c in candidate):
        return None
    return f"sha256:{candidate}"


def _owned_by_job(pod: dict, job_uid: str) -> bool:
    """Does the API SERVER say this pod descends from that exact Job object?

    Checked on uid, not name. A name is reusable -- delete a Job and create another
    with the same name and every label selector still matches, while the uid does
    not. `controller: true` is required so a pod that merely references the Job
    among its owners cannot pass as its child.
    """
    for ref in pod.get("metadata", {}).get("ownerReferences") or []:
        if ref.get("kind") == "Job" and ref.get("uid") == job_uid and ref.get("controller"):
            return True
    return False


def observe_worker_pod(
    pod: dict,
    *,
    run_id: str,
    nonce: str,
    job_uid: str,
    service_account: str,
    approved_digests: list[str],
    container: str = WORKER_CONTAINER,
) -> dict[str, Any]:
    """Confirm this pod is the protected worker, and return what it actually is.

    Ordered so the ownership question is settled before anything else is read: if
    this pod is not this run's, its phase and image are somebody else's business and
    reporting them would be the beginning of acting on them.

    Raises ObservationError on the first condition that fails, naming it and the
    verifier line it corresponds to, because an operator reading the refusal needs
    to know whether to fix the fixture or stop.
    """
    meta = pod.get("metadata") or {}
    status = pod.get("status") or {}
    spec = pod.get("spec") or {}

    pod_name = meta.get("name") or ""
    pod_uid = meta.get("uid") or ""
    namespace = meta.get("namespace") or ""

    if not pod_uid:
        raise ObservationError(
            f"the pod {pod_name or '<unnamed>'} has no metadata.uid. The uid is the only "
            "unforgeable identity Kubernetes assigns, so without it this pod cannot be "
            "recorded as this run's and could not be safely deleted at teardown."
        )
    if not job_uid:
        raise ObservationError(
            "no Job uid was supplied to bind this pod against. A label selector finds pods "
            "that CARRY a label, which anything can do; only the owning Job's server-assigned "
            "uid shows the pod descends from the object this run created."
        )
    if not _owned_by_job(pod, job_uid):
        refs = [f"{r.get('kind')}/{r.get('name')}:{r.get('uid')}"
                for r in (meta.get("ownerReferences") or [])]
        raise ObservationError(
            f"pod {pod_name} (uid {pod_uid}) is not controlled by the Job this run created "
            f"(uid {job_uid}); its owners are {refs or 'none'}. Refusing to bind it: a pod "
            "wearing this run's labels but owned by something else is a different workload, "
            "and recording it would authorise deleting a resource this run did not create."
        )

    labels = meta.get("labels") or {}
    if labels.get("adp.io/w2-fixture") != run_id:
        raise ObservationError(
            f"pod {pod_name} carries adp.io/w2-fixture={labels.get('adp.io/w2-fixture')!r}, "
            f"not this run's {run_id!r}. The isolation NetworkPolicies select on exactly this "
            "label, so a pod without it is not inside the run-scoped policy at all."
        )
    if labels.get("adp.io/w2-nonce") != nonce:
        raise ObservationError(
            f"pod {pod_name} carries adp.io/w2-nonce={labels.get('adp.io/w2-nonce')!r}, not "
            f"this run's nonce. Two runs can share a run id shape; the nonce is what "
            "distinguishes their objects."
        )

    if meta.get("deletionTimestamp"):
        raise ObservationError(
            f"pod {pod_name} is terminating (deletionTimestamp "
            f"{meta['deletionTimestamp']}). workload.py refuses a pod being deleted, and "
            "binding evidence to one would describe a workload that is going away."
        )

    # spec.serviceAccountName: the verifier compares this against the TokenReview
    # username's subject, so a mismatch is a bootstrap refusal.
    observed_sa = spec.get("serviceAccountName") or ""
    if observed_sa != service_account:
        raise ObservationError(
            f"pod {pod_name} runs as service account {observed_sa!r}, not the protected "
            f"{service_account!r}. workload.py compares spec.serviceAccountName against the "
            "reviewed token's subject; these must be the same account or the worker cannot "
            "authenticate."
        )

    phase = status.get("phase")
    if phase != "Running":
        raise ObservationError(
            f"pod {pod_name} is in phase {phase!r}, not 'Running'. workload.py requires a "
            "Running pod, so a Pending or Failed worker cannot hold protected authority. "
            "This is observed rather than assumed because a template cannot promise it."
        )

    pod_ip = (status.get("podIP") or "").strip()
    if not pod_ip:
        raise ObservationError(
            f"pod {pod_name} has no status.podIP yet. The control listener binds to POD_IP "
            "explicitly (never 0.0.0.0), and workload.py requires a non-empty address, so "
            "there is nothing for the gateway to reach and nothing to measure."
        )

    # Exactly one containerStatus under the expected name -- the status-side twin of
    # the spec-side check render_fixture already made.
    statuses = [cs for cs in (status.get("containerStatuses") or [])
                if cs.get("name") == container]
    if len(statuses) != 1:
        names = [cs.get("name") for cs in (status.get("containerStatuses") or [])]
        raise ObservationError(
            f"pod {pod_name} has {len(statuses)} container statuses named {container!r} "
            f"(all containers: {names}). workload.py requires exactly one, in status as well "
            "as in spec, so it can attribute the running image to a single container."
        )
    container_status = statuses[0]

    # The RESOLVED digest: what is actually running, not what was requested. A tag in
    # the spec can have moved since the pod was admitted; imageID cannot.
    image_id = container_status.get("imageID") or ""
    resolved = digest_of(image_id)
    if resolved is None:
        raise ObservationError(
            f"pod {pod_name}'s container status reports imageID {image_id!r}, which names no "
            "sha256 digest. Without a resolved digest there is no way to say which bytes are "
            "running, and workload.py has nothing to compare against its approved list."
        )
    approved = [d.strip() for d in approved_digests if d and d.strip()]
    if not approved:
        raise ObservationError(
            "the approved worker image digest list is empty, so every digest would fail the "
            "comparison. An empty allowlist admits nothing; it must not be read as admitting "
            "anything. Widening AGENT_WORKER_IMAGE_DIGESTS is Terraform-owned "
            "(agent_authority_worker_image_digests) and is root's change."
        )
    if resolved not in approved:
        raise ObservationError(
            f"pod {pod_name} is running digest {resolved}, which is NOT on the gateway's "
            f"approved list ({len(approved)} entries). The worker would be refused at "
            "bootstrap. Pinned to some digest is not pinned to an ALLOWED digest."
        )

    # The selected container must actually be RUNNING. Root's executed regression
    # (5808699406): a pod with `status.phase: Running` whose agent-worker container
    # state was `{terminated: {exitCode: 1}}` and `ready: false` was still returned as
    # an accepted observation, with `started_at: None` quietly standing in for the
    # missing `running` block.
    #
    # Pod phase is not the container's state. `phase: Running` means "at least one
    # container is running", so it coexists with a worker that crashed while a sidecar
    # lives on -- and it is the WORKER the gateway admits. workload.py:151 refuses on
    # exactly this axis (`"running" not in containers[0].get("state", {})`), so
    # accepting a terminated or waiting container here would claim an admission
    # condition that the real verifier rejects. The observation would assert the pod is
    # admissible while the gateway refuses it.
    #
    # Neither `imageID` nor `phase` may be used to infer running: an imageID is
    # populated once the image is pulled, which happens before start and survives exit.
    state = container_status.get("state")
    if not isinstance(state, dict) or "running" not in state:
        reported = sorted(state) if isinstance(state, dict) else state
        raise ObservationError(
            f"pod {pod_name}'s container {container!r} is not running: status.state reports "
            f"{reported!r}. workload.py requires 'running' in the selected container's state "
            f"(line 151), and status.phase == {phase!r} does not establish it -- a phase of "
            "Running only means SOME container runs, which is satisfied while the worker "
            "itself is waiting or has terminated. There is no admissible worker to measure."
        )

    ready = bool(container_status.get("ready"))
    started_at = (state.get("running") or {}).get("startedAt")

    return {
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "namespace": namespace,
        "service_account": observed_sa,
        "container": container,
        "pod_ip": pod_ip,
        "phase": phase,
        "container_ready": ready,
        "started_at": started_at,
        # Both kept: the digest is the identity, the full reference is the evidence
        # an operator can look up in ECR.
        "runtime_image": image_id,
        "runtime_image_digest": resolved,
        "job_uid": job_uid,
        "run_id": run_id,
        "nonce": nonce,
        "approved_digest_count": len(approved),
        "verifier_conditions_observed": [
            "metadata.deletionTimestamp is absent",
            f"spec.serviceAccountName == {service_account}",
            "status.phase == Running",
            "status.podIP is non-empty",
            f"exactly one containerStatus named {container}",
            f"containerStatus {container} state is running",
            "resolved status.containerStatuses[].imageID digest is on the approved list",
        ],
        "_ownership": (
            f"bound to Job uid {job_uid} via metadata.ownerReferences (controller: true), "
            "not by label match: a label is wearable, a server-assigned uid is not"
        ),
    }


def expected_identity_document(
    observation: dict[str, Any],
    *,
    account_id: str,
    region: str,
    aws_role_arn: str,
    ledger_path: str,
    control_endpoint: str,
) -> dict[str, Any]:
    """The reference an unsupervised experiment is measured AGAINST.

    Written by the operator, OUTSIDE the pod, from what the API server reported.
    The experiment cannot produce this for itself and is not permitted to: every
    field here is one the process could otherwise assert about itself, which is
    exactly the defect root reproduced -- "two env vars are not identity".

    ``aws_role_arn`` must be the SCOPED fixture role, read from the service
    account's own ``eks.amazonaws.com/role-arn`` annotation rather than named by
    hand, so the document describes the role the pod will really assume.

    ``pod_uid`` is included because a pod NAME is not sufficient: a Job can create a
    same-named pod after a deletion, and the experiment proves which pod it is by
    reading its own projected uid. Matching on name alone would accept the
    replacement.
    """
    if not account_id or not region:
        raise ObservationError(
            "the expected-identity document needs the account and region it was provisioned "
            "in: the same role name in another account is a different principal."
        )
    if not aws_role_arn:
        raise ObservationError(
            "no scoped AWS role ARN was resolved for the worker's service account. An "
            "unsupervised run's blast radius is exactly what that role can do, so an "
            "unresolved role is a refusal rather than an assumption that it is harmless. "
            "It is read from the service account's eks.amazonaws.com/role-arn annotation."
        )
    if not ledger_path:
        raise ObservationError(
            "no ledger path was supplied. The document must point at the ledger that "
            "recorded this run's objects, so the evidence names WHAT was measured and not "
            "merely when."
        )
    return {
        "_what_this_is": (
            "The authoritative description of the one pod this run's experiment may run as. "
            "Produced by the operator from the API server's response, outside the pod. A "
            "subject that authors its own reference has asserted nothing."
        ),
        "_how_the_pod_proves_it_is_this_pod": (
            "It reads its own metadata.uid from the downwardAPI volume mounted at "
            "W2_POD_IDENTITY_DIR and compares it to pod_uid below. The protected service "
            "account cannot get or list pods, so it never reads this from the API server."
        ),
        "expected_identity": {
            "run_id": observation["run_id"],
            "nonce": observation["nonce"],
            "account_id": account_id,
            "region": region,
            "namespace": observation["namespace"],
            "pod_name": observation["pod_name"],
            "pod_uid": observation["pod_uid"],
            "container": observation["container"],
            "service_account": observation["service_account"],
            "aws_role_arn": aws_role_arn,
            "runtime_image": observation["runtime_image"],
            "runtime_image_digest": observation["runtime_image_digest"],
            "job_uid": observation["job_uid"],
            "control_endpoint": control_endpoint,
            "ledger": ledger_path,
        },
        "observation": observation,
    }


# ---------------------------------------------------------------------------
# CLI -- the seam the shell launcher calls
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-json", required=True,
                        help="path to `kubectl get pod <name> -o json` output")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--job-uid", required=True,
                        help="server-assigned uid of the Job this run created")
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--approved-digests", required=True,
                        help="comma-separated AGENT_WORKER_IMAGE_DIGESTS")
    parser.add_argument("--container", default=WORKER_CONTAINER)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--aws-role-arn", required=True,
                        help="the worker service account's eks.amazonaws.com/role-arn")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--control-endpoint", required=True)
    parser.add_argument("--out", required=True,
                        help="where to write the expected-identity document")
    args = parser.parse_args(argv)

    pod = json.loads(Path(args.pod_json).read_text(encoding="utf-8"))
    try:
        observation = observe_worker_pod(
            pod,
            run_id=args.run_id, nonce=args.nonce, job_uid=args.job_uid,
            service_account=args.service_account,
            approved_digests=args.approved_digests.split(","),
            container=args.container,
        )
        document = expected_identity_document(
            observation,
            account_id=args.account_id, region=args.region,
            aws_role_arn=args.aws_role_arn, ledger_path=args.ledger,
            control_endpoint=args.control_endpoint,
        )
    except ObservationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")

    print(f"ok   bound pod {observation['pod_name']} uid={observation['pod_uid']}")
    print(f"     container {observation['container']} running "
          f"{observation['runtime_image_digest']}")
    print(f"     on the approved list ({observation['approved_digest_count']} entries)")
    for condition in observation["verifier_conditions_observed"]:
        print(f"     verifier condition observed: {condition}")
    print(f"     expected-identity document: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
