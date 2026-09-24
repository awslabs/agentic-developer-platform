#!/usr/bin/env python3
"""Bounded, ownership-verified teardown for the Wave 2 fixture (issue #3968, W2-10).

WHAT THIS FIXES
---------------
Root's review found four ways the published ``90-cleanup-ledger.sh`` reported a
successful cleanup without having verified one:

1. **Any queue lookup error counted as absence.** ``get-queue-url`` failing was
   treated as "queue already gone". A denied permission, an expired credential,
   a throttle or a network blip all produce a nonzero exit -- and all were
   recorded as ``absent_confirmed: true``. A fixture queue left live with a
   control listener attached would be reported as cleaned up.

2. **Kubernetes absence was never actually checked.** ``kubectl get`` exits 1 for
   "not found" and also for "cannot reach the API server"; the published code
   read only whether stdout was empty, so an unreachable cluster read as absent.

3. **Deletion was assumed synchronous.** ``delete-queue`` returning success means
   the request was accepted; SQS documents up to 60 seconds before the queue is
   really gone, and a Kubernetes object with a finalizer can persist far longer.
   The published code set ``absent_confirmed: true`` on the delete's exit code.

4. **Authority to delete came from a default.** ``q.get("run_bound", True)``
   defaulted to True, so a queue entry that never proved ownership was deletable,
   and the k8s path ignored ownership entirely. A single hardcoded date string
   was the only thing protecting the probe queue.

HOW ABSENCE IS ESTABLISHED HERE
-------------------------------
Three outcomes, never two:

  ABSENT    a specific not-found signal was observed
            (SQS ``AWS.SimpleQueueService.NonExistentQueue``, or a Kubernetes
            ``NotFound`` reason)
  PRESENT   the resource was read back
  UNKNOWN   anything else -- permission denied, throttled, unreachable

UNKNOWN is a cleanup FAILURE, never absence. Deletion is followed by bounded
re-polling until ABSENT is genuinely observed, and ownership is re-verified
immediately before the delete so a same-name replacement is skipped rather than
destroyed.

This module is pure logic over an injected command runner so every one of those
paths is testable without touching a cloud account.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

# SQS's specific not-found code. Matching this exact string is what separates
# "absent" from "could not tell".
SQS_NOT_FOUND_CODES = (
    "AWS.SimpleQueueService.NonExistentQueue",
    "QueueDoesNotExist",
    "NonExistentQueue",
)

# Kubernetes API path segments per kind. Only the kinds this fixture creates are
# listed: an unknown kind must refuse rather than guess a path, because a wrong
# path would 404 and the 404 must never be read as "the object is gone".
#
# Declared here, above the presence probes, because the plural is needed for two
# jobs and not one: building the delete path, and CHECKING that a NotFound reply is
# about the resource we asked for. The second use is why an unmapped kind can never
# yield ABSENT -- see ``status_identifies``.
K8S_API_PATHS: dict[str, tuple[str, str]] = {
    "Deployment": ("/apis/apps/v1", "deployments"),
    "Job": ("/apis/batch/v1", "jobs"),
    "Service": ("/api/v1", "services"),
    "ConfigMap": ("/api/v1", "configmaps"),
    "Secret": ("/api/v1", "secrets"),
    "Pod": ("/api/v1", "pods"),
    "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies"),
    "Ingress": ("/apis/networking.k8s.io/v1", "ingresses"),
    "ServiceAccount": ("/api/v1", "serviceaccounts"),
}


def k8s_resource_plural(kind: str) -> str | None:
    """The API plural for this kind, or None if the kind is unmapped."""
    mapping = K8S_API_PATHS.get(kind)
    return None if mapping is None else mapping[1]


def k8s_resource_group(kind: str) -> str | None:
    """The API group for this kind ("" for core/legacy), or None if unmapped.

    Derived from the same mapping that supplies the plural, so the two can never
    disagree about which resource a kind means. ``/api/v1`` is the core group,
    which has no name; ``/apis/<group>/<version>`` names it explicitly.
    """
    mapping = K8S_API_PATHS.get(kind)
    if mapping is None:
        return None
    prefix = mapping[0]
    parts = prefix.strip("/").split("/")
    # "api/v1" -> core (""); "apis/apps/v1" -> "apps".
    return "" if parts[0] == "api" else parts[1]


def k8s_resource_path(kind: str, name: str, namespace: str) -> str | None:
    """The namespaced API path for one object, or None if the kind is unmapped."""
    mapping = K8S_API_PATHS.get(kind)
    if mapping is None:
        return None
    prefix, plural = mapping
    return f"{prefix}/namespaces/{namespace}/{plural}/{name}"


# kubectl renders a typed NotFound Status as ONE sentence with a fixed grammar:
#
#     Error from server (NotFound): deployments.apps "my-app" not found
#     Error from server (NotFound): configmaps "my-cm" not found
#
# The resource comes first (plural, optionally `.group`), then the name in double
# quotes, then the literal `not found`. Matching those three parts INDEPENDENTLY is
# what root defeated (5807547817): asking separately "does the plural appear
# anywhere?" and "does the quoted name appear anywhere?" accepts
#
#     Error from server (NotFound): configmaps "deployments-probe" not found
#
# as the absence of `Deployment/deployments-probe`, because the expected plural
# `deployments` is inside the *name*. The parts have to be matched in their
# positions, as one statement, so a word appearing in the wrong slot cannot stand in
# for the same word in the right one.
_K8S_NOT_FOUND_PROSE = re.compile(
    r"""
    (?P<plural>[a-z][a-z0-9]*)          # the resource plural, lower-case
    (?:\.(?P<group>[a-z0-9.\-]+))?      # optional API group suffix, e.g. .apps
    \s+"(?P<name>[^"]*)"                # the object name, in double quotes
    \s+not\ found                       # kubectl's fixed trailer
    """,
    re.VERBOSE,
)


def prose_identifies(text: str, *, plural: str, name: str, group: str | None = None) -> bool:
    """Does kubectl's rendered NotFound sentence name THIS resource and name?

    Parsed as one structured statement rather than as independent substring tests,
    so the plural must occupy the resource slot and the name must occupy the name
    slot. A message may contain several such sentences (stderr from a multi-object
    command); one that identifies our object is enough, and a sentence about some
    other object contributes nothing.

    THE GROUP, WHEN THE SERVER STATES ONE
    -------------------------------------
    The grammar was already parsing the optional ``.group`` suffix and then
    discarding it, which root defeated (5808156074) with an executed reply:

        Error from server (NotFound): deployments.other.example "deployments-probe" not found

    That sentence is about ``deployments`` in the group ``other.example``. It was
    accepted as the absence of ``Deployment`` in ``apps``, but a plural is only
    unique WITHIN a group: a custom resource may share a plural with a built-in and
    be an entirely different resource. So when the server names a group, it must be
    the group we expect.

    kubectl legitimately omits the suffix for core resources (``configmaps "x" not
    found``) and sometimes for others, and an omitted group is not a contradiction --
    it is the server declining to disambiguate a plural it considers unambiguous.
    That formatting stays supported: absent group means "not stated", and the plural
    and name alone decide. Passing ``group=None`` keeps the old behaviour for callers
    that cannot say which group to expect.
    """
    for match in _K8S_NOT_FOUND_PROSE.finditer(text):
        if match.group("plural") != plural or match.group("name") != name:
            continue
        stated = match.group("group")
        if group is not None and stated is not None and stated != group:
            # A different group's same plural and name is a different resource.
            continue
        return True
    return False


def status_identifies(payload: dict, *, kind: str, name: str) -> bool:
    """Does this typed ``Status`` reply concern the exact object we asked about?

    Root's reproduction (5806825628, restated at d92ee87b9): a ``Status`` carrying
    ``code: 404`` with empty or absent ``details`` was accepted as this object's
    absence, and ``delete_k8s_with_uid_precondition`` classified *any* 404 as
    ``not_found``. Both are false-absence generators, and the second is worse than
    it looks: ``not_found`` sets ``confirmed_absent: True`` WITHOUT deleting
    anything, so a 404 from a mistyped path, a pruned API group, a proxy that
    answered before reaching the API server, or an RBAC configuration that hides a
    resource behind 404 would empty the surviving-workload list -- and the policy
    retention added in d92ee87b9 would then remove the NetworkPolicies protecting a
    workload still running. A generic 404 could disarm the very guard that exists to
    make that impossible.

    So identity is required, not inferred, on BOTH axes the server reports:

      * ``details.name`` must equal the name we requested. A 404 about some other
        object is a fact about that object.
      * ``details.kind`` (the API plural, e.g. ``deployments``) must match the kind
        we requested. Names are only unique within a resource; ``configmaps "x" not
        found`` says nothing about ``secrets/x``.

    An unmapped kind returns False: we cannot state which plural to expect, so we
    cannot confirm the reply is about it. That is deliberately unprovable rather
    than optimistically true -- an unverifiable absence must read as UNKNOWN, which
    is a cleanup failure, not a success.
    """
    if payload.get("kind") != "Status" or payload.get("reason") != "NotFound":
        return False
    details = payload.get("details")
    if not isinstance(details, dict) or not details:
        # A reason without details names nothing. Previously this was the branch
        # that let a bare 404 pass.
        return False
    if details.get("name") != name:
        return False
    expected = k8s_resource_plural(kind)
    if expected is None:
        return False
    reported = details.get("kind")
    # `details.kind` is the lowercase plural resource. Require it to be stated:
    # omitting it leaves the resource unidentified, which is the same gap as
    # omitting details entirely.
    return reported == expected


# Kinds that PROVIDE containment rather than consume it. They are created first and
# must be torn down LAST, so no window exists in which a control-enabled workload is
# running with its isolation already removed. Matched by kind rather than by name
# prefix: a name convention is a claim about a resource, whereas the kind is what
# determines whether removing it widens what the fixture can reach.
POLICY_KINDS = frozenset({"NetworkPolicy"})


class Presence(str, Enum):
    ABSENT = "absent"
    PRESENT = "present"
    UNKNOWN = "unknown"


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


def real_runner(argv: Sequence[str]) -> CommandResult:
    """Execute one command. Recognises the uid-preconditioned-delete pseudo-command.

    ``w2-k8s-delete-with-preconditions <api-path> <json-body>`` is not a real
    binary: no kubectl subcommand can send a DELETE request body (see
    ``delete_k8s_with_uid_precondition`` for the measurement that established
    this). It is served here by ``kubectl proxy`` so that the whole cleanup still
    goes through ONE injectable runner and remains fully testable.
    """
    argv = list(argv)
    if argv and argv[0] == "w2-k8s-delete-with-preconditions":
        return _delete_via_proxy(argv[1], argv[2])
    proc = subprocess.run(argv, capture_output=True, text=True)
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def _delete_via_proxy(api_path: str, body: str) -> CommandResult:
    """DELETE `api_path` with `body`, through a short-lived ``kubectl proxy``.

    The proxy inherits the kubeconfig's cluster and credentials, so this
    introduces no second authentication path -- it is the same session, with a
    transport that can carry a request body.

    Bound to 127.0.0.1 on an ephemeral port, and torn down in a ``finally`` so a
    failure cannot leave an unauthenticated local proxy to the cluster running.
    """
    import contextlib
    import socket
    import time
    import urllib.error
    import urllib.request

    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    proxy = subprocess.Popen(
        ["kubectl", "proxy", f"--port={port}", "--address=127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline:
            if proxy.poll() is not None:
                out = (proxy.stdout.read() if proxy.stdout else "") or ""
                return CommandResult(1, "", f"kubectl proxy exited early: {out.strip()[:400]}")
            try:
                with contextlib.closing(socket.create_connection(("127.0.0.1", port), 0.25)):
                    ready = True
                    break
            except OSError:
                time.sleep(0.1)
        if not ready:
            return CommandResult(1, "", "kubectl proxy did not become ready within 15s")

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{api_path}",
            data=body.encode("utf-8"), method="DELETE",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return CommandResult(0, response.read().decode("utf-8", "replace").strip(), "")
        except urllib.error.HTTPError as exc:
            # The body of an error response is the typed Status object, which is
            # what distinguishes 409 Conflict (uid mismatch) from 404 NotFound.
            detail = exc.read().decode("utf-8", "replace").strip()
            return CommandResult(1, detail, f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return CommandResult(1, "", f"proxy request failed: {exc.reason}")
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()


@dataclass
class Sleeper:
    """Injectable delay so the bounded-poll logic is testable without waiting."""

    calls: list[float] = field(default_factory=list)
    real: bool = False

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.real:
            import time

            time.sleep(seconds)


# ---------------------------------------------------------------------------
# presence probes
# ---------------------------------------------------------------------------
def queue_presence(run: Runner, name: str) -> tuple[Presence, str]:
    """Is this SQS queue present, absent, or undetermined?

    Only the documented not-found code means absent. Every other failure is
    UNKNOWN, because "I could not ask" is not "it is gone".
    """
    result = run(["aws", "sqs", "get-queue-url", "--queue-name", name,
                  "--query", "QueueUrl", "--output", "text"])
    if result.returncode == 0 and result.stdout:
        return Presence.PRESENT, result.stdout
    combined = f"{result.stderr} {result.stdout}"
    if any(code in combined for code in SQS_NOT_FOUND_CODES):
        return Presence.ABSENT, ""
    return Presence.UNKNOWN, combined.strip()


def is_k8s_not_found(result: CommandResult, *, kind: str, name: str) -> bool:
    """Did the API SERVER say this specific object does not exist?

    Root's reproduction of the defect this replaces: a substring test for
    ``"not found"`` classified

        Unable to connect to the server: getting credentials:
        exec: executable aws not found

    as Kubernetes absence. The phrase came from the *credential exec plugin*
    failing -- the API server was never reached, so nothing at all was learned
    about the object. Cleanup then reported a live fixture as torn down.

    Two independent conditions must both hold, so no single coincidental phrase
    can produce a false absence:

    1. A ``Status`` object with ``reason: NotFound`` is parsed from the output --
       a typed API reply, not prose. ``kubectl get -o json`` is asked for
       ``--ignore-not-found=false`` errors on stderr, so we also accept the
       canonical server-rendered form ``Error from server (NotFound):``, which is
       kubectl's own rendering of that same typed Status.
    2. The message identifies the object we asked about BY KIND AND NAME. A
       NotFound for some other resource (a missing CRD, a stale namespace, a
       same-named object of a different kind) says nothing about this one.

    Anything else -- credential failure, TLS error, timeout, 403, unreachable
    host -- is not absence.
    """
    combined = f"{result.stderr}\n{result.stdout}".strip()
    if not combined:
        return False

    # Path 1: a real typed Status object (kubectl -o json on some paths, or a raw
    # API response). This is the strongest signal available -- but only when it
    # identifies the object; see status_identifies for why a bare 404 must not.
    for candidate in (result.stdout, result.stderr):
        payload = _parse_json_object(candidate)
        if payload is None:
            continue
        if payload.get("kind") == "Status":
            # A Status was returned, so we have the server's own answer: either it
            # identifies our object or this reply tells us nothing about it. Either
            # way, stop -- falling through to the prose path would let the text of a
            # non-identifying Status be re-matched loosely.
            return status_identifies(payload, kind=kind, name=name)

    # Path 2: kubectl's rendering of that typed Status. The parenthesised
    # "(NotFound)" is emitted only for an actual API StatusError, unlike a bare
    # "not found" which any transport or plugin failure can produce.
    if "Error from server (NotFound)" not in combined:
        return False
    # An unmapped kind has no plural to require, so it cannot be confirmed here.
    plural = k8s_resource_plural(kind)
    if plural is None:
        return False
    # The group comes from the same mapping, so a sentence that names a DIFFERENT
    # group's same plural cannot stand in for this resource (root 5808156074).
    return prose_identifies(
        combined, plural=plural, name=name, group=k8s_resource_group(kind))


def k8s_presence(run: Runner, kind: str, name: str, namespace: str) -> tuple[Presence, str]:
    """Is this Kubernetes object present, absent, or undetermined?

    ``kubectl get`` exits nonzero for NotFound *and* for an unreachable API
    server, an expired credential, a denied permission and a DNS failure. The
    exit code cannot distinguish them, and neither can a substring search of the
    message, so absence is established only from a typed NotFound Status that
    names this object (see ``is_k8s_not_found``).
    """
    result = run(["kubectl", "get", kind, name, "-n", namespace, "-o", "json"])
    if result.returncode == 0 and result.stdout:
        try:
            obj = json.loads(result.stdout)
        except json.JSONDecodeError:
            return Presence.UNKNOWN, "kubectl returned unparseable JSON"
        if not isinstance(obj, dict):
            return Presence.UNKNOWN, "kubectl returned JSON that is not an object"
        # A Status object on a SUCCESSFUL exit is still not the resource. And a
        # NotFound Status only establishes absence if it identifies THIS object --
        # the same requirement as on the failure path, applied here too because a
        # zero exit code makes a non-identifying 404 look even more trustworthy.
        if obj.get("kind") == "Status":
            if status_identifies(obj, kind=kind, name=name):
                return Presence.ABSENT, ""
            return Presence.UNKNOWN, (
                f"kubectl exited 0 with a Status that does not identify {kind}/{name}: "
                f"{result.stdout[:200]}")
        uid = (obj.get("metadata") or {}).get("uid", "")
        if not uid:
            # Present but unidentifiable: ownership cannot be verified, so this
            # must not read as a clean PRESENT that a later uid compare passes.
            return Presence.UNKNOWN, f"{kind}/{name} was read back with no metadata.uid"
        return Presence.PRESENT, uid
    if is_k8s_not_found(result, kind=kind, name=name):
        return Presence.ABSENT, ""
    return Presence.UNKNOWN, f"{result.stderr} {result.stdout}".strip()


def row_presence(run: Runner, table: str, event_id: str, arrived_at: str) -> tuple[Presence, str]:
    """Is this row present? Read with --consistent-read.

    An eventually-consistent read can report an item gone before it is, which
    would confirm a deletion that has not happened.
    """
    key = json.dumps({"event_id": {"S": event_id}, "arrived_at": {"S": arrived_at}})
    result = run(["aws", "dynamodb", "get-item", "--table-name", table,
                  "--key", key, "--consistent-read", "--output", "json"])
    if result.returncode != 0:
        return Presence.UNKNOWN, f"{result.stderr} {result.stdout}".strip()
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return Presence.UNKNOWN, "unparseable get-item response"
    # A successful get with no Item is a definite absence, unlike the error paths.
    return (Presence.PRESENT, "") if payload.get("Item") else (Presence.ABSENT, "")


# ---------------------------------------------------------------------------
# bounded wait for real absence
# ---------------------------------------------------------------------------
def wait_for_absence(
    probe: Callable[[], tuple[Presence, str]],
    *,
    attempts: int = 6,
    delay: float = 5.0,
    sleep: Callable[[float], None] | None = None,
) -> tuple[Presence, str]:
    """Poll until ABSENT is observed, or give up and report what was last seen.

    Deletion is asynchronous: SQS documents up to 60 seconds, and a Kubernetes
    object with a finalizer can outlive its delete call indefinitely. Returning
    the delete call's own exit code as absence -- what the published version did
    -- confirms a state nobody observed.

    A trailing PRESENT or UNKNOWN is returned as-is so the caller records a
    failure rather than a confirmation.
    """
    sleep = sleep or Sleeper(real=True)
    presence, detail = probe()
    for _ in range(max(0, attempts - 1)):
        if presence is Presence.ABSENT:
            return presence, detail
        sleep(delay)
        presence, detail = probe()
    return presence, detail


# ---------------------------------------------------------------------------
# deletion, gated on ownership
# ---------------------------------------------------------------------------
def delete_row(
    run: Runner, table: str, entry: dict, *,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 6, delay: float = 2.0,
) -> dict:
    """Delete one synthetic row by BOTH key halves and confirm with a consistent read."""
    event_id, arrived_at = entry.get("event_id"), entry.get("arrived_at")
    record = {"event_id": event_id, "arrived_at": arrived_at,
              "both_keys_present": bool(event_id and arrived_at)}
    if not record["both_keys_present"]:
        record.update(deleted=False, confirmed_absent=False,
                      error="refusing a partial-key delete: both event_id and arrived_at "
                            "are required, and guessing the other half can match an "
                            "unrelated item")
        return record

    key = json.dumps({"event_id": {"S": event_id}, "arrived_at": {"S": arrived_at}})
    result = run(["aws", "dynamodb", "delete-item", "--table-name", table, "--key", key])
    record["deleted"] = result.returncode == 0
    if result.returncode != 0:
        record.update(confirmed_absent=False,
                      error=f"delete-item failed: {result.stderr or result.stdout}")
        return record

    presence, detail = wait_for_absence(
        lambda: row_presence(run, table, event_id, arrived_at),
        attempts=attempts, delay=delay, sleep=sleep,
    )
    record["confirmed_absent"] = presence is Presence.ABSENT
    if presence is Presence.PRESENT:
        record["error"] = "row still present after delete and bounded re-reads"
    elif presence is Presence.UNKNOWN:
        record["error"] = (f"could not determine whether the row is gone: {detail}. "
                           "Recorded as unverified, NOT as absent.")
    return record


def delete_k8s_with_uid_precondition(
    run: Runner, *, kind: str, name: str, namespace: str, uid: str,
) -> tuple[str, str]:
    """Ask the API SERVER to delete this object only if its uid still matches.

    Returns ``(status, detail)`` where status is one of ``deleted``,
    ``uid_mismatch``, ``not_found``, ``unsupported`` or ``error``.

    WHY THIS IS NOT read-then-delete-by-name
    ----------------------------------------
    The previous revision read the uid, compared it, then ran
    ``kubectl delete <kind> <name>``. Root reproduced the hole: if the object is
    replaced between those two steps, the delete removes the REPLACEMENT -- an
    object this run did not create -- and the confirming re-read then finds
    nothing and reports ``confirmed_absent: true``. The post-delete re-read cannot
    detect the problem it was supposed to catch, because the delete is what caused
    the absence it observes.

    ``DeleteOptions.preconditions.uid`` moves the comparison to the server, where
    it is atomic with the delete: a uid mismatch is rejected with 409 Conflict and
    nothing is deleted.

    TRANSPORT, AND ONE MEASURED DEAD END
    ------------------------------------
    ``kubectl delete --raw <path> -f body.json`` is accepted by kubectl and looks
    like it carries DeleteOptions. It does not. Verified against a local capture
    server: the DELETE arrives with an EMPTY body, so the precondition is silently
    dropped and every delete succeeds unconditionally -- a guard that appears to
    work while enforcing nothing, which is the exact defect class under repair.

    ``kubectl proxy`` plus a DELETE carrying the body does transmit it (verified
    the same way: the uid arrives intact). The proxy also reuses the kubeconfig's
    own transport and credentials, so this adds no new auth path. The Python
    ``kubernetes`` client would be the other option but is not installed, and a
    teardown guard should not depend on a package that may be absent.
    """
    path = k8s_resource_path(kind, name, namespace)
    if path is None:
        return "unsupported", (
            f"no API path is mapped for kind {kind!r}, so a uid-preconditioned delete cannot "
            "be built. Refusing to fall back to delete-by-name, which cannot enforce ownership."
        )

    body = json.dumps({
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {"uid": uid},
        # Foreground propagation so dependents go with the object rather than
        # being orphaned into the namespace after teardown reports success.
        "propagationPolicy": "Foreground",
    })
    result = run(["w2-k8s-delete-with-preconditions", path, body])
    combined = f"{result.stdout}\n{result.stderr}".strip()

    # `not_found` is the ONLY status here that sets confirmed_absent without a
    # delete having happened, so it is held to the same identity standard as a
    # presence probe. A 404 can come from a mistyped API path, a pruned API group, a
    # proxy that answered before reaching the API server, or RBAC that hides a
    # resource behind NotFound -- in every one of those cases the object may still
    # be running, and calling it `not_found` would both report a false teardown and
    # (since d92ee87b9) clear the surviving-workload list that stops the fixture's
    # NetworkPolicies from being removed underneath it. An unidentified 404 is
    # therefore `error`: unverified, which fails the cleanup.
    def classify(payload: dict) -> tuple[str, str] | None:
        if payload.get("kind") != "Status":
            return None
        reason, code = payload.get("reason"), payload.get("code")
        if reason == "Conflict" or code == 409:
            return "uid_mismatch", combined
        if reason == "NotFound" or code == 404:
            if status_identifies(payload, kind=kind, name=name):
                return "not_found", combined
            return "error", (
                f"the server replied NotFound/404 without identifying {kind}/{name}, so it "
                f"is not evidence that this object is gone -- it may not even be about it "
                f"(a wrong path, a pruned API group, or a proxy error all render this way). "
                f"Treating as unverified rather than absent. Detail: {combined}")
        if payload.get("status") == "Failure":
            return "error", combined
        return None

    if result.returncode == 0:
        # The proxy returns the deleted object or a Success Status.
        payload = _parse_json_object(result.stdout)
        if payload is not None:
            verdict = classify(payload)
            if verdict is not None:
                return verdict
        return "deleted", combined

    payload = _parse_json_object(result.stdout) or _parse_json_object(result.stderr)
    if payload is not None and payload.get("kind") == "Status":
        return classify(payload) or ("error", combined)
    return "error", combined or f"delete transport exited {result.returncode}"


def _parse_json_object(text: str | None) -> dict | None:
    payload = (text or "").strip()
    if not payload.startswith("{"):
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def delete_k8s(
    run: Runner, entry: dict, *,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 12, delay: float = 5.0,
) -> dict:
    """Delete one fixture Kubernetes object, gated on its recorded uid.

    The uid precondition is the whole point: if the live object's uid differs from
    the one recorded at creation, the name has been reused by something this run
    did not create, and deleting it would destroy an unrelated resource. The
    comparison is performed BY THE SERVER, atomically with the delete.
    """
    kind, name = entry.get("kind"), entry.get("name")
    namespace, recorded_uid = entry.get("namespace"), entry.get("uid")
    record = {"kind": kind, "name": name, "namespace": namespace,
              "recorded_uid": recorded_uid}

    if not entry.get("delete"):
        record.update(skipped=True, reason="ledger does not mark this object for deletion")
        return record
    if not entry.get("created_by_this_run"):
        record.update(skipped=True, confirmed_absent=False,
                      reason="not created by this run; adoption does not authorise deletion")
        return record
    if not recorded_uid:
        record.update(skipped=True, confirmed_absent=False,
                      reason="no recorded uid, so ownership cannot be verified; refusing "
                             "to delete by name alone")
        return record

    # A pre-delete read is kept for the EVIDENCE record (what was live, and with
    # which uid) -- but it is no longer what authorises the delete. Authority comes
    # from the server-side precondition below, so a replacement appearing after
    # this read cannot be deleted by us.
    presence, live_uid = k8s_presence(run, kind, name, namespace)
    record["observed_before_delete"] = {"presence": presence.value, "uid": live_uid}
    if presence is Presence.ABSENT:
        record.update(deleted=False, confirmed_absent=True,
                      note="already absent before this teardown")
        return record
    if presence is Presence.UNKNOWN:
        record.update(deleted=False, confirmed_absent=False,
                      error=f"could not determine presence: {live_uid}. Recorded as "
                            "unverified, NOT as absent.")
        return record
    if live_uid != recorded_uid:
        record.update(skipped=True, deleted=False, confirmed_absent=False,
                      live_uid=live_uid,
                      error=f"{kind}/{name} now has uid {live_uid!r} but this run created "
                            f"{recorded_uid!r}. The name was reused by a different object; "
                            "refusing to delete something this run did not create.")
        return record

    # The delete itself carries DeleteOptions.preconditions.uid, so the server
    # performs the ownership comparison atomically with the removal. A same-name
    # replacement racing in after the read above is rejected with 409 Conflict and
    # is NOT deleted -- the hole that read-then-delete-by-name left open.
    record["uid_precondition"] = "server-enforced via DeleteOptions.preconditions.uid"
    status, detail = delete_k8s_with_uid_precondition(
        run, kind=kind, name=name, namespace=namespace, uid=recorded_uid)
    record["delete_status"] = status

    if status == "uid_mismatch":
        record.update(deleted=False, confirmed_absent=False, skipped=True,
                      error=f"the server REFUSED this delete: {kind}/{name} no longer has uid "
                            f"{recorded_uid!r}, so the live object was created by something "
                            "else. Nothing was deleted. Detail: " + detail)
        return record
    if status == "not_found":
        record.update(deleted=False, confirmed_absent=True,
                      note="disappeared between the probe and the delete; the server reported "
                           "NotFound for this exact object")
        return record
    if status == "unsupported":
        record.update(deleted=False, confirmed_absent=False, error=detail)
        return record
    if status == "error":
        record.update(deleted=False, confirmed_absent=False,
                      error=f"uid-preconditioned delete failed: {detail}. Recorded as "
                            "unverified; the object may still exist.")
        return record

    record["deleted"] = True
    presence, detail = wait_for_absence(
        lambda: k8s_presence(run, kind, name, namespace),
        attempts=attempts, delay=delay, sleep=sleep,
    )
    record["confirmed_absent"] = presence is Presence.ABSENT
    if presence is Presence.PRESENT:
        record["error"] = (f"{kind}/{name} still present after delete and {attempts} bounded "
                           "re-reads; a finalizer may be holding it. Keep recovery dependencies "
                           "running and retry after the gateway records the terminal outcome.")
    elif presence is Presence.UNKNOWN:
        record["error"] = (f"could not determine whether {kind}/{name} is gone: {detail}. "
                           "Recorded as unverified, NOT as absent.")
    return record


def delete_queue(
    run: Runner, entry: dict, *, run_nonce: str,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 14, delay: float = 5.0,
) -> dict:
    """Delete one dedicated fixture queue, gated on its owner-nonce tag.

    Never purges. A purge on a shared queue would destroy ordinary traffic, and
    the protected probe queue is protected here by the absence of this run's
    nonce on it -- not by a hardcoded name, which only ever covers the one
    resource somebody remembered.
    """
    name = entry.get("name")
    record = {"name": name}

    if not entry.get("delete"):
        record.update(skipped=True, reason="ledger does not mark this queue for deletion")
        return record
    if not entry.get("created_by_this_run"):
        record.update(skipped=True, confirmed_absent=False,
                      reason="not created by this run; CreateQueue is idempotent on a name "
                             "match, so this may be a pre-existing queue")
        return record
    recorded_nonce = entry.get("owner_tag_nonce")
    if not recorded_nonce or recorded_nonce != run_nonce:
        record.update(skipped=True, confirmed_absent=False,
                      error=f"queue owner nonce {recorded_nonce!r} does not match this run's "
                            f"{run_nonce!r}; refusing to delete a queue this run cannot prove "
                            "it created")
        return record

    presence, detail = queue_presence(run, name)
    if presence is Presence.ABSENT:
        record.update(deleted=False, confirmed_absent=True,
                      note="already absent before this teardown")
        return record
    if presence is Presence.UNKNOWN:
        record.update(deleted=False, confirmed_absent=False,
                      error=f"could not determine whether queue {name} exists: {detail}. "
                            "Recorded as unverified, NOT as absent -- a live fixture queue "
                            "reported as cleaned up is the defect this replaces.")
        return record

    url = detail
    # Verify the nonce tag on the LIVE queue before deleting it.
    tag_result = run(["aws", "sqs", "list-queue-tags", "--queue-url", url, "--output", "json"])
    if tag_result.returncode != 0:
        record.update(deleted=False, confirmed_absent=False,
                      error=f"could not read tags for {name}: "
                            f"{tag_result.stderr or tag_result.stdout}. Ownership unverified, "
                            "so not deleting.")
        return record
    try:
        tags = (json.loads(tag_result.stdout or "{}") or {}).get("Tags") or {}
    except json.JSONDecodeError:
        record.update(deleted=False, confirmed_absent=False,
                      error="unparseable list-queue-tags response; ownership unverified")
        return record
    live_nonce = tags.get("adp-w2-nonce")
    record["live_tag_nonce"] = live_nonce
    if live_nonce != run_nonce:
        record.update(skipped=True, deleted=False, confirmed_absent=False,
                      error=f"live queue {name} carries nonce {live_nonce!r}, not this run's "
                            f"{run_nonce!r}. The name was reused, or this is not our queue; "
                            "refusing to delete it.")
        return record

    result = run(["aws", "sqs", "delete-queue", "--queue-url", url])
    record["deleted"] = result.returncode == 0
    if result.returncode != 0:
        record.update(confirmed_absent=False,
                      error=f"delete-queue failed: {result.stderr or result.stdout}")
        return record

    # SQS documents up to 60 seconds before a deleted queue is really gone.
    presence, detail = wait_for_absence(
        lambda: queue_presence(run, name), attempts=attempts, delay=delay, sleep=sleep,
    )
    record["confirmed_absent"] = presence is Presence.ABSENT
    if presence is Presence.PRESENT:
        record["error"] = (f"queue {name} still resolvable after delete and {attempts} bounded "
                           "re-reads")
    elif presence is Presence.UNKNOWN:
        record["error"] = (f"could not confirm queue {name} is gone: {detail}. Recorded as "
                           "unverified, NOT as absent.")
    return record


# ---------------------------------------------------------------------------
# the whole teardown
# ---------------------------------------------------------------------------
def run_cleanup(
    ledger: dict, *, table: str, run: Runner, dry_run: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Tear down everything the ledger records, and report honestly.

    ``cleanup_ok`` is True only when every item reached a CONFIRMED absence.
    An item whose state could not be determined makes this False -- the
    evaluation would rather see a failed cleanup than a false one.
    """
    outcome = {
        "dry_run": dry_run,
        "table": table,
        "run_id": ledger.get("run_id"),
        "run_nonce": ledger.get("run_nonce"),
        "rows": [],
        "k8s": [],
        "queues": [],
        # A dry run deletes nothing and therefore VERIFIES nothing. Reporting
        # `true` here would let a plan-only invocation be filed as a cleanup
        # proof, which is the same class of defect as treating an error as
        # absence. null means "not established".
        "cleanup_ok": None if dry_run else True,
        "unverified": [],
    }
    nonce = ledger.get("run_nonce") or ""

    # TEARDOWN ORDER IS A SAFETY PROPERTY, NOT BOOKKEEPING.
    #
    # The fixture is created policies-FIRST, so there is never a window in which it
    # is reachable but unprotected. Walking the ledger in creation order at teardown
    # inverts that: it removes the NetworkPolicies while the control-enabled
    # workload is still running, leaving exactly the window creation was careful to
    # avoid -- an unrestricted control listener, in the environment the whole
    # evaluation exists to keep contained.
    #
    # So workloads are removed first and their absence is confirmed before any
    # policy is touched. This is also what #5825's teardown contract requires
    # ("the control-enabled workload must be observed gone BEFORE the fixture's
    # NetworkPolicies are removed"), but it would be right regardless: the ordering
    # is what makes the window impossible, not what makes the artifact validate.
    entries = list(ledger.get("k8s", []))
    workloads = [e for e in entries if e.get("kind") not in POLICY_KINDS]
    # Accepted abort pods may be retained by a finalizer until the gateway records
    # actual exit and releases reservations. Keep recovery dependencies alive while
    # UID-guarded deletion and bounded polling drain every owned Pod/Job.
    workers = [e for e in workloads if e.get("kind") in {"Pod", "Job"}]
    dependencies = [e for e in workloads if e.get("kind") not in {"Pod", "Job"}]
    workloads = workers + dependencies
    worker_survivors = []
    policies = [e for e in entries if e.get("kind") in POLICY_KINDS]

    for entry in workloads:
        if dry_run:
            outcome["k8s"].append({**entry, "action": "would delete (uid-gated)"})
            continue
        if entry in dependencies and worker_survivors:
            outcome["k8s"].append({
                **entry, "skipped": True, "deleted": False, "confirmed_absent": False,
                "reason": "retained for abort recovery: owned workers have not drained; "
                          "keep gateway, authority, keys and permissions available",
            })
            continue
        record = delete_k8s(run, entry, sleep=sleep)
        outcome["k8s"].append(record)
        if entry in workers and record.get("confirmed_absent") is not True:
            worker_survivors.append(record)

    # If a workload could not be confirmed gone, the policies protecting it are NOT
    # deleted -- not merely reported afterwards. Removing them would strip
    # containment from something still running, turning a failed teardown into an
    # exposure. The retention correctly fails cleanup_ok below: a fixture that could
    # not be fully removed is not a clean one, and saying so is the entire point.
    surviving = [r for r in outcome["k8s"] if r.get("confirmed_absent") is not True]
    blocked = ", ".join(f"{r.get('kind')}/{r.get('name')}" for r in surviving)

    for entry in policies:
        if dry_run:
            outcome["k8s"].append({**entry, "action": "would delete (uid-gated, after workloads)"})
            continue
        if surviving:
            outcome["k8s"].append({
                "kind": entry.get("kind"), "name": entry.get("name"),
                "namespace": entry.get("namespace"), "recorded_uid": entry.get("uid"),
                "skipped": True, "deleted": False, "confirmed_absent": False,
                "reason": f"retained deliberately: {blocked} could not be confirmed gone, so "
                          "removing the isolation protecting it would leave a control-enabled "
                          "workload running unrestricted. Remove the workload, then re-run.",
            })
            continue
        outcome["k8s"].append(delete_k8s(run, entry, sleep=sleep))

    for entry in ledger.get("synthetic_rows", []):
        if dry_run:
            outcome["rows"].append({**entry, "action": "would delete (both keys, after workers)"})
        elif worker_survivors:
            outcome["rows"].append({
                **entry, "skipped": True, "deleted": False, "confirmed_absent": False,
                "reason": "retained for abort recovery until owned workers drain",
            })
        else:
            outcome["rows"].append(delete_row(run, table, entry, sleep=sleep))

    for entry in ledger.get("queues", []):
        if dry_run:
            outcome["queues"].append({**entry, "action": "would delete (nonce-gated)"})
            continue
        if worker_survivors:
            outcome["queues"].append({
                **entry, "skipped": True, "deleted": False, "confirmed_absent": False,
                "reason": "retained for abort recovery until owned workers drain",
            })
            continue
        outcome["queues"].append(delete_queue(run, entry, run_nonce=nonce, sleep=sleep))

    if not dry_run:
        for bucket in ("rows", "k8s", "queues"):
            for record in outcome[bucket]:
                # A SKIP IS NOT A CLEANUP. The previous revision let any entry
                # marked `skipped` with no explicit confirmed_absent pass, so a
                # retained resource -- including one skipped because ownership
                # could not be proven -- counted toward a successful teardown.
                # Root's point: "if intentionally retained items exist they cannot
                # count as full fixture cleanup." Absence is the only thing that
                # discharges a ledger entry; everything else is reported.
                if record.get("confirmed_absent") is True:
                    continue
                outcome["cleanup_ok"] = False
                outcome["unverified"].append({
                    "bucket": bucket,
                    "id": record.get("name") or record.get("event_id"),
                    "skipped": bool(record.get("skipped")),
                    "reason": record.get("error") or record.get("reason")
                              or "absence not confirmed",
                })
    # Counted for the caller so a shell wrapper cannot describe a partial teardown
    # as a complete one without contradicting the record it just wrote.
    outcome["retained"] = [item for item in outcome["unverified"] if item["skipped"]]
    return outcome


