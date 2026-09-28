"""HTTP client for adp-trigger — SigV4-signed calls to the agent plane.

Reads lineage context from the pod environment and constructs the request
bodies expected by the agent-plane handlers.

Environment variables (required for the spawn path — set by entrypoint.py from
the SQS envelope):
  ADP_CORRELATION_ID  — current chain's correlation ID
  ADP_MESSAGE_ID      — this run's invocation ID (becomes parent_invocation_id)
  ADP_CHAIN_DEPTH     — current depth in the chain (informational)
  ADP_TRIGGER_ENDPOINT — full URL of the /agent/trigger route

For the status/control paths (#5028):
  ADP_RUN_CREDENTIAL_FILE — optional path to a run credential, reread for each
                         request. A configured file takes precedence and fails
                         closed if unreadable; no stale environment fallback.
  ADP_RUN_CREDENTIAL   — credential used when no file is configured.
  ADP_AGENT_CONTROL_ENDPOINT — base URL of the agent control plane. Optional:
                         derived from ADP_TRIGGER_ENDPOINT when absent, so a
                         deployment that has not added the variable still works.

Optional:
  AWS_REGION — defaults to us-east-1

## Why the status/control paths do not send ADP_MESSAGE_ID

``build_body`` maps ``ADP_MESSAGE_ID`` to ``parent_invocation_id`` because that
is the contract ``/agent/trigger`` has today, and changing it would break every
existing caller. But a worker can rewrite its own environment, so that value is
a self-assertion: it identifies the caller only as long as the caller is honest.

The status and control paths therefore do not send an identity in the body at
all. They present ``ADP_RUN_CREDENTIAL`` in a header, and the gateway derives the
caller's invocation and attempt from inside it. Rewriting ``ADP_MESSAGE_ID``
buys nothing on those paths, and rewriting ``ADP_RUN_CREDENTIAL`` produces a
token that fails its MAC check — the key never enters the pod.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request

from lib.authenticated_http import open_authenticated as urlopen

# The header the gateway reads the caller's identity from. Must match
# ``CREDENTIAL_HEADER`` in ``modules/gateway/src/agentauth/adapter.py``.
CREDENTIAL_HEADER = "X-Adp-Run-Credential"

# Credential delivery contracts. Trusted bootstrap/renewal must provision these;
# the CLI cannot mint authority from a claimed invocation ID.
CREDENTIAL_ENV = "ADP_RUN_CREDENTIAL"
CREDENTIAL_FILE_ENV = "ADP_RUN_CREDENTIAL_FILE"
_MAX_CREDENTIAL_BYTES = 4096

# Base URL of the agent control plane, when configured explicitly.
CONTROL_ENDPOINT_ENV = "ADP_AGENT_CONTROL_ENDPOINT"

# Exit code for "this verb is not implemented in this deployment" (HTTP 501).
# Distinguished from a refusal so a calling script can tell "not built here" from
# "you may not do that" without parsing stderr.
EXIT_UNSUPPORTED = 3

# A timed-out command may have reached the worker. Keep that uncertainty
# distinct from both success and a definite refusal.
EXIT_UNKNOWN = 4


class ControlOutcomeUnknown(Exception):
    """A control request timed out; one read was attempted and no command retried."""

    def __init__(self, *, run_id: str, action: str, command_id: str, status: dict | None):
        super().__init__("control outcome is unknown")
        self.result = {
            "run_id": run_id,
            "action": action,
            "command_id": command_id,
            "command_status": "unknown",
            "detail": "The control request timed out. No command was retried.",
            "status_lookup": status,
        }


class _RequestTimeout(Exception):
    """Allow the control caller to reconcile without changing dispatch errors."""


# Read timeout. Longer than the gateway's own pod-facing timeout (5s) so a
# gateway waiting on a pod gets to return its own answer rather than having the
# CLI give up first and report a transport failure for a slow-but-successful call.
_TIMEOUT_SECONDS = 30


def get_config() -> dict:
    """Read and validate the spawn path's required environment variables.

    Returns a dict with keys: correlation_id, message_id, chain_depth,
    trigger_endpoint.

    Exits with code 2 if any required variable is missing.
    """
    required = {
        "ADP_CORRELATION_ID": "lineage context (set by entrypoint from SQS envelope)",
        "ADP_MESSAGE_ID": "this run's invocation ID (set by entrypoint from SQS envelope)",
        "ADP_CHAIN_DEPTH": "chain depth (set by entrypoint from SQS envelope)",
        "ADP_TRIGGER_ENDPOINT": "trigger API URL (set by ScaledJob pod spec)",
    }

    missing = []
    for var, desc in required.items():
        if not os.environ.get(var):
            missing.append(f"{var} ({desc})")

    if missing:
        print(
            "error: adp-trigger must run inside an agent pod. "
            "Missing environment variables:\n  " + "\n  ".join(missing),
            file=sys.stderr,
        )
        sys.exit(2)

    return {
        "correlation_id": os.environ["ADP_CORRELATION_ID"],
        "message_id": os.environ["ADP_MESSAGE_ID"],
        "chain_depth": os.environ["ADP_CHAIN_DEPTH"],
        "trigger_endpoint": os.environ["ADP_TRIGGER_ENDPOINT"],
    }


def get_credential() -> str:
    """Read the run credential, or exit 2 with an actionable message.

    Deliberately separate from :func:`get_config`: the spawn path must keep
    working in deployments where the credential has not been provisioned yet, so
    a missing credential must not be an error for ``--persona``. Only the
    status/control subcommands require it.

    A trusted renewal process can atomically replace the configured file. Open it
    anew on every request, including timeout reconciliation, so an already-running
    process observes the replacement. The file contains one ASCII token with an
    optional trailing newline; it is not a source of unsigned identity claims.
    """
    if CREDENTIAL_FILE_ENV in os.environ:
        try:
            # NONBLOCK also prevents a misconfigured FIFO from hanging the CLI.
            fd = os.open(os.environ[CREDENTIAL_FILE_ENV], os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as credential_file:
                if not stat.S_ISREG(os.fstat(credential_file.fileno()).st_mode):
                    raise ValueError("credential file is not regular")
                raw = credential_file.read(_MAX_CREDENTIAL_BYTES + 3)
            credential = raw.decode("ascii").removesuffix("\n").removesuffix("\r")
            if (
                not credential
                or len(credential) > _MAX_CREDENTIAL_BYTES
                or any(ord(char) <= 32 or ord(char) == 127 for char in credential)
            ):
                raise ValueError("invalid credential file contents")
            return credential
        except (OSError, UnicodeError, ValueError):
            # Never include file contents, its path or the exception: any of
            # these may contain credential material supplied by the caller.
            print(
                f"error: {CREDENTIAL_FILE_ENV} is unreadable or invalid; "
                "no request was sent and no environment credential was used.",
                file=sys.stderr,
            )
            sys.exit(2)

    credential = os.environ.get(CREDENTIAL_ENV, "")
    if not credential:
        print(
            f"error: {CREDENTIAL_ENV} is not set in this pod, so this run has no "
            "verifiable identity to present.\n"
            "  Status and control commands require it — the gateway derives the "
            "caller's invocation and attempt from the credential, not from the "
            "environment.\n"
            "  This is expected where delegated monitoring has not been "
            "provisioned; --persona dispatch is unaffected.",
            file=sys.stderr,
        )
        sys.exit(2)
    return credential


def control_base_url() -> str:
    """Resolve the base URL of the agent control plane.

    Explicit configuration wins. Absent that, it is derived from
    ``ADP_TRIGGER_ENDPOINT`` by removing a trailing ``/trigger`` — the two routes
    live under the same ``/agent`` resource on the same API, so deriving one from
    the other is accurate rather than a guess, and it means the CLI works in an
    environment that has not yet added a second variable.

    Derivation is only a suffix removal on a known path, so it can never rewrite
    the host. A value that does not end in ``/trigger`` is refused rather than
    used with a path appended, because appending to an unrecognized base is how a
    credential ends up sent somewhere nobody intended.
    """
    configured = os.environ.get(CONTROL_ENDPOINT_ENV, "").strip()
    if configured:
        return configured.rstrip("/")

    trigger = os.environ.get("ADP_TRIGGER_ENDPOINT", "").strip().rstrip("/")
    if not trigger:
        print(
            f"error: neither {CONTROL_ENDPOINT_ENV} nor ADP_TRIGGER_ENDPOINT is set; "
            "cannot locate the agent control plane.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not trigger.endswith("/trigger"):
        print(
            f"error: {CONTROL_ENDPOINT_ENV} is not set and ADP_TRIGGER_ENDPOINT "
            f"({trigger}) does not end in /trigger, so the control endpoint cannot "
            f"be derived from it. Set {CONTROL_ENDPOINT_ENV} explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)
    return trigger[: -len("/trigger")]


def build_body(persona: str, issue: int, repo: str, reason: str | None = None) -> dict:
    """Construct the request body for POST /agent/trigger.

    Uses lineage context from the pod environment. The agent never handles
    trust values — those are server-resolved from the chain record.

    ``parent_invocation_id`` remains sourced from ``ADP_MESSAGE_ID`` because that
    is the existing route contract, and changing it here would break the route.
    It is not an authenticated identity, which is exactly why the status/control
    paths below do not use it.
    """
    if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true":
        intent = {
            "persona": persona,
            "target": {"repo": repo, "issue": issue},
            "reason": reason or "",
        }
        # Stable through credential refresh and CLI retries. The server scopes
        # this digest to the authenticated invocation/attempt.
        intent["request_id"] = hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return intent
    config = get_config()

    body = {
        "correlation_id": config["correlation_id"],
        "parent_invocation_id": config["message_id"],
        "persona": persona,
        "target": {
            "repo": repo,
            "issue": issue,
        },
    }

    if reason:
        body["reason"] = reason

    return body


def build_control_body(
    command_id: str,
    instruction: str | None = None,
    reason: str | None = None,
) -> dict:
    """Construct the body for a control command.

    ``command_id`` is mandatory and is the idempotency key: the worker journals
    it, and the gateway binds it into the authorization envelope. The same ID
    resubmitted returns the recorded outcome rather than acting twice.

    No caller identity here, by design — see the module docstring.
    """
    body: dict[str, object] = {"command_id": command_id}
    if instruction:
        body["instruction"] = instruction
    if reason:
        body["reason"] = reason
    return body


def send_trigger(body: dict) -> dict:
    """SigV4-sign and POST the trigger request.

    Returns the parsed JSON response body on success (202).
    Exits with a non-zero code on failure.
    """
    if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true":
        return _send(
            "POST",
            f"{control_base_url()}/dispatch",
            body=body,
            extra_headers={CREDENTIAL_HEADER: get_credential()},
        )
    endpoint = os.environ["ADP_TRIGGER_ENDPOINT"]
    return _send("POST", endpoint, body=body)


def send_wave_binding(body: dict) -> dict:
    """Bind materialized issues to the caller's already approved graph."""
    return _send(
        "POST",
        f"{control_base_url()}/waves",
        body=body,
        extra_headers={CREDENTIAL_HEADER: get_credential()},
    )


