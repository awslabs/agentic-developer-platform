#!/usr/bin/env python3
"""Live-control evaluation harness (Issue #3960, revision revival-2026-09-12).

Issue: #3960 (S1). Live acceptance run: #3967.

This script does NOT run in CI. It is an operator-initiated evaluation that
exercises the authenticated control path against a real, deployed environment and
emits a redacted evidence report. CI covers its *guards* only
(``platform/scripts/tests/test_agent_control_eval.py``), because the properties
that matter most here — refusing an unisolated fixture, refusing the wrong
account — are unverifiable at the moment they matter, when someone is already
pointing it at a live account.

Invocation (revival-design §7; this is the published smoke command)::

    python3 agent-control-eval.py --wave 1 --config "$CONTROL_EVAL_CONFIG" \\
                                  --evidence-dir "$CONTROL_EVIDENCE_DIR"

    jq -e '.failed == 0 and .skipped == 0 and .not_run == 0
           and .passed == .required and .cleanup_ok == true' \\
       "$CONTROL_EVIDENCE_DIR/result.json"

The check IDs come from the evaluation file
-------------------------------------------
revival-design §7 is explicit that "checks in each evaluation file are the
authoritative required check IDs", so :data:`WAVE_CHECKS` below is transcribed
from the acceptance table in **evaluation #3967**, including each check's owned
acceptance IDs. That table is *not* a renaming of some other partition of this
space — W1-01 is the preflight/provenance record, the 401/404/501/capabilities
family is all of W1-02 across both adapters, and the non-gateway peer probe is
W1-04. Keying a report by these IDs with different meanings would satisfy the
`jq` gate while proving something other than what the evaluation requires, and
nothing downstream would flag it — which is worse than a check that is plainly
missing. ``test_agent_control_eval.py`` pins each ID's meaning so that drift
fails CI instead of reading OK.

What a status means
-------------------
``passed``/``failed`` are observations. ``not_run`` is the important one: a check
whose prerequisite is absent is recorded ``not_run`` *individually*, naming the
prerequisite it wanted, and the run exits nonzero. It is never a pass, and never
a single blanket "precondition failed" for the whole run — an operator has to be
able to tell a broken fixture from an evaluation that was never wired up. The
gate reads ``.not_run == 0``, so an unrunnable check cannot be mistaken for a
passing one.

Observations the harness can make from where it runs — HTTP through the gateway,
consistent DynamoDB reads on the fixture row — it makes itself. Observations that
only exist inside the cluster (the named non-gateway probe pod, the flag-off /
flag-on fixture pair, the worker's own journal tests) are consumed as
operator-recorded artifacts and **validated, not trusted**: a malformed or
incomplete artifact is a failure, and an absent one is ``not_run``.

Why the guards are so blunt
---------------------------
This harness talks to a live control plane. Its own bugs are the risk, so every
precondition fails closed with a nonzero exit and no partial evidence:

  * **Fixture isolation is mandatory** (DP-INV-1). The flag may be enabled only in
    an operator-created isolated test fixture, never on a shared environment.
    Unset or false isolation is an error, not a default.
  * **The account must be named explicitly and must match.** No ambient account.
    An evaluation that ran against whatever credential happened to be in the
    environment would be worthless as evidence and dangerous as an action.
  * **The DynamoDB key schema is verified before any write.** The control record
    is written onto the invocation row; a wrong key means either a silent no-op or
    a write onto an unrelated item.
  * **Cleanup failure fails the run**, and cleanup runs on the failure path too. A
    fixture left with a control listener enabled is the exact state DP-INV-1
    forbids, so it is reported as failure even when all ten checks passed.

Credentials are never read from the config file. It names *environment variables*
(``identity_env``), which is what lets the fixture description be committed as
documentation while the bearer tokens come from the supported credential path.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("agent-control-eval")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PRECONDITION = 3
EXIT_CHECKS_FAILED = 4
EXIT_CLEANUP = 5

# Check statuses. These are the four the §7 aggregates count, and the report uses
# exactly these strings because the operator's `jq` compares against "passed".
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NOT_RUN = "not_run"

# The four verbs, all of which must answer 501 in S1.
CONTROL_VERBS: tuple[str, ...] = ("pause", "resume", "steer", "abort")

# Both adapters that reach the shared control service. W1-02 requires the
# identical authorization behaviour on each, which is the whole point of there
# being one `control_service.py`: a path template pair here is what proves the
# two HTTP edges did not drift.
ADAPTERS: dict[str, dict[str, str]] = {
    "activity": {
        "verb": "/activity/invocations/{run_id}/agent/{verb}",
        "ping": "/activity/invocations/{run_id}/agent/ping",
        "state": "/activity/invocations/{run_id}/agent/state",
    },
    "orchestration": {
        "verb": "/orchestration/runs/{run_id}/{verb}",
        "ping": "/orchestration/runs/{run_id}/ping",
        "state": "/orchestration/runs/{run_id}/state",
    },
}


@dataclass(frozen=True)
class CheckSpec:
    """One row of the evaluation file's acceptance table."""

    check_id: str
    acceptance_ids: tuple[str, ...]
    description: str


# Transcribed from evaluation #3967's "Commands and expected output" table. The
# descriptions are deliberately the *required observation*, not a paraphrase, so a
# reader comparing this file against the issue can do it line by line.
WAVE1_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "W1-01",
        ("Gate/regression",),
        "preflight records the account, the four identities, the exact event_id + "
        "arrived_at + generation, and source/deployed digests; required CI jobs "
        "passed; isolation exists before listener start; ordinary flags are false",
    ),
    CheckSpec(
        "W1-02",
        ("AC-S1", "AC-S2"),
        "on BOTH adapters and all four verbs: missing browser auth is 401; wrong "
        "tenant, same-tenant nonowner and unknown ID return identical 404s; the pod "
        "rejects a missing/wrong token with 401 before verb parsing; authorized "
        "unsupported verbs are 501; capabilities are all false",
    ),
    CheckSpec(
        "W1-03",
        ("AC-S3",),
        "a short-lived isolated token works before expiry and fails after it; a "
        "stale generation fails; no ordinary run's clock is changed; public state "
        "and logs contain no token",
    ),
    CheckSpec(
        "W1-04",
        ("AC-S4",),
        "an authenticated gateway ping reaches the fixture worker (200) and a named "
        "non-gateway probe pod cannot connect within a finite timeout; actual "
        "connection results are recorded, not policy YAML alone",
    ),
    CheckSpec(
        "W1-05",
        ("AC-S5",),
        "each command rejects malformed JSON/schema with 400, extra "
        "actor/target/token fields with 400 and an oversized payload with 413; the "
        "fixture task subsequently completes with unchanged deterministic output",
    ),
    CheckSpec(
        "W1-06",
        ("AC-S6",),
        "the owner sees 410 on a terminal row while nonowner and other tenant still "
        "see 404; a missing registration or dead worker reports unavailable and "
        "cannot acknowledge a command; terminal teardown clears the private fields",
    ),
    CheckSpec(
        "W1-07",
        ("AC-S7",),
        "unregistered IP, wrong port, metadata/link-local/loopback/public targets "
        "and redirects are blocked before transport; a caller cannot override "
        "actor/target; browser responses and captured request logs contain no pod "
        "address or token",
    ),
    CheckSpec(
        "W1-08",
        ("AC-F1", "AC-F2"),
        "two deterministic fixtures compare normalized task events/output/outcome: "
        "flag-off has no listener or control fields, flag-on/no-command differs only "
        "by declared registration/state metadata; authorized flag-off routes return "
        "503; ordinary gateway/worker/SPA remain off",
    ),
    CheckSpec(
        "W1-09",
        ("Gate/regression",),
        "the live ping/state response matches control_schemas.py including run_id, "
        "generation, available, reason, capabilities, state, active_tool_count, "
        "updated_at and commands; a state read causes zero assistant turns; "
        "SDK-independent journal tests prove same-ID replay, content conflict, "
        "bounds and expiry-as-unknown",
    ),
    CheckSpec(
        "W1-10",
        ("Gate/regression",),
        "harness negative tests fail on wrong account/isolation/key/digest, on an "
        "absent or unknown required check and on failed cleanup; the result contains "
        "every listed ID with redacted command evidence; the exact fixtures are removed",
    ),
)

