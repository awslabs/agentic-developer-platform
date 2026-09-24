#!/usr/bin/env python3
"""Superplane workspace and provider onboarding, as `adp superplane onboarding ...`.

WHY THIS IS A SEPARATE FILE AND NOT MORE OF adp-superplane.py
------------------------------------------------------------
`adp-superplane.py` is the *operational* surface: run work in a workspace that
already exists. This is the *onboarding* surface: discover what the environment
can do, review a plan before spending money, bind a provider credential, and
resolve a submission whose reply was lost. They have different failure modes and
different audiences, and the onboarding half is under concurrent change by the
CLI-transport work. Keeping it in its own file means neither body of work has to
land on top of the other's rewrite of the same functions.

Registration is one delegation in `adp-superplane.py`'s `main()`, the same shape
`adp-admin.py` uses for its sub-areas.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
----------------------------------------
No path is invented here. Every request is declared in ENDPOINTS below, which
mirrors `modules/domain-apps/superplane/ui/contract.ts` field for field so the
browser and the CLI cannot drift into two different ideas of the API. That
mirroring is not a convention kept by review — `tests/cli/test_superplane_onboarding.py`
parses both files plus the gateway's own route allowlist and fails on any
disagreement, in either direction.

The reason it matters: a Superplane request reaches the domain API through the
ADP gateway's domain proxy, which forwards only method/path pairs in its
allowlist and 404s everything else. A CLI posting to a plausible-looking path
that is not on that list gets a 404 indistinguishable from "that workspace does
not exist", which sends the operator hunting for a missing resource instead of a
missing route. So an endpoint the proxy does not serve is reported as
*unavailable* without a request being sent at all.

ROUTE AVAILABILITY AND DEPLOYMENT CAPABILITIES
--------------------------------------------
All mapped onboarding routes are mounted, allowlisted and inventoried by the
composed API. Route availability alone does not confirm the deployment's
capabilities or authorize submission: the reviewed plan, advertised operation
identity and server approval checks still apply. Missing support exits 4
(`unavailable`); a failed request exits 5 (`failed`).

SECRETS
-------
No provider secret passes through this file. Onboarding binds a credential
*reference* — an id the ADP vault already holds the value for. There is no flag
that takes a value, credential-shaped flags are refused before argparse sees
them, and every request body and every byte written to the state file is checked
against SECRET_MATERIAL_FIELDS on the way out. That check is a tripwire, not a
sanitizer: it raises rather than redacting, because a redacted leak is a leak
that shipped.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

Api = common.Api
CliError = common.CliError
superplane = common.load_provider("adp-superplane.py")

NAME = "superplane onboarding"

# Relative to the `/api` that adp_common.gateway_url() already appends, matching
# adp-superplane.py. `DOMAIN_BASE` in contract.ts is the same string.
API_BASE = "/superplane/v1"

# Its own state file, NOT a new key inside adp-superplane.py's. Two files cannot
# collide, which is what keeps this module's receipts independent of the
# operational helper's recovery records.
STATE = "superplane_onboarding"

# ---------------------------------------------------------------------------
# The endpoint table. Mirrors contract.ts; see the module docstring.
# ---------------------------------------------------------------------------

# `served` records whether the method/path pair is in the gateway's domain-proxy
# allowlist at this revision. `capability` is prose for a human, in the user's
# terms — never a story or ticket number, because "blocked on #1234" tells the
# person in front of the terminal nothing they can act on. The stable
# machine-readable pair is the error `code` and the `endpoint` name.
ENDPOINTS = {
    "listWorkspaces": {"method": "GET", "path": "/workspaces", "served": True},
    "getWorkspace": {"method": "GET", "path": "/workspaces/{workspace_id}", "served": True},
    "createWorkspace": {"method": "POST", "path": "/workspaces", "served": True},
    # Shared route vocabulary for the browser's operational surface. A served
    # route does not install a CLI command or establish workload readiness.
    "cancelBatchJob": {"method": "POST", "path": "/workspaces/{workspace_id}/batch-jobs/{job_id}/cancellation", "served": True},
    "cancelDeployment": {"method": "POST", "path": "/workspaces/{workspace_id}/deployments/{dep_id}/cancellation", "served": True},
    "batchProfiles": {"method": "GET", "path": "/workspaces/{workspace_id}/batch-profiles", "served": True},
    "listBatchJobs": {"method": "GET", "path": "/workspaces/{workspace_id}/batch-jobs", "served": True},
    "previewBatchJob": {"method": "POST", "path": "/workspaces/{workspace_id}/batch-jobs/preview", "served": True},
    "createBatchJob": {"method": "POST", "path": "/workspaces/{workspace_id}/batch-jobs", "served": True},
    "previewBatchTeardown": {"method": "POST", "path": "/workspaces/{workspace_id}/batch-jobs/{job_id}/teardown-preview", "served": True},
    "deleteBatchJob": {"method": "DELETE", "path": "/workspaces/{workspace_id}/batch-jobs/{job_id}", "served": True},
    "listDeployments": {"method": "GET", "path": "/workspaces/{workspace_id}/deployments", "served": True},
    "servingProfiles": {"method": "GET", "path": "/workspaces/{workspace_id}/deployment-profiles", "served": True},
    "previewDeployment": {"method": "POST", "path": "/workspaces/{workspace_id}/deployments/preview", "served": True},
    "createDeployment": {"method": "POST", "path": "/workspaces/{workspace_id}/deployments", "served": True},
    "previewDeploymentTeardown": {
        "method": "POST",
        "path": "/workspaces/{workspace_id}/deployments/{dep_id}/teardown-preview",
        "served": True,
    },
    "deleteDeployment": {"method": "DELETE", "path": "/workspaces/{workspace_id}/deployments/{dep_id}", "served": True},
    "registerConnection": {
        "method": "POST",
        "path": "/workspaces/{workspace_id}/provider-connections",
        "served": True,
    },
    "getConnection": {
        "method": "GET",
        "path": "/workspaces/{workspace_id}/provider-connections/{connection_id}",
        "served": True,
    },
    "validateConnection": {
        "method": "POST",
        "path": "/workspaces/{workspace_id}/provider-connections/{connection_id}/validation",
        "served": True,
    },
    "revokeConnection": {
        "method": "DELETE",
        "path": "/workspaces/{workspace_id}/provider-connections/{connection_id}",
        "served": True,
    },
    "listCredentials": {"method": "GET", "path": "/vault/credentials", "served": True},
    "listLifecycleProposals": {
        "method": "GET",
        "path": "/workspaces/{workspace_id}/lifecycle-proposals",
        "served": True,
        "capability": "listing the next workspace lifecycle plan",
    },
    "previewLifecycleProposal": {
        "method": "POST",
        "path": "/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview",
        "served": True,
        "capability": "reviewing the next recorded workspace plan",
    },
    "continueLifecycleProposal": {
        "method": "POST",
        "path": "/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/continue",
        "served": True,
        "capability": "continuing an approved workspace lifecycle plan",
    },
    "requestApproval": {
        "method": "POST",
        "path": "/operation-approvals",
        "served": True,
        "capability": "requesting approval for a reviewed operation",
    },
    "getApproval": {
        "method": "GET",
        "path": "/operation-approvals/{approval_id}",
        "served": True,
        "capability": "reading a requested operation approval",
    },
    "decideApproval": {
        "method": "POST",
        "path": "/operation-approvals/{approval_id}/decision",
        "served": True,
        "capability": "deciding an operation approval",
    },
    "adoptWorkspace": {
        "method": "POST",
        "path": "/workspaces/adopt",
        "served": True,
        "capability": "adopting an existing cluster you already operate",
    },
    "capabilities": {
        "method": "GET",
        "path": "/capabilities",
        "served": True,
        "capability": "reporting which workspace features and providers this environment supports",
    },
    "previewWorkspace": {
        "method": "POST",
        "path": "/workspaces/preview",
        "served": True,
        "capability": "reviewing the exact plan, capacity and cost before anything is created",
    },
    "getOperation": {
        "method": "GET",
        "path": "/operations/{operation_id}",
        "served": True,
        "capability": "tracking a submitted operation through to completion",
    },
    "recoverOperation": {
        "method": "GET",
        "path": "/operations/by-idempotency/{idempotency_key}",
        "served": True,
        "capability": "recovering the result of a submission whose reply was lost",
    },
}

# The feature name the server must advertise before a create may carry a
# client-chosen operation identity.
#
# WHY A CREATE IS REFUSED WITHOUT IT, RATHER THAN SENT HOPEFULLY
# -------------------------------------------------------------
# `POST /workspaces` validates its body with a model that has no
# operation-identity field, and unknown fields are ignored by default. A client
# that sends one therefore gets a 201 and *believes* the submission was
# idempotent while the server deduplicated nothing. The first lost reply then
# produces a retry that builds a second workspace and spends twice — arrived at
# through a request that looked entirely successful. Silently-ignored is the
# worst case, so the server has to say it honours the identity first.
#
# `adp-superplane.py` gates its own creates on the same string, so the two
# clients cannot disagree about when a create is safe.
CREATE_IDEMPOTENCY_FEATURE = "create-operation-id-v1"

# The identity travels in the BODY. A header cannot work: the gateway's domain
# proxy rebuilds the upstream request with exactly `Authorization` and
# `Content-Type` and forwards nothing else, so an `Idempotency-Key` header would
# be dropped in transit with no error — the request would succeed, deduplicate
# nothing, and report success.
OPERATION_ID_FIELD = "operation_id"
IDEMPOTENCY_TRANSPORT = "body"

# Field names that would mean secret material rather than a reference. Mirrors
# contract.ts's list so both clients test against one definition of "secret"
# instead of each inventing its own and missing a case.
SECRET_MATERIAL_FIELDS = (
    "secret",
    "secret_value",
    "secretValue",
    "password",
    "token",
    "access_token",
    "accessToken",
    "api_key",
    "apiKey",
    "private_key",
    "privateKey",
    "credential_value",
    "credentialValue",
    "aws_secret_access_key",
    "awsSecretAccessKey",
)

# Flags shaped like a credential. Refused before argparse so the error names the
# leak rather than reporting an unrecognized argument.
_CREDENTIAL_OPTION = re.compile(
    r"(?:^|[-_])(?:password|passwd|secret|token)(?:$|[-_])"
    r"|(?:^|[-_])(?:access|api|private)[-_]?key(?:$|[-_])",
    re.IGNORECASE,
)

# The domain API degrades a cluster to `Degraded` after five minutes without a
# heartbeat. Kept equal deliberately: a longer window here would print "fresh"
# beside a server-side `Degraded`.
STALE_AFTER_SECONDS = 5 * 60

CANCELLED = (
    "Cancelled locally. This did NOT cancel work the domain API already accepted and did "
    "NOT release any provider resource — read 'adp superplane onboarding operation show' "
    "and your provider console before assuming anything stopped or stopped billing."
)


def progress(message):
    """Progress and warnings go to stderr, so stdout stays parseable."""
    print(message, file=sys.stderr)


LazyApi = superplane.LazyApi


def segment(value):
    """One path segment. `safe=""` so a value carrying a slash cannot add one.

    A name with a slash in it must not be able to reach a different route than
    the one declared in ENDPOINTS — which is the whole value of declaring them.
    """
    return urllib.parse.quote(value, safe="")


def resolve_path(name, params=None):
    """Fill an endpoint's `{param}` templates, percent-encoding each value."""
    declaration = ENDPOINTS[name]
    params = params or {}

    def substitute(match):
        key = match.group(1)
        value = params.get(key)
        if value in (None, ""):
            raise CliError(f"Missing path parameter {key!r} for {declaration['path']}.", "usage_error", 1)
        return segment(str(value))

    return API_BASE + re.sub(r"\{(\w+)\}", substitute, declaration["path"])