def send_status(run_id: str) -> dict:
    """GET the status of ``run_id`` on behalf of this run.

    The run ID is the only thing the caller supplies. Everything the gateway
    decides with — the caller's identity, the target's tenant, flow, generation
    and relationship to the caller — is resolved server-side.
    """
    credential = get_credential()
    url = f"{control_base_url()}/status?{urlencode({'run': run_id})}"
    return _send("GET", url, body=None, extra_headers={CREDENTIAL_HEADER: credential})


def send_control(run_id: str, action: str, body: dict) -> dict:
    """POST a control command for ``run_id``.

    The action is in the path rather than the body so the gateway binds the
    envelope to the same action the route dispatched on. An action in the body
    would be a second source for one value, and the envelope would end up
    authorizing whichever of the two each end happened to read.

    On a timeout, raises ControlOutcomeUnknown with the original command ID
    after one bounded status read. Run state alone cannot establish the command
    outcome, so a successful lookup never turns uncertainty into a success.
    """
    credential = get_credential()
    url = f"{control_base_url()}/control/{quote(run_id, safe='')}/{quote(action, safe='')}"
    try:
        return _send(
            "POST",
            url,
            body=body,
            extra_headers={CREDENTIAL_HEADER: credential},
            defer_timeout=True,
        )
    except _RequestTimeout:
        # A running/paused/terminal run does not establish this command's
        # outcome. Keep it unknown even if the status read succeeds: the current
        # agent status projection has no receipt bound to this body/generation.
        # The read is bounded, and can never resend the mutating request.
        status = None
        try:
            observed = send_status(run_id)
            if isinstance(observed, dict):
                status = observed
        except (Exception, SystemExit):
            # Losing the follow-up read must not erase the original uncertainty.
            # Do not include exception text, credentials or the instruction.
            pass
        raise ControlOutcomeUnknown(
            run_id=run_id, action=action, command_id=body["command_id"], status=status
        ) from None