# S1 delivers wave 1. Waves 2-4 are extended by their owning stories (§7:
# "S2/S5 extend wave 2; S4/S6 extend wave 3; S7 extends wave 4"). Asking for one
# of those is an honest nonzero, not an empty pass.
WAVE_CHECKS: dict[int, tuple[CheckSpec, ...]] = {1: WAVE1_CHECKS}
SUPPORTED_WAVES: tuple[int, ...] = tuple(sorted(WAVE_CHECKS))

# Retained for the manifest guard and for callers that only need the ID set.
EXPECTED_CHECK_IDS: tuple[str, ...] = tuple(spec.check_id for spec in WAVE1_CHECKS)

CHECK_DESCRIPTIONS: dict[str, str] = {
    spec.check_id: spec.description for spec in WAVE1_CHECKS
}

CHECK_ACCEPTANCE_IDS: dict[str, tuple[str, ...]] = {
    spec.check_id: spec.acceptance_ids for spec in WAVE1_CHECKS
}

REQUIRED_CONFIG_FIELDS: tuple[str, ...] = (
    "account_id",
    "environment",
    "fixture_isolated",
    "gateway_url",
    "invocation_table",
    "live_run_id",
    "terminal_run_id",
    "tenant_id",
)

# The invocation table's real key schema. Verified against the live table before
# any write, because a mismatch means writing the control record somewhere other
# than the invocation row it is supposed to describe.
EXPECTED_KEY_SCHEMA: tuple[tuple[str, str], ...] = (
    ("event_id", "HASH"),
    ("arrived_at", "RANGE"),
)

# The identity roles W1-02 needs to tell "not yours" apart from "does not exist".
# Config supplies the *env var name* holding each bearer token, never the token.
IDENTITY_ROLES: tuple[str, ...] = ("owner", "nonowner", "other_tenant")

# Operator-recorded artifacts, and the keys each must carry to be usable. Absent
# → the owning check is not_run. Present but missing a key → the owning check
# FAILS, because a half-filled artifact is a claim without its evidence.
REQUIRED_ARTIFACT_KEYS: dict[str, tuple[str, ...]] = {
    "provenance": ("source_digest", "deployed_digest", "ci_jobs", "isolation_before_listener", "ordinary_flags_off"),
    "listener_auth": ("missing_token_status", "wrong_token_status", "rejected_before_verb_parse"),
    "token_lifecycle": ("before_expiry_status", "after_expiry_status", "stale_generation_status", "ordinary_clock_unchanged"),
    "peer_probe": ("probe_pod", "gateway_ping_status", "probe_connect_result", "policy_selectors", "timeout_seconds"),
    "fixture_task": ("completed", "normalized_output_digest"),
    "worker_unavailable": ("state", "command_acknowledged"),
    "transport_guard": ("blocked_targets", "redirect_blocked", "blocked_before_transport"),
    "flag_parity": ("flag_off_events_digest", "flag_on_events_digest", "differing_fields", "ordinary_flags_off"),
    "journal_tests": ("replay_same_id", "content_conflict", "bounds_enforced", "expiry_is_unknown", "assistant_turns"),
    "negative_tests": ("wrong_account", "missing_isolation", "wrong_key", "absent_required_check", "unknown_check_id", "failed_cleanup"),
}

# Substrings that mark a value as secret regardless of its own key name — a
# credential nested inside an opaque blob still has to be scrubbed.
_SECRET_KEY_PATTERNS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "session",
    "access_key",
    "private",
    "signature",
    "cookie",
    "api_key",
    "apikey",
    "bearer",
)

REDACTED = "[REDACTED]"

# Value-shaped redaction, applied to strings even under innocuous keys. Evidence
# is written to disk and pasted into issues, so a bearer header echoed inside a
# free-text error message must not survive.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\bASIA[0-9A-Z]{12,}\b"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), REDACTED),
    (re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{10,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}\b"), REDACTED),  # JWT-shaped
)


class EvalConfigError(Exception):
    """The fixture description is unusable. Nothing has been contacted yet."""


class EvalPreconditionError(Exception):
    """The live environment is not a safe or valid target for this evaluation."""


class EvalCleanupError(Exception):
    """Checks may have passed, but the fixture was not returned to a safe state."""