def assert_no_secret_material(value, where):
    """Raise if `value` carries a secret-shaped field name, at any depth.

    A tripwire, not a sanitizer. It is called on every request body and on the
    state file before it is written, so a future change that starts carrying a
    secret through onboarding fails loudly instead of shipping. Redacting would
    hide the defect; the point is that it cannot be introduced unnoticed.
    """

    def walk(node, path):
        if isinstance(node, list | tuple):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        for key, child in node.items():
            if key in SECRET_MATERIAL_FIELDS:
                raise CliError(
                    f"{where} carries a field named {key!r} at {path}. Onboarding carries "
                    "credential references, never secret values. Nothing was sent or stored.",
                    "secret_material_refused",
                    1,
                )
            walk(child, f"{path}.{key}")

    walk(value, where)


def reject_secret_arguments(argv):
    """Refuse a credential-shaped flag without reflecting its value.

    Rejected rather than accepted-and-warned: by the time a warning could print,
    the value is already in the shell's history file and was already visible in
    the process list to every other user on the machine. There is no way to
    un-leak it.

    Onboarding needs no secret at all — it binds an id the vault already holds —
    so unlike `provider add` there is no prompt to fall back to. The flag is
    simply not part of this surface.
    """
    for argument in argv:
        name = argument.split("=", 1)[0]
        if argument.startswith("-") and _CREDENTIAL_OPTION.search(name.lstrip("-")):
            raise CliError(
                "Onboarding accepts no credential values, only references. Store the secret "
                "with 'adp superplane provider add' and bind its id with "
                "'adp superplane onboarding connection bind --credential-id <id>'. Nothing was sent.",
                "secret_in_argv",
                1,
            )


# ---------------------------------------------------------------------------
# Unavailability, reported the same way every time
# ---------------------------------------------------------------------------


def unavailable_for(name):
    """The envelope detail for an action that needs an endpoint nothing serves.

    Leads with the capability, because that is the part the reader can act on:
    they can ask whether their platform intends to enable it. The method and
    path follow for whoever is diagnosing the deployment. `endpoint` and the
    error `code` are the stable machine-readable pair — automation branches on
    those and never on this prose.
    """
    declaration = ENDPOINTS[name]
    capability = declaration.get("capability")
    explanation = (
        f"This environment's Superplane API does not support {capability} yet."
        if capability
        else "This environment's Superplane API does not support this action yet."
    )
    return {
        "reason": "not-deployed",
        "detail": f"{explanation} ({declaration['method']} {API_BASE}{declaration['path']} is not available.)",
        "endpoint": name,
        "capability": capability,
    }


def unserved_endpoints():
    return sorted(name for name, declaration in ENDPOINTS.items() if not declaration["served"])


def require_served(name, command, performed="nothing"):
    """Return an `unavailable` envelope, or None when the endpoint is served.

    Returning rather than raising, because this is not a failure: the
    environment lacks a feature, the request was never sent, and exit 4 is the
    resumable state a script distinguishes from "broken".
    """
    if ENDPOINTS[name]["served"]:
        return None
    detail = dict(unavailable_for(name), performed=performed)
    return common.envelope(
        "unavailable",
        command,
        detail,
        "Ask whether this deployment will enable that capability; nothing was attempted.",
    )


def request(api, name, params=None, body=None):
    """Send one declared request, refusing to send an undeclared one.

    Bodies are tripwired on the way out, so a secret cannot leave through a
    request even if some future caller puts one in the dict.
    """
    if not ENDPOINTS[name]["served"]:
        # Defence in depth behind require_served: reaching here is a bug, and a
        # bug that sends a request is worse than one that raises.
        raise CliError(
            f"{name} is not served by this environment and must not be requested.",
            "endpoint_not_served",
            4,
        )
    if body is not None:
        assert_no_secret_material(body, f"the {name} request body")
    return api.request(ENDPOINTS[name]["method"], resolve_path(name, params), body)


# ---------------------------------------------------------------------------
# Receipts: durable operation identity, scoped to a tenant
# ---------------------------------------------------------------------------


def receipt_scope(explicit_org=None, api=None):
    """The namespace a receipt belongs to.

    Deployment as well as organization: the same organization id in a different
    deployment is a different operation namespace, and a receipt that crossed
    between them would name an operation that does not exist there. A receipt
    read under a different scope is not returned at all — replaying one after an
    organization switch would attach this terminal to another tenant's operation.
    """
    context = superplane.current_recovery_context(api)
    requested = explicit_org or os.environ.get("ADP_ORG")
    if requested and requested != context["tenant"]:
        raise CliError("The requested organization differs from the signed-in organization. Nothing was submitted.", "organization_mismatch", 3)
    return {
        "deployment_id": context["gateway"],
        "org_id": context["tenant"],
    }


