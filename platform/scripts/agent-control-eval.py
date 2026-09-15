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

# The four verbs, all of which must answer 501 in S1 and still in S3.
CONTROL_VERBS: tuple[str, ...] = ("pause", "resume", "steer", "abort")

# The neutral control contract's protocol version, and the first production
# adapter's identity and pinned SDK. Mirrored from
# `modules/agent-factory/agent/src/control-runtime.ts` and
# `harnesses/claude-control.ts` rather than imported, for the same reason the
# request bodies above are mirrored: this harness runs standalone against a URL
# and must not acquire the agent module's dependency tree. A test pins these
# against the TypeScript sources, so a bump there surfaces as a harness test to
# update rather than as a live evaluation that silently accepts stale evidence.
CONTROL_PROTOCOL_VERSION = 1
CLAUDE_ADAPTER_ID = "claude"
EXPECTED_CLAUDE_SDK_VERSION = "0.3.220"

# `steer` is the one verb whose request model REQUIRES its free text: pause,
# resume and abort take the idempotency key plus an optional `reason`, while
# steer takes the key plus a non-empty length-bounded `instruction`
# (control_schemas.py: ControlCommandRequest vs ControlSteerRequest, both
# `extra="forbid"`).
#
# Mirrored here, not imported: the harness runs standalone against a URL and must
# not acquire the gateway's dependency tree. `valid_command_body` below is the
# single place that renders it, and a test pins that the shapes are per-verb — so
# a schema change on the gateway side surfaces as a harness test to update rather
# than as a silent 400 in a live evaluation.
STEER_VERB = "steer"
ABORTED_STATUS = "aborted"

# Short and deliberately inert. Every command in wave 1 is refused — by
# authorization, by the terminal-row gate, or by the 501 that follows both — so
# this text is never delivered to an agent. It is bounded well under
# MAX_INSTRUCTION_CHARS (4000) so it cannot be confused with the oversize probe
# in W1-05, which is a *different* leg that must keep being rejected.
STEER_INSTRUCTION = "evaluation probe: no action required"

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


def valid_command_body(verb: str, command_id: str) -> dict:
    """The body a given verb's request model accepts, for the ladder probes.

    Every check that exercises the *authorization* ladder — 401, three
    indistinguishable 404s, 410 on a terminal row, 503 with the flag off, 501 for
    an authorized owner — must send a body that passes schema validation first,
    because body validation deliberately precedes the authorization gate on both
    adapters (control_service.validate_command_body). A key-only body sent to
    `steer` therefore collapses every rung of that ladder into one 400, and the
    check reports a failure the deployment does not have.

    This is a one-body-per-verb helper rather than one shared literal because that
    shared literal was the defect: three of the four verbs accepted it, so the
    fourth's authorization behaviour was never observed at all.

    It is NOT used for the invalid-body legs of W1-05 (malformed JSON, an
    `actor`/`target`/`token` over-reach, an oversized payload). Those bodies are
    supposed to be rejected — routing them through here would retire the
    ordering guarantee that a 400 outranks the 501.
    """
    if verb == STEER_VERB:
        return {"command_id": command_id, "instruction": STEER_INSTRUCTION}
    return {"command_id": command_id}


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

# Transcribed from evaluation #3968's acceptance table (revision
# harness-neutral-2026-09-15), on the same rule §7 states for wave 1: the
# evaluation file's check IDs are authoritative.
#
# Keep all ten checks even while only S3 and S5 have landed: shrinking the
# manifest to implemented checks would turn incomplete Wave 2 into a false pass.
WAVE2_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "W2-01",
        ("Gate/regression",),
        "preflight includes accepted wave 1 evidence and current merged S3/S2/S5 "
        "revisions, protocol/adapter/package versions and capabilities; worker AND "
        "gateway digests verified, CI passed, fixture-only flags, required check "
        "inventory and cleanup configuration",
    ),
    CheckSpec(
        "W2-02",
        ("AC-T7",),
        "one neutral contract suite runs against Claude plus an independently shaped "
        "non-Claude test adapter with a missing capability; no provider SDK/types in "
        "the shared contract; capability intersection, normalized "
        "annotation/steering input, authorization at handoff, unknown outcomes, "
        "opaque attempt replacement, stale-event rejection and disposal once are "
        "proven; real Claude/version lifecycle plus forced idle/error retry proves "
        "fresh private input, preserved session/no-option behavior and cancel "
        "preventing another query; both adapter results recorded",
    ),
    CheckSpec(
        "W2-03",
        ("AC-P1",),
        "selected Claude adapter on the lockfile SDK with bypassPermissions and "
        "existing spill hooks through the neutral coordinator: pause_requested "
        "closes new admission, admitted tools finish, paused only with "
        "active_tool_count=0; across a timed hold new admissions, fixture writes, "
        "fixture service calls and task output are all zero; untracked activity or "
        "hook timeout yields unavailable/requested, never paused",
    ),
    CheckSpec(
        "W2-04",
        ("AC-P2",),
        "resume releases once; neutral attempt and Claude live Query/session "
        "identity plus prior history preserved; task completes; no interrupt call or "
        "replayed initial prompt; pending-resume and repeated-resume races serialized",
    ),
    CheckSpec(
        "W2-05",
        ("AC-P3", "AC-P5", "AC-P6"),
        "a shortened fixture timeout auto-resumes with one neutral annotation "
        "(Claude maps to shouldQuery:false), no extra assistant turn and no pod "
        "kill/idle retry/exit watchdog; heartbeats continue and paused differs from "
        "stalled; deadline clamp/no-budget rejection, held-hook timeout and "
        "cancellation without admitting blocked work are tested",
    ),
    CheckSpec(
        "W2-06",
        ("AC-A3", "AC-A9"),
        "provider-independent fixtures produce identical normalized outcome "
        "accounting and native interruption alone is not aborted; a uniquely named "
        "synthetic aborted invocation seeded through the real writer contract has "
        "completed_at populated; live API/detail plus browser fixtures show aborted "
        "in list, detail, card, chain and filter with no active/no-op fallback",
    ),
    CheckSpec(
        "W2-07",
        ("AC-A10",),
        "a dedicated four-category dataset increments total and aborted once, with "
        "today total=completed+failed+active+aborted; daily/persona buckets and "
        "mixed blocked/skipped/budget_stopped fixtures preserve existing accounting; "
        "isolated before/after deltas are asserted, never shared production totals",
    ),
    CheckSpec(
        "W2-08",
        ("AC-A11", "AC-A12"),
        "shared terminal/renderer parity, the existing guard and worker writer tests "
        "pass on merged head; the writer rejects an unknown status; both the "
        "deployed writer and the gateway readers support aborted",
    ),
    CheckSpec(
        "W2-09",
        ("AC-A10",),
        "live GET /api/me/agent-run-stats carries every declared field at each "
        "level — window_days/active_runs/today/daily/by_persona/recent_failures/"
        "top_repos/spend, today and daily and by_persona aborted counts, active-run "
        "and recent_failures and top_repos keys and nonnull spend — compared field "
        "by field against RunStatsResponse with nonempty seeded arrays",
    ),
    CheckSpec(
        "W2-10",
        ("Gate/regression",),
        "only synthetic rows are deleted using event_id AND arrived_at with a "
        "consistent get confirming absence; exact fixture workloads/probes removed "
        "and cleanup recorded even after failure; isolation and relevant wave 1 "
        "security rechecked on the current revision preserving #5029 "
        "admission/delivery authorization; unsupported adapters/verbs remain "
        "false/501 with no general flag enablement",
    ),
)