class PrerequisiteMissingError(Exception):
    """A check cannot run because an input it needs is absent.

    Raised by a predicate and caught by the driver, which records the check as
    ``not_run`` with this message as its reason. Distinct from a check failing:
    "I could not look" and "I looked and it was wrong" must not collapse into one
    answer, because only the second is a defect in the thing under evaluation.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value):  # noqa: ANN001, ANN201
    """Recursively strip credentials from an evidence structure.

    Two independent passes, because either alone is insufficient: key-name
    matching catches a well-named field holding an unrecognisable value, and
    value-shape matching catches a credential embedded in a message or stored
    under a bland key like ``detail``.

    Redaction is deliberately lossy — it replaces rather than truncates or hashes.
    A prefix is still a secret, and a stable hash of a bearer token is a stable
    identifier for it.
    """
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            lowered = str(key).lower()
            if any(pattern in lowered for pattern in _SECRET_KEY_PATTERNS):
                out[key] = REDACTED
            else:
                out[key] = redact(inner)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern, replacement in _VALUE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result
    return value


@dataclass
class Observation:
    """One recorded interaction with the live system.

    ``command`` is the operator-reproducible equivalent of what was sent (§7
    requires "exact commands, outputs, timestamps"). It is assembled without the
    bearer value — the header is rendered as a placeholder rather than redacted
    after the fact, so a token cannot reach the evidence file even transiently.
    """

    command: str
    status: int | None = None
    body: object = None
    error: str | None = None
    at: str = field(default_factory=_now)

    def to_evidence(self) -> dict:
        return redact(
            {
                "command": self.command,
                "status": self.status,
                "body": self.body,
                "error": self.error,
                "at": self.at,
            }
        )


@dataclass
class CheckResult:
    """One W1 outcome.

    ``status`` is explicit and has no default: a check that forgot to set an
    outcome must not inherit a pass.
    """

    check_id: str
    status: str
    description: str = ""
    acceptance_ids: tuple[str, ...] = ()
    observations: list[Observation] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def passed(self) -> bool:
        """Kept so callers can ask the boolean question directly."""
        return self.status == STATUS_PASSED

    def to_evidence(self) -> dict:
        # §7: each entry has `status`, `acceptance_ids` and a NONEMPTY `evidence`
        # list of artifact paths and recorded observations. The nonemptiness is
        # load-bearing in the operator's `check()` function, so a check that
        # recorded nothing is a check that cannot pass the gate — including
        # not_run, whose evidence is the reason it could not run.
        evidence: list[object] = [obs.to_evidence() for obs in self.observations]
        evidence.extend({"artifact": path} for path in self.artifacts)
        if not evidence:
            evidence.append({"note": redact(self.message) or "no observation recorded"})
        return {
            "status": self.status,
            "acceptance_ids": list(self.acceptance_ids)
            or list(CHECK_ACCEPTANCE_IDS.get(self.check_id, ())),
            "description": self.description
            or CHECK_DESCRIPTIONS.get(self.check_id, ""),
            "evidence": evidence,
            "message": redact(self.message),
        }


def load_config(path: Path) -> dict:
    """Read and validate the fixture description.

    Every failure here is a config error rather than a precondition error: at this
    point nothing has been contacted, so the operator can fix the file and rerun
    with no live side effects to unwind.
    """
    if not path.is_file():
        raise EvalConfigError(f"fixture config not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalConfigError(f"fixture config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise EvalConfigError("fixture config must be a JSON object")

    missing = [
        key
        for key in REQUIRED_CONFIG_FIELDS
        if key not in raw or raw[key] in ("", None)
    ]
    if missing:
        raise EvalConfigError(
            f"fixture config is missing required fields: {sorted(missing)}. "
            "Every field is mandatory; this harness has no defaults because a default target for a "
            "control-plane evaluation would be whatever account the ambient credential resolves to."
        )

    # Isolation must be the literal boolean true. A truthy string ("false" is
    # truthy!) or a 1 would let a shared environment pass the gate that exists
    # specifically to keep the flag off shared environments (DP-INV-1).
    if raw["fixture_isolated"] is not True:
        raise EvalConfigError(
            "fixture_isolated must be exactly true (JSON boolean). "
            f"Got {raw['fixture_isolated']!r}. FEATURE_AGENT_CONTROL_ENABLED may only be enabled in an "
            "operator-created isolated test fixture; it must never be turned on for a shared environment "
            "or to work around fixture setup failing."
        )

    account_id = str(raw["account_id"])
    if not re.fullmatch(r"\d{12}", account_id):
        raise EvalConfigError(
            f"account_id must be exactly 12 digits, got {account_id!r}"
        )

    # A committed fixture description must not carry credentials. `identity_env`
    # names environment variables; a field whose name says "token" and whose value
    # is not an env var name is the mistake this rejects, and it is a config error
    # rather than a redaction problem because the file itself is the leak.
    for key, value in raw.items():
        lowered = str(key).lower()
        if any(pattern in lowered for pattern in ("token", "secret", "password", "credential")):
            raise EvalConfigError(
                f"fixture config must not contain credentials, but carries {key!r}. "
                "Name the environment variable holding it under 'identity_env' instead; credentials come "
                "from the supported credential path and are never committed (revival-design §7)."
            )

    return raw


def verify_account(config: dict, sts_client) -> str:  # noqa: ANN001
    """Confirm the live credential resolves to the account the fixture names.

    The mismatch direction that matters: a config naming the isolated fixture
    account while the ambient credential points at a shared or production account.
    Refusing is the only safe answer — the operator's intent is unknowable and one
    of the two is wrong.
    """
    identity = sts_client.get_caller_identity()
    live_account = str(identity.get("Account", ""))
    expected = str(config["account_id"])
    if live_account != expected:
        raise EvalPreconditionError(
            f"account mismatch: fixture config names {expected} but the active credential resolves to "
            f"{live_account}. Refusing to run. Check AWS_PROFILE / the connected credential."
        )
    logger.info("account verified: %s (%s)", live_account, config["environment"])
    return live_account


def verify_table_key_schema(config: dict, dynamodb_client) -> None:  # noqa: ANN001
    """Verify the invocation table's key schema before anything is written.

    The control record is an update to an existing invocation row, guarded by
    ``attribute_exists(event_id)``. If the key schema is not what the writer
    assumes, the update either silently no-ops or lands on an unrelated item — and
    the evaluation would then report on a row it invented.
    """
    table_name = config["invocation_table"]
    try:
        described = dynamodb_client.describe_table(TableName=table_name)
    except Exception as exc:  # noqa: BLE001 - any failure to read the schema is fatal
        raise EvalPreconditionError(
            f"cannot describe invocation table {table_name!r}: {exc}"
        ) from exc

    schema = described.get("Table", {}).get("KeySchema", [])
    actual = tuple(
        (entry.get("AttributeName"), entry.get("KeyType")) for entry in schema
    )
    if actual != EXPECTED_KEY_SCHEMA:
        raise EvalPreconditionError(
            f"invocation table {table_name!r} has key schema {actual}, expected {EXPECTED_KEY_SCHEMA}. "
            "Refusing to write a control record against an unexpected key schema."
        )
    logger.info("invocation table key schema verified: %s", table_name)


def assert_check_manifest(
    results: list[CheckResult], expected_ids: tuple[str, ...] = EXPECTED_CHECK_IDS
) -> None:
    """Compare emitted check IDs against the closed manifest, for equality.

    Both directions are errors. Missing IDs mean the report claims less coverage
    than its name implies while looking complete. Unexpected IDs mean the harness
    and the evaluation file have diverged, so the reviewer cannot tell which set of
    checks the evidence actually represents. Duplicates are rejected too: two rows
    for one ID lets a pass and a fail coexist, and which one a reader believes
    depends on ordering.
    """
    emitted = [result.check_id for result in results]
    duplicates = sorted(
        {check_id for check_id in emitted if emitted.count(check_id) > 1}
    )
    if duplicates:
        raise EvalPreconditionError(f"duplicate check ids emitted: {duplicates}")

    expected = set(expected_ids)
    actual = set(emitted)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise EvalPreconditionError(
            f"check manifest mismatch — missing: {missing}, unexpected: {unexpected}. "
            "This harness must emit exactly the evaluation file's check IDs; a short report is a failed "
            "evaluation, not a partial one."
        )


class Probe:
    """Records every HTTP interaction with the gateway as reproducible evidence.

    Injected rather than constructed inside the checks so the guard tests can
    drive the whole driver without a network, and so a real run has exactly one
    place where a redirect policy or a timeout is set.
    """

    def __init__(self, base_url: str, client, *, timeout: float = 10.0):  # noqa: ANN001
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout
        self.log: list[Observation] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        role: str | None = None,
        token: str | None = None,
        json_body: object = None,
        raw_body: bytes | None = None,
    ) -> Observation:
        url = f"{self.base_url}{path}"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # The rendered command names the ROLE, never the bearer value. Redaction
        # is a second line of defence, not the mechanism.
        auth_note = f" -H 'Authorization: Bearer $<{role}>'" if role else " (no auth)"
        body_note = ""
        if raw_body is not None:
            body_note = f" --data-binary <{len(raw_body)} bytes>"
        elif json_body is not None:
            body_note = f" -d '{json.dumps(json_body, sort_keys=True)}'"
        observation = Observation(
            command=f"curl -sS -X {method} '{url}'{auth_note}{body_note}"
        )
        try:
            response = self._client.request(
                method,
                url,
                headers=headers,
                content=raw_body,
                json=None if raw_body is not None else json_body,
                timeout=self._timeout,
            )
            observation.status = response.status_code
            try:
                observation.body = response.json()
            except Exception:  # noqa: BLE001 - a non-JSON body is still evidence
                observation.body = response.text
        except Exception as exc:  # noqa: BLE001 - a transport failure is an observation
            observation.error = f"{type(exc).__name__}: {exc}"
        self.log.append(observation)
        return observation


class ArtifactStore:
    """Operator-recorded observations that only exist inside the cluster.

    Validated, not trusted. Three distinct answers, and the distinction is the
    point:

      * absent            → ``PrerequisiteMissingError`` (the check is ``not_run``)
      * present, missing keys → a failure (a claim without its evidence)
      * present and complete  → usable, and its path goes in the evidence list
    """

    def __init__(self, base_dir: Path | None, mapping: dict):
        self._base = base_dir
        self._mapping = mapping if isinstance(mapping, dict) else {}
        self._cache: dict[str, tuple[dict, str]] = {}

    def require(self, name: str) -> tuple[dict, str]:
        """Return ``(payload, path)`` for an artifact, or raise."""
        if name in self._cache:
            return self._cache[name]
        raw_path = self._mapping.get(name)
        if not raw_path:
            raise PrerequisiteMissingError(
                f"operator-recorded artifact {name!r} is not declared in the fixture config's "
                f"'artifacts' mapping. This observation only exists inside the cluster, so the harness "
                f"cannot make it from here; record it and point at it. Required keys: "
                f"{list(REQUIRED_ARTIFACT_KEYS.get(name, ()))}."
            )
        path = Path(raw_path)
        if not path.is_absolute() and self._base is not None:
            path = self._base / path
        if not path.is_file():
            raise PrerequisiteMissingError(
                f"artifact {name!r} is declared as {path} but no such file exists"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"artifact {name!r} at {path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AssertionError(f"artifact {name!r} at {path} must be a JSON object")
        missing = [key for key in REQUIRED_ARTIFACT_KEYS.get(name, ()) if key not in payload]
        if missing:
            raise AssertionError(
                f"artifact {name!r} at {path} is missing required keys {sorted(missing)}; an incomplete "
                "artifact is a claim without its evidence, so this is a failure rather than a skip"
            )
        self._cache[name] = (payload, str(path))
        return self._cache[name]


class Driver:
    """Executes the wave-1 predicates and records what it saw.

    Every ``check_*`` method either returns normally (pass), raises
    ``AssertionError`` (fail, with the reason), or raises
    ``PrerequisiteMissingError`` (not_run, naming what it wanted).
    """

    def __init__(self, config: dict, probe: Probe, artifacts: ArtifactStore, *, dynamodb=None):  # noqa: ANN001
        self.config = config
        self.probe = probe
        self.artifacts = artifacts
        self.dynamodb = dynamodb
        self._used_artifacts: list[str] = []

    # ---- helpers -------------------------------------------------------

    def _token(self, role: str) -> str:
        """Resolve one identity's bearer token from the environment."""
        env_map = self.config.get("identity_env") or {}
        var = env_map.get(role)
        if not var:
            raise PrerequisiteMissingError(
                f"fixture config declares no environment variable for the {role!r} identity under "
                f"'identity_env'. W1-02 needs {list(IDENTITY_ROLES)} to tell 'not yours' apart from "
                "'does not exist'."
            )
        value = os.environ.get(var)
        if not value:
            raise PrerequisiteMissingError(
                f"environment variable {var} (the {role!r} identity) is unset or empty"
            )
        return value

    def _require(self, key: str):  # noqa: ANN202
        value = self.config.get(key)
        if value in (None, "", [], {}):
            raise PrerequisiteMissingError(f"fixture config field {key!r} is required for this check")
        return value

    def _artifact(self, name: str) -> dict:
        payload, path = self.artifacts.require(name)
        if path not in self._used_artifacts:
            self._used_artifacts.append(path)
        return payload

    @staticmethod
    def _body_of(observation: Observation) -> dict:
        return observation.body if isinstance(observation.body, dict) else {}

    # ---- W1-01 ---------------------------------------------------------

    def check_w1_01(self) -> None:
        """Preflight and provenance: the record that makes the rest interpretable."""
        for key in ("live_run_id", "arrived_at", "generation", "tenant_id"):
            self._require(key)
        for role in IDENTITY_ROLES:
            self._token(role)
        provenance = self._artifact("provenance")

        if provenance["source_digest"] != provenance["deployed_digest"]:
            raise AssertionError(
                f"deployed digest {provenance['deployed_digest']!r} does not match the source digest "
                f"{provenance['source_digest']!r}: the evaluation would be describing a different build "
                "than the one under review (§7 rejects a stale deployment digest)"
            )
        failed_jobs = [
            name for name, status in (provenance.get("ci_jobs") or {}).items() if status != "passed"
        ]
        if failed_jobs:
            raise AssertionError(f"required CI jobs did not pass: {sorted(failed_jobs)}")
        if provenance["isolation_before_listener"] is not True:
            raise AssertionError(
                "isolation was not recorded as existing before listener start; DP-INV-1 requires the "
                "ingress policy to be applied before any enabled listener"
            )
        if provenance["ordinary_flags_off"] is not True:
            raise AssertionError("ordinary gateway/worker/SPA control flags were not recorded as false")

    # ---- W1-02 ---------------------------------------------------------

    def check_w1_02(self) -> None:
        """The authorization gate, on both adapters and all four verbs."""
        live = self._require("live_run_id")
        unknown = self._require("unknown_run_id")
        owner = self._token("owner")
        nonowner = self._token("nonowner")
        other_tenant = self._token("other_tenant")
        body = {"command_id": self._require("command_id")}

        for adapter, paths in ADAPTERS.items():
            for verb in CONTROL_VERBS:
                anonymous = self.probe.request(
                    "POST", paths["verb"].format(run_id=live, verb=verb), json_body=body
                )
                if anonymous.status != 401:
                    raise AssertionError(
                        f"{adapter}/{verb}: an unauthenticated command returned {anonymous.status}, "
                        "expected 401"
                    )

                # The three refusals that must be indistinguishable. Comparing the
                # bodies, not just the codes: a 404 whose message says "not yours"
                # is an enumeration oracle wearing a 404's clothes.
                refusals = {
                    "unknown_run": self.probe.request(
                        "POST",
                        paths["verb"].format(run_id=unknown, verb=verb),
                        role="owner",
                        token=owner,
                        json_body=body,
                    ),
                    "other_tenant": self.probe.request(
                        "POST",
                        paths["verb"].format(run_id=live, verb=verb),
                        role="other_tenant",
                        token=other_tenant,
                        json_body=body,
                    ),
                    "same_tenant_nonowner": self.probe.request(
                        "POST",
                        paths["verb"].format(run_id=live, verb=verb),
                        role="nonowner",
                        token=nonowner,
                        json_body=body,
                    ),
                }
                for label, observation in refusals.items():
                    if observation.status != 404:
                        raise AssertionError(
                            f"{adapter}/{verb}: {label} returned {observation.status}, expected 404"
                        )
                bodies = {label: json.dumps(self._body_of(obs), sort_keys=True) for label, obs in refusals.items()}
                if len(set(bodies.values())) != 1:
                    raise AssertionError(
                        f"{adapter}/{verb}: the three 404s are distinguishable by body, which lets a "
                        f"caller enumerate another tenant's runs: {bodies}"
                    )

                authorized = self.probe.request(
                    "POST",
                    paths["verb"].format(run_id=live, verb=verb),
                    role="owner",
                    token=owner,
                    json_body=body,
                )
                if authorized.status != 501:
                    raise AssertionError(
                        f"{adapter}/{verb}: an authorized owner returned {authorized.status}, expected "
                        "501 — every verb is unsupported in S1"
                    )

            state = self.probe.request(
                "GET", paths["state"].format(run_id=live), role="owner", token=owner
            )
            if state.status != 200:
                raise AssertionError(f"{adapter}: state read returned {state.status}, expected 200")
            capabilities = self._body_of(state).get("capabilities")
            if not isinstance(capabilities, dict):
                raise AssertionError(f"{adapter}: state response carries no capabilities object")
            enabled = sorted(verb for verb, value in capabilities.items() if value)
            if enabled:
                raise AssertionError(
                    f"{adapter}: capabilities advertise {enabled} as available, but S1 implements no "
                    "verb — a true capability puts a button on the dashboard whose handler returns 501"
                )

        listener = self._artifact("listener_auth")
        if listener["missing_token_status"] != 401 or listener["wrong_token_status"] != 401:
            raise AssertionError(
                "the pod did not answer 401 for a missing/wrong control token "
                f"(missing={listener['missing_token_status']}, wrong={listener['wrong_token_status']})"
            )
        if listener["rejected_before_verb_parse"] is not True:
            raise AssertionError(
                "the pod parsed the verb before authenticating the caller; authentication must come first"
            )

    # ---- W1-03 ---------------------------------------------------------

    def check_w1_03(self) -> None:
        """Token generation and expiry, without touching a real run's clock."""
        lifecycle = self._artifact("token_lifecycle")
        if lifecycle["before_expiry_status"] != 200:
            raise AssertionError(
                f"a valid short-lived token was refused before expiry "
                f"({lifecycle['before_expiry_status']})"
            )
        if lifecycle["after_expiry_status"] != 401:
            raise AssertionError(
                f"an expired token was accepted ({lifecycle['after_expiry_status']}), expected 401"
            )
        if lifecycle["stale_generation_status"] != 401:
            raise AssertionError(
                f"a stale generation was accepted ({lifecycle['stale_generation_status']}), expected 401 "
                "— this is the replay a retry pod makes possible"
            )
        if lifecycle["ordinary_clock_unchanged"] is not True:
            raise AssertionError(
                "expiry was demonstrated by changing a real run's clock; §7 requires a short-lived "
                "isolated token instead"
            )

        # The public read must not carry the token. Observed here directly rather
        # than taken from the artifact, because this is reachable from where the
        # harness runs and a self-observation is stronger evidence.
        live = self._require("live_run_id")
        owner = self._token("owner")
        state = self.probe.request(
            "GET", ADAPTERS["activity"]["state"].format(run_id=live), role="owner", token=owner
        )
        for forbidden in ("token", "control_token", "address", "pod_ip", "port"):
            if forbidden in self._body_of(state):
                raise AssertionError(
                    f"the public state response carries {forbidden!r}; the private half of the control "
                    "record is what lets a caller talk to the pod directly"
                )

    # ---- W1-04 ---------------------------------------------------------

    def check_w1_04(self) -> None:
        """Reachability for the gateway, and only the gateway."""
        probe_record = self._artifact("peer_probe")
        if probe_record["gateway_ping_status"] != 200:
            raise AssertionError(
                f"the authenticated gateway ping did not reach the fixture worker "
                f"({probe_record['gateway_ping_status']}), expected 200"
            )
        result = str(probe_record["probe_connect_result"]).lower()
        # A named pod that reports "refused"/"timeout" is the observation; a policy
        # document that merely *says* the port is closed is what §7 explicitly
        # rules out ("not policy YAML alone").
        if not any(token in result for token in ("refused", "timeout", "timed out", "unreachable")):
            raise AssertionError(
                f"the non-gateway probe pod {probe_record['probe_pod']!r} reported "
                f"{probe_record['probe_connect_result']!r}, which is not a failure to connect"
            )
        if not probe_record.get("policy_selectors"):
            raise AssertionError("no policy selectors were recorded alongside the connection result")
        timeout = probe_record["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise AssertionError(
                f"the probe's timeout must be finite and positive, got {timeout!r} — an unbounded wait "
                "cannot distinguish 'blocked' from 'still trying'"
            )

    # ---- W1-05 ---------------------------------------------------------

    def check_w1_05(self) -> None:
        """Malformed, over-reaching and oversized bodies, on every verb."""
        live = self._require("live_run_id")
        owner = self._token("owner")
        command_id = self._require("command_id")
        oversize = self.config.get("oversize_bytes", 32 * 1024)

        for adapter, paths in ADAPTERS.items():
            for verb in CONTROL_VERBS:
                path = paths["verb"].format(run_id=live, verb=verb)

                malformed = self.probe.request(
                    "POST", path, role="owner", token=owner, raw_body=b"{not json"
                )
                if malformed.status != 400:
                    raise AssertionError(
                        f"{adapter}/{verb}: malformed JSON returned {malformed.status}, expected 400"
                    )

                # actor/target/token must be REJECTED, not ignored: the only reason
                # to send them is to try to override the authenticated actor and
                # the registered transport target.
                for forbidden in ("actor", "target", "token"):
                    overreach = self.probe.request(
                        "POST",
                        path,
                        role="owner",
                        token=owner,
                        json_body={"command_id": command_id, forbidden: "injected"},
                    )
                    if overreach.status != 400:
                        raise AssertionError(
                            f"{adapter}/{verb}: a body carrying {forbidden!r} returned "
                            f"{overreach.status}, expected 400 — silently ignoring it would leave a "
                            "caller believing the override took effect"
                        )

                huge = self.probe.request(
                    "POST", path, role="owner", token=owner, raw_body=b"x" * int(oversize)
                )
                if huge.status != 413:
                    raise AssertionError(
                        f"{adapter}/{verb}: a {oversize}-byte body returned {huge.status}, expected 413"
                    )

        task = self._artifact("fixture_task")
        if task["completed"] is not True:
            raise AssertionError(
                "the fixture task did not complete after the rejected commands; a rejected command must "
                "not disturb the run"
            )
        expected_digest = self.config.get("expected_output_digest")
        if expected_digest and task["normalized_output_digest"] != expected_digest:
            raise AssertionError(
                f"the fixture task's normalized output digest changed: expected {expected_digest!r}, "
                f"recorded {task['normalized_output_digest']!r}"
            )

    # ---- W1-06 ---------------------------------------------------------

    def check_w1_06(self) -> None:
        """Terminal runs, and a worker that is no longer there."""
        terminal = self._require("terminal_run_id")
        owner = self._token("owner")
        nonowner = self._token("nonowner")
        other_tenant = self._token("other_tenant")
        body = {"command_id": self._require("command_id")}

        for adapter, paths in ADAPTERS.items():
            path = paths["verb"].format(run_id=terminal, verb="pause")
            owner_view = self.probe.request(
                "POST", path, role="owner", token=owner, json_body=body
            )
            if owner_view.status != 410:
                raise AssertionError(
                    f"{adapter}: the owner of a terminal run got {owner_view.status}, expected 410"
                )
            for label, token in (("nonowner", nonowner), ("other_tenant", other_tenant)):
                observation = self.probe.request(
                    "POST", path, role=label, token=token, json_body=body
                )
                if observation.status != 404:
                    raise AssertionError(
                        f"{adapter}: {label} got {observation.status} on a terminal run, expected 404 — "
                        "a 410 here would confirm the run exists to someone who may not know that"
                    )

        unavailable = self._artifact("worker_unavailable")
        if unavailable["state"] != "unavailable":
            raise AssertionError(
                f"a run with no reachable registration reported state {unavailable['state']!r}, "
                "expected 'unavailable'"
            )
        if unavailable["command_acknowledged"] is not False:
            raise AssertionError(
                "a dead worker acknowledged a command; an acknowledgement that nothing produced is the "
                "worst possible answer for an operator watching a run"
            )

        # Terminal teardown must clear the private fields from the row itself.
        self._assert_private_fields_cleared(terminal)

    def _assert_private_fields_cleared(self, run_id: str) -> None:
        table = self.config.get("invocation_table")
        arrived_at = self.config.get("terminal_arrived_at")
        if self.dynamodb is None or not arrived_at:
            raise PrerequisiteMissingError(
                "a DynamoDB client and 'terminal_arrived_at' are required to confirm terminal teardown "
                "cleared the private control fields (§7 requires event_id/arrived_at for DDB reads)"
            )
        observation = Observation(
            command=(
                f"aws dynamodb get-item --table-name {table} --consistent-read "
                f"--key '{{\"event_id\":{{\"S\":\"{run_id}\"}},\"arrived_at\":{{\"S\":\"{arrived_at}\"}}}}'"
            )
        )
        try:
            item = self.dynamodb.get_item(
                TableName=table,
                Key={"event_id": {"S": run_id}, "arrived_at": {"S": arrived_at}},
                ConsistentRead=True,
            ).get("Item", {})
        except Exception as exc:  # noqa: BLE001
            observation.error = f"{type(exc).__name__}: {exc}"
            self.probe.log.append(observation)
            raise AssertionError(f"cannot read the terminal invocation row: {exc}") from exc
        leftover = sorted(
            key for key in ("control_token", "control_address", "control_port", "control_token_expires_at")
            if key in item
        )
        observation.body = {"private_fields_present": leftover}
        self.probe.log.append(observation)
        if leftover:
            raise AssertionError(
                f"terminal teardown left private control fields on the row: {leftover}. Pod IPs are "
                "reused, so a stale address eventually names a different tenant's pod"
            )

    # ---- W1-07 ---------------------------------------------------------

    def check_w1_07(self) -> None:
        """Transport targets, and the absence of the private half from responses."""
        guard = self._artifact("transport_guard")
        blocked = guard["blocked_targets"] or {}
        # The families §7 names explicitly. Absent from the artifact is as bad as
        # recorded-but-allowed: an unlisted family is one nobody tested.
        for family in ("unregistered_ip", "wrong_port", "metadata", "link_local", "loopback", "public"):
            if family not in blocked:
                raise AssertionError(
                    f"no result recorded for the {family!r} target family; §7 requires each to be "
                    "blocked before transport"
                )
            if blocked[family] is not True:
                raise AssertionError(f"the {family!r} target family was not blocked: {blocked[family]!r}")
        if guard["redirect_blocked"] is not True:
            raise AssertionError("a redirect was followed; the test environment proxy must not redirect transport")
        if guard["blocked_before_transport"] is not True:
            raise AssertionError(
                "targets were rejected only after a connection attempt; validation must precede transport"
            )

        # Self-observed leakage scan over everything recorded so far, which is
        # strictly stronger than asking one endpoint: it covers every response
        # this run has already collected.
        secrets = [str(self.config[key]) for key in ("fixture_pod_ip",) if self.config.get(key)]
        secrets.extend(
            os.environ[var]
            for var in (self.config.get("identity_env") or {}).values()
            if var and os.environ.get(var)
        )
        for observation in self.probe.log:
            rendered = json.dumps(observation.to_evidence(), sort_keys=True)
            for secret in secrets:
                if secret and secret in rendered:
                    raise AssertionError(
                        "a recorded response or request log contains the pod address or a bearer token; "
                        f"found it in: {observation.command}"
                    )

    # ---- W1-08 ---------------------------------------------------------

    def check_w1_08(self) -> None:
        """Flag parity: turning the flag on must change nothing but metadata."""
        parity = self._artifact("flag_parity")
        if parity["flag_off_events_digest"] != parity["flag_on_events_digest"]:
            differing = parity.get("differing_fields") or []
            allowed = set(self.config.get("allowed_parity_fields") or ("control", "registration", "state"))
            unexpected = [
                name for name in differing if not any(token in str(name) for token in allowed)
            ]
            if unexpected:
                raise AssertionError(
                    f"flag-on changed task behaviour beyond declared control metadata: {unexpected}"
                )
        if parity["ordinary_flags_off"] is not True:
            raise AssertionError("ordinary gateway/worker/SPA flags were not recorded as off (DP-INV-1)")

        flag_off_url = self.config.get("flag_off_gateway_url")
        if not flag_off_url:
            raise PrerequisiteMissingError(
                "'flag_off_gateway_url' is required: AC-F2 needs an authorized request against a "
                "flag-off deployment to observe the 503 that follows authorization"
            )
        owner = self._token("owner")
        run_id = self._require("live_run_id")
        flag_off_probe = Probe(flag_off_url, self.probe._client)  # noqa: SLF001 - same injected client
        for adapter, paths in ADAPTERS.items():
            observation = flag_off_probe.request(
                "POST",
                paths["verb"].format(run_id=run_id, verb="pause"),
                role="owner",
                token=owner,
                json_body={"command_id": self._require("command_id")},
            )
            self.probe.log.append(observation)
            if observation.status != 503:
                raise AssertionError(
                    f"{adapter}: an authorized request on a flag-off deployment returned "
                    f"{observation.status}, expected 503 after authorization"
                )

    # ---- W1-09 ---------------------------------------------------------

    def check_w1_09(self) -> None:
        """Schema conformance of the live read contract, plus the journal proofs."""
        live = self._require("live_run_id")
        owner = self._token("owner")
        state = self.probe.request(
            "GET", ADAPTERS["activity"]["state"].format(run_id=live), role="owner", token=owner
        )
        if state.status != 200:
            raise AssertionError(f"state read returned {state.status}, expected 200")
        body = self._body_of(state)
        # The field list §7 names, checked for presence rather than truth: this
        # check is about the contract S7 will render, not about the values.
        required_fields = (
            "run_id",
            "generation",
            "available",
            "reason",
            "capabilities",
            "state",
            "active_tool_count",
            "updated_at",
            "commands",
        )
        missing = [name for name in required_fields if name not in body]
        if missing:
            raise AssertionError(
                f"the live state response is missing {missing}; S7 renders this and nothing else, so an "
                "absent field is a control the dashboard cannot describe"
            )
        ping = self.probe.request(
            "GET", ADAPTERS["activity"]["ping"].format(run_id=live), role="owner", token=owner
        )
        if ping.status != 200:
            raise AssertionError(f"ping returned {ping.status}, expected 200")
        for name in ("run_id", "available"):
            if name not in self._body_of(ping):
                raise AssertionError(f"the ping response is missing {name!r}")

        journal = self._artifact("journal_tests")
        for key in ("replay_same_id", "content_conflict", "bounds_enforced", "expiry_is_unknown"):
            if journal[key] is not True:
                raise AssertionError(f"journal proof {key!r} did not hold: {journal[key]!r}")
        turns = journal["assistant_turns"]
        if turns != 0:
            raise AssertionError(
                f"a state read caused {turns} assistant turn(s); polling a read contract must not cost "
                "model tokens or perturb the run"
            )

    # ---- W1-10 ---------------------------------------------------------

    def check_w1_10(self, *, emitted_ids: tuple[str, ...] = ()) -> None:
        """The harness's own negative coverage, and the fixtures being gone."""
        negatives = self._artifact("negative_tests")
        for key in REQUIRED_ARTIFACT_KEYS["negative_tests"]:
            if negatives[key] is not True:
                raise AssertionError(
                    f"the harness's negative test for {key!r} is not recorded as passing "
                    f"({negatives[key]!r}); these are the guards that become unverifiable exactly when "
                    "they matter"
                )
        # Self-referential on purpose: the report must contain every ID the
        # evaluation file lists, and this check is the one that says so.
        expected = set(EXPECTED_CHECK_IDS)
        if emitted_ids and set(emitted_ids) != expected:
            raise AssertionError(
                f"the result does not carry exactly the evaluation file's IDs: "
                f"missing {sorted(expected - set(emitted_ids))}, unexpected {sorted(set(emitted_ids) - expected)}"
            )


