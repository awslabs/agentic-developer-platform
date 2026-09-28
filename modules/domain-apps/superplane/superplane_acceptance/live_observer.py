"""The reviewed read-only observer for U12's R17 baseline capture.

Issue #5289. This is the client code :mod:`superplane_acceptance.live_baseline`
observes through, kept in its own module because it is the part that touches a
real environment and therefore the part worth reviewing on its own.

## Read-only by construction, not by intention

Every outward call goes through one of two narrow transports, and each accepts
only an allow-listed read:

* :class:`SkyPilotReads` permits exactly ``GET /api/health``,
  ``POST /status`` and ``GET /enabled_clouds`` -- the read subset of the
  maintained client in ``src/superplane-controller/skypilot/client.go``.
  ``/launch`` and ``/down`` are not reachable from here: the allow-list is
  consulted before the URL is built, so there is no argument that makes this
  transport launch or tear down anything.
* :class:`KubectlReads` permits a fixed tuple of ``kubectl get`` invocations.
  The only interpolated value is a namespace, validated as a DNS label, so no
  caller-supplied string can become a verb or a flag.

``POST /status`` is the one read that uses POST. It is a read: the maintained
client sends **no body at all** when no cluster filter is given
(``client.go:88-91``, pinned by its ``TestStatus_NoFilter``), and the server
answers with the cluster list. Matching that exactly matters -- sending
``{"cluster_names": []}`` instead is a different request.

## What this observer can and cannot establish

It reports what it actually saw, and says so when it saw nothing. Three
categories:

* **Observable now.** Node readiness and GPU allocatable, the SuperplaneNode
  record's link to its Kubernetes node, enabled-cloud restriction, the effective
  autostop on the running cluster, cluster-status mapping, single ownership per
  resource, absence of activation material, reported hourly cost. These produce
  ``SATISFIED`` or ``REFUTED`` from a live read.
* **Established from the authorized operation's retained records.** Provider
  ordering at launch time, launch-failure fallback, streamed progress and its
  terminal event, cancellation behavior, controller restart, state-store survival,
  spend against its ceiling, and teardown. None can be read from steady state
  afterwards and this module will not operate to create them -- but the operator
  *retains* the records, and :mod:`superplane_acceptance.operation_receipts` reads
  them under hash, window, target, resource and authority validation. With no
  records supplied these stay ``INDETERMINATE`` naming the exact missing record,
  which fails their check and blocks the run with a precise list.
* **Confirmed against the provider itself.** Provider-side absence is read live
  from a registered read-only client in :data:`PROVIDER_READERS`, never from a
  retained file and never from the tool that performed the teardown. A provider
  with no reviewed client cannot confirm an absence, and the check names that gap
  instead of passing.

## Being registered is not enough to publish

:func:`live_baseline.register_live_observer` marks this class publishable, and
:meth:`LiveBaselineObserver.transport_is_live` additionally requires both
transports to be the real ones by exact type. Driven by the offline transports
the tests use, this same class reports fixture evidence -- which is how the
transport behavior is tested without an environment.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from spike.harness import DEFAULT_IDLE_MINUTES_TO_AUTOSTOP, map_cluster_status
from spike.parity_matrix import Dimension, dimension_by_name

from .cli_delivery import EvidenceError, require
from .live_baseline import (
    MAX_IDENTIFIER,
    ExistingStateRecord,
    ObservedEnvironmentIdentity,
    ObservedFact,
    Outcome,
    ResourceIdentity,
    ServingInventory,
    register_live_observer,
)
from .operation_receipts import (
    MISSING_RECORD,
    OperationLedger,
    ReceiptEvidence,
    _BODY_NAME,
    _manifest_digest,
    load_ledger,
)

# The read subset of the maintained SkyPilot client. Paths are matched exactly
# and the method is fixed per path, so no caller can reach /launch or /down.
SKYPILOT_READS: dict[str, str] = {
    "/api/health": "GET",
    "/status": "POST",
    "/enabled_clouds": "GET",
}
SKYPILOT_TOKEN_VARIABLE = "SUPERPLANE_LIVE_SKYPILOT_TOKEN"
# Credential for reading an execution lane's own record of what it published.
# Read-only scope; the capture never dispatches or re-runs anything.
PRODUCER_TOKEN_VARIABLE = "SUPERPLANE_LIVE_PRODUCER_TOKEN"
# The artifact name a reviewed lane publishes its retained evidence under. Fixed
# rather than caller-supplied: letting a submission choose which artifact attests
# it would hand back the freedom the attestation exists to remove.
EVIDENCE_ARTIFACT_NAME = "superplane-baseline-evidence"

# Where the operator places the downloaded evidence artifact archive. The archive
# is the producer's own copy of the evidence set; it is read from the caller's
# filesystem, which is safe only because its bytes must hash to the digest GitHub
# independently recorded for that artifact. A substituted or edited archive fails
# that comparison, so the caller's control over the file is control over nothing.
ARCHIVE_DIR_VARIABLE = "SUPERPLANE_LIVE_BASELINE_EVIDENCE_ARCHIVE_DIR"

# Bounds on the archive, because it is operator-supplied and zip decompression is
# attacker-controllable. Refused rather than truncated: a capture must fail on an
# archive it cannot fully read instead of authenticating part of one.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024

# The producer contract, stated so a lane can implement it. The reviewed lane
# uploads ONE artifact named EVIDENCE_ARTIFACT_NAME whose members are EXACTLY the
# evidence body files the retained submissions point at, by their `body` names,
# flat with no directories -- byte-for-byte the bodies, not the submission
# pointers and nothing else. The set digest is derived from those members' names
# and bytes, so omitting a record that would have refuted a check, or adding a
# forged one, changes the value GitHub's digest authenticates.
#
# Member names are screened with `operation_receipts._BODY_NAME`, the same rule
# that validates a submission's `body`, deliberately rather than a second pattern
# of this module's own: the two sets have to be comparable by name, so a member
# this accepted but ingestion rejected -- or the reverse -- would be a contract
# the lane cannot satisfy. That is the U12-203 failure mode repeating, and an
# earlier draft of this repair reproduced it by requiring a `.json` suffix the
# published bodies do not carry. One authority, cited once.
ARCHIVE_MEMBER_PATTERN = _BODY_NAME
KUBECONFIG_VARIABLE = "SUPERPLANE_LIVE_KUBECONFIG"
NAMESPACE_VARIABLE = "SUPERPLANE_LIVE_SUPERPLANE_NAMESPACE"
SKYPILOT_NAMESPACE_VARIABLE = "SUPERPLANE_LIVE_SKYPILOT_NAMESPACE"

# Matches the maintained client's own ceiling (client.go:18).
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
READ_TIMEOUT_SECONDS = 30

# Reviewed read-only provider clients able to confirm an instance is gone
# provider-side. One entry per provider whose instance-describe contract and
# credential boundary have been reviewed; a provider absent from this map cannot
# confirm an absence, and the affected checks say so by name rather than falling
# back to the tool that performed the teardown. Populated at the bottom of this
# module, once the reader classes exist.
PROVIDER_READERS: dict[str, type] = {}


def register_provider_reader(provider: str, reader: type) -> None:
    """Record a reviewed read-only provider client for one provider."""
    __tracebackhide__ = True
    require(
        provider == provider.lower().strip() and bool(provider),
        "a provider reader is registered under the provider's lowercase name",
    )
    PROVIDER_READERS[provider] = reader


_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

# How the SkyPilot API server's durable state is declared, used to tell an external
# database from node-bound storage from scratch. Both come from the maintained
# manifest k8s/40-skypilot-api.yaml, which sets the URI through a secretKeyRef and
# mounts ~/.sky from an emptyDir it explicitly documents as "NOT a
# PersistentVolumeClaim".
DB_URI_VARIABLE = "SKYPILOT_DB_CONNECTION_URI"
SKY_STATE_PATH = "/home/sky/.sky"

# Env keys that must never appear in captured output, reused from the migration
# adapter's list so the two agree on what counts as activation material.
ACTIVATION_MARKERS = (
    "activationCode",
    "activationId",
    "activation_code",
    "activation_id",
    "SSM_ACTIVATION",
)


class ReadTransport(Protocol):
    """A single allow-listed read returning raw bytes."""

    def __call__(self, selector: str) -> bytes: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        __tracebackhide__ = True
        # Never follow or echo a Location: it can carry the token forward or
        # contain sensitive data. The maintained Go client refuses redirects for
        # the same reason (client.go:56, :297).
        raise EvidenceError("SkyPilot read redirected; credential forwarding refused")


class SkyPilotReads:
    """Authenticated read-only access to the baseline's SkyPilot API server.

    Constructed with the selected target's API base URL, which is re-checked here
    at the credential boundary even if a caller bypassed
    :func:`live_baseline.settings`.
    """

    def __init__(self, base_url: str) -> None:
        __tracebackhide__ = True
        require(
            base_url.startswith(("http://", "https://")) and "@" not in base_url,
            "BLOCKED: the SkyPilot API base URL must be a plain http(s) origin "
            "without embedded credentials",
        )
        self._base = base_url.rstrip("/")

    def __call__(self, selector: str) -> bytes:
        __tracebackhide__ = True
        method = SKYPILOT_READS.get(selector)
        require(
            method is not None,
            f"{selector!r} is not a permitted read; this transport exposes only "
            + ", ".join(sorted(SKYPILOT_READS)),
        )
        token = os.environ.get(SKYPILOT_TOKEN_VARIABLE, "")
        require(
            1 <= len(token) <= 16_384 and all(32 < ord(c) < 127 for c in token),
            f"BLOCKED: an existing valid {SKYPILOT_TOKEN_VARIABLE} is required to "
            "read the baseline's SkyPilot API",
        )
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store",
        }
        url = self._base + selector
        # POST /status with no cluster filter sends no body and no Content-Type,
        # matching the maintained client exactly (client.go:88-91).
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
            with opener.open(request, timeout=READ_TIMEOUT_SECONDS) as response:
                require(response.status == 200, f"SkyPilot {selector} returned non-200")
                require(
                    response.geturl() == url,
                    f"SkyPilot {selector} endpoint changed during the request",
                )
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except EvidenceError as exc:
            # Detach urllib frames, which can retain the request headers.
            raise EvidenceError(str(exc)) from None
        except Exception:  # noqa: BLE001 - narrowing this would leak the request
            # Deliberately blind, for the same reason as contracts' redaction path:
            # urllib and the TLS stack below it raise a wide, version-dependent set
            # of types whose payloads embed the request -- including its auth header
            # -- and the whole point here is that none of that reaches a caller.
            # A narrower except would let an unanticipated type propagate with the
            # chain attached.
            raise EvidenceError(f"BLOCKED: SkyPilot read {selector} failed") from None
        require(
            len(content) <= MAX_RESPONSE_BYTES,
            f"SkyPilot {selector} response exceeds the read limit",
        )
        return content


class KubectlReads:
    """Read-only Kubernetes access to the selected workspace cluster.

    Only the invocations in :meth:`commands` are reachable, each a ``get`` or a
    ``config view``. The kubeconfig comes from the environment and is never
    copied into config or evidence, and neither stdout nor stderr of a failed
    call is echoed -- kubectl's diagnostics can quote tokens from a kubeconfig.
    """

    def __init__(self, namespace: str, skypilot_namespace: str) -> None:
        __tracebackhide__ = True
        for label in (namespace, skypilot_namespace):
            require(
                _DNS_LABEL.fullmatch(label) is not None,
                "BLOCKED: namespaces must be DNS labels",
            )
        self._namespace = namespace
        self._skypilot_namespace = skypilot_namespace

    def commands(self) -> dict[str, tuple[str, ...]]:
        return {
            "context": ("config", "view", "--minify", "-o", "json"),
            "nodes": ("get", "nodes", "-o", "json"),
            "superplane-nodes": (
                "get",
                "superplanenodes.superplane.ai",
                "--all-namespaces",
                "-o",
                "json",
            ),
            "nodepools": ("get", "nodepools.superplane.ai", "-o", "json"),
            "pods": ("get", "pods", "--all-namespaces", "-o", "json"),
            "api-state": (
                "get",
                "deployments,statefulsets",
                "-n",
                self._skypilot_namespace,
                "-o",
                "json",
            ),
            "controllers": (
                "get",
                "deployments",
                "-n",
                self._namespace,
                "-o",
                "json",
            ),
        }

    def __call__(self, selector: str) -> bytes:
        __tracebackhide__ = True
        arguments = self.commands().get(selector)
        require(
            arguments is not None,
            f"{selector!r} is not a permitted read; this transport exposes only "
            + ", ".join(sorted(self.commands())),
        )
        assert arguments is not None  # narrowed by the require above
        kubeconfig = os.environ.get(KUBECONFIG_VARIABLE, "")
        require(
            bool(kubeconfig) and Path(kubeconfig).is_file(),
            f"BLOCKED: {KUBECONFIG_VARIABLE} must point at a readable kubeconfig "
            "for the selected workspace cluster",
        )
        try:
            result = subprocess.run(  # fixed argv, no shell
                [
                    "kubectl",
                    "--kubeconfig",
                    kubeconfig,
                    "--request-timeout=30s",
                    *arguments,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise EvidenceError(
                f"BLOCKED: kubectl read {selector} could not be executed"
            ) from None
        require(
            result.returncode == 0,
            f"BLOCKED: kubectl read {selector} failed",
        )
        require(
            len(result.stdout) <= MAX_RESPONSE_BYTES,
            f"kubectl read {selector} exceeds the read limit",
        )
        return result.stdout


class Ec2InstanceReads:
    """Read-only AWS access, to confirm a rented EC2 machine no longer exists.

    This is the independent confirmation the cleanup criterion needs. A SkyPilot
    ``down`` or ``purge`` succeeding only proves SkyPilot dropped its local handle;
    the machine may still exist and still bill.

    **The scope is deliberately narrow, and the narrowness is the point.** It
    answers only for EC2 instance ids (``i-...``). Superplane's node records carry
    an SSM *managed instance* id (``SuperplaneNode.status.ssmInstanceId``,
    ``api/v1/superplanenode_types.go:116``), and for a hybrid node rented from
    another cloud an ``mi-...`` activation being deregistered says the node left
    the cluster's control plane -- **not** that the rented machine stopped
    billing. Treating the two as equivalent would repeat exactly the error this
    check exists to catch, so an ``mi-`` handle is refused and the criterion stays
    unconfirmed until that provider's own instance-describe contract is reviewed
    and registered.

    One allow-listed read, a ``describe``, bounded, with no terminate or modify
    path. The AWS CLI is invoked with a fixed argv and no shell, and credentials
    come from the ambient AWS environment -- so this client neither holds nor
    logs one.
    """

    def __init__(self, region: str) -> None:
        __tracebackhide__ = True
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", region) is not None,
            "BLOCKED: the provider reader needs the selected target's region",
        )
        self._region = region

    def describe(self, instance: str) -> bytes:
        """Raw ``ec2 describe-instances`` output for exactly one instance id.

        The id is validated against the EC2 shape before anything is sent, so a
        handle from another provider's namespace cannot be silently asked about
        here, and it is passed as its own argv element so it cannot become a flag.
        """
        __tracebackhide__ = True
        require(
            re.fullmatch(r"i-[0-9a-f]{8,17}", instance) is not None,
            "BLOCKED: this provider reader answers only for EC2 instance ids; an "
            "SSM managed-instance handle does not establish that a rented machine "
            "stopped billing",
        )
        try:
            result = subprocess.run(  # fixed argv, no shell
                [
                    "aws",
                    "ec2",
                    "describe-instances",
                    "--region",
                    self._region,
                    "--instance-ids",
                    instance,
                    "--output",
                    "json",
                ],
                stdout=subprocess.PIPE,
                # AWS CLI errors can echo the caller identity and request
                # parameters; nothing from stderr reaches the record.
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise EvidenceError(
                "BLOCKED: the provider instance read could not be executed"
            ) from None
        # A non-zero exit is NOT read as absence. `InvalidInstanceID.NotFound` and
        # "your credentials cannot see this account" are indistinguishable here
        # once stderr is suppressed, and guessing between them is how a capture
        # would report a still-billing machine as cleaned up. Absence must come
        # from a successful describe returning no reservation.
        require(
            result.returncode == 0,
            "BLOCKED: the provider instance read failed; absence is unconfirmed, "
            "and a failed read is not evidence the machine is gone",
        )
        require(
            len(result.stdout) <= MAX_RESPONSE_BYTES,
            "the provider instance read exceeds the read limit",
        )
        return result.stdout

    @staticmethod
    def instance_present(payload: object) -> bool:
        """Whether the provider still reports this instance as existing.

        ``shutting-down`` and ``stopping`` still exist, so they are present: a
        machine mid-teardown has not finished being released, and reporting it
        absent would publish a cleanup pass slightly too early. Only
        ``terminated`` -- or no reservation at all -- is absence.
        """
        __tracebackhide__ = True
        require(
            isinstance(payload, dict), "the provider instance read was not an object"
        )
        assert isinstance(payload, dict)
        reservations = payload.get("Reservations")
        require(
            isinstance(reservations, list),
            "the provider instance read carried no Reservations array",
        )
        assert isinstance(reservations, list)
        states = []
        for reservation in reservations:
            if not isinstance(reservation, dict):
                continue
            for instance in reservation.get("Instances") or []:
                if not isinstance(instance, dict):
                    continue
                state = instance.get("State") or {}
                states.append(
                    _text(state.get("name", ""), MAX_IDENTIFIER).lower()
                    if isinstance(state, dict)
                    else ""
                )
        return any(state != "terminated" for state in states)


class WorkflowRunReads:
    """Read-only access to an execution lane's own record of what it published.

    This is the independent producer the evidence ingestion authenticates against.
    A locally-recomputed digest establishes only that a file agrees with itself; it
    says nothing about who produced it. An offline reproduction used exactly that
    gap -- an arbitrary body, a self-declared authority label, and a hand-written
    field driving the verdict past a byte-identical digest.

    So the question "is this really what the operation produced?" is put to the
    lane that produced it, through its own API: for a given run and attempt, what
    digest did you record? The answer comes from a party with no stake in the
    check's outcome, which is the same principle as asking the provider -- rather
    than the tool that deleted it -- whether a machine is gone.

    **Read-only by construction.** One allow-listed API path, ``GET`` only,
    bounded, with no dispatch, re-run, cancel or delete path. The token comes from
    the ambient environment and is never copied into config or evidence.

    The expected digest is read from the lane's recorded artifact metadata rather
    than from anything the submission supplies, so a caller cannot choose the
    value they will be compared against.

    HOW THE ANSWER IS ESTABLISHED (PR #5543 foreground review, U12-202/U12-203)

    Two mechanical errors previously made this path unusable for honest evidence,
    both confirmed by read-only probe against this repository:

    *   The artifacts were requested at
        ``/actions/runs/{run}/attempts/{attempt}/artifacts``, which GitHub does not
        implement -- it returns 404. Artifacts are listed per *run*; the attempt is
        established separately, from the per-attempt record that does exist. So a
        genuine lane could never be reached at all, and every read reported "no
        producer record", which the caller treats as unauthenticated rather than as
        this client's own bug.
    *   The digest GitHub records authenticates the uploaded **ZIP archive**, while
        the caller compares against a digest over the evidence set's
        ``name:digest`` lines. Those are different byte sequences, so even a
        genuine matching artifact was refused. An authentication path that cannot
        succeed for honest evidence is indistinguishable from not having one.

    The repair chains the two authorities rather than conflating them.
    :meth:`published_digest` authenticates the archive's bytes against GitHub's
    recorded digest, and only then reads the archive's members to derive the set
    digest the retained evidence must hash to. GitHub vouches for the archive; the
    archive yields the expected value.

    The attempt binding comes from time, following the pattern PR #5544 arrived at
    for this same class of bug (``teardown.attempt_evidence``): the per-attempt
    record supplies the commit that attempt executed and the interval it ran in,
    and an artifact is credited to the attempt only if GitHub's own upload stamp
    falls inside that interval. Since attempts of a run are consecutive, an
    artifact from an earlier failed attempt precedes the later attempt's start and
    is refused. Deliberately NOT from an artifact ``run_attempt`` field: the
    artifacts API does not return one, so requiring it would admit only fabricated
    records -- which is exactly the trap #5544 documents.
    """

    def __init__(
        self,
        base_url: str,
        repository: str,
        *,
        workflow: str,
        job: str,
        step: str,
        archive_dir: str = "",
        transport=None,
    ) -> None:
        __tracebackhide__ = True
        require(
            base_url.startswith("https://") and "@" not in base_url,
            "BLOCKED: the evidence producer API must be an https origin without "
            "embedded credentials",
        )
        require(
            re.fullmatch(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}", repository)
            is not None,
            "BLOCKED: the evidence producer needs the lane's owner/repository",
        )
        # The reviewed producer workflow, publishing job and upload step. Required
        # rather than inferred: an artifact of the right name published by some
        # other workflow in the same repository is not the reviewed lane's output,
        # and a run that never reached its upload step has not "published nothing"
        # -- it has failed to publish, which is a different fact.
        for label, value in (("workflow", workflow), ("job", job), ("step", step)):
            require(
                isinstance(value, str) and 0 < len(value.strip()) <= MAX_IDENTIFIER,
                f"BLOCKED: the evidence producer needs the reviewed {label} name",
            )
        self._base = base_url.rstrip("/")
        self._repository = repository
        self._workflow = workflow.strip()
        self._job = job.strip()
        self._step = step.strip()
        self._archive_dir = archive_dir or os.environ.get(ARCHIVE_DIR_VARIABLE, "")
        # Injected only so the request-building and parsing are regressable
        # offline against documented response shapes. Default is the real
        # read-only HTTPS transport.
        self._transport = transport or self._read

    # -- transport -------------------------------------------------------

    def _read(self, path: str) -> bytes:
        """One allow-listed read-only GET against the lane's API."""
        __tracebackhide__ = True
        token = os.environ.get(PRODUCER_TOKEN_VARIABLE, "")
        require(
            1 <= len(token) <= 16_384 and all(32 < ord(c) < 127 for c in token),
            f"BLOCKED: an existing valid {PRODUCER_TOKEN_VARIABLE} is required to "
            "read the execution lane's own record of what it published",
        )
        url = f"{self._base}/repos/{self._repository}/{path}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "Cache-Control": "no-cache, no-store",
            },
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
            with opener.open(request, timeout=READ_TIMEOUT_SECONDS) as response:
                require(
                    response.status == 200,
                    "the evidence producer read returned non-200",
                )
                require(
                    response.geturl() == url,
                    "the evidence producer endpoint changed during the request",
                )
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except EvidenceError as exc:
            raise EvidenceError(str(exc)) from None
        except Exception:  # noqa: BLE001 - narrowing this would leak the request
            # Same reasoning as SkyPilotReads: urllib and the TLS stack raise a
            # wide, version-dependent set of types whose payloads embed the
            # request including its auth header.
            raise EvidenceError(
                "BLOCKED: the evidence producer read failed; evidence is "
                "unauthenticated and a failed read is not an attestation"
            ) from None
        require(
            len(content) <= MAX_RESPONSE_BYTES,
            "the evidence producer response exceeds the read limit",
        )
        return content

    def _get(self, path: str, label: str):
        __tracebackhide__ = True
        return _parse(self._transport(path), label)

    # -- the producer's answer -------------------------------------------

    def published_digest(self, *, run: str, attempt: int, revision: str) -> str:
        """The evidence-set digest this lane's published archive really contains.

        Returns an empty string when the lane has no retrievable record for that
        attempt -- which leaves the evidence unauthenticated rather than raising,
        because "no producer record" is a gap the caller reports as diagnostics,
        not a contradiction. A malformed or unreachable producer *is* raised:
        silently treating a failed read as "no record" would turn an outage into a
        permanently unauthenticatable capture without saying so.
        """
        __tracebackhide__ = True
        require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", run) is not None,
            "BLOCKED: the producer is asked about a single readable run id",
        )
        require(
            isinstance(attempt, int) and 1 <= attempt <= 1000,
            "BLOCKED: the producer is asked about a single positive attempt number",
        )
        require(
            isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision),
            "BLOCKED: the producer is asked about one exact 40-hex deployed revision",
        )
        window = self.attempt_evidence(
            self._get(
                f"actions/runs/{run}/attempts/{attempt}",
                "evidence producer attempt record",
            ),
            run=run,
            attempt=attempt,
            revision=revision,
        )
        # The publishing step must have actually run and succeeded on THIS attempt.
        # Otherwise a run that failed before uploading would read as "this lane
        # published nothing", and an absent record is treated as an honest gap.
        self.require_published(
            self._get(
                f"actions/runs/{run}/attempts/{attempt}/jobs",
                "evidence producer job record",
            ),
            run=run,
            attempt=attempt,
        )
        recorded = self.recorded_artifact(
            self._get(
                f"actions/runs/{run}/artifacts",
                "evidence producer artifact listing",
            ),
            window=window,
        )
        if not recorded:
            return ""
        return self.manifest_of_archive(self._archive(recorded), recorded)

    # -- parsing, separated from transport so it is regressable offline ---

    def attempt_evidence(
        self, record: object, *, run: str, attempt: int, revision: str
    ) -> dict:
        """GitHub's own record of ONE attempt: the commit it ran, and when.

        This is the endpoint that exists and does report ``run_attempt``. It gives
        two facts the artifact listing cannot: the commit this attempt executed --
        which must be the revision under verification, since a re-run may be
        dispatched against another head -- and the interval it ran in, which bounds
        when it could have uploaded anything.
        """
        __tracebackhide__ = True
        require(
            isinstance(record, dict),
            "the evidence producer attempt record was not an object",
        )
        assert isinstance(record, dict)
        require(
            record.get("run_attempt") == attempt,
            f"the lane's attempt record is not attempt {attempt}, so the evidence "
            "cannot be bound to the attempt it claims",
        )
        require(
            str(record.get("id", "")) == run,
            "the lane's attempt record belongs to a different run than the one "
            "the evidence names",
        )
        require(
            record.get("head_sha") == revision,
            "the lane's attempt executed a different revision than the deployed one "
            "under verification; a re-run against another head is a different "
            "execution and its evidence is not evidence for this one",
        )
        # The reviewed producer workflow, and the intended repository. An artifact
        # of the right name from another workflow is not this lane's output.
        require(
            _text(record.get("path", ""), MAX_IDENTIFIER) == self._workflow
            or _text(record.get("name", ""), MAX_IDENTIFIER) == self._workflow,
            "the lane's attempt was not produced by the reviewed evidence producer "
            "workflow",
        )
        repository = record.get("repository")
        if isinstance(repository, dict):
            require(
                _text(repository.get("full_name", ""), MAX_IDENTIFIER)
                == self._repository,
                "the lane's attempt belongs to another repository than the "
                "reviewed producer's",
            )
        require(
            record.get("status") == "completed"
            and record.get("conclusion") == "success",
            "the lane's attempt did not itself complete successfully; a later "
            "attempt's success cannot be credited to it",
        )
        started = _moment(record.get("run_started_at"), "the attempt start time")
        finished = _moment(record.get("updated_at"), "the attempt completion time")
        require(
            started <= finished,
            "the lane's attempt start and completion times are inconsistent",
        )
        return {"attempt": attempt, "started_at": started, "completed_at": finished}

    def require_published(self, payload: object, *, run: str, attempt: int) -> None:
        """Require the reviewed publishing step to have really run and succeeded.

        A run that failed before its upload step has not published nothing; it has
        failed to publish. Left unchecked, the two are indistinguishable, and the
        caller would read the second as an honest absence of evidence.
        """
        __tracebackhide__ = True
        require(
            isinstance(payload, dict),
            "the evidence producer job record was not an object",
        )
        assert isinstance(payload, dict)
        jobs = payload.get("jobs")
        require(
            isinstance(jobs, list),
            "the evidence producer job record carried no jobs array",
        )
        assert isinstance(jobs, list)
        matching = [
            job
            for job in jobs
            if isinstance(job, dict)
            and _text(job.get("name", ""), MAX_IDENTIFIER) == self._job
        ]
        require(
            len(matching) == 1,
            f"the lane's attempt {attempt} does not report exactly one "
            f"{self._job!r} job, so whether it published evidence is not "
            "established",
        )
        job = matching[0]
        require(
            job.get("conclusion") == "success",
            f"the lane's {self._job!r} job did not succeed on attempt {attempt}, so "
            "an absent artifact is a failure to publish rather than an absence of "
            "evidence",
        )
        steps = job.get("steps")
        require(
            isinstance(steps, list),
            "the lane's publishing job reported no steps, so the upload cannot be "
            "shown to have run",
        )
        assert isinstance(steps, list)
        uploads = [
            step
            for step in steps
            if isinstance(step, dict)
            and _text(step.get("name", ""), MAX_IDENTIFIER) == self._step
        ]
        require(
            len(uploads) == 1,
            f"the lane's {self._job!r} job does not report exactly one "
            f"{self._step!r} step",
        )
        require(
            uploads[0].get("conclusion") == "success",
            f"the lane's {self._step!r} step did not succeed (it reported "
            f"{_text(uploads[0].get('conclusion'), 40)!r}), so this attempt did not "
            "publish the evidence it is being asked to vouch for",
        )

    def recorded_artifact(self, payload: object, *, window: dict) -> dict:
        """GitHub's record for the evidence artifact, bound to one attempt by time.

        Read from the documented per-run artifacts endpoint. Every field used here
        is GitHub's own: the artifact's name, expiry, run binding and digest, plus
        the upload stamp that places it inside the verified attempt's interval.

        Returns ``{}`` when the lane published no retrievable evidence artifact --
        an honest gap. An expired artifact is such a gap: its bytes are gone, so
        nothing can be authenticated against its digest.
        """
        __tracebackhide__ = True
        require(
            isinstance(payload, dict),
            "the evidence producer response was not an object",
        )
        assert isinstance(payload, dict)
        artifacts = payload.get("artifacts")
        require(
            isinstance(artifacts, list),
            "the evidence producer response carried no artifacts array",
        )
        assert isinstance(artifacts, list)
        require(
            len(artifacts) <= MAX_ARCHIVE_MEMBERS,
            "the evidence producer listed more artifacts than this reads",
        )
        named = [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("name") == EVIDENCE_ARTIFACT_NAME
        ]
        live = [item for item in named if item.get("expired") is not True]
        if not live:
            return {}
        # THE ATTEMPT BINDING, from GitHub's upload stamp against the verified
        # attempt's interval. Attempts of a run are consecutive, so an artifact
        # left by an earlier attempt is created before this attempt began.
        within = []
        for artifact in live:
            created = _moment(
                artifact.get("created_at"), "the evidence artifact's creation time"
            )
            if window["started_at"] <= created <= window["completed_at"]:
                within.append((artifact, created))
        if not within:
            return {}
        require(
            len(within) == 1,
            "the lane recorded more than one evidence artifact inside this "
            "attempt's window; which one attests the retained set is ambiguous",
        )
        artifact, created = within[0]
        digest = artifact.get("digest")
        # Refused rather than skipped: an artifact published under the evidence
        # name with no usable digest is the lane breaking its own contract.
        require(
            isinstance(digest, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest.strip()) is not None,
            "the execution lane recorded its evidence artifact without a usable "
            "sha256 digest, so its archive cannot be authenticated",
        )
        assert isinstance(digest, str)
        size = artifact.get("size_in_bytes")
        require(
            not isinstance(size, int) or 0 < size <= MAX_ARCHIVE_BYTES,
            "the lane's evidence artifact is empty or exceeds the archive size "
            "limit this reads",
        )
        return {
            "digest": digest.strip(),
            "created_at": created,
            "attempt": window["attempt"],
            "size_in_bytes": size,
        }

    # -- the archive ------------------------------------------------------

    def _archive(self, recorded: dict) -> bytes:
        __tracebackhide__ = True
        require(
            bool(self._archive_dir),
            f"BLOCKED: set {ARCHIVE_DIR_VARIABLE} to the directory holding the "
            f"downloaded {EVIDENCE_ARTIFACT_NAME!r} archive; its bytes are checked "
            "against the digest GitHub recorded for that artifact",
        )
        path = Path(self._archive_dir) / f"{EVIDENCE_ARTIFACT_NAME}.zip"
        require(
            path.is_file() and not path.is_symlink(),
            f"BLOCKED: the evidence archive was not found as a regular file at "
            f"{path.name}; download the artifact GitHub recorded for that run",
        )
        try:
            require(
                0 < path.stat().st_size <= MAX_ARCHIVE_BYTES,
                "BLOCKED: the evidence archive is empty or exceeds the size limit",
            )
            return path.read_bytes()
        except OSError:
            raise EvidenceError(
                "BLOCKED: the evidence archive could not be read"
            ) from None

    @staticmethod
    def manifest_of_archive(raw: bytes, recorded: dict) -> str:
        """Authenticate the archive, then derive the evidence set's digest from it.

        The order is the whole point. GitHub's recorded digest authenticates the
        ZIP **archive**, so the archive's bytes are hashed and compared against it
        first; only then is the archive opened and its members read. Comparing
        GitHub's archive digest directly against the evidence set's manifest digest
        -- two different byte sequences -- is what made every genuine artifact fail.

        The returned value is the canonical set digest ``load_ledger`` computes from
        the retained bodies, derived here from the producer's own copy of the same
        bodies using the same function. So agreement means the retained set is the
        set the lane published.
        """
        __tracebackhide__ = True
        require(
            0 < len(raw) <= MAX_ARCHIVE_BYTES,
            "the evidence archive is empty or exceeds the size limit",
        )
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        require(
            actual == recorded["digest"],
            "the evidence archive does not match the digest GitHub recorded for "
            "that artifact, so it is not the archive the lane published",
        )
        bodies: list[tuple[str, str]] = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
                entries = bundle.infolist()
                require(
                    0 < len(entries) <= MAX_ARCHIVE_MEMBERS,
                    "the evidence archive is empty or holds more members than this "
                    "reads",
                )
                total = 0
                for entry in entries:
                    name = entry.filename
                    # Refused, not skipped. An archive carrying anything other than
                    # the flat evidence bodies is not the contracted artifact, and
                    # reading it best-effort would authenticate a subset of it.
                    require(
                        ARCHIVE_MEMBER_PATTERN.fullmatch(name) is not None,
                        "the evidence archive holds a member that is not a plain "
                        "evidence body name; absolute paths, directories and "
                        "traversal are refused",
                    )
                    require(
                        not entry.is_dir() and (entry.external_attr >> 28) != 0xA,
                        "the evidence archive holds a directory or symlink member",
                    )
                    total += entry.file_size
                    require(
                        entry.file_size <= MAX_ARCHIVE_UNCOMPRESSED_BYTES
                        and total <= MAX_ARCHIVE_UNCOMPRESSED_BYTES,
                        "the evidence archive expands beyond the uncompressed limit",
                    )
                    bodies.append((name, hashlib.sha256(bundle.read(name)).hexdigest()))
        except EvidenceError:
            raise
        except Exception:  # noqa: BLE001 - zipfile raises a wide, version-dependent set
            raise EvidenceError(
                "BLOCKED: the evidence archive could not be read as a zip archive"
            ) from None
        names = [name for name, _ in bodies]
        require(
            len(set(names)) == len(names),
            "the evidence archive names the same evidence body twice, so which "
            "bytes it attests is ambiguous",
        )
        return _manifest_digest(bodies)


