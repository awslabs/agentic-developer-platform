"""Recognise a Task API assignment on the shared queue — Task API T4 (#5797).

Two unrelated kinds of work arrive on one FIFO queue. A *legacy* message names a
GitHub repository, issue and App installation, and its worker mints a token and
clones. A *task* message (design revision
``b5761a4a2502aceaa9133afef552b567a19cb46e``, section 7) carries instructions and
supplied evidence and has none of those things.

This module owns the decision between them, and nothing else. It performs no I/O
and acquires no authority, so the entrypoint can call it before it has any
credential — which is the point: the task branch must execute *before* GitHub
preparation, so a task with no repository fields never reaches a check that would
reject it for lacking them.

## Why the discriminator is checked in both directions

The obvious implementation is ``envelope.get("kind") == "adp.task"``. That is
insufficient, and the contract says so in two rejection fixtures
(``envelope-legacy-persona-in-task-kind``, ``legacy-envelope-claiming-task-kind``).
An envelope that satisfies *both* descriptions would be routed by whichever
consumer read it first, which is nondeterministic dispatch of the same accepted
work. So:

- a task envelope must carry the discriminator, a task persona, and **no**
  repository/installation routing at any depth;
- a legacy envelope must carry its routing and **neither** the discriminator nor
  a task run handle.

Anything matching both, or matching the task marker only partially, is refused as
:class:`TaskDispatchError`. Refusing is safe; guessing is not. A malformed task
assignment that fell through to the legacy branch would attempt a checkout and a
token mint for a task that has no repository and no repository grant, and a legacy
message that reached the task branch would skip the GitHub setup its agent needs.

## Why the shape is validated here rather than trusted from the queue

The envelope is a durable queue message. Validating its shape before any
authority is acquired means a malformed or truncated body fails as a dispatch
error with nothing acquired and nothing cleaned up, instead of failing partway
through bootstrap. The fields validated are exactly those the gateway will
compare against its own protected dispatch record — this check does not *grant*
anything, it only refuses to proceed on a body that cannot be an assignment.

Normative shapes: ``docs/task-api/contracts/v1/schemas/envelope.schema.json``
(``task_dispatch_envelope`` and ``legacy_github_envelope``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The envelope discriminator the task branch matches. Design section 7.
TASK_ENVELOPE_KIND = "adp.task"

#: Task personas are ``agent-task-*``; legacy personas never are. The legacy
#: schema asserts the negative, so this prefix is exclusive in both directions.
TASK_PERSONA_PREFIX = "agent-task-"

_SCHEMA_VERSION = "1.0"

_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TASK_ID = re.compile(
    r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PERSONA = re.compile(r"^agent-task-[a-z0-9]+(-[a-z0-9]+)*$")
_ARTIFACT_ID = re.compile(
    r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_GRANT_SK = re.compile(r"^TASK_RUN#[0-9a-f-]{36}#GEN#[0-9]{10}$")

#: Fields a task envelope must never carry. Transport identifiers and credentials
#: are forbidden because the queue is durable and broadly readable in-account: a
#: credential here would outlive the run and be recoverable from the retention
#: window rather than acquired under a verified identity. Caller-selected
#: authority and GitHub routing are forbidden because body fields do not select
#: authority and a task has no repository.
_FORBIDDEN_TASK_FIELDS = (
    "receipt_handle",
    "sqs_message_id",
    "run_credential",
    "token",
    "tenant_id",
    "repository",
    "installation_id",
    "issue_number",
)

#: Legacy routing keys. Their presence anywhere (top level or nested under
#: ``source_ref``) disqualifies an envelope from the task branch. The schema note
#: is explicit that routing lives nested, so checking the top level alone would
#: miss every real legacy message.
_LEGACY_ROUTING_KEYS = ("installation_id", "repo", "issue", "pr")

_REQUIRED_TASK_FIELDS = (
    "kind",
    "schema_version",
    "task_id",
    "invocation_id",
    "message_id",
    "persona",
    "dispatch_id",
    "request_digest",
    "input_ref",
    "assignment_ref",
)


class TaskDispatchError(Exception):
    """This body cannot be dispatched as a task assignment.

    Raised for a malformed task envelope and for one that is ambiguous between
    the two branches. The caller must fail the run: it must not retry on the
    legacy branch, because the legacy branch would treat caller-supplied content
    as repository work.

    Carries a short static reason for logging. It never embeds envelope content,
    so a diagnostic log cannot become an echo of a caller-supplied body.
    """


@dataclass(frozen=True)
class TaskAssignment:
    """A validated task assignment, as carried by the dispatch envelope.

    Frozen because it is the run's fixed identity: every later gateway call is
    bound to these values, and a mutable copy is how a superseded generation ends
    up presenting itself as the live one.

    This is the *envelope's claim*, not authority. The gateway compares the
    envelope digest against its own protected dispatch record at bootstrap and
    derives the real grant itself. Nothing here may be used as permission.
    """

    task_id: str
    invocation_id: str
    persona: str
    dispatch_id: str
    request_digest: str
    generation: int
    grant_pk: str
    grant_sk: str
    input_digest: str
    artifact_refs: tuple[dict, ...]

    @property
    def message_id(self) -> str:
        """The existing receive adapter's identifier, pinned equal to the invocation.

        The design fixes them equal so there is exactly one run handle. A
        redelivery has a new transport identifier but the same invocation;
        conflating the two is how a redelivery becomes a second run.
        """
        return self.invocation_id


def is_task_envelope(envelope: object) -> bool:
    """Whether this body claims the task discriminator at all.

    Deliberately narrow: it answers "does this want the task branch", not "is it
    a valid assignment". The entrypoint uses it to choose a branch, then calls
    :func:`parse_task_envelope`, which is what refuses a malformed claim. Keeping
    them separate is what makes a malformed task assignment fail *as a task*
    rather than silently falling through to the legacy path.
    """
    return isinstance(envelope, dict) and envelope.get("kind") == TASK_ENVELOPE_KIND


def is_task_persona(persona: object) -> bool:
    """Whether this persona name belongs to the task path."""
    return isinstance(persona, str) and persona.startswith(TASK_PERSONA_PREFIX)


def _reject(reason: str) -> TaskDispatchError:
    return TaskDispatchError(reason)


def _require_str(container: dict, key: str, pattern: re.Pattern[str], label: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not pattern.match(value):
        raise _reject(f"task envelope {label} is not well formed")
    return value


def _validate_input_ref(envelope: dict) -> tuple[str, tuple[dict, ...]]:
    ref = envelope.get("input_ref")
    if not isinstance(ref, dict):
        raise _reject("task envelope input_ref is not an object")
    if ref.get("record_type") != "TASK":
        raise _reject("task envelope input_ref record_type is not TASK")
    if set(ref) - {"record_type", "input_digest", "artifact_refs"}:
        raise _reject("task envelope input_ref carries unknown fields")
    input_digest = _require_str(ref, "input_digest", _SHA256, "input_digest")

    raw_artifacts = ref.get("artifact_refs", [])
    if not isinstance(raw_artifacts, list):
        raise _reject("task envelope artifact_refs is not an array")
    if len(raw_artifacts) > 4:
        raise _reject("task envelope exceeds the 4 input artifact limit")
    artifacts: list[dict] = []
    for item in raw_artifacts:
        if not isinstance(item, dict):
            raise _reject("task envelope artifact reference is not an object")
        if set(item) != {"artifact_id", "version", "content_sha256"}:
            raise _reject("task envelope artifact reference has unexpected fields")
        artifact_id = _require_str(item, "artifact_id", _ARTIFACT_ID, "artifact_id")
        version = item.get("version")
        # bool is an int subclass in Python; True would otherwise pass as version 1.
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise _reject("task envelope artifact version is not a positive integer")
        digest = _require_str(item, "content_sha256", _SHA256, "artifact content_sha256")
        artifacts.append(
            {"artifact_id": artifact_id, "version": version, "content_sha256": digest}
        )
    return input_digest, tuple(artifacts)


def _validate_assignment_ref(envelope: dict) -> tuple[int, str, str]:
    ref = envelope.get("assignment_ref")
    if not isinstance(ref, dict):
        raise _reject("task envelope assignment_ref is not an object")
    if set(ref) != {"grant_pk", "grant_sk", "generation"}:
        raise _reject("task envelope assignment_ref has unexpected fields")
    grant_pk = ref.get("grant_pk")
    if not isinstance(grant_pk, str) or not grant_pk.startswith("TENANT#"):
        raise _reject("task envelope grant_pk is not a tenant authority key")
    grant_sk = _require_str(ref, "grant_sk", _GRANT_SK, "grant_sk")
    generation = ref.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise _reject("task envelope generation is not a positive integer")

    # The authority key encodes the generation as ten zero-padded digits, and the
    # conditional write is checked against the *key*. If the number and the key
    # could disagree, a superseded worker could present a stale key beside a
    # current generation number and be admitted as the live attempt.
    encoded = grant_sk.rsplit("#", 1)[-1]
    if int(encoded) != generation:
        raise _reject("task envelope generation disagrees with its authority key")

    # Same reasoning for the invocation: the grant key names the run it binds.
    key_invocation = grant_sk.split("#")[1]
    if key_invocation != envelope.get("invocation_id"):
        raise _reject("task envelope assignment key names a different invocation")
    return generation, grant_pk, grant_sk


def parse_task_envelope(envelope: object) -> TaskAssignment:
    """Validate a task dispatch envelope and return its fixed run identity.

    :raises TaskDispatchError: the body is not a well-formed, unambiguous task
        assignment. The caller fails the run; it never falls back to the legacy
        branch or to a legacy persona.
    """
    if not isinstance(envelope, dict):
        raise _reject("task envelope is not a JSON object")
    if envelope.get("kind") != TASK_ENVELOPE_KIND:
        raise _reject("task envelope does not carry the task discriminator")
    if envelope.get("schema_version") != _SCHEMA_VERSION:
        raise _reject("task envelope schema_version is unsupported")

    missing = [key for key in _REQUIRED_TASK_FIELDS if key not in envelope]
    if missing:
        raise _reject(f"task envelope is missing required fields: {','.join(sorted(missing))}")

    present_forbidden = [key for key in _FORBIDDEN_TASK_FIELDS if key in envelope]
    if present_forbidden:
        raise _reject(
            "task envelope carries forbidden transport/credential/routing fields: "
            + ",".join(sorted(present_forbidden))
        )

    # Nested GitHub routing is the ambiguous case the rejection fixtures target.
    # A task envelope may not carry repository routing at any depth.
    for container_key in ("source_ref", "intent", "payload"):
        container = envelope.get(container_key)
        if isinstance(container, dict) and any(k in container for k in _LEGACY_ROUTING_KEYS):
            raise _reject(f"task envelope carries GitHub routing under {container_key}")
    if "source_ref" in envelope:
        raise _reject("task envelope carries a legacy source_ref")

    persona = envelope.get("persona")
    if not is_task_persona(persona):
        raise _reject("task envelope persona is not a task persona")
    assert isinstance(persona, str)  # narrowed by is_task_persona
    if not _PERSONA.match(persona) or len(persona) > 64:
        raise _reject("task envelope persona is not a well-formed task persona name")

    task_id = _require_str(envelope, "task_id", _TASK_ID, "task_id")
    invocation_id = _require_str(envelope, "invocation_id", _UUID4, "invocation_id")
    dispatch_id = _require_str(envelope, "dispatch_id", _UUID4, "dispatch_id")
    request_digest = _require_str(envelope, "request_digest", _SHA256, "request_digest")

    # message_id is pinned equal to the invocation so the run has exactly one
    # handle. Two divergent identifiers is how one run is reported twice.
    message_id = _require_str(envelope, "message_id", _UUID4, "message_id")
    if message_id != invocation_id:
        raise _reject("task envelope message_id is not the invocation id")

    input_digest, artifact_refs = _validate_input_ref(envelope)
    generation, grant_pk, grant_sk = _validate_assignment_ref(envelope)

    unknown = set(envelope) - set(_REQUIRED_TASK_FIELDS)
    if unknown:
        raise _reject(f"task envelope carries unknown fields: {','.join(sorted(unknown))}")

    return TaskAssignment(
        task_id=task_id,
        invocation_id=invocation_id,
        persona=persona,
        dispatch_id=dispatch_id,
        request_digest=request_digest,
        generation=generation,
        grant_pk=grant_pk,
        grant_sk=grant_sk,
        input_digest=input_digest,
        artifact_refs=artifact_refs,
    )


def reject_task_persona_on_legacy_path(envelope: object) -> None:
    """Refuse task-marked work that arrived on the legacy branch.

    The legacy branch mints a GitHub token and clones. A task persona has no
    repository and no repository grant, so reaching that branch means attempting
    a checkout for work that has neither — and, worse, handing caller-supplied
    task content to a runtime provisioned with a repository credential.

    Called by the entrypoint on the legacy side so the exclusion holds in both
    directions rather than only in the task branch's own check.
    """
    if not isinstance(envelope, dict):
        return
    task_markers = {
        "task_id",
        "invocation_id",
        "dispatch_id",
        "request_digest",
        "input_ref",
        "assignment_ref",
    }
    if is_task_persona(envelope.get("persona")) or task_markers.intersection(envelope):
        raise _reject("task-marked work is not dispatchable on the GitHub path")