# ---------------------------------------------------------------------------
# CLI — 90-cleanup-ledger.sh calls this
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, ownership-verified W2 teardown")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--table", default="adp-dev-webhook-events")
    parser.add_argument("--run-id", required=True,
                        help="refuse a ledger belonging to a different run")
    parser.add_argument("--account-id", required=True,
                        help="refuse a ledger written against a different account")
    parser.add_argument("--generated-at", required=True,
                        help="ISO8601 UTC stamp, supplied by the caller so this "
                             "output is reproducible")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ownership  # noqa: PLC0415 -- same-directory sibling

    try:
        ledger = ownership.load_ledger(
            Path(args.ledger), run_id=args.run_id, account_id=args.account_id)
    except (ValueError, FileNotFoundError) as exc:
        # A ledger that cannot be validated must NOT fall through to "nothing to
        # clean up": that would report a successful teardown for a run whose
        # resources are still live.
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        datetime.datetime.strptime(args.generated_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        print(f"FAIL: --generated-at {args.generated_at!r} is not YYYY-MM-DDTHH:MM:SSZ",
              file=sys.stderr)
        return 1

    outcome = run_cleanup(ledger, table=args.table, run=real_runner, dry_run=args.dry_run)
    outcome["generated_at"] = args.generated_at
    outcome["ledger_version"] = ledger.get("ledger_version")

    dest = Path(args.evidence_dir)
    dest.mkdir(parents=True, exist_ok=True)
    result_path = dest / "cleanup-ledger-result.json"
    result_path.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")

    print(json.dumps({
        "cleanup_ok": outcome["cleanup_ok"],
        "written": str(result_path),
        "rows": len(outcome["rows"]),
        "k8s": len(outcome["k8s"]),
        "queues": len(outcome["queues"]),
        "unverified": outcome["unverified"],
    }, indent=2))
    if args.dry_run:
        print("\nDRY RUN: nothing was deleted, so cleanup_ok is null rather than true. "
              "This\noutput is a plan, NOT a cleanup proof.", file=sys.stderr)
        return 0
    if not outcome["cleanup_ok"]:
        print("\nCLEANUP DID NOT VERIFY. The items above are in an UNKNOWN or "
              "still-present state.\nDP-INV-1 requires the fixture control listener be "
              "gone; check each item by hand\nbefore reporting this run cleaned up.",
              file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
