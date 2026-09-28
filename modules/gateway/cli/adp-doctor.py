#!/usr/bin/env python3
"""Discover what this deployment supports, and diagnose a failure — Issue #5621.

Two read-only verbs, both of which answer a question the CLI previously could not:

``adp capabilities``  "Can I do this here, and if not, whose problem is it —
                      my CLI, this deployment, my permissions, or a dependency?"
``adp doctor``        "Why did that fail?", answered from bounded reads rather
                      than by re-running the thing that failed.

Three properties are load-bearing and are each asserted by tests:

**Every check is a read.** Neither verb writes configuration, starts work, or
performs paid inference. A diagnostic that changes state to answer a question
stops being a diagnostic — and a user running `doctor` on a broken production
deployment must be able to do so without wondering what it touched. Remediation
is therefore never implicit: the commands report a cause and, where one exists,
the exact command that would fix it, and then stop.

**Discovery narrows, it never authorizes.** A definitive "this cannot work" is
used to refuse a mutation BEFORE sending it, so a doomed write is not attempted.
Nothing here ever grants: the server authorizes every request that is sent, and
absent or stale evidence produces "unknown" — never a fall back to an older code
path, which is how a CLI ends up sending a mutation the user's own tooling
believed was unavailable.

**Four states, kept apart.** `supported` (both sides implement it), `enabled`
(the deployment switched the module on), `permitted` (this caller may act) and
`ready` (dependencies are usable) are independent. `src/cli_capabilities/contract.py`
explains at length why collapsing any two produces confidently wrong answers;
this helper's job is to preserve that distinction all the way to the user's
screen, including rendering "unknown" as unknown rather than as a failure.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

CliError = common.CliError
Parser = common.Parser

COMMAND = "doctor"
CAPABILITIES_COMMAND = "capabilities"
CAPABILITIES_PATH = "/me/cli-capabilities"

# The contract versions this CLI can read. A server answering anything else is
# reported as a version mismatch rather than parsed hopefully: reading an unknown
# schema is how a client invents an answer the server never gave.
SUPPORTED_SCHEMA_VERSIONS = ("2026-09-21",)

# Three-valued answers, mirroring the server contract. "unknown" is a real value
# and must never be rendered, compared or cached as if it were "no".
YES, NO, UNKNOWN = "yes", "no", "unknown"

# How long a cached capability document may be used. Short on purpose: the answer
# depends on deployment flags and the caller's permissions, both of which change
# without warning, and a stale "you may do this" is worse than no answer at all.
# The cache exists to stop every command paying a round trip, not to survive a
# permission change.
CACHE_TTL_SECONDS = 300
CACHE_STATE_NAME = "capabilities"

# Stable error subcodes. A script distinguishes these; they do NOT remap any
# established exit code. Each maps to one of the CLI contract's existing exits:
# 2 auth, 3 permission, 4 pending/unavailable, 5 failed.
SUBCODES = {
    "unsupported_operation": 5,
    "feature_disabled": 5,
    "permission_denied": 3,
    "stale_revision": 4,
    "budget_exhausted": 4,
    "dependency_pending": 4,
    "request_timeout": 4,
    "unknown_mutation_outcome": 4,
    "capability_unknown": 4,
    "schema_unsupported": 5,
}

SUBCODE_MESSAGES = {
    "unsupported_operation": (
        "This ADP deployment does not offer that operation. Run `adp update` to get a "
        "newer CLI, or ask your ADP administrator whether the gateway needs upgrading."
    ),
    "feature_disabled": "That feature is switched off on this ADP deployment. Ask your ADP administrator to enable it.",
    "permission_denied": "You are not permitted to do that on this ADP deployment. Ask your ADP administrator for access.",
    "stale_revision": "What you are acting on changed since you read it. Read its current state and try again.",
    "budget_exhausted": "Your spending limit is blocking this. Check `adp doctor --checks budget`.",
    "dependency_pending": "A service this needs is not ready yet. Try again shortly.",
    "request_timeout": "ADP did not answer in time. Read the current state before retrying.",
    "unknown_mutation_outcome": (
        "ADP may or may not have applied that change. Read the current state before retrying — do not repeat the command blindly."
    ),
    "capability_unknown": "ADP could not establish whether that operation is available. Nothing was sent.",
    "schema_unsupported": "This ADP deployment speaks a capability format this CLI does not understand. Run `adp update`.",
}

# The bounded default check set. Each is a READ of configuration or own-scope
# state. `models` and `agents` resolve readiness, never invoke anything.
ALL_CHECKS = ("auth", "api", "budget", "models", "agents")

# Anything matching these is never printed, in any mode, at any verbosity. The
# doctor's output is pasted into tickets and chat by people trying to get help,
# so a token that reaches the screen reaches a place it cannot be recalled from.
_SECRET_KEYS = re.compile(
    r"(?:^|[-_.])(?:password|passwd|secret|token|credential|cookie|authorization)(?:$|[-_.])"
    r"|(?:^|[-_.])(?:access|api|private|refresh|id)[-_.]?(?:key|token)(?:$|[-_.])",
    re.IGNORECASE,
)


def subcode_error(subcode, extra=""):
    """A refusal a script can branch on, with a message a person can act on."""
    message = SUBCODE_MESSAGES[subcode]
    return CliError(f"{message} {extra}".strip(), subcode, SUBCODES[subcode])


def redact(value):
    """Strip anything secret-shaped before it can be printed.

    Key-name based, applied recursively, and deliberately conservative: a field
    this does not recognise is still dropped if its *name* looks like a
    credential. Over-redacting a diagnostic costs a user one question; leaking a
    token costs them a rotation.
    """
    if isinstance(value, dict):
        return {key: ("[redacted]" if _SECRET_KEYS.search(str(key)) else redact(inner)) for key, inner in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# --- capability discovery -----------------------------------------------------


def _cache_key():
    """Deployment + identity + tenant. All three, or a cached answer can cross one.

    A document describes one caller in one tenant on one gateway. Keyed on less
    than all three, `adp --deployment prod capabilities` could serve the answer
    cached for dev, or one user's permissions could be read as another's after a
    re-login on a shared machine.
    """
    return common.capability_cache_key()


def _cached_document(key):
    """A cached document, or None. Any doubt returns None rather than the cache."""
    try:
        cached = common.read_state(CACHE_STATE_NAME)
    except CliError:
        # An unreadable or unsafe state file must not be a hard failure of a
        # read-only diagnostic — the fetch path still works without a cache.
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("key") != key:
        # A different deployment, gateway or identity. Not ours.
        return None
    age = time.time() - cached.get("fetched_at", 0)
    if age < 0 or age > CACHE_TTL_SECONDS:
        # Negative age means a clock moved; treat it as stale rather than trusting
        # a document that claims to be from the future.
        return None
    document = cached.get("document")
    return document if isinstance(document, dict) else None


def _store_document(key, document):
    try:
        common.write_state(CACHE_STATE_NAME, {"key": key, "fetched_at": int(time.time()), "document": document})
    except (CliError, OSError):
        # Caching is an optimization. Failing to cache must never fail the read
        # the user actually asked for.
        pass


def _validate_document(document):
    if not isinstance(document, dict) or not isinstance(document.get("operations"), list):
        raise CliError("ADP returned a capability document this CLI could not read.", "invalid_response")
    version = document.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise subcode_error("schema_unsupported", f"It reported format {version!r}.")
    for operation in document["operations"]:
        if not isinstance(operation, dict) or not isinstance(operation.get("id"), str):
            raise CliError("ADP returned a capability document this CLI could not read.", "invalid_response")
        for axis in ("supported", "enabled", "permitted", "ready"):
            if operation.get(axis) not in (YES, NO, UNKNOWN):
                # An unrecognised state is NOT coerced. Coercing it to "no" would
                # deny a user access on a value the server never meant, and to
                # "yes" would promise an operation that may not work.
                raise CliError("ADP reported a capability state this CLI could not read.", "invalid_response")
    return document


def fetch(*, refresh=False, request=common.api):
    """The capability document for this caller, via the shared scoped cache."""
    try:
        return common.read_capabilities(refresh=refresh, request=request)
    except CliError as exc:
        if exc.status_code == 404:
            raise CliError(
                "This ADP deployment is older than capability discovery. Ask your ADP "
                "administrator to upgrade the gateway; meanwhile commands still work "
                "and report their own errors.",
                "server_too_old",
                4,
            ) from None
        if exc.code == "schema_unsupported":
            raise subcode_error("schema_unsupported") from None
        raise


def find(document, operation_id):
    """One operation's four states, or None if this server never mentioned it.

    Absence is meaningful and is the ONLY way an older server can correctly
    report an operation it has never heard of: it cannot describe a newer
    client's vocabulary, so the client reads silence as "not supported here".
    """
    for operation in document.get("operations", []):
        if operation.get("id") == operation_id:
            return operation
    return None


def blocking_reason(operation):
    """Why an operation cannot proceed, or None if nothing definitively blocks it.

    Returns a stable subcode. UNKNOWN on any axis is NOT a block — it is reported
    as unknown by the caller, because refusing on an undetermined state would
    deny a user work they are entitled to on evidence nobody produced.
    """
    if operation is None:
        return "unsupported_operation"
    if operation.get("supported") == NO:
        return "unsupported_operation"
    if operation.get("enabled") == NO:
        return "feature_disabled"
    if operation.get("permitted") == NO:
        return "permission_denied"
    if operation.get("ready") == NO:
        return "dependency_pending"
    return None


def ensure_can_mutate(operation_id, *, refresh=False, request=common.api):
    """Compatibility wrapper around the shared mutation preflight."""
    return common.ensure_can_mutate(operation_id, refresh=refresh, request=request)


# --- doctor checks ------------------------------------------------------------
#
# Each returns a small dict: a state, a human-readable finding, and nothing that
# could identify internal topology or carry a credential. A check that cannot
# determine its answer reports "unknown" and says why — it never guesses, and it
# never escalates to a write in order to find out.


def check_auth(client):
    """Session presence and expiry metadata only — never the token itself.

    Reads `/auth/me`, which any authenticated caller may read. Deliberately NOT
    `/auth/cli/admin-session`: that one is behind `require_admin`, so using it
    here would report "not signed in" to every correctly-signed-in ordinary user
    — turning a diagnostic into a source of the confusion it exists to remove.
    """
    try:
        session = client("GET", "/auth/me", timeout=30)
    except CliError as exc:
        if exc.exit_code == 2:
            return {"state": "failed", "finding": "Not signed in, or the session expired.", "next": "adp login"}
        return {"state": UNKNOWN, "finding": f"Could not confirm the session ({exc.code}).", "next": "adp status"}
    detail = redact(session if isinstance(session, dict) else {})
    return {
        "state": "ok",
        "finding": "Signed in.",
        # Expiry metadata is useful and is not a secret. The token is neither
        # requested nor printed — `adp token` exists for the one case that needs it.
        "expires_at": detail.get("expires_at") or UNKNOWN,
        "organization": detail.get("org_id") or UNKNOWN,
    }


def check_api(client):
    """Gateway reachability and the capability contract version it speaks."""
    try:
        document, source = fetch(request=client)
    except CliError as exc:
        return {"state": "failed" if exc.code == "server_too_old" else UNKNOWN, "finding": str(exc), "next": ""}
    gateway = document.get("gateway") or {}
    release = gateway.get("release") or ""
    return {
        "state": "ok",
        "finding": f"Reachable; capability format {document.get('schema_version')}.",
        # An unestablished release is reported as unknown, not as an empty version
        # a client could mistake for "old".
        "gateway_release": release or UNKNOWN,
        "read_from": source,
    }


def check_budget(client):
    """Whether spending limits are blocking, and why — an own-scope read."""
    try:
        budget = client("GET", "/me/budget", timeout=30)
    except CliError as exc:
        if exc.status_code == 404:
            return {"state": UNKNOWN, "finding": "This deployment does not report your budget.", "next": ""}
        return {"state": UNKNOWN, "finding": f"Could not read your budget ({exc.code}).", "next": ""}
    if not isinstance(budget, dict):
        return {"state": UNKNOWN, "finding": "ADP returned a budget this CLI could not read.", "next": ""}
    if budget.get("cap_status") == "uncapped":
        return {"state": "ok", "finding": "No spending limit applies to you."}
    # Blocking requires BOTH conditions, per the server's own contract: `band`
    # says the cap is exceeded, `enforcement_mode` says what happens when it is.
    # `soft` warns and allows, so reporting "exceeded" alone as blocked would tell
    # a user their work is stopped when it is running fine.
    band, mode = budget.get("band"), budget.get("enforcement_mode")
    if band == "exceeded" and mode == "hard":
        return {
            "state": "failed",
            "finding": f"A spending limit is blocking requests (your {budget.get('period', 'current')} limit is exhausted).",
            "next": "Ask your ADP administrator to raise the limit, or wait for the period to reset.",
        }
    if band == "exceeded":
        return {"state": "ok", "finding": "Your spending limit is exceeded but set to warn only, so requests still run."}
    if budget.get("identity_status") == "unresolved":
        # The server states outright that an unresolved identity means the cloud
        # ledger is ABSENT from these figures — so "not blocked" is not a
        # conclusion available here.
        return {
            "state": UNKNOWN,
            "finding": "Part of your spend could not be read, so whether a limit is blocking cannot be confirmed.",
            "next": "",
        }
    return {"state": "ok", "finding": f"No spending limit is blocking requests (usage band: {band or UNKNOWN})."}


def check_models(client):
    """Resolve effective own-scope model routes without invoking a model."""
    document, _ = _document_or_empty(client)
    operation = find(document, "models.catalog.read")
    if operation is None:
        return {"state": UNKNOWN, "finding": "This deployment does not report model capability.", "next": ""}
    reason = blocking_reason(operation)
    if reason:
        return {"state": "failed", "finding": SUBCODE_MESSAGES[reason], "next": ""}
    try:
        response = client("GET", "/me/persona-models", timeout=30)
    except CliError as exc:
        if exc.status_code in (401, 403):
            return {"state": "failed", "finding": "The effective model route is not visible to this session.", "next": "adp login"}
        if exc.status_code == 404:
            return {"state": UNKNOWN, "finding": "This gateway cannot resolve effective model routes.", "next": ""}
        return {"state": UNKNOWN, "finding": f"Could not resolve effective model routes ({exc.code}).", "next": ""}
    if not isinstance(response, dict) or not isinstance(response.get("entries"), list):
        return {"state": UNKNOWN, "finding": "ADP returned model routing data this CLI could not read.", "next": ""}
    routes = []
    unavailable = []
    for entry in response["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("persona_key"), str):
            return {"state": UNKNOWN, "finding": "ADP returned model routing data this CLI could not read.", "next": ""}
        status = entry.get("status") or UNKNOWN
        availability = entry.get("availability_status") or UNKNOWN
        route = {
            "persona": entry["persona_key"],
            "model": entry.get("effective_model_id") or UNKNOWN,
            "status": status,
            "availability": availability,
        }
        routes.append(route)
        if not entry.get("effective_model_id") or status in {"unavailable", "disallowed", "stale", "not-configured"}:
            unavailable.append(entry.get("availability_reason") or status or "not configured")
        elif availability not in {"selectable", "verified"}:
            if availability in {"unavailable", "disallowed", "stale"}:
                unavailable.append(entry.get("availability_reason") or availability)
    if not routes:
        return {"state": UNKNOWN, "finding": "No persona model routes are registered.", "routes": [], "next": ""}
    usable = [
        route
        for route in routes
        if route["model"] != UNKNOWN
        and route["status"] not in {"unavailable", "disallowed", "stale", "not-configured"}
        and route["availability"] in {"selectable", "verified"}
    ]
    unresolved = len(routes) - len(usable) - len(unavailable)
    if not usable and unresolved:
        return {
            "state": UNKNOWN,
            "finding": "Effective model routes exist, but their availability could not be confirmed.",
            "routes": routes,
            "next": "adp models explain --persona <key>",
        }
    if not usable:
        return {
            "state": "failed",
            "finding": f"No effective model route is usable ({unavailable[0]}).",
            "routes": routes,
            "next": "adp models explain --persona <key>",
        }
    return {"state": "ok", "finding": f"Resolved {len(usable)} usable effective model route(s).", "routes": routes, "next": ""}


def check_agents(client):
    """Hosted worker readiness, as far as a read can establish it."""
    document, _ = _document_or_empty(client)
    operation = find(document, "agents.activity.read")
    if operation is None:
        return {"state": UNKNOWN, "finding": "This deployment does not report agent capability.", "next": ""}
    reason = blocking_reason(operation)
    if reason:
        return {"state": "failed", "finding": SUBCODE_MESSAGES[reason], "next": ""}
    if operation.get("ready") == UNKNOWN:
        return {
            "state": UNKNOWN,
            "finding": "Agent workers are configured, but whether one is running now cannot be read without starting work.",
            "next": "",
        }
    return {"state": "ok", "finding": "Agent workers are available."}


def _document_or_empty(client):
    try:
        return fetch(request=client)
    except CliError:
        return {}, "unavailable"


CHECKS = {
    "auth": check_auth,
    "api": check_api,
    "budget": check_budget,
    "models": check_models,
    "agents": check_agents,
}


def run_checks(names, client):
    findings = {}
    for name in names:
        try:
            findings[name] = redact(CHECKS[name](client))
        except CliError as exc:
            # One failing check must not abort the rest — the value of a
            # diagnostic is the whole picture, and the first failure is often a
            # symptom of a later one.
            findings[name] = {"state": UNKNOWN, "finding": str(exc), "next": ""}
    return findings


def lookup_request(request_id, client):
    """Explain one past request, within the caller's own log permissions.

    The permission decision and tenant boundary are the SERVER's. This reads the
    request-log lookup dedicated to gateway request IDs; it returns the same 404
    for absent, foreign-tenant and unauthorized lookups.

    A not-found and a not-yours answer are therefore reported identically, which
    is deliberate: distinguishing them would confirm to a stranger that another
    user's request exists.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
        # Validated before it reaches a URL path, so a traversal sequence or an
        # encoded slash cannot reshape the request this sends.
        raise CliError("A request ID may contain only letters, digits and - _ . :", "usage_error", 1)
    try:
        detail = client("GET", f"/me/cli-requests/{request_id}", timeout=30)
    except CliError as exc:
        if exc.status_code in (403, 404):
            raise CliError(
                "No request with that ID is visible to you. Check the ID, or ask an administrator with log access to look it up.",
                "request_not_visible",
                4,
            ) from None
        raise
    return redact(detail if isinstance(detail, dict) else {})