# Predicate lookup. Explicit rather than derived from ``dir()`` so a renamed
# method is an immediate KeyError instead of a silently shorter report.
WAVE1_PREDICATES: dict[str, str] = {
    "W1-01": "check_w1_01",
    "W1-02": "check_w1_02",
    "W1-03": "check_w1_03",
    "W1-04": "check_w1_04",
    "W1-05": "check_w1_05",
    "W1-06": "check_w1_06",
    "W1-07": "check_w1_07",
    "W1-08": "check_w1_08",
    "W1-09": "check_w1_09",
    "W1-10": "check_w1_10",
}


def run_checks(driver: Driver, specs: tuple[CheckSpec, ...] = WAVE1_CHECKS) -> list[CheckResult]:
    """Execute every predicate, converting outcomes into check results.

    One check's failure never stops the others: a partial report with nine real
    answers and one named failure is far more useful to the operator who has to
    fix it than an abort at the first problem.
    """
    results: list[CheckResult] = []
    for spec in specs:
        method_name = WAVE1_PREDICATES[spec.check_id]
        method = getattr(driver, method_name)
        result = CheckResult(
            check_id=spec.check_id,
            status=STATUS_PASSED,
            description=spec.description,
            acceptance_ids=spec.acceptance_ids,
        )
        before = len(driver.probe.log)
        try:
            if spec.check_id == "W1-10":
                method(emitted_ids=tuple(spec.check_id for spec in specs))
            else:
                method()
        except PrerequisiteMissingError as exc:
            result.status = STATUS_NOT_RUN
            result.message = f"prerequisite missing: {exc}"
            logger.warning("%s NOT RUN — %s", spec.check_id, exc)
        except AssertionError as exc:
            result.status = STATUS_FAILED
            result.message = str(exc)
            logger.error("%s FAILED — %s", spec.check_id, exc)
        except Exception as exc:  # noqa: BLE001 - an unexpected error is a failure, not a pass
            result.status = STATUS_FAILED
            result.message = f"unexpected {type(exc).__name__}: {exc}"
            logger.error("%s FAILED (unexpected) — %s", spec.check_id, exc)
        else:
            logger.info("%s passed", spec.check_id)
        result.observations = driver.probe.log[before:]
        result.artifacts = list(driver._used_artifacts)  # noqa: SLF001
        driver._used_artifacts = []  # noqa: SLF001
        results.append(result)
    return results