def _parse(content: bytes, label: str):
    """Parse a response without letting its body survive into a traceback."""
    __tracebackhide__ = True

    def pairs(items):
        __tracebackhide__ = True
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate field in {label}")
            result[key] = value
        return result

    def constant(_value):
        __tracebackhide__ = True
        raise EvidenceError(f"non-JSON numeric constant in {label}")

    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)
    except EvidenceError as exc:
        raise EvidenceError(str(exc)) from None
    except (ValueError, UnicodeError, RecursionError):
        raise EvidenceError(f"malformed {label}; body withheld") from None


class _RawStore:
    """Retains raw observations privately and publishes only reference and digest.

    The evidence artifact is meant to be shared, so raw API and cluster output
    stays here beside it at ``0600`` while the report carries the filename and its
    sha256. That is what makes a finding re-derivable without republishing a body
    that may contain node addresses, annotations or tokens.
    """

    def __init__(self, evidence_file: str) -> None:
        __tracebackhide__ = True
        self.directory = Path(evidence_file + ".raw")
        try:
            # Exclusive: refuses an existing directory, so one run cannot land in
            # another run's retained evidence.
            self.directory.mkdir(mode=0o700)
        except OSError:
            raise EvidenceError(
                "BLOCKED: a retained-evidence directory could not be created "
                "beside the evidence file"
            ) from None
        self._sequence = 0

    def retain(self, label: str, content: bytes) -> tuple[str, str]:
        __tracebackhide__ = True
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,60}", label) is not None,
            "retained-evidence labels must be lowercase readable names",
        )
        self._sequence += 1
        name = f"{self._sequence:03d}-{label}"
        path = self.directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise EvidenceError("raw observation could not be retained") from None
        return f"{self.directory.name}/{name}", hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class _Reference:
    """Where a retained raw observation lives, and its digest."""

    reference: str
    digest: str

    @staticmethod
    def combined(*parts: _Reference) -> _Reference:
        """One reference for a finding derived from several retained reads.

        A record that names two files must pin both of them. Publishing only the
        first file's digest would leave the second half of its evidence
        unverifiable: a later reader could not tell whether the nodepool listing
        behind an enumerated handle had been altered. The combined digest is the
        sha256 over each part's ``reference:digest`` line, so it changes if
        either constituent read changes, and the order is fixed by the argument
        order rather than by dict iteration.
        """
        __tracebackhide__ = True
        require(bool(parts), "a combined reference needs at least one part")
        return _Reference(
            reference="; ".join(part.reference for part in parts),
            digest=hashlib.sha256(
                "\n".join(f"{p.reference}:{p.digest}" for p in parts).encode()
            ).hexdigest(),
        )