def fingerprint(payload):
    """A stable digest of the submission payload.

    Keys are sorted so two payloads differing only in property order — which
    happens whenever a caller rebuilds a dict — fingerprint identically and are
    recognised as the same intent rather than as a conflict.

    Not a security boundary: this detects accidental payload drift between
    retries of one user intent. The server independently binds a submission to
    the plan revision it approved.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = 0x811C9DC5
    for character in canonical:
        digest = ((digest ^ ord(character)) * 0x01000193) & 0xFFFFFFFF
    return format(digest, "08x")


def read_receipts():
    return common.read_state(STATE).get("receipts") or {}


def receipt_key(scope, intent):
    """The state key a receipt is filed under: scope FIRST, then intent.

    The scope has to be in the key, not merely in the record. Keyed by intent
    alone, `create:research` in organization A and `create:research` in
    organization B are the same slot, so B's claim overwrites A's — and A's
    identity, which may be the only record of a paid operation whose reply was
    lost, is gone. The scope check on read then reports A's receipt as absent
    rather than as overwritten, which is the same data loss with a reassuring
    face on it.

    The record still carries its scope as well, and reads still verify it. That
    is deliberate redundancy: the key protects against collision, the embedded
    check protects against a hand-edited or migrated file whose key lies.
    """
    return "{}|{}|{}".format(scope.get("deployment_id", ""), scope.get("org_id", ""), intent)


def write_receipt(scope, intent, receipt):
    # Checked before it reaches the disk, not after: the state file is exactly
    # the kind of place a secret survives a process and gets copied into a bug
    # report.
    assert_no_secret_material(receipt, "the onboarding receipt")
    state = common.read_state(STATE)
    receipts = dict(state.get("receipts") or {})
    key = receipt_key(scope, intent)
    previous = receipts.get(key)
    if isinstance(previous, dict) and previous.get("idempotency_key") != receipt.get("idempotency_key") and previous.get("state") in TERMINAL_STATES:
        # A new completed-name intent must not erase the previous operation's
        # recovery receipt. The active slot still prevents concurrent changes.
        receipts[receipt_key(scope, "history:" + previous["idempotency_key"])] = previous
    receipts[key] = receipt
    state["receipts"] = receipts
    common.write_state(STATE, state)


def same_scope(left, right):
    return left.get("deployment_id") == right.get("deployment_id") and left.get("org_id") == right.get("org_id")


TERMINAL_STATES = ("succeeded", "failed")

# The busy message for the receipt lock. Names what is being protected, because
# "a lock is held" tells the operator nothing about whether it is safe to wait.
CLAIM_BUSY = (
    "Another adp superplane onboarding command is claiming an operation identity for this "
    "organization. Nothing was submitted by this invocation; wait for it to finish and retry."
)


def peek_identity(intent, scope, payload):
    """What a claim WOULD do, without writing anything.

    Needed because the claim now persists. A dry run must not leave a claim on
    disk — the next real run with different inputs would then collide with a
    record for a request nobody ever sent — and a conflict is worth reporting
    *before* the operator is asked to confirm, rather than after. So the read-only
    question and the committing one are separate calls, and only the second writes.

    Unlocked on purpose: this decides nothing and commits nothing. A racing
    command can change the answer between this call and the claim, which is
    precisely why `claim_identity` re-reads under the lock rather than trusting
    what this returned.
    """
    existing = read_receipts().get(receipt_key(scope, intent))
    if not (isinstance(existing, dict) and same_scope(existing.get("scope") or {}, scope)):
        return "new", None
    if existing.get("fingerprint") == fingerprint(payload):
        return "resume", existing
    if existing.get("state") in TERMINAL_STATES:
        return "new", None
    return "conflict", existing


def claim_identity(intent, scope, payload, mint=None, now=None, *, draft=False):
    """The identity to submit under: new, resumed, or refused as a conflict.

    Reusing an identity is only safe while the intent is unchanged. A payload
    edit is a *different* request, and sending it under the old identity asks
    the server to treat two different requests as one — either silently
    returning the first workspace for the second request, or conflicting. So a
    changed payload against a live receipt is reported, not resolved: only the
    caller can say whether they meant to replace a request that may already have
    built something.

    WHY THIS PERSISTS BEFORE RETURNING, AND WHY IT IS LOCKED
    -------------------------------------------------------
    An earlier version decided an identity and left the caller to persist it
    later. Two effects, both bad. First, the decision was a read-modify-write
    with no lock: two commands run together both read no receipt, both mint, and
    the two submissions carry different identities — so a server deduplicating
    faithfully still builds two workspaces, because nobody told it the requests
    were one. Second, an identity that exists only in memory is not a claim at
    all; a concurrent command has nothing to find.

    So the whole read-decide-write runs inside a cross-process lock and the new
    receipt is on disk before this returns. That is also why the caller's own
    `write_receipt` at submit time is no longer load-bearing for safety — it
    records progress, not the claim.
    """
    mint = mint or (lambda: str(uuid.uuid4()))
    print_ = fingerprint(payload)
    with common.state_lock(STATE, CLAIM_BUSY):
        existing = read_receipts().get(receipt_key(scope, intent))

        if isinstance(existing, dict) and same_scope(existing.get("scope") or {}, scope):
            if existing.get("fingerprint") == print_:
                return "resume", existing
            if existing.get("state") not in TERMINAL_STATES and not (draft and existing.get("submission_stage") == "draft"):
                return "conflict", existing
            # The earlier intent finished, so a changed payload is a genuinely
            # new request and a fresh identity is correct.

        receipt = _blank(print_, scope, mint(), now)
        if draft:
            receipt["submission_stage"] = "draft"
        write_receipt(scope, intent, receipt)
        return "new", receipt


def _blank(print_, scope, identity, now=None):
    return {
        "idempotency_key": identity,
        "operation_id": None,
        "fingerprint": print_,
        "scope": scope,
        "created_at": _iso(now),
        # `accepted` rather than a fourth "not yet sent" state: the receipt
        # exists precisely so a submission that may have been accepted is not
        # assumed lost.
        "state": "accepted",
        "workspace_id": None,
    }


def _iso(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))


def record_observation(intent, receipt, state, operation_id=None, workspace_id=None):
    """Write what the server said.

    `unknown` is recorded as `unknown` and is never rewritten to `failed`.
    Collapsing those two is the single most damaging simplification available
    here: reporting a lost reply as a failure invites a resubmission, and the
    operation it would duplicate may have succeeded.

    Locked, and filed under the receipt's own scope rather than an ambient one:
    this runs after a network round trip, so a second command has had ample time
    to touch the same state file, and an unlocked read-modify-write here would
    discard whatever it wrote.
    """
    scope = receipt.get("scope") or {}
    updated = dict(
        receipt,
        state=state,
        operation_id=operation_id or receipt.get("operation_id"),
        workspace_id=workspace_id or receipt.get("workspace_id"),
        observed_at=_iso(),
    )
    with common.state_lock(STATE, CLAIM_BUSY):
        current = read_receipts().get(receipt_key(scope, intent))
        if isinstance(current, dict):
            if current.get("idempotency_key") != receipt.get("idempotency_key"):
                return current
            if current.get("state") in TERMINAL_STATES and state not in TERMINAL_STATES:
                return current
        write_receipt(scope, intent, updated)
    return updated


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def confirm(args, summary):
    """Approve a mutation, or refuse to guess.

    A non-interactive caller that has not passed --yes has stated no intent, and
    choosing an answer on its behalf is exactly what a scripted onboarding must
    not do. `--dry-run` returns the plan instead and changes nothing.
    """
    if getattr(args, "dry_run", False):
        return False
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        raise CliError(
            "This change needs your explicit approval and there is no terminal to ask on. "
            "Re-run with --yes to state that intent, or --dry-run to inspect it first. Nothing was changed.",
            "confirmation_required",
            1,
        )
    progress(summary)
    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        raise CliError("Cancelled; nothing was changed.", "cancelled", 1)
    return True


# ---------------------------------------------------------------------------
# capabilities and readiness
# ---------------------------------------------------------------------------


def parse_capabilities(raw):
    """Read the capability report, fail-closed.

    Every list is filtered to strings rather than taken as-is: a server sending
    `features: [null, "create-operation-id-v1"]` would otherwise put a null into
    a list this program then treats as strings. Unknown modes are dropped for
    the same reason.

    A non-object reply parses to None, which `advertises` reads as "nothing is
    advertised" — so a malformed or ancient response blocks a create rather than
    accidentally permitting one.
    """
    if not isinstance(raw, dict):
        return None
    strings = lambda value: [item for item in value if isinstance(item, str)] if isinstance(value, list) else []  # noqa: E731
    return {
        "features": strings(raw.get("features")),
        "modes": [mode for mode in strings(raw.get("modes")) if mode in ("managed", "adopt")],
        "providers": strings(raw.get("providers")),
    }


def advertises(capabilities, feature):
    """Whether a report advertises a feature. The single place that is decided.

    Fail-closed by construction: a None report — no observation, malformed
    response, or a server with no such concept — is not a feature. Keeping this
    in one function stops a caller reaching for `capabilities["features"]` on a
    None and getting an exception, or worse, inverting a falsy check into
    "supported".
    """
    return bool(capabilities) and feature in (capabilities.get("features") or [])


def capabilities_command(args, api):
    command = "superplane onboarding capabilities"
    blocked = require_served("capabilities", command)
    if blocked:
        # Everything this verb would report is unknown, and the verb says which
        # things those are rather than printing an empty report that reads as
        # "nothing is supported".
        blocked["detail"]["unknown"] = [
            "which lifecycle modes this deployment serves",
            "which providers a connection may be registered against",
            f"whether a create may carry an operation identity ({CREATE_IDEMPOTENCY_FEATURE})",
        ]
        blocked["detail"]["unserved_endpoints"] = unserved_endpoints()
        return blocked

    try:
        report = parse_capabilities(request(api, "capabilities"))
    except CliError as exc:
        if exc.status_code in (401, 403):
            raise
        return common.envelope(
            "unavailable",
            command,
            {
                "reason": "not-deployed" if exc.status_code == 404 else "unreachable",
                "endpoint": "capabilities",
                "capability": ENDPOINTS["capabilities"]["capability"],
                "detail": "The capability report could not be read from this deployment.",
                "unknown": [
                    "which lifecycle modes this deployment serves",
                    "which providers a connection may be registered against",
                    f"whether a create may carry an operation identity ({CREATE_IDEMPOTENCY_FEATURE})",
                ],
            },
        )
    if report is None:
        raise CliError(
            "The capability report could not be read. Treating it as advertising nothing; no create was attempted.",
            "capabilities_unreadable",
            5,
        )
    return common.envelope(
        "ok",
        command,
        dict(report, create_idempotency=advertises(report, CREATE_IDEMPOTENCY_FEATURE)),
    )


def freshness_of(observed_at, now=None):
    """How current an observation is.

    `unknown` is an observation that never happened; `stale` is one that
    happened too long ago. A workspace that has never reported is not the same
    as one that stopped reporting, and only the second is a fault.
    """
    if not observed_at:
        return "unknown"
    text = observed_at.replace("Z", "+00:00")
    try:
        observed = datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return "unknown"
    reference = now if now is not None else time.time()
    return "stale" if reference - observed > STALE_AFTER_SECONDS else "fresh"


def reading(ready, reason, observed_at=None, now=None):
    """One readiness reading.

    `ready` is tri-state. None means "we do not know" — not measured, not
    reachable, or not served here — and must never be presented like False,
    because "not ready" invites a fix and "unknown" invites a check.
    """
    return {
        "ready": ready,
        "reason": reason,
        "freshness": freshness_of(observed_at, now),
        "observed_at": observed_at,
    }


def control_plane_reading():
    """What the governed surface can actually say about the control plane.

    Not much, and that is the honest answer. The domain's `/health` route is not
    in the gateway's proxy allowlist, so neither a browser session nor an `adp`
    session can reach it, and the adapter-capability readings that would justify
    a claim of production capability are on an internal route too. Reporting
    "reachable" from the fact that some other request succeeded would be
    inferring control-plane health from a workspace read.
    """
    return reading(
        None,
        "The control plane's own health endpoint is not reachable through the governed API "
        "surface, so its status has not been observed. A successful workspace read is not "
        "evidence of control-plane health.",
    )


def workspace_reading(workspace, now=None):
    """Workspace readiness from the workspace row.

    Requires BOTH a terminal-successful status AND a fresh healthy cluster
    observation. The two failure modes read differently: a workspace still
    `Provisioning` is a wait, whereas a registered workspace whose cluster has
    gone silent is a fault.

    A stale observation yields None, not True. A cluster that was healthy ten
    minutes ago and has said nothing since is not evidence that it is healthy
    now, and treating it as such is how a "ready" verdict outlives the thing it
    describes.
    """
    if not isinstance(workspace, dict):
        return reading(None, "No workspace was read.")
    status = workspace.get("status")
    heartbeat = workspace.get("last_heartbeat")
    if status not in ("Active", "Ready"):
        return reading(False, f'The workspace is "{status}". It cannot run work in this state.', heartbeat, now)

    freshness = freshness_of(heartbeat, now)
    if freshness != "fresh":
        return reading(
            None,
            "The workspace is registered, but its cluster has not reported recently, so its current ability to run work is unknown."
            if freshness == "stale"
            else "The workspace is registered, but its cluster has never reported, so its ability to run work has not been observed.",
            heartbeat,
            now,
        )

    health = workspace.get("cluster_health")
    healthy = health == "Healthy"
    return reading(
        healthy,
        "The workspace is registered and its cluster reported healthy."
        if healthy
        else f'The workspace is registered but its cluster reported "{health or "nothing"}".',
        heartbeat,
        now,
    )


def provider_reading(validation, admits_new_work, now=None):
    """Provider readiness from the connection's separate validation readings.

    Each failing reading is named, because the operator action differs per
    reading: an invalid credential is re-entered, insufficient permissions are
    widened at the provider, exhausted quota is raised or waited out. A single
    "provider not ready" would send someone looking in the wrong place.

    `observed_capacity` is excluded from the verdict on purpose — the contract
    leaves it null when capacity was not measured, and "we did not look" must
    not read as "there is none".
    """
    if not isinstance(validation, dict):
        return reading(None, "The provider connection has not been validated by the service yet.")

    failures = []
    if validation.get("credential_valid") is False:
        failures.append("the credential is not valid")
    if validation.get("permissions_sufficient") is False:
        failures.append("its permissions are insufficient")
    if validation.get("quota_available") is False:
        failures.append("no quota is available")
    unmeasured = any(validation.get(field) is None for field in ("credential_valid", "permissions_sufficient", "quota_available"))
    checked_at = validation.get("checked_at")
    if admits_new_work is False:
        return reading(False, "The connection does not admit new work (it may be disabled or revoked).", checked_at, now)
    if freshness_of(checked_at, now) != "fresh":
        return reading(
            None, "Provider readiness is unknown because the observation is stale or unavailable. Request a fresh validation.", checked_at, now
        )

    if failures:
        return reading(False, f"The provider connection cannot admit work: {', '.join(failures)}.", checked_at, now)
    if unmeasured:
        return reading(
            None,
            "The provider validation is incomplete — at least one reading was not taken, so readiness is unknown.",
            checked_at,
            now,
        )
    if admits_new_work is not True:
        return reading(None, "The service did not report whether the connection admits new work; readiness is unknown.", checked_at, now)
    return reading(True, "The service validated the credential, its permissions and its quota.", checked_at, now)


def readiness_command(args, api):
    """Three readings, reported separately, with no aggregate.

    The absence of an overall verdict is the feature, not an omission. Control
    plane health says nothing about whether a given workspace has a reconciling
    controller, a valid credential or any capacity; a single green answer
    computed from it would tell someone they can launch work, and they would.
    There is deliberately no key in this output that a caller could read as
    "execution-ready".

    Partial failure is the normal case during onboarding rather than an
    exception — the control plane answers while a brand-new workspace has never
    reported and no connection exists at all — so each reading is derived from
    whatever was actually obtained and one failed read degrades one reading.
    """
    command = "superplane onboarding readiness"
    workspace = None
    problems = {}
    try:
        workspace = request(api, "getWorkspace", {"workspace_id": args.workspace})
    except CliError as exc:
        if exc.status_code in (401, 403):
            raise
        problems["workspace"] = exc.code

    validation, admits = None, None
    if args.connection_id:
        try:
            connection = request(
                api,
                "getConnection",
                {"workspace_id": args.workspace, "connection_id": args.connection_id},
            )
            if isinstance(connection, dict):
                validation = connection.get("validation")
                admits = connection.get("admits_new_work")
        except CliError as exc:
            if exc.status_code in (401, 403):
                raise
            problems["provider"] = exc.code

    detail = {
        "workspace_id": args.workspace,
        # Three named readings, each with its own reason and its own observation
        # time. No fourth field combining them.
        "control_plane": control_plane_reading(),
        "workspace": workspace_reading(workspace),
        "provider": provider_reading(validation, admits),
    }
    if not args.connection_id:
        detail["provider"]["reason"] = "No provider connection was named, so provider readiness was not assessed. Pass --connection-id to include it."
    if problems:
        detail["unread"] = problems

    # `ok` because the reading itself succeeded. Whether the workspace is ready
    # is in the readings, and a caller that wants to branch on readiness reads
    # them — a non-zero exit here would mean "could not report", which is a
    # different fact from "not ready".
    return common.envelope(
        "ok",
        command,
        detail,
        "Each reading stands alone; control-plane health does not establish that a workspace can run work.",
    )


# ---------------------------------------------------------------------------
# provider connections
# ---------------------------------------------------------------------------


def credential_ref(raw):
    """Read a registry handle joined with canonical Gateway metadata."""
    if not isinstance(raw, dict):
        return None
    values = (raw.get("adp_credential_id"), raw.get("provider"), raw.get("label"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    return dict(zip(("credential_id", "service", "label"), values))


def credential_references(api):
    raw = request(api, "listCredentials")
    rows = raw.get("credentials") if isinstance(raw, dict) else raw
    metadata = api.request("GET", "/auth/credentials")
    if not isinstance(rows, list) or not isinstance(metadata, list):
        raise CliError("The credential metadata response is incomplete.", "invalid_response", 4)
    canonical = {row.get("id"): row for row in metadata if isinstance(row, dict)}
    references = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        vault = canonical.get(row.get("adp_credential_id"))
        if not vault or vault.get("service") != row.get("provider"):
            continue
        ref = credential_ref({"adp_credential_id": row.get("adp_credential_id"), "provider": vault.get("service"), "label": vault.get("label")})
        if ref:
            references.append(ref)
    return references


def connection_credential_ref(raw):
    """Map the `credential` block of a CONNECTION response to a reference.

    Two shapes, two functions, deliberately. A vault row
    (`GET /vault/credentials`) names the fields `adp_credential_id`/`provider`/
    `name`; the block nested in a connection response
    (`emission.connection_response`) already names them
    `credential_id`/`service`/`label`. Reading either with the other's parser
    yields an all-null reference that looks like a credential with no identity.

    Same barrier rule as `credential_ref`: fields are named, never spread, so a
    value the server should not have sent cannot reach the output.
    """
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("credential_id")
    if not isinstance(identifier, str) or not identifier:
        return None
    return {
        "credential_id": identifier,
        "service": raw.get("service") if isinstance(raw.get("service"), str) else "",
        "label": raw.get("label") if isinstance(raw.get("label"), str) else "",
    }


def build_bind_body(ref):
    """The bind body, built from one vetted reference.

    Takes the whole reference rather than loose strings so the three fields cannot
    come from three places and disagree. The server requires all three and
    separately requires `provider` to equal `service`, so assembling this from
    independent flags is a 400 waiting to happen -- and a way to bind one
    credential while labelling it as another.
    """
    return {
        "provider": ref["service"],
        "credential_id": ref["credential_id"],
        "service": ref["service"],
        "label": ref["label"],
    }


def connection_command(args, api):
    if args.subcommand == "credentials":
        refs = credential_references(api)
        return common.envelope(
            "ok",
            "superplane onboarding connection credentials",
            {"credentials": refs, "count": len(refs)},
            "Bind one with 'adp superplane onboarding connection bind --credential-id <id>'. "
            "The id is the credential's vault id (adp_credential_id).",
        )

    if args.subcommand == "bind":
        command = "superplane onboarding connection bind"
        # Resolve the reference from the vault rather than trusting loose flags.
        #
        # The server requires the full three-field reference and requires the
        # connection's provider to equal the credential's own service. `--service`
        # and `--label` as free-text flags would let a caller bind one credential
        # while labelling it as another, and would fail a mismatch check with an
        # error naming neither field. So the id is looked up in the org's own
        # credential list and the body is built from the row the server itself
        # returned.
        progress(f"Resolving credential {args.credential_id} in this organization's vault...")
        refs = credential_references(api)
        ref = next((r for r in refs if r["credential_id"] == args.credential_id), None)
        if ref is None:
            raise CliError(
                f"credential {args.credential_id} is not registered in this organization's "
                "vault. List the available references with 'adp superplane onboarding "
                "connection credentials'; use the credential's vault id, not the registry "
                "row id. Nothing was sent.",
                "credential_not_registered",
                1,
            )
        # An explicit --provider is honoured as an ASSERTION, not an override: it
        # must agree with the credential, because the server refuses a mismatch.
        if args.provider and args.provider != ref["service"]:
            raise CliError(
                f"credential {args.credential_id} is a {ref['service']} credential, not "
                f"{args.provider}. Omit --provider to use the credential's own provider "
                "family. Nothing was sent.",
                "credential_provider_mismatch",
                1,
            )
        body = build_bind_body(ref)
        if not confirm(
            args,
            f"Bind credential {args.credential_id} ({ref['label']}) as the {ref['service']} connection for workspace {args.workspace}.",
        ):
            return common.envelope(
                "ok",
                command,
                {"dry_run": True, "performed": "nothing", "would_send": body},
                "Rerun with --yes to bind.",
            )
        progress(f"Binding {ref['service']} credential to workspace {args.workspace}...")
        api.request("PUT", f"/auth/credentials/{segment(args.credential_id)}/workspaces/{segment(args.workspace)}")
        result = request(api, "registerConnection", {"workspace_id": args.workspace}, body)
        return common.envelope(
            "ok",
            command,
            _connection_detail(result),
            "A successful bind is not a validation: the credential stays unvalidated until the attesting service reports a reading.",
        )

    if args.subcommand == "show":
        # Read the connection, including whatever readings the attesting service
        # has filed. This is the honest counterpart to the unavailable `validate`:
        # a GET cannot cause a check and does not claim to, so every reading it
        # reports was established by the service rather than by this command. A
        # connection nobody has attested reports null readings and unassessed
        # readiness, which is what a fresh bind really looks like.
        command = "superplane onboarding connection show"
        progress(f"Reading connection {args.connection_id}...")
        result = request(api, "getConnection", {"workspace_id": args.workspace, "connection_id": args.connection_id})
        validation = result.get("validation") if isinstance(result, dict) else None
        admits = result.get("admits_new_work") if isinstance(result, dict) else None
        detail = _connection_detail(result)
        detail["readings"] = _validation_detail(validation)
        detail["provider_readiness"] = provider_reading(validation, admits)
        return common.envelope(
            "ok",
            command,
            detail,
            "Readings come from the attesting service. A connection with no readings has not "
            "been checked, which is not the same as having failed a check.",
        )

    if args.subcommand == "validate":
        command = "superplane onboarding connection validate"
        if not confirm(args, f"Validate the credential bound to workspace {args.workspace} using the provider service."):
            return common.envelope("ok", command, {"dry_run": True, "performed": "nothing"}, "Rerun with --yes to validate.")
        params = {"workspace_id": args.workspace, "connection_id": args.connection_id}
        connection = request(api, "getConnection", params)
        ref = connection_credential_ref(connection.get("credential") if isinstance(connection, dict) else None)
        if ref is None:
            raise CliError("The connection has no usable credential reference.", "invalid_response", 4)
        evidence = api.request("POST", f"/auth/credentials/{segment(ref['credential_id'])}/workspaces/{segment(args.workspace)}/validation")
        if not isinstance(evidence, dict) or evidence.get("credential_id") != ref["credential_id"] or evidence.get("workspace_id") != args.workspace:
            raise CliError("The validation evidence does not name this credential and workspace.", "invalid_response", 4)
        report = evidence.get("validation")
        if not isinstance(report, dict) or any(
            not isinstance(report.get(key), bool) for key in ("credential_valid", "permissions_sufficient", "quota_available")
        ):
            raise CliError("The provider returned incomplete validation evidence.", "invalid_response", 4)
        report = {
            key: report.get(key)
            for key in ("credential_valid", "permissions_sufficient", "quota_available", "observed_capacity", "checked_at", "detail")
        }
        result = request(api, "validateConnection", params, report)
        return common.envelope("ok", command, _connection_detail(result))

    command = "superplane onboarding connection revoke"
    if not confirm(
        args,
        f"Revoke connection {args.connection_id} from workspace {args.workspace}. The credential itself stays in your ADP vault.",
    ):
        return common.envelope(
            "ok",
            command,
            {"dry_run": True, "performed": "nothing", "connection_id": args.connection_id},
            "Rerun with --yes to revoke.",
        )
    progress(f"Revoking connection {args.connection_id}...")
    request(api, "revokeConnection", {"workspace_id": args.workspace, "connection_id": args.connection_id})
    # An empty body is still a revoke: adp_common maps a 204 to {}, so success
    # here is not inferred from response content.
    return common.envelope(
        "ok",
        command,
        {"revoked": args.connection_id, "workspace_id": args.workspace, "vault_credential": "untouched"},
    )


def _connection_detail(raw):
    """Named fields only, for the same reason as `credential_ref`."""
    if not isinstance(raw, dict):
        return {"connection": None}
    return {
        "connection_id": raw.get("connection_id"),
        "provider": raw.get("provider"),
        "status": raw.get("status"),
        "workspace_id": raw.get("workspace_id"),
        "credential": connection_credential_ref(raw.get("credential")),
        # Separate facts, deliberately: a connection can exist and admit no work.
        "admits_new_work": raw.get("admits_new_work"),
        "allows_renewal": raw.get("allows_renewal"),
        "limitation": raw.get("limitation"),
    }


def _validation_detail(raw):
    """The readings, kept separate, with null preserved as null.

    The domain emits these four and computes no aggregate on purpose: "the
    credential is valid but its permissions are insufficient" is a different
    operator action from "the credential is invalid". Reducing them to one `ok`
    at this boundary would undo that for every consumer of this JSON.
    """
    if not isinstance(raw, dict):
        return None
    return {
        "credential_valid": raw.get("credential_valid"),
        "permissions_sufficient": raw.get("permissions_sufficient"),
        "quota_available": raw.get("quota_available"),
        # null means not measured, which is not the same as measuring zero.
        "observed_capacity": raw.get("observed_capacity"),
        "checked_at": raw.get("checked_at"),
        "detail": raw.get("detail"),
    }


# ---------------------------------------------------------------------------
# plan, create and adopt
# ---------------------------------------------------------------------------


def onboarding_inputs(args):
    body = {"name": args.name, "isolation_mode": args.isolation, "mode": "adopt" if getattr(args, "cluster", None) else "managed"}
    for key, value in (
        ("account", getattr(args, "account", None)),
        ("region", getattr(args, "region", None)),
        ("cluster_reference", getattr(args, "cluster", None)),
        ("budget_max_daily_usd", getattr(args, "budget_daily", None)),
        ("budget_max_gpus", getattr(args, "budget_gpus", None)),
    ):
        if value is not None:
            body[key] = value
    return body


def onboarding_intent(inputs):
    return f"{'adopt' if inputs.get('mode') == 'adopt' else 'create'}:{inputs['name']}"


def prepared_plan(args, api, inputs):
    scope = receipt_scope(args.org, api)
    intent = onboarding_intent(inputs)
    kind, receipt = claim_identity(intent, scope, inputs, draft=True)
    if kind == "conflict":
        raise CliError("Another request for this workspace is unresolved. Its identity was retained.", "operation_conflict", 4)
    plan = request(api, "previewWorkspace", {}, dict(inputs, operation_id=receipt["idempotency_key"]))
    if not isinstance(plan, dict) or not isinstance(plan.get("revision"), str):
        raise CliError("The preview response has no plan revision.", "invalid_response", 4)
    assert_no_secret_material(plan, "the plan response")
    return scope, intent, receipt, plan


def set_receipt_stage(scope, intent, receipt, stage, approval_id=None):
    with common.state_lock(STATE, CLAIM_BUSY):
        current = read_receipts().get(receipt_key(scope, intent))
        if not isinstance(current, dict) or current.get("idempotency_key") != receipt["idempotency_key"]:
            raise CliError("The pending request changed. Nothing was submitted.", "operation_conflict", 4)
        if current.get("submission_stage") == "submitted" and stage == "approval":
            return current
        current["submission_stage"] = stage
        if approval_id:
            current["approval_id"] = approval_id
        write_receipt(scope, intent, current)
        return current


def plan_command(args, api):
    command = "superplane onboarding plan"
    blocked = require_served("previewWorkspace", command)
    if blocked:
        blocked["detail"]["consequence"] = "Without a reviewable plan, no workspace can be submitted."
        blocked["detail"]["inputs"] = onboarding_inputs(args)
        return blocked
    _, _, receipt, plan = prepared_plan(args, api, onboarding_inputs(args))
    return common.envelope("ok", command, {"plan": plan, "request_id": receipt["idempotency_key"]})


def lifecycle_proposal(raw, workspace_id, artifact_id=None, request_id=None):
    """Validate the saved plan and bind its exact approval body to this request."""
    required = ("artifact_id", "workspace_id", "source_operation_id", "request_revision", "phase", "account_id")
    if (
        not isinstance(raw, dict)
        or raw.get("status") != "awaiting_plan_approval"
        or any(not isinstance(raw.get(key), str) or not raw[key] for key in required)
        or raw["workspace_id"] != workspace_id
        or (artifact_id is not None and raw["artifact_id"] != artifact_id)
    ):
        raise CliError("The lifecycle plan does not match the requested workspace or artifact.", "invalid_response", 4)
    if raw["phase"] == "apply-infrastructure" and any(
        not isinstance(raw.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", raw[key]) for key in ("plan_file_sha256", "plan_json_sha256")
    ):
        raise CliError("The saved infrastructure plan is missing its exact hashes.", "invalid_response", 4)
    if request_id is not None:
        approval = raw.get("approval_request")
        if (
            raw.get("request_id") != request_id
            or not isinstance(raw.get("revision"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", raw["revision"])
            or not isinstance(approval, dict)
            or approval.get("workspace_id") != workspace_id
            or approval.get("idempotency_key") != request_id
            or approval.get("action") != "provision"
            or not isinstance(approval.get("parameters"), dict)
            or any(not isinstance(value, str) for value in approval["parameters"].values())
        ):
            raise CliError("The lifecycle preview does not bind this request to an exact approval.", "invalid_response", 4)
    assert_no_secret_material(raw, "the lifecycle plan")
    visible = {key: raw.get(key) for key in (*required, "status", "target", "plan_file_sha256", "plan_json_sha256", "inventory", "estimate")}
    if request_id is not None:
        visible.update(request_id=request_id, revision=raw["revision"])
        visible["approval_request"] = {key: raw["approval_request"][key] for key in ("workspace_id", "action", "idempotency_key", "parameters")}
    return visible


def lifecycle_approval(raw):
    assert_no_secret_material(raw, "approval")
    return {
        key: raw.get(key)
        for key in (
            "approval_id",
            "workspace_id",
            "plan_digest",
            "envelope",
            "result",
            "expires_at",
            "revoked",
            "can_decide",
        )
    }


def lifecycle_review(plan):
    return fingerprint(
        {
            key: plan.get(key)
            for key in (
                "artifact_id",
                "workspace_id",
                "source_operation_id",
                "request_revision",
                "phase",
                "account_id",
                "target",
                "plan_file_sha256",
                "plan_json_sha256",
                "inventory",
                "estimate",
                "revision",
                "approval_request",
            )
        }
    )


def lifecycle_observation(command, intent, receipt, observed, workspace_id):
    if (
        not isinstance(observed, dict)
        or observed.get("request_id") != receipt["idempotency_key"]
        or observed.get("workspace_id") != workspace_id
        or not isinstance(observed.get("provisioning_operation_id"), str)
        or not observed["provisioning_operation_id"]
    ):
        raise CliError("The continuation response has no matching operation identity. Keep the existing receipt.", "invalid_response", 4)
    assert_no_secret_material(observed, "the lifecycle operation")
    observed = {
        key: observed.get(key) for key in ("request_id", "workspace_id", "provisioning_operation_id", "state", "phase", "observed_at", "retryable")
    }
    state = observed.get("state")
    if state not in ("accepted", "running", "succeeded", "failed"):
        state = "unknown"
    saved = record_observation(intent, receipt, state, observed["provisioning_operation_id"], workspace_id)
    return common.envelope(
        "ok",
        command,
        {
            "operation": observed,
            "receipt": _public_receipt(saved),
            "readiness": "Workspace readiness is checked separately from phase completion.",
        },
    )


def lifecycle_command(args, api):
    command = f"superplane onboarding lifecycle {args.subcommand}"
    endpoint = {
        "list": "listLifecycleProposals",
        "plan": "previewLifecycleProposal",
        "request-approval": "requestApproval",
        "continue": "continueLifecycleProposal",
    }[args.subcommand]
    blocked = require_served(endpoint, command)
    if blocked:
        return blocked
    if args.subcommand == "list":
        result = request(api, endpoint, {"workspace_id": args.workspace})
        if not isinstance(result, dict) or result.get("workspace_id") != args.workspace or not isinstance(result.get("proposals"), list):
            raise CliError("The lifecycle listing does not match this workspace.", "invalid_response", 4)
        plans = [lifecycle_proposal(item, args.workspace) for item in result["proposals"]]
        return common.envelope("ok", command, {"proposals": plans})
    if getattr(args, "dry_run", False):
        return common.envelope(
            "ok", command, {"dry_run": True, "performed": "nothing", "workspace_id": args.workspace, "artifact_id": args.artifact_id}
        )
    scope = receipt_scope(args.org, api)
    inputs = {"workspace_id": args.workspace, "artifact_id": args.artifact_id}
    intent = "lifecycle:" + json.dumps([args.workspace, args.artifact_id], separators=(",", ":"))
    kind, receipt = claim_identity(intent, scope, inputs, draft=True)
    if kind == "conflict":
        raise CliError("Another lifecycle request is unresolved. Keep its receipt.", "operation_conflict", 4)
    # A successful continuation advances the source proposal. Recover directly
    # from the durable request identity even when that proposal has disappeared.
    if receipt.get("submission_stage") == "submitted":
        blocked = require_served("recoverOperation", command)
        if blocked:
            return blocked
        observed = request(api, "recoverOperation", {"idempotency_key": receipt["idempotency_key"]})
        return lifecycle_observation(command, intent, receipt, observed, args.workspace)
    blocked = require_served("previewLifecycleProposal", command)
    if blocked:
        return blocked
    plan = lifecycle_proposal(
        request(api, "previewLifecycleProposal", inputs, {"operation_id": receipt["idempotency_key"]}),
        args.workspace,
        args.artifact_id,
        receipt["idempotency_key"],
    )
    review = lifecycle_review(plan)
    with common.state_lock(STATE, CLAIM_BUSY):
        current = read_receipts().get(receipt_key(scope, intent))
        if (
            not isinstance(current, dict)
            or current.get("idempotency_key") != receipt["idempotency_key"]
            or current.get("submission_stage") == "submitted"
        ):
            raise CliError("The lifecycle request changed during review. Recover its receipt.", "operation_conflict", 4)
        if args.subcommand == "plan" and current.get("submission_stage") == "draft":
            current["lifecycle_review"] = review
            current["workspace_id"] = args.workspace
            write_receipt(scope, intent, current)
        if current.get("lifecycle_review") != review or (args.subcommand != "plan" and plan["revision"] != args.plan_revision):
            raise CliError("The lifecycle plan changed or was not reviewed. Run lifecycle plan before requesting approval.", "plan_changed", 4)
        receipt = current
    if args.subcommand == "plan":
        return common.envelope("ok", command, {"plan": plan, "request_id": receipt["idempotency_key"]})
    if args.subcommand == "request-approval":
        progress(json.dumps(plan, sort_keys=True))
        if not confirm(args, f"Request approval for the displayed phase in workspace {args.workspace}."):
            return common.envelope("ok", command, {"performed": "nothing", "plan": plan})
        receipt = set_receipt_stage(scope, intent, receipt, "approval")
        approval = request(api, "requestApproval", {}, plan["approval_request"])
        if (
            not isinstance(approval, dict)
            or not isinstance(approval.get("approval_id"), str)
            or not approval["approval_id"]
            or approval.get("workspace_id") != args.workspace
            or approval.get("plan_digest") != plan["revision"]
        ):
            raise CliError("The approval reply was incomplete. Keep the same request identity.", "invalid_response", 4)
        approval = lifecycle_approval(approval)
        set_receipt_stage(scope, intent, receipt, "approval", approval["approval_id"])
        return common.envelope("ok", command, {"approval": approval})
    approval_id = args.approval_id or receipt.get("approval_id")
    if not approval_id:
        raise CliError("Request approval for this lifecycle plan before continuing.", "approval_required", 4)
    blocked = require_served("getApproval", command)
    if blocked:
        return blocked
    approval = request(api, "getApproval", {"approval_id": approval_id})
    try:
        expires = datetime.fromisoformat(approval.get("expires_at", "").replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        expires = 0
    if (
        not isinstance(approval, dict)
        or approval.get("approval_id") != approval_id
        or approval.get("workspace_id") != args.workspace
        or approval.get("plan_digest") != plan["revision"]
        or approval.get("result") != "allowed-once"
        or approval.get("revoked") is not False
        or expires <= time.time()
    ):
        raise CliError("This exact lifecycle plan does not have a current approval. Keep its receipt.", "approval_required", 4)
    progress(json.dumps(plan, sort_keys=True))
    if not confirm(args, f"Continue the displayed approved phase in workspace {args.workspace}."):
        return common.envelope("ok", command, {"performed": "nothing", "plan": plan})
    checked = lifecycle_proposal(
        request(api, "previewLifecycleProposal", inputs, {"operation_id": receipt["idempotency_key"]}),
        args.workspace,
        args.artifact_id,
        receipt["idempotency_key"],
    )
    if lifecycle_review(checked) != review:
        raise CliError("The lifecycle plan changed during approval review. Nothing was submitted.", "plan_changed", 4)
    receipt = set_receipt_stage(scope, intent, receipt, "submitted", approval_id)
    try:
        observed = request(api, "continueLifecycleProposal", inputs, {"operation_id": receipt["idempotency_key"], "approval_id": approval_id})
        return lifecycle_observation(command, intent, receipt, observed, args.workspace)
    except CliError as exc:
        record_observation(intent, receipt, "unknown")
        raise CliError(
            f"The continuation outcome is unknown ({exc}). Recover with "
            f"'adp superplane onboarding operation recover --key {receipt['idempotency_key']}'.",
            "operation_unknown",
            4,
        ) from None


def approval_command(args, api):
    command = f"superplane onboarding approval {args.subcommand}"
    endpoint = {"request": "requestApproval", "show": "getApproval", "decide": "decideApproval"}[args.subcommand]
    blocked = require_served(endpoint, command)
    if blocked:
        return blocked
    if args.subcommand == "show":
        approval = request(api, endpoint, {"approval_id": args.approval_id})
    elif args.subcommand == "decide":
        approval = request(api, "getApproval", {"approval_id": args.approval_id})
        if not isinstance(approval, dict) or approval.get("can_decide") is not True:
            raise CliError("Only a selected current approver can decide this operation.", "approval_not_permitted", 3)
        assert_no_secret_material(approval, "approval")
        progress(json.dumps(approval, sort_keys=True))
        if not confirm(args, f"Record {args.result} for approval {args.approval_id} with the displayed plan and limits."):
            return common.envelope("ok", command, {"dry_run": True, "performed": "nothing"})
        approval = request(api, endpoint, {"approval_id": args.approval_id}, {"result": args.result})
    else:
        inputs = onboarding_inputs(args)
        if args.dry_run:
            return common.envelope("ok", command, {"dry_run": True, "performed": "nothing", "inputs": inputs})
        scope, intent, receipt, plan = prepared_plan(args, api, inputs)
        if plan["revision"] != args.plan_revision:
            raise CliError("The reviewed plan revision changed. Review it again before requesting approval.", "plan_changed", 4)
        body = plan.get("approval_request")
        if not isinstance(body, dict):
            raise CliError("The preview did not return an approval request.", "approval_unavailable", 4)
        if not confirm(args, f"Request approval for plan {args.plan_revision} and workspace {args.name}."):
            return common.envelope("ok", command, {"performed": "nothing", "plan": plan})
        receipt = set_receipt_stage(scope, intent, receipt, "approval")
        approval = request(api, endpoint, {}, body)
        if isinstance(approval, dict) and isinstance(approval.get("approval_id"), str):
            set_receipt_stage(scope, intent, receipt, "approval", approval["approval_id"])
    assert_no_secret_material(approval, "approval")
    return common.envelope("ok", command, {"approval": approval})


def submit_under_identity(args, api, *, command, endpoint, intent, inputs, extra, summary, noun):
    """Submit one mutating onboarding request, once, under a durable identity.

    WHY CREATE AND ADOPT SHARE THIS
    -------------------------------
    They differ only in which route they call and what they say. Every property
    that makes either one safe to retry is identical, and the earlier code had it
    in `create` alone: `adopt` peeked at nothing, claimed nothing, asked nobody,
    and ignored `--dry-run`, so the moment its route was served an `adp superplane
    onboarding adopt` would have taken over a cluster the operator already runs —
    with no confirmation, no reviewed plan, and no receipt to recover with if the
    reply was lost. Adoption is not a smaller act than creation: it points the
    platform at infrastructure that already holds someone's work.

    Sharing the path rather than copying it is deliberate. A copy drifts, and a
    guard that exists in one of two branches is the defect this repairs.

    THE ORDER HERE IS THE SAFETY PROPERTY
    -------------------------------------
    1. Refuse unless the server says it honours a submitted operation identity.
    2. Peek, so a conflict is reported and a dry run writes nothing.
    3. Claim the identity and persist the receipt BEFORE the request goes out.
    4. Send, with the identity in the body and the approved plan revision.
    5. Record what came back, treating a lost reply as `unknown`.

    Step 3 before step 4 is what makes a crash mid-request recoverable: the
    receipt is on disk even if the reply never arrives, so a retry reuses the
    same identity instead of building a second workspace. A receipt written after
    a successful reply would be exactly no help in the case it exists for.
    """
    report = None
    if ENDPOINTS["capabilities"]["served"]:
        report = parse_capabilities(request(api, "capabilities"))
    if not advertises(report, CREATE_IDEMPOTENCY_FEATURE):
        # Fail-closed, and `unavailable` rather than `failed`: nothing was sent.
        # A submission without this guarantee looks successful and silently
        # deduplicates nothing, which is worse than not submitting.
        detail = {
            "reason": "not-deployed",
            "performed": "nothing",
            "required_feature": CREATE_IDEMPOTENCY_FEATURE,
            "capability": f"safely retrying {noun} without duplicating it",
            "detail": (
                "This environment cannot confirm that it honours a submitted operation identity, "
                "so a lost reply could not be retried without risking a second workspace. "
                "Nothing was submitted."
            ),
        }
        if not ENDPOINTS["capabilities"]["served"]:
            detail["endpoint"] = "capabilities"
        return common.envelope("unavailable", command, detail, "Nothing was submitted.")

    scope = receipt_scope(args.org, api)
    claimed = dict(inputs, **extra)

    # Inspect without writing so --dry-run cannot reserve an operation. The
    # preview reuses the persisted request UUID and verifies its reviewed revision.
    kind, existing = peek_identity(intent, scope, inputs)
    if kind == "conflict":
        raise CliError(
            "A different submission is already in progress for this workspace name under identity "
            f"{existing.get('idempotency_key')}. Resolve it with "
            "'adp superplane onboarding operation show' before submitting changed inputs, so two "
            "workspaces are not created. Nothing was submitted.",
            "operation_conflict",
            1,
        )

    if not confirm(args, summary):
        return common.envelope(
            "ok",
            command,
            {
                "dry_run": True,
                "performed": "nothing",
                "inputs": claimed,
                # The identity an existing claim already holds, or null. NOT a
                # freshly minted one: printing a key that was never claimed
                # invites the operator to quote it at support for an operation
                # that does not exist, and minting it would make the dry run
                # write.
                "identity": (existing or {}).get("idempotency_key"),
                "would_resume": kind == "resume",
            },
            "Rerun with --yes to submit.",
        )

    scope, intent, receipt, plan = prepared_plan(args, api, inputs)
    if plan["revision"] != args.plan_revision:
        raise CliError("The reviewed plan revision changed. Review it again before submission.", "plan_changed", 4)
    approval_id = getattr(args, "approval_id", None) or receipt.get("approval_id")
    if plan.get("approval_required") is not False:
        if not approval_id:
            raise CliError(
                "This plan requires approval. Request it with onboarding approval request and retain the request identity.", "approval_required", 4
            )
        approval = request(api, "getApproval", {"approval_id": approval_id})
        try:
            expires = datetime.fromisoformat(approval.get("expires_at", "").replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError):
            expires = 0
        if (
            not isinstance(approval, dict)
            or approval.get("result") != "allowed-once"
            or approval.get("revoked") is not False
            or expires <= time.time()
        ):
            raise CliError("The operation does not have a current approval. Its receipt was retained.", "approval_required", 4)
    receipt = set_receipt_stage(scope, intent, receipt, "submitted", approval_id)
    if approval_id:
        claimed["approval_id"] = approval_id
    body = dict(claimed, **{OPERATION_ID_FIELD: receipt["idempotency_key"]})
    progress(f"Submitting {noun} under operation identity {receipt['idempotency_key']}...")
    try:
        result = request(api, endpoint, {}, body)
    except CliError as exc:
        # The submission may have been accepted. `unknown`, never `failed`:
        # guessing failure invites a retry that duplicates, and this identity is
        # exactly what makes a safe retry possible.
        record_observation(intent, receipt, "unknown")
        raise CliError(
            f"The reply to the submission was not received ({exc}). The operation state is UNKNOWN, "
            "not failed — it may have been accepted. Resolve it with "
            f"'adp superplane onboarding operation recover --key {receipt['idempotency_key']}' before "
            "retrying, or retry this exact command, which reuses the same identity.",
            "operation_unknown",
            4,
        ) from None

    return common.envelope("ok", command, submission_result(intent, receipt, result))


def observed_state(raw):
    """The operation state a reply establishes — which is rarely `succeeded`.

    WHY A READABLE REPLY IS NOT A FINISHED OPERATION
    -----------------------------------------------
    `POST /workspaces` answers 201 with `status: "Provisioning"` and then builds
    the cluster asynchronously (`routers/workspaces.py`). Recording that as
    `succeeded` — which this used to do for any reply at all — asserts a terminal
    outcome the server never claimed. The cost is concrete: a receipt marked
    terminal lets a later `create` with edited inputs mint a fresh identity
    instead of reporting the conflict, and it tells the operator their workspace
    is ready while provisioning may still fail.

    So a reply moves the receipt to `accepted` unless the server names a state
    this client can map. The mapping is deliberately narrow and case-folded, and
    anything unrecognised stays `accepted` — non-terminal is the safe default,
    because the only cost of waiting is a poll, while the cost of a wrong
    terminal state is a duplicate or a false assurance.

    The domain's vocabulary is not internally consistent: the router writes
    `Provisioning`, `Teardown` and `Failed`, the model defines `pending`,
    `bootstrapping`, `active` and `drift_detected`, and the proxy admits work
    only on `Active`. That is recorded here as observed rather than tidied — a
    client must not invent a uniformity the server does not have. Only `Failed`
    and the active spellings are claimed as terminal; the rest are a wait.
    """
    status = raw.get("status") if isinstance(raw, dict) else None
    if not isinstance(status, str):
        return "accepted"
    folded = status.strip().lower()
    if folded == "failed":
        return "failed"
    if folded in ("active", "ready", "healthy"):
        return "succeeded"
    # `Provisioning`, `pending`, `bootstrapping`, `reconciling`, `Teardown`,
    # `drift_detected` and anything new: still running, not yet finished.
    return "running"


def submission_result(intent, receipt, result):
    """Record what the reply established, and report it without overstating it."""
    state = result.get("operation_state", observed_state(result)) if isinstance(result, dict) else "unknown"
    if state not in ("accepted", "running", "succeeded", "failed", "unknown"):
        state = "unknown"
    updated = record_observation(
        intent,
        receipt,
        state,
        operation_id=(result or {}).get("provisioning_operation_id") if isinstance(result, dict) else None,
        workspace_id=(result or {}).get("id") if isinstance(result, dict) else None,
    )
    detail = {"workspace": result, "receipt": updated}
    if not state_is_terminal(state):
        # Said in the output, not just left implicit in the state field: the
        # operator's next action differs entirely between "done" and "accepted,
        # still building", and the difference must not have to be inferred.
        detail["still_running"] = True
        detail["next_check"] = f"adp superplane onboarding operation show --key {updated['idempotency_key']}"
    return detail


def state_is_terminal(state):
    return state in TERMINAL_STATES


def create_command(args, api):
    """Create a workspace, bound to a plan the operator reviewed."""
    command = "superplane onboarding create"

    # Nothing can be confirmed against a plan that cannot be produced, so this
    # is refused before an identity is minted or anything is written.
    blocked = require_served("previewWorkspace", command)
    if blocked:
        blocked["detail"]["consequence"] = (
            "A create must be bound to the exact plan you reviewed. This environment cannot produce that plan, so no submission was made."
        )
        return blocked

    inputs = onboarding_inputs(args)
    return submit_under_identity(
        args,
        api,
        command=command,
        endpoint="createWorkspace",
        intent=f"create:{args.name}",
        inputs=inputs,
        extra={"plan_revision": args.plan_revision},
        summary=f"Create workspace {args.name} against plan revision {args.plan_revision}.",
        noun=f"workspace {args.name}",
    )


def adopt_command(args, api):
    """Adopt a cluster the operator already runs — through the same guards.

    WHY THE PLAN IS REQUIRED HERE TOO
    ---------------------------------
    Adoption binds the platform to infrastructure that already exists and
    already holds work. What it will change about that cluster is exactly what
    the operator needs to have read before agreeing, so this is refused unless a
    plan can be produced and is submitted bound to the revision that was
    reviewed — the same rule as `create`, for a stronger reason.
    """
    command = "superplane onboarding adopt"

    blocked = require_served("adoptWorkspace", command)
    if blocked:
        # The implementation exists as a library, not as an HTTP route, so there
        # is nothing for an `adp` invocation to call. Said plainly rather than
        # offering a verb that 404s.
        blocked["detail"]["inputs"] = onboarding_inputs(args)
        return blocked

    plan_blocked = require_served("previewWorkspace", command)
    if plan_blocked:
        plan_blocked["detail"]["consequence"] = (
            "An adoption must be bound to the exact plan you reviewed, because it points the "
            "platform at a cluster that already holds your work. This environment cannot "
            "produce that plan, so no submission was made."
        )
        return plan_blocked

    inputs = onboarding_inputs(args)
    if not inputs.get("cluster_reference"):
        # Refused locally, before anything is claimed or sent: an adoption with
        # no cluster named is not a request the server can interpret, and
        # minting an identity for it would leave a receipt for nothing.
        raise CliError(
            "Adoption needs the cluster you already operate. Pass --cluster with its reference. Nothing was submitted.",
            "usage_error",
            1,
        )

    return submit_under_identity(
        args,
        api,
        command=command,
        endpoint="adoptWorkspace",
        # An unresolved adoption owns the workspace-name slot. A new target can
        # claim it after completion, preserving the earlier receipt in history.
        intent=f"adopt:{args.name}",
        inputs=inputs,
        extra={"plan_revision": args.plan_revision},
        summary=(
            f"Adopt cluster {inputs['cluster_reference']} as workspace {args.name} against plan "
            f"revision {args.plan_revision}. This points ADP at infrastructure you already run."
        ),
        noun=f"adoption of cluster {inputs['cluster_reference']}",
    )


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def operation_command(args, api):
    """Read a receipt, and resolve it against the server when that is possible.

    The local receipt is always reported, even when the server cannot be asked.
    That is the point of persisting it: after a lost reply the identity is the
    only thing standing between a retry and a duplicate workspace, and an
    operator needs to be able to read it back.
    """
    command = f"superplane onboarding operation {args.subcommand}"
    receipts = read_receipts()
    scope = receipt_scope(args.org, api)

    if args.subcommand == "list":
        # Other scopes are not listed. A receipt from another tenant or another
        # deployment names an operation that does not exist in this namespace,
        # and showing it invites acting on it.
        mine = {
            intent: _public_receipt(receipt)
            for intent, receipt in receipts.items()
            if isinstance(receipt, dict) and same_scope(receipt.get("scope") or {}, scope)
        }
        return common.envelope("ok", command, {"receipts": mine, "count": len(mine)})

    wanted = args.key or args.operation_id
    match = next(
        (
            receipt
            for receipt in receipts.values()
            if isinstance(receipt, dict)
            and same_scope(receipt.get("scope") or {}, scope)
            and wanted in (receipt.get("idempotency_key"), receipt.get("operation_id"))
        ),
        None,
    )

    endpoint = "getOperation" if args.operation_id else "recoverOperation"
    blocked = require_served(endpoint, command)
    if blocked:
        if match:
            blocked["detail"]["local_receipt"] = _public_receipt(match)
            blocked["detail"]["state_is"] = match.get("state")
            blocked["detail"]["consequence"] = (
                "The locally recorded state is the only state available. A state of 'unknown' "
                "cannot be resolved here and must not be retried blindly: re-run the original "
                "command, which reuses this identity, rather than submitting a new one."
            )
        else:
            blocked["detail"]["local_receipt"] = None
        return blocked

    params = {"operation_id": wanted} if endpoint == "getOperation" else {"idempotency_key": wanted}
    observed = request(api, endpoint, params)
    if not isinstance(observed, dict) or not isinstance(observed.get("request_id"), str):
        raise CliError("The operation response has no request identity. The receipt was retained.", "invalid_response", 4)
    if match and observed["request_id"] != match["idempotency_key"]:
        raise CliError("The response names a different request. The receipt was retained.", "invalid_response", 4)
    if match:
        state = observed.get("state")
        if state not in ("accepted", "running", "succeeded", "failed"):
            state = "unknown"
        key = next(key for key, receipt in receipts.items() if receipt is match)
        prefix = receipt_key(scope, "")
        match = record_observation(
            key[len(prefix) :], match, state, operation_id=observed.get("provisioning_operation_id"), workspace_id=observed.get("workspace_id")
        )
    return common.envelope("ok", command, {"operation": observed, "local_receipt": _public_receipt(match) if match else None})


def _public_receipt(receipt):
    """The receipt's non-secret identifiers. Named, never spread."""
    return {
        "idempotency_key": receipt.get("idempotency_key"),
        "operation_id": receipt.get("operation_id"),
        "approval_id": receipt.get("approval_id"),
        "submission_stage": receipt.get("submission_stage"),
        "state": receipt.get("state"),
        "workspace_id": receipt.get("workspace_id"),
        "fingerprint": receipt.get("fingerprint"),
        "created_at": receipt.get("created_at"),
        "observed_at": receipt.get("observed_at"),
    }


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parser():
    import argparse

    root = common.Parser(
        prog="adp superplane onboarding",
        description="Discover, plan and bind Superplane workspace onboarding, using your existing ADP login.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    # --json on the leaf, via a shared parent parser, so
    # `... connection bind --json` works. A root-only flag would have to precede
    # the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="Print machine-readable JSON on stdout")
    shared.add_argument("--org", help="Organization the receipt scope belongs to (defaults to ADP_ORG)")

    mutating = argparse.ArgumentParser(add_help=False)
    mutating.add_argument("--yes", action="store_true", help="Approve without a prompt, for scripts")
    mutating.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would happen; change nothing")

    def leaf(subcommands, name, parents=(shared,), **kwargs):
        return subcommands.add_parser(name, parents=list(parents), **kwargs)

    leaf(commands, "capabilities", help="Report which modes, providers and features this environment serves")

    readiness = leaf(commands, "readiness", help="Report control-plane, workspace and provider readiness separately")
    readiness.add_argument("--workspace", required=True, help="Workspace id")
    readiness.add_argument("--connection-id", dest="connection_id", help="Include provider readiness for this connection")

    connection = commands.add_parser("connection", help="Bind, validate and revoke a provider connection")
    connection_subcommands = connection.add_subparsers(dest="subcommand", required=True)
    leaf(connection_subcommands, "credentials", help="List bindable vault credential references")
    bind = leaf(connection_subcommands, "bind", (shared, mutating), help="Bind a vault credential reference to a workspace")
    bind.add_argument("--workspace", required=True)
    # Optional, and checked rather than trusted: the provider family is a property
    # of the credential, and the server refuses a connection whose provider differs
    # from the credential's own service. Supplying it asserts an expectation; it
    # cannot override the vault.
    bind.add_argument(
        "--provider",
        help="Optional: assert the expected provider family, for example bedrock",
    )
    bind.add_argument(
        "--credential-id",
        dest="credential_id",
        required=True,
        help="Vault credential REFERENCE id -- the adp_credential_id, never a secret value",
    )

    show_connection = leaf(
        connection_subcommands,
        "show",
        help="Read a connection and any readings the attesting service has filed",
    )
    show_connection.add_argument("--workspace", required=True)
    show_connection.add_argument("--connection-id", dest="connection_id", required=True)

    validate = leaf(
        connection_subcommands,
        "validate",
        (shared, mutating),
        help="Request independent provider validation for the bound credential",
    )
    validate.add_argument("--workspace", required=True)
    validate.add_argument("--connection-id", dest="connection_id", required=True)
    revoke = leaf(connection_subcommands, "revoke", (shared, mutating), help="Revoke a connection; the vault credential is untouched")
    revoke.add_argument("--workspace", required=True)
    revoke.add_argument("--connection-id", dest="connection_id", required=True)

    def onboarding_arguments(command):
        command.add_argument("--name", required=True)
        command.add_argument("--isolation", default="dedicated", choices=("dedicated", "namespace", "research"))
        command.add_argument("--account", help="Cloud account (the domain requires one for research isolation)")
        command.add_argument("--region")
        command.add_argument("--budget-daily", type=float, dest="budget_daily", help="Daily spend cap in USD")
        command.add_argument("--budget-gpus", type=int, dest="budget_gpus", help="Maximum GPUs")
        return command

    plan = onboarding_arguments(leaf(commands, "plan", help="Review the exact plan, capacity and cost before creating"))
    plan.add_argument("--cluster", help="Cluster reference for an adoption plan")

    create = onboarding_arguments(leaf(commands, "create", (shared, mutating), help="Create a workspace, bound to a reviewed plan"))
    create.add_argument("--approval-id", help="Approval reference for this exact plan")
    create.add_argument(
        "--plan-revision",
        dest="plan_revision",
        required=True,
        help="Revision of the plan you reviewed. Submission is bound to it, so a plan you did not see cannot be submitted.",
    )

    adopt = onboarding_arguments(leaf(commands, "adopt", (shared, mutating), help="Adopt a cluster you already operate (BYOC)"))
    adopt.add_argument("--approval-id", help="Approval reference for this exact plan")
    adopt.add_argument("--cluster", help="Reference of the cluster you already operate")
    adopt.add_argument(
        "--plan-revision",
        dest="plan_revision",
        required=True,
        help=(
            "Revision of the plan you reviewed. An adoption is bound to it, so a plan you did "
            "not see cannot be submitted against a cluster you already run."
        ),
    )

    lifecycle = commands.add_parser("lifecycle", help="Review and continue immutable saved workspace phases")
    lifecycle_commands = lifecycle.add_subparsers(dest="subcommand", required=True)
    for verb in ("list", "plan", "request-approval", "continue"):
        command = leaf(lifecycle_commands, verb, (shared,) if verb in ("list", "plan") else (shared, mutating))
        command.add_argument("--workspace", required=True)
        if verb != "list":
            command.add_argument("--artifact-id", required=True, help="Immutable saved lifecycle plan reference")
        if verb in ("request-approval", "continue"):
            command.add_argument("--plan-revision", required=True)
        if verb == "continue":
            command.add_argument("--approval-id", help="Approval for the exact reviewed phase; defaults to the retained receipt")

    approvals = commands.add_parser("approval", help="Request, read and decide operation approvals")
    approval_commands = approvals.add_subparsers(dest="subcommand", required=True)
    approve_request = onboarding_arguments(leaf(approval_commands, "request", (shared, mutating)))
    approve_request.add_argument("--cluster")
    approve_request.add_argument("--plan-revision", required=True)
    approval_show = leaf(approval_commands, "show")
    approval_show.add_argument("--approval-id", required=True)
    approval_decide = leaf(approval_commands, "decide", (shared, mutating))
    approval_decide.add_argument("--approval-id", required=True)
    approval_decide.add_argument("--result", choices=("allowed-once", "rejected"), required=True)

    operation = commands.add_parser("operation", help="Read and recover durable operation receipts")
    operation_subcommands = operation.add_subparsers(dest="subcommand", required=True)
    leaf(operation_subcommands, "list", help="List this scope's operation receipts")
    show = leaf(operation_subcommands, "show", help="Show one operation's state")
    show.add_argument("--operation-id", dest="operation_id", help="Server operation id")
    show.add_argument("--key", help="Operation identity this client submitted under")
    recover = leaf(operation_subcommands, "recover", help="Resolve a submission whose reply was lost")
    recover.add_argument("--key", required=True, help="Operation identity the submission used")
    recover.add_argument("--operation-id", dest="operation_id", help=argparse.SUPPRESS)

    return root


HANDLERS = {
    "capabilities": capabilities_command,
    "readiness": readiness_command,
    "connection": connection_command,
    "plan": plan_command,
    "create": create_command,
    "adopt": adopt_command,
    "operation": operation_command,
    "approval": approval_command,
    "lifecycle": lifecycle_command,
}


def run(args, api):
    session = api if isinstance(api, superplane.SessionApi) else superplane.SessionApi(api)
    if getattr(args, "org", None) or os.environ.get("ADP_ORG"):
        receipt_scope(getattr(args, "org", None), session)
    return HANDLERS[args.command](args, session)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    command = "superplane onboarding " + (argv[0] if argv and not argv[0].startswith("-") else "")
    try:
        # Before argparse: a credential-shaped flag must be reported as the leak
        # it is, not as an unrecognized argument.
        reject_secret_arguments(argv)
        args = parser().parse_args(argv)
        return common.emit(run(args, superplane.LazyApi()), args.json)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, command, as_json)
    except (KeyboardInterrupt, EOFError):
        return common.report_error(CliError(CANCELLED, "interrupted", 130), command, as_json)


if __name__ == "__main__":
    sys.exit(main())