# --- rendering ----------------------------------------------------------------


def render_capabilities(document, operation_id=None):
    """Plain-language capability output, preserving the four states separately."""
    operations = document.get("operations", [])
    if operation_id:
        operations = [operation for operation in operations if operation.get("id") == operation_id]
        if not operations:
            raise subcode_error("unsupported_operation", f"This deployment did not report {operation_id!r}.")
    lines = []
    for operation in operations:
        reason = blocking_reason(operation)
        if reason:
            verdict = {
                "unsupported_operation": "not available here",
                "feature_disabled": "switched off on this deployment",
                "permission_denied": "not permitted for you",
                "dependency_pending": "a service it needs is not ready",
            }[reason]
        elif UNKNOWN in (operation.get("permitted"), operation.get("ready"), operation.get("enabled")):
            # Reported as unknown rather than as available. Saying "available"
            # here would promise something nobody established.
            verdict = "available, with something unconfirmed"
        else:
            verdict = "available"
        lines.append(f"{operation.get('id')}: {verdict} — {operation.get('summary', '')}".rstrip(" —"))
    return lines


def emit_capabilities(document, args):
    if args.operation:
        selected = find(document, args.operation)
        if selected is None:
            raise subcode_error("unsupported_operation", f"This deployment did not report {args.operation!r}.")
        document = {**document, "operations": [selected]}
    result = common.envelope("ok", CAPABILITIES_COMMAND, redact(document))
    if args.json:
        return common.emit(result, True)
    gateway = document.get("gateway") or {}
    print(f"{CAPABILITIES_COMMAND}: {result['status']}")
    print(f"Gateway release: {gateway.get('release') or UNKNOWN}")
    print(f"Capability format: {document.get('schema_version')}")
    for line in render_capabilities(document, args.operation):
        print(line)
    return 0


