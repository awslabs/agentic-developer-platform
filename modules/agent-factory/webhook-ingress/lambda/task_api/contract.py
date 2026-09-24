"""Frozen Task API contract constants for the submit path.

Every value here is transcribed from the accepted design revision
b5761a4a2502aceaa9133afef552b567a19cb46e:

- ``docs/task-api/contracts/v1/limits.json`` (``submit_body``, ``artifacts``)
- ``docs/task-api/contracts/v1/schemas/public-api.schema.json``
  (``$defs/submit_request``, ``$defs/submit_response``)
- ``docs/task-api/contracts/v1/schemas/errors.schema.json``
- ``docs/task-api/contracts/v1/identity-and-lifecycle.json``

The Lambda cannot read those files at runtime (they are repository docs, not
packaged with the function), so they are mirrored here and guarded by
``tests/test_contract_parity.py``, which fails if this module and the contract
files ever disagree.
"""

from __future__ import annotations

import re

SCHEMA_VERSION = "1.0"

# --- public route -------------------------------------------------------

#: REST API v1 ``event["resource"]`` value for the submit route.
SUBMIT_RESOURCE = "/v1/tasks"
SUBMIT_METHOD = "POST"
#: Cognito resource-server scope required to submit a task.
SUBMIT_SCOPE = "adp-tasks/submit"

# --- internal adapter (T3-owned, called by this Lambda) -----------------

ADMIT_ROUTE = "/internal/v1/tasks/admit"

#: Original caller bearer token is forwarded here, never in ``Authorization``
#: (which carries the ingress role's SigV4 signature on the internal call).
CALLER_TOKEN_HEADER = "X-Adp-Task-Caller-Token"
#: Existing STS producer-proof header, reused with a task-specific binding.
PRODUCER_PROOF_HEADER = "X-Adp-Producer-Proof"
#: Header on the inner STS attestation that binds the proof to this request.
PROOF_BINDING_HEADER = "x-adp-work-invocation"
#: Versioned tag for the length-delimited proof binding encoding.
PROOF_BINDING_VERSION = "adp.task.admit.v1"

# --- rollout flag -------------------------------------------------------

ADMISSION_FLAG = "ADP_TASK_API_ADMISSION_ENABLED"

# --- submit_body limits -------------------------------------------------

MAX_BODY_BYTES = 65536
MAX_INSTRUCTIONS_CHARACTERS = 16000
MAX_EXTERNAL_REFERENCE_CHARACTERS = 256
MAX_ACCEPTANCE_CRITERIA_COUNT = 10
MAX_ACCEPTANCE_CRITERION_CHARACTERS = 1000
IDEMPOTENCY_KEY_MIN_CHARACTERS = 1
IDEMPOTENCY_KEY_MAX_CHARACTERS = 128

# --- artifact limits ----------------------------------------------------

MAX_INPUT_ARTIFACTS = 4

# --- shapes -------------------------------------------------------------

IDEMPOTENCY_KEY_HEADER = "idempotency-key"
#: Printable ASCII, per ``limits.json#/submit_body/idempotency_key_charset``.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x20-\x7e]+$")

#: Only registered ``agent-task-*`` personas are accepted. Existing GitHub
#: personas and path/executable injection are rejected without publication.
PERSONA_PATTERN = re.compile(r"^agent-task-[a-z0-9]+(-[a-z0-9]+)*$")
MAX_PERSONA_CHARACTERS = 64

_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
ARTIFACT_ID_PATTERN = re.compile(rf"^art_{_UUID4}$")
TASK_ID_PATTERN = re.compile(rf"^tsk_{_UUID4}$")
UUID4_PATTERN = re.compile(rf"^{_UUID4}$")
RFC3339_UTC_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
)

SUBMIT_REQUIRED_FIELDS = ("schema_version", "persona", "instructions")
SUBMIT_OPTIONAL_FIELDS = (
    "inputs",
    "artifact_ids",
    "external_reference",
    "acceptance_criteria",
)
SUBMIT_ALLOWED_FIELDS = frozenset(SUBMIT_REQUIRED_FIELDS + SUBMIT_OPTIONAL_FIELDS)

#: A public submit request may never select any of these. The verified
#: credential decides identity and the server decides execution parameters;
#: a body field claiming otherwise is refused rather than ignored.
CALLER_FORBIDDEN_FIELDS = frozenset(
    {
        "tenant",
        "tenant_id",
        "owner",
        "canonical_principal",
        "executable",
        "model",
        "model_id",
        "model_override",
        "generation",
        "runtime_attempt_id",
        "invocation_id",
        "task_id",
        "queue",
        "grant",
        "dispatch_id",
        "transport_credentials",
        "scope",
        "scopes",
    }
)

#: ``submit_response`` required keys; a 202 is only returned when the gateway
#: receipt carries all of them.
SUBMIT_RESPONSE_REQUIRED = (
    "schema_version",
    "task_id",
    "invocation_id",
    "status",
    "created_at",
    "deadline_at",
    "status_url",
    "events_url",
    "request_id",
)
SUBMIT_RESPONSE_ALLOWED = frozenset(SUBMIT_RESPONSE_REQUIRED + ("idempotent_replay",))
#: A 202 reports durable acceptance. It never reports running or terminal.
SUBMIT_RESPONSE_STATUSES = frozenset({"accepted", "queued"})

# --- error codes and their only permitted HTTP statuses -----------------

ERROR_STATUS = {
    "invalid_request": 400,
    "invalid_credential": 401,
    "disallowed_scope": 403,
    "disallowed_persona": 403,
    "not_found": 404,
    "idempotency_conflict": 409,
    "payload_too_large": 413,
    "rate_limited": 429,
    "queue_full": 429,
    "prerequisite_unavailable": 503,
}

#: ``details`` is a bounded safe context. Never a token, proof, secret or
#: unrestricted task body.
ERROR_DETAIL_FIELDS = frozenset(
    {
        "task_id",
        "command_id",
        "current_status",
        "oldest_event_cursor",
        "latest_event_cursor",
        "limit_name",
        "limit_value",
    }
)
MAX_SAFE_MESSAGE_CHARACTERS = 1000