# S1 delivered wave 1; the wave owners extend the rest (§7: "S2/S5 extend wave 2;
# S4/S6 extend wave 3; S7 extends wave 4"). Asking for a wave with no manifest at
# all is an honest nonzero, not an empty pass.
WAVE_CHECKS: dict[int, tuple[CheckSpec, ...]] = {1: WAVE1_CHECKS, 2: WAVE2_CHECKS}
SUPPORTED_WAVES: tuple[int, ...] = tuple(sorted(WAVE_CHECKS))

# The evaluation issue that reads each wave's report, and the design revision that
# wave's checks were transcribed from. #3967 accepted wave 1 with 10/10 and is
# closed; #3968 owns wave 2's live acceptance.
WAVE_EVALUATIONS: dict[int, str] = {1: "3967", 2: "3968"}
WAVE_REVISIONS: dict[int, str] = {
    1: "revival-2026-09-12",
    2: "harness-neutral-2026-09-15",
}

# Which story owns each check whose predicate is not implemented yet, so a
# not_run says who to go to rather than just "missing". Registering a check with
# no predicate is deliberate — see WAVE2_CHECKS above — but it must never be
# indistinguishable from a check the harness forgot.
PENDING_CHECK_OWNERS: dict[str, str] = {
    "W2-01": "S2 #3961 — consolidated Wave 2 preflight after S3/S2/S5 merge",
    "W2-10": "evaluation #3968 — Wave 2 cleanup and security recheck",
}

# Retained for the manifest guard and for callers that only need wave 1's ID set.
# Deliberately still wave 1: it is the DEFAULT for `assert_check_manifest`, and a
# default that silently grew to span every wave would make a wave-1 report pass
# the manifest guard while missing nine checks.
EXPECTED_CHECK_IDS: tuple[str, ...] = tuple(spec.check_id for spec in WAVE1_CHECKS)

ALL_CHECK_SPECS: tuple[CheckSpec, ...] = tuple(
    spec for wave in sorted(WAVE_CHECKS) for spec in WAVE_CHECKS[wave]
)

# Keyed by check ID across every wave. Safe to span waves because these are only
# ever read as a per-ID fallback, and the IDs are globally unique by construction
# (W1-* / W2-*) — which a test pins, since two waves sharing an ID would make one
# check's evidence silently describe the other's.
CHECK_DESCRIPTIONS: dict[str, str] = {
    spec.check_id: spec.description for spec in ALL_CHECK_SPECS
}

CHECK_ACCEPTANCE_IDS: dict[str, tuple[str, ...]] = {
    spec.check_id: spec.acceptance_ids for spec in ALL_CHECK_SPECS
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
    # Wave 2 / S5 (#3964). Each of these is an observation the harness cannot make
    # over HTTP: two adapter fixtures normalized side by side, before/after counter
    # snapshots around a controlled seed, and the deployed digests of two separate
    # images. Declaring the required keys here means a partially filled artifact is
    # an immediate named failure rather than a KeyError mid-check.
    "harness_neutrality": (
        "adapter_a",
        "adapter_b",
        "native_interrupt_status",
        "shared_code_imports_sdk",
    ),
    "aborted_counters": (
        "today_before",
        "today_after",
        "seeded_aborted",
        "four_category_dataset",
        "mixed_dataset",
        "mixed_expected",
        "daily_deltas",
        "persona_deltas",
    ),
    "vocabulary_parity": (
        "writer_digest_deployed",
        "gateway_digest_deployed",
        "writer_allowed_statuses",
        "gateway_terminal_statuses",
        "unknown_status_rejected",
        "unknown_status_reached_table",
        "suites",
    ),
    "stats_schema_keys": ("levels",),
    # Wave 2 / S2 (#3961). The pause barrier's proof is inherently in-cluster: the
    # claim is about tool side effects during an interval, which no HTTP reader can
    # observe. Keys are declared per named property for the same reason as
    # neutral_contract below — "pause_suite_passed: true" cannot say which of AC-P1's
    # four zero-counters was actually measured.
    "pause_boundary": (
        "adapter_id",
        "sdk_version",
        "permission_mode",
        "spill_hooks_composed",
        "requested",
        "held_interval",
        "tool_coverage",
        "confirmed",
        "degraded",
    ),
    "pause_resume": (
        "released_count",
        "session_id_before",
        "session_id_after",
        "attempt_id_before",
        "attempt_id_after",
        "interrupt_called",
        "initial_prompt_replayed",
        "prior_history_preserved",
        "task_completed",
        "held_tools_admitted_after_resume",
        "races",
    ),
    "pause_expiry": (
        "auto_resumed",
        "annotation_count",
        "extra_assistant_turn",
        "neutral_annotation",
        "resolved_before_release",
        "pod_killed",
        "idle_retry_fired",
        "exit_watchdog_fired",
        "heartbeats_during_pause",
        "paused_distinguishable_from_stalled",
        "spill_output_preserved",
        "held_hook_timeout",
        "deadline_clamp",
        "cancellation",
    ),
    # W2-02 / AC-T7. One key per property the acceptance table names, rather than
    # a single "contract_suite_passed": a green suite is not the claim, the named
    # properties are, and a collapsed boolean cannot say which one is unproven.
    "neutral_contract": (
        "protocol_version",
        "adapter_id",
        "sdk_version",
        "sdk_matches_lockfile",
        "adapters",
        "second_adapter",
        "no_provider_types_in_shared_contract",
        "capability_intersection_proven",
        "normalized_input_kinds_proven",
        "authorization_at_handoff",
        "unknown_outcome_supported",
        "opaque_attempt_replacement",
        "stale_events_rejected",
        "disposed_once",
        "fresh_private_input_per_attempt",
        "session_and_no_option_behavior_preserved",
        "cancel_prevents_new_query",
        "forced_retry_exercised",
    ),
}

# Keys of the neutral_contract artifact that carry data rather than a proof
# boolean. Listed explicitly so the boolean loop cannot accidentally demand
# ``adapters is True``, and so adding a proof key without listing it here is
# checked by default rather than skipped by default.
_NEUTRAL_CONTRACT_NON_BOOLEAN_KEYS: frozenset[str] = frozenset(
    {"protocol_version", "adapter_id", "sdk_version", "adapters", "second_adapter"}
)