def _send(
    method: str,
    url: str,
    *,
    body: dict | None,
    extra_headers: dict[str, str] | None = None,
    defer_timeout: bool = False,
) -> dict:
    """SigV4-sign and execute one request against the agent plane.

    One implementation for all three paths. Signing is over the exact bytes that
    will be transmitted, and the credential header is added *before* signing so
    it is covered by the signature — a header added afterwards is one an
    intermediary can strip or replace without invalidating anything.
    """
    try:
        import botocore.auth
        import botocore.awsrequest
        import botocore.session
    except ImportError:
        print(
            "error: botocore is required for SigV4 auth. Install boto3.",
            file=sys.stderr,
        )
        sys.exit(1)

    from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

    region = gateway_signing_region(url)

    # Get IRSA credentials from the pod's service account
    session = botocore.session.get_session()
    credentials = worker_credentials(session)
    if credentials is None:
        print(
            "error: no AWS credentials available for SigV4 signing. "
            "Ensure the pod has an IRSA-annotated service account.",
            file=sys.stderr,
        )
        sys.exit(1)
    credentials = credentials.get_frozen_credentials()

    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true":
        from lib.run_identity import RunIdentityError, read_workload_token

        try:
            headers["X-Adp-Workload-Token"] = read_workload_token()
        except RunIdentityError:
            print(
                "error: projected workload identity unavailable; no request was sent",
                file=sys.stderr,
            )
            sys.exit(2)

    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    signed_headers = dict(aws_request.headers)
    req = Request(url, data=data, headers=signed_headers, method=method)

    try:
        with urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        if defer_timeout and exc.code == 504:
            raise _RequestTimeout from None
        error_body = exc.read().decode() if exc.fp else ""
        try:
            error_json = json.loads(error_body)
            detail = error_json.get("detail", error_json.get("error", error_body))
        except (json.JSONDecodeError, ValueError):
            detail = error_body
        print(f"error: agent plane returned {exc.code}: {detail}", file=sys.stderr)
        sys.exit(EXIT_UNSUPPORTED if exc.code == 501 else 1)
    except TimeoutError:
        if defer_timeout:
            raise _RequestTimeout from None
        print("error: agent plane request timed out", file=sys.stderr)
        sys.exit(1)
    except URLError as exc:
        if defer_timeout and (
            isinstance(exc.reason, TimeoutError)
            or str(exc.reason).lower() in {"timed out", "timeout"}
        ):
            raise _RequestTimeout from None
        print(f"error: cannot reach agent plane: {exc.reason}", file=sys.stderr)
        sys.exit(1)