def emit_doctor(findings, args, *, request_detail=None):
    states = [finding.get("state") for finding in findings.values()]
    status = "failed" if "failed" in states else "unavailable" if UNKNOWN in states else "ok"
    detail = {"checks": findings}
    if request_detail is not None:
        detail["request"] = request_detail
    result = common.envelope(status, COMMAND, detail)
    if args.json:
        return common.emit(result, True)
    print(f"{COMMAND}: {status}")
    for name, finding in findings.items():
        print(f"{name}: {finding.get('state')} — {finding.get('finding', '')}")
        if finding.get("next"):
            print(f"  try: {finding['next']}")
    if request_detail is not None:
        print("request:")
        print(json.dumps(request_detail, indent=2))
    # Established envelope mapping, unchanged: 5 failed, 4 pending/unavailable, 0 ok.
    return 5 if status == "failed" else 4 if status == "unavailable" else 0


# --- entry point --------------------------------------------------------------


def parse_checks(value):
    if not value:
        return ALL_CHECKS
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        raise CliError(
            f"Unknown check(s): {', '.join(unknown)}. Choose from {', '.join(ALL_CHECKS)}.",
            "usage_error",
            1,
        )
    return tuple(names)


def parser():
    root = Parser(prog="adp", add_help=True)
    verbs = root.add_subparsers(dest="verb", required=True)

    capabilities = verbs.add_parser(CAPABILITIES_COMMAND)
    capabilities.add_argument("--json", action="store_true")
    capabilities.add_argument("--refresh", action="store_true", help="Ignore the cached answer and re-read from ADP")
    capabilities.add_argument("--operation", help="Report one operation ID only")

    doctor = verbs.add_parser(COMMAND)
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--checks", help=f"Comma-separated subset of: {', '.join(ALL_CHECKS)}")
    doctor.add_argument("--request-id", dest="request_id", help="Explain one past request you are allowed to see")
    return root


def run(args, client=common.api):
    if args.verb == CAPABILITIES_COMMAND:
        document, _ = fetch(refresh=args.refresh, request=client)
        return emit_capabilities(document, args)
    if args.request_id:
        # A request lookup is a targeted question; running the full check set
        # alongside it would send reads the user did not ask for.
        detail = lookup_request(args.request_id, client)
        return emit_doctor({}, args, request_detail=detail)
    return emit_doctor(run_checks(parse_checks(args.checks), client), args)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    command = CAPABILITIES_COMMAND if argv[:1] == [CAPABILITIES_COMMAND] else COMMAND
    try:
        args = parser().parse_args(argv)
        return run(args)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, command, as_json)
    except KeyboardInterrupt:
        # Both verbs are reads, so an interrupt leaves nothing half-done. Said
        # plainly, because the established message for mutating commands ("a
        # write may already have been accepted") would be false here and would
        # send a user hunting for damage that cannot exist.
        return common.report_error(
            CliError("Interrupted. Nothing was changed — both of these commands only read.", "interrupted", 130),
            command,
            as_json,
        )


if __name__ == "__main__":
    sys.exit(main())
