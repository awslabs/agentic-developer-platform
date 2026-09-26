#!/usr/bin/env python3
"""Superplane operational commands, reached as `adp superplane <verb>` (Issue #5039).

Standard library only, like every other `adp` area helper. The point of this file
is what it does NOT have: a credential store. The upstream Superplane CLI wrote a
plaintext ``token`` into ``~/.superplane/config.yaml``, so a developer laptop held
a second long-lived secret outside ADP's vault and outside its revocation path.
Here the only credential path is the ADP session that ``adp login`` already
established — one token is retained in memory for each command and nothing is
written to disk by this helper except the *non-secret* current-workspace pointer.

Three treatments, deliberately not uniform:

* **reused** — account, aws-onboard, node, quota, workspace, cost, events, deploy
  keep their upstream request shapes but run against the domain API through the
  shared ADP transport, which authorizes server-side.
* **redirected** — org and user perform NO administration. ADP owns organization,
  user and SSO administration; porting those verbs would rebuild the second
  administration surface the design retires. They report where to go instead.
* **replaced** — the upstream ``vault`` commands become ``provider``, which posts
  the value to ADP's vault API and keeps only the returned credential *id* as
  domain metadata.

Provider values are read from a hidden prompt or explicit stdin. ``--api-key
VALUE`` is refused: a secret in argv lands in shell history and in every process
listing on the machine. Request bodies are never traced for the same reason.
"""

from __future__ import annotations

import argparse
import binascii
import fcntl
import getpass
import json
import os
import re
import stat
import sys
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

Api = common.Api
CliError = common.CliError

NAME = "superplane"

# The domain API is mounted behind the same gateway as every other ADP API, so
# the ADP session authorizes it and this helper needs no endpoint of its own.
#
# This is the base the design proposes — "/api/superplane/v1 on the existing ADP
# origin" (design §7 line 448) — expressed relative to the /api that
# adp_common.gateway_url() already appends. The earlier "/domain-apps/superplane"
# matched neither the design nor anything mounted in this repo (Issue #5039).
#
# Every path below is now checked against two authorities rather than proposed
# (Issue #5637): the gateway's route allowlist
# (src/domain_proxy/superplane_routes.json) decides what can be forwarded at all,
# and the domain API's own request models decide what body each route accepts. A
# path or field this helper invents is a request the gateway 404s before the
# domain ever sees it, so tests/cli/test_superplane_contract.py compares this
# module's emitted (method, path) pairs against that allowlist directly.
API_BASE = "/superplane/v1"
ACCOUNT_ADAPTER_SUPPORT = "/superplane/installation-support"
DOMAIN_CAPABILITIES = API_BASE + "/capabilities"

# ADP's own vault. Provider values go here and nowhere else; the response carries
# an id, never the value and never a Secrets Manager ARN.
VAULT_CREDENTIALS = "/auth/credentials"

# The domain's credential-reference lifecycle. NOT "/providers": that path was
# invented by this helper and is absent from the gateway allowlist, so every
# provider verb 404'd at the proxy (Issue #5637). The real contract is
# app/routers/accounts.py — POST/GET /vault/credentials and DELETE
# /vault/credentials/{id} — carrying an `adp_credential_id` reference, never a
# value and never a secret ARN.
DOMAIN_CREDENTIALS = "/vault/credentials"

# The two stores do NOT share a credential-type vocabulary, and the narrower one
# decides what the domain will accept (Issue #5637). ADP's vault takes the eight
# values of its CredentialType enum (src/shared/models/vault.py); the domain's
# RegisterCredentialRequest pins `^(api_key|service_account|oauth_token)$`
# (schemas/account.py). Sending ADP's word for a type the domain does not know
# would be a 422 AFTER the secret was already stored, so the CLI's own choices are
# translated here. `config_file` maps to the domain's `service_account` because
# that is what a multi-line provider key file is; bearer and basic_auth have no
# domain equivalent and are recorded as api_key, the domain's generic value.
DOMAIN_CREDENTIAL_TYPES = {
    "api_key": "api_key",
    "oauth_token": "oauth_token",
    "bearer": "api_key",
    "basic_auth": "api_key",
    "config_file": "service_account",
}

# Non-secret CLI state: which workspace the operational verbs default to. Lives
# in ADP's private state directory (0600, owner-checked by adp_common) rather
# than a new dotfile, and holds no token — that is the whole point of this unit.
STATE = "superplane"
PROVIDER_RECOVERIES = "provider_recoveries"
PROVIDER_DELETE_RECOVERIES = "provider_delete_recoveries"
CREATE_RECOVERIES = "create_recoveries"
CREATE_PENDING_STATUSES = {"pending", "provisioning", "running", "unknown", "needsrecovery"}
CREATE_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}
CREATE_RETIRED_STATUSES = {"deleted", "deleting", "teardown"}
CREATE_SUCCEEDED_STATUSES = {"active", "ready", "created"}
RECOVERY_CONTEXT_KEYS = ("deployment_id", "gateway", "principal", "tenant")

# Argument names that would put a secret in argv. Checked before argparse so the
# error names the real problem instead of "unrecognized arguments".
SECRET_FLAGS = ("--api-key", "--token", "--secret", "--oauth-token", "--password")

# Verbs ADP owns. Listing them flat alongside the operational verbs would imply
# this CLI administers organizations and users, which it must not.
REDIRECTED = {
    "org": (
        "/settings/organization",
        "Organization, quota-policy and SSO administration is ADP's, not this CLI's.",
    ),
    "user": (
        "/settings/users",
        "User, invitation and role administration is ADP's, not this CLI's.",
    ),
}

# Onboarding — capability discovery, plan review, provider binding and durable
# operation recovery — lives in its own helper file.
#
# It is a separate file because it is a different surface with different failure
# modes: this file runs work in a workspace that exists, while onboarding decides
# whether one may be built and spends money doing it. Delegating instead of
# growing this file also means the onboarding work and the concurrent changes to
# the operational verbs do not have to land on top of each other.
ONBOARDING_HELPER = "adp-superplane-onboarding.py"
LIFECYCLE_HELPER = "adp-superplane-lifecycle.py"


def lifecycle_helper():
    module = common.load_provider(LIFECYCLE_HELPER)
    if module is None:
        raise CliError("Superplane lifecycle extension is not installed. Run adp update.", "provider_unavailable", 4)
    return module


CANCELLED = (
    "Cancelled locally. This did NOT cancel work already accepted by the domain API "
    "and did NOT release any provider resource — check 'adp superplane events' and "
    "your provider console before assuming anything stopped or stopped billing."
)


def progress(message):
    """Progress and warnings go to stderr, so stdout stays parseable."""
    print(message, file=sys.stderr)


class LazyApi:
    """Resolve the gateway on the FIRST request, not at startup.

    Some verbs are purely local — `workspace use` only records which workspace
    later commands default to. Building the transport eagerly made those verbs
    demand a configured gateway and fail with "reinstall the CLI", which is both
    wrong and unactionable. It also masked argument errors: a missing workspace
    selection reported a gateway problem instead of the usage error it is.
    """

    def __init__(self):
        self._api = None

    def _transport(self):
        if self._api is None:
            self._api = Api()
        return self._api

    @property
    def base(self):
        return self._transport().base

    def request(self, method, path, body=None, **kwargs):
        return self._transport().request(method, path, body, **kwargs)


class SessionApi:
    """Pin one token for a command without changing the shared login or transport.

    Another terminal can switch the saved organization while this command is
    running. Receipt identity, reconciliation reads and writes must all use the
    same bearer. Expiry remains a server refusal; never refresh into another
    identity halfway through recovery. Resolution stays lazy for local commands.
    """

    def __init__(self, api):
        self._api = api
        self._token = None
        self._capabilities_checked = False

    def session_token(self):
        if self._token is None:
            self._token = common.access_token()
        return self._token

    def session_gateway(self):
        # LazyApi resolves its transport here if no request has done so yet.
        # Receipt authority must name the actual request destination, never a
        # fresh read of legacy config that another terminal may have replaced.
        return self._api.base

    def request(self, method, path, body=None, **kwargs):
        if kwargs.get("authenticated", True):
            token = self.session_token()
            if kwargs.get("token") not in (None, token):
                raise CliError("A Superplane command cannot change its authenticated identity.", "authentication_required", 2)
            if method not in {"GET", "HEAD", "OPTIONS"}:
                if not self._capabilities_checked:
                    common.ensure_can_mutate("superplane.workspace.write", request=self.request, token=token)
                    self._capabilities_checked = True
                current_recovery_context(self)
            kwargs["token"] = token
        return self._api.request(method, path, body, **kwargs)


def segment(value):
    """One path segment, matching adp-aws.py and adp-bedrock.py.

    `safe=""` rather than quote()'s default `safe="/"`: a name carrying a slash
    must not be able to add a path segment. Every value here is trusted CLI input,
    so this is the house pattern rather than a fix.
    """
    return urllib.parse.quote(value, safe="")


def query(path, params):
    """Append a query string, dropping unset values."""
    pairs = {key: value for key, value in params.items() if value is not None}
    return path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")


def reject_secret_arguments(argv):
    """Refuse a provider value supplied on the command line.

    Rejected rather than accepted-and-warned: by the time a warning prints, the
    value is already in the shell's history file and was already visible in the
    process list to every other user on the machine. There is no way to un-leak
    it, so the only safe answer is to not accept it at all.
    """
    for argument in argv:
        name = argument.split("=", 1)[0]
        if name in SECRET_FLAGS:
            raise CliError(
                f"{name} on the command line would leak the value into your shell history and "
                "process list. Omit it to be prompted, or pipe the value in with --stdin.",
                "secret_in_argv",
                1,
            )