def build_report(
    config: dict,
    results: list[CheckResult],
    *,
    cleanup_ok: bool,
    wave: int = 1,
    expected_ids: tuple[str, ...] = EXPECTED_CHECK_IDS,
) -> dict:
    """Assemble the redacted evidence report in the shape §7's gate reads.

    ``checks`` is an OBJECT keyed by check ID — the operator's ``check()``
    function does ``.checks[$id]``, which cannot index a list. The aggregates are
    the six §7 names, with ``passed`` a COUNT compared against ``required``, not a
    boolean.

    ``cleanup_ok`` is part of the gate for the same reason it fails the run: a
    fixture left with the control listener enabled is the state DP-INV-1 forbids,
    so it cannot be reported as success no matter how the checks went.
    """
    counts = {STATUS_PASSED: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0, STATUS_NOT_RUN: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    report = {
        "harness": "agent-control-eval",
        "issue": "3960",
        "evaluation": "3967",
        "revision": "revival-2026-09-12",
        "wave": wave,
        "generated_at": _now(),
        "environment": config.get("environment"),
        "account_id": config.get("account_id"),
        "fixture_isolated": config.get("fixture_isolated"),
        "source_revision": config.get("source_digest"),
        "deployed_revision": config.get("deployed_digest"),
        "expected_check_ids": list(expected_ids),
        "checks": {result.check_id: result.to_evidence() for result in results},
        # §7 aggregate names, exactly.
        "required": len(expected_ids),
        "passed": counts[STATUS_PASSED],
        "failed": counts[STATUS_FAILED],
        "skipped": counts[STATUS_SKIPPED],
        "not_run": counts[STATUS_NOT_RUN],
        "cleanup_ok": cleanup_ok,
        "supported_verbs": [],
    }
    # Belt and braces: the whole report goes through redaction again, so a field
    # added later cannot leak by forgetting to call redact() at its own site.
    return redact(copy.deepcopy(report))


def report_is_passing(report: dict) -> bool:
    """The same predicate §7's `jq` expression evaluates.

    Duplicated in Python so the exit code and the gate cannot disagree — an exit 0
    that the operator's `jq` then rejects would be the worst of both.
    """
    return (
        report.get("failed") == 0
        and report.get("skipped") == 0
        and report.get("not_run") == 0
        and report.get("passed") == report.get("required")
        and report.get("cleanup_ok") is True
    )


def write_report(report: dict, evidence_dir: Path) -> Path:
    """Write ``result.json`` — the exact filename §7's gate reads."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "result.json"
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logger.info("evidence written: %s", path)
    return path


def run_cleanup(config: dict, dynamodb_client) -> tuple[bool, list[str]]:  # noqa: ANN001
    """Delete exactly the fixture rows, then confirm absence with a consistent read.

    Bounded by construction: it deletes only the ``(event_id, arrived_at)`` pairs
    the config names. §7 forbids purging a shared queue or deleting ordinary
    objects, so there is no scan, no prefix and no wildcard here — an item this
    function was not told about cannot be reached by it.
    """
    notes: list[str] = []
    items = config.get("cleanup_items") or []
    if not items:
        return True, ["no fixture rows declared for cleanup"]
    if dynamodb_client is None:
        return False, ["no DynamoDB client available to run cleanup"]

    ok = True
    table = config.get("invocation_table")
    for item in items:
        event_id = item.get("event_id")
        arrived_at = item.get("arrived_at")
        if not event_id or not arrived_at:
            ok = False
            notes.append(f"cleanup item {item!r} lacks event_id/arrived_at; refusing a partial-key delete")
            continue
        key = {"event_id": {"S": str(event_id)}, "arrived_at": {"S": str(arrived_at)}}
        try:
            dynamodb_client.delete_item(TableName=table, Key=key)
            remaining = dynamodb_client.get_item(
                TableName=table, Key=key, ConsistentRead=True
            ).get("Item")
        except Exception as exc:  # noqa: BLE001
            ok = False
            notes.append(f"cleanup failed for {event_id}/{arrived_at}: {type(exc).__name__}: {exc}")
            continue
        if remaining:
            ok = False
            notes.append(f"{event_id}/{arrived_at} still present after delete (consistent read)")
        else:
            notes.append(f"removed {event_id}/{arrived_at}; consistent read confirms absence")
    return ok, notes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent-control-eval.py",
        description="Live-control evaluation harness for Issue #3960. Requires an isolated fixture.",
    )
    # --wave and --evidence-dir are the names the published smoke command in the
    # issue and revival-design §7 actually passes. --output-dir is kept as a
    # deprecated alias so an operator following an older note is redirected
    # rather than getting an argparse error.
    parser.add_argument(
        "--wave",
        type=int,
        default=1,
        help=f"Which wave's checks to run. S1 delivers {list(SUPPORTED_WAVES)}.",
    )
    # Required with no default, deliberately: invoked bare this exits nonzero
    # rather than discovering a target.
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to the isolated fixture description (JSON).",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Directory to write result.json and raw observation artifacts into.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,  # deprecated alias for --evidence-dir
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and preconditions, then stop without contacting the control path.",
    )
    args = parser.parse_args(argv)
    if args.evidence_dir is None:
        args.evidence_dir = args.output_dir or Path("./test-results/agent-control")
    return args


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - each exit is a distinct diagnosis
    """Entry point. Every failure path returns a distinct nonzero code."""
    try:
        args = parse_args(argv)
    except SystemExit as exc:  # argparse already reported the problem
        return EXIT_CONFIG if exc.code else EXIT_OK

    if args.wave not in WAVE_CHECKS:
        logger.error(
            "wave %s has no checks in this revision. S1 delivers wave %s; waves 2-4 are extended by "
            "their owning stories (revival-design §7). Refusing rather than emitting an empty pass.",
            args.wave,
            list(SUPPORTED_WAVES),
        )
        return EXIT_CONFIG

    try:
        config = load_config(args.config)
    except EvalConfigError as exc:
        logger.error("config error: %s", exc)
        return EXIT_CONFIG

    dynamodb = None
    try:
        import boto3

        session = boto3.session.Session(
            region_name=config.get("aws_region", "us-east-1")
        )
        verify_account(config, session.client("sts"))
        dynamodb = session.client("dynamodb")
        verify_table_key_schema(config, dynamodb)
    except EvalPreconditionError as exc:
        logger.error("precondition failed: %s", exc)
        return EXIT_PRECONDITION
    except Exception as exc:  # noqa: BLE001
        logger.error("precondition could not be established: %s", exc)
        return EXIT_PRECONDITION

    if args.dry_run:
        logger.info(
            "dry-run: config and preconditions OK; not contacting the control path"
        )
        return EXIT_OK

    specs = WAVE_CHECKS[args.wave]
    try:
        import httpx

        client = httpx.Client(follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot construct an HTTP client: %s", exc)
        return EXIT_PRECONDITION

    probe = Probe(config["gateway_url"], client)
    artifacts = ArtifactStore(args.config.resolve().parent, config.get("artifacts") or {})
    driver = Driver(config, probe, artifacts, dynamodb=dynamodb)

    # Cleanup in `finally`: the failure path is exactly when a fixture is most
    # likely to be left with a live listener, which is the state DP-INV-1 forbids.
    # Raw evidence is written before cleanup runs (§7: "preserve raw evidence first").
    results: list[CheckResult] = []
    try:
        results = run_checks(driver, specs)
    finally:
        cleanup_ok, cleanup_notes = run_cleanup(config, dynamodb)
        for note in cleanup_notes:
            logger.info("cleanup: %s", note)
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing the client must not mask a result
            pass

    expected_ids = tuple(spec.check_id for spec in specs)
    try:
        assert_check_manifest(results, expected_ids)
    except EvalPreconditionError as exc:
        logger.error("manifest error: %s", exc)
        return EXIT_PRECONDITION

    report = build_report(
        config, results, cleanup_ok=cleanup_ok, wave=args.wave, expected_ids=expected_ids
    )
    report["cleanup_notes"] = redact(cleanup_notes)
    write_report(report, args.evidence_dir)

    logger.info(
        "wave %s: %s/%s passed, %s failed, %s not run, cleanup_ok=%s",
        args.wave,
        report["passed"],
        report["required"],
        report["failed"],
        report["not_run"],
        report["cleanup_ok"],
    )

    if not cleanup_ok:
        logger.error(
            "cleanup did not complete: a fixture left with a control listener enabled is the state "
            "DP-INV-1 forbids, so this is a failed evaluation even if every check passed."
        )
        return EXIT_CLEANUP
    if not report_is_passing(report):
        logger.error(
            "evaluation did not pass: %s failed, %s not run. A missing prerequisite is NOT RUN and "
            "nonzero, never a pass.",
            report["failed"],
            report["not_run"],
        )
        return EXIT_CHECKS_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