class _ReceiptWindow:
    """The authorized window a retained record's observation time must fall in.

    Deliberately the same rule ``live_baseline._Window`` applies to published
    facts, stated here separately because it is applied at a different moment: to
    the *declared* time inside an operator-supplied file, before that file is
    allowed to settle anything. It opens when the operator's authorized window
    opened and closes now, so a record cannot be dated into the future and a
    record retained from an earlier, differently-authorized session cannot be
    replayed into this one.

    ``contains`` reads the clock on each call rather than pinning a close instant
    at construction, because the ledger is loaded once and consulted per record.
    """

    def __init__(self, opened: datetime) -> None:
        self.opened = opened

    def contains(self, moment: datetime) -> bool:
        return self.opened <= moment <= datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Machine:
    """One baseline machine, correlated across its records.

    The identity is built once from the SuperplaneNode record and reused for every
    fact about this machine in every dimension, which is what lets
    ``live_baseline._Correlation`` confirm the provisioning, join, workload and
    cleanup evidence all concern the same resource.
    """

    identity: ResourceIdentity
    record: dict
    node: dict | None
    cluster: dict | None


def _moment(value: object, label: str) -> datetime:
    """An instant GitHub stamped, as an aware UTC datetime.

    Raised rather than defaulted: a missing or unreadable timestamp on the
    producer's own record leaves the attempt's interval unknown, and an unknown
    interval cannot bind an artifact to an attempt.
    """
    __tracebackhide__ = True
    require(
        isinstance(value, str) and value.strip() != "",
        f"BLOCKED: {label} is missing from the lane's own record, so the attempt "
        "window it bounds cannot be established",
    )
    assert isinstance(value, str)
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError(f"BLOCKED: {label} is not an ISO-8601 instant") from None
    if moment.tzinfo is None:
        # GitHub stamps UTC; a naive value here would otherwise compare against
        # aware instants and raise deep inside the window comparison.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _text(value: object, limit: int = 120) -> str:
    """A bounded readable rendering of an observed scalar."""
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, int | float):
        return str(value)
    return str(value)[:limit].replace("\n", " ").strip() or "(empty)"