def reject_ignored_event_filter(argv):
    """Refuse `events --workspace`, which the server has no filter for.

    Reported as its own error rather than argparse's "unrecognized arguments"
    because the flag USED to be accepted, and a user who has it in a script needs
    to know their results were never scoped — not merely that a flag went away.
    """
    if argv[:1] == ["events"] and any(item == "--workspace" or item.startswith("--workspace=") for item in argv):
        raise CliError(
            "events has no workspace filter: GET /events filters by resource type, user, action, event "
            "type and time range, so --workspace was accepted and then ignored, showing every "
            "workspace's events. Use --resource-type workspace, or --resource-type deployment, with "
            "--start-time/--end-time.",
            "usage_error",
            1,
        )


def current_workspace(explicit=None):
    """The workspace an operational verb applies to."""
    workspace = explicit or common.read_state(STATE).get("workspace")
    if not workspace:
        raise CliError(
            "No workspace selected. Run adp superplane workspace use <name>, or pass --workspace.",
            "workspace_not_selected",
            1,
        )
    return workspace


def looks_like_uuid(value):
    """True when `value` is already the identifier the domain routes take.

    Every workspace route is declared `workspace_id: uuid.UUID`
    (app/routers/workspaces.py, proxy.py, cost.py, quota.py), so FastAPI answers
    422 for anything else. Checked locally because it decides whether a lookup is
    needed at all — a caller who passes an id must not be forced to have list
    permission just to use it.
    """
    parts = value.split("-")
    return (
        len(parts) == 5
        and [len(part) for part in parts] == [8, 4, 4, 4, 12]
        and all(character in "0123456789abcdefABCDEF" for part in parts for character in part)
    )


def resolve_workspace(api, selected):
    """Translate the name a person sees into the id the routes require.

    `workspace use prod` records what the user typed, and every operational verb
    then put that straight into the path — which the domain rejects as a malformed
    UUID, so commands that looked correct failed for everyone (Issue #5637).

    Resolution reads GET /workspaces and matches on `name` first, then on the
    `display_name` the list response also carries (schemas/workspace.py), because
    that is the string the UI shows and therefore the one a user is most likely to
    copy.

    An ambiguous name RAISES rather than picking one. Two workspaces can share a
    name across isolation modes, and silently choosing either would aim a delete
    or a deployment at a workspace the user did not name — the ids are listed so
    the choice is theirs.
    """
    if looks_like_uuid(selected):
        return selected

    listed = api.request("GET", API_BASE + "/workspaces").get("workspaces") or []
    matches = [row for row in listed if isinstance(row, dict) and selected in (row.get("name"), row.get("display_name"))]
    identifiers = [str(row.get("id")) for row in matches if row.get("id")]

    if not identifiers:
        raise CliError(
            f"No workspace named {selected!r}. List them with adp superplane workspace list, or pass its id to --workspace.",
            "workspace_not_found",
            1,
        )
    if len(set(identifiers)) > 1:
        raise CliError(
            f"{selected!r} matches {len(set(identifiers))} workspaces ({', '.join(sorted(set(identifiers)))}). "
            "Pass the one you mean to --workspace. Nothing was changed.",
            "workspace_ambiguous",
            1,
        )
    return identifiers[0]


def workspace_id(api, explicit=None):
    """The resolved id of the workspace an operational verb applies to."""
    return resolve_workspace(api, current_workspace(explicit))


def read_provider_value(from_stdin, prompt):
    """Read a provider secret from an explicit pipe or a hidden prompt."""
    if from_stdin:
        # read(), not readline(): --type config_file takes a multi-line JSON key,
        # and readline() sent the vault just "{" while still reporting ok
        # (Issue #5039). Strip only the trailing newline the shell/heredoc adds
        # — interior newlines are part of the credential, and leading whitespace
        # can be significant (e.g. an indented PEM block), so only the trailing
        # run of newlines goes.
        value = sys.stdin.read().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise CliError(
                "No terminal available for a hidden prompt. Pipe the value in with --stdin.",
                "usage_error",
                1,
            )
        value = getpass.getpass(f"{prompt}: ").strip()
    # Checked on the stripped form so a pipe carrying only blank lines is still
    # "empty", but the value RETURNED keeps its interior and leading whitespace.
    if not value.strip():
        raise CliError("No value supplied; nothing was stored.", "usage_error", 1)
    return value


def mutation_guard(args, command, detail, prompt):
    """Return a no-write preview or require explicit approval before mutation."""
    if args.dry_run:
        return common.envelope(
            "ok",
            command,
            {"dry_run": True, "performed": "nothing", **detail},
            "Rerun without --dry-run and confirm, or add --yes for automation.",
        )
    if args.yes:
        return None
    if not sys.stdin.isatty():
        raise CliError(
            "Non-interactive changes require --yes. Use --dry-run to inspect the operation first. Nothing was changed.",
            "usage_error",
            1,
        )
    if input(f"{prompt} Type 'yes' to continue: ").strip().lower() != "yes":
        raise CliError("Cancelled; nothing was changed.", "cancelled", 1)
    return None


@contextmanager
def locked_state():
    path = common.state_path(STATE)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CliError(
                "Superplane state lock must be owned by you with permissions 0600.",
                "unsafe_file",
            )
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = common.read_state(STATE)
        yield state
        common.write_state(STATE, state)


def update_state(change):
    with locked_state() as state:
        change(state)


def _recoveries(state, key):
    value = state.get(key) or {}
    return value if isinstance(value, dict) else {}


def provider_recoveries():
    return _recoveries(common.read_state(STATE), PROVIDER_RECOVERIES)


def save_provider_recovery(receipt):
    def save(state):
        recoveries = _recoveries(state, PROVIDER_RECOVERIES)
        recoveries[receipt["adp_credential_id"]] = receipt
        state[PROVIDER_RECOVERIES] = recoveries

    update_state(save)


def clear_provider_recovery(credential_id):
    def clear(state):
        recoveries = _recoveries(state, PROVIDER_RECOVERIES)
        recoveries.pop(credential_id, None)
        if recoveries:
            state[PROVIDER_RECOVERIES] = recoveries
        else:
            state.pop(PROVIDER_RECOVERIES, None)

    update_state(clear)


def provider_delete_recoveries():
    return _recoveries(common.read_state(STATE), PROVIDER_DELETE_RECOVERIES)


def create_recoveries():
    return _recoveries(common.read_state(STATE), CREATE_RECOVERIES)


def save_create_recovery(receipt):
    def save(state):
        recoveries = _recoveries(state, CREATE_RECOVERIES)
        recoveries[receipt["operation_id"]] = receipt
        state[CREATE_RECOVERIES] = recoveries

    update_state(save)


def current_recovery_context(api=None):
    deployment = common.deployment_stamp()
    gateway = api.session_gateway() if isinstance(api, SessionApi) else common.gateway_url()
    try:
        token = api.session_token() if isinstance(api, SessionApi) else common.access_token()
        claims = common._jwt_claims(token)
    except (
        IndexError,
        ValueError,
        TypeError,
        AttributeError,
        binascii.Error,
        UnicodeError,
    ):
        raise CliError(
            "The signed-in identity could not be determined. Sign in again before a Superplane mutation.",
            "authentication_required",
            2,
        ) from None
    principal = claims.get("sub") if isinstance(claims, dict) else None
    tenant = (claims.get("custom:org_id") or claims.get("org_id")) if isinstance(claims, dict) else None
    if not isinstance(principal, str) or not principal:
        raise CliError(
            "The signed-in identity does not carry a stable principal. Sign in again.",
            "authentication_required",
            2,
        )
    if not isinstance(tenant, str) or not tenant.strip():
        raise CliError(
            "Superplane mutations and recovery require an access token bound to a tenant. This token has no tenant claim; "
            "no mutation was sent and existing recovery receipts were retained. "
            "Org-less login support requires the production authentication integration.",
            "tenant_bound_token_required",
            2,
        )
    return {
        **deployment,
        "gateway": gateway,
        "principal": principal,
        "tenant": tenant,
    }


def require_recovery_context(receipt, current=None, *, api=None):
    expected = receipt.get("recovery_context")
    if not isinstance(expected, dict):
        raise CliError(
            "This provider recovery receipt predates deployment and tenant binding, so it cannot be replayed safely. "
            "Nothing was sent and the receipt was retained for manual reconciliation in its original deployment.",
            "provider_recovery_context_missing",
            4,
        )
    current = current or current_recovery_context(api)
    mismatched = [key for key in RECOVERY_CONTEXT_KEYS if expected.get(key) != current.get(key)]
    if mismatched:
        raise CliError(
            "This provider recovery receipt belongs to a different deployment or signed-in tenant/principal "
            f"({', '.join(mismatched)}). Nothing was sent; select the original context and retry.",
            "provider_recovery_context_mismatch",
            4,
        )


def find_provider_delete_recovery(identifier):
    recoveries = provider_delete_recoveries()
    direct = recoveries.get(identifier)
    matches = [item for item in recoveries.values() if item.get("adp_credential_id") == identifier]
    candidates = [item for item in [direct, *matches] if isinstance(item, dict)]
    unique = {item.get("domain_record"): item for item in candidates}
    if len(unique) > 1:
        raise CliError(
            f"More than one delete recovery receipt matches {identifier!r}. Pass the domain record id. Nothing was sent.",
            "provider_ambiguous",
            1,
        )
    return next(iter(unique.values()), None)


def save_provider_delete_recovery(receipt):
    def save(state):
        recoveries = _recoveries(state, PROVIDER_DELETE_RECOVERIES)
        recoveries[receipt["domain_record"]] = receipt
        state[PROVIDER_DELETE_RECOVERIES] = recoveries

    update_state(save)


def clear_provider_delete_recovery(record_id):
    def clear(state):
        recoveries = _recoveries(state, PROVIDER_DELETE_RECOVERIES)
        recoveries.pop(record_id, None)
        if recoveries:
            state[PROVIDER_DELETE_RECOVERIES] = recoveries
        else:
            state.pop(PROVIDER_DELETE_RECOVERIES, None)

    update_state(clear)