# Why each neutral-contract property is required, quoted into the failure message.
# A report that says `disposed_once: False` tells an operator what was observed;
# it does not tell them why anyone cared, and this evidence gets read by people who
# did not write the story.
_NEUTRAL_CONTRACT_WHY: dict[str, str] = {
    "no_provider_types_in_shared_contract": (
        "A shared consumer that imports the provider SDK or exposes Query/SDKUserMessage/AsyncIterable "
        "input means the next harness cannot arrive without editing pause and abort code."
    ),
    "capability_intersection_proven": (
        "Capability selection is the intersection of ADP-implemented verbs, adapter support and current "
        "availability; combined any other way, one input can enable a verb the other two refuse."
    ),
    "normalized_input_kinds_proven": (
        "Steering starts a turn and an annotation records context without starting one. Collapsing them "
        "makes an auto-resume note cost a model turn the operator never asked for."
    ),
    "authorization_at_handoff": (
        "#5029 requires revalidation immediately before physical handoff. A buffer that revalidates "
        "earlier delivers instructions authorized by a grant that has since been revoked."
    ),
    "unknown_outcome_supported": (
        "An ambiguous handoff must resolve unknown rather than delivered-or-rejected: 'unknown' never "
        "triggers replay, while a wrong 'rejected' resends an instruction that already landed."
    ),
    "opaque_attempt_replacement": (
        "Attempt identity must be opaque and replaceable, or a control aimed at the run reaches a "
        "session that was torn down and rebuilt underneath it."
    ),
    "stale_events_rejected": (
        "A replaced attempt's events must be inert rather than an error path — a retry is normal "
        "operation, and treating its late events as failures makes every retry look like a fault."
    ),
    "disposed_once": (
        "Disposal must happen exactly once. Two owners each disposing correctly once is still a double "
        "close, which is the defect this property exists to keep fixed."
    ),
    "fresh_private_input_per_attempt": (
        "Each attempt needs its own open input channel: an iterable a previous query consumed is "
        "exhausted, so a reused one yields an attempt that looks live and can never receive a command."
    ),
    "session_and_no_option_behavior_preserved": (
        "The one-time fail-soft session callback and the exact no-option query shape are relied on by "
        "callers outside the control path; changing them makes unrelated runs repeat work."
    ),
    "cancel_prevents_new_query": (
        "Cancellation during setup, backoff or idle must not launch another query nor be classified as "
        "a retryable error — cancellation text contains words the retry patterns match."
    ),
    "forced_retry_exercised": (
        "The retry-safety properties are only evidence if a retry actually happened; asserted on a run "
        "that never retried, they are all vacuously true."
    ),
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
        command_id = self._require("command_id")

        for adapter, paths in ADAPTERS.items():
            for verb in CONTROL_VERBS:
                # Per-verb, because body validation runs before authorization: a
                # body `steer` rejects turns all five rungs below into one 400.
                body = valid_command_body(verb, command_id)
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
        body = valid_command_body("pause", self._require("command_id"))

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
                json_body=valid_command_body("pause", self._require("command_id")),
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

    # ---- W2-02 ---------------------------------------------------------

    def check_w2_02(self) -> None:
        """The harness-neutral adapter contract (AC-T7, owned by S3 #3962).

        Two halves, and the split is deliberate.

        The contract suite itself runs where the code is — a jest run over the
        neutral contract, the Claude adapter and the independently shaped test
        adapter — so it is consumed here as an operator-recorded artifact,
        validated and not trusted, exactly like every other observation that
        cannot be made from outside the cluster. That is not a weaker form of
        evidence than an HTTP probe: it is a *different* observation, and the
        story is explicit that development and PR tests need no AWS credential.

        The second half the harness does make itself: it reads the deployed
        capability surface. That matters because the artifact describes the source
        tree while the evaluation is about a deployment, and the failure this
        catches is a green suite paired with a build that advertises a verb. §7's
        digest guard makes them the same revision; this makes them the same
        *behaviour*.
        """
        contract = self._artifact("neutral_contract")

        # --- protocol and adapter identity -----------------------------------
        if contract["protocol_version"] != CONTROL_PROTOCOL_VERSION:
            raise AssertionError(
                f"the recorded control protocol version is {contract['protocol_version']!r}, but this "
                f"harness verifies version {CONTROL_PROTOCOL_VERSION}. A protocol change must update the "
                "gateway peer, the adapter and this harness together, so a mismatch means one of the "
                "three is describing a different contract than the other two."
            )
        if contract["adapter_id"] != CLAUDE_ADAPTER_ID:
            raise AssertionError(
                f"the production adapter recorded is {contract['adapter_id']!r}, expected "
                f"{CLAUDE_ADAPTER_ID!r}. Claude is the first production adapter in this wave; a different "
                "selection is not accepted live second-harness support."
            )
        if contract["sdk_version"] != EXPECTED_CLAUDE_SDK_VERSION:
            raise AssertionError(
                f"the adapter was exercised against SDK {contract['sdk_version']!r}, but the lockfile "
                f"pins {EXPECTED_CLAUDE_SDK_VERSION!r}. The streaming-input and shouldQuery behaviours "
                "this adapter relies on are observed SDK behaviour rather than a documented permanent "
                "guarantee, so evidence from a different version does not carry over."
            )
        if contract["sdk_matches_lockfile"] is not True:
            raise AssertionError(
                "'sdk_matches_lockfile' is not True: the installed SDK was not recorded as matching the "
                "lockfile, so the evaluation would be describing a dependency tree the deployment does "
                "not have"
            )

        # --- both adapters, and the second one genuinely differently shaped ---
        adapters = contract["adapters"]
        if not isinstance(adapters, dict) or len(adapters) < 2:
            raise AssertionError(
                f"the contract suite must record a result for BOTH adapters, got {adapters!r}. One "
                "adapter passing a neutral suite proves the suite runs, not that the contract is neutral."
            )
        if CLAUDE_ADAPTER_ID not in adapters:
            raise AssertionError(
                f"no result recorded for the {CLAUDE_ADAPTER_ID!r} adapter: {sorted(adapters)}"
            )
        others = [name for name in adapters if name != CLAUDE_ADAPTER_ID]
        if not others:
            raise AssertionError(
                "only the Claude adapter was exercised; AC-T7 requires an independently shaped "
                "non-Claude adapter, which is what distinguishes a neutral contract from a Claude "
                "contract with an interface in front of it"
            )
        for name, outcome in adapters.items():
            if not isinstance(outcome, dict):
                raise AssertionError(f"adapter {name!r} result must be an object, got {outcome!r}")
            if outcome.get("passed") is not True:
                raise AssertionError(
                    f"the neutral contract suite did not pass against the {name!r} adapter: {outcome!r}"
                )
            # A suite that ran zero tests passes. Both adapters must have been
            # driven through real assertions for "both passed" to mean anything.
            count = outcome.get("test_count")
            if type(count) is not int or count <= 0:
                raise AssertionError(
                    f"adapter {name!r} records {count!r} tests; a suite that ran nothing reports "
                    "success, so a positive count is what makes 'passed' evidence"
                )

        # The second adapter must be missing a capability the Claude one has, and
        # must not imitate the provider. Both are what force the contract to be
        # exercised rather than merely satisfied by a look-alike.
        second = contract["second_adapter"]
        if not isinstance(second, dict):
            raise AssertionError("second_adapter must be an object")
        if second.get("name") not in others:
            raise AssertionError(
                f"second_adapter names {second.get('name')!r}, which is not among the non-Claude "
                f"adapter results {sorted(others)}"
            )
        if second.get("declares_missing_capability") is not True:
            raise AssertionError(
                "the second adapter does not declare a missing capability; without one, capability "
                "intersection is never observed doing anything and an adapter that ignored support "
                "entirely would pass"
            )
        if second.get("imports_provider_sdk") is not False:
            raise AssertionError(
                "the second adapter was recorded as importing the provider SDK; an adapter that mimics "
                "Query/SDKUserMessage proves the shared contract accepts Claude's shape, which is the "
                "opposite of the property under test"
            )

        # --- the named contract properties ------------------------------------
        # Each is a distinct failure mode named by the acceptance table, checked
        # individually so a report says WHICH property is unproven. An `all(...)`
        # over the group would collapse seven answers into one boolean.
        for prop in REQUIRED_ARTIFACT_KEYS["neutral_contract"]:
            if prop in _NEUTRAL_CONTRACT_NON_BOOLEAN_KEYS:
                continue
            if contract[prop] is not True:
                raise AssertionError(
                    f"neutral-contract property {prop!r} is recorded as {contract[prop]!r}, not True. "
                    f"{_NEUTRAL_CONTRACT_WHY.get(prop, '')}".rstrip()
                )

        # --- the deployed surface must agree ----------------------------------
        # S3 has no implemented verbs; S2 adds pause/resume later in this wave.
        # Record the source build's expectations rather than freezing the final
        # Wave 2 gate at S3's temporary capability surface.
        implemented = contract.get("implemented_verbs", [])
        if not isinstance(implemented, list) or implemented not in ([], ["pause", "resume"]):
            raise AssertionError("implemented_verbs must be [] (S3) or ['pause', 'resume'] (S2)")
        live = self._require("live_run_id")
        owner = self._token("owner")
        for adapter, paths in ADAPTERS.items():
            state = self.probe.request(
                "GET", paths["state"].format(run_id=live), role="owner", token=owner
            )
            if state.status != 200:
                raise AssertionError(f"{adapter}: state read returned {state.status}, expected 200")
            capabilities = self._body_of(state).get("capabilities")
            if not isinstance(capabilities, dict):
                raise AssertionError(f"{adapter}: state response carries no capabilities object")
            # Presence, not just truth: `capabilities.get(verb)` is falsy for an
            # absent key, so a dropped verb would read as "unsupported" while the
            # deployed contract said nothing about it at all.
            absent = [verb for verb in CONTROL_VERBS if verb not in capabilities]
            if absent:
                raise AssertionError(
                    f"{adapter}: the deployed capability map omits {absent}; the intersection must "
                    "produce an explicit answer for every verb, because an absent key and a false one "
                    "are indistinguishable to the dashboard but not to the contract"
                )
            invalid = [verb for verb, value in capabilities.items() if type(value) is not bool]
            if invalid:
                raise AssertionError(f"{adapter}: capabilities must be booleans: {invalid}")
            enabled = sorted(verb for verb, value in capabilities.items() if value)
            if enabled != sorted(implemented):
                raise AssertionError(
                    f"{adapter}: deployed capabilities {enabled} disagree with the tested build's "
                    f"implemented_verbs {implemented}"
                )



    def check_w2_06(self) -> None:
        """Aborted is terminal end-to-end, and native interruption alone is not.

        The seeded row is read back through the live API rather than asserted from
        the write: the whole point of AC-A3 is what the READERS do with the status,
        and a writer test cannot see a reader that still treats the row as active.
        """
        run_id = self._require("aborted_run_id")
        owner = self._token("owner")

        detail = self.probe.request(
            "GET", f"/me/agent-invocations/{run_id}", role="owner", token=owner
        )
        if detail.status != 200:
            raise AssertionError(
                f"the seeded aborted invocation returned {detail.status}, expected 200"
            )
        body = self._body_of(detail)
        if body.get("status") != ABORTED_STATUS:
            raise AssertionError(
                f"the seeded row reports status {body.get('status')!r}, expected {ABORTED_STATUS!r} — "
                "if the writer normalized it to something else, every reader below is testing the "
                "wrong row"
            )
        # AC-A3. `completed_at` null on a terminal row is the defect this check
        # exists for: the run is over, and a null here is what makes the dashboard
        # keep it in the active set forever.
        if not body.get("completed_at"):
            raise AssertionError(
                f"the aborted run has completed_at={body.get('completed_at')!r}; a terminal row with no "
                "completion timestamp reads as still running (AC-A3)"
            )
        if body.get("liveness") != "exited":
            raise AssertionError(
                f"the aborted run reports liveness {body.get('liveness')!r}, expected 'exited' — a "
                "deliberately stopped run was positively observed to end, which is exactly the "
                "evidence 'exited' requires"
            )

        # AC-A9: the row must be reachable BY the aborted filter, not merely
        # present in an unfiltered list. A filter that silently returns everything
        # would satisfy a presence-only assertion.
        listed = self.probe.request(
            "GET",
            f"/me/agent-invocations?status={ABORTED_STATUS}&page_size=50",
            role="owner",
            token=owner,
        )
        if listed.status != 200:
            raise AssertionError(f"the aborted filter returned {listed.status}, expected 200")
        items = self._body_of(listed).get("items") or []
        if not any(item.get("invocation_id") == run_id for item in items):
            raise AssertionError(
                f"the seeded aborted run {run_id!r} is not returned by status={ABORTED_STATUS}; the "
                "filter option exists but does not select the rows it names (AC-A9)"
            )
        foreign = sorted(
            {item.get("status") for item in items if item.get("status") != ABORTED_STATUS}
        )
        if foreign:
            raise AssertionError(
                f"the aborted filter also returned {foreign}; a filter that ignores its argument would "
                "have passed the presence check above"
            )

        # Harness neutrality: two differently named adapter fixtures whose native
        # outcomes normalize to the same thing must produce identical accounting,
        # and a native interruption with no confirmed ADP abort finalization must
        # NOT have become an aborted run.
        neutrality = self._artifact("harness_neutrality")
        first, second = neutrality["adapter_a"], neutrality["adapter_b"]
        if not isinstance(first, dict) or not first or not isinstance(second, dict) or not second:
            raise AssertionError("both adapters must record nonempty normalized outcome accounting")
        if first != second:
            raise AssertionError(
                f"two adapters' normalized outcome accounting differs: {first!r} vs {second!r}. The "
                "shared writers and readers must not be able to tell which harness produced a run"
            )
        if neutrality["native_interrupt_status"] == ABORTED_STATUS:
            raise AssertionError(
                "a native interrupted turn with no confirmed ADP abort finalization was recorded as "
                f"{ABORTED_STATUS!r}. Only a confirmed abort finalization may carry this status; "
                "pattern-matching a provider's interrupt string is the specific error forbidden here"
            )
        if neutrality["shared_code_imports_sdk"] is not False:
            raise AssertionError(
                "the shared writer/reader path was recorded as importing a provider SDK; the shared "
                "contract must not depend on any one harness"
            )

    # ---- W2-07 (S5 / #3964) --------------------------------------------

    def check_w2_07(self) -> None:
        """Additive counting: once each, and nothing else reclassified.

        Deltas, never absolute totals. §7 forbids asserting against shared
        production numbers, and an equality on a live tenant's totals would be
        flaky for reasons that have nothing to do with this story.
        """
        counters = self._artifact("aborted_counters")

        before, after = counters["today_before"], counters["today_after"]
        seeded = counters["seeded_aborted"]

        if type(seeded) is not int or seeded <= 0:
            raise AssertionError("seeded_aborted must be a positive row count")

        delta_total = after["total"] - before["total"]
        delta_aborted = after[ABORTED_STATUS] - before[ABORTED_STATUS]
        if delta_total != seeded or delta_aborted != seeded:
            raise AssertionError(
                f"seeding {seeded} aborted row(s) moved total by {delta_total} and aborted by "
                f"{delta_aborted}; each aborted row must contribute exactly once to each (AC-A10)"
            )
        # The other three buckets must not move at all. This is the "counted once"
        # half that a total-only assertion cannot see: a row counted into both
        # `aborted` and `failed` keeps `total` correct while doubling the failure
        # rate an operator is judged on.
        for bucket in ("completed", "failed", "active"):
            moved = after[bucket] - before[bucket]
            if moved != 0:
                raise AssertionError(
                    f"seeding aborted rows moved {bucket!r} by {moved}; an aborted run must never also "
                    f"count as {bucket} (AC-A10)"
                )

        # The four-way equality, asserted ONLY on the dedicated four-category
        # dataset. It does not hold in general — blocked/skipped/no_op rows are
        # counted in `total` and in none of the four buckets — so asserting it on a
        # mixed dataset would be a false claim that someone would later "fix" by
        # breaking the counters.
        four = counters["four_category_dataset"]
        bucket_sum = four["completed"] + four["failed"] + four["active"] + four[ABORTED_STATUS]
        if four["total"] != bucket_sum:
            raise AssertionError(
                f"on the controlled four-category dataset total={four['total']} but the buckets sum to "
                f"{bucket_sum}; with exactly these four outcomes present they must agree"
            )

        # Mixed dataset: existing outcomes preserved, and the four-way equality
        # explicitly NOT claimed.
        mixed = counters["mixed_dataset"]
        mixed_sum = mixed["completed"] + mixed["failed"] + mixed["active"] + mixed[ABORTED_STATUS]
        if mixed["total"] <= mixed_sum:
            raise AssertionError(
                f"the mixed dataset's total ({mixed['total']}) does not exceed its four buckets "
                f"({mixed_sum}); it is supposed to contain blocked/skipped/budget_stopped rows that "
                "count toward total only. If it no longer does, it is not testing preservation"
            )
        for bucket, expected in (counters["mixed_expected"] or {}).items():
            if mixed[bucket] != expected:
                raise AssertionError(
                    f"mixed dataset bucket {bucket!r} is {mixed[bucket]}, expected {expected}: adding "
                    "aborted reclassified a pre-existing outcome"
                )

        for scope in ("daily", "persona"):
            entry = counters[f"{scope}_deltas"]
            if entry.get(ABORTED_STATUS) != seeded:
                raise AssertionError(
                    f"the {scope} breakdown moved aborted by {entry.get(ABORTED_STATUS)}, expected "
                    f"{seeded}; the per-{scope} counter is a separate accumulator and can drift from "
                    "today's independently"
                )
            for bucket in ("completed", "failed"):
                if entry.get(bucket, 0) != 0:
                    raise AssertionError(
                        f"the {scope} breakdown moved {bucket!r} by {entry.get(bucket)}, expected 0"
                    )

    # ---- W2-08 (S5 / #3964) --------------------------------------------

    def check_w2_08(self) -> None:
        """Parity across the split: the deployed writer and the deployed readers.

        The two live in different images and ship through different workflows
        (`agent-worker-image.yml` and `gateway-deploy.yml`), so "merged" does not
        imply "both deployed" — a gateway that understands aborted in front of a
        worker that cannot write it is a silent half-deployment.
        """
        parity = self._artifact("vocabulary_parity")

        for name in ("writer_digest_deployed", "gateway_digest_deployed"):
            if parity[name] is not True:
                raise AssertionError(
                    f"{name} is {parity[name]!r}: the aborted vocabulary spans the worker image and the "
                    "gateway, and both must be the reviewed revision before abort is enabled"
                )
        if ABORTED_STATUS not in (parity["writer_allowed_statuses"] or []):
            raise AssertionError(
                f"the deployed writer's allowlist does not contain {ABORTED_STATUS!r}; the abort's own "
                "terminal write would be refused and the run would read as live forever (AC-A12)"
            )
        if ABORTED_STATUS not in (parity["gateway_terminal_statuses"] or []):
            raise AssertionError(
                f"the deployed gateway's terminal set does not contain {ABORTED_STATUS!r} (AC-A11)"
            )
        # The reject path, which is the load-bearing half: an allowlist that
        # accepts everything is indistinguishable from no allowlist at all until
        # something unknown arrives.
        if parity["unknown_status_rejected"] is not True:
            raise AssertionError(
                "the deployed writer did not reject an unknown status; an allowlist whose reject path "
                "never fires is not a validation (AC-A12)"
            )
        if parity["unknown_status_reached_table"] is not False:
            raise AssertionError(
                "an unknown status reached the invocation table despite being rejected; validation must "
                "happen BEFORE the write, not be corrected after it"
            )
        suites = parity["suites"]
        required_suites = {
            "tests/activity/test_status_aborted.py",
            "tests/test_status_vocabulary.py",
            "src/__tests__/utils/status.test.ts",
            "src/__tests__/components/InvocationChain.test.tsx",
        }
        if not isinstance(suites, dict) or not required_suites.issubset(suites):
            raise AssertionError("vocabulary parity must include every required writer/reader/renderer suite")
        failed_suites = [name for name, status in suites.items() if status != "passed"]
        if failed_suites:
            raise AssertionError(
                f"shared vocabulary/renderer parity suites did not pass on merged head: "
                f"{sorted(failed_suites)}"
            )

    # ---- W2-09 (S5 / #3964) --------------------------------------------

    def check_w2_09(self) -> None:
        """The live stats contract, key by key, at every level.

        Presence, not values: this check is about whether the response the SPA
        destructures actually carries the fields it reads. A missing key is an
        `undefined` in a dashboard, which renders as a blank rather than an error.
        """
        fixture = self._artifact("stats_schema_keys")
        owner = self._token("owner")
        observation = self.probe.request(
            "GET", "/me/agent-run-stats?days=7", role="owner", token=owner
        )
        if observation.status != 200:
            raise AssertionError(f"agent-run-stats returned {observation.status}, expected 200")
        body = self._body_of(observation)

        top_level = (
            "window_days",
            "active_runs",
            "today",
            "daily",
            "by_persona",
            "recent_failures",
            "top_repos",
            "spend",
        )
        missing = [name for name in top_level if name not in body]
        if missing:
            raise AssertionError(f"the stats response is missing top-level keys {missing}")

        today_keys = ("total", "completed", "failed", "active", ABORTED_STATUS)
        missing = [name for name in today_keys if name not in (body.get("today") or {})]
        if missing:
            raise AssertionError(
                f"`today` is missing {missing}; the aborted counter is the field this story adds and an "
                "absent key is indistinguishable from zero to every client"
            )

        # Arrays must be NONEMPTY before their keys mean anything: an empty list
        # trivially satisfies "every element has the required keys".
        for name, required in (
            ("daily", ("date", "total", "completed", "failed", ABORTED_STATUS)),
            ("by_persona", ("persona", "total", "completed", "failed", ABORTED_STATUS)),
            ("active_runs", ("invocation_id", "invoked_at", "persona", "repo", "topic")),
            (
                "recent_failures",
                ("invocation_id", "invoked_at", "persona", "repo", "topic", "error_message"),
            ),
            ("top_repos", ("repo", "total")),
        ):
            rows = body.get(name) or []
            if not rows:
                raise PrerequisiteMissingError(
                    f"`{name}` is empty, so its keys cannot be verified. §7 requires seeded nonempty "
                    "arrays: an empty list satisfies any per-element assertion vacuously"
                )
            for index, row in enumerate(rows):
                absent = [key for key in required if key not in row]
                if absent:
                    raise AssertionError(f"`{name}[{index}]` is missing {absent}")

        spend = body.get("spend")
        if not isinstance(spend, dict):
            raise PrerequisiteMissingError(
                f"`spend` is {spend!r}, so its keys cannot be verified; §7 requires a nonnull spend "
                "aggregate, which means the fixture runs must have recorded cost"
            )
        absent = [
            key for key in ("total_cost_usd", "total_tokens", "total_calls") if key not in spend
        ]
        if absent:
            raise AssertionError(f"`spend` is missing {absent}")

        # The comparison the issue asks for in the other direction: every key the
        # backend schema declares, checked against the live response. Presence
        # checks above are a fixed list in this file and would not notice a field
        # ADDED to the schema and omitted by the deployment.
        levels = fixture["levels"]
        required_levels = {"response", "today", "daily", "by_persona", "active_runs", "recent_failures", "top_repos", "spend"}
        if not isinstance(levels, dict) or not required_levels.issubset(levels):
            raise AssertionError("schema-derived keys must cover every stats response level")
        for level, expected_keys in levels.items():
            if not isinstance(expected_keys, list) or not expected_keys or not all(isinstance(key, str) and key for key in expected_keys):
                raise AssertionError(f"schema-derived keys for {level!r} must be a nonempty string list")
            actual = body if level == "response" else body.get(level)
            actual_keys = set(
                actual.keys()
                if isinstance(actual, dict)
                else (actual[0].keys() if isinstance(actual, list) and actual else ())
            )
            absent = sorted(set(expected_keys) - actual_keys)
            if absent:
                raise AssertionError(
                    f"schema-derived keys missing from the live `{level}`: {absent}. The fixture is "
                    "exported from the backend models, so this catches a field the deployment predates"
                )


    # ---- W2-03 (S2 / #3961) --------------------------------------------

    def check_w2_03(self) -> None:
        """Pause closes the tool boundary, and `paused` means it (AC-P1).

        The assertion that carries this check is about **side effects, not
        statuses**. A run can report `paused` while a tool writes a file, and every
        weaker form of this check — a check-run count, a command status, a quiet
        progress display — would pass in exactly that case. So the fixture is
        required to hold a pause across an interval and record what the run did to
        the world during it: files written, service calls made, tool invocations
        admitted, task output produced. All four must be zero.

        The state read is the second half rather than the first, because a
        `paused` state whose zero counters are missing is the failure this check
        exists to catch, not evidence.
        """
        pause = self._artifact("pause_boundary")

        # --- the mechanism actually under test -------------------------------
        if pause["adapter_id"] != CLAUDE_ADAPTER_ID:
            raise AssertionError(
                f"the pause evidence is from adapter {pause['adapter_id']!r}, expected "
                f"{CLAUDE_ADAPTER_ID!r}: Claude is this wave's production adapter, and another "
                "adapter's barrier is not evidence for the one that ships"
            )
        if pause["sdk_version"] != EXPECTED_CLAUDE_SDK_VERSION:
            raise AssertionError(
                f"pause was exercised against SDK {pause['sdk_version']!r} but the lockfile pins "
                f"{EXPECTED_CLAUDE_SDK_VERSION!r}. The hook-timeout and PreToolUse behaviours the "
                "barrier rests on are observed SDK behaviour, so evidence from another version does "
                "not carry over"
            )
        if pause.get("permission_mode") != "bypassPermissions":
            raise AssertionError(
                f"the experiment ran with permission_mode {pause.get('permission_mode')!r}, expected "
                "'bypassPermissions'. A run that asks permission before each tool would appear to "
                "contain side effects no matter whether the barrier works"
            )
        if pause.get("spill_hooks_composed") is not True:
            raise AssertionError(
                "'spill_hooks_composed' is not True: the barrier must be proven with the existing "
                "spill hooks in place, because composition is where a PreToolUse addition could "
                "displace another hook's output"
            )

        # --- pause_requested closes admission immediately --------------------
        # Each nested object is shape-checked before it is read: `REQUIRED_ARTIFACT_KEYS`
        # guarantees the key is present, not that it holds an object, and an
        # AttributeError from deep in a predicate tells an operator far less than a
        # sentence naming the field.
        requested = pause["requested"]
        if not isinstance(requested, dict):
            raise AssertionError(f"`requested` must be an object, got {requested!r}")
        if requested.get("admission_closed") is not True:
            raise AssertionError(
                "admission was not closed at `pause_requested`: the operator-visible state and the "
                "barrier must change together, or the run keeps starting tools while the dashboard "
                "says it is pausing"
            )

        # --- the interval, and what the run did during it ---------------------
        held = pause["held_interval"]
        if not isinstance(held, dict):
            raise AssertionError(f"`held_interval` must be an object, got {held!r}")
        duration = held.get("duration_ms")
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise AssertionError(
                f"`held_interval.duration_ms` is {duration!r}; a pause held for no measurable time "
                "cannot show that side effects ceased"
            )
        for key in ("new_admissions", "fixture_writes", "fixture_service_calls", "task_output_bytes"):
            value = held.get(key)
            if value != 0:
                raise AssertionError(
                    f"`held_interval.{key}` is {value!r}, expected 0. This is the whole claim of a "
                    "pause: while the operator is told the run is paused, it must not admit tools, "
                    "write files, call services or produce output. A nonzero value here means "
                    "`paused` was displayed over a run that was still acting"
                )
        if held.get("observed_by") != "fixture":
            raise AssertionError(
                f"`held_interval.observed_by` is {held.get('observed_by')!r}, expected 'fixture': the "
                "counters must come from instrumentation outside the agent, since a paused agent "
                "reporting its own inactivity is the claim under test rather than evidence for it"
            )

        # --- long, delegated and background tool activity ---------------------
        coverage = pause.get("tool_coverage")
        if not isinstance(coverage, dict):
            raise AssertionError(f"`tool_coverage` must be an object, got {coverage!r}")
        for kind in ("long_running_bash", "delegated_task", "background_task"):
            if coverage.get(kind) is not True:
                raise AssertionError(
                    f"`tool_coverage.{kind}` is not True. A barrier that only ever held a fast "
                    "foreground tool has not met AC-P1: the hard cases are the tool that outlives "
                    "the settle wait and the work that continues behind a completed parent"
                )

        # --- confirmation requires an observed zero --------------------------
        confirmed = pause["confirmed"]
        if not isinstance(confirmed, dict):
            raise AssertionError(f"`confirmed` must be an object, got {confirmed!r}")
        if confirmed.get("state") != "paused":
            raise AssertionError(
                f"the confirmed state is {confirmed.get('state')!r}, expected 'paused'"
            )
        if confirmed.get("active_tool_count") != 0:
            raise AssertionError(
                f"the run reported `paused` with active_tool_count "
                f"{confirmed.get('active_tool_count')!r}. `paused` with unsettled work is precisely "
                "the false claim this story forbids"
            )

        # --- the honest-degradation half -------------------------------------
        # Both failure modes must have been exercised, and neither may have
        # produced `paused`. An implementation that can only succeed has not been
        # shown to fail safely.
        degraded = pause.get("degraded")
        if not isinstance(degraded, dict):
            raise AssertionError(f"`degraded` must be an object, got {degraded!r}")
        for case in ("untracked_activity", "hook_timeout"):
            outcome = degraded.get(case)
            if not isinstance(outcome, dict):
                raise AssertionError(f"`degraded.{case}` must be an object, got {outcome!r}")
            state = outcome.get("state")
            if state not in {"pause_requested", "running"}:
                raise AssertionError(
                    f"`degraded.{case}` reported state {state!r}. Untracked activity and a timed-out "
                    "hook must degrade to requested/unavailable with a reason — never to `paused`, "
                    "which would tell an operator the run was contained when it was not"
                )
            if not str(outcome.get("reason") or "").strip():
                raise AssertionError(
                    f"`degraded.{case}` carries no reason; an operator told only that the pause did "
                    "not take cannot tell whether to retry or to abort"
                )

        # --- and the live surface agrees with the artifact --------------------
        # The artifact describes the experiment; this confirms the deployment the
        # evaluation is about reports the same capability. Without it a green
        # experiment could sit beside a build that never shipped the barrier.
        run_id = self._require("live_run_id")
        for adapter, paths in ADAPTERS.items():
            observation = self.probe.request(
                "GET", paths["state"].format(run_id=run_id), role="owner", token=self._token("owner")
            )
            if observation.status != 200:
                raise AssertionError(
                    f"{adapter}: the state read returned {observation.status}, expected 200"
                )
            body = self._body_of(observation)
            capabilities = body.get("capabilities")
            if not isinstance(capabilities, dict):
                raise AssertionError(f"{adapter}: capabilities missing from the state read: {body!r}")
            if capabilities.get("pause") is not True:
                raise AssertionError(
                    f"{adapter}: the deployment reports pause capability "
                    f"{capabilities.get('pause')!r} while the recorded experiment proves a working "
                    "barrier. A capability an operator cannot use is not an accepted pause, and a "
                    "passing artifact beside a build that disables the verb is the mismatch §7 exists "
                    "to catch"
                )
            if "active_tool_count" not in body:
                raise AssertionError(
                    f"{adapter}: the state read omits `active_tool_count`; the gateway must report "
                    "runtime truth from the barrier rather than let a reader infer it from "
                    "invocation status"
                )

    # ---- W2-04 (S2 / #3961) --------------------------------------------

    def check_w2_04(self) -> None:
        """Resume continues the same execution, exactly once (AC-P2).

        "Same execution" is the property that separates a pause from a restart. An
        implementation that interrupts the turn and starts a new one with the
        history replayed can look identical in a status field and in a transcript
        summary, so this check demands the identity evidence — same session, same
        attempt, prior history intact, no replayed prompt — and treats an interrupt
        call as a failure rather than as an implementation detail.
        """
        resume = self._artifact("pause_resume")

        if resume.get("released_count") != 1:
            raise AssertionError(
                f"the pause was released {resume.get('released_count')!r} times, expected exactly 1. "
                "A double release admits held work twice and makes 'resumed' unreliable as a record"
            )
        if resume.get("session_id_before") != resume.get("session_id_after"):
            raise AssertionError(
                f"the session changed across the pause: {resume.get('session_id_before')!r} → "
                f"{resume.get('session_id_after')!r}. A new session is a restart, not a resume, and "
                "loses the run's accumulated context"
            )
        if resume.get("attempt_id_before") != resume.get("attempt_id_after"):
            raise AssertionError(
                f"the attempt changed across the pause: {resume.get('attempt_id_before')!r} → "
                f"{resume.get('attempt_id_after')!r}; interrupt-and-new-turn is explicitly not a "
                "successful same-execution pause/resume"
            )
        if resume.get("interrupt_called") is not False:
            raise AssertionError(
                "an interrupt was called: the contract keeps `Query.interrupt` out of the adapter, "
                "because an interrupted turn cannot then be continued as the same execution"
            )
        if resume.get("initial_prompt_replayed") is not False:
            raise AssertionError(
                "the initial prompt was replayed. A replayed prompt means the model is starting the "
                "task again rather than continuing it, which duplicates every side effect it already "
                "performed"
            )
        if resume.get("prior_history_preserved") is not True:
            raise AssertionError(
                "prior history was not preserved across the pause; a resumed run that has forgotten "
                "its work will redo or contradict it"
            )
        if resume.get("task_completed") is not True:
            raise AssertionError(
                "the fixture task did not complete after resuming. A pause that leaves the run unable "
                "to finish has converted a pause into an abort"
            )
        held = resume.get("held_tools_admitted_after_resume")
        if not isinstance(held, int) or held <= 0:
            raise AssertionError(
                f"`held_tools_admitted_after_resume` is {held!r}; the tools parked at the barrier must "
                "actually run once the operator lets them, or pause silently dropped the model's work"
            )

        # Races, which is where "exactly once" is usually lost.
        races = resume.get("races")
        if not isinstance(races, dict):
            raise AssertionError(f"`races` must be an object, got {races!r}")
        for case in ("resume_before_pause", "repeated_resume"):
            outcome = races.get(case)
            if not isinstance(outcome, dict):
                raise AssertionError(f"`races.{case}` must be an object, got {outcome!r}")
            if outcome.get("serialized") is not True:
                raise AssertionError(
                    f"`races.{case}` was not serialized: two transitions each observing the "
                    "pre-state is how a pause gets released twice or confirmed after it was cancelled"
                )
            if outcome.get("errored") is not False:
                raise AssertionError(
                    f"`races.{case}` errored. An operator double-clicking resume, or a resume racing "
                    "ahead of its pause, is ordinary and must not fail the run"
                )

    # ---- W2-05 (S2 / #3961) --------------------------------------------

    def check_w2_05(self) -> None:
        """Expiry, visibility and the deadline clamp (AC-P3, AC-P5, AC-P6).

        Three properties that share one theme: a pause must be bounded, and the
        boundary must be visible. An unbounded pause silently consumes a pod's
        remaining life and ends as an expiry the operator never sees; a pause
        indistinguishable from a stall gets killed by a watchdog that was trying to
        help.
        """
        expiry = self._artifact("pause_expiry")

        # --- auto-resume on expiry -------------------------------------------
        if expiry.get("auto_resumed") is not True:
            raise AssertionError(
                "the shortened fixture timeout did not auto-resume; an unbounded pause outlives the "
                "pod and the operator learns about it when the run vanishes"
            )
        if expiry.get("annotation_count") != 1:
            raise AssertionError(
                f"expiry produced {expiry.get('annotation_count')!r} annotations, expected exactly 1. "
                "The model has to be told the run continued by itself, once — zero leaves it acting on "
                "a stale belief, more than one is noise in the transcript"
            )
        if expiry.get("extra_assistant_turn") is not False:
            raise AssertionError(
                "expiry produced an extra assistant turn: the annotation must ride the existing turn "
                "rather than provoke a new one, which would cost a model call and confuse the "
                "transcript"
            )
        if expiry.get("neutral_annotation") is not True:
            raise AssertionError(
                "the expiry annotation was not published as a neutral runtime fact. Only the Claude "
                "adapter may translate it into `shouldQuery:false`; a provider-shaped event in the "
                "shared path is the leak the neutral contract forbids"
            )
        # An expiry that never reported the pause it ended is the defect found in
        # review of this story: "pausing…" for the whole budget, then a silent resume.
        if expiry.get("resolved_before_release") is not True:
            raise AssertionError(
                "the pause was released without first reporting a confirmation or a failure. An "
                "operator who pressed Pause, waited out the budget and was never told the pause did "
                "not take has been shown a state that never resolved"
            )

        # --- nothing mistook a pause for a stall ------------------------------
        for key, consequence in (
            ("pod_killed", "the pod was killed during a valid pause"),
            ("idle_retry_fired", "the idle-retry watchdog fired during a valid pause"),
            ("exit_watchdog_fired", "the post-completion exit watchdog fired during a valid pause"),
        ):
            if expiry.get(key) is not False:
                raise AssertionError(
                    f"{consequence}: a run that is quiet *because an operator paused it* must not be "
                    "treated as stalled, or pausing a run becomes a way to lose it"
                )
        heartbeats = expiry.get("heartbeats_during_pause")
        if not isinstance(heartbeats, int) or heartbeats <= 0:
            raise AssertionError(
                f"`heartbeats_during_pause` is {heartbeats!r}; visibility output must continue so a "
                "paused run stays distinguishable from a dead one"
            )
        if expiry.get("paused_distinguishable_from_stalled") is not True:
            raise AssertionError(
                "the heartbeat did not distinguish `paused` from `stalled`. Going silent is the one "
                "thing a pause must not do, because silence is what a hung run looks like"
            )
        if expiry.get("spill_output_preserved") is not True:
            raise AssertionError(
                "spill output was not preserved across the pause; the barrier composes with the spill "
                "hooks, so a lost tool-output locator is a regression the pause caused"
            )

        # --- the clamp, and refusing a pause there is no room for -------------
        clamp = expiry.get("deadline_clamp")
        if not isinstance(clamp, dict):
            raise AssertionError(f"`deadline_clamp` must be an object, got {clamp!r}")
        granted = clamp.get("granted_ms")
        remaining = clamp.get("remaining_ms")
        margin = clamp.get("finalization_margin_ms")
        for name, value in (("granted_ms", granted), ("remaining_ms", remaining),
                            ("finalization_margin_ms", margin)):
            if not isinstance(value, (int, float)):
                raise AssertionError(f"`deadline_clamp.{name}` is {value!r}, expected a number")
        if granted > remaining - margin:
            raise AssertionError(
                f"the granted pause of {granted!r}ms exceeds the remaining deadline {remaining!r}ms "
                f"less the finalization margin {margin!r}ms. A pause that consumes the whole deadline "
                "leaves no room to write a terminal state, so the run ends indistinguishably from a "
                "pod that vanished"
            )
        if clamp.get("nonpositive_budget_rejected") is not True:
            raise AssertionError(
                "a nonpositive safe budget was not rejected: a pause that expires the instant it "
                "begins looks to an operator exactly like a pause that never happened"
            )

        # --- a hook held past its own bound ----------------------------------
        # The barrier parks a tool inside a PreToolUse hook, and the CLI enforces that
        # hook's timeout on its side. If the bound is shorter than the pause budget
        # then every long pause has its parked tool aborted out from under it — so the
        # case has to be exercised deliberately, and its outcome must be an honest
        # degradation rather than a pause that appears to hold.
        hook = expiry.get("held_hook_timeout")
        if not isinstance(hook, dict):
            raise AssertionError(f"`held_hook_timeout` must be an object, got {hook!r}")
        if hook.get("exercised") is not True:
            raise AssertionError(
                "the held-hook timeout was never exercised. It is the one bound the adapter does not "
                "enforce itself, so leaving it untested means the pause budget and the hook budget "
                "could disagree in production with nothing to catch it"
            )
        if hook.get("state") == "paused":
            raise AssertionError(
                "a pause whose hook timed out still reported `paused`. The parked tool was released by "
                "the CLI, so admission is no longer closed and the operator is being shown containment "
                "that has already lapsed"
            )
        if not str(hook.get("reason") or "").strip():
            raise AssertionError(
                "the hook timeout produced no reason; 'pause did not take' without a cause leaves an "
                "operator with nothing to act on"
            )
        bound = hook.get("hook_timeout_seconds")
        budget = hook.get("pause_budget_seconds")
        if not isinstance(bound, (int, float)) or not isinstance(budget, (int, float)):
            raise AssertionError(
                f"`held_hook_timeout` must record both bounds as numbers, got "
                f"hook_timeout_seconds={bound!r} pause_budget_seconds={budget!r}"
            )
        if bound <= budget:
            raise AssertionError(
                f"the hook bound of {bound!r}s does not exceed the pause budget of {budget!r}s. The "
                "hook has to outlive the pause it is holding, or the budget is decorative and every "
                "pause held to its limit ends as an aborted tool"
            )

        # --- cancellation must not flush the work it cancelled -----------------
        cancel = expiry.get("cancellation")
        if not isinstance(cancel, dict):
            raise AssertionError(f"`cancellation` must be an object, got {cancel!r}")
        if cancel.get("held_work_admitted") is not False:
            raise AssertionError(
                "abort admitted work that was held at the barrier. An abort that flushes its parked "
                "tools on the way out runs exactly the side effects the operator aborted to prevent"
            )
        if cancel.get("annotation_emitted") is not False:
            raise AssertionError(
                "cancellation emitted a resume annotation; an aborted run is not a resumed one and "
                "must not tell the model to carry on"
            )
        if cancel.get("held_work_denied") is not True:
            raise AssertionError(
                "held work was neither admitted nor denied on cancellation, so those tool calls were "
                "left unresolved and the aborting run cannot finish cleanly"
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

# S3 provides W2-02, S2 provides W2-03..05 and S5 provides W2-06..09. W2-01 and
# W2-10 retain named NOT RUN results until their implementation and live evidence
# land — see PENDING_CHECK_OWNERS.
WAVE2_PREDICATES: dict[str, str] = {
    "W2-02": "check_w2_02",
    "W2-03": "check_w2_03",
    "W2-04": "check_w2_04",
    "W2-05": "check_w2_05",
    "W2-06": "check_w2_06",
    "W2-07": "check_w2_07",
    "W2-08": "check_w2_08",
    "W2-09": "check_w2_09",
}

CHECK_PREDICATES: dict[str, str] = {**WAVE1_PREDICATES, **WAVE2_PREDICATES}


def run_checks(driver: Driver, specs: tuple[CheckSpec, ...] = WAVE1_CHECKS) -> list[CheckResult]:
    """Execute every predicate, converting outcomes into check results.

    One check's failure never stops the others: a partial report with nine real
    answers and one named failure is far more useful to the operator who has to
    fix it than an abort at the first problem.

    A spec with no predicate is ``not_run`` naming its owning story. That is the
    honest answer for a wave under construction, and it keeps the run nonzero —
    the alternative, dropping the check, would make an incomplete wave produce a
    report that passes its own gate.
    """
    results: list[CheckResult] = []
    for spec in specs:
        method_name = CHECK_PREDICATES.get(spec.check_id)
        if method_name is None:
            owner = PENDING_CHECK_OWNERS.get(spec.check_id)
            if owner is None:
                # In the manifest, not implemented, and nobody named. That is a
                # harness bug rather than a wave in progress, so it is a FAILURE:
                # an unowned not_run is how a check quietly stops being anyone's
                # job.
                message = (
                    f"{spec.check_id} is in this wave's manifest but has no predicate and no owning "
                    "story recorded in PENDING_CHECK_OWNERS; the harness cannot say who delivers it"
                )
                logger.error("%s FAILED — %s", spec.check_id, message)
                results.append(
                    CheckResult(
                        check_id=spec.check_id,
                        status=STATUS_FAILED,
                        description=spec.description,
                        acceptance_ids=spec.acceptance_ids,
                        message=message,
                    )
                )
                continue
            message = f"not implemented in this revision; delivered by {owner}"
            logger.warning("%s NOT RUN — %s", spec.check_id, message)
            results.append(
                CheckResult(
                    check_id=spec.check_id,
                    status=STATUS_NOT_RUN,
                    description=spec.description,
                    acceptance_ids=spec.acceptance_ids,
                    message=message,
                )
            )
            continue
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
        # Which evaluation issue reads this report, per wave. Not one constant:
        # #3967 accepted wave 1 and is closed, so a wave-2 report labelled 3967
        # would attach evidence to a finished evaluation. Unknown waves keep the
        # wave-1 label only because they cannot be reached — main() refuses a wave
        # with no manifest before any report is built.
        "evaluation": WAVE_EVALUATIONS.get(wave, "3967"),
        "revision": WAVE_REVISIONS.get(wave, "revival-2026-09-12"),
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
        help=(
            f"Which wave's checks to run. This revision carries {list(SUPPORTED_WAVES)}. "
            "Checks whose owning story has not landed report not_run, so an incomplete wave "
            "exits nonzero rather than passing short."
        ),
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
            "wave %s has no checks in this revision. This harness carries wave %s; the remaining waves "
            "are extended by their owning stories (revival-design §7). Refusing rather than emitting an "
            "empty pass.",
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