class LiveBaselineObserver:
    """Observes the selected baseline through the two read-only transports.

    Constructed by :func:`build_observer` from the validated settings; the
    transports are injectable so the read and mapping behavior can be regressed
    offline, in which case :meth:`transport_is_live` reports False and nothing can
    be published as live.
    """

    def __init__(
        self,
        config: dict,
        skypilot: ReadTransport,
        kubectl: ReadTransport,
        store: _RawStore,
        provider: object | None = None,
    ) -> None:
        self._config = config
        self._metadata = config["target_metadata"]
        self._skypilot = skypilot
        self._kubectl = kubectl
        self._store = store
        # None when the selected provider has no reviewed read-only client, in
        # which case provider-side absence stays unconfirmed and says so.
        self._provider = provider
        self._cache: dict[str, tuple[object, _Reference]] = {}
        self._context: tuple[str, str, _Reference] | None = None
        self._ledger_cache: OperationLedger | None = None

    # -- reads ---------------------------------------------------------------

    def _ledger(self) -> OperationLedger:
        """The operator's retained records, read once and validated as a whole.

        The machines are enumerated first, because every record has to name a
        resource this capture saw for itself -- that is what stops a well-formed
        record about a different operation from settling a check here.
        """
        __tracebackhide__ = True
        if self._ledger_cache is None:
            machines, _ = self._machines()
            observed = frozenset(
                handle
                for machine in machines
                for handle in machine.identity.correlation_handles()
            )
            self._ledger_cache = load_ledger(
                self._config,
                window=_ReceiptWindow(self._config["window_start"]),
                retain=self._store.retain,
                observed_handles=observed,
            )
        return self._ledger_cache

    def _derived_facts(
        self, check_id: str, identity: ResourceIdentity
    ) -> tuple[ObservedFact, ...]:
        """Whatever retained records settle this check for this resource, if any.

        Matching is by the resource's own correlation handles, so a record about
        another operation's machine cannot settle this one's check even when it is
        otherwise valid. An empty result means no record named this resource, which
        the callers turn into a named missing input rather than a silent skip.
        """
        __tracebackhide__ = True
        records = self._ledger().evidence_for(check_id, identity.correlation_handles())
        return tuple(
            self._from_record(check_id, identity, record) for record in records
        )

    def _receipt_facts(
        self, check_id: str, identity: ResourceIdentity, fallback: _Reference
    ) -> tuple[ObservedFact, ...]:
        """Facts for a check only a retained record can settle.

        Returns the derived outcomes when records exist for this resource, and a
        single indeterminate naming the missing record when they do not. Either way
        the check is answered explicitly; it is never silently skipped.
        """
        __tracebackhide__ = True
        derived = self._derived_facts(check_id, identity)
        if derived:
            return derived
        return (
            self._needs_receipt(check_id, identity, fallback, MISSING_RECORD[check_id]),
        )

    def _from_record(
        self, check_id: str, identity: ResourceIdentity, record: ReceiptEvidence
    ) -> ObservedFact:
        """One retained record's derived outcome, as a fact about this machine.

        For the two cleanup checks the record can only *claim* satisfaction, the
        provider is asked directly and its answer overrides the claim. A record
        saying the machine was released while the provider still reports it is a
        refutation, which is precisely the case a teardown's own success hides.

        An unauthenticated record is **not** taken to the provider at all. The
        instance id would itself come from unvouched material, so a forged
        submission naming any long-terminated machine would come back confirmed
        absent -- turning the independent provider read into a laundering step for
        the very evidence it is meant to check. Such a record stays the
        indeterminate the ledger already made it.
        """
        __tracebackhide__ = True
        outcome, detail = record.outcome, record.detail
        confirmed = False
        reference = _Reference(record.evidence_reference, record.evidence_sha256)
        if record.needs_provider_confirmation and record.authenticated:
            outcome, detail, confirmed, reference = self._confirm_with_provider(
                record, reference
            )
        return ObservedFact(
            check_id=check_id,
            environment=self._config["environment"],
            revision=self._config["revision"],
            # The record's own observation time, not now: a cancellation or a
            # teardown was observed when it happened, and relabelling it with the
            # capture's clock would erase the fact the window check relies on.
            observed_at=record.observed_at,
            resource=identity,
            outcome=outcome,
            detail=detail,
            evidence_reference=reference.reference,
            evidence_sha256=reference.digest,
            provider_absence_confirmed=confirmed,
        )

    def _confirm_with_provider(
        self, record: ReceiptEvidence, reference: _Reference
    ) -> tuple[Outcome, str, bool, _Reference]:
        """Ask the provider whether the instance is really gone.

        Three outcomes, kept distinct. No registered reader for this provider: the
        check stays indeterminate and names the gap, because nothing here may stand
        in for the provider. Reader says gone: satisfied, and
        ``provider_absence_confirmed`` is set -- the only path that sets it. Reader
        says still there: refuted, whatever the record claimed.
        """
        __tracebackhide__ = True
        if self._provider is None:
            return (
                Outcome.INDETERMINATE,
                (
                    f"{record.detail} No reviewed read-only provider client is "
                    f"registered for {_text(self._metadata['provider'])}, so "
                    "provider-side absence cannot be confirmed."
                ),
                False,
                reference,
            )
        content = self._provider.describe(record.instance)
        retained, digest = self._store.retain(
            f"provider-describe-{record.instance}.json"[:60], content
        )
        combined = _Reference.combined(reference, _Reference(retained, digest))
        payload = _parse(content, "provider instance description")
        if type(self._provider).instance_present(payload):
            return (
                Outcome.REFUTED,
                (
                    f"The provider still reports instance {_text(record.instance)} "
                    "as existing, so capacity was not released despite the recorded "
                    "teardown."
                ),
                False,
                combined,
            )
        return (
            Outcome.SATISFIED,
            (
                f"{record.detail} The provider independently confirms instance "
                f"{_text(record.instance)} no longer exists."
            ),
            True,
            combined,
        )

    def _read(self, transport: ReadTransport, selector: str, label: str):
        __tracebackhide__ = True
        if label not in self._cache:
            content = transport(selector)
            reference, digest = self._store.retain(label, content)
            self._cache[label] = (
                _parse(content, label),
                _Reference(reference, digest),
            )
        return self._cache[label]

    def _sky(self, selector: str, label: str):
        return self._read(self._skypilot, selector, label)

    def _kube(self, selector: str):
        __tracebackhide__ = True
        self._require_selected_cluster()
        return self._read(self._kubectl, selector, f"kubectl-{selector}.json")

    def _require_selected_cluster(self) -> tuple[str, str, _Reference]:
        """Refuse evidence read from a cluster other than the selected one.

        Reading the right fields from the wrong cluster is exactly the foreign
        evidence the record is supposed to exclude, and the kubeconfig is an
        environment variable this code did not choose.

        The comparison is by **exact canonical identity**, not containment. The
        second review reproduced a context named
        ``arn:aws:eks:...:cluster/workspace-eks-attacker`` passing a containment
        test against ``workspace-eks``, after which its node facts were labelled as
        the selected cluster's -- so a prefix, a suffix and a lookalike all had to
        become simply different strings. Two things must agree: the ARN, which
        carries account, region and name, and the API server address actually being
        talked to, so a proxy pointed elsewhere cannot answer for the right ARN.

        A context whose name is not an ARN is refused rather than compared loosely:
        without account and region there is nothing to distinguish the selected
        cluster from a same-named cluster in another account.
        """
        __tracebackhide__ = True
        if self._context is not None:
            return self._context
        view, reference = self._read(self._kubectl, "context", "kubectl-context.json")
        require(isinstance(view, dict), "kubectl context view was not a JSON object")
        clusters = view.get("clusters") or []
        require(
            isinstance(clusters, list) and len(clusters) == 1,
            "the kubeconfig must resolve to exactly one cluster",
        )
        require(
            isinstance(clusters[0], dict), "the kubeconfig cluster was not an object"
        )
        name = _text(clusters[0].get("name", ""), MAX_IDENTIFIER)
        entry = clusters[0].get("cluster") or {}
        require(
            isinstance(entry, dict), "the kubeconfig cluster entry was not an object"
        )
        server = _text(entry.get("server", ""), MAX_IDENTIFIER)
        require(
            name == self._metadata["workspace_cluster_arn"],
            "BLOCKED: the kubeconfig's cluster identity is not the selected "
            "target's EKS ARN; observations would concern another cluster",
        )
        require(
            server == self._metadata["workspace_api_endpoint"],
            "BLOCKED: the kubeconfig's API server is not the selected cluster's "
            "endpoint; observations would be read from another API server",
        )
        # An exec credential plugin in a kubeconfig runs a program of the
        # kubeconfig's choosing. installation/runner.py refuses the same shapes for
        # the same reason; this read path is no more entitled to run one.
        require(
            not entry.get("insecure-skip-tls-verify")
            and not entry.get("proxy-url")
            and not entry.get("tls-server-name"),
            "BLOCKED: the kubeconfig overrides TLS verification, proxy transport or "
            "the TLS server name, so the observed endpoint cannot be trusted",
        )
        for user in view.get("users") or []:
            if not isinstance(user, dict):
                continue
            credential = user.get("user") or {}
            require(
                isinstance(credential, dict) and "exec" not in credential,
                "BLOCKED: the kubeconfig would execute a credential plugin; supply "
                "static token or client-certificate access for a read-only capture",
            )
        self._context = (name, server, reference)
        return self._context

    # -- correlated machine inventory ---------------------------------------

    def _observed_cloud(self, machine_cluster: dict | None) -> str:
        """The cloud the machine's cluster reports it was launched on.

        Normalized to lower case because the API server reports ``Nebius`` while a
        registered target records ``nebius``; the comparison is meant to catch a
        different provider, not a different capitalization of the same one.
        """
        handle = (machine_cluster or {}).get("handle") or {}
        if not isinstance(handle, dict):
            return ""
        launched = handle.get("launched_resources") or {}
        if not isinstance(launched, dict):
            return ""
        return _text(launched.get("cloud", ""), MAX_IDENTIFIER).lower().strip()

    def _observed_owner(self, record: dict) -> str:
        """Which controller the environment says owns this node record.

        Read from the record's own ownership metadata. An unowned record reports
        nothing rather than inheriting the selected controller's name, because
        "no owner is recorded" is a finding the lifecycle checks need to see.
        """
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict):
            return ""
        for owner in metadata.get("ownerReferences") or []:
            if isinstance(owner, dict) and owner.get("name"):
                return _text(owner.get("name", ""), MAX_IDENTIFIER)
        labels = metadata.get("labels") or {}
        if isinstance(labels, dict):
            managed = labels.get("app.kubernetes.io/managed-by")
            if managed:
                return _text(managed, MAX_IDENTIFIER)
        return ""

    def _machines(self) -> tuple[tuple[_Machine, ...], _Reference]:
        __tracebackhide__ = True
        observed_cluster, _, _ = self._require_selected_cluster()
        records, reference = self._kube("superplane-nodes")
        require(isinstance(records, dict), "SuperplaneNode listing was not an object")
        items = records.get("items")
        require(isinstance(items, list), "SuperplaneNode listing has no items array")
        nodes, _ = self._kube("nodes")
        by_name = {
            str((item.get("metadata") or {}).get("name", "")): item
            for item in (nodes.get("items") or [])
            if isinstance(item, dict)
        }
        clusters = {entry.get("name"): entry for entry in self._clusters()[0]}
        machines = []
        for item in items:
            require(isinstance(item, dict), "a SuperplaneNode entry was not an object")
            status = item.get("status") or {}
            require(
                isinstance(status, dict), "a SuperplaneNode status was not an object"
            )
            skypilot_cluster = _text(status.get("skypilotCluster", ""), 200)
            node_name = _text(status.get("k8sNodeName", ""), 200)
            instance = _text(status.get("ssmInstanceId", ""), 200)
            if not any(
                handle and handle != "(empty)"
                for handle in (skypilot_cluster, node_name, instance)
            ):
                # A record with no handle identifies nothing; it cannot carry a
                # fact and must not silently seed a correlation.
                continue
            # Provider and controller come from what the environment reported about
            # THIS machine, not from the selection. Copying the expectation in and
            # then comparing it back was the defect: a machine launched on another
            # cloud, or owned by another controller, was relabelled as the selected
            # target's. Where the environment reports neither, the field is left
            # empty -- unverified rather than assumed to match.
            cloud = self._observed_cloud(machine_cluster=clusters.get(skypilot_cluster))
            owner = self._observed_owner(item)
            identity = ResourceIdentity(
                provider=cloud,
                provider_resource_id="" if instance == "(empty)" else instance,
                # The cluster these observations were actually read from, established
                # by _require_selected_cluster against the target's canonical ARN.
                cluster=observed_cluster,
                kubernetes_node="" if node_name == "(empty)" else node_name,
                controller=owner,
                skypilot_cluster=(
                    "" if skypilot_cluster == "(empty)" else skypilot_cluster
                ),
            )
            machines.append(
                _Machine(
                    identity=identity,
                    record=item,
                    node=by_name.get(identity.kubernetes_node),
                    cluster=clusters.get(identity.skypilot_cluster),
                )
            )
        return tuple(machines), reference

    def _clusters(self) -> tuple[list[dict], _Reference]:
        __tracebackhide__ = True
        payload, reference = self._sky("/status", "skypilot-status.json")
        # The maintained client's StatusResponse is a bare array (client.go:87-97).
        require(isinstance(payload, list), "SkyPilot /status did not return an array")
        entries = [entry for entry in payload if isinstance(entry, dict)]
        require(
            len(entries) == len(payload),
            "SkyPilot /status returned a non-object cluster entry",
        )
        return entries, reference

    # -- fact construction ---------------------------------------------------

    def _fact(
        self,
        check_id: str,
        identity: ResourceIdentity,
        outcome: Outcome,
        detail: str,
        reference: _Reference,
        *,
        request_reference: str = "",
        hourly_cost: float | None = None,
        provider_absence_confirmed: bool = False,
    ) -> ObservedFact:
        return ObservedFact(
            check_id=check_id,
            environment=self._config["environment"],
            revision=self._config["revision"],
            observed_at=datetime.now(timezone.utc),
            resource=identity,
            outcome=outcome,
            detail=detail,
            evidence_reference=reference.reference,
            evidence_sha256=reference.digest,
            request_reference=request_reference,
            hourly_cost=hourly_cost,
            provider_absence_confirmed=provider_absence_confirmed,
        )

    def _needs_receipt(
        self,
        check_id: str,
        identity: ResourceIdentity,
        reference: _Reference,
        missing: str,
    ) -> ObservedFact:
        """An honest indeterminate result naming the exact input that is absent.

        These are the checks a steady-state read cannot settle after the fact.
        Reporting them as indeterminate fails their check, which is the point: the
        run is blocked on a named input instead of passing on a partial read.
        """
        return self._fact(
            check_id,
            identity,
            Outcome.INDETERMINATE,
            f"Not observable from a read-only steady-state read. Required input: {missing}",
            reference,
        )

    # -- BaselineObserver ----------------------------------------------------

    def observe(self, dimension: Dimension) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        require(
            isinstance(dimension, Dimension),
            "observations are requested per parity dimension",
        )
        builders = {
            Dimension.PROVIDER_SELECTION: self._provider_facts,
            Dimension.NODE_REGISTRATION: self._node_facts,
            Dimension.BATCH_WORKLOAD: self._batch_facts,
            Dimension.SERVING_WORKLOAD: self._serving_facts,
            Dimension.STATUS_AND_LOGS: self._status_facts,
            Dimension.STOP_CANCELLATION: self._cancel_facts,
            Dimension.CONTROLLER_LIFECYCLE: self._lifecycle_facts,
            Dimension.COST_AND_CLEANUP: self._cost_facts,
        }
        builder = builders.get(dimension)
        require(
            builder is not None,
            f"no read is defined for dimension {dimension.value}",
        )
        assert builder is not None  # narrowed by the require above
        facts = builder()
        known = frozenset(
            check.check_id for check in dimension_by_name(dimension).checks
        )
        stray = sorted({fact.check_id for fact in facts} - known)
        require(
            not stray,
            f"{dimension.value}: built facts for foreign checks: " + ", ".join(stray),
        )
        return facts

    def _provider_facts(self) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        machines, reference = self._machines()
        if not machines:
            return ()
        clouds, clouds_reference = self._sky(
            "/enabled_clouds", "skypilot-enabled-clouds.json"
        )
        require(isinstance(clouds, dict), "enabled clouds response was not an object")
        enabled = {
            str(entry.get("name", "")).lower()
            for entry in (clouds.get("enabled_clouds") or [])
            if isinstance(entry, dict) and entry.get("enabled") is True
        }
        facts: list[ObservedFact] = []
        for machine in machines:
            resources = (machine.cluster or {}).get("handle") or {}
            launched = (
                resources.get("launched_resources") or {}
                if isinstance(resources, dict)
                else {}
            )
            cloud = str(launched.get("cloud", "")).lower()
            if not machine.cluster:
                facts.append(
                    self._needs_receipt(
                        "provider.configured-clouds-restrict-selection",
                        machine.identity,
                        reference,
                        "the machine's SkyPilot cluster is no longer registered; "
                        "capture cloud restriction while the cluster exists",
                    )
                )
            elif not cloud:
                facts.append(
                    self._fact(
                        "provider.configured-clouds-restrict-selection",
                        machine.identity,
                        Outcome.INDETERMINATE,
                        "The cluster reports no launched cloud, so restriction "
                        "cannot be established.",
                        clouds_reference,
                    )
                )
            else:
                satisfied = cloud in enabled
                facts.append(
                    self._fact(
                        "provider.configured-clouds-restrict-selection",
                        machine.identity,
                        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
                        f"Launched cloud {_text(cloud)} is "
                        + ("within" if satisfied else "outside")
                        + " the enabled clouds "
                        + (", ".join(sorted(enabled)) or "(none reported)"),
                        clouds_reference,
                    )
                )
            autostop = (machine.cluster or {}).get("autostop")
            if isinstance(autostop, bool) or not isinstance(autostop, int):
                facts.append(
                    self._fact(
                        "provider.launch-request-shape",
                        machine.identity,
                        Outcome.INDETERMINATE,
                        "The cluster reports no numeric idle-autostop value.",
                        reference,
                    )
                )
            else:
                satisfied = autostop == DEFAULT_IDLE_MINUTES_TO_AUTOSTOP
                facts.append(
                    self._fact(
                        "provider.launch-request-shape",
                        machine.identity,
                        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
                        f"Effective idle-autostop is {autostop} minutes; the "
                        f"baseline default is {DEFAULT_IDLE_MINUTES_TO_AUTOSTOP}",
                        reference,
                    )
                )
            # Neither the option list a launch was offered nor a failed first
            # option survives into steady state; both come from the authorized
            # launch's retained record, or the check names what is missing.
            facts.extend(
                self._receipt_facts(
                    "provider.ordering-cheapest-first", machine.identity, reference
                )
            )
            facts.extend(
                self._receipt_facts(
                    "provider.fallback-on-launch-failure", machine.identity, reference
                )
            )
        return tuple(facts)

    def _node_facts(self) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        machines, reference = self._machines()
        facts: list[ObservedFact] = []
        for machine in machines:
            node = machine.node
            if node is None:
                facts.append(
                    self._fact(
                        "node.join-produces-ready-node",
                        machine.identity,
                        Outcome.REFUTED,
                        "The record names no Kubernetes node present in the "
                        "selected cluster, so the join is not evidenced.",
                        reference,
                    )
                )
            else:
                conditions = {
                    str(entry.get("type", "")): str(entry.get("status", ""))
                    for entry in ((node.get("status") or {}).get("conditions") or [])
                    if isinstance(entry, dict)
                }
                ready = conditions.get("Ready", "Unknown")
                outcome = (
                    Outcome.SATISFIED
                    if ready == "True"
                    else Outcome.REFUTED
                    if ready == "False"
                    else Outcome.INDETERMINATE
                )
                facts.append(
                    self._fact(
                        "node.join-produces-ready-node",
                        machine.identity,
                        outcome,
                        f"Node condition Ready={_text(ready)}",
                        reference,
                    )
                )
            linked = bool(machine.identity.kubernetes_node) and node is not None
            facts.append(
                self._fact(
                    "node.status-links-to-k8s-node",
                    machine.identity,
                    Outcome.SATISFIED if linked else Outcome.REFUTED,
                    "The node record resolves to an existing Kubernetes node."
                    if linked
                    else "The node record does not resolve to a Kubernetes node.",
                    reference,
                )
            )
            # The CNI prerequisite is asserted during onboarding and leaves no
            # trace on a joined node, so it is settled from the onboarding record.
            facts.extend(
                self._receipt_facts(
                    "node.cni-prerequisite-recorded", machine.identity, reference
                )
            )
            leaked = self._activation_material(machine)
            facts.append(
                self._fact(
                    "node.activation-secret-not-leaked",
                    machine.identity,
                    Outcome.REFUTED if leaked else Outcome.SATISFIED,
                    "Activation material appears in the node record."
                    if leaked
                    else "No activation id or code appears in the read records.",
                    reference,
                )
            )
        return tuple(facts)

    def _activation_material(self, machine: _Machine) -> bool:
        """Whether any read record exposes SSM activation material."""
        rendered = json.dumps(
            [machine.record, machine.node], sort_keys=True, default=str
        )
        return any(marker in rendered for marker in ACTIVATION_MARKERS)

    def _batch_facts(self) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        machines, reference = self._machines()
        if not machines:
            return ()
        pods, pods_reference = self._kube("pods")
        require(isinstance(pods, dict), "pod listing was not an object")
        facts: list[ObservedFact] = []
        for machine in machines:
            gpu = self._gpu_pods(pods, machine.identity.kubernetes_node)
            if not gpu:
                facts.append(
                    self._needs_receipt(
                        "batch.gpu-workload-schedules",
                        machine.identity,
                        pods_reference,
                        "an authorized GPU workload present on the node at "
                        "capture time",
                    )
                )
            else:
                phases = {
                    str((pod.get("status") or {}).get("phase", "")) for pod in gpu
                }
                satisfied = phases <= {"Running", "Succeeded"} and bool(phases)
                facts.append(
                    self._fact(
                        "batch.gpu-workload-schedules",
                        machine.identity,
                        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
                        "GPU workload phases on the node: " + ", ".join(sorted(phases)),
                        pods_reference,
                        request_reference=_text(
                            (gpu[0].get("metadata") or {}).get("name", ""), 100
                        ),
                    )
                )
            allocatable = self._gpu_allocatable(machine)
            facts.append(
                self._fact(
                    "batch.device-plugin-advertises-gpu",
                    machine.identity,
                    Outcome.SATISFIED if allocatable > 0 else Outcome.REFUTED,
                    f"Node advertises nvidia.com/gpu allocatable {allocatable}",
                    reference,
                )
            )
        return tuple(facts)

    def _gpu_pods(self, pods: dict, node_name: str) -> list[dict]:
        if not node_name:
            return []
        selected = []
        for pod in pods.get("items") or []:
            if not isinstance(pod, dict):
                continue
            if str((pod.get("spec") or {}).get("nodeName", "")) != node_name:
                continue
            containers = (pod.get("spec") or {}).get("containers") or []
            requests_gpu = any(
                "nvidia.com/gpu" in ((container.get("resources") or {}).get(key) or {})
                for container in containers
                if isinstance(container, dict)
                for key in ("limits", "requests")
            )
            if requests_gpu:
                selected.append(pod)
        return selected

    def _gpu_allocatable(self, machine: _Machine) -> int:
        allocatable = ((machine.node or {}).get("status") or {}).get(
            "allocatable"
        ) or {}
        try:
            return int(str(allocatable.get("nvidia.com/gpu", "0")))
        except ValueError:
            return 0

    SERVING_CHECKS = (
        "serving.endpoint-reachable",
        "serving.unauthenticated-request-refused",
        "serving.owning-controller-identified",
        "serving.teardown-removes-replicas",
    )

    def _serving_facts(self) -> tuple[ObservedFact, ...]:
        """Serving checks, reported per service the authoritative listing names.

        Every one of them concerns a request or an operation against a service, so
        none is readable from steady state; they are settled from the retained
        records of the authorized operation, per service, or they name what is
        missing. Each service gets its own identity, because "one of the services
        was reachable" is not the same finding as "this service was".

        Emitting nothing when the listing enumerates no service is what keeps the
        serving criterion's absence branch consistent with the inventory --
        ``live_baseline._serving_consistency`` refuses a run where the two disagree.
        Returning nothing because no listing was retained would be the old defect,
        so that case is impossible here: ``serving_inventory`` returns ``None`` and
        the criterion is blocked before reaching this point.
        """
        __tracebackhide__ = True
        inventory = self.serving_inventory()
        if inventory is None or not inventory.services:
            return ()
        listing = self._ledger().service_inventory
        require(
            listing is not None,
            "serving inventory requires its authoritative retained listing",
        )
        reference = _Reference(inventory.evidence_reference, inventory.evidence_sha256)
        facts: list[ObservedFact] = []
        for index, service in enumerate(inventory.services):
            # The owning controller comes from the listing, which is the only thing
            # that reports it. Where it reported none the field stays empty:
            # unverified, rather than backfilled from the selection.
            controller = listing.controllers[index] if listing.controllers else ""
            identity = ResourceIdentity(
                cluster=self._selected_cluster_identity(),
                controller=controller,
                skypilot_cluster=service,
            )
            for check_id in self.SERVING_CHECKS:
                facts.extend(self._receipt_facts(check_id, identity, reference))
        return tuple(facts)

    def _selected_cluster_identity(self) -> str:
        """The canonical cluster ARN this capture verified it is reading from.

        Read back from the verified kubeconfig context rather than from the target
        metadata, so a serving identity carries the cluster actually talked to.
        """
        __tracebackhide__ = True
        observed, _, _ = self._require_selected_cluster()
        return observed

    def _status_facts(self) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        machines, reference = self._machines()
        facts: list[ObservedFact] = []
        for machine in machines:
            raw = str((machine.cluster or {}).get("status", ""))
            mapped = map_cluster_status(raw) if raw else "unknown"
            if not raw:
                facts.append(
                    self._fact(
                        "status.cluster-status-mapped",
                        machine.identity,
                        Outcome.INDETERMINATE,
                        "The machine's SkyPilot cluster reports no status.",
                        reference,
                    )
                )
            else:
                facts.append(
                    self._fact(
                        "status.cluster-status-mapped",
                        machine.identity,
                        Outcome.SATISFIED if mapped != "unknown" else Outcome.REFUTED,
                        f"Cluster status {_text(raw)} maps to {_text(mapped)}",
                        reference,
                    )
                )
            # A progress stream exists only while the request runs; afterwards only
            # the operator's retained copy of it does.
            for check_id in (
                "status.progress-lines-streamed",
                "status.terminal-event-ends-stream",
            ):
                facts.extend(self._receipt_facts(check_id, machine.identity, reference))
        return tuple(facts)

    CANCEL_CHECKS = (
        "cancel.in-flight-launch-stops",
        "cancel.timeout-bounded",
        "cancel.cancelled-launch-releases-capacity",
    )

    def _cancel_facts(self) -> tuple[ObservedFact, ...]:
        """Cancellation, which by definition leaves nothing behind to read.

        All three checks come from the controller's retained cancellation record,
        and the capacity one additionally has the provider asked directly -- a
        cancellation that dropped its handle while the instance kept billing is the
        failure this criterion exists to catch.
        """
        __tracebackhide__ = True
        machines, reference = self._machines()
        return tuple(
            fact
            for machine in machines
            for check_id in self.CANCEL_CHECKS
            for fact in self._receipt_facts(check_id, machine.identity, reference)
        )

    def _lifecycle_facts(self) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        machines, reference = self._machines()
        facts: list[ObservedFact] = []
        owners: dict[str, int] = {}
        for machine in machines:
            handle = machine.identity.skypilot_cluster
            if handle:
                owners[handle] = owners.get(handle, 0) + 1
        for machine in machines:
            handle = machine.identity.skypilot_cluster
            if not handle:
                facts.append(
                    self._fact(
                        "lifecycle.single-owner-per-resource",
                        machine.identity,
                        Outcome.INDETERMINATE,
                        "The record claims no SkyPilot cluster, so ownership "
                        "cannot be established.",
                        reference,
                    )
                )
            else:
                count = owners[handle]
                facts.append(
                    self._fact(
                        "lifecycle.single-owner-per-resource",
                        machine.identity,
                        Outcome.SATISFIED if count == 1 else Outcome.REFUTED,
                        f"{count} node records claim this SkyPilot cluster",
                        reference,
                    )
                )
            # A restart and a redeploy are both events: a steady-state read cannot
            # tell whether the controller resumed or duplicated, only that it is
            # running now.
            for check_id in (
                "lifecycle.restart-resumes-not-duplicates",
                "lifecycle.api-state-store-survives-redeploy",
            ):
                facts.extend(self._receipt_facts(check_id, machine.identity, reference))
        return tuple(facts)

    def _cost_facts(self) -> tuple[ObservedFact, ...]:
        __tracebackhide__ = True
        machines, reference = self._machines()
        facts: list[ObservedFact] = []
        for machine in machines:
            status = machine.record.get("status") or {}
            reported = status.get("hourlyCost")
            cost = (
                float(reported)
                if isinstance(reported, int | float) and not isinstance(reported, bool)
                else None
            )
            facts.append(
                self._fact(
                    "cost.hourly-and-daily-aggregation",
                    machine.identity,
                    Outcome.SATISFIED if cost is not None else Outcome.INDETERMINATE,
                    f"Record reports hourly cost {_text(reported)} as an estimate"
                    if cost is not None
                    else "The record reports no hourly cost; unknown, not zero.",
                    reference,
                    hourly_cost=cost,
                )
            )
            # Spend past a ceiling and the down-then-purge sequence are both
            # histories, gone once the operation ends.
            for check_id in (
                "cost.observation-not-a-spend-control",
                "cleanup.down-then-purge-fallback",
            ):
                facts.extend(self._receipt_facts(check_id, machine.identity, reference))
            # Provider-side absence needs both halves: a retained teardown naming
            # the instance, and a registered reader to ask the provider about it.
            # Whichever is missing is named, because "the teardown succeeded" is
            # exactly the claim this check must not accept.
            derived = self._derived_facts(
                "cleanup.provider-side-absence-verified", machine.identity
            )
            if derived:
                facts.extend(derived)
            else:
                facts.append(
                    self._needs_receipt(
                        "cleanup.provider-side-absence-verified",
                        machine.identity,
                        reference,
                        MISSING_RECORD["cleanup.provider-side-absence-verified"]
                        if self._provider is not None
                        else "a reviewed read-only instance-describe client for "
                        f"{_text(self._metadata['provider'])}; none is registered, "
                        "so provider-side absence cannot be confirmed at all",
                    )
                )
        return tuple(facts)

    def environment_identity(self) -> ObservedEnvironmentIdentity:
        """Read provider, region, controller and runtime from the environment itself.

        Every field here comes from a response or from deployment metadata, and
        none is copied from the selection -- that was the defect. The allow-listed
        health and controller-deployment reads, previously unused, are what make
        the runtime version and the controller identity observable.

        A field the environment does not report is left empty and named in
        ``unreported`` rather than filled in from the expectation, so the record
        distinguishes "the environment agrees" from "nobody could tell".
        """
        __tracebackhide__ = True
        cluster_arn, api_endpoint, context_reference = self._require_selected_cluster()

        # Runtime version from the API server's own health response, which is the
        # endpoint the maintained client polls (client.go:67).
        health, health_reference = self._sky("/api/health", "skypilot-health.json")
        require(isinstance(health, dict), "SkyPilot health response was not an object")
        runtime = _text(health.get("version", ""), MAX_IDENTIFIER).strip()

        # Provider and region from the clusters the API server actually launched.
        # Disagreement between machines is refused rather than resolved by
        # majority: two providers in one baseline is a finding, not a tie-break.
        clusters, _ = self._clusters()
        clouds, regions = set(), set()
        for entry in clusters:
            handle = entry.get("handle") or {}
            launched = (
                handle.get("launched_resources") or {}
                if isinstance(handle, dict)
                else {}
            )
            if not isinstance(launched, dict):
                continue
            cloud = _text(launched.get("cloud", ""), MAX_IDENTIFIER).lower().strip()
            region = _text(launched.get("region", ""), MAX_IDENTIFIER).strip()
            if cloud:
                clouds.add(cloud)
            if region:
                regions.add(region)
        require(
            len(clouds) <= 1,
            "The baseline's clusters report more than one provider: "
            + ", ".join(sorted(clouds)),
        )
        require(
            len(regions) <= 1,
            "The baseline's clusters report more than one region: "
            + ", ".join(sorted(regions)),
        )

        # Controller identity from the deployments actually running in the
        # Superplane namespace.
        deployments, controllers_reference = self._kube("controllers")
        require(isinstance(deployments, dict), "controller listing was not an object")
        names = sorted(
            _text((item.get("metadata") or {}).get("name", ""), MAX_IDENTIFIER)
            for item in (deployments.get("items") or [])
            if isinstance(item, dict) and (item.get("metadata") or {}).get("name")
        )
        expected_controller = self._metadata["controller"]
        # Selected by exact name among what is deployed. Reporting the only
        # deployment when it is NOT the expected one would hand the comparison a
        # value that passes for the wrong reason, so a missing controller reports
        # nothing and the comparison then refuses the capture.
        controller = expected_controller if expected_controller in names else ""

        observed = {
            "provider": next(iter(clouds), ""),
            "region": next(iter(regions), ""),
            "controller": controller,
            "runtime_version": runtime,
            "cluster_arn": cluster_arn,
            "api_endpoint": api_endpoint,
        }
        return ObservedEnvironmentIdentity(
            **observed,
            observed_at=datetime.now(timezone.utc),
            # All three reads pin this record: the identity is derived from the
            # kubeconfig context, the health response and the controller listing
            # together, so altering any one of them changes the digest.
            evidence_reference=_Reference.combined(
                context_reference, health_reference, controllers_reference
            ).reference,
            evidence_sha256=_Reference.combined(
                context_reference, health_reference, controllers_reference
            ).digest,
            unreported=tuple(
                sorted(name for name, value in observed.items() if not value)
            ),
        )

    def serving_inventory(self) -> ServingInventory | None:
        """Enumerate serving from the authoritative service listing.

        The previous implementation scanned the ordinary cluster list for names
        beginning ``sky-serve-controller`` and treated no match as verified absence.
        U12's own inventory records why that cannot work: serving has **no
        controller-side inventory** (``spike/baseline_inventory.py``'s
        ``skyserve_services`` class), so the authoritative source is
        ``sky serve status`` from an authorized client. A running service whose
        controller cluster is not in the cluster list -- or is named differently --
        was missed entirely, and the serving criterion then passed through its
        absence branch on no evidence at all.

        There is no read-only serve endpoint to call instead: the maintained client
        exposes health, status, enabled_clouds, launch, down and the request stream
        and nothing else. So the listing arrives as a retained record, hashed and
        window-checked like every other one, and its service handles are real
        service identities rather than cluster-name guesses.

        Returning ``None`` when no listing was retained is what keeps the absence
        branch honest: the serving criterion is blocked, not passed as "nothing to
        check".
        """
        __tracebackhide__ = True
        listing = self._ledger().service_inventory
        if listing is None:
            return None
        return ServingInventory(
            enumerated_via=listing.enumerated_via,
            environment=self._config["environment"],
            revision=self._config["revision"],
            observed_at=listing.observed_at,
            evidence_reference=listing.evidence_reference,
            evidence_sha256=listing.evidence_sha256,
            services=listing.services,
            # Carried through so the absence branch can refuse an unvouched
            # listing: a forged empty listing must not establish that this
            # baseline runs no service.
            authenticated=listing.authenticated,
        )

    def existing_state(self) -> tuple[ExistingStateRecord, ...]:
        """Enumerate all five recorded state classes, every decision undecided.

        Each class is enumerated from *its own* authoritative source, which is the
        repair the second review asked for. Previously three of the five were filled
        with whatever the nearest listing held: every Kubernetes node became
        migration state regardless of whether it belonged to this system, the API
        server's backing store was recorded as a list of workload *names* -- which
        cannot distinguish a durable database from scratch -- and serving rested on
        cluster-name matching.

        A class this capture cannot settle is reported unresolved with the reason,
        never padded out. ``live_baseline.capture`` lists unresolved classes at the
        top level, so an unsettled class blocks U19's handover decision instead of
        leaving U19 to notice the gap.
        """
        __tracebackhide__ = True
        return (
            self._cluster_state(),
            self._cr_state(),
            self._api_store_state(),
            self._hybrid_node_state(),
            self._serving_state(),
        )

    def _cluster_state(self) -> ExistingStateRecord:
        """Live SkyPilot clusters, with the operations currently in flight.

        In-flight operations are recorded rather than left for the cluster list to
        imply, because a cutover during a launch or a pending teardown is exactly
        the case a handover plan has to cover: the cluster exists, and something is
        about to change it.
        """
        __tracebackhide__ = True
        clusters, reference = self._clusters()
        handles, active = [], []
        for entry in clusters:
            name = _text(entry.get("name", ""), MAX_IDENTIFIER)
            if not name or name == "(empty)":
                continue
            handles.append(name)
            status = _text(entry.get("status", ""), MAX_IDENTIFIER).upper()
            if status == "INIT":
                active.append(f"launch-in-progress/{name}")
            if entry.get("to_down") is True:
                active.append(f"teardown-pending/{name}")
        return self._state(
            "skypilot_clusters",
            "SkyPilot POST /status with no cluster_names filter, the API server's "
            "own cluster list; in-flight operations from each cluster's INIT status "
            "and to_down flag",
            tuple(sorted(handles)),
            reference,
            active_operations=tuple(sorted(set(active))),
        )

    def _cr_state(self) -> ExistingStateRecord:
        """The controller's own custom resources, from the two CRD listings."""
        __tracebackhide__ = True
        records, records_reference = self._kube("superplane-nodes")
        pools, pools_reference = self._kube("nodepools")
        handles = {
            f"{kind}/{name}"
            for kind, listing in (("superplanenode", records), ("nodepool", pools))
            for item in (listing.get("items") or [])
            if isinstance(item, dict)
            for name in [
                _text((item.get("metadata") or {}).get("name", ""), MAX_IDENTIFIER)
            ]
            if name and name != "(empty)"
        }
        return self._state(
            "superplane_node_crs",
            "kubectl get superplanenodes.superplane.ai and nodepools.superplane.ai",
            tuple(sorted(handles)),
            # Both listings are pinned: this record's handles come from the
            # SuperplaneNode and the NodePool reads together.
            _Reference.combined(records_reference, pools_reference),
        )

    def _api_store_state(self) -> ExistingStateRecord:
        """The API server's backing store, by kind and durable identity.

        This is the class the previous implementation got wrong in the way that
        costs money. It recorded the *names* of the SkyPilot namespace's workloads,
        and a name cannot tell you where cluster handles live. The distinction that
        matters is whether the store is durable and external to the pod: a redeploy
        onto fresh storage orphans every running cluster, which keeps billing with
        nothing tracking it.

        Three kinds are distinguished, from the workload's own spec:

        * an external database named through a secret reference
          (``SKYPILOT_DB_CONNECTION_URI`` via ``secretKeyRef``, as
          ``k8s/40-skypilot-api.yaml`` sets it) -- durable, and identified by the
          secret and key rather than by its value;
        * SkyPilot's default SQLite state under ``~/.sky`` on a
          ``persistentVolumeClaim`` -- durable but node-bound;
        * the same SQLite state on an ``emptyDir`` -- scratch, which that manifest
          calls out explicitly as "NOT a PersistentVolumeClaim". A redeploy loses it.

        A connection URI written inline rather than referenced is reported
        unresolved, not published: a literal URI carries a password, and this
        record is published evidence.
        """
        __tracebackhide__ = True
        payload, reference = self._kube("api-state")
        items = [
            item for item in (payload.get("items") or []) if isinstance(item, dict)
        ]
        handles, unresolved = [], []
        for item in items:
            kind = _text(item.get("kind", "workload"), MAX_IDENTIFIER).lower()
            name = _text((item.get("metadata") or {}).get("name", ""), MAX_IDENTIFIER)
            if not name or name == "(empty)":
                continue
            store, reason = self._backing_store(item)
            if reason:
                unresolved.append(f"{kind}/{name}: {reason}")
            else:
                handles.append(f"{kind}/{name} state={store}")
        if unresolved:
            return self._state(
                "skypilot_api_server_state",
                "kubectl get deployments,statefulsets in the SkyPilot namespace, "
                "read for the backing store each one resolves cluster handles from",
                (),
                reference,
                unresolved_reason=(
                    "the backing store could not be identified for: "
                    + "; ".join(sorted(unresolved))
                ),
            )
        return self._state(
            "skypilot_api_server_state",
            "kubectl get deployments,statefulsets in the SkyPilot namespace, "
            "classified by the backing store each one resolves cluster handles "
            "from: a secret-referenced external database, a PersistentVolumeClaim, "
            "or scratch",
            tuple(sorted(handles)),
            reference,
        )

    def _backing_store(self, workload: dict) -> tuple[str, str]:
        """One workload's backing store as ``(identity, unresolved_reason)``."""
        __tracebackhide__ = True
        spec = ((workload.get("spec") or {}).get("template") or {}).get("spec") or {}
        if not isinstance(spec, dict):
            return "", "the workload declares no pod template to read"
        containers = [c for c in (spec.get("containers") or []) if isinstance(c, dict)]
        for container in containers:
            for variable in container.get("env") or []:
                if (
                    not isinstance(variable, dict)
                    or variable.get("name") != DB_URI_VARIABLE
                ):
                    continue
                source = variable.get("valueFrom")
                if not isinstance(source, dict) or not isinstance(
                    source.get("secretKeyRef"), dict
                ):
                    # An inline URI holds a password. Refuse rather than publish it
                    # or silently record "external database" without its identity.
                    return "", (
                        f"{DB_URI_VARIABLE} is set inline rather than through a "
                        "secret reference, so its identity cannot be recorded "
                        "without publishing a credential"
                    )
                ref = source["secretKeyRef"]
                secret = _text(ref.get("name", ""), MAX_IDENTIFIER)
                key = _text(ref.get("key", ""), MAX_IDENTIFIER)
                # Phrased as a Kubernetes object path rather than `secret=<name>`:
                # the published-content screen reads `secret=` as a credential
                # assigned inline and refuses it, which is the right instinct on a
                # published string even though the name here is only a reference.
                return f"external-database from secret {secret}/{key}", ""
        # No connection URI: SkyPilot resolves its state under ~/.sky, so the volume
        # mounted there decides whether a redeploy keeps or loses the handles.
        mounts = {
            _text(mount.get("mountPath", ""), MAX_IDENTIFIER): _text(
                mount.get("name", ""), MAX_IDENTIFIER
            )
            for container in containers
            for mount in (container.get("volumeMounts") or [])
            if isinstance(mount, dict)
        }
        volume_name = next(
            (name for path, name in sorted(mounts.items()) if path == SKY_STATE_PATH),
            "",
        )
        if not volume_name:
            return "", (
                f"no {DB_URI_VARIABLE} and no volume mounted at {SKY_STATE_PATH}, so "
                "whether cluster handles survive a redeploy is unknown"
            )
        for volume in spec.get("volumes") or []:
            if not isinstance(volume, dict) or volume.get("name") != volume_name:
                continue
            claim = volume.get("persistentVolumeClaim")
            if isinstance(claim, dict):
                return (
                    "sqlite-on-persistentvolumeclaim claim="
                    + _text(claim.get("claimName", ""), MAX_IDENTIFIER),
                    "",
                )
            if isinstance(volume.get("emptyDir"), dict):
                # Scratch. Named as such because this is the case that orphans
                # running machines on a redeploy.
                return "sqlite-on-emptydir (scratch; lost on redeploy)", ""
            return "", (
                f"the volume backing {SKY_STATE_PATH} is neither a "
                "persistentVolumeClaim nor an emptyDir, so its durability is "
                "unclassified"
            )
        return "", f"the volume mounted at {SKY_STATE_PATH} is not declared"

    def _hybrid_node_state(self) -> ExistingStateRecord:
        """Joined nodes that belong to *this* system, correlated three ways.

        The second review reproduced an ``unrelated-managed-node`` -- a node the
        cluster happens to have -- being recorded as state the migration must
        handle, because the previous implementation listed every Kubernetes node.
        Draining someone else's node is a workload-affecting action, so getting this
        wrong is not a cosmetic inaccuracy.

        A node is this system's only when all three authoritative sources agree: a
        SuperplaneNode record names it, that record carries the SSM managed-instance
        handle the node was onboarded with, and the SkyPilot cluster it names is in
        the API server's live cluster list. Nodes no record names are excluded, and
        the count of exclusions is stated so the exclusion is visible rather than
        silent.

        A node a record *does* name but cannot be fully correlated leaves the whole
        class unresolved. A partially correlated node is the ambiguous case -- it may
        be ours with a stale handle, or another system's -- and guessing either way
        is what this class must not do.
        """
        __tracebackhide__ = True
        nodes, nodes_reference = self._kube("nodes")
        records, records_reference = self._kube("superplane-nodes")
        clusters, clusters_reference = self._clusters()
        reference = _Reference.combined(
            nodes_reference, records_reference, clusters_reference
        )
        live_clusters = {
            _text(entry.get("name", ""), MAX_IDENTIFIER) for entry in clusters
        }
        by_node: dict[str, dict] = {}
        for item in records.get("items") or []:
            if not isinstance(item, dict):
                continue
            status = item.get("status") or {}
            if not isinstance(status, dict):
                continue
            name = _text(status.get("k8sNodeName", ""), MAX_IDENTIFIER)
            if name and name != "(empty)":
                by_node[name] = status
        correlated, partial, foreign = [], [], 0
        for item in nodes.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = _text((item.get("metadata") or {}).get("name", ""), MAX_IDENTIFIER)
            if not name or name == "(empty)":
                continue
            status = by_node.get(name)
            if status is None:
                foreign += 1
                continue
            instance = _text(status.get("ssmInstanceId", ""), MAX_IDENTIFIER)
            cluster = _text(status.get("skypilotCluster", ""), MAX_IDENTIFIER)
            missing = []
            if not instance or instance == "(empty)":
                missing.append("no SSM managed-instance handle on its record")
            if not cluster or cluster == "(empty)":
                missing.append("no SkyPilot cluster on its record")
            elif cluster not in live_clusters:
                missing.append(
                    f"its SkyPilot cluster {cluster} is not in the live cluster list"
                )
            if missing:
                partial.append(f"{name}: " + ", ".join(missing))
            else:
                correlated.append(f"node/{name} ssm/{instance} cluster/{cluster}")
        via = (
            "kubectl get nodes in the selected workspace cluster, correlated to "
            "SuperplaneNode records by k8sNodeName and to the live SkyPilot cluster "
            "list by skypilotCluster, with each record's ssmInstanceId; "
            f"{foreign} node(s) named by no SuperplaneNode record were excluded as "
            "not this system's"
        )
        if partial:
            return self._state(
                "joined_eks_hybrid_nodes",
                via,
                (),
                reference,
                unresolved_reason=(
                    "these nodes could not be correlated to this system's records, "
                    "and a partially correlated node must not be presumed either "
                    "way: " + "; ".join(sorted(partial))
                ),
            )
        return self._state(
            "joined_eks_hybrid_nodes", via, tuple(sorted(correlated)), reference
        )

    def _serving_state(self) -> ExistingStateRecord:
        """Services from the authoritative listing, or the class left unresolved."""
        __tracebackhide__ = True
        serving = self.serving_inventory()
        if serving is None:
            # No listing was retained. Previously this class was filled from cluster
            # names, which is how a live service became invisible to the migration.
            _, reference = self._clusters()
            return self._state(
                "skyserve_services",
                "sky serve status from an authorized client; there is no "
                "controller-side inventory to read instead",
                (),
                reference,
                unresolved_reason=(
                    "no retained sky serve status listing was supplied, and serving "
                    "has no controller-side inventory, so neither the services nor "
                    "their absence can be established"
                ),
            )
        return self._state(
            "skyserve_services",
            serving.enumerated_via,
            serving.services,
            _Reference(serving.evidence_reference, serving.evidence_sha256),
        )

    def _state(
        self,
        kind: str,
        enumerated_via: str,
        handles: tuple[str, ...],
        reference: _Reference,
        *,
        unresolved_reason: str = "",
        active_operations: tuple[str, ...] = (),
    ) -> ExistingStateRecord:
        return ExistingStateRecord(
            kind=kind,
            enumerated_via=enumerated_via,
            environment=self._config["environment"],
            revision=self._config["revision"],
            observed_at=datetime.now(timezone.utc),
            evidence_reference=reference.reference,
            evidence_sha256=reference.digest,
            handles=handles,
            # An empty enumeration is a verified-empty finding, never a silence --
            # but only when the class was actually settled.
            empty_verified=not handles and not unresolved_reason,
            unresolved_reason=unresolved_reason,
            active_operations=() if unresolved_reason else active_operations,
            # U19 owns the handover decision; this capture only records its inputs.
            decision="undecided",
        )

    def transport_is_live(self) -> bool:
        """True only when both transports are the reviewed real ones.

        Exact type, so a Protocol-conforming offline double cannot make this
        instance's output publishable as live evidence.
        """
        return (
            type(self._skypilot) is SkyPilotReads
            and type(self._kubectl) is KubectlReads
        )