def _create_fingerprint(command, path, body):
    return json.dumps(
        {"command": command, "path": path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_create_idempotency(api):
    try:
        capabilities = api.request("GET", DOMAIN_CAPABILITIES)
    except CliError as exc:
        if exc.status_code in (401, 403):
            raise
        raise CliError(
            "The deployed Superplane domain could not confirm replay-safe create operations. "
            "Workspace/deployment creation stopped before writing; update or restore the domain and retry.",
            "create_idempotency_unavailable",
            4,
        ) from None
    features = capabilities.get("features") if isinstance(capabilities, dict) else None
    if not isinstance(features, list) or "create-operation-id-v1" not in features:
        raise CliError(
            "This Superplane domain does not support replay-safe workspace/deployment creation. Update the domain; nothing was written.",
            "create_idempotency_unavailable",
            4,
        )


def _prepare_create_recovery(command, path, body, *, api=None, operation_id=None):
    context = current_recovery_context(api)
    fingerprint = _create_fingerprint(command, path, body)
    with locked_state() as state:
        recoveries = _recoveries(state, CREATE_RECOVERIES)
        for receipt in recoveries.values():
            if not isinstance(receipt, dict) or receipt.get("fingerprint") != fingerprint:
                continue
            if operation_id is not None and receipt.get("operation_id") != operation_id:
                continue
            recorded = receipt.get("recovery_context")
            # Display names may change without moving the authenticated request.
            # Only stable deployment and caller identity govern receipt reuse.
            same_context = isinstance(recorded, dict) and all(recorded.get(key) == context.get(key) for key in RECOVERY_CONTEXT_KEYS)
            if same_context and looks_like_uuid(str(receipt.get("operation_id", ""))):
                return receipt
        if operation_id is not None and operation_id in recoveries:
            raise CliError(
                "This operation ID already has a receipt for different inputs or a different signed-in context. "
                "Reconcile the original request before changing its identity.",
                "create_identity_conflict",
                4,
            )
        receipt = {
            "operation_id": operation_id or str(uuid.uuid4()),
            "command": command,
            "path": path,
            "fingerprint": fingerprint,
            "recovery_context": context,
        }
        recoveries[receipt["operation_id"]] = receipt
        state[CREATE_RECOVERIES] = recoveries
        return receipt


def _reject_terminal_create_receipt(receipt):
    phase = receipt.get("phase")
    if phase not in {"failed", "retired"}:
        return
    status = receipt.get("status") or phase
    raise CliError(
        f"{receipt['command']} operation {receipt['operation_id']} refers to a resource that is {status}. "
        "Its recovery receipt was retained; identical invocations cannot start another operation. "
        "Inspect the original resource before intentionally creating a resource with a different name.",
        "create_operation_failed" if phase == "failed" else "create_operation_retired",
        5,
    )


def replay_safe_create(api, command, path, body, *, expected_name, id_field, operation_id=None):
    _require_create_idempotency(api)
    receipt = _prepare_create_recovery(command, path, body, api=api, operation_id=operation_id)
    _reject_terminal_create_receipt(receipt)
    operation_id = receipt["operation_id"]
    wire_body = {**body, "operation_id": operation_id}
    try:
        result = api.request("POST", path, wire_body)
    except CliError as exc:
        terminal_failure = exc.status_code is not None and 400 <= exc.status_code < 500 and exc.code == "create_operation_failed"
        if terminal_failure:
            save_create_recovery({**receipt, "phase": "failed", "status": "Failed"})
            raise
        definitive = (
            exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code not in (408, 425, 429) and exc.status_code != 409
        )
        if definitive:
            raise
        raise CliError(
            f"{command} delivery is uncertain. The operation was not repeated with a new identity. "
            f"Recovery receipt: operation id {operation_id}. Rerun the identical command to reconcile that operation; "
            "do not change its inputs or assume the resource is absent.",
            "create_delivery_uncertain",
            5,
        ) from None
    if (
        not isinstance(result, dict)
        or not result.get(id_field)
        or result.get("name") != expected_name
        or not isinstance(result.get("status"), str)
        or not result["status"].strip()
        or (receipt.get("resource_id") and receipt["resource_id"] != str(result[id_field]))
    ):
        raise CliError(
            f"{command} returned a malformed success and may have committed. Recovery receipt: operation id "
            f"{operation_id}. Rerun the identical command to reconcile that operation.",
            "create_delivery_uncertain",
            5,
        )
    create_status = result["status"].lower()
    receipt = {**receipt, "resource_id": str(result[id_field]), "status": result["status"]}
    if create_status in CREATE_FAILED_STATUSES:
        receipt["phase"] = "failed"
        save_create_recovery(receipt)
        _reject_terminal_create_receipt(receipt)
    if create_status in CREATE_RETIRED_STATUSES:
        receipt["phase"] = "retired"
        save_create_recovery(receipt)
        _reject_terminal_create_receipt(receipt)
    if create_status in CREATE_PENDING_STATUSES:
        save_create_recovery({**receipt, "phase": "accepted_pending"})
    elif create_status in CREATE_SUCCEEDED_STATUSES:
        # A successful reply does not prove its output reached the caller. Keep
        # the same identity across that crash window and later identical calls.
        save_create_recovery({**receipt, "phase": "completed"})
    else:
        raise CliError(
            f"{command} returned an unrecognized resource status. Its outcome remains uncertain. "
            f"Recovery receipt: operation id {operation_id}. Rerun the identical command to reconcile that operation.",
            "create_delivery_uncertain",
            5,
        )
    return result


# --- reused: workspace -------------------------------------------------------


def workspace(args, api):
    if args.subcommand == "delete":
        return lifecycle_helper().workspace_delete(args, api)
    if args.subcommand == "use":
        # Purely local, and deliberately not validated against the API: selecting
        # a context is not an authorization decision. The next call carries the
        # session and the server decides.
        update_state(lambda state: state.update(workspace=args.name))
        return common.envelope(
            "ok",
            "superplane workspace use",
            {"workspace": args.name},
            "adp superplane workspace describe",
        )

    if args.subcommand == "create":
        operation_id = deployment_uuid(args.operation_id, "--operation-id") if args.operation_id else None
        if args.isolation == "research" and not args.account:
            raise CliError("Research workspaces need --account <name>.", "usage_error", 1)
        body = {"name": args.name, "isolation_mode": args.isolation}
        for key, value in (
            ("account", args.account),
            ("budget_max_daily_usd", args.budget_daily),
            ("budget_max_gpus", args.budget_gpus),
        ):
            if value is not None:
                body[key] = value
        preview = mutation_guard(
            args,
            "superplane workspace create",
            {"workspace": body, **({"operation_id": operation_id} if operation_id else {})},
            f"Create workspace {args.name!r}?",
        )
        if preview:
            return preview
        progress(f"Creating workspace {args.name}...")
        created = replay_safe_create(
            api,
            "superplane workspace create",
            API_BASE + "/workspaces",
            body,
            expected_name=args.name,
            id_field="id",
            operation_id=operation_id,
        )
        if created["status"].lower() in CREATE_PENDING_STATUSES:
            return common.envelope(
                "pending",
                "superplane workspace create",
                created,
                f"Reconcile with adp superplane workspace describe --workspace {created['id']}",
            )
        return common.envelope("ok", "superplane workspace create", created)

    if args.subcommand == "list":
        result = api.request("GET", API_BASE + "/workspaces")
        return common.envelope(
            "ok",
            "superplane workspace list",
            {"workspaces": result.get("workspaces") or []},
        )

    identifier = workspace_id(api, getattr(args, "workspace", None))
    if args.subcommand == "kubeconfig":
        # POST, not GET. The route is declared POST (app/routers/workspaces.py:314)
        # because it MINTS short-lived access rather than reading a stored file,
        # and only the POST pair is in the gateway allowlist — the GET this helper
        # sent was 404'd at the proxy (Issue #5637).
        result = api.request("POST", f"{API_BASE}/workspaces/{segment(identifier)}/kubeconfig")
        # expires_at is required by KubeconfigResponse and was being discarded, so
        # the caller had no way to know when their cluster access dies.
        return common.envelope(
            "ok",
            "superplane workspace kubeconfig",
            {
                "kubeconfig": result.get("kubeconfig", ""),
                "expires_at": result.get("expires_at"),
            },
        )

    result = api.request("GET", f"{API_BASE}/workspaces/{segment(identifier)}")
    return common.envelope("ok", "superplane workspace describe", result)


# --- reused: node, cost, events, deploy, quota -------------------------------


def node(args, api):
    identifier = workspace_id(api, args.workspace)
    result = api.request("GET", f"{API_BASE}/workspaces/{segment(identifier)}/nodes")
    return common.envelope("ok", "superplane node list", {"nodes": result.get("nodes") or []})


def cost(args, api):
    """Workspace cost, or organization-wide cost with --org.

    There is no `/cost/summary`: this helper invented it, and it is absent from the
    gateway allowlist, so every `adp superplane cost` was 404'd at the proxy
    (Issue #5637). The domain has two distinct routes (app/routers/cost.py) — one
    per workspace and one per organization — so the CLI must choose between them
    rather than pass the workspace as a query parameter to a single endpoint.
    """
    if args.org:
        if args.workspace:
            raise CliError("Use either --org or --workspace, not both.", "usage_error", 1)
        path = query(
            API_BASE + "/orgs/cost",
            {"start_date": args.start_date, "end_date": args.end_date},
        )
        return common.envelope("ok", "superplane cost", api.request("GET", path))

    identifier = workspace_id(api, args.workspace)
    path = query(
        f"{API_BASE}/workspaces/{segment(identifier)}/cost",
        {"start_date": args.start_date, "end_date": args.end_date},
    )
    return common.envelope("ok", "superplane cost", api.request("GET", path))


def events(args, api):
    """Audit events, filtered only by parameters the route actually has.

    `--workspace` used to be accepted, sent as `?workspace=`, and DROPPED by the
    server: GET /events declares resource_type, user, action, event_type,
    start_time, end_time, limit and offset, and nothing else
    (app/routers/events.py). An unknown query parameter is ignored, so the user
    was shown every workspace's events while believing the list was scoped —
    which is worse than not offering the filter. It is now refused with a pointer
    to the filters that exist (see parser()).
    """
    if getattr(args, "workspace", None) or getattr(args, "follow", False) or getattr(args, "after", None):
        return lifecycle_helper().events(args, api)
    path = query(
        API_BASE + "/events",
        {
            "resource_type": args.resource_type,
            "user": args.user,
            "action": args.action,
            "event_type": args.event_type,
            "start_time": args.start_time,
            "end_time": args.end_time,
            "limit": args.limit,
            "offset": args.offset,
        },
    )
    result = api.request("GET", path)
    return common.envelope(
        "ok",
        "superplane events",
        {
            "events": result.get("events") or [],
            "total": result.get("total"),
            "offset": result.get("offset"),
        },
    )


def deployment_name(value):
    """Validate the name locally, against the server's own pattern.

    CreateDeploymentRequest pins `^[a-z0-9][a-z0-9-]*[a-z0-9]$` (schemas/proxy.py)
    because the name becomes a Kubernetes object name. Checking here turns a 422
    from a round trip into an immediate, specific usage error.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", value) or len(value) > 255:
        raise CliError(
            f"Deployment name {value!r} is not usable: use lowercase letters, digits and hyphens, starting and ending with a letter or digit.",
            "usage_error",
            1,
        )
    return value


def deployment_uuid(value, label):
    if not looks_like_uuid(value):
        raise CliError(f"{label} must be a UUID.", "usage_error", 1)
    return str(uuid.UUID(value))


def deployment_revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise CliError("Use the exact plan revision returned by deployment preview.", "usage_error", 1)
    return value


def deployment_preview(args, api, identifier, path, body):
    """Review the server's exact plan; requesting approval never records a decision."""
    command = "superplane deploy " + args.subcommand
    if args.request_approval:
        deployment_revision(args.plan_revision)
    elif args.plan_revision is not None:
        raise CliError("--plan-revision requires --request-approval on a preview command.", "usage_error", 1)
    if args.dry_run:
        return mutation_guard(args, command, {"workspace_id": identifier, "request": body}, "")
    review = api.request("POST", path, body)
    action = "teardown" if args.subcommand == "teardown-preview" else "provision"
    approval = review.get("approval_request") if isinstance(review, dict) else None
    if (
        not isinstance(review, dict)
        or review.get("request_id") != body["operation_id"]
        or not looks_like_uuid(str(review.get("deployment_id", "")))
        or not looks_like_uuid(str(review.get("allocation_id", "")))
        or not re.fullmatch(r"[a-f0-9]{64}", str(review.get("revision", "")))
        or not isinstance(review.get("controller_plan"), dict)
        or not isinstance(approval, dict)
        or set(approval) != {"workspace_id", "action", "idempotency_key", "parameters"}
        or approval["workspace_id"] != identifier
        or approval["action"] != action
        or not isinstance(approval["idempotency_key"], str)
        or approval["idempotency_key"] != body["operation_id"]
        or not isinstance(approval["parameters"], dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in approval["parameters"].items())
        or (action == "teardown" and review["deployment_id"] != str(uuid.UUID(args.id)))
    ):
        raise CliError(
            "Deployment preview returned incomplete or mismatched request identity. No approval was requested.", "invalid_deployment_preview", 4
        )
    if not args.request_approval:
        return common.envelope(
            "ok",
            command,
            review,
            "Review the controller plan, then repeat this preview with --request-approval --plan-revision <revision> --yes. "
            "An eligible human decides through adp superplane onboarding approval decide.",
        )
    if review["revision"] != args.plan_revision:
        raise CliError("The deployment plan changed. Review its new revision before requesting approval.", "plan_changed", 4)
    progress(json.dumps(review, sort_keys=True))
    mutation_guard(args, command, {}, f"Request human approval for deployment plan {args.plan_revision}?")
    requested = api.request("POST", API_BASE + "/operation-approvals", approval)
    return common.envelope(
        "ok",
        command,
        {"preview": review, "approval": requested},
        "Read or decide this request with adp superplane onboarding approval show/decide. "
        "After approval, submit the original operation ID and exact reviewed inputs with --approval-id and --plan-revision.",
    )


def deploy(args, api):
    if args.subcommand == "profiles":
        return lifecycle_helper().deployment_profiles(args, api)
    if args.subcommand != "list":
        operation_id = deployment_uuid(args.operation_id, "--operation-id")
        if args.subcommand in {"delete", "teardown-preview"} and not looks_like_uuid(args.id):
            raise CliError("Use the deployment UUID returned by create or list.", "usage_error", 1)
        if args.subcommand in {"create", "delete"}:
            approval_id = deployment_uuid(args.approval_id, "--approval-id")
            deployment_revision(args.plan_revision)
    identifier = workspace_id(api, getattr(args, "workspace", None))
    if looks_like_uuid(identifier):
        identifier = str(uuid.UUID(identifier))
    base = f"{API_BASE}/workspaces/{segment(identifier)}/deployments"

    if args.subcommand in {"create", "preview"}:
        # `model_name`, not `model`: the server's field name (schemas/proxy.py).
        # `name` is REQUIRED there with no default, so the old "generated when
        # omitted" help was false — nothing generated it and the request was a 422.
        body = {
            "name": deployment_name(args.name),
            "model_name": args.model,
            "precision": args.precision,
            "profile_id": args.profile_id,
        }
        if getattr(args, "namespace", None) is not None:
            body["expected_namespace"] = args.namespace
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", args.profile_id):
            raise CliError("Use a configured deployment --profile-id (lowercase letters, digits and hyphens).", "usage_error", 1)
        # Sent only when the user asked for them, so the server's own defaults
        # (vllm, 1 replica, 1 GPU) stay authoritative.
        for key, value in (
            ("serving_framework", args.serving_framework),
            ("replicas", args.replicas),
            ("gpu_per_replica", args.gpu_per_replica),
            ("tensor_parallel_size", args.tensor_parallel_size),
            ("max_model_len", args.max_model_len),
        ):
            if value is not None:
                body[key] = value
        if args.subcommand == "preview":
            return deployment_preview(args, api, identifier, base + "/preview", {**body, "operation_id": operation_id})
        body.update(approval_id=approval_id, plan_revision=args.plan_revision)
        preview = mutation_guard(
            args,
            "superplane deploy create",
            {"workspace_id": identifier, "deployment": {**body, "operation_id": operation_id}},
            f"Create deployment {args.name!r} in workspace {identifier}?",
        )
        if preview:
            return preview
        progress(f"Requesting deployment of {args.model} in {args.workspace or 'the selected workspace'}...")
        created = replay_safe_create(
            api,
            "superplane deploy create",
            base,
            body,
            expected_name=args.name,
            id_field="deployment_id",
            operation_id=operation_id,
        )
        if created["status"].lower() in CREATE_PENDING_STATUSES:
            return common.envelope(
                "pending",
                "superplane deploy create",
                created,
                f"Reconcile with adp superplane deploy list --workspace {identifier}",
            )
        return common.envelope("ok", "superplane deploy create", created)

    if args.subcommand in {"delete", "teardown-preview"}:
        path = f"{base}/{segment(str(uuid.UUID(args.id)))}"
        body = {"operation_id": operation_id}
        if args.subcommand == "teardown-preview":
            return deployment_preview(args, api, identifier, path + "/teardown-preview", body)
        body.update(approval_id=approval_id, plan_revision=args.plan_revision)
        preview = mutation_guard(
            args,
            "superplane deploy delete",
            {"workspace_id": identifier, "deployment_id": args.id, "request": body},
            f"Delete deployment {args.id!r} from workspace {identifier}?",
        )
        if preview:
            return preview
        progress(f"Deleting deployment {args.id}...")
        try:
            deleted = api.request("DELETE", path, body)
        except CliError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code not in {408, 409, 425, 429}:
                raise
            raise CliError(
                f"Teardown delivery is uncertain for operation {operation_id}. Reconcile with deploy list or repeat the exact "
                "command with the same operation ID, approval ID and plan revision; do not generate another identity.",
                "teardown_delivery_uncertain",
                5,
            ) from None
        if not isinstance(deleted, dict) or not deleted.get("name") or not deleted.get("status"):
            raise CliError(
                f"Teardown response was incomplete. Reconcile operation {operation_id} using the same request identity.",
                "teardown_delivery_uncertain",
                5,
            )
        pending = str(deleted.get("status", "")).lower() != "deleted"
        return common.envelope("pending" if pending else "ok", "superplane deploy delete", deleted)

    result = api.request("GET", base)
    return common.envelope("ok", "superplane deploy list", {"deployments": result.get("deployments") or []})


def quota(args, api):
    # Org-level quota is a policy decision ADP owns, so this helper only ever
    # addresses a workspace's quota. `org` is redirected for the same reason.
    identifier = workspace_id(api, args.workspace)
    path = f"{API_BASE}/workspaces/{segment(identifier)}/quota"

    if args.subcommand == "show":
        return common.envelope("ok", "superplane quota show", api.request("GET", path))

    body = {}
    for key, value in (
        ("max_gpus", args.max_gpus),
        ("max_cost_per_day", args.max_cost_per_day),
        ("max_nodes", args.max_nodes),
    ):
        if value is not None:
            body[key] = value
    if args.allowed_clouds is not None:
        body["allowed_clouds"] = [item.strip() for item in args.allowed_clouds.split(",") if item.strip()]
    if not body:
        raise CliError(
            "Set at least one of --max-gpus, --max-cost-per-day, --max-nodes or --allowed-clouds.",
            "usage_error",
            1,
        )
    preview = mutation_guard(
        args,
        "superplane quota set",
        {"workspace_id": identifier, "quota": body},
        f"Change quota for workspace {identifier}?",
    )
    if preview:
        return preview
    return common.envelope("ok", "superplane quota set", api.request("PATCH", path, body))


# --- reused: account and AWS onboarding --------------------------------------


def register_account_reference(args, api, verb):
    credential_id = args.credential_id
    if "arn:" in credential_id.lower() or "secret" in credential_id.lower():
        raise CliError(
            "--credential-id takes the opaque ADP credential id from adp aws connect, not a secret ARN. Nothing was sent.",
            "usage_error",
            1,
        )
    body = {
        "name": args.name or args.account_id,
        "provider": "aws",
        "account_id": args.account_id,
        "adp_credential_id": credential_id,
    }
    preview = mutation_guard(
        args,
        verb,
        body,
        f"Register AWS account {args.account_id} with Superplane?",
    )
    if preview:
        return preview
    try:
        support = api.request("GET", ACCOUNT_ADAPTER_SUPPORT)
    except CliError as exc:
        if exc.status_code != 404:
            raise
        support = {}
    if not isinstance(support, dict) or "account-vault-reference-v1" not in (support.get("features") or []):
        raise CliError(
            "This gateway does not support Superplane account registration from an ADP connection. Update the server; nothing was written.",
            "account_adapter_unavailable",
            4,
        )
    progress(f"Registering AWS account {args.account_id}...")
    result = api.request("POST", API_BASE + "/accounts", body)
    if not isinstance(result, dict) or not result.get("id") or result.get("account_id") != args.account_id:
        raise CliError(
            "Superplane returned a malformed account response. The registration may have succeeded; run adp superplane account list before retrying.",
            "malformed_response",
            5,
        )
    references = result.get("adp_credential_ids") or []
    if credential_id not in references:
        raise CliError(
            "Superplane did not confirm the requested ADP connection reference. "
            "The registration may have succeeded; run adp superplane account list "
            "before retrying.",
            "malformed_response",
            5,
        )
    public_result = {key: value for key, value in result.items() if key not in {"role_arn", "external_id"}}
    return common.envelope("ok", verb, {"account": public_result})


def account(args, api):
    if args.subcommand == "list":
        result = api.request("GET", API_BASE + "/accounts")
        accounts = [
            {key: value for key, value in row.items() if key not in {"role_arn", "external_id"}}
            for row in result.get("accounts") or []
            if isinstance(row, dict)
        ]
        return common.envelope("ok", "superplane account list", {"accounts": accounts})

    if args.subcommand == "delete":
        # The route is DELETE /accounts/{account_id} with `account_id: uuid.UUID`
        # — the domain RECORD's id (app/routers/accounts.py), not the 12-digit
        # cloud account number this verb used to send, which the server answered
        # 422 for. Resolved from the list so the existing argument keeps working.
        identifier = args.account_id
        if not looks_like_uuid(identifier):
            listed = api.request("GET", API_BASE + "/accounts").get("accounts") or []
            matches = [str(row.get("id")) for row in listed if isinstance(row, dict) and identifier in (row.get("account_id"), row.get("name"))]
            if not matches:
                raise CliError(
                    f"No registered account matches {identifier!r}. List them with adp superplane account list.",
                    "account_not_found",
                    1,
                )
            if len(set(matches)) > 1:
                raise CliError(
                    f"{identifier!r} matches {len(set(matches))} registrations "
                    f"({', '.join(sorted(set(matches)))}). Pass the one you mean. Nothing was changed.",
                    "account_ambiguous",
                    1,
                )
            identifier = matches[0]
        preview = mutation_guard(
            args,
            "superplane account delete",
            {"account": args.account_id, "domain_record": identifier},
            f"Deregister account {args.account_id!r}?",
        )
        if preview:
            return preview
        progress(f"Deregistering account {args.account_id}...")
        api.request("DELETE", f"{API_BASE}/accounts/{segment(identifier)}")
        return common.envelope("ok", "superplane account delete", {"deregistered": args.account_id})

    return register_account_reference(args, api, "superplane account onboard")


def aws_onboard(args, api):
    """Register an AWS account ADP has already connected.

    The CLI sends only the opaque ADP connection id. The gateway authorizes that
    reference and resolves the domain-required role ARN and ExternalId without
    returning either value to this process or creating another IAM role.
    """
    return register_account_reference(args, api, "superplane aws-onboard register")


# --- replaced: provider credentials go to ADP's vault ------------------------


def validate_domain_credential(record, *, source="Superplane credential response"):
    if not isinstance(record, dict):
        raise CliError(
            f"{source} contains a non-object credential. Nothing was changed.",
            "malformed_response",
            5,
        )

    record_id = record.get("id")
    try:
        uuid.UUID(str(record_id))
    except (TypeError, ValueError, AttributeError):
        raise CliError(
            f"{source} contains an invalid domain credential id. Nothing was changed.",
            "malformed_response",
            5,
        ) from None

    reference = record.get("adp_credential_id")
    if not isinstance(reference, str) or not reference.strip() or reference != reference.strip():
        raise CliError(
            f"{source} does not contain a usable ADP vault reference. Nothing was changed.",
            "malformed_response",
            5,
        )
    return record


def list_domain_credentials(api):
    response = api.request("GET", API_BASE + DOMAIN_CREDENTIALS)
    if not isinstance(response, dict) or not isinstance(response.get("credentials"), list):
        raise CliError(
            "Superplane returned a malformed credential-list response. Nothing was changed.",
            "malformed_response",
            5,
        )
    total = response.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < len(response["credentials"]):
        raise CliError(
            "Superplane returned an invalid credential-list total. Nothing was changed.",
            "malformed_response",
            5,
        )
    records = [validate_domain_credential(row, source="Superplane credential list") for row in response["credentials"]]
    return records, total


def find_domain_credential(api, wanted, receipt=None):
    """Locate the domain record for a credential, by either of its two ids.

    This is the fix for the defect at the centre of this verb (Issue #5637): there
    are TWO identifiers and the old code used one value for both requests.

    * The domain record's own `id` is a UUID minted by the domain and is what
      DELETE /vault/credentials/{credential_id} takes (app/routers/accounts.py).
    * `adp_credential_id` is the opaque ADP vault reference the record POINTS AT,
      and is what DELETE /auth/credentials/{id} in ADP's vault takes.

    Sending one id to both endpoints means one of the two deletes addresses
    something that is not there — and in the direction that matters, it leaves a
    live secret in the vault while the command reports success.

    Matching on either id so an operator can name whichever one they have.
    """
    listed, _ = list_domain_credentials(api)
    if receipt is not None:
        expected_record = str(receipt.get("domain_record", ""))
        expected_reference = receipt.get("adp_credential_id")
        linked = [row for row in listed if str(row.get("id")) == expected_record or row.get("adp_credential_id") == expected_reference]
        exact = [row for row in linked if str(row.get("id")) == expected_record and row.get("adp_credential_id") == expected_reference]
        if linked and (len(linked) != 1 or len(exact) != 1):
            raise CliError(
                "The provider delete recovery receipt no longer identifies the live domain registration. "
                "Nothing was deleted and the receipt was retained for manual reconciliation.",
                "provider_recovery_conflict",
                5,
            )
        return exact[0] if exact else None

    matches = [row for row in listed if wanted in (str(row.get("id")), row.get("adp_credential_id"))]
    if not matches:
        return None
    if len({str(row.get("id")) for row in matches}) > 1:
        raise CliError(
            f"{wanted!r} matches more than one registered credential. Pass the domain record id. Nothing was changed.",
            "provider_ambiguous",
            1,
        )
    return matches[0]


def require_matching_domain_credential(record, receipt):
    expected = {
        "adp_credential_id": receipt["adp_credential_id"],
        "name": receipt["name"],
        "provider": receipt["provider"],
        "credential_type": receipt["credential_type"],
    }
    mismatched = [key for key, value in expected.items() if record.get(key) != value]
    if mismatched:
        raise CliError(
            "The domain credential reference resolves to different registration metadata "
            f"({', '.join(mismatched)}). Nothing was registered or deleted.",
            "provider_recovery_conflict",
            5,
        )


def provider_delete(args, api):
    """Remove the domain record, then the vault credential it points at.

    The two deletes are separate requests and cannot be atomic, so the only
    honest design is to make the second one RESUMABLE and to report which half
    happened. Order matters: the vault credential is the secret, so it must not
    be left behind silently. The domain record goes first because it only ever
    held a reference — losing it leaves a real credential the user can still
    retry on, whereas the reverse leaves a domain row pointing at nothing.

    A retry after a partial failure must finish the job, so an already-gone
    domain row is treated as "that half is done" and the vault delete still runs.
    Only that case is tolerated: any other domain failure, and EVERY vault
    failure, stays visible and non-zero (Issue #5039).

    The vault reference is read from the domain record BEFORE anything is deleted.
    Reading it afterwards would be impossible — the record carrying it is gone —
    which is the other half of why the old single-id version could orphan a secret.
    """
    current_context = current_recovery_context(api)
    receipt = find_provider_delete_recovery(args.credential_id)
    if receipt is not None:
        require_recovery_context(receipt, current_context)
    record = find_domain_credential(api, args.credential_id, receipt=receipt)
    resumed = False
    if record is None and receipt is not None:
        record = validate_domain_credential(
            {
                "id": receipt.get("domain_record"),
                "adp_credential_id": receipt.get("adp_credential_id"),
            },
            source="Provider delete recovery receipt",
        )
        resumed = True
    progress(f"Removing provider credential {args.credential_id}...")

    if record is None:
        # Nothing in the domain to remove. The vault reference lived in that
        # record, so there is no id left to delete safely — and guessing that the
        # argument IS the vault reference is exactly how an unrelated credential
        # gets deleted. Refuse rather than delete something we cannot attribute.
        raise CliError(
            f"No registered provider credential matches {args.credential_id!r}. Nothing was deleted. "
            "List them with adp superplane provider list; if a vault credential was left behind by an "
            "interrupted add, remove it with adp's vault credential tooling using the id from that run's receipt.",
            "provider_not_found",
            1,
        )

    record_id = str(record["id"])
    reference = record["adp_credential_id"]

    preview = mutation_guard(
        args,
        "superplane provider delete",
        {
            "credential": args.credential_id,
            "domain_record": record_id,
            "adp_credential_id": reference,
        },
        f"Delete provider credential {args.credential_id!r} from Superplane and ADP's vault?",
    )
    if preview:
        return preview

    if receipt is None:
        receipt = {
            "domain_record": record_id,
            "adp_credential_id": reference,
            "requested_as": args.credential_id,
            "recovery_context": current_context,
        }
    save_provider_delete_recovery(receipt)

    metadata = "already_absent" if resumed else "deleted"
    if not resumed:
        try:
            api.request("DELETE", f"{API_BASE}{DOMAIN_CREDENTIALS}/{segment(record_id)}")
        except CliError as exc:
            if exc.status_code == 404 or exc.code == "not_found":
                metadata = "already_absent"
                progress("The domain record was already gone; continuing to the vault credential.")
            elif exc.code == "gateway_unavailable" or exc.status_code is None or exc.status_code >= 500:
                raise CliError(
                    f"Deleting domain record {record_id} had an uncertain result, so vault credential {reference} "
                    f"was NOT deleted. Retry adp superplane provider delete {record_id} --yes; the private recovery "
                    "receipt will finish only this credential after reconciling domain state.",
                    "provider_delete_uncertain",
                    5,
                ) from None
            else:
                clear_provider_delete_recovery(record_id)
                raise

    progress("Deleting the credential from ADP's vault...")
    try:
        api.request("DELETE", f"{VAULT_CREDENTIALS}/{segment(reference)}")
    except CliError as exc:
        vault = "already_absent" if exc.status_code == 404 or exc.code == "not_found" else None
        if vault is None:
            # The secret may still exist. Say so, and say what to do about it —
            # the domain row is gone, so `provider list` will no longer show it.
            raise CliError(
                f"The domain record for {args.credential_id} was {metadata}, but deleting the vault "
                f"credential failed: {exc}. The stored secret may still exist under ADP credential id "
                f"{reference} and will no longer appear in adp superplane provider list. Retry "
                f"adp superplane provider delete {record_id} --yes; the private receipt retains only "
                "this domain/vault ID pair.",
                "provider_delete_incomplete",
                5,
            ) from None
    else:
        vault = "deleted"

    clear_provider_delete_recovery(record_id)

    return common.envelope(
        "ok",
        "superplane provider delete",
        {
            "deleted": args.credential_id,
            "domain_record": record_id,
            "domain_metadata": metadata,
            "vault_credential": vault,
        },
    )


def registration_receipt(args, credential_id, *, api=None):
    return {
        "adp_credential_id": credential_id,
        "name": args.name,
        "provider": args.provider,
        "vault_credential_type": args.type,
        "credential_type": DOMAIN_CREDENTIAL_TYPES[args.type],
        "phase": "vault_pending",
        "recovery_context": current_recovery_context(api),
    }


def find_vault_credential(api, credential_id):
    listed = api.request("GET", VAULT_CREDENTIALS)
    rows = listed if isinstance(listed, list) else []
    matches = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == credential_id]
    if len(matches) > 1:
        raise CliError(
            "ADP's vault returned the same credential id more than once.",
            "malformed_response",
        )
    return matches[0] if matches else None


def vault_credential_type(receipt):
    return receipt.get("vault_credential_type") or {"service_account": "config_file"}.get(receipt["credential_type"], receipt["credential_type"])


def require_matching_vault_credential(stored, receipt):
    expected = {
        "id": receipt["adp_credential_id"],
        "service": receipt["provider"],
        "label": receipt["name"],
        "credential_type": vault_credential_type(receipt),
        "scope": "user",
    }
    mismatched = [key for key, value in expected.items() if stored.get(key) != value]
    if mismatched:
        raise CliError(
            f"The vault operation id resolves to different credential metadata ({', '.join(mismatched)}). Nothing was registered or deleted.",
            "provider_recovery_conflict",
            5,
        )


def store_vault_credential(api, receipt, value, *, recovery=False):
    credential_id = receipt["adp_credential_id"]
    body = {
        "service": receipt["provider"],
        "label": receipt["name"],
        "credential_type": vault_credential_type(receipt),
        "value": value,
        "scope_hint": "user",
    }
    try:
        stored = api.request("PUT", f"{VAULT_CREDENTIALS}/{segment(credential_id)}", body)
        if not isinstance(stored, dict) or str(stored.get("id")) != credential_id:
            raise CliError(
                "ADP's vault returned an incomplete success response; storage may have committed.",
                "gateway_unavailable",
            )
    except CliError as exc:
        if exc.status_code == 409:
            # The deterministic operation secret may survive a metadata commit
            # conflict. Metadata alone also cannot disprove a secret mismatch.
            save_provider_recovery({**receipt, "phase": "vault_conflict"})
            raise CliError(
                "The vault operation conflicted with existing state. Its secret may already exist even when "
                "credential metadata is absent. The recovery receipt was retained. "
                f"Retry only this operation with adp superplane provider add --recover {credential_id} --stdin --yes "
                "and the original secret; do not start a new add or infer that the secret was removed.",
                "provider_vault_conflict",
                5,
                status_code=409,
            ) from None
        if exc.status_code in (404, 405):
            retain_receipt = recovery or receipt.get("phase") == "vault_conflict"
            if not retain_receipt:
                clear_provider_recovery(credential_id)
            raise CliError(
                (
                    "This gateway cannot accept the recovery PUT. The original secret may still exist, so its "
                    f"receipt was retained. Restore the gateway and retry adp superplane provider add --recover {credential_id} --stdin --yes."
                    if retain_receipt
                    else "This gateway does not support idempotent vault writes, so provider add stopped before storing the secret. "
                    "Upgrade the gateway and retry."
                ),
                "vault_idempotency_unavailable",
                4,
            ) from None
        definitive = exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code not in (408, 425, 429)
        if definitive:
            if not recovery and receipt.get("phase") != "vault_conflict":
                clear_provider_recovery(credential_id)
            raise
        try:
            stored = find_vault_credential(api, credential_id)
        except CliError:
            stored = None
        if stored is None or receipt.get("phase") == "vault_conflict":
            raise CliError(
                "Vault storage delivery is uncertain. The operation was not repeated with a new identity. "
                f"Recovery receipt: ADP credential id {credential_id}, name {receipt['name']!r}, provider "
                f"{receipt['provider']!r}. Retry the same operation with adp superplane provider add --recover "
                f"{credential_id} --stdin --yes, supplying the same secret only if requested.",
                "provider_vault_uncertain",
                5,
            ) from None

    require_matching_vault_credential(stored, receipt)
    receipt = {**receipt, "phase": "domain_pending"}
    save_provider_recovery(receipt)
    return receipt


def complete_provider_registration(api, receipt, *, compensate):
    """Register or reconcile one exact vault reference without blind cleanup."""
    credential_id = receipt["adp_credential_id"]
    body = {
        "name": receipt["name"],
        "provider": receipt["provider"],
        "credential_type": receipt["credential_type"],
        "adp_credential_id": credential_id,
    }
    try:
        registered = api.request("POST", API_BASE + DOMAIN_CREDENTIALS, body)
        registered = validate_domain_credential(registered, source="Superplane registration response")
        require_matching_domain_credential(registered, receipt)
    except CliError as exc:
        definitive = exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code not in (408, 409, 425, 429)
        if not definitive:
            try:
                registered = find_domain_credential(api, credential_id)
            except CliError:
                registered = None
            if registered is not None:
                require_matching_domain_credential(registered, receipt)
                clear_provider_recovery(credential_id)
                return registered, True
            raise CliError(
                "Superplane registration delivery is uncertain. The vault credential was NOT deleted because "
                f"the domain may already reference it. Recovery receipt: ADP credential id {credential_id}, "
                f"name {receipt['name']!r}, provider {receipt['provider']!r}. Reconcile the same operation with "
                f"adp superplane provider add --recover {credential_id} --yes; do not add the secret again.",
                "provider_add_uncertain",
                5,
            ) from None

        if not compensate:
            raise
        progress("Registration was rejected; removing the credential this run just stored...")
        try:
            api.request("DELETE", f"{VAULT_CREDENTIALS}/{segment(credential_id)}")
        except CliError as cleanup:
            if cleanup.status_code == 404 or cleanup.code == "not_found":
                clear_provider_recovery(credential_id)
                raise CliError(
                    f"Registering the credential with Superplane failed: {exc}. The vault credential was already absent, "
                    "so nothing was left behind and it is safe to retry.",
                    "provider_add_rolled_back",
                    5,
                ) from None
            raise CliError(
                f"Registering the credential with Superplane failed ({exc}), and removing the credential "
                f"this run had just stored in ADP's vault ALSO failed ({cleanup}). Recovery receipt: ADP "
                f"credential id {credential_id}. Remove it with ADP vault credential tooling.",
                "provider_add_incomplete",
                5,
            ) from None
        clear_provider_recovery(credential_id)
        raise CliError(
            f"Registering the credential with Superplane failed: {exc}. The credential this run stored in "
            "ADP's vault has been removed, so nothing was left behind and it is safe to retry.",
            "provider_add_rolled_back",
            5,
        ) from None

    clear_provider_recovery(credential_id)
    return registered, False


def provider_add(args, api):
    """Store the value in ADP's vault, then register only its reference.

    Two writes that cannot be one. The recovery behaviour here is the point
    (Issue #5637): if the vault write succeeds and the domain registration fails,
    an untracked secret is left in the vault that `provider list` will never show.

    The CLI records a caller-generated UUID before the first write and uses the
    vault's idempotent PUT contract, so even a lost first response is resumable.
    A definitive second-stage failure compensates only that UUID. It never
    searches for something that looks similar, and never deletes by label: a
    retried add must not be able to remove a credential another run or another
    person owns. If the compensating delete itself fails, the command exits
    non-zero with a receipt naming the exact id to remove, because a silent
    "ok" over a leaked secret is the worst available outcome.
    """
    if args.recover:
        receipt = provider_recoveries().get(args.recover)
        if not isinstance(receipt, dict):
            raise CliError(
                f"No local recovery receipt exists for {args.recover!r}. Nothing was sent.",
                "provider_recovery_not_found",
                1,
            )
        require_recovery_context(receipt, api=api)
        preview = mutation_guard(
            args,
            "superplane provider add",
            {"recovery": receipt},
            f"Reconcile provider registration for ADP credential {args.recover}?",
        )
        if preview:
            return preview
        stored = find_vault_credential(api, args.recover)
        replay_conflict = receipt.get("phase") == "vault_conflict"
        if replay_conflict and stored is not None:
            require_matching_vault_credential(stored, receipt)
        if stored is None or replay_conflict:
            if not args.stdin:
                raise CliError(
                    "The recovery receipt requires confirmation of the original vault write. Metadata visibility "
                    "does not prove the secret is absent or that a conflicted write succeeded. "
                    f"Rerun adp superplane provider add --recover {args.recover} --stdin --yes with the original secret; "
                    "the command will reuse the receipt's exact operation id.",
                    "provider_secret_required",
                    4,
                )
            value = read_provider_value(True, "recovery credential")
            try:
                receipt = store_vault_credential(api, receipt, value, recovery=True)
            finally:
                del value
        elif args.stdin:
            raise CliError(
                "The vault credential already exists; omit --stdin so the recovery cannot replace or duplicate it.",
                "usage_error",
                1,
            )
        else:
            require_matching_vault_credential(stored, receipt)
            receipt = {**receipt, "phase": "domain_pending"}
            save_provider_recovery(receipt)
        existing = find_domain_credential(api, args.recover)
        if existing is not None:
            require_matching_domain_credential(existing, receipt)
            clear_provider_recovery(args.recover)
            registered, reconciled = existing, True
        else:
            registered, reconciled = complete_provider_registration(api, receipt, compensate=False)
        return common.envelope(
            "ok",
            "superplane provider add",
            {
                "adp_credential_id": args.recover,
                "domain_record": registered.get("id"),
                "name": receipt["name"],
                "provider": receipt["provider"],
                "reconciled": reconciled,
            },
        )

    if not args.name or not args.provider:
        raise CliError(
            "Provider add requires --name and --provider unless --recover is used.",
            "usage_error",
            1,
        )
    preview = mutation_guard(
        args,
        "superplane provider add",
        {"name": args.name, "provider": args.provider, "credential_type": args.type},
        f"Store and register provider credential {args.name!r}?",
    )
    if preview:
        return preview

    value = read_provider_value(args.stdin, f"{args.provider} {args.type.replace('_', ' ')}")

    credential_id = str(uuid.uuid4())
    receipt = registration_receipt(args, credential_id, api=api)
    save_provider_recovery(receipt)

    # The caller-generated UUID makes the first write idempotent. Its non-secret
    # intent receipt exists before delivery, so a lost response can reconcile the
    # exact operation instead of searching or deleting by label.
    progress(f"Storing the {args.provider} credential in ADP's vault...")
    try:
        receipt = store_vault_credential(api, receipt, value)
    finally:
        del value

    # Only the reference crosses into the domain — never the value, never an ARN.
    # Field names are the domain's: `name`, `provider`, `adp_credential_id`
    # (RegisterCredentialRequest). `credential_id` was this helper's own invention.
    progress("Registering the credential reference with Superplane...")
    registered, reconciled = complete_provider_registration(api, receipt, compensate=True)

    return common.envelope(
        "ok",
        "superplane provider add",
        {
            "adp_credential_id": credential_id,
            "domain_record": registered.get("id") if isinstance(registered, dict) else None,
            "name": args.name,
            "provider": args.provider,
            "reconciled": reconciled,
        },
        "Verify with adp superplane provider list.",
    )


def provider(args, api):
    if args.subcommand == "list":
        providers, total = list_domain_credentials(api)
        return common.envelope(
            "ok",
            "superplane provider list",
            {"providers": providers, "total": total},
        )

    if args.subcommand == "delete":
        return provider_delete(args, api)

    return provider_add(args, api)


# --- redirected: ADP owns organization and user administration ---------------


def redirect(verb):
    """Report where the administration lives. Performs no administration.

    Returns `unavailable` (exit 4), not `ok`: a script that pipes `adp superplane
    org update ...` must not read a zero exit as "the change was applied".
    """
    path, reason = REDIRECTED[verb]
    return common.envelope(
        "unavailable",
        f"superplane {verb}",
        {"redirected_to": path, "reason": reason, "performed": "nothing"},
        f"Use ADP: {path} in the console, or the ADP admin API. This CLI performs no {verb} administration.",
    )


# --- parsing -----------------------------------------------------------------


def parser():
    root = common.Parser(
        prog="adp superplane",
        description="Superplane workloads, using your existing ADP login.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    # --json belongs on the leaf, so `adp superplane workspace list --json` works
    # (a root-only flag would have to precede the subcommand). A shared parent
    # parser is argparse's own idiom for that and keeps one definition.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="Print machine-readable JSON on stdout")

    def leaf(subcommands, name, **kwargs):
        return subcommands.add_parser(name, parents=[shared], **kwargs)

    def mutation(command):
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="Show the resolved operation without changing anything",
        )
        command.add_argument(
            "--yes",
            action="store_true",
            help="Approve the fully specified operation without a prompt",
        )
        return command

    workspace_command = commands.add_parser("workspace", help="Create, list, select and describe workspaces")
    workspace_subcommands = workspace_command.add_subparsers(dest="subcommand", required=True)
    create_workspace = mutation(leaf(workspace_subcommands, "create", help="Create a workspace"))
    create_workspace.add_argument("--name", required=True)
    create_workspace.add_argument("--operation-id", help="Stable request UUID; retain externally and reuse unchanged after client loss")
    create_workspace.add_argument(
        "--isolation",
        default="dedicated",
        choices=("dedicated", "namespace", "research"),
    )
    create_workspace.add_argument("--account", help="AWS account name or id (required for research isolation)")
    create_workspace.add_argument("--budget-daily", type=float, dest="budget_daily", help="Daily spend cap in USD")
    create_workspace.add_argument("--budget-gpus", type=int, dest="budget_gpus", help="Maximum GPUs")
    leaf(workspace_subcommands, "list", help="List workspaces")
    use_workspace = leaf(workspace_subcommands, "use", help="Select the workspace later verbs default to")
    use_workspace.add_argument("name")
    describe_workspace = leaf(workspace_subcommands, "describe", help="Describe the selected workspace")
    describe_workspace.add_argument("--workspace")
    kubeconfig = leaf(
        workspace_subcommands,
        "kubeconfig",
        help="Print kubectl access for the workspace",
    )
    kubeconfig.add_argument("--workspace")

    node_command = commands.add_parser("node", parents=[shared], help="List nodes in a workspace")
    node_command.add_argument("--workspace")

    quota_command = commands.add_parser("quota", help="Show or set a workspace's quota")
    quota_subcommands = quota_command.add_subparsers(dest="subcommand", required=True)
    show_quota = leaf(quota_subcommands, "show", help="Show the workspace quota")
    show_quota.add_argument("--workspace")
    set_quota = mutation(leaf(quota_subcommands, "set", help="Set the workspace quota"))
    set_quota.add_argument("--workspace")
    set_quota.add_argument("--max-gpus", type=int, dest="max_gpus")
    set_quota.add_argument("--max-cost-per-day", type=float, dest="max_cost_per_day")
    set_quota.add_argument("--max-nodes", type=int, dest="max_nodes")
    set_quota.add_argument(
        "--allowed-clouds",
        dest="allowed_clouds",
        help="Comma-separated, for example aws,lambda",
    )

    cost_command = commands.add_parser("cost", parents=[shared], help="Show workspace or organization cost")
    cost_command.add_argument("--workspace")
    cost_command.add_argument("--org", action="store_true", help="Show cost across the whole organization")
    cost_command.add_argument("--start-date", dest="start_date", help="Start of the cost window (ISO 8601)")
    cost_command.add_argument("--end-date", dest="end_date", help="End of the cost window (ISO 8601)")

    # Only the filters GET /events actually declares. `--workspace` is deliberately
    # NOT among them and is refused in main(): events carry a resource_type and a
    # resource id, not a workspace scope, so accepting the flag and dropping it
    # server-side showed every workspace's events as though they were scoped.
    events_command = commands.add_parser("events", parents=[shared], help="Show recent audit events")
    events_command.add_argument(
        "--resource-type",
        dest="resource_type",
        help="For example workspace, credential, deployment",
    )
    events_command.add_argument("--user", help="Filter by user id")
    events_command.add_argument("--action", help="created, updated, deleted or read")
    events_command.add_argument(
        "--event-type",
        dest="event_type",
        help="api_call, credential_access or lifecycle",
    )
    events_command.add_argument("--start-time", dest="start_time", help="Start of the time range (ISO 8601)")
    events_command.add_argument("--end-time", dest="end_time", help="End of the time range (ISO 8601)")
    events_command.add_argument("--limit", type=int, default=50, help="Maximum events to return (1-500)")
    events_command.add_argument("--offset", type=int, default=None, help="Pagination offset")

    deploy_command = commands.add_parser("deploy", help="Review, approve, create, list and tear down model deployments")
    deploy_subcommands = deploy_command.add_subparsers(dest="subcommand", required=True)
    for name, help_text in (
        ("preview", "Review a model deployment and optionally request approval"),
        ("create", "Submit the exact approved model deployment"),
    ):
        create_deploy = mutation(leaf(deploy_subcommands, name, help=help_text))
        create_deploy.add_argument("--model", required=True, help="Model to serve, for example a HuggingFace name")
        create_deploy.add_argument("--namespace", help="Assert the workspace-owned namespace; never override placement")
        create_deploy.add_argument("--precision", default="fp16", choices=("fp8", "fp16", "bf16", "awq", "int8"))
        create_deploy.add_argument("--name", required=True, help="Deployment name (lowercase letters, digits, hyphens)")
        create_deploy.add_argument("--workspace")
        create_deploy.add_argument("--operation-id", required=True, help="Request UUID; reuse unchanged from preview through submission and recovery")
        create_deploy.add_argument("--profile-id", required=True, help="Configured serving profile with reviewed image and authentication")
        create_deploy.add_argument("--serving-framework", choices=("vllm", "sglang"), help="Defaults to the server's choice")
        create_deploy.add_argument("--replicas", type=int, help="The current controller supports one replica")
        create_deploy.add_argument("--gpu-per-replica", type=int, help="1-8")
        create_deploy.add_argument("--tensor-parallel-size", type=int, help="1-8")
        create_deploy.add_argument("--max-model-len", type=int, help="Maximum context length")
        create_deploy.add_argument("--plan-revision", required=name == "create", help="Exact revision returned by preview")
        if name == "preview":
            create_deploy.add_argument(
                "--request-approval", action="store_true", help="Issue the reviewed request for a human decision; requires --plan-revision"
            )
        else:
            create_deploy.add_argument("--approval-id", required=True, help="Approved operation request UUID")
    list_deploy = leaf(deploy_subcommands, "list", help="List deployments")
    list_deploy.add_argument("--workspace")
    for name, help_text in (
        ("teardown-preview", "Review teardown and optionally request approval"),
        ("delete", "Submit the exact approved deployment teardown"),
    ):
        delete_deploy = mutation(leaf(deploy_subcommands, name, help=help_text))
        delete_deploy.add_argument("--id", required=True, help="Deployment UUID returned by create or list")
        delete_deploy.add_argument("--workspace")
        delete_deploy.add_argument(
            "--operation-id", required=True, help="Teardown request UUID; reuse unchanged through preview, submission and recovery"
        )
        delete_deploy.add_argument("--plan-revision", required=name == "delete", help="Exact revision returned by teardown-preview")
        if name == "teardown-preview":
            delete_deploy.add_argument(
                "--request-approval", action="store_true", help="Issue the reviewed teardown for a human decision; requires --plan-revision"
            )
        else:
            delete_deploy.add_argument("--approval-id", required=True, help="Approved teardown request UUID")

    account_command = commands.add_parser("account", help="Register, list and deregister cloud accounts")
    account_subcommands = account_command.add_subparsers(dest="subcommand", required=True)
    onboard_account = mutation(leaf(account_subcommands, "onboard", help="Register a cloud account"))
    onboard_account.add_argument("--name", required=True)
    onboard_account.add_argument("--provider", required=True, choices=("aws",))
    onboard_account.add_argument("--account-id", dest="account_id", required=True)
    onboard_account.add_argument(
        "--credential-id",
        dest="credential_id",
        required=True,
        help="The ADP credential id from adp aws connect (see adp aws list)",
    )
    leaf(account_subcommands, "list", help="List registered accounts")
    delete_account = mutation(leaf(account_subcommands, "delete", help="Deregister an account"))
    delete_account.add_argument("account_id")

    # No `plan` subcommand: it called a GET /aws/onboarding-plan that exists
    # neither upstream nor in the design's endpoint table (Issue #5039). The
    # connection itself is created by `adp aws connect`, which is where the
    # CloudFormation template and the role already live.
    aws_command = commands.add_parser("aws-onboard", help="Register an AWS account ADP has already connected")
    aws_subcommands = aws_command.add_subparsers(dest="subcommand", required=True)
    register_aws = mutation(
        leaf(
            aws_subcommands,
            "register",
            help="Register an AWS account already connected to ADP",
        )
    )
    register_aws.add_argument("--account-id", dest="account_id", required=True)
    # The vault credential id `adp aws connect` returned — the handle `adp aws`
    # itself uses. Required: registering an account with no reference to a
    # connection is what produced an unverifiable registration before.
    register_aws.add_argument(
        "--credential-id",
        dest="credential_id",
        required=True,
        help="The ADP credential id from adp aws connect (see adp aws list)",
    )
    register_aws.add_argument("--name", help="Account name as Superplane should record it")

    provider_command = commands.add_parser("provider", help="Provider credentials, stored in ADP's vault")
    provider_subcommands = provider_command.add_subparsers(dest="subcommand", required=True)
    add_provider = mutation(
        leaf(
            provider_subcommands,
            "add",
            help="Store a provider credential in ADP's vault (prompted, never on the command line)",
        )
    )
    add_provider.add_argument("--name", help="Label for this credential")
    add_provider.add_argument("--provider", help="Provider, for example nebius or lambda")
    add_provider.add_argument(
        "--type",
        default="api_key",
        choices=("api_key", "oauth_token", "bearer", "basic_auth", "config_file"),
        help="Credential type as ADP's vault records it",
    )
    add_provider.add_argument(
        "--stdin",
        action="store_true",
        help="Read the value from stdin instead of prompting",
    )
    add_provider.add_argument(
        "--recover",
        metavar="ADP_CREDENTIAL_ID",
        help="Reconcile a prior uncertain registration using its recovery receipt",
    )
    leaf(
        provider_subcommands,
        "list",
        help="List provider credentials registered with Superplane",
    )
    delete_provider = mutation(leaf(provider_subcommands, "delete", help="Remove a provider credential"))
    delete_provider.add_argument(
        "credential_id",
        help="The domain record id from provider list, or the ADP credential id it references",
    )

    # Listed so the redirect is discoverable in `--help`. Their arguments are
    # never parsed — main() intercepts these verbs first (see there for why).
    for verb in REDIRECTED:
        commands.add_parser(verb, add_help=False, help=f"Redirected: ADP administers {verb}s")

    # Onboarding is a separate helper file, and it is advertised only when that
    # file actually shipped. An install missing it must not list a verb that then
    # fails — the same rule `adp-admin.py` applies to its sub-areas.
    research = common.load_provider("adp-superplane-research.py")
    if research:
        commands.add_parser("research", parents=[research.parser()], add_help=False, help="Read research and review exact proposal revisions")
    if Path(__file__).with_name(ONBOARDING_HELPER).is_file():
        commands.add_parser("onboarding", add_help=False, help="Discover, plan and bind workspace and provider onboarding")

    if Path(__file__).with_name(LIFECYCLE_HELPER).is_file():
        lifecycle_helper().configure(commands, workspace_subcommands, deploy_subcommands, leaf, mutation, events_command)
    return root


HANDLERS = {
    "cluster": lambda args, api: lifecycle_helper().cluster_list(args, api),
    "provider-connection": lambda args, api: lifecycle_helper().provider_connection(args, api),
    "workspace": workspace,
    "node": node,
    "quota": quota,
    "cost": cost,
    "events": events,
    "deploy": deploy,
    "account": account,
    "aws-onboard": aws_onboard,
    "provider": provider,
}


def run(args, api):
    if args.command in REDIRECTED:
        return redirect(args.command)
    return HANDLERS[args.command](args, SessionApi(api))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    command = "superplane " + (argv[0] if argv and not argv[0].startswith("-") else "")
    try:
        # Before this file's own secret check, because the onboarding helper runs a
        # STRICTER one: onboarding takes no secret at any time and refuses outright,
        # whereas this file offers a prompt for the verbs that legitimately accept
        # one. Rejecting here first would answer with the wrong remedy — "type it at
        # the prompt instead" — for a surface that has no such prompt. The helper's
        # check is a superset of this one and is the first thing its main() runs.
        if argv and argv[0] == "research":
            module = common.load_provider("adp-superplane-research.py")
            if not module:
                raise CliError("Research helper is missing. Run adp update.", "provider_unavailable", 4)
            return module.main(argv[1:])
        if argv and argv[0] == "onboarding":
            module = common.load_provider(ONBOARDING_HELPER)
            if not module:
                raise CliError("Workspace onboarding is not installed. Run adp update.", "provider_unavailable", 4)
            return module.main(argv[1:])
        # Before argparse: a rejected secret flag must be reported as the leak it
        # is, not as an unrecognized argument.
        reject_secret_arguments(argv)
        # Also before argparse: a filter that was silently dropped server-side must
        # name that, not fail as an unknown flag (Issue #5637).
        if not Path(__file__).with_name(LIFECYCLE_HELPER).is_file():
            reject_ignored_event_filter(argv)
        # Also before argparse, and before any gateway or token is resolved: a
        # redirected verb performs nothing, so neither its arguments nor the
        # user's session are relevant. Parsing them would mean either rebuilding
        # the upstream org/user flag surface — the administration surface the
        # design retires — or failing valid upstream invocations with a usage
        # error instead of telling the caller where the administration actually
        # lives.
        if argv and argv[0] in REDIRECTED:
            return common.emit(redirect(argv[0]), as_json)
        args = parser().parse_args(argv)
        return common.emit(run(args, LazyApi()), args.json)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, command, as_json)
    except (KeyboardInterrupt, EOFError):
        return common.report_error(CliError(CANCELLED, "interrupted", 130), command, as_json)


if __name__ == "__main__":
    sys.exit(main())