def build_observer(
    config: dict,
    skypilot: ReadTransport | None = None,
    kubectl: ReadTransport | None = None,
    provider: object | None = None,
) -> LiveBaselineObserver:
    """Build the observer for a validated selection.

    The transports default to the real read-only ones. Injecting doubles is how
    the read and mapping behavior is regressed offline; it cannot produce live
    evidence, because :meth:`LiveBaselineObserver.transport_is_live` checks their
    exact types.

    The provider reader defaults to the registered one for the selected target's
    provider, and stays ``None`` when that provider has no reviewed client. That
    is not a silent degradation: the provider-absence check then reports the gap
    by name and fails, which is the correct answer for a provider whose read
    contract nobody has reviewed.
    """
    __tracebackhide__ = True
    metadata = config["target_metadata"]
    namespace = os.environ.get(NAMESPACE_VARIABLE, "superplane")
    skypilot_namespace = os.environ.get(SKYPILOT_NAMESPACE_VARIABLE, "skypilot")
    if provider is None:
        reader = PROVIDER_READERS.get(str(metadata["provider"]).lower())
        provider = reader(metadata["region"]) if reader is not None else None
    return LiveBaselineObserver(
        config=config,
        skypilot=skypilot or SkyPilotReads(metadata["skypilot_api"]),
        kubectl=kubectl or KubectlReads(namespace, skypilot_namespace),
        store=_RawStore(config["evidence_file"]),
        provider=provider,
    )


# Publishable only as an exact type, and only with live transports.
register_live_observer(LiveBaselineObserver)

# The one provider whose instance-describe contract and credential boundary have
# been reviewed. Registering it here rather than at the class means the registry
# reflects review, not the mere existence of a class.
register_provider_reader("aws", Ec2InstanceReads)
