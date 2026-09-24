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
import ast
import copy
import hashlib
import json
import math
import logging
import os
import re
import subprocess  # noqa: S404 - invokes only the operator's declared teardown command
import sys
from collections.abc import Sequence
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

# The checkout this script lives in. W2-01 reads the commit graph from here to compute
# revision containment itself instead of accepting the operator's `is_ancestor` claim;
# this file sits at `platform/scripts/`, so the repository root is two levels up.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

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

# Wave 3's two numeric bounds, named because both are easy to get subtly wrong.
#
# The marker bound is measured from the recorded SDK HANDOFF, never from
# submission. #3969 is explicit about that and the distinction is load-bearing: a
# steer submitted during a long tool call is legitimately pending for minutes, so
# measuring from submission would fail a correct run for being patient. What the
# bound actually constrains is the gap between "the SDK accepted the input" and
# "the operator can see that it did" — an honest acknowledgement arriving late is
# still a defect, because the operator is left unable to distinguish delivery from
# a dropped command.
#
# The queue cap mirrors `DEFAULT_MAX_PENDING` in
# `modules/agent-factory/agent/src/control-state.ts`. Mirrored rather than
# imported, as with the SDK version above; a test pins the pair.
STEER_MARKER_MAX_LATENCY_SECONDS = 35
STEER_QUEUE_CAP = 10

# The stories whose source must be merged and deployed before wave 2's evidence
# means anything, keyed by the story number the evaluation names. W2-01 requires a
# recorded revision for each: wave 2's checks span all three, so evidence gathered
# while one of them is only partly deployed describes a build no reviewer approved.
#
# Keyed by story rather than a flat list so the failure message can say WHICH
# story's revision is missing — the three have different owners.
WAVE2_REQUIRED_STORIES: dict[str, str] = {
    "S3": "3962 — harness-neutral adapter contract (W2-02)",
    "S2": "3961 — proven pause/resume (W2-03..W2-05)",
    "S5": "3964 — aborted vocabulary and counters (W2-06..W2-09)",
}

# A git revision as recorded in provenance: a full 40-character SHA. Short SHAs
# and branch names are refused, because "merged at main" is not a revision — it
# names whatever main happened to be, which is the ambiguity this field exists to
# remove.
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

# A content digest as recorded for a built image or source tree.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# The packages whose versions wave 2's behaviour depends on, beyond the adapter SDK
# already pinned above. Both are recorded in the preflight because the control path
# is split across two runtimes: a worker built against a different agent SDK, or a
# gateway serving a different control-schema package, is a different contract than
# the one under review.
WAVE2_REQUIRED_PACKAGES: tuple[str, ...] = (
    "@anthropic-ai/claude-agent-sdk",
    "control-runtime",
)

# The gates that must be green, BY THE NAME CI actually defines, on the revision
# actually deployed. Mirrored from `.github/workflows/agent-control-ci.yml`'s three
# job names (a test pins them against that file, so a rename there surfaces as a
# harness test to update rather than as an evaluation that accepts a gate nobody
# ran).
#
# Why by name: an earlier revision of this check accepted any nonempty job map whose
# every value was "passed", so `{"anything": "passed"}` demonstrated a green build.
# That is a check on the operator's spelling, not on the build. Requiring these
# exact names means a missing gate is a NAMED failure, and a renamed one cannot
# silently drop out of the required set.
WAVE2_REQUIRED_CI_GATES: tuple[str, ...] = (
    "Agent control tests",
    "Worker control tests",
    "Control evaluation harness tests",
)

# The two components the control path is split across. Each ships as its own image
# from its own workflow, so each has its own deployed revision and digest — and a
# gateway speaking the new contract in front of a worker that does not is the
# half-deployment no single-component check can see.
#
# "Deployed revision" here means the revision the RUNNING build was produced from,
# which is not the same thing as the commit where a story merged: a correct
# deployment is normally newer, carrying that story plus later changes. Conflating
# the two made this check reject correct deployments and implicitly demand that an
# operator redeploy an old merge commit to satisfy the evaluator. Containment
# (is each required story actually IN what is deployed?) is asserted separately,
# against `contained_in`.
WAVE2_DEPLOYED_COMPONENTS: tuple[str, ...] = ("worker", "gateway")

# Wave 4's deployed surface is wave 2's plus the SPA. The frontend is a third
# separately-deployed artifact, and it is the one the browser checks actually
# exercise: a stale bundle in front of a current gateway is the normal
# half-deployment, and it is invisible to any check that reads only the two
# container images. It is a static asset rather than an image, so it carries a
# revision and an asset digest instead of an `image_digest` — see
# `WAVE4_FRONTEND_KEYS`.
WAVE4_DEPLOYED_COMPONENTS: tuple[str, ...] = WAVE2_DEPLOYED_COMPONENTS

# Wave 4's own frontend deployed identity. Separate from DEPLOYED_COMPONENT_KEYS
# because an SPA bundle has no registry digest: what identifies it is the revision
# it was built from and the digest of the asset actually being SERVED, which is what
# the browser capture independently reports as `bundle_revision`. Requiring both
# means the preflight and the capture have to agree about which bundle was under
# test, and a disagreement names a half-deployment rather than passing quietly.
WAVE4_FRONTEND_KEYS: tuple[str, ...] = (
    "revision",
    "asset_digest",
    "served_asset_evidence",
)

# The dashboard story's own gates, by the name CI defines — read off
# `.github/workflows/gateway-ci.yml`'s `jobs.*.name`, and pinned against that file by
# a test so a rename there becomes a harness test to update rather than an evaluation
# that requires a gate nobody runs.
#
# Wave 4's row asks for "current green Vitest/typecheck/build". That is three
# activities across TWO jobs, which is worth stating rather than smoothing over:
# `Frontend Unit Tests` runs `npx vitest run` and then `npx tsc --noEmit` in the same
# job, so Vitest and typecheck share a gate and cannot be reported separately. Naming
# a third "Frontend typecheck" gate would have been an invented name — unsatisfiable
# by any real run, which is the "requirement no operator could fix" failure mode, and
# strictly worse than a gate that is honestly coarser than the row's phrasing.
#
# `Build Container` is the build half. It smoke-builds the gateway image through
# CodeBuild, which is what "build" means on this deployment path.
WAVE4_REQUIRED_CI_GATES: tuple[str, ...] = (
    "Frontend Unit Tests",
    "Build Container",
)

# The control-path gates are deliberately NOT repeated in W4-01.
#
# W2-01 already validates them at full strength — archived run document parsed at its
# field locations, plus the job's own uploaded checkout artifact bound to the run,
# attempt and trigger. W4-01 requires wave 2 to be ACCEPTED, which means W2-01 passed,
# which means those gates were validated to that standard on a revision contained in
# what is deployed.
#
# Re-checking them here would put two implementations of one claim in one report, free
# to disagree — and the second implementation would necessarily be the weaker one,
# because it would be written to a schema chosen for the frontend gates. The first
# draft of this check did exactly that, and the weaker copy would have been the one an
# operator could satisfy. Prior-wave acceptance is the stronger link, so it is the one
# used.
#
# Why the frontend gates cannot reach that standard: `gateway-ci.yml` publishes no
# `checked-out-revision-*` artifact, so there is nothing to bind a checkout to. That
# is a gap in the workflow rather than in this check, and closing it means adding the
# archive step to `gateway-ci.yml` — another story's file. Until then W4-01 validates
# frontend gates against the archived RUN document (which does carry the conclusion,
# the named job and the head revision) and says so, rather than requiring an artifact
# that does not exist or pretending the binding is as tight as wave 2's.
WAVE4_FRONTEND_GATE_RAW_DOCUMENTS: tuple[str, ...] = ("run",)

# Wave 4's own required stories. S7 is the dashboard this wave evaluates; the
# consolidating checks additionally need the stories whose criteria they repeat, and
# those are named per-check in WAVE4_CONSOLIDATED_SOURCES rather than here, because
# W4-01 must be able to fail on "the dashboard is not deployed" distinctly from
# "wave 3's abort evidence is stale".
WAVE4_REQUIRED_STORIES: dict[str, str] = {
    "S7": "3966 — dashboard live run controls (W4-02, W4-04, W4-07, W4-08)",
}

# The waves whose acceptance wave 4 consolidates, and the evaluation that accepted
# each. Wave 4 is the LAST evaluation: its row says "all four evaluations may close
# only with this evidence", so every earlier wave has to be accepted before this one
# can be complete.
#
# Wave 3 is in this tuple even though this revision may carry no wave-3 manifest.
# That is the point rather than an oversight: if wave 3 is unregistered, wave 4
# cannot be complete, and the honest way to say so is a named refusal from the check
# that depends on it — not a quietly shorter prerequisite list.
WAVE4_PRIOR_WAVES: tuple[int, ...] = (1, 2, 3)

# Keys a prior wave's acceptance record must carry. Each is a separate way for
# "wave N was accepted" to be false while looking true:
#
#   evaluation  — accepted on the right issue, not attached to a different one
#   run_id      — the run whose report a reviewer can retrieve and re-read
#   revision    — the exact 40-char commit it was accepted on
#   passed/required — the counts, which distinguish a full acceptance from a partial
#   cleanup_ok  — an accepted wave whose fixture was left enabled is the DP-INV-1
#                 state, so its environment is not a usable baseline
#
# Deliberately NOT a key: `compatible`. Compatibility is the conclusion these
# checks exist to reach, so reading it from the record would make the check restate
# its own subject. It is computed from the commit graph — see
# `_assert_prior_wave_accepted`.
PRIOR_WAVE_ACCEPTANCE_KEYS: tuple[str, ...] = (
    "evaluation",
    "run_id",
    "revision",
    "passed",
    "required",
    "cleanup_ok",
)

# What each consolidating wave-4 check repeats, and from where. Keyed by check ID so
# a not_run or a failure can name the owner AND the prior evidence it needed, which
# are different things to go and fix.
#
# `wave` is which earlier wave last evidenced these criteria; `surfaces` are the
# source paths whose modification invalidates that evidence. The second is what
# makes "rerun any stale/touched criterion" checkable rather than advisory: if a
# surface changed after the evidence was taken, the evidence describes code that is
# no longer running, and the criterion has to be rerun.
WAVE4_CONSOLIDATED_SOURCES: dict[str, dict] = {
    "W4-03": {
        "wave": 3,
        "artifact": "wave4_steering_evidence",
        "surfaces": (
            "modules/agent-factory/agent/src/control-state.ts",
            "modules/agent-factory/agent/src/control-revalidation.ts",
            "modules/gateway/src/activity/control_service.py",
            "modules/gateway/frontend/src/services/agentControl.ts",
        ),
    },
    "W4-05": {
        "wave": 3,
        "artifact": "wave4_abort_evidence",
        "surfaces": (
            "modules/agent-factory/agent/src/control-state.ts",
            "modules/gateway/src/activity/control_service.py",
            "modules/gateway/src/activity/stats_service.py",
            "modules/gateway/frontend/src/components/InvocationChain.tsx",
        ),
    },
    "W4-06": {
        "wave": 3,
        "artifact": "wave4_security_matrix",
        "surfaces": (
            "modules/gateway/src/activity/routes.py",
            "modules/gateway/src/activity/control_service.py",
            "modules/gateway/frontend/src/services/agentControl.ts",
        ),
    },
    "W4-09": {
        "wave": 2,
        "artifact": "wave4_runtime_comparison",
        "surfaces": (
            "modules/agent-factory/agent/src/control-state.ts",
            "modules/gateway/src/activity/stats_schemas.py",
            "modules/gateway/src/activity/control_schemas.py",
        ),
    },
}

# Per-component deployed-identity keys. `source_revision` is what the image was
# BUILT FROM, which is what ties a running digest back to reviewed source; without
# it a digest pair only establishes that two recorded strings match each other.
#
# `build_record` is what makes even that pair mean something. Root's review named the
# defect precisely: `source_revision == revision` compares two fields the same hand
# wrote, so any invented pair of matching valid-looking SHAs passed. The link has to
# come from the system that PERFORMED the build — the workflow run and the registry
# read — so the entry carries a retrievable identity for each and the archived raw
# response they produced. See `BUILD_RECORD_KEYS`.
DEPLOYED_COMPONENT_KEYS: tuple[str, ...] = (
    "revision",
    "image_digest",
    "source_revision",
    "build_record",
)

# What the archived build provenance for ONE component must record.
#
# The shape follows the build path this platform actually deploys through, which is
# CodeBuild: `platform/scripts/codebuild-run.sh` uploads `git archive <sha>` to
# `codebuild/src/<sha>-<unique>.zip`, starts the project with
# --source-location-override and an `ADP_SOURCE_SHA` override, and the buildspec
# (e.g. `codebuild/bs-agent-runtime.yml`) does `docker push $REGISTRY/$ECR_REPO:$IMAGE_TAG`.
# An earlier revision of this schema modelled a GitHub Actions build instead, which
# root's review rejected: a fixture describing a build path we do not deploy through
# cannot establish anything about a running image, and there is no reason to invent an
# attestation format when the deployed path already emits every fact.
#
#   * `project` / `build_id` / `build_url`  — which build, retrievably. `build_id` is
#     what `aws codebuild batch-get-builds --ids` takes; `build_url` is what a
#     reviewer opens.
#   * `built_revision`   — the revision the build consumed, as the BUILD reported it
#     (`ADP_SOURCE_SHA`), corroborated against the source archive key it actually
#     built, not as the operator retyped it.
#   * `image_tag`        — the tag the build pushed (`IMAGE_TAG`), which is the only
#     thing binding a build to a registry entry.
#   * `built_digest`     — the digest the build's own `docker push` output reported
#     for that tag. Read out of the build log, because a digest the operator typed is
#     a digest nobody published.
#   * `repository` / `registry_digest` — the ECR repository read, and the digest it
#     reports for that tag: the image the cluster is actually pulling.
#   * `raw` — the archived responses themselves (`build`, `build_log`, `registry`),
#     each with the command that produced it, so every link above is checked against
#     the bytes rather than against a summary of them. `RAW_METADATA_KEYS` names their
#     shape.
#
# None of these are new facts the operator has to invent: `aws codebuild
# batch-get-builds`, `aws logs get-log-events` and `aws ecr describe-images` emit all
# of them. What changes is that the harness now PARSES those responses at their real
# field locations and requires the build to have succeeded — where it previously asked
# only whether the expected strings appeared somewhere in the archived text, which a
# failed build and an unrelated document that merely mentioned them both satisfied.
BUILD_RECORD_KEYS: tuple[str, ...] = (
    "project",
    "build_id",
    "build_url",
    "built_revision",
    "image_tag",
    "built_digest",
    "repository",
    "registry_digest",
    "raw",
)

# The archived documents a build record must carry, in the order the chain reads them:
# what the build was and whether it succeeded, what digest it pushed for its tag, and
# what the registry serves for that tag today.
BUILD_RECORD_RAW_DOCUMENTS: tuple[str, ...] = ("build", "build_log", "registry")

# What one archived raw document must carry. `command` is how it was obtained (so a
# reviewer can re-run it) and `body` is what came back verbatim. `retrieved_at` dates
# the retrieval, which is what distinguishes evidence collected for this evaluation
# from a document carried forward from an earlier one.
RAW_METADATA_KEYS: tuple[str, ...] = ("command", "retrieved_at", "body")

# What one required CI gate must record. A gate without the revision it TESTED is
# not evidence about this build: it names a green run of unknown subject.
#
# `run_url` and `raw` are the same correction as above, for the same reason: an
# arbitrary truthy `run_id` was accepted, so `run_id: true` demonstrated a gate. The
# gate's own run document has to be archived, and the harness reads the status,
# workflow name, run identity and tested revision OUT of it — the summary fields are
# then a cross-check on the archive rather than the evidence themselves.
CI_GATE_KEYS: tuple[str, ...] = (
    "status",
    "run_id",
    "run_url",
    "tested_revision",
    "raw",
)

# The archived documents a gate must carry, and where each comes from.
#
# `run` is the unmodified `gh run view --json ...` response. `checkout` is the
# per-job artifact `.github/workflows/agent-control-ci.yml` uploads, downloaded with
# `gh run download`. They are kept as two separate documents deliberately: an earlier
# revision had the operator paste a `checked_out_revision` field INTO the run
# response, which root rejected — that made the one fact the manual path exists to
# establish an operator assertion inside a document otherwise written by GitHub, and
# it silently rewrote the authoritative response. The workflow already emits the real
# value, so the harness reads it from the artifact GitHub stored and binds it to the
# run; the run response stays byte-for-byte what the API returned.
CI_GATE_RAW_DOCUMENTS: tuple[str, ...] = ("run", "checkout")

# The fields of that artifact (see the `Verify and record the checked-out revision`
# step). Limited to what the workflow actually writes — this is not a general
# attestation format, it is one JSON file with six known keys.
CHECKOUT_ARTIFACT_KEYS: tuple[str, ...] = (
    "job",
    "checked_out_revision",
    "run_id",
    "run_attempt",
    "event_name",
    "workflow_ref_sha",
)

# Required-check NAME -> the workflow's job id, which is what the artifact's `job`
# field and the `checked-out-revision-<id>` artifact name both carry. Spelled out
# rather than derived by slugifying the display name: the two are independent strings
# in the workflow, and a job renamed on one side only must fail here rather than
# resolve to a plausible-looking artifact that does not exist.
CI_GATE_JOB_IDS: dict[str, str] = {
    "Agent control tests": "agent-control-tests",
    "Worker control tests": "worker-control-tests",
    "Control evaluation harness tests": "control-evaluation-harness-tests",
}

# What one creation-ledger entry must record, and what one teardown observation must
# record about it. `identity` is the resource's own unforgeable identity as observed
# at creation (a Kubernetes UID, a queue URL, an ARN) rather than its name: root's
# fixture review established that a name prefix does not establish ownership,
# because `kubectl apply` can adopt a pre-existing same-name object and
# `create-queue` can return an existing queue. Recording identity at creation is
# what makes "this exact object is gone" checkable, and distinguishes it from "some
# object of this name is gone" — which a recreated resource also satisfies.
LEDGER_ENTRY_KEYS: tuple[str, ...] = ("kind", "name", "identity", "created")
LEDGER_REMOVAL_KEYS: tuple[str, ...] = ("identity", "absent", "observed_by", "removed_at")

# The two ledger kinds whose removal ORDER matters, rather than only their end state.
#
# The fixture creates both the control-enabled workload (the "listener") and the
# NetworkPolicies restricting reach to it. Both must be gone at the end — they are
# ledger resources — but removing the policy FIRST opens a window in which a
# control-enabled pod is still running with its ingress restriction already deleted.
# That interval is strictly worse than either end state and is invisible to a check
# that only looks at what is true afterwards, which is why removal timestamps are
# required and compared rather than just collected.
#
# This is also what makes DP-INV-1 and ledger completeness satisfiable together: the
# fixture's own policies come down (so nothing leaks), the environment's persistent
# baseline isolation stays (so the environment is still isolated), and the ordering
# assertion covers the gap between those two facts.
LISTENER_LEDGER_KINDS: tuple[str, ...] = ("listener", "workload", "pod", "deployment")
POLICY_LEDGER_KINDS: tuple[str, ...] = ("networkpolicy", "policy")

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

# The stats response model W4-09's row calls "the complete current RunStatsResponse".
# Named here rather than inlined because it is the one piece of the parity comparison
# that must be a literal: the FIELDS are parsed from the deployed source so they
# cannot go stale (see `Driver._stats_response_fields`), but which class to parse has
# to be stated somewhere. A rename on the gateway side makes W4-09 report that it
# could not find the model — not that the live response matched nothing.
STATS_RESPONSE_MODEL = "StatsResponse"

# The outcomes a native provider interruption may legitimately normalize to, and the
# provenance the experiment that observed one must carry.
#
# Why an explicit set rather than "anything but aborted": the claim W2-06 makes is
# that a provider's own interrupted turn does NOT by itself become a deliberate ADP
# abort. The evidence for that is an OBSERVED outcome. An earlier revision tested
# only `!= "aborted"`, so `null`, `""`, `{}` and `"invented"` all passed — meaning an
# operator who never ran the experiment, or whose collection script wrote an empty
# field, got a pass. A missing measurement cannot prove a negative, and a value
# outside the writer's vocabulary is not an outcome the deployment could have
# produced: it is a typo or a fabrication, and either way nothing was measured.
#
# Mirrored from the writer's allowlist (W2-08's `writer_allowed_statuses`): these are
# the non-aborted terminal statuses a run that was cut off can legitimately land in.
# `active`/`in_progress` are deliberately absent — the experiment interrupts a turn,
# so its subject has stopped, and a still-running row means the experiment did not
# reach the state it claims to describe.
NATIVE_INTERRUPT_ALLOWED_STATUSES: tuple[str, ...] = (
    "failed",
    "complete",
    "completed",
    "skipped",
    "budget_stopped",
)

# What the native-interruption experiment must record besides its outcome. Without
# these the status is a bare word: `run_id` names the run that was interrupted, and
# `observed_by` records HOW the outcome was read back, which is what makes it an
# observation rather than an expectation.
NATIVE_INTERRUPT_KEYS: tuple[str, ...] = ("status", "run_id", "observed_by")

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

# Transcribed from evaluation #3969's acceptance table (revision
# revival-2026-09-12, with the harness-neutral-2026-09-15 compatibility
# amendment), on the same rule §7 states for waves 1 and 2.
#
# Registered by S6 #3965, which implements the steering half (W3-06..09, W3-11).
# The abort half (W3-02..04) belongs to S4 #3963, W3-01/05/10/12 to the wave's
# gate/regression owner, and all of those are registered here WITHOUT predicates
# on purpose. Two things follow from that, and both are the point:
#
#  - `run_checks` FAILS an unimplemented check that has no owner, so every ID
#    below without a predicate has an entry in PENDING_CHECK_OWNERS naming the
#    story that owes it. A not_run then says who to go to.
#  - a wave-3 run today cannot pass. That is correct and is the reason the whole
#    manifest lands in one edit rather than growing check by check: a manifest
#    trimmed to the checks S6 implements would let five of twelve steering and
#    abort criteria be absent while `--wave 3` exited 0, which is exactly the
#    false pass the comment on WAVE2_CHECKS warns about.
WAVE3_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "W3-01",
        ("Gate/regression",),
        "preflight has wave 2 acceptance, merged S4 then S6, current worker/gateway "
        "digests and green named CI; ordinary flags remain off while disposable "
        "fixtures exercise all four implemented capabilities",
    ),
    CheckSpec(
        "W3-02",
        ("AC-A1", "AC-A2", "AC-A8"),
        "API abort returns pending, then exactly one finalized aborted-by-user "
        "comment, DDB status aborted with completed_at, check conclusion cancelled "
        "and a revoked control record; later transcript/budget classification "
        "cannot overwrite abort",
    ),
    CheckSpec(
        "W3-03",
        ("AC-A4", "AC-A5"),
        "one correlated logical run, SQS message and successful DeleteMessage; pod "
        "exit 0 with no deletion or kill; no new execution of that run through "
        "measured visibility expiry plus margin; a replayed terminal-aborted "
        "envelope starts zero tasks; termination evidence preserved before TTL",
    ),
    CheckSpec(
        "W3-04",
        ("AC-A6", "AC-A7"),
        "abort during confirmed/pending pause, tool completion and retry backoff "
        "cancels held work with no auto-resume note or new query; double and "
        "concurrent abort produce one comment and one successful delete; a failed "
        "acknowledgement never reports a successful abort; versioned sentinel "
        "malformed/stale cases preserve unrelated credential retry behaviour",
    ),
    CheckSpec(
        "W3-05",
        ("AC-S1", "AC-S2", "AC-S3", "AC-S5", "AC-S6", "AC-S7"),
        "the all-verb auth, nonowner/tenant, token expiry, malformed payload, "
        "terminal/stale generation and unsafe-target matrix repeated now that the "
        "verbs execute; unauthorized or malformed commands have zero side effects",
    ),
    CheckSpec(
        "W3-06",
        ("AC-T2", "AC-T4"),
        "a steer submitted during a fixture long tool is 202/pending and hands off "
        "at the next boundary; state and log command IDs match; the live-comment "
        "marker appears no later than 35 seconds AFTER the recorded SDK handoff, "
        "not after submission; no model-comprehension claim is made",
    ),
    CheckSpec(
        "W3-07",
        ("AC-T5", "AC-T8"),
        "with delivery held, ten steers are accepted and the eleventh rejected with "
        "429; release compares exact submission/handoff ID ordering; paused steers "
        "stay pending until resume; abort cancels pending commands; journal "
        "expiry/generation loss becomes unknown with no replay",
    ),
    CheckSpec(
        "W3-08",
        ("AC-S8",),
        "the actual SDK-bound steer text contains the wrapUntrusted delimiters with "
        "trusted actor attribution, human origin and shouldQuery:true; "
        "attacker-supplied actor metadata is rejected and the raw instruction is "
        "never elevated to system text",
    ),
    CheckSpec(
        "W3-09",
        ("AC-T6",),
        "the real SDK input stream consumes the initial task plus at least two later "
        "user messages, and attempt finalization disposes the generator and closes "
        "Query; recorded message and turn counts, not a source grep or mocks alone",
    ),
    CheckSpec(
        "W3-10",
        ("AC-T3",),
        "in the explicitly authorized disposable fixture repo/branch a steer changes "
        "a deterministic target artifact, target tests pass and the resulting test PR "
        "is merged; PR URL, merge SHA and expected file content recorded; no manual "
        "pod access, and operator fixture setup is separate from agent interaction",
    ),
    CheckSpec(
        "W3-11",
        ("AC-T7",),
        "a forced in-process retry with one queued steer delivers that command to the "
        "new Query/input exactly once, never replays a confirmed handoff, preserves "
        "the session on continuation, reports an ambiguous handoff as unknown, and an "
        "abort during retry starts no next attempt",
    ),
    CheckSpec(
        "W3-12",
        ("Gate/regression",),
        "all prior-wave invariant and security regressions pass on current code; "
        "evidence collected and exact rows/workloads/test objects removed; cleanup "
        "failures keep the gate open",
    ),
)

# Wave 4 (#3970): the dashboard's own evidence, plus the consolidation of all 37
# acceptance criteria. Transcribed from the wave-4 evaluation body under revision
# revival-2026-09-12, one spec per `check` row in its table.
#
# Two properties of this manifest are deliberate and worth stating, because both
# make the wave HARDER to pass rather than easier:
#
#  1. It registers all ten checks now, including the six whose evidence this story
#     cannot produce. A wave that only listed what S7 can prove would publish a
#     complete-looking report while the epic's real consolidation checks were
#     absent, which is precisely the "passes its own gate" failure the wave
#     machinery exists to prevent. The unimplemented ones are owned in
#     PENDING_CHECK_OWNERS, so they report not_run naming their owner.
#  2. The browser checks read a captured Playwright run rather than asserting
#     against the frontend source. A DOM claim that is verified by reading the
#     component would pass on a bundle that was never deployed.
WAVE4_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "W4-01",
        ("Gate/regression",),
        "preflight links accepted waves 1-3, S7 merged head, the actual frontend "
        "asset revision and gateway/worker digests, and current green "
        "vitest/typecheck/build plus control CI; the isolated fixture browser "
        "identity is the owner and ordinary users remain gated pending acceptance",
    ),
    CheckSpec(
        "W4-02",
        ("AC-F3",),
        "Playwright visits /activity?id=<invocation_id>; flag off, still loading and "
        "backend error each yield zero control panel nodes and zero command "
        "requests; the positive fixture flag exposes only advertised capabilities to "
        "the authorized owner, and terminal/unavailable/nonowner cannot submit",
    ),
    CheckSpec(
        "W4-03",
        ("AC-T1", "AC-T2", "AC-T3", "AC-T4", "AC-T5", "AC-T6", "AC-T7", "AC-T8", "AC-S8"),
        "the browser submits a mid-run steer with a valid request schema/path "
        "answered 202, and the DOM shows pending tied to command_id then a matching "
        "delivery marker/state; the fixture pivot and merged test-PR assertion from "
        "W3-10 execute, and FIFO/retry/cap/SDK proof is retained at current "
        "compatible revisions and rerun on touched surfaces",
    ),
    CheckSpec(
        "W4-04",
        ("AC-P1", "AC-P2", "AC-P3", "AC-P4", "AC-P5", "AC-P6"),
        "the browser observes running→pause_requested→paused→running from fresh "
        "server state; a long/untracked tool reason stays truthful and the copy says "
        "spend may continue; the timeout and all wave 2 barrier assertions are "
        "exercised, with no frozen detailItem and no fabricated paused state",
    ),
    CheckSpec(
        "W4-05",
        (
            "AC-A1", "AC-A2", "AC-A3", "AC-A4", "AC-A5", "AC-A6",
            "AC-A7", "AC-A8", "AC-A9", "AC-A10", "AC-A11", "AC-A12",
        ),
        "an abort cancel leaves the run untouched while a confirmed abort "
        "transitions pending→terminal showing one finalized comment and the aborted "
        "renderers; repeat paused/double-abort and per-run ack/exit/replay checks "
        "hold; the actual row has completed_at and every wave 2 stats/writer "
        "assertion still passes",
    ),
    CheckSpec(
        "W4-06",
        ("AC-S1", "AC-S2", "AC-S3", "AC-S4", "AC-S5", "AC-S6", "AC-S7"),
        "the deployed security matrix repeats, and Playwright captures ALL browser "
        "request destinations and JSON bodies: controls target gateway activity "
        "routes only, carry no pod address or token, and a spoofed identity is "
        "rejected; a real non-gateway probe is still blocked and the bundle scan is "
        "supplemental only",
    ),
    CheckSpec(
        "W4-07",
        ("Gate/regression",),
        "live GET state and POST command JSON match frontend agentControl.ts and "
        "backend control_schemas.py field for field at each level — state "
        "run_id/generation/available/reason/capabilities/state/active_tool_count/"
        "updated_at/commands, response run_id/action/state/command_id/"
        "command_status, entries command_id/action/status/accepted_at/delivered_at/"
        "reason — with backend-derived fixture keys subsets of the live keys",
    ),
    CheckSpec(
        "W4-08",
        ("Gate/regression",),
        "Playwright clock/network assertions: polling every 2 seconds only while the "
        "modal is open and visible, backing off on errors, stopping on "
        "close/terminal/unavailable and refreshing detail after commands; a "
        "generation change, an expired ack and cancelled/rejected/unknown delivery "
        "render distinctly, and the DOM cannot label an enqueue as delivered",
    ),
    CheckSpec(
        "W4-09",
        ("AC-F1", "AC-F2"),
        "the deterministic flag-off and flag-on/no-command runtime comparison reruns "
        "with the final code and no ordinary flag enablement as a testing shortcut; "
        "the live stats schema/provenance matches the complete current "
        "RunStatsResponse with no invented mock fields",
    ),
    CheckSpec(
        "W4-10",
        ("Gate/regression",),
        "the evidence index contains exactly all 37 acceptance IDs, each with an "
        "owner and passing compatible source/deployment evidence, rerunning any "
        "stale or touched criterion; there is no missing/skipped/not-run result and "
        "exact fixture cleanup is confirmed; all four evaluations may close only "
        "with this evidence",
    ),
)

# S1 delivered wave 1; the wave owners extend the rest (§7: "S2/S5 extend wave 2;
# S4/S6 extend wave 3; S7 extends wave 4"). Asking for a wave with no manifest at
# all is an honest nonzero, not an empty pass.
WAVE_CHECKS: dict[int, tuple[CheckSpec, ...]] = {
    1: WAVE1_CHECKS,
    2: WAVE2_CHECKS,
    3: WAVE3_CHECKS,
    4: WAVE4_CHECKS,
}
SUPPORTED_WAVES: tuple[int, ...] = tuple(sorted(WAVE_CHECKS))

# The evaluation issue that reads each wave's report, and the design revision that
# wave's checks were transcribed from. #3967 accepted wave 1 with 10/10 and is
# closed; #3968 owns wave 2's live acceptance and #3969 wave 3's.
WAVE_EVALUATIONS: dict[int, str] = {1: "3967", 2: "3968", 3: "3969", 4: "3970"}
WAVE_REVISIONS: dict[int, str] = {
    1: "revival-2026-09-12",
    2: "harness-neutral-2026-09-15",
    # #3969 states revision revival-2026-09-12 with the harness-neutral amendment
    # applied on top, rather than superseding it.
    3: "revival-2026-09-12",
    4: "revival-2026-09-12",
}

# Which story owns each check whose predicate is not implemented yet, so a
# not_run says who to go to rather than just "missing". Registering a check with
# no predicate is deliberate — see WAVE2_CHECKS above — but it must never be
# indistinguishable from a check the harness forgot.
#
# Waves 1, 2 and 4 have predicates. Pending Wave 3 checks retain explicit owners.
PENDING_CHECK_OWNERS: dict[str, str] = {
    "W3-01": "the wave-3 gate owner (consolidated preflight, as #5825 did for wave 2)",
    "W3-02": "S4 #3963 (graceful abort: finalization, comment, DDB status)",
    "W3-03": "S4 #3963 (abort lifecycle: SQS delete, pod exit, no redelivery)",
    "W3-04": "S4 #3963 (abort during pause/tool/retry; double and concurrent abort)",
    "W3-05": "the wave-3 gate owner (wave-1 security matrix rerun with verbs live)",
    "W3-10": "the wave-3 gate owner (authorized disposable fixture repo, merged test PR)",
    "W3-12": "the wave-3 gate owner (prior-wave regressions plus verified cleanup)",
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

# Rows that carry no acceptance criterion of their own. "Gate/regression" is the
# evaluation tables' marker for a check that establishes whether the OTHER checks are
# describing the right build — a preflight, a schema gate, a consolidation. It is not
# an acceptance ID and must not be counted as one.
_NON_ACCEPTANCE_ROW_LABELS: frozenset[str] = frozenset({"Gate/regression"})


def all_acceptance_ids() -> tuple[str, ...]:
    """Every real acceptance criterion across every registered wave, sorted.

    Computed from the manifests rather than written down as a list of 37 strings.
    That is the whole reliability argument for W4-10: the wave-4 row requires the
    evidence index to contain "exactly all 37 acceptance IDs", and a hardcoded
    expectation would be satisfiable by editing the constant to match whatever the
    index happened to contain. Derived from the same CheckSpec rows the checks
    themselves are driven by, the set can only change by changing a wave's manifest.

    Sorted numerically within each family, so AC-A2 precedes AC-A10 rather than
    following it. Purely presentational — the comparisons are set-based — but the
    failure messages list these IDs, and a reader scanning for a missing one in a
    lexicographic list reads AC-A10 as the second entry.
    """
    ids = {
        acceptance_id
        for spec in ALL_CHECK_SPECS
        for acceptance_id in spec.acceptance_ids
        if acceptance_id not in _NON_ACCEPTANCE_ROW_LABELS
    }

    def sort_key(value: str) -> tuple[str, int, str]:
        match = re.match(r"^(AC-[A-Z]+)(\d+)$", value)
        if match:
            return (match.group(1), int(match.group(2)), "")
        # Anything unparseable sorts last under its own name rather than crashing:
        # an unexpected row label is W4-10's business to report, not this helper's
        # to raise on.
        return ("zz", 0, value)

    return tuple(sorted(ids, key=sort_key))


# The count wave 4's row names. Asserted rather than assumed: if a wave's manifest
# changes the criterion set, this module fails to import with a message naming the
# discrepancy, instead of W4-10 quietly consolidating a different number than the
# evaluation asked for.
WAVE4_TOTAL_ACCEPTANCE_IDS = 37

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
    # W2-01 / Gate-regression. The consolidated wave-2 preflight. Everything here
    # is either a fact about a build (a digest, a merge, a CI result) or about the
    # fixture's configuration at listener-start time — none of which is observable
    # over HTTP after the fact, and all of which decides whether the other nine
    # checks are describing the thing under review at all.
    #
    # One key per named observation rather than a rolled-up "preflight_ok": a
    # single boolean cannot say whether the gateway digest or the worker digest was
    # the stale one, and those have different owners.
    # `deployed_components` replaces the four flat digest fields an earlier revision
    # carried: each component now records the revision it runs, its image digest and
    # the source revision that image was BUILT FROM, so a running digest is tied back
    # to reviewed source rather than merely equal to another recorded string.
    # `merged_revisions` records containment against those deployed revisions instead
    # of demanding the deployment BE a story's merge commit.
    "wave2_preflight": (
        "wave1_evidence",
        "merged_revisions",
        "protocol_version",
        "adapter_id",
        "sdk_version",
        "package_versions",
        "deployed_components",
        "ci_gates",
        "isolation_before_listener",
        "ordinary_flags_off",
        "fixture_only_flag_scope",
        "fixture_identity",
        "creation_ledger",
    ),
    # W2-10 / Gate-regression, the PRE-teardown half. Captured while the fixture
    # still exists, because a torn-down fixture cannot answer — requiring it to was
    # the defect that made a correct teardown produce NOT RUN. The harness makes its
    # own live capability reads at this point too; this artifact is the in-cluster
    # part it cannot see over HTTP.
    #
    # `observed_revisions` binds these observations to the deployed build: a security
    # observation that cannot be tied to what was running is a true statement about
    # an unknown subject.
    "security_capture": (
        "captured_before_teardown",
        "observed_revisions",
        "fixture_identity",
        "isolation_present",
        "wave1_security",
        "unsupported_verbs",
        "unsupported_adapter_capabilities",
        "general_flag_enablement",
        "ordinary_flags_off",
    ),
    # W2-10 / Gate-regression, the POST-teardown half. Only what teardown is supposed
    # to have ACHIEVED: absence. The ROW deletions are not here — those are the
    # harness's own first-hand record (`CleanupOutcome`), because an
    # operator-supplied "cleanup succeeded: true" is exactly the substitute for
    # observation this check exists to refuse.
    #
    # `removals` is reconciled against the preflight's `creation_ledger` rather than
    # being a caller-chosen map of names: a map whose keys the operator picks can
    # only confirm the resources it mentions, so omitting a leaked workload passed.
    # `captured_at` is what places these observations AFTER the teardown the harness
    # itself invoked. Without it the artifact's own `verified_after_teardown: true` is
    # the only ordering evidence, and that is the claim it cannot be trusted on: a file
    # written before the resources were removed says exactly the same thing.
    "teardown_verification": (
        "verified_after_teardown",
        "captured_at",
        "fixture_identity",
        "removals",
        "baseline_isolation_present",
        "general_flag_enablement",
        "ordinary_flags_off",
    ),
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
    # Wave 3 / S6 (#3965). Live steering. Everything here is a fact about a
    # *moment inside a run* — whether a parked reader existed, when a handoff
    # physically happened, what bytes the SDK received — and none of it is legible
    # to an HTTP reader after the fact. The harness makes the outside half itself
    # (the 202, the journal projection, the 429); these are the inside half.
    #
    # `handoff_at` is the load-bearing field and the reason this artifact exists
    # rather than being derived from the state endpoint. #3969 requires the marker
    # within 35 seconds of the *recorded SDK handoff*, not of submission — a
    # distinction that is meaningless without a separately recorded handoff
    # timestamp, and one that quietly converts into "35 seconds after submission"
    # if the harness has only `accepted_at` to work from. During a long tool call
    # those two are minutes apart, so the substitution would fail a correct run.
    "steering_delivery": (
        "command_id",
        "accepted_at",
        "handoff_at",
        "marker_at",
        "state_command_ids",
        "log_command_ids",
        "tool_active_at_submission",
        "status_at_submission",
        "delivered_at_matches_handoff",
        "model_comprehension_claimed",
    ),
    # W3-07 / AC-T5, AC-T8. Ordering and the bound, plus the three terminal
    # outcomes that must not be replays. `submission_order`/`handoff_order` are
    # recorded as full ID sequences rather than a "fifo_ok" boolean: the failure
    # this catches is a queue that delivers ten commands in the wrong order, and a
    # boolean cannot say which pair inverted.
    "steering_queue": (
        "submission_order",
        "handoff_order",
        "accepted_count",
        "overflow_status",
        "paused_pending_ids",
        "paused_delivered_after_resume",
        "abort_cancelled_ids",
        "expiry_outcome",
        "replayed_after_unknown",
        "authority_revalidated_at_handoff",
    ),
    # W3-08 / AC-S8. The bytes, not a claim about the bytes. `delimiters_present`
    # alone would be satisfiable by a wrapper appended AFTER the raw instruction,
    # so `instruction_inside_delimiters` is separate and is the one that matters.
    "steering_trust_boundary": (
        "delimiters_present",
        "instruction_inside_delimiters",
        "actor_attribution",
        "origin_kind",
        "should_query",
        "attacker_actor_metadata_rejected",
        "raw_instruction_in_system_text",
    ),
    # W3-09 / AC-T6. Recorded message and turn counts from a real stream, which
    # #3969 explicitly refuses to accept as a source grep or a mock.
    "steering_input_stream": (
        "initial_task_consumed",
        "later_user_messages",
        "generator_disposed",
        "query_closed",
        "message_count",
        "turn_count",
        "observed_by",
    ),
    # W3-11 / AC-T7. The retry. `deliveries_of_queued_command` is an integer
    # because both 0 and 2 are real failures with opposite causes — a stranded
    # instruction and a duplicated one — and a boolean would merge them.
    "steering_retry": (
        "queued_command_id",
        "deliveries_of_queued_command",
        "confirmed_handoffs_replayed",
        "session_preserved",
        "attempt_id_before",
        "attempt_id_after",
        "ambiguous_handoff_outcome",
        "abort_during_retry_started_next_attempt",
    ),
    # Wave 4 / S7 (#3966). A captured Playwright run against the DEPLOYED SPA.
    #
    # Why an artifact rather than the harness driving the browser itself: this
    # harness is a Python HTTP prober with no browser dependency, and giving it one
    # would make every wave-1 run require a Chromium download. The operator runs
    # `tests/e2e/agent-control.config.ts` and records what the browser observed.
    #
    # What makes that trustworthy rather than a restated claim: the keys below are
    # OBSERVATIONS a browser can make and a source file cannot — the destinations
    # actually requested, the poll intervals actually measured, the DOM text
    # actually rendered. `bundle_revision` ties them to a deployed asset, and the
    # gate checks compare it against the preflight's frontend revision, so a
    # capture from a developer's laptop cannot answer for production.
    "browser_control_run": (
        # Provenance: which bundle, which run, when. Without these the capture is
        # an anonymous JSON blob that could describe any environment.
        "bundle_revision",
        "gateway_url",
        "captured_at",
        "spec_digest",
        # AC-F3: the three fail-closed renders, each with the node and request
        # counts the wave-4 table demands be zero.
        "flag_off",
        "flag_loading",
        "flag_error",
        # Capability gating: only advertised verbs, and who cannot submit.
        "advertised_capabilities",
        "rendered_controls",
        "nonowner_submit_blocked",
        "terminal_submit_blocked",
        # AC-P4 and AC-T1: the observed phase sequence and the steer journal.
        "phase_sequence",
        "pause_copy_mentions_spend",
        "active_tool_reason",
        "steer_request",
        "steer_status_sequence",
        # W4-08: measured polling behaviour, not a configured constant.
        "poll_intervals_ms",
        "polled_while_hidden",
        "polled_after_close",
        "polled_after_terminal",
        "backoff_intervals_ms",
        "detail_refreshed_after_command",
        # W4-06: every destination and body the browser actually sent.
        "request_destinations",
        "request_bodies_contain_pod_address",
        "request_bodies_contain_token",
        "spoofed_identity_rejected",
    ),
    # W4-01 / Gate-regression. Wave 4's own preflight. Wave 2's analogue plus the
    # two things wave 4 adds: the prior waves' ACCEPTANCE records, and the frontend
    # as a third deployed component.
    #
    # `prior_waves` is a map keyed by wave number rather than a single
    # `waves_1_3_accepted` boolean, for the reason that boolean would be useless:
    # wave 3 being unaccepted and wave 1 being stale have different owners and
    # different fixes, and a collapsed flag names neither. Each entry's
    # compatibility is COMPUTED from the commit graph, never read — see
    # PRIOR_WAVE_ACCEPTANCE_KEYS.
    #
    # `browser_identity` is here rather than in the browser capture because it is a
    # statement about the fixture's authorization, not about the DOM: the identity
    # the browser drove must be the run's owner, and ordinary users must still be
    # gated. A capture cannot establish the second half at all — it only ever drove
    # one identity.
    "wave4_preflight": (
        "prior_waves",
        "merged_revisions",
        "deployed_components",
        "frontend",
        "ci_gates",
        "browser_identity",
        "ordinary_users_gated",
        "ordinary_flags_off",
        "fixture_identity",
    ),
    # W4-03 / AC-T1..T8, AC-S8. The steering half wave 4 repeats from wave 3.
    #
    # `criteria` is the per-AC evidence map, and `evidenced_at`/`evidenced_revision`
    # are what make staleness checkable: an AC evidenced before a surface it covers
    # was last modified describes code that is no longer running. The FIFO, retry,
    # cap and SDK proofs are named individually rather than rolled into a
    # `steering_proven` boolean, because W4-03's row lists them separately and a
    # collapsed flag cannot say which one was never made.
    "wave4_steering_evidence": (
        "wave",
        "evaluation",
        "criteria",
        "evidenced_revision",
        "evidenced_at",
        "fifo_order_proven",
        "retry_delivery_proven",
        "pending_cap_proven",
        "sdk_bound_text_proven",
        "fixture_pivot",
        "merged_test_pr",
        "fixture_identity",
    ),
    # W4-05 / AC-A1..A12. The abort half, from wave 3.
    #
    # `completed_at_observed` is a read of the ACTUAL row rather than a claim about
    # it: W4-05's row demands "the actual row has completed_at", which is the
    # difference between a UI that renders a terminal state and a record that is one.
    "wave4_abort_evidence": (
        "wave",
        "evaluation",
        "criteria",
        "evidenced_revision",
        "evidenced_at",
        "cancel_left_run_untouched",
        "confirmed_abort_terminal",
        "finalized_comment_count",
        "aborted_renderers",
        "repeat_and_double_abort",
        "completed_at_observed",
        "stats_writer_assertions",
        "fixture_identity",
    ),
    # W4-06 / AC-S1..S7. The deployed security matrix, repeated at wave 4's
    # revisions, plus the browser-destination capture that is wave 4's own addition.
    #
    # `bundle_scan_supplemental` is required to be an explicit acknowledgement rather
    # than absent: the row says the bundle scan is supplemental ONLY, and a matrix
    # whose evidence is a static scan of the bundle has not probed a deployment.
    "wave4_security_matrix": (
        "wave",
        "evaluation",
        "criteria",
        "evidenced_revision",
        "evidenced_at",
        "non_gateway_probe_blocked",
        "bundle_scan_supplemental",
        "fixture_identity",
    ),
    # W4-09 / AC-F1, AC-F2. The flag-off/flag-on runtime comparison rerun with final
    # code, and the live stats provenance.
    #
    # `stats_response_keys` and `stats_source` are separate because the row asks two
    # different questions: does the live schema match the CURRENT RunStatsResponse
    # (parity), and did these numbers come from the live endpoint rather than a mock
    # (provenance). A schema can match perfectly on fabricated data.
    "wave4_runtime_comparison": (
        "wave",
        "evaluation",
        "criteria",
        "evidenced_revision",
        "evidenced_at",
        "flag_off_events_digest",
        "flag_on_events_digest",
        "differing_fields",
        "ordinary_flags_off",
        "stats_response_keys",
        "stats_source",
        "fixture_identity",
    ),
    # W4-10 / Gate-regression. The 37-criterion consolidation index.
    #
    # `criteria` must cover EXACTLY the 37 IDs the four waves' manifests declare —
    # computed from those manifests, so the number cannot drift by editing a
    # constant. Each entry names its owner, the evaluation that evidenced it, the
    # revision that evidence was taken at, and whether it was a LIVE observation:
    # the row forbids a unit mock standing in for a named live SDK/API/browser check,
    # and that is only checkable if the record says which it was.
    "wave4_evidence_index": (
        "criteria",
        "evaluations",
        "compiled_at",
        "compiled_revision",
        "fixture_identity",
    ),
}

# Per-criterion keys in the wave-4 evidence index. Each is a separate way for a
# green index row to be worthless:
#
#   owner      — who produced it, so a reviewer can go to them
#   evaluation — which wave evidenced it (an AC claimed by no evaluation is unowned)
#   revision   — the commit it was evidenced at, for the containment check
#   evidence   — the artifact path or observation reference, nonempty
#   live       — whether it was a live observation or a mocked/unit result. W4-10's
#                row forbids a unit mock replacing a named live check, so this has to
#                be recorded rather than assumed.
EVIDENCE_INDEX_ENTRY_KEYS: tuple[str, ...] = (
    "owner",
    "evaluation",
    "revision",
    "evidence",
    "live",
)

# Acceptance IDs whose rows name a live SDK, API or browser observation, so a
# mocked or unit-test result cannot satisfy them. Derived from the wave-4 table's
# own wording: every AC carried by a browser check (AC-F3, the pause and steer
# families) or by the deployed security matrix.
#
# Not "every AC": some criteria are genuinely provable by a contract suite — AC-T7's
# harness neutrality is asserted against two adapter fixtures by design — and
# demanding a live browser for those would be a requirement the specification does
# not make.
LIVE_EVIDENCE_REQUIRED_IDS: frozenset[str] = frozenset(
    {
        # AC-F3 and the pause family are observed in a browser against a deployment.
        "AC-F3",
        "AC-P1", "AC-P2", "AC-P3", "AC-P4", "AC-P5", "AC-P6",
        # The steering family is a live delivery to a real SDK attempt.
        "AC-T1", "AC-T2", "AC-T3", "AC-T4", "AC-T5", "AC-T6", "AC-T8",
        # The abort family transitions a real row to terminal.
        "AC-A1", "AC-A2", "AC-A3", "AC-A4", "AC-A5", "AC-A6",
        "AC-A7", "AC-A8", "AC-A9",
        # The security matrix is a probe of a deployment, not a bundle scan.
        "AC-S1", "AC-S2", "AC-S3", "AC-S4", "AC-S5", "AC-S6", "AC-S7", "AC-S8",
    }
)

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


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 instant, or ``None`` if it is not one.

    Ordering two recorded times is the only way this harness can establish that an
    observation postdates the removal it describes, and comparing them as STRINGS
    does not do that. Two spellings of the same instant differ by punctuation —
    ``2026-09-12T01:00:30Z`` versus ``2026-09-12T01:00:30+00:00`` — and ``"Z"``
    sorts after ``"+"``, so a ``Z``-suffixed artifact compares as *later* than a
    harness record written at the same moment. That is a false pass in the exact
    direction this check exists to refuse, so the comparison has to go through real
    datetimes.

    A naive timestamp is read as UTC rather than rejected: the harness's own records
    are always offset-aware, and an operator's collection script writing local-naive
    time should not make the comparison silently incomparable. Returning ``None`` for
    an unparseable value lets the caller say "this cannot be ordered" instead of
    guessing.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    # Ancestry answers the harness computed itself while running this check. Recorded
    # because they are first-hand observations, and because a reviewer reading the
    # evidence file should be able to see WHICH revisions were compared rather than
    # only that containment held.
    ancestry: list[dict] = field(default_factory=list)
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
        evidence.extend({"git_ancestry": answer} for answer in self.ancestry)
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


@dataclass(frozen=True)
class RowDeletion:
    """What actually happened to one declared fixture row.

    Both key halves, the delete, and the confirming read are recorded separately
    because they fail separately and W2-10 must be able to name which one. A
    single ``removed: bool`` would make "deleted with a partial key" and "deleted
    and confirmed gone" the same evidence.
    """

    event_id: str
    arrived_at: str
    both_keys_present: bool
    deleted: bool
    confirmed_absent: bool
    error: str | None = None
    # Whether the row was actually there to delete, from DeleteItem's ALL_OLD.
    # `None` means the delete did not report (an error path). DynamoDB's DeleteItem
    # is idempotent and succeeds identically on a key that never existed, so
    # without this every field above reads the same for "removed the fixture row"
    # and "deleted nothing at all" — which would let a config naming the wrong
    # table, environment or key format report a clean, fully-verified teardown.
    existed: bool | None = None

    def to_evidence(self) -> dict:
        return {
            "event_id": self.event_id,
            "arrived_at": self.arrived_at,
            "both_keys_present": self.both_keys_present,
            "deleted": self.deleted,
            "confirmed_absent": self.confirmed_absent,
            "existed": self.existed,
            "error": self.error,
        }


@dataclass(frozen=True)
class CleanupOutcome:
    """The harness's own record of the teardown it performed.

    This exists so W2-10 can be a *verified* cleanup check. The alternative —
    reading a "cleanup_succeeded" boolean out of an operator artifact — would pass
    on a fixture that was never torn down, which is the specific defect W2-10 is
    supposed to catch. ``deletions`` is first-hand: it is written by the code that
    issued the DeleteItem and read the consistent get.
    """

    ok: bool
    notes: list[str]
    deletions: list[RowDeletion]
    declared_items: int

    def to_evidence(self) -> dict:
        return {
            "ok": self.ok,
            "declared_items": self.declared_items,
            "deletions": [deletion.to_evidence() for deletion in self.deletions],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FixtureCleanup:
    """Whether the WHOLE fixture is established as cleaned up — not just its rows.

    ``CleanupOutcome.ok`` answers a narrower question than the report's top-level
    ``cleanup_ok`` implies: it covers the DynamoDB rows the harness deleted itself,
    and nothing else. Root reproduced what that costs. A run whose resource teardown
    could not even be executed — leaving all three fixture resources in place —
    still printed ``cleanup_ok=true`` on its summary line and wrote it into
    ``result.json``. The exit code and W2-10 were both right; the field an operator
    reads to decide whether the environment was left clean was wrong, and that is
    the field that matters for DP-INV-1.

    So the row record keeps its own name and stays in the report as ``cleanup``,
    and the top-level ``cleanup_ok`` becomes this aggregate: the rows were deleted,
    AND the fixture's resource teardown actually ran and succeeded, AND the
    post-teardown absence verification passed.

    ``ok`` is WITHHELD on anything unestablished rather than granted, including a
    verification check that failed for some reason other than absence. The check is
    not decomposable from out here, and cleanup reporting is one place where the
    conservative direction is not arbitrary: "this run did not establish that the
    fixture is gone" invites somebody to look, while the opposite mistake leaves a
    control-enabled workload running with a green line beside it.
    """

    ok: bool
    rows_ok: bool
    resources_ok: bool
    absence_verified: bool
    notes: list[str]

    def to_evidence(self) -> dict:
        return {
            "ok": self.ok,
            "rows_ok": self.rows_ok,
            "resources_ok": self.resources_ok,
            "absence_verified": self.absence_verified,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class LiveCapabilityCapture:
    """One adapter's capability surface, read live while the fixture still existed.

    This is the harness's own half of the security capture, and it exists because of
    an ordering defect worth stating plainly: the question "does this deployment
    still refuse the unimplemented verbs?" can only be answered while there is a
    deployment to ask. An earlier revision asked it AFTER teardown, about a run whose
    row teardown had just deleted — so a *correct* teardown produced a not-found and
    the check reported NOT RUN. A removed resource must never be required to answer.

    ``status`` and ``capabilities`` are what the live read actually returned, kept as
    raw observations rather than reduced to a verdict, so a reviewer can see what was
    read and not merely what was concluded from it.

    ``refusals`` maps each verb the deployment itself reported as UNAVAILABLE to the
    status it returned for an authorized owner's attempt at it. Recording the attempt
    is what makes "unsupported verbs are still refused" a first-hand observation
    rather than a restatement of the operator's map. It is confined to verbs the
    capability surface reports false precisely so it cannot mutate the fixture: an
    unimplemented verb's handler refuses before doing anything, whereas posting an
    IMPLEMENTED verb (pause and resume, in this wave) would actually act on the run.
    """

    adapter: str
    run_id: str
    status: int | None
    capabilities: dict
    refusals: dict
    error: str | None = None

    def to_evidence(self) -> dict:
        return {
            "adapter": self.adapter,
            "run_id": self.run_id,
            "status": self.status,
            "capabilities": dict(self.capabilities),
            "refusals": dict(self.refusals),
            "error": self.error,
        }


@dataclass(frozen=True)
class SecurityCapture:
    """The pre-teardown security observations, bound to the run that made them.

    Held by `main` across the teardown boundary and handed to W2-10 afterwards. The
    binding matters as much as the content: ``run_id`` is what stops an observation
    from another run being presented as this one's evidence.

    Binding to the BUILD is deliberately not carried here, because the harness cannot
    honestly observe it: the §7 state contract carries run state, not build identity
    (`run_id`, `generation`, `available`, `reason`, `capabilities`, `state`,
    `active_tool_count`, `updated_at`, `commands` — no revision or digest anywhere).
    A `deployed_revisions` field on this record could therefore only ever be a copy
    of the operator artifact it is supposed to corroborate, which is circular. The
    build binding is instead asserted in W2-10 by comparing the artifact's
    `observed_revisions` against the preflight's `deployed_components` — two
    separately recorded operator observations that must agree — while this record
    supplies the part the harness genuinely saw for itself: the capability surface
    the deployment returned, pre-teardown, for this run.

    ``ok`` false means the capture itself did not complete, which is NOT RUN for the
    half that depends on it — never a pass. An absent capture (``None`` at the call
    site) means the same thing.
    """

    ok: bool
    run_id: str
    adapters: list[LiveCapabilityCapture]
    notes: list[str]

    def to_evidence(self) -> dict:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "adapters": [adapter.to_evidence() for adapter in self.adapters],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ResourceTeardown:
    """The harness's own record of INVOKING the fixture's resource teardown.

    This closes an ordering gap that documentation could not: the published command
    captured live state, deleted ROWS only, and then judged W2-10 against an artifact
    already declaring the pods, queues and policies gone. Nothing in between removed a
    resource or waited for one to go, so a normal sequential operator run could not
    legitimately produce that artifact at the point it was read — the only way to have
    it was to write it before the resources were removed, which is the prefilled
    absence the evaluation must refuse.

    The harness cannot perform resource teardown itself. It has no cluster access, and
    acquiring any would give a read-only evaluator the ability to delete workloads. So
    the seam is an explicit command the operator's fixture script supplies
    (``resource_teardown`` in the fixture config), which the harness INVOKES between
    the capture and the verification, recording:

    * ``invoked``       — whether the harness actually ran it (not whether the
                          operator says it was run);
    * ``exit_code`` /
      ``ok``            — what it reported;
    * ``started_at`` /
      ``finished_at``   — the window, so the absence artifact can be required to have
                          been written INSIDE it rather than beforehand;
    * ``stdout_digest`` — a digest of its output, kept as evidence without putting
                          arbitrary command output (a plausible place for a token to
                          appear) into the report.

    ``configured=False`` is the "no seam" case, which makes W2-10 ``not_run``: the
    lifecycle the check verifies was never executed, and that is a missing
    prerequisite rather than a pass.

    **Freshness is established by the harness, not by the artifact.** The two
    ``verification_*_before`` fields are a digest of the post-teardown absence
    artifact taken *immediately before* the teardown command is invoked. W2-10 then
    digests the file it actually read and refuses a match. This is what makes the
    ordering real rather than documentary: a file that has not changed across the
    removal it describes was written before that removal happened, and no field the
    operator can put *inside* the artifact can establish otherwise — a ``captured_at``
    is a string the same hand wrote. So the operator's teardown script has to record
    absence as part of tearing down, which is the lifecycle this check is supposed to
    be verifying.
    """

    configured: bool
    invoked: bool
    ok: bool
    exit_code: int | None
    started_at: str | None
    finished_at: str | None
    stdout_digest: str | None
    verification_present_before: bool
    verification_digest_before: str | None
    notes: list[str]

    def to_evidence(self) -> dict:
        return {
            "configured": self.configured,
            "invoked": self.invoked,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stdout_digest": self.stdout_digest,
            "verification_present_before": self.verification_present_before,
            "verification_digest_before": self.verification_digest_before,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class GitAncestry:
    """The harness's own answer to "is this revision contained in that one?".

    A first-hand record, in the same sense as the other dataclasses here: it holds
    what the harness OBSERVED rather than what the operator claimed. The artifact's
    ``is_ancestor`` field is the conclusion the check is supposed to reach, so
    accepting it made the check a restatement of the claim under test.

    ``available`` separates the two negative answers that must not be confused:
    ``available=True, is_ancestor=False`` is git saying the running build does not
    contain the revision (a failed evaluation), while ``available=False`` is git being
    unable to answer — an absent commit, a shallow clone, no git at all — which is a
    missing prerequisite. ``reason`` carries which, so the check can say so.
    """

    ancestor: str
    descendant: str
    available: bool
    is_ancestor: bool | None
    reason: str | None

    def to_evidence(self) -> dict:
        return {
            "ancestor": self.ancestor,
            "descendant": self.descendant,
            "available": self.available,
            "is_ancestor": self.is_ancestor,
            "reason": self.reason,
        }


class ProvenanceParseError(Exception):
    """An archived tool document could not be parsed as the schema it claims to be.

    Separate from :class:`AssertionError` at the raising site only so the parsers can
    be written as ordinary functions; every caller converts it into an
    ``AssertionError``, because an unparseable provenance document is a failed
    evaluation rather than a missing one — the document IS present, it just does not
    say what the summary claims.
    """


# The build outcomes that mean "this build produced the artifact". Anything else —
# FAILED, FAULT, STOPPED, TIMED_OUT, IN_PROGRESS for CodeBuild; failure, cancelled,
# skipped, action_required, or a null in-progress conclusion for GitHub — is a build
# whose output must not be treated as a published image. Root's reproduction (b) was
# exactly this: a build document with `conclusion: failure` passed, because nothing
# read the field at all.
_CODEBUILD_SUCCESS = "SUCCEEDED"
_GITHUB_SUCCESS = "success"


def _require(condition: bool, message: str) -> None:  # noqa: FBT001 - internal guard
    if not condition:
        raise ProvenanceParseError(message)


def _as_list(value: object, what: str) -> list:
    _require(isinstance(value, list) and bool(value), f"{what} must be a nonempty list")
    return list(value)  # type: ignore[arg-type]


def _as_dict(value: object, what: str) -> dict:
    _require(isinstance(value, dict), f"{what} must be an object")
    return value  # type: ignore[return-value]


def parse_codebuild_build(body: object) -> dict:
    """Extract the build facts from a real `aws codebuild batch-get-builds` response.

    This is the parser root's review asked for, and the reason it is a parser rather
    than a substring search is worth stating exactly. The previous implementation
    dumped the archived body to JSON and asked whether the expected values appeared
    anywhere in that text. Presence in text is not the same claim as a field holding
    a value, and root demonstrated the gap with three cases that all passed: a build
    whose ``conclusion`` was ``failure``, a CI run whose every job had failed, and a
    body containing nothing but ``{"unrelated_notes": "...<run id> ... <revision>"}``.
    The third is the clearest: any document that merely MENTIONS the right strings
    satisfied a substring test, so the archive requirement had bought nothing.

    So the fields are read from their actual locations and the outcome is checked:

    * ``builds[0].id``                                   — the build identity
    * ``builds[0].buildStatus``                          — must be ``SUCCEEDED``
    * ``builds[0].environment.environmentVariables``     — ``ADP_SOURCE_SHA`` is the
      revision the release build archived, and ``IMAGE_TAG`` is the tag it pushed
    * ``builds[0].source.location``                      — the S3 source object, which
      ``codebuild-run.sh`` names ``codebuild/src/<sha>-<unique>.zip``, so the archive
      the build actually consumed is bound to the revision by the key itself and not
      only by the environment override

    Returns the extracted facts so the caller can compare them against the summary
    fields; raises :class:`ProvenanceParseError` if the document is not a build
    response, describes more or fewer than one build, or reports a non-success
    outcome.
    """
    document = _as_dict(body, "the archived CodeBuild document")
    builds = _as_list(document.get("builds"), "'builds' in the CodeBuild response")
    _require(
        len(builds) == 1,
        f"the archived CodeBuild response describes {len(builds)} builds; archive the response for "
        "exactly the build that produced this image, so there is no ambiguity about which one the "
        "summary refers to",
    )
    build = _as_dict(builds[0], "the CodeBuild build entry")

    status = build.get("buildStatus")
    _require(
        status == _CODEBUILD_SUCCESS,
        f"the archived CodeBuild build reports buildStatus {status!r}, not {_CODEBUILD_SUCCESS!r}. A "
        "build that failed, was stopped, timed out or is still running did not publish an image, so "
        "its output must not be read as provenance for a running one",
    )
    build_id = build.get("id")
    _require(
        isinstance(build_id, str) and bool(build_id),
        f"the archived CodeBuild build records id {build_id!r}; the build identity is what makes the "
        "record retrievable",
    )

    environment = _as_dict(
        build.get("environment"), "'environment' in the CodeBuild build"
    )
    variables = _as_list(
        environment.get("environmentVariables"),
        "'environment.environmentVariables' in the CodeBuild build",
    )
    resolved: dict[str, str] = {}
    for variable in variables:
        entry = _as_dict(variable, "a CodeBuild environment variable")
        name, value = entry.get("name"), entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            resolved[name] = value
    source_sha = resolved.get("ADP_SOURCE_SHA")
    _require(
        isinstance(source_sha, str) and bool(_GIT_REVISION_RE.match(source_sha)),
        f"the archived CodeBuild build records ADP_SOURCE_SHA {source_sha!r}, which is not a full "
        "40-character git SHA. On the release path (`codebuild-run.sh` with ADP_RELEASE_BUILD=true) "
        "this override IS the revision whose `git archive` became the build's source, so it is the "
        "only field in the response that names the commit",
    )
    image_tag = resolved.get("IMAGE_TAG")
    _require(
        isinstance(image_tag, str) and bool(image_tag),
        f"the archived CodeBuild build records IMAGE_TAG {image_tag!r}; the tag is what binds the "
        "build to a registry entry, and a build whose tag is unrecorded cannot be matched to the "
        "image the component is running",
    )

    # The source the build CONSUMED, not only the revision it was told it was
    # building. `codebuild-run.sh` uploads `git archive <sha>` to
    # `codebuild/src/<sha>-<unique>.zip` and passes that key as
    # --source-location-override, so the key itself carries the revision. Checking it
    # means a build whose ADP_SOURCE_SHA override disagrees with the archive it
    # actually built is rejected, rather than the override being taken on trust.
    source = _as_dict(build.get("source"), "'source' in the CodeBuild build")
    location = source.get("location")
    _require(
        isinstance(location, str) and bool(location),
        f"the archived CodeBuild build records source.location {location!r}; without the source "
        "location there is nothing to bind the build to the archive it consumed",
    )
    _require(
        source_sha in location,
        f"the archived CodeBuild build consumed source {location!r}, which does not name revision "
        f"{source_sha!r}. `codebuild-run.sh` uploads `git archive <sha>` to "
        "`codebuild/src/<sha>-<unique>.zip`, so the key names the revision it packaged; a build whose "
        "ADP_SOURCE_SHA claims one revision while its source archive is another built something else",
    )
    return {
        "build_id": build_id,
        "status": status,
        "source_revision": source_sha,
        "source_location": location,
        "image_tag": image_tag,
        "publish_latest": resolved.get("PUBLISH_LATEST"),
    }


def parse_push_digest_from_build_log(body: object, *, image_tag: str) -> str:
    """Read the digest `docker push` reported for one tag out of an archived build log.

    This is the link root's review said was missing entirely: ``built_digest`` was
    compared against the running image and against the registry, but nothing ever
    corroborated it against the BUILD's own output, so the value was still one the
    operator typed. The build log is where the build states the digest it published —
    `docker push` ends each tag with a line of the form::

        <tag>: digest: sha256:<64 hex> size: <bytes>

    so the digest can be extracted rather than asserted. Accepts the
    ``aws logs get-log-events`` response shape (``{"events": [{"message": ...}]}``),
    which is what the real collection command returns, or the log text directly.

    Raises :class:`ProvenanceParseError` if no push line for ``image_tag`` is present,
    or if the log reports pushing that tag more than once with different digests —
    an ambiguous log cannot establish which digest the tag ended up at.
    """
    if isinstance(body, str):
        text = body
    else:
        document = _as_dict(body, "the archived build-log document")
        events = _as_list(document.get("events"), "'events' in the build-log response")
        messages = []
        for event in events:
            entry = _as_dict(event, "a build-log event")
            message = entry.get("message")
            _require(
                isinstance(message, str),
                f"a build-log event records message {message!r}; log events carry their text in "
                "'message'",
            )
            messages.append(message)
        text = "\n".join(messages)

    pattern = re.compile(
        rf"^\s*{re.escape(image_tag)}:\s+digest:\s+(sha256:[0-9a-f]{{64}})\s+size:\s+\d+",
        re.MULTILINE,
    )
    found = {match.group(1) for match in pattern.finditer(text)}
    _require(
        bool(found),
        f"the archived build log contains no `docker push` digest line for tag {image_tag!r}. The "
        "build states the digest it published on the line `<tag>: digest: sha256:... size: ...`, and "
        "without it the recorded built_digest is a value nobody read out of the build — which is the "
        "case root's review found uncorroborated",
    )
    _require(
        len(found) == 1,
        f"the archived build log reports pushing tag {image_tag!r} at {len(found)} different digests "
        f"({sorted(found)}). An ambiguous log cannot establish which digest the tag ended up at, so it "
        "cannot corroborate one",
    )
    return found.pop()


def parse_github_run(body: object, *, required_job: str | None = None) -> dict:
    """Extract the run facts from a real `gh run view --json ...` response.

    The GitHub counterpart of :func:`parse_codebuild_build`, and it closes root's
    reproduction (a): every job in the archived document had ``conclusion: failure``
    and the gate still passed, because the conclusions were never read.

    * ``databaseId``  — the run identity ``gh run view`` takes
    * ``headSha``     — the revision the run checked out
    * ``conclusion``  — must be ``success`` when present
    * ``jobs[]``      — when ``required_job`` is given, a job of that exact name must
      be present AND have concluded ``success``. A gate is a NAMED job; "some job in
      this run passed" is a different and much weaker claim.

    This response must be **unmodified**. The revision a manual run actually checked
    out does not come from here — it comes from the workflow's own uploaded artifact
    (:func:`parse_checkout_artifact`). An earlier revision let the operator paste a
    ``checked_out_revision`` key into this document and then trusted it; root rejected
    that, because it makes the one fact the manual path exists to establish an
    assertion inside a document otherwise written by GitHub. Such a key is now a
    parse error rather than an accepted override.
    """
    document = _as_dict(body, "the archived GitHub run document")

    run_id = document.get("databaseId")
    _require(
        isinstance(run_id, (str, int)) and str(run_id).strip() != "",
        f"the archived run document records databaseId {run_id!r}; without the run identity the "
        "document cannot be tied to a retrievable run",
    )
    head_sha = document.get("headSha")
    _require(
        isinstance(head_sha, str) and bool(_GIT_REVISION_RE.match(head_sha)),
        f"the archived run document records headSha {head_sha!r}, which is not a full 40-character "
        "git SHA",
    )
    conclusion = document.get("conclusion")
    if conclusion is not None:
        _require(
            conclusion == _GITHUB_SUCCESS,
            f"the archived run document reports conclusion {conclusion!r}, not {_GITHUB_SUCCESS!r}. A "
            "failed, cancelled or still-running workflow is not a green gate, and an in-progress run "
            "carries a null conclusion rather than a passing one",
        )

    job_conclusions: dict[str, object] = {}
    if "jobs" in document:
        for job in _as_list(document.get("jobs"), "'jobs' in the archived run document"):
            entry = _as_dict(job, "a job in the archived run document")
            name = entry.get("name")
            if isinstance(name, str):
                job_conclusions[name] = entry.get("conclusion")
    if required_job is not None:
        _require(
            required_job in job_conclusions,
            f"the archived run document contains no job named {required_job!r} (it has "
            f"{sorted(job_conclusions)}). The required gate is a NAMED job, so a run that never ran it "
            "does not satisfy it however green the rest of the run was",
        )
        actual = job_conclusions[required_job]
        _require(
            actual == _GITHUB_SUCCESS,
            f"the archived run document reports job {required_job!r} as {actual!r}, not "
            f"{_GITHUB_SUCCESS!r}. The gate is this job's outcome; the run's overall status can be "
            "green while a specific required job was skipped or failed",
        )

    # A hand-added checked-out SHA is refused rather than believed. This is the
    # authoritative API response and nothing else; the revision the jobs checked out
    # is read from the workflow's uploaded artifact, which the operator did not write.
    for key in ("checked_out_revision", "checkedOutRevision"):
        _require(
            key not in document,
            f"the archived run document carries a {key!r} key. `gh run view` does not return one, so it "
            "was added by hand — and the revision a manual run checked out is exactly the fact that must "
            "not be an assertion. Archive the workflow's own `checked-out-revision-*` artifact instead, "
            "and leave this response as the API returned it",
        )
    return {
        "run_id": str(run_id),
        "head_revision": head_sha,
        "conclusion": conclusion,
        "jobs": job_conclusions,
        "attempt": document.get("attempt"),
        "event": document.get("event"),
    }


def parse_checkout_artifact(body: object, *, expected_job: str) -> dict:
    """Read the revision a CI job actually checked out, from the job's own artifact.

    The counterpart to :func:`parse_github_run`, and the answer to root's second
    manual-CI finding. `.github/workflows/agent-control-ci.yml` runs `git rev-parse
    HEAD` inside each test job, fails the job when it disagrees with the dispatched
    SHA, and uploads the result as `checked-out-revision-<job>`. That file is written
    by the job, in the job, about the tree the job had — which is what makes it
    evidence rather than a claim, and it is why the operator archives the artifact
    instead of transcribing a SHA into the run response.

    Scoped to the six keys that step actually writes (:data:`CHECKOUT_ARTIFACT_KEYS`).
    ``job`` must be the job this gate is about, because a run has three of these
    artifacts and the one for a different job says nothing about this gate's checkout.
    The caller additionally binds ``run_id``/``run_attempt`` to the archived run.
    """
    document = _as_dict(body, "the archived checkout artifact")
    missing = [key for key in CHECKOUT_ARTIFACT_KEYS if key not in document]
    _require(
        not missing,
        f"the archived checkout artifact is missing {sorted(missing)}; the workflow's "
        f"`Verify and record the checked-out revision` step writes all of "
        f"{list(CHECKOUT_ARTIFACT_KEYS)}, so a document without them is not that artifact",
    )
    job = document["job"]
    _require(
        job == expected_job,
        f"the archived checkout artifact is for job {job!r}, not {expected_job!r}. A manual run uploads "
        "one of these per test job, so an artifact from a different job establishes a different job's "
        "checkout",
    )
    revision = document["checked_out_revision"]
    _require(
        isinstance(revision, str) and bool(_GIT_REVISION_RE.match(revision)),
        f"the archived checkout artifact records checked_out_revision {revision!r}, which is not a full "
        "40-character git SHA",
    )
    workflow_ref_sha = document["workflow_ref_sha"]
    _require(
        isinstance(workflow_ref_sha, str) and bool(_GIT_REVISION_RE.match(workflow_ref_sha)),
        f"the archived checkout artifact records workflow_ref_sha {workflow_ref_sha!r}, which is not a "
        "full 40-character git SHA",
    )
    return {
        "job": job,
        "checked_out_revision": revision,
        "run_id": str(document["run_id"]),
        "run_attempt": str(document["run_attempt"]),
        "event_name": document["event_name"],
        "workflow_ref_sha": workflow_ref_sha,
    }


def parse_ecr_images(body: object) -> dict:
    """Extract the served digest and its tags from `aws ecr describe-images` output.

    The registry is the one side of the chain the operator cannot retype their way
    around, so this reads ``imageDetails[0].imageDigest`` and ``imageTags`` from their
    real locations rather than asking whether a digest-shaped string appears somewhere
    in the response.
    """
    document = _as_dict(body, "the archived ECR document")
    details = _as_list(
        document.get("imageDetails"), "'imageDetails' in the ECR response"
    )
    _require(
        len(details) == 1,
        f"the archived ECR response describes {len(details)} images; archive the response for exactly "
        "the image the component is running, so there is no ambiguity about which digest is served",
    )
    detail = _as_dict(details[0], "the ECR image detail")
    digest = detail.get("imageDigest")
    _require(
        isinstance(digest, str) and bool(_DIGEST_RE.match(digest)),
        f"the archived ECR response records imageDigest {digest!r}, which is not a sha256 digest",
    )
    tags = detail.get("imageTags")
    if tags is not None:
        tags = _as_list(tags, "'imageTags' in the ECR image detail")
    return {
        "digest": digest,
        "tags": [tag for tag in (tags or []) if isinstance(tag, str)],
        "repository": detail.get("repositoryName"),
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

    def resolve(self, name: str) -> Path | None:
        """Where an artifact WOULD be read from, without reading or validating it.

        Separate from :meth:`require` because one caller needs the path before the
        file is supposed to exist: the teardown seam has to observe whether the
        post-teardown absence artifact was already sitting there beforehand, and
        ``require`` would either raise on the absent file or cache a stale read of a
        prefilled one.
        """
        raw_path = self._mapping.get(name)
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_absolute() and self._base is not None:
            path = self._base / path
        return path

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

    def __init__(  # noqa: ANN001, PLR0913 - each collaborator is a separately substitutable seam
        self,
        config: dict,
        probe: Probe,
        artifacts: ArtifactStore,
        *,
        dynamodb=None,
        repo_root: Path | None = None,
        git_runner=None,
    ):
        self.config = config
        self.probe = probe
        self.artifacts = artifacts
        self.dynamodb = dynamodb
        # Where the commit graph is read from when W2-01 computes containment. Defaults
        # to the checkout this script lives in, which is the repository whose revisions
        # the provenance names.
        self._repo_root = repo_root if repo_root is not None else REPO_ROOT
        self._git_runner = git_runner
        self._used_artifacts: list[str] = []
        # Every ancestry answer git gave, in order, so the evidence file records what
        # was computed rather than only that the check passed.
        self._ancestry: list[dict] = []

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

    @staticmethod
    def _deployed_components(preflight: dict) -> dict:
        """Per-component deployed identity: what is running, and what it was built from.

        Three facts per component rather than a source/deployed digest pair. The pair
        was insufficient in a way worth being explicit about: two equal recorded
        strings establish that the operator wrote the same value twice, and nothing
        about the running image. ``source_revision`` is what the image was BUILT FROM,
        so the chain is source revision → image digest → running component, and each
        link is separately checkable.

        The components must also not be identical to each other: they are separate
        images from separate Dockerfiles, so an equal digest pair means one recorded
        value was copied over the other — which would let a stale half-deployment
        satisfy every per-component comparison.
        """
        entries = preflight["deployed_components"]
        if not isinstance(entries, dict):
            raise AssertionError(
                f"'deployed_components' must be an object keyed by component, got {entries!r}. The control "
                f"path ships as {len(WAVE2_DEPLOYED_COMPONENTS)} images from separate workflows, so a "
                "single flat record cannot say which of them is stale"
            )
        resolved: dict[str, dict] = {}
        for component in WAVE2_DEPLOYED_COMPONENTS:
            entry = entries.get(component)
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"no deployed identity recorded for the {component!r} component ({entry!r}). A "
                    "gateway speaking this contract in front of a worker that does not is the normal "
                    "half-deployment, and it is invisible to any check that reads only one side"
                )
            missing = [key for key in DEPLOYED_COMPONENT_KEYS if key not in entry]
            if missing:
                raise AssertionError(
                    f"the {component!r} deployed identity is missing {sorted(missing)}; without all of "
                    f"{list(DEPLOYED_COMPONENT_KEYS)} a running image cannot be tied back to reviewed "
                    "source"
                )
            for key in ("revision", "source_revision"):
                value = entry[key]
                if not isinstance(value, str) or not _GIT_REVISION_RE.match(value):
                    raise AssertionError(
                        f"the {component!r} {key} is {value!r}, which is not a full 40-character git SHA. "
                        "A branch name or short SHA names whatever that ref happened to point at, which "
                        "is the ambiguity this field exists to remove"
                    )
            digest = entry["image_digest"]
            if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
                raise AssertionError(
                    f"the {component!r} image digest is {digest!r}, which is not a sha256 digest. An "
                    "unparseable digest cannot identify a build, and a comparison between two malformed "
                    "values would succeed whenever they are equally malformed"
                )
            # The link that makes the digest mean something, and the one root's review
            # said was missing. Comparing `source_revision` to `revision` compares two
            # fields the same hand wrote, so it establishes only that the operator
            # typed one SHA twice. What has to agree is the archived output of the
            # system that performed the build.
            Driver._assert_build_record(component, entry)
            resolved[component] = entry

        digests = {name: entry["image_digest"] for name, entry in resolved.items()}
        if len(set(digests.values())) != len(digests):
            raise AssertionError(
                f"two components record the same image digest ({digests}). They are separate images built "
                "from separate Dockerfiles, so an equal pair means one recorded value was copied over the "
                "other — which would make a stale half-deployment pass every per-component comparison"
            )
        return resolved

    @staticmethod
    def _assert_raw_metadata(raw: object, *, subject: str, expected: tuple[str, ...]) -> dict:
        """Validate an archive of raw tool output, and return it keyed by document.

        The distinction this enforces is the whole of root's finding 3: a *summary* of
        what a tool said is written by the operator, whereas the tool's own output is
        written by the tool. Requiring the archive does not make forgery impossible —
        nothing available from outside the cluster can — but it moves the bar from
        "retype one SHA twice" to "hand-forge a set of self-consistent tool responses",
        and it leaves a reviewer something to re-retrieve and compare.

        Each document carries the ``command`` that produced it, so the retrieval is
        reproducible, a ``retrieved_at`` so a document carried over from an earlier
        evaluation is visible as one, and the ``body`` verbatim. An empty body is
        refused explicitly: a present-but-empty archive is the shape a placeholder
        takes, and it would otherwise satisfy a presence check.
        """
        if not isinstance(raw, dict):
            raise AssertionError(
                f"{subject}: 'raw' must be an object holding the archived {list(expected)} tool output, "
                f"got {raw!r}. Summary fields are written by the operator; the archived response is "
                "written by the tool, and only the second can corroborate the first"
            )
        absent = sorted(set(expected) - set(raw))
        if absent:
            raise AssertionError(
                f"{subject}: 'raw' archives no {absent} document(s). Each link in the provenance chain "
                f"needs the retrieval that established it, so all of {list(expected)} must be present"
            )
        for name in expected:
            document = raw[name]
            if not isinstance(document, dict):
                raise AssertionError(
                    f"{subject}: the archived {name!r} document must be an object carrying "
                    f"{list(RAW_METADATA_KEYS)}, got {document!r}"
                )
            missing = [key for key in RAW_METADATA_KEYS if not document.get(key)]
            if missing:
                raise AssertionError(
                    f"{subject}: the archived {name!r} document is missing or empty at {sorted(missing)}. "
                    f"'command' is how a reviewer re-retrieves it, 'retrieved_at' dates the retrieval, and "
                    "'body' is the response itself — an archive without the body is a claim that one exists"
                )
            if _parse_timestamp(document["retrieved_at"]) is None:
                raise AssertionError(
                    f"{subject}: the archived {name!r} document records retrieved_at "
                    f"{document['retrieved_at']!r}, which is not a parseable ISO-8601 instant. An undatable "
                    "retrieval cannot be told apart from one carried forward from an earlier evaluation"
                )
        return raw

    @staticmethod
    def _assert_build_record(component: str, entry: dict) -> None:
        """Tie a running image digest to the build that produced it from reviewed source.

        The chain, with every link PARSED out of an archived retrieval at its real
        field location rather than found as a substring of one:

            source archive --(the build consumed it)------> build, SUCCEEDED
            build ----------(its `docker push` printed)---> built digest, for a tag
            tag ------------(the registry serves it)------> running component

        Why parsing and not presence. The previous implementation dumped each archived
        body to JSON text and asked whether the expected values appeared anywhere in
        it. Root's review demonstrated that this establishes almost nothing, with three
        documents that all passed: a CI run whose every job had ``conclusion:
        failure``; a build whose own ``conclusion`` was ``failure``; and a build
        document replaced wholesale by ``{"unrelated_notes": "<run id> <revision>"}``.
        The third is the decisive one — a document that merely MENTIONS the right
        strings satisfied the check, so requiring the archive had bought nothing over
        requiring the summary. And ``built_digest`` was never compared against build
        output at all: it was checked against the running image and the registry, both
        of which the same operator also recorded.

        So each fact is now read from the field that holds it, by
        :func:`parse_codebuild_build`, :func:`parse_push_digest_from_build_log` and
        :func:`parse_ecr_images`; a build that did not reach ``SUCCEEDED`` is rejected
        on its status rather than on what it happens to mention; and the digest is the
        one the build's own push output reported for the tag the build was given.

        This is the deployed CodeBuild path specifically — see ``BUILD_RECORD_KEYS``.
        The archived bodies stay in the report verbatim, so a reviewer can re-run the
        three commands and compare.
        """
        record = entry["build_record"]
        subject = f"the {component!r} build_record"
        if not isinstance(record, dict):
            raise AssertionError(
                f"{subject} is {record!r}; it must be an object carrying {list(BUILD_RECORD_KEYS)}. A "
                "digest with no build behind it is a string of the right shape, which is what an "
                "invented one also is"
            )
        missing = [key for key in BUILD_RECORD_KEYS if not record.get(key)]
        if missing:
            raise AssertionError(
                f"{subject} is missing or empty at {sorted(missing)}. Without all of "
                f"{list(BUILD_RECORD_KEYS)} the running digest cannot be traced to a build of reviewed "
                "source: the build identity is what makes the build retrievable, the tag is what binds "
                "it to a registry entry, and the registry read is what establishes that this digest is "
                "the one actually being served"
            )
        for key in ("project", "build_id", "build_url", "image_tag", "repository"):
            if not isinstance(record[key], str):
                raise AssertionError(
                    f"{subject} records {key}={record[key]!r}; it must be a string identifying the build "
                    f"run. A non-string is not an identity anybody can look up — an arbitrary truthy "
                    "value satisfying a presence check is the defect this replaces"
                )
        if not record["build_url"].startswith("https://"):
            raise AssertionError(
                f"{subject} records build_url {record['build_url']!r}, which is not an https URL. The "
                "point of the field is that a reviewer can open the build and read it; a value that is "
                "not a location cannot be opened"
            )
        for key in ("built_digest", "registry_digest"):
            value = record[key]
            if not isinstance(value, str) or not _DIGEST_RE.match(value):
                raise AssertionError(
                    f"{subject} records {key}={value!r}, which is not a sha256 digest. An unparseable "
                    "digest cannot identify an image, and a comparison between two malformed values "
                    "succeeds whenever they are malformed in the same way"
                )
        built_revision = record["built_revision"]
        if not isinstance(built_revision, str) or not _GIT_REVISION_RE.match(built_revision):
            raise AssertionError(
                f"{subject} records built_revision {built_revision!r}, which is not a full 40-character "
                "git SHA. The revision a build consumed is the anchor of the whole chain, so a moving ref "
                "cannot stand in for it"
            )
        raw = Driver._assert_raw_metadata(
            record["raw"], subject=subject, expected=BUILD_RECORD_RAW_DOCUMENTS
        )

        # --- (1) the build: it exists, it is THIS build, and it SUCCEEDED -------
        try:
            build = parse_codebuild_build(raw["build"]["body"])
        except ProvenanceParseError as error:
            raise AssertionError(
                f"{subject}: the archived build document (retrieved by {raw['build']['command']!r}) is "
                f"not a usable `aws codebuild batch-get-builds` response: {error}"
            ) from error
        for label, recorded, parsed in (
            ("build_id", record["build_id"], build["build_id"]),
            ("built_revision", built_revision, build["source_revision"]),
            ("image_tag", record["image_tag"], build["image_tag"]),
        ):
            if recorded != parsed:
                raise AssertionError(
                    f"{subject} records {label} {recorded!r}, but the archived build document reports "
                    f"{parsed!r} (retrieved by {raw['build']['command']!r}). The summary is a restatement "
                    "of that response, so a disagreement means the summary describes a different build "
                    "than the one archived"
                )

        # --- (2) the digest, read out of the build's own push output ------------
        # The link root's review found entirely missing. Without it `built_digest` was
        # corroborated only by fields the same operator wrote.
        try:
            pushed = parse_push_digest_from_build_log(
                raw["build_log"]["body"], image_tag=build["image_tag"]
            )
        except ProvenanceParseError as error:
            raise AssertionError(
                f"{subject}: the archived build log (retrieved by {raw['build_log']['command']!r}) does "
                f"not establish the digest this build published: {error}"
            ) from error
        if pushed != record["built_digest"]:
            raise AssertionError(
                f"{subject} records built_digest {record['built_digest']!r}, but the archived build log "
                f"reports the build pushed tag {build['image_tag']!r} at {pushed!r}. The digest a build "
                "produced is stated by the build; a recorded value that disagrees with it was not read "
                "out of the build"
            )

        # --- (3) the registry: what that tag serves now -------------------------
        try:
            served = parse_ecr_images(raw["registry"]["body"])
        except ProvenanceParseError as error:
            raise AssertionError(
                f"{subject}: the archived registry document (retrieved by "
                f"{raw['registry']['command']!r}) is not a usable `aws ecr describe-images` response: "
                f"{error}"
            ) from error
        if served["digest"] != record["registry_digest"]:
            raise AssertionError(
                f"{subject} records registry_digest {record['registry_digest']!r}, but the archived "
                f"registry document reports {served['digest']!r}. The digest being served has to be read "
                "from the registry, not asserted alongside it"
            )
        if served["repository"] is not None and served["repository"] != record["repository"]:
            raise AssertionError(
                f"{subject} records repository {record['repository']!r}, but the archived registry "
                f"document describes {served['repository']!r}. A digest read from a different repository "
                "is a digest for a different image"
            )
        if served["tags"] and build["image_tag"] not in served["tags"]:
            raise AssertionError(
                f"{subject}: the build pushed tag {build['image_tag']!r}, but the archived registry "
                f"document reports the served image carrying tags {sorted(served['tags'])}. The tag is "
                "what binds the build to the registry entry, so an image that does not carry it is not "
                "the one this build published"
            )
        if record["built_digest"] != entry["image_digest"]:
            raise AssertionError(
                f"the {component!r} build produced digest {record['built_digest']!r} but the component is "
                f"recorded as running {entry['image_digest']!r}. A running image the build did not produce "
                "is an image built somewhere else, which is exactly what this chain exists to detect"
            )
        if record["registry_digest"] != entry["image_digest"]:
            raise AssertionError(
                f"the {component!r} registry reports digest {record['registry_digest']!r} for the image "
                f"being served, but the component is recorded as running {entry['image_digest']!r}. The "
                "registry is the side that cannot be retyped, so a disagreement means the recorded "
                "deployment is not the deployed one"
            )
        if built_revision != entry["source_revision"]:
            raise AssertionError(
                f"the {component!r} build run built source revision {built_revision!r}, but the component "
                f"records source_revision {entry['source_revision']!r}. The revision under review is the "
                "one the RUN consumed; the recorded field is a restatement of it and must agree"
            )
        if entry["source_revision"] != entry["revision"]:
            raise AssertionError(
                f"the {component!r} image was built from source revision "
                f"{entry['source_revision']!r} but the component is recorded as running "
                f"{entry['revision']!r}. A running image that does not trace to the revision under "
                "review is the stale-deployment case this pairing exists to catch"
            )

    def _assert_contained_in(
        self,
        revision: str,
        *,
        subject: str,
        deployed_revisions: dict,
        hint: str = "",
    ) -> None:
        """Assert a revision is contained in every deployed component — by asking git.

        Containment, not equality, is the correct relation between "a story merged
        here" and "this is what is running". A correct deployment is normally NEWER
        than a story's merge commit: it carries that story plus the later changes the
        fixture needs. Demanding equality therefore fails correct deployments, and —
        worse — pressures an operator into redeploying an old merge commit purely to
        satisfy the evaluator, which would make the evaluation the reason the
        environment is wrong.

        **The answer is computed, not read.** An earlier revision took the artifact's
        ``contained_in[component].is_ancestor`` at face value, which root's review
        named as an assertion presented as evidence: ``is_ancestor: true`` IS the
        conclusion this method exists to reach, so accepting it made the check restate
        its own subject and let any invented pair of well-formed SHAs pass. The commit
        graph in the harness's own checkout answers the question directly, with no
        credential and no network, so there is no reason to ask the operator for it.

        Because git also answers "does this commit exist at all", an invented revision
        now fails HERE rather than passing a syntax check. That is the
        internally-consistent-but-invented case, and it is the one the reproduction in
        the review exercised.

        A checkout that cannot answer — shallow, or without the deployed revision
        fetched — raises :class:`PrerequisiteMissingError` rather than failing. The
        harness could not look, which is a gap in the evidence-gathering environment
        rather than a defect in the deployment, and conflating the two would let a
        broken clone report a deployment defect that is not there.
        """
        for component, deployed in sorted(deployed_revisions.items()):
            ancestry = git_ancestry(
                revision, deployed, repo=self._repo_root, runner=self._git_runner
            )
            self._ancestry.append({"subject": subject, "component": component, **ancestry.to_evidence()})
            if not ancestry.available:
                raise PrerequisiteMissingError(
                    f"the harness could not establish whether {subject} is contained in the deployed "
                    f"{component!r} revision {deployed}: {ancestry.reason}. Containment is computed here "
                    "rather than read from the artifact, because the artifact's own 'is_ancestor' field is "
                    "the conclusion this check exists to reach"
                )
            if not ancestry.is_ancestor:
                raise AssertionError(
                    f"{subject} is not contained in the deployed {component!r} revision {deployed}: "
                    f"`git merge-base --is-ancestor` says no. The running build does not include it, so "
                    f"wave 2's checks would be observing a deployment that lacks it. {hint}".strip()
                )

    @staticmethod
    def _assert_creation_ledger(ledger: object) -> dict:
        """Validate the record of what the fixture actually CREATED, with identities.

        This is the half that makes teardown completeness checkable. Without it,
        removal evidence is a map whose keys the operator chose, so it can only
        confirm the resources it happens to mention and omitting a leaked workload
        passes. Reconciling against what was created is the only way "everything is
        gone" can be a claim about the fixture rather than about the list.

        Each entry carries the resource's own identity as observed AT CREATION — a
        Kubernetes UID, a queue URL, an ARN — not just its name. Root's fixture review
        established why: `kubectl apply` can adopt a pre-existing same-name object and
        `create-queue` can return an existing queue, so a name proves neither
        ownership nor, at teardown, that the thing removed was the thing created. An
        identity distinguishes "this exact object is gone" from "some object of this
        name is gone", which a recreated resource also satisfies.
        """
        if not isinstance(ledger, list) or not ledger:
            raise AssertionError(
                f"'creation_ledger' must be a nonempty list of the resources this fixture created, got "
                f"{ledger!r}. Teardown completeness is measured against it, and an empty ledger makes "
                "'everything was removed' vacuously true — indistinguishable from a fixture nobody "
                "inventoried"
            )
        by_identity: dict[str, dict] = {}
        for entry in ledger:
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"creation-ledger entry {entry!r} must be an object carrying {list(LEDGER_ENTRY_KEYS)}"
                )
            missing = [key for key in LEDGER_ENTRY_KEYS if not entry.get(key)]
            if missing:
                raise AssertionError(
                    f"creation-ledger entry {entry!r} is missing {sorted(missing)}. Without an observed "
                    "identity a resource cannot be distinguished from a same-named one that already "
                    "existed, so neither ownership nor its later removal is establishable"
                )
            if entry.get("created") is not True:
                raise AssertionError(
                    f"creation-ledger entry {entry.get('name')!r} is not recorded as created by this run "
                    f"({entry.get('created')!r}). A resource this fixture adopted rather than created is "
                    "not one it may delete — recording a pre-existing object here would authorize removing "
                    "someone else's"
                )
            identity = str(entry["identity"])
            if identity in by_identity:
                raise AssertionError(
                    f"creation-ledger identity {identity!r} appears twice ({by_identity[identity].get('name')!r} "
                    f"and {entry.get('name')!r}); identities must be unique or reconciliation cannot tell "
                    "which resource a removal observation refers to"
                )
            by_identity[identity] = entry
        return by_identity

    @staticmethod
    def _assert_listener_died_before_policies(
        ledger: dict, removals: dict
    ) -> None:
        """The fixture's listener must be gone BEFORE its network policies are.

        Both are ledger resources, so both must be absent at the end — that much the
        reconciliation above already establishes. This is about the ORDER, which the end
        state cannot show: deleting the NetworkPolicy first leaves a control-enabled
        workload running with its ingress restriction already removed. That window is
        worse than either end state, and DP-INV-1 is about exactly that interval rather
        than about the final snapshot.

        Ordering is asserted only between resources whose kinds are recognised, and a
        fixture with no policy in its ledger is not forced to have one: the fixture's
        shape is #3968's to define, and this check's job is to refuse a shape that is
        unsafe rather than to mandate one.
        """

        def kinds_of(names: tuple[str, ...]) -> list[tuple[str, dict]]:
            return [
                (identity, removals[identity])
                for identity, entry in ledger.items()
                if str(entry.get("kind", "")).lower() in names and identity in removals
            ]

        listeners = kinds_of(LISTENER_LEDGER_KINDS)
        policies = kinds_of(POLICY_LEDGER_KINDS)
        if not listeners or not policies:
            # Nothing to order. Not a pass for the ordering property so much as an
            # absence of the pair it constrains; completeness is the reconciliation's
            # job and it has already run.
            return
        for policy_identity, policy in policies:
            policy_removed = _parse_timestamp(policy.get("removed_at"))
            if policy_removed is None:
                raise AssertionError(
                    f"the fixture policy {ledger[policy_identity].get('name')!r} records "
                    f"removed_at={policy.get('removed_at')!r}, which is not a parseable instant, so it "
                    "cannot be shown to have been deleted AFTER the control-enabled workload it "
                    "restricted. Removing a policy while its workload is still running leaves a "
                    "control-enabled pod reachable without its ingress restriction"
                )
            for listener_identity, listener in listeners:
                listener_removed = _parse_timestamp(listener.get("removed_at"))
                if listener_removed is None:
                    raise AssertionError(
                        f"the fixture workload {ledger[listener_identity].get('name')!r} records "
                        f"removed_at={listener.get('removed_at')!r}, which is not a parseable instant, so "
                        "the order of its removal against the fixture's policies is unestablished — and "
                        "that order is what DP-INV-1 constrains"
                    )
                if policy_removed < listener_removed:
                    raise AssertionError(
                        f"the fixture policy {ledger[policy_identity].get('name')!r} was removed at "
                        f"{policy.get('removed_at')!r}, BEFORE the control-enabled workload "
                        f"{ledger[listener_identity].get('name')!r} was removed at "
                        f"{listener.get('removed_at')!r}. That leaves an interval in which a "
                        "control-enabled pod is running with its ingress restriction already deleted; the "
                        "workload has to die first"
                    )

    @staticmethod
    def _assert_native_interrupt_outcome(recorded: object) -> None:
        """A native interruption must have an OBSERVED non-aborted outcome.

        W2-06's claim is that a provider's own interrupted turn does not by itself
        become a deliberate ADP abort. That is a negative, and a negative cannot be
        established by the absence of a measurement — which is exactly what an earlier
        revision did: it tested only ``!= "aborted"``, so ``null``, ``""``, ``{}`` and
        ``"invented"`` every one of them PASSED. An operator who never ran the
        experiment, or whose collection script wrote an empty field, got a green check
        for a property nobody had observed.

        Three outcomes, deliberately distinct:

        * absent (``None``, ``""``, or no ``status``) → ``PrerequisiteMissingError``,
          i.e. ``not_run``. Nothing was measured, so there is nothing to judge.
        * present but not a recognised writer status → failure. Something was
          recorded and it is not an outcome this deployment could have produced.
        * exactly ``"aborted"`` → failure, unchanged: that IS the defect this check
          has always existed to catch.
        """
        # Absent measurement. A dict/list here means the collection script wrote a
        # structure where a status belongs, so the field was never populated either.
        if recorded is None or (isinstance(recorded, (str, dict, list)) and not recorded):
            raise PrerequisiteMissingError(
                "the harness-neutrality artifact records no outcome for the native-interruption "
                f"experiment ({recorded!r}). AC-A3's claim is that a provider's interrupted turn does "
                "NOT by itself become an ADP abort, which is a negative: it requires an OBSERVED "
                f"non-aborted outcome from {list(NATIVE_INTERRUPT_ALLOWED_STATUSES)}. An absent "
                "measurement cannot establish it, so this is NOT RUN rather than a pass"
            )
        # Provenance is required, so the experiment must be recorded as an object. A
        # bare status string — which is what the fixture carried while this check was
        # passing on unmeasured fields — says nothing about which run was interrupted
        # or how its outcome was read back, and those are what separate an observation
        # from an expectation somebody typed.
        if not isinstance(recorded, dict):
            raise AssertionError(
                f"the native interruption is recorded as {recorded!r}. It must be an object carrying "
                f"{list(NATIVE_INTERRUPT_KEYS)}: a bare status cannot say WHICH run was interrupted or "
                "HOW its outcome was read back, and an outcome with no provenance is indistinguishable "
                "from an expectation nobody measured"
            )
        missing = [key for key in NATIVE_INTERRUPT_KEYS if not recorded.get(key)]
        if missing:
            raise AssertionError(
                f"the native-interruption experiment is missing {sorted(missing)}. A bare outcome "
                "cannot say WHICH run was interrupted or HOW its status was read back, and without "
                "both the value is an expectation rather than an observation"
            )
        status = recorded["status"]
        if status == ABORTED_STATUS:
            raise AssertionError(
                "a native interrupted turn with no confirmed ADP abort finalization was recorded as "
                f"{ABORTED_STATUS!r}. Only a confirmed abort finalization may carry this status; "
                "pattern-matching a provider's interrupt string is the specific error forbidden here"
            )
        if not isinstance(status, str) or status not in NATIVE_INTERRUPT_ALLOWED_STATUSES:
            raise AssertionError(
                f"the native interruption is recorded as {status!r}, which is not one of the writer's "
                f"non-aborted terminal statuses {list(NATIVE_INTERRUPT_ALLOWED_STATUSES)}. A value the "
                "deployment could not have written is a typo or a fabrication, not a measured outcome — "
                "and accepting anything that merely is not 'aborted' is how an unmeasured field passed"
            )

    @staticmethod
    def _assert_fixture_identity(payload: dict, artifact: str, config: dict) -> str:
        """The artifact must describe THIS fixture, in THIS account and environment.

        Without it a complete, internally consistent artifact from a previous run
        against a different fixture is indistinguishable from this run's evidence —
        and a reused artifact is the easiest way to produce a green wave with no live
        observations behind it at all.
        """
        identity = payload["fixture_identity"]
        if not isinstance(identity, dict):
            raise AssertionError(
                f"{artifact}: 'fixture_identity' must be an object identifying the fixture these "
                f"observations were taken against, got {identity!r}"
            )
        for key in ("account_id", "environment", "run_id"):
            if not identity.get(key):
                raise AssertionError(
                    f"{artifact}: 'fixture_identity.{key}' is missing or empty; an observation that "
                    "cannot be tied to a specific fixture run cannot be distinguished from one carried "
                    "over from an earlier evaluation"
                )
        for key, expected in (
            ("account_id", str(config.get("account_id"))),
            ("environment", config.get("environment")),
        ):
            if str(identity.get(key)) != str(expected):
                raise AssertionError(
                    f"{artifact}: 'fixture_identity.{key}' is {identity.get(key)!r} but this run "
                    f"targets {expected!r}; the artifact describes a different fixture than the one under "
                    "evaluation"
                )
        return str(identity["run_id"])

    # ---- wave 4 shared helpers -----------------------------------------

    def _assert_prior_wave_accepted(
        self, wave: int, record: object, *, deployed_revisions: dict
    ) -> None:
        """One earlier wave's acceptance, as evidence rather than as a claim.

        Wave 4 is the last evaluation and its row says all four may close only with
        its evidence, so every earlier wave has to be accepted first. The failure this
        exists to prevent is the cheapest one available: an operator writing
        ``"accepted": true`` and the evaluator believing it.

        Four things are required, and each is a distinct way for a recorded acceptance
        to be false while looking true:

        1. **The evaluation it was accepted on.** Evidence attached to a different
           issue is not this wave's acceptance.
        2. **The counts.** A wave accepted at 9/10 is not accepted, and the word
           "accepted" alone cannot tell the two apart. Compared against the wave's
           OWN manifest length, so it tracks the manifest rather than a constant.
        3. **Cleanup.** An accepted wave whose fixture was left with the control
           listener enabled is the DP-INV-1 state; that environment is not a usable
           baseline for the next wave regardless of how its checks went.
        4. **Containment, computed.** The accepted revision must be an ancestor of
           every revision running now. This is the one that catches accepted-but-stale
           evidence, which is the subtle case: it was a TRUE observation, of a build
           this one has since changed. Asking git is what makes it evidence — a
           recorded ``compatible: true`` is the conclusion this method exists to
           reach.

        A wave with no manifest in this revision is a refusal, not a pass. Wave 3 is
        owned by another story and may legitimately not be registered yet; when it is
        not, wave 4 cannot be complete, and the honest way to say so is to name the
        missing manifest.
        """
        specs = WAVE_CHECKS.get(wave)
        if not specs:
            raise PrerequisiteMissingError(
                f"wave {wave} has no manifest in this revision, so its acceptance cannot be established "
                f"and wave 4 cannot consolidate it. Wave 4's evidence closes all four evaluations, so a "
                f"wave that does not yet exist as a manifest is a genuine blocker rather than a gap to "
                f"skip past. Registered waves: {sorted(WAVE_CHECKS)}"
            )
        if not isinstance(record, dict):
            raise AssertionError(
                f"'prior_waves.{wave}' must be an object recording that wave's acceptance, got {record!r}. "
                f"A bare boolean cannot carry the run, revision and counts that distinguish an acceptance "
                f"from an assertion of one"
            )
        missing = [key for key in PRIOR_WAVE_ACCEPTANCE_KEYS if key not in record]
        if missing:
            raise AssertionError(
                f"wave {wave}'s acceptance record is missing {sorted(missing)}; without all of "
                f"{list(PRIOR_WAVE_ACCEPTANCE_KEYS)} the record cannot be checked against the evaluation "
                f"that produced it"
            )
        if record.get("accepted") is not True:
            raise AssertionError(
                f"wave {wave} is not recorded as accepted ({record.get('accepted')!r}). Wave 4 "
                f"consolidates every earlier wave's criteria, so an unaccepted prior wave means this "
                f"wave's consolidation has nothing established to rest on"
            )
        expected_evaluation = WAVE_EVALUATIONS.get(wave)
        if expected_evaluation and str(record.get("evaluation") or "") != expected_evaluation:
            raise AssertionError(
                f"wave {wave}'s acceptance names evaluation {record.get('evaluation')!r}, expected "
                f"{expected_evaluation!r}. Evidence attached to a different evaluation is not this wave's "
                f"acceptance"
            )
        if not record.get("run_id"):
            raise AssertionError(
                f"wave {wave}'s acceptance records no 'run_id'. A revision says which build was "
                f"evaluated; the run identity is what lets a reviewer retrieve that report and confirm "
                f"this summary of it rather than take it on trust"
            )
        passed, required = record.get("passed"), record.get("required")
        if required != len(specs) or passed != required:
            raise AssertionError(
                f"wave {wave} is recorded as {passed!r}/{required!r}, but its manifest has "
                f"{len(specs)} checks and acceptance means all of them passed. A partial prior wave is "
                f"not an accepted baseline, and 'accepted' alone cannot distinguish the two"
            )
        if record.get("cleanup_ok") is not True:
            raise AssertionError(
                f"wave {wave}'s recorded run did not complete cleanup. An accepted wave whose fixture was "
                f"left with the control listener enabled is the state DP-INV-1 forbids, so that "
                f"environment is not a baseline this wave may build on"
            )
        revision = record.get("revision")
        if not isinstance(revision, str) or not _GIT_REVISION_RE.match(revision):
            raise AssertionError(
                f"wave {wave}'s accepted revision is {revision!r}, which is not a full 40-character git "
                f"SHA. A branch name or short SHA names whatever that ref happened to point at, which is "
                f"the ambiguity this field exists to remove"
            )
        # Compatibility as containment, computed here. See `_assert_contained_in`:
        # the artifact's own compatibility flag is the conclusion under test.
        self._assert_contained_in(
            revision,
            subject=f"wave {wave}'s accepted revision {revision}",
            deployed_revisions=deployed_revisions,
            hint=(
                "Accepted-but-stale prior-wave evidence is the case to watch: it was a true observation "
                "of a build this one has since changed, so it cannot carry forward unchecked"
            ),
        )

    def _surface_last_modified(self, path: str) -> datetime | None:
        """When a source surface was last changed, from the commit graph.

        First-hand, like the ancestry answers, and for the same reason: "has this
        criterion gone stale?" is answerable from the repository the harness is
        already running out of, so asking the operator for it would be asking them to
        supply the conclusion.

        ``None`` means git could not answer — a shallow clone, a path that does not
        exist at HEAD, no git at all. The caller turns that into ``not_run`` rather
        than into a pass: an unanswerable staleness question is a gap in the
        evidence-gathering environment, and treating it as "not stale" would let a
        broken checkout certify evidence it never checked.
        """
        runner = self._git_runner or _default_git_runner
        argv = [
            "git",
            "-C",
            str(self._repo_root),
            "log",
            "-1",
            "--format=%cI",
            "--",
            path,
        ]
        try:
            result = runner(argv)
        except Exception:  # noqa: BLE001 - an unavailable git is not_run, never a pass
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return _parse_timestamp((getattr(result, "stdout", "") or "").strip())

    def _assert_evidence_not_stale(
        self, payload: dict, check_id: str, *, deployed_revisions: dict
    ) -> None:
        """A consolidated criterion's evidence must postdate the code it describes.

        This is what makes "rerun any stale/touched criterion" a check rather than an
        instruction. The evidence for, say, the abort family was taken at some
        revision and some instant; if a surface those criteria cover has been modified
        since, the evidence describes code that is no longer running. It was a true
        observation and it is now the wrong one, which is exactly the case that a
        status field reading ``passed`` cannot distinguish.

        Two independent conditions, because either alone is insufficient:

        * The evidence's revision must be contained in what is deployed. Evidence from
          a branch that was never merged describes a build nobody is running.
        * The evidence's timestamp must be at or after the last modification of every
          surface it covers. A revision can be an ancestor of the deployment and still
          predate a later change to the very file the criterion is about.
        """
        source = WAVE4_CONSOLIDATED_SOURCES[check_id]
        recorded_wave = payload.get("wave")
        if recorded_wave != source["wave"]:
            raise AssertionError(
                f"{check_id}: this evidence records wave {recorded_wave!r} but the criteria it carries "
                f"were evidenced by wave {source['wave']}. Evidence filed under the wrong wave cannot be "
                f"reconciled against that wave's acceptance"
            )
        revision = payload.get("evidenced_revision")
        if not isinstance(revision, str) or not _GIT_REVISION_RE.match(revision):
            raise AssertionError(
                f"{check_id}: 'evidenced_revision' is {revision!r}, which is not a full 40-character git "
                f"SHA. Which build these observations were made against is part of the observation"
            )
        self._assert_contained_in(
            revision,
            subject=f"{check_id}'s evidence revision {revision}",
            deployed_revisions=deployed_revisions,
            hint="Evidence from a revision the deployment does not contain describes a build nobody runs.",
        )
        evidenced_at = _parse_timestamp(payload.get("evidenced_at"))
        if evidenced_at is None:
            raise AssertionError(
                f"{check_id}: 'evidenced_at' is {payload.get('evidenced_at')!r}, which is not an ISO-8601 "
                f"instant. Without an orderable time the evidence cannot be shown to postdate the code it "
                f"describes, so staleness is unanswerable"
            )
        for path in source["surfaces"]:
            changed = self._surface_last_modified(path)
            if changed is None:
                raise PrerequisiteMissingError(
                    f"{check_id}: the harness could not read the last modification of {path} from the "
                    f"commit graph, so it cannot establish whether this evidence has gone stale. Fetch "
                    f"full history and re-run — an unanswerable staleness question is not a pass"
                )
            self._ancestry.append(
                {
                    "subject": f"{check_id} staleness vs {path}",
                    "surface": path,
                    "surface_last_modified": changed.isoformat(),
                    "evidenced_at": evidenced_at.isoformat(),
                    "stale": evidenced_at < changed,
                }
            )
            if evidenced_at < changed:
                raise AssertionError(
                    f"{check_id}: the evidence was taken at {evidenced_at.isoformat()} but {path} was "
                    f"last modified at {changed.isoformat()}. The criteria this check consolidates cover "
                    f"that surface, so the evidence describes code that has since changed and must be "
                    f"rerun. Stale-but-passing is the failure mode here: it was a true observation of a "
                    f"build that is no longer deployed"
                )

    def _assert_criteria_cover(
        self, payload: dict, check_id: str, expected: tuple[str, ...]
    ) -> dict:
        """The per-criterion map must cover exactly this check's acceptance IDs.

        Exactly, in both directions, and the two failures are different:

        * A MISSING ID is a criterion nobody evidenced, which is the whole thing the
          evaluation is counting.
        * An UNKNOWN ID is evidence filed against a criterion this check does not
          carry. That sounds harmless and is not: it inflates the apparent coverage
          of the index W4-10 reconciles, so an unknown ID can hide a missing one.

        Each entry must also carry a status and nonempty evidence. A status string
        with no evidence behind it is exactly what the evaluation's "a status string
        without the specified evidence is insufficient" sentence refuses.
        """
        criteria = payload.get("criteria")
        if not isinstance(criteria, dict):
            raise AssertionError(
                f"{check_id}: 'criteria' must be an object keyed by acceptance ID, got {criteria!r}"
            )
        recorded = {str(key) for key in criteria}
        wanted = set(expected)
        missing = sorted(wanted - recorded)
        unknown = sorted(recorded - wanted)
        if missing:
            raise AssertionError(
                f"{check_id}: no evidence recorded for {missing}. These are criteria this check carries, "
                f"so an absent entry is an unevidenced criterion rather than an omission from a summary"
            )
        if unknown:
            raise AssertionError(
                f"{check_id}: evidence recorded for {unknown}, which this check does not carry. Evidence "
                f"filed against the wrong criterion inflates apparent coverage, so an unknown ID can "
                f"conceal a missing one"
            )
        for acceptance_id in expected:
            entry = criteria[acceptance_id]
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"{check_id}: criterion {acceptance_id} is {entry!r}; it must be an object carrying a "
                    f"status and the evidence behind it. A bare boolean is a verdict where an observation "
                    f"belongs"
                )
            if entry.get("status") != STATUS_PASSED:
                raise AssertionError(
                    f"{check_id}: criterion {acceptance_id} is {entry.get('status')!r}, not "
                    f"{STATUS_PASSED!r}. Wave 4 may only close with every criterion passing, so anything "
                    f"else — including not_run — keeps this check failed"
                )
            evidence = entry.get("evidence")
            if not evidence or (isinstance(evidence, (list, str)) and len(evidence) == 0):
                raise AssertionError(
                    f"{check_id}: criterion {acceptance_id} records status {STATUS_PASSED!r} with no "
                    f"evidence. A status string without the observation behind it is the substitute for "
                    f"evidence this evaluation exists to refuse"
                )
        return criteria

    @staticmethod
    def _assert_named_proofs(payload: dict, check_id: str, keys: tuple[str, ...]) -> None:
        """Each named proof in a row must be an observed ``True``.

        Named individually rather than rolled into one flag because the evaluation
        table names them individually. The row for steering, for instance, demands
        FIFO order, retry delivery, the pending cap and the SDK-bound text; a single
        ``steering_proven`` boolean cannot say which of the four was never made, and
        those have different owners.

        ``True`` exactly, not truthy: a string like ``"yes"`` or a nonzero count would
        satisfy a truthiness test while recording something other than the observation
        asked for.
        """
        for key in keys:
            if payload.get(key) is not True:
                raise AssertionError(
                    f"{check_id}: '{key}' is {payload.get(key)!r}, expected an observed True. This is one "
                    f"of the proofs the wave-4 row names separately, so it cannot be satisfied by another "
                    f"proof in the same artifact passing"
                )

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

    # ---- W2-01 ---------------------------------------------------------

    def check_w2_01(self, *, emitted_ids: tuple[str, ...] = ()) -> None:
        """The consolidated wave-2 preflight: does this evidence describe the build under review?

        Every other wave-2 check reports on a deployment. This one establishes that
        the deployment is the one the reviewer thinks it is — which is why it is
        `Gate/regression` rather than an AC: if this is wrong, the other nine
        checks are *correct observations of the wrong thing*, which reads exactly
        like a passing wave.

        Four families of observation, and each is a distinct way for that to go
        wrong:

        1. **Prior-wave compatibility.** Wave 2 extends wave 1's contract rather
           than replacing it, so wave 1 must have been accepted — identified by the
           run and revision it was accepted on, and recorded as contained in what is
           deployed now. An accepted wave 1 from before a breaking change is not
           evidence about this build, and a bare ``compatible: true`` is an assertion
           of that rather than evidence for it.
        2. **Deployed identity, containment and gates.** Each component records the
           revision it RUNS, its image digest, and the source revision that image was
           built from. Each required story then records the gates that tested it and
           the deployed revisions it is contained in. Containment is the right
           relation and equality is not: a correct deployment is normally NEWER than
           a story's merge commit, carrying that story plus later changes the fixture
           needs, so demanding equality would reject correct deployments and
           implicitly demand redeploying an old merge to satisfy the evaluator.
        3. **Gates by name, on the tested revision.** The required gates are compared
           against the names CI actually defines, each carrying the run it came from
           and the revision it tested. An arbitrary nonempty map of "passed" values
           demonstrates the operator's spelling, not the build's gates.
        4. **Fixture scope, ownership and teardown readiness.** The flag is on in the
           disposable fixture and nowhere shared, ordinary flags are off, every one
           of the wave's ten check IDs is present, cleanup is configured BEFORE
           anything is seeded, and every created resource is in the ledger with its
           own observed identity — so teardown can later be reconciled against what
           was actually created rather than against what someone remembered to list.

        Each is asserted individually with its own message. A rolled-up verdict
        would tell the operator that preflight failed without telling them which of
        four different owners has to fix it.
        """
        preflight = self._artifact("wave2_preflight")

        # --- (0) what is actually deployed, per component ----------------------
        # Parsed first because everything downstream is relative to it: story
        # containment, gate subjects, and the security capture's binding all have to
        # name the revisions running RIGHT NOW. Recording them once here is what
        # keeps "deployed" a single fact rather than a phrase each section
        # interprets for itself.
        deployed = self._deployed_components(preflight)
        deployed_revisions = {name: entry["revision"] for name, entry in deployed.items()}

        # --- (1) accepted wave 1 evidence, and its compatibility --------------
        wave1 = preflight["wave1_evidence"]
        if not isinstance(wave1, dict):
            raise AssertionError(
                f"'wave1_evidence' must be an object describing the accepted wave-1 run, got {wave1!r}"
            )
        if wave1.get("accepted") is not True:
            raise AssertionError(
                f"wave 1 is not recorded as accepted ({wave1.get('accepted')!r}). Wave 2 extends wave 1's "
                f"contract rather than replacing it, so evaluation #{WAVE_EVALUATIONS[1]}'s acceptance is a "
                "prerequisite: without it there is no established baseline for these checks to be an "
                "increment on"
            )
        if str(wave1.get("evaluation") or "") != WAVE_EVALUATIONS[1]:
            raise AssertionError(
                f"'wave1_evidence.evaluation' is {wave1.get('evaluation')!r}, expected "
                f"{WAVE_EVALUATIONS[1]!r}; evidence attached to a different evaluation is not wave 1's "
                "acceptance"
            )
        # The counts, not just the word "accepted". #3967 accepted wave 1 at 10/10,
        # and a recorded acceptance with nine passes is either a different run or a
        # misremembered one — both of which make this baseline claim false.
        passed, required = wave1.get("passed"), wave1.get("required")
        if required != len(WAVE1_CHECKS) or passed != required:
            raise AssertionError(
                f"wave 1 is recorded as {passed!r}/{required!r}, but its acceptance was "
                f"{len(WAVE1_CHECKS)}/{len(WAVE1_CHECKS)}. A partial wave-1 result is not an accepted "
                "baseline, and 'accepted' alone cannot distinguish the two"
            )
        if wave1.get("cleanup_ok") is not True:
            raise AssertionError(
                "wave 1's recorded run did not complete cleanup; an accepted wave whose fixture was left "
                "enabled is the DP-INV-1 state, and reusing that environment is what this catches"
            )
        wave1_revision = wave1.get("revision")
        if not isinstance(wave1_revision, str) or not _GIT_REVISION_RE.match(wave1_revision):
            raise AssertionError(
                f"wave 1's accepted revision is {wave1_revision!r}, which is not a full 40-character git "
                "SHA. A branch name or short SHA does not identify a build: it names whatever that ref "
                "happened to point at, which is the ambiguity this field exists to remove"
            )
        # Identity, not just a revision: the run that produced wave 1's report, so a
        # reviewer can go and read that report rather than take this record's word
        # for what it said.
        if not wave1.get("run_id"):
            raise AssertionError(
                "wave 1's evidence records no 'run_id' identifying the accepted evaluation run. A "
                "revision says which build was evaluated; the run identity is what lets a reviewer "
                "retrieve the report and confirm this summary of it"
            )
        # Compatibility as CONTAINMENT rather than as a claim. An earlier revision
        # accepted `compatible_with_current_revision: true`, which is the conclusion
        # this check is supposed to reach — asserting it is not evidence for it.
        # Wave 1's accepted revision must be an ancestor of every deployed component,
        # which is exactly what "this build still contains what wave 1 accepted"
        # means and is checkable from recorded ancestry.
        self._assert_contained_in(
            wave1_revision,
            subject=f"wave 1's accepted revision {wave1_revision}",
            deployed_revisions=deployed_revisions,
            hint=(
                "Accepted-but-stale prior-wave evidence is the subtle case: it was a true observation of "
                "a build this one has since changed, so it cannot carry forward unchecked"
            ),
        )

        # --- (2) merged revisions, versions and CI ----------------------------
        merged = preflight["merged_revisions"]
        if not isinstance(merged, dict):
            raise AssertionError(f"'merged_revisions' must be an object keyed by story, got {merged!r}")
        for story, description in WAVE2_REQUIRED_STORIES.items():
            entry = merged.get(story)
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"no merged revision recorded for {story} ({description}); wave 2's checks span all "
                    f"{len(WAVE2_REQUIRED_STORIES)} stories, so evidence gathered while one is unmerged "
                    "describes a build that does not implement the wave"
                )
            if entry.get("merged") is not True:
                raise AssertionError(
                    f"{story} ({description}) is not recorded as merged: {entry.get('merged')!r}"
                )
            revision = entry.get("revision")
            if not isinstance(revision, str) or not _GIT_REVISION_RE.match(revision):
                raise AssertionError(
                    f"{story}'s merged revision is {revision!r}, which is not a full 40-character git "
                    f"SHA. {description} must be pinned to an exact commit, not to a moving ref"
                )
            # Merged is necessary and not sufficient. What matters for this evaluation
            # is whether the story is IN what is running — and a merge commit that
            # predates the deployment is the normal, correct case, so this is an
            # ancestry question rather than an equality one.
            self._assert_contained_in(
                revision,
                subject=f"{story}'s merged revision {revision} ({description})",
                deployed_revisions=deployed_revisions,
                hint="A merged story that is not in the running build is not deployed.",
            )

        if preflight["protocol_version"] != CONTROL_PROTOCOL_VERSION:
            raise AssertionError(
                f"the preflight records control protocol version {preflight['protocol_version']!r}, but "
                f"this harness verifies version {CONTROL_PROTOCOL_VERSION}. The gateway peer, the adapter "
                "and this harness must move together; a mismatch means one of the three describes a "
                "different contract than the other two"
            )
        if preflight["adapter_id"] != CLAUDE_ADAPTER_ID:
            raise AssertionError(
                f"the preflight records adapter {preflight['adapter_id']!r}, expected "
                f"{CLAUDE_ADAPTER_ID!r}: Claude is the first production adapter in this wave"
            )
        if preflight["sdk_version"] != EXPECTED_CLAUDE_SDK_VERSION:
            raise AssertionError(
                f"the preflight records SDK {preflight['sdk_version']!r}, but the lockfile pins "
                f"{EXPECTED_CLAUDE_SDK_VERSION!r}. The streaming-input and shouldQuery behaviours this "
                "wave relies on are observed SDK behaviour rather than documented guarantees, so evidence "
                "from a different version does not carry over"
            )
        packages = preflight["package_versions"]
        if not isinstance(packages, dict):
            raise AssertionError(f"'package_versions' must be an object, got {packages!r}")
        for name in WAVE2_REQUIRED_PACKAGES:
            version = packages.get(name)
            if not isinstance(version, str) or not version.strip():
                raise AssertionError(
                    f"no version recorded for the {name!r} package ({version!r}). The control path spans "
                    "two runtimes, and an unrecorded version on either side is a contract nobody pinned"
                )

        # --- (3) the required gates, by name, on the revision they tested -------
        # Every gate this wave depends on, named as CI names it. An earlier revision
        # accepted any nonempty map whose values were all "passed", so
        # `{"anything": "passed"}` demonstrated a green build — a check on the
        # operator's spelling rather than on the gates. Requiring these exact names
        # makes a missing gate a NAMED failure, and each gate must carry the run it
        # came from and the revision it tested, because a green run of unknown subject
        # is not evidence about this build.
        gates = preflight["ci_gates"]
        if not isinstance(gates, dict):
            raise AssertionError(
                f"'ci_gates' must be an object keyed by the CI job name, got {gates!r}"
            )
        absent_gates = sorted(set(WAVE2_REQUIRED_CI_GATES) - set(gates))
        if absent_gates:
            raise AssertionError(
                f"no result recorded for the required CI gate(s) {absent_gates}. The required set is "
                f"{list(WAVE2_REQUIRED_CI_GATES)}, named as `.github/workflows/agent-control-ci.yml` names "
                "them; an unrecorded gate is one nobody confirmed ran, and a gate absent from the record "
                "is indistinguishable from a gate that was never required"
            )
        for gate in WAVE2_REQUIRED_CI_GATES:
            entry = gates[gate]
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"the CI gate {gate!r} is recorded as {entry!r}; it must be an object carrying "
                    f"{list(CI_GATE_KEYS)} so the result can be traced to a run and a tested revision"
                )
            missing = [key for key in CI_GATE_KEYS if not entry.get(key)]
            if missing:
                raise AssertionError(
                    f"the CI gate {gate!r} is missing {sorted(missing)}. A bare pass/fail cannot say WHICH "
                    "run produced it or WHAT revision it tested, and both are required for it to be "
                    "evidence about this deployment"
                )
            if entry["status"] != STATUS_PASSED:
                raise AssertionError(
                    f"the required CI gate {gate!r} is recorded as {entry['status']!r} rather than "
                    f"{STATUS_PASSED!r} (run {entry['run_id']!r}); a merge with red required checks is a "
                    "merge, not a verified revision"
                )
            tested = entry["tested_revision"]
            if not isinstance(tested, str) or not _GIT_REVISION_RE.match(tested):
                raise AssertionError(
                    f"the CI gate {gate!r} records tested revision {tested!r}, which is not a full "
                    "40-character git SHA; the subject of the gate is unidentifiable"
                )
            # The gate must have tested something that is actually deployed. A green
            # gate on an unrelated revision is the case this catches: true, and about
            # a build nobody is evaluating.
            if tested not in set(deployed_revisions.values()):
                raise AssertionError(
                    f"the CI gate {gate!r} tested revision {tested!r}, which is not any deployed component "
                    f"revision ({sorted(set(deployed_revisions.values()))}). A gate that passed on a "
                    "different build is not a gate on this deployment"
                )
            # The archive, and the three fields that have to be readable out of it.
            # Root's review: an arbitrary truthy `run_id` was accepted, so `run_id:
            # true` demonstrated a gate. A run document that mentions neither the run
            # nor the revision nor the job is not a document about this gate, and the
            # `gh run view --json` output that a real collection step produces mentions
            # all three.
            for key in ("run_id", "run_url"):
                if not isinstance(entry[key], str):
                    raise AssertionError(
                        f"the CI gate {gate!r} records {key}={entry[key]!r}; it must be a string "
                        f"identifying the run. An arbitrary truthy value passes a presence check without "
                        "naming anything a reviewer can retrieve"
                    )
            if not entry["run_url"].startswith("https://"):
                raise AssertionError(
                    f"the CI gate {gate!r} records run_url {entry['run_url']!r}, which is not an https "
                    "URL. A gate is evidence only if the run behind it can be opened and read"
                )
            raw = self._assert_raw_metadata(
                entry["raw"],
                subject=f"the CI gate {gate!r}",
                expected=CI_GATE_RAW_DOCUMENTS,
            )
            # Parsed, not searched. Root's reproduction (a) archived a run document in
            # which the run AND every job had `conclusion: failure`, and the gate
            # passed, because a substring search over the dumped body finds the run id,
            # the revision and the job name in a red document exactly as readily as in
            # a green one. `parse_github_run` reads the fields at their locations and
            # requires this NAMED job to have concluded `success`.
            try:
                run = parse_github_run(raw["run"]["body"], required_job=gate)
            except ProvenanceParseError as error:
                raise AssertionError(
                    f"the CI gate {gate!r}: the archived run document (retrieved by "
                    f"{raw['run']['command']!r}) does not establish a passing run of that job: {error}"
                ) from error
            if run["run_id"] != str(entry["run_id"]):
                raise AssertionError(
                    f"the CI gate {gate!r} records run_id {entry['run_id']!r}, but the archived run "
                    f"document reports databaseId {run['run_id']!r}. The recorded field is a summary of "
                    "that response, so a disagreement means the summary describes a different run than "
                    "the one archived"
                )
            # Which revision the run TESTED — read from the job's own uploaded
            # artifact, never from `headSha` and never from a field the operator added
            # to the run response. On a `workflow_dispatch` run `headSha` names the ref
            # the WORKFLOW file was loaded from, so treating it as the tested source
            # would label the workflow ref as the thing under test; root flagged both
            # that and the operator-appended override that replaced it.
            job_id = CI_GATE_JOB_IDS[gate]
            try:
                checkout = parse_checkout_artifact(
                    raw["checkout"]["body"], expected_job=job_id
                )
            except ProvenanceParseError as error:
                raise AssertionError(
                    f"the CI gate {gate!r}: the archived checkout artifact (retrieved by "
                    f"{raw['checkout']['command']!r}) does not establish which revision the job checked "
                    f"out: {error}"
                ) from error
            # Bound to the SAME run and the same attempt as the archived run document.
            # Without this an artifact from any other run of the same job — including a
            # green run of an entirely different revision — would serve as this gate's
            # checkout.
            if checkout["run_id"] != run["run_id"]:
                raise AssertionError(
                    f"the CI gate {gate!r} archives a checkout artifact from run "
                    f"{checkout['run_id']!r}, but the archived run document is run {run['run_id']!r}. The "
                    "artifact is evidence about the run that produced it, so one from a different run says "
                    "nothing about this gate"
                )
            # `attempt` and `event` are REQUIRED rather than skipped when absent. Both
            # comparisons below used to be conditional on the run document carrying the
            # field, which held a document that said less to a weaker standard: omitting
            # `attempt` skipped the attempt binding entirely, and omitting `event`
            # skipped the trigger binding that decides which head rule applies. The
            # collector asks for both (`gh run view --json ...,attempt,event`), so an
            # absent one is an incomplete archive, not a licence to check less.
            for run_field in ("attempt", "event"):
                if run[run_field] is None:
                    raise AssertionError(
                        f"the CI gate {gate!r}: the archived run document records no {run_field!r}. It is "
                        f"needed to bind the checkout artifact to this run — a missing value would "
                        f"otherwise SKIP that binding rather than fail it, so a less complete document "
                        f"would be judged less strictly. Archive the response of `gh run view <id> --json "
                        f"databaseId,headSha,attempt,event,conclusion,jobs`, which returns it"
                    )
            if checkout["run_attempt"] != str(run["attempt"]):
                raise AssertionError(
                    f"the CI gate {gate!r} archives a checkout artifact from attempt "
                    f"{checkout['run_attempt']!r} of run {run['run_id']!r}, but the archived run document "
                    f"reports attempt {run['attempt']!r}. A re-run checks out afresh, so the attempt that "
                    "produced the evidence must be the attempt being reported"
                )
            if checkout["event_name"] != run["event"]:
                raise AssertionError(
                    f"the CI gate {gate!r} archives a checkout artifact recording event "
                    f"{checkout['event_name']!r}, but the archived run document reports "
                    f"{run['event']!r}. The two describe the same run and cannot disagree about how it "
                    "was triggered"
                )
            actual = checkout["checked_out_revision"]
            if actual != tested:
                raise AssertionError(
                    f"the CI gate {gate!r} records tested_revision {tested!r}, but the job's own checkout "
                    f"artifact says it checked out {actual!r}. A gate is evidence about the revision the "
                    "jobs actually built, so the recorded subject must be that revision"
                )
            # How the run's head relates to what the job checked out depends on the
            # TRIGGER, and the three cases are genuinely different. An earlier revision
            # applied one rule — equality for everything that is not a manual dispatch —
            # which rejected the honest `pull_request` case root reproduced with the real
            # artifact of run 35956224007: API `headSha` 1166f1d9 (the branch tip), the
            # job's own artifact 92d6cb4c (the merge commit it actually built). Both
            # documents were authentic and unmodified, and the gate failed. A rule that
            # refuses genuine evidence is as harmful as one that accepts invented
            # evidence, because the way around it is to make the documents agree by hand.
            if checkout["event_name"] == "pull_request":
                # GitHub does not build the branch as pushed: it builds a temporary merge
                # of the branch into its base, and THAT is what `actions/checkout` gives
                # the job and what the tests ran against. So the checkout is expected to
                # differ from the head — but not arbitrarily. A merge of this branch
                # CONTAINS this branch, which is a real relation the harness can verify
                # first-hand from the commit graph in its own checkout, with no
                # credential and no network. Containment accepts the merge commit and
                # still rejects an artifact naming an unrelated revision.
                ancestry = git_ancestry(
                    run["head_revision"], actual, repo=self._repo_root, runner=self._git_runner
                )
                self._ancestry.append(
                    {"subject": f"the CI gate {gate!r} head", "component": "ci_checkout", **ancestry.to_evidence()}
                )
                if not ancestry.available:
                    raise PrerequisiteMissingError(
                        f"the CI gate {gate!r}: the harness could not establish whether the run's head "
                        f"{run['head_revision']} is contained in the revision its job checked out "
                        f"{actual}: {ancestry.reason}. On a pull_request run the job builds a merge "
                        "commit, so containment is the relation to check — and a checkout that cannot "
                        "answer is an unrun check, never a pass"
                    )
                if not ancestry.is_ancestor:
                    raise AssertionError(
                        f"the CI gate {gate!r} archives a pull_request run whose head is "
                        f"{run['head_revision']!r}, but its job checked out {actual!r}, which does not "
                        "contain that head. A pull_request job builds a merge of the head into its base, "
                        "and such a merge contains the head; a revision that does not is not this run's "
                        "checkout"
                    )
                # On a pull_request run `GITHUB_SHA` is that same merge commit, so the
                # step's own `workflow_ref_sha` must equal what it checked out. Checked
                # for the same reason as the manual case below: the field was parsed and
                # then discarded, so an artifact could name any workflow ref at all.
                if checkout["workflow_ref_sha"] != actual:
                    raise AssertionError(
                        f"the CI gate {gate!r} archives a pull_request run whose job checked out "
                        f"{actual!r} but recorded workflow_ref_sha {checkout['workflow_ref_sha']!r}. On a "
                        "pull_request run GITHUB_SHA is the merge commit the job built, so the two are "
                        "the same value and a disagreement means the artifact is not this job's record"
                    )
            elif checkout["event_name"] == "workflow_dispatch":
                # On a manual run `headSha` names the ref the WORKFLOW FILE was loaded
                # from, which is exactly what the step records as `workflow_ref_sha`. So
                # the two are checkable against each other — this is the binding that was
                # parsed, shape-validated and then discarded, which let an artifact name
                # any workflow ref at all. The tested revision still comes only from
                # `checked_out_revision`.
                if checkout["workflow_ref_sha"] != run["head_revision"]:
                    raise AssertionError(
                        f"the CI gate {gate!r} archives a workflow_dispatch run whose head is "
                        f"{run['head_revision']!r}, but the job recorded workflow_ref_sha "
                        f"{checkout['workflow_ref_sha']!r}. On a manual run those are the same value — "
                        "the ref the workflow file was loaded from — so a disagreement means the run "
                        "document and the artifact describe different runs"
                    )
            elif actual != run["head_revision"]:
                # Every other trigger (`push`, `schedule`, …) checks out the run's head
                # directly, so equality is the correct relation and is kept.
                raise AssertionError(
                    f"the CI gate {gate!r} archives a {checkout['event_name']!r} run whose head is "
                    f"{run['head_revision']!r} but whose job checked out {actual!r}. On that trigger the "
                    "job checks out the run's head, so a mismatch means the run document and the "
                    "artifact describe different runs"
                )

        # --- (4) fixture scope, check inventory and teardown readiness ---------
        # This artifact must describe THIS fixture. Checked here rather than left
        # implicit because every observation above is otherwise satisfiable by a
        # complete, internally consistent record from a previous run.
        self._assert_fixture_identity(preflight, "wave2_preflight", self.config)
        self._assert_creation_ledger(preflight["creation_ledger"])
        if preflight["isolation_before_listener"] is not True:
            raise AssertionError(
                "isolation was not recorded as existing before listener start; DP-INV-1 requires the "
                "ingress policy to be applied BEFORE any enabled listener, and the reverse order means "
                "the fixture was briefly reachable while enabled"
            )
        if preflight["ordinary_flags_off"] is not True:
            raise AssertionError(
                "ordinary gateway/worker/SPA control flags were not recorded as false; the flag may be "
                "enabled only in the isolated fixture (DP-INV-1)"
            )
        scope = preflight["fixture_only_flag_scope"]
        if not isinstance(scope, dict):
            raise AssertionError(f"'fixture_only_flag_scope' must be an object, got {scope!r}")
        if scope.get("enabled_in_fixture") is not True:
            raise AssertionError(
                "the control flag is not recorded as enabled in the fixture; wave 2's checks are about an "
                "enabled control path, so a flag-off fixture cannot produce this wave's evidence"
            )
        # The other direction, and the one that matters for DP-INV-1: enabled
        # NOWHERE ELSE. A count is required rather than a boolean, because
        # "enabled_elsewhere: false" is the claim and an enumerated empty list is
        # the evidence for it.
        elsewhere = scope.get("enabled_elsewhere")
        if not isinstance(elsewhere, list):
            raise AssertionError(
                f"'fixture_only_flag_scope.enabled_elsewhere' must be a list of every other environment "
                f"the flag was found enabled in, got {elsewhere!r}. A boolean cannot be audited; an "
                "enumerated empty list can"
            )
        if elsewhere:
            raise AssertionError(
                f"the control flag is enabled outside the fixture, in {sorted(map(str, elsewhere))}. "
                "DP-INV-1 permits it only in an operator-created isolated test fixture — this is the "
                "invariant the whole evaluation is conditioned on"
            )
        fixture_environment = scope.get("fixture_environment")
        if fixture_environment != self.config.get("environment"):
            raise AssertionError(
                f"the flag was enabled in environment {fixture_environment!r} but this run targets "
                f"{self.config.get('environment')!r}; the preflight is describing a different fixture "
                "than the one under evaluation"
            )

        # The wave's own completeness. Self-referential deliberately: a future
        # revision that drops a check would otherwise produce a short wave whose
        # every present check passes.
        expected = {spec.check_id for spec in WAVE2_CHECKS}
        if emitted_ids and set(emitted_ids) != expected:
            raise AssertionError(
                f"the wave's check inventory is not exactly evaluation #{WAVE_EVALUATIONS[2]}'s table: "
                f"missing {sorted(expected - set(emitted_ids))}, unexpected "
                f"{sorted(set(emitted_ids) - expected)}. A short wave whose present checks all pass is "
                "the failure this assertion exists for"
            )

        # Teardown must be CONFIGURED before anything is seeded — W2-10 verifies it
        # ran, and this is the half that has to be true at preflight time. A
        # fixture seeded without a declared teardown is one the harness cannot
        # clean up, and §7 forbids reaching for a scan to compensate.
        items = self.config.get("cleanup_items") or []
        if not items:
            raise AssertionError(
                "no 'cleanup_items' are declared, so the synthetic rows this wave seeds could not be "
                "removed by exact key afterwards. Cleanup is bounded to declared pairs by design — there "
                "is no scan to fall back on — so an undeclared row is one that survives the evaluation"
            )
        for item in items:
            if not isinstance(item, dict) or not item.get("event_id") or not item.get("arrived_at"):
                raise AssertionError(
                    f"cleanup item {item!r} does not carry BOTH event_id and arrived_at. A delete keyed "
                    "on the partition key alone could match an unrelated item, so the harness refuses it "
                    "at teardown — declaring it here is what makes the refusal a preflight failure "
                    "instead of a surprise after the fixture exists"
                )
        # Every synthetic row this wave seeds must be covered. The unknown run ID
        # is deliberately NOT required: it names a row that must not exist, so
        # "cleaning" it would mean deleting something the harness never created.
        seeded = {
            key: self.config.get(key)
            for key in ("live_run_id", "terminal_run_id", "aborted_run_id")
            if self.config.get(key)
        }
        covered = {str(item.get("event_id")) for item in items}
        uncovered = sorted(
            f"{key}={value}" for key, value in seeded.items() if str(value) not in covered
        )
        if uncovered:
            raise AssertionError(
                f"these seeded fixture rows are not covered by 'cleanup_items': {uncovered}. A row left "
                "behind is a fixture left in the state DP-INV-1 forbids"
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
        # Prior-wave checks must remain runnable on later merged stages. This
        # validates the declared surface, not acceptance of abort or steering;
        # the live capability comparison below still has to match it exactly.
        implemented = contract.get("implemented_verbs", [])
        valid_stages = (
            frozenset(), frozenset({"pause", "resume"}),
            frozenset({"pause", "resume", "abort"}),
            frozenset({"pause", "resume", "abort", "steer"}),
        )
        if (not isinstance(implemented, list)
                or any(not isinstance(verb, str) for verb in implemented)
                or len(set(implemented)) != len(implemented)
                or frozenset(implemented) not in valid_stages):
            raise AssertionError("implemented_verbs must name a valid S3/S2/S4/S6 stage without duplicates")
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
        self._assert_native_interrupt_outcome(neutrality["native_interrupt_status"])
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
        for identity in ("session_id", "attempt_id"):
            for boundary in ("before", "after"):
                value = resume.get(f"{identity}_{boundary}")
                if not isinstance(value, str) or not value.strip():
                    raise AssertionError(
                        f"{identity} {boundary} resume was not observed as a non-empty identity"
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

    # ---- W2-10 ---------------------------------------------------------

    def check_w2_10(
        self,
        *,
        cleanup: CleanupOutcome | None = None,
        capture: SecurityCapture | None = None,
        teardown: ResourceTeardown | None = None,
        emitted_ids: tuple[str, ...] = (),
    ) -> None:
        """Verified teardown, and the security posture observed while it was possible.

        This check runs AFTER `run_cleanup`, which is what makes it a verification
        rather than a claim. The distinction is the whole point: a predicate that
        ran with the other nine would necessarily execute before the deletions it
        describes, so the only thing it could assert is that an operator *said*
        cleanup would work. `cleanup` here is the harness's own first-hand record —
        the pairs it issued DeleteItem for, whether it had both key halves, and
        what the confirming consistent read returned.

        So there is no input to this check that reports a successful teardown
        without one having happened. `cleanup=None` means cleanup did not run, which
        is `not_run` (nonzero), never a pass. A recorded failure is a failure.

        **Capture then verify.** The security half is observed BEFORE teardown
        (`capture`, taken by `main` while the fixture still exists) and only absence
        is verified after it. This ordering is a correction, not a preference: an
        earlier revision made its live capability reads after teardown, against a run
        whose row teardown had just deleted, so a real gateway's not-found made a
        CORRECT teardown report NOT RUN — the wave could never pass. No removed
        resource is required to answer here. Nor is a not-found accepted AS the pass:
        row absence is established by the harness's own consistent read, and resource
        absence by reconciling against the preflight's creation ledger.

        The security observations exist because teardown is a plausible moment to
        quietly relax something: a probe pod left running, an
        ingress policy dropped along with the fixture, or an unsupported verb
        switched on to get something to pass. Those are in-cluster facts, so they
        arrive as an operator artifact — but the unsupported-verb claim is ALSO
        compared against the harness's own pre-teardown live reads, so the recording
        and the deployment have to agree.
        """
        # --- (1) the harness's own deletion record ----------------------------
        if cleanup is None:
            raise PrerequisiteMissingError(
                "the cleanup record is absent, so this check cannot confirm the teardown actually "
                "happened. W2-10 is verified against the harness's own DeleteItem/consistent-read record "
                "rather than an operator assertion, and a missing record is NOT RUN — treating it as a "
                "pass is exactly the false green this check exists to prevent"
            )
        if not cleanup.ok:
            raise AssertionError(
                f"cleanup did not complete: {'; '.join(cleanup.notes) or 'no detail recorded'}. A fixture "
                "left with rows present is the state DP-INV-1 forbids, and this check must never pass "
                "before cleanup has actually succeeded"
            )
        if cleanup.declared_items <= 0 or not cleanup.deletions:
            raise AssertionError(
                f"the cleanup record covers {cleanup.declared_items} declared row(s) and "
                f"{len(cleanup.deletions)} deletion(s). Wave 2 seeds synthetic invocations, so an empty "
                "teardown record means either they were never removed or they were never declared — and "
                "an empty record would make every per-row assertion below vacuously true"
            )
        if len(cleanup.deletions) != cleanup.declared_items:
            raise AssertionError(
                f"{cleanup.declared_items} row(s) were declared for cleanup but {len(cleanup.deletions)} "
                "were acted on; a declared row with no deletion record is one nobody can account for"
            )
        for deletion in cleanup.deletions:
            if not deletion.both_keys_present:
                raise AssertionError(
                    f"row {deletion.event_id!r}/{deletion.arrived_at!r} was not removed using BOTH "
                    "event_id and arrived_at. A delete keyed on the partition key alone could match an "
                    "unrelated item, which is deleting an ordinary row — explicitly forbidden"
                )
            if not deletion.deleted or not deletion.confirmed_absent:
                raise AssertionError(
                    f"row {deletion.event_id!r}/{deletion.arrived_at!r} is not confirmed absent "
                    f"(deleted={deletion.deleted}, confirmed_absent={deletion.confirmed_absent}, "
                    f"error={deletion.error!r}). Absence is established by a CONSISTENT read: an "
                    "eventually-consistent one can report an item gone before it is"
                )
        # Bounded to exactly the declared synthetic rows. Asserted here as well as
        # in `run_cleanup` because this is the check the evaluation reads: the
        # record must not contain a row the config never declared, which is what a
        # scan-and-delete would produce.
        declared = {
            (str(item.get("event_id")), str(item.get("arrived_at")))
            for item in (self.config.get("cleanup_items") or [])
        }
        acted = {(deletion.event_id, deletion.arrived_at) for deletion in cleanup.deletions}
        unexpected = sorted(acted - declared)
        if unexpected:
            raise AssertionError(
                f"the teardown removed rows that the fixture config never declared: {unexpected}. "
                "Cleanup must be bounded to declared pairs — no scan, no prefix, no wildcard — so an "
                "undeclared deletion means an ordinary row was reachable"
            )
        # The unknown run ID names a row that must NOT exist. Deleting it would mean
        # the harness removed something it never created.
        unknown = self.config.get("unknown_run_id")
        if unknown and any(deletion.event_id == str(unknown) for deletion in cleanup.deletions):
            raise AssertionError(
                f"the teardown deleted {unknown!r}, which is the 'unknown' run ID: it names a row that "
                "must not exist, so deleting it means the harness removed an object it did not create"
            )

        # --- (2) the security observations captured BEFORE teardown ------------
        # Ordering is the whole correction here. An earlier revision asked the live
        # deployment "do you still refuse the unimplemented verbs?" AFTER teardown,
        # about a run whose row teardown had just deleted — so a CORRECT teardown
        # produced a not-found and this check reported NOT RUN, which is the defect
        # that made wave 2 unpassable. A removed resource must never be required to
        # answer.
        #
        # So the live capability reads happen while the fixture still exists
        # (`capture_security_observations`, called by `main` before `run_cleanup`), and
        # what arrives here is that first-hand record plus the operator's in-cluster
        # half. Absent capture is NOT RUN, never a pass.
        if capture is None:
            raise PrerequisiteMissingError(
                "the pre-teardown security capture is absent, so the deployed capability surface was "
                "never observed while the fixture existed. These observations cannot be made after "
                "teardown — the resources are gone by then — so this is NOT RUN rather than a pass"
            )
        if not capture.ok:
            raise AssertionError(
                f"the pre-teardown security capture did not complete: "
                f"{'; '.join(capture.notes) or 'no detail recorded'}. Without it the wave has no evidence "
                "about the capability surface of the build it evaluated"
            )
        capture_artifact = self._artifact("security_capture")
        if capture_artifact["captured_before_teardown"] is not True:
            raise AssertionError(
                f"'security_capture.captured_before_teardown' is "
                f"{capture_artifact['captured_before_teardown']!r}. These observations are only meaningful "
                "if they were taken while the fixture was still running; recorded after teardown they "
                "describe an environment that no longer existed"
            )
        capture_run = self._assert_fixture_identity(
            capture_artifact, "security_capture", self.config
        )
        if capture_run != capture.run_id:
            raise AssertionError(
                f"the security capture artifact describes run {capture_run!r} but this run is "
                f"{capture.run_id!r}; observations from another run are not this evaluation's evidence"
            )

        # The capture must be bound to what was actually DEPLOYED, per component.
        # An earlier revision compared it against one story's historical merge commit,
        # which a correct (newer) deployment does not equal — so it failed correct
        # deployments and implicitly demanded redeploying an old merge. The right
        # subject is the running build.
        try:
            preflight = self._artifact("wave2_preflight")
        except (PrerequisiteMissingError, AssertionError):
            # W2-01 owns that failure and reports it. Not re-raised here: two checks
            # failing for one missing artifact would double-count a single defect.
            preflight = {}
        deployed_entries = preflight.get("deployed_components")
        expected_identity = {}
        if isinstance(deployed_entries, dict):
            for component in WAVE2_DEPLOYED_COMPONENTS:
                entry = deployed_entries.get(component)
                if isinstance(entry, dict):
                    expected_identity[component] = {
                        "revision": entry.get("revision"),
                        "image_digest": entry.get("image_digest"),
                    }
        observed = capture_artifact["observed_revisions"]
        if not isinstance(observed, dict):
            raise AssertionError(
                f"'security_capture.observed_revisions' must be an object keyed by component, got "
                f"{observed!r}. A security observation that cannot be tied to the build it was made "
                "against is a true statement about an unknown subject"
            )
        for component, expected in sorted(expected_identity.items()):
            entry = observed.get(component)
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"the security capture records no observed revision/digest for the {component!r} "
                    f"component ({entry!r}); it must name what it observed, per component"
                )
            for key in ("revision", "image_digest"):
                if expected.get(key) and entry.get(key) != expected.get(key):
                    raise AssertionError(
                        f"the security capture observed {component!r} {key} {entry.get(key)!r}, but "
                        f"the preflight records the DEPLOYED {key} as {expected.get(key)!r}. "
                        "Observing a different build is the stale-evidence case: it was a true "
                        "observation, just not of this deployment"
                    )

        if capture_artifact["isolation_present"] is not True:
            raise AssertionError(
                f"isolation was not recorded as present while the fixture was running "
                f"({capture_artifact['isolation_present']!r}); DP-INV-1 requires it for as long as "
                "anything control-enabled exists"
            )
        if capture_artifact["ordinary_flags_off"] is not True:
            raise AssertionError(
                "ordinary gateway/worker/SPA control flags were not recorded as off during the capture; "
                "the flag must not have spread beyond the fixture during the evaluation"
            )
        if capture_artifact["general_flag_enablement"] is not False:
            raise AssertionError(
                f"'security_capture.general_flag_enablement' is "
                f"{capture_artifact['general_flag_enablement']!r}: the evaluation must not have enabled "
                "the control flag generally. Widening the flag to make a check pass is the specific "
                "shortcut this assertion forbids"
            )

        # Wave 1's security properties, re-observed rather than assumed to persist.
        # Named individually: these are separate guarantees with separate failure
        # modes, and #5029's authorization-at-handoff is called out explicitly
        # because it is the one a pause/resume implementation could plausibly have
        # relaxed.
        security = capture_artifact["wave1_security"]
        if not isinstance(security, dict):
            raise AssertionError(f"'wave1_security' must be an object, got {security!r}")
        required_properties = (
            "unauthenticated_rejected",
            "cross_tenant_indistinguishable",
            "nonowner_indistinguishable",
            "transport_targets_blocked",
            "no_token_in_public_state",
            "admission_authorization_preserved",
            "delivery_authorization_preserved",
        )
        absent = [name for name in required_properties if name not in security]
        if absent:
            raise AssertionError(
                f"the wave-1 security recheck records no result for {absent}; an unrecorded property is "
                "one nobody re-observed, and these are exactly the guarantees a new control verb could "
                "have relaxed"
            )
        failed = sorted(name for name in required_properties if security[name] is not True)
        if failed:
            raise AssertionError(
                f"wave-1 security properties did not hold on the current revision: {failed}. #5029 "
                "requires admission and delivery authorization to be revalidated immediately before "
                "physical handoff, and pause/resume is precisely the code path that could have moved that "
                "revalidation earlier"
            )

        # --- (3) unsupported verbs and adapters: recorded AND live-observed ------
        # The recorded claim, then the harness's OWN live reads from the capture. Either
        # alone is weaker: the artifact describes what the operator probed, and the
        # capture is what the deployment answered the harness while it was running.
        unsupported = capture_artifact["unsupported_verbs"]
        if not isinstance(unsupported, dict) or not unsupported:
            raise AssertionError(
                f"'unsupported_verbs' must be a nonempty object mapping each unimplemented verb to the "
                f"status it returned, got {unsupported!r}"
            )
        wrong = sorted(
            f"{verb}={status!r}" for verb, status in unsupported.items() if status != 501
        )
        if wrong:
            raise AssertionError(
                f"unsupported verbs did not answer 501: {wrong}. An authorized request for a verb this "
                "build does not implement must be refused as unimplemented — any other status means it "
                "was either enabled or is failing for a different reason"
            )
        capabilities_claim = capture_artifact["unsupported_adapter_capabilities"]
        if not isinstance(capabilities_claim, dict) or not capabilities_claim:
            raise AssertionError(
                f"'unsupported_adapter_capabilities' must be a nonempty object, got {capabilities_claim!r}"
            )
        enabled_claim = sorted(
            verb for verb, value in capabilities_claim.items() if value is not False
        )
        if enabled_claim:
            raise AssertionError(
                f"these unsupported adapter capabilities are not recorded as false: {enabled_claim}"
            )

        # The harness's own live reads, taken pre-teardown. Both adapters must have
        # been reached: a capture covering one edge cannot speak for the other, which
        # is the whole reason there are two path templates.
        captured_adapters = {entry.adapter for entry in capture.adapters}
        missing_adapters = sorted(set(ADAPTERS) - captured_adapters)
        if missing_adapters:
            raise AssertionError(
                f"the pre-teardown capture did not observe the {missing_adapters} adapter edge(s). The two "
                "edges share one control service, so a capture of only one of them cannot establish that "
                "they did not drift"
            )
        for entry in capture.adapters:
            if entry.status != 200 or entry.error:
                raise AssertionError(
                    f"{entry.adapter}: the pre-teardown capability read returned status {entry.status!r} "
                    f"(error={entry.error!r}). This read happens while the fixture is still running, so a "
                    "non-200 here is the deployment failing to answer rather than a torn-down fixture"
                )
            if not entry.capabilities:
                raise AssertionError(
                    f"{entry.adapter}: the pre-teardown state read carried no capabilities object, so the "
                    "deployed capability surface was not observed"
                )
            for verb, recorded_status in sorted(unsupported.items()):
                if entry.capabilities.get(verb) is not False:
                    raise AssertionError(
                        f"{entry.adapter}: the deployed capability map reported {verb!r} as "
                        f"{entry.capabilities.get(verb)!r}, but the recheck records it as unsupported. The "
                        "recording and the deployment disagree, and the deployment is what the next "
                        "operator inherits"
                    )
                # The harness's own authorized attempt, not just the advertised map.
                # A capability map is a claim the deployment makes about itself; the
                # status it returns when the verb is actually POSTed is what a caller
                # would get. An enabled-but-still-advertised-false verb is exactly the
                # quiet relaxation this half of the check exists to catch, and only the
                # attempt can see it.
                observed_status = entry.refusals.get(verb)
                if verb not in entry.refusals:
                    raise AssertionError(
                        f"{entry.adapter}: the pre-teardown capture made no authorized attempt at the "
                        f"unsupported verb {verb!r}, so nothing observed how the deployment answers it. "
                        "The advertised capability map alone is the deployment's own claim about itself"
                    )
                if observed_status != recorded_status:
                    raise AssertionError(
                        f"{entry.adapter}: an authorized owner's {verb!r} returned {observed_status!r} "
                        f"live, but the recheck records {recorded_status!r}. The harness's own observation "
                        "and the operator's recording describe different deployments"
                    )

        # --- (4) what teardown ACHIEVED: absence, against the creation ledger ----
        # Only absence is asked for after teardown, because absence is the only thing
        # teardown is supposed to produce. Nothing here requires a removed resource to
        # respond.
        # The harness's own record of INVOKING the fixture's teardown. Asserted BEFORE
        # the artifact is read, because it is what makes reading the artifact
        # meaningful at all: without this step the published command captured state,
        # deleted rows, and then read a file already declaring the pods and queues
        # gone — so no sequential operator run could have produced that file honestly
        # at that point, and the only way to have it was to write it beforehand.
        if teardown is None or not teardown.configured:
            raise PrerequisiteMissingError(
                "the fixture config declares no 'resource_teardown' command, so the harness never tore "
                "the fixture's resources down between observing them and verifying their absence. The "
                "absence observations below would necessarily predate the removal they describe, which "
                "is the prefilled-absence artifact this check must refuse — NOT RUN, not a pass"
            )
        if not teardown.invoked:
            raise AssertionError(
                f"the resource teardown was declared but not invoked: "
                f"{'; '.join(teardown.notes) or 'no detail recorded'}"
            )
        if not teardown.ok:
            raise AssertionError(
                f"the fixture's resource teardown failed (exit {teardown.exit_code!r}): "
                f"{'; '.join(teardown.notes) or 'no detail recorded'}. Its resources are not established "
                "as removed, and this check must never pass before teardown has actually succeeded"
            )

        verification = self._artifact("teardown_verification")
        # Freshness, established first-hand rather than read out of the artifact. The
        # seam digested this file immediately before invoking teardown; if the bytes
        # are identical now, the observations inside it were recorded before the
        # removal they describe. That is the prefilled-absence artifact, and no field
        # the artifact carries could rule it out — a `captured_at` is a string written
        # by the same hand as the absence claims.
        after_digest = file_digest(self.artifacts.resolve("teardown_verification"))
        if (
            teardown.verification_present_before
            and after_digest is not None
            and after_digest == teardown.verification_digest_before
        ):
            raise AssertionError(
                "the post-teardown absence artifact is byte-for-byte unchanged across the resource "
                f"teardown the harness invoked ({teardown.verification_digest_before}). It therefore "
                "records state observed BEFORE the removal it describes: a prefilled absence "
                "artifact. The fixture's teardown has to write these observations as part of tearing "
                "down, because 'the resources are gone' is only checkable after they have gone"
            )
        # The absence observations must also be DATED after the teardown, which catches
        # a rewritten-but-backdated file the digest comparison alone would accept.
        captured_at = _parse_timestamp(verification.get("captured_at"))
        if captured_at is None:
            raise AssertionError(
                f"'teardown_verification.captured_at' is {verification.get('captured_at')!r}, not a "
                "parseable ISO-8601 instant, so the absence observations cannot be placed relative to the "
                f"teardown the harness ran ({teardown.started_at!r} → {teardown.finished_at!r}). An "
                "artifact that cannot be dated cannot be shown to postdate the removal it describes"
            )
        # Against the teardown's START, not its finish. The honest producer of this
        # artifact is the teardown command itself — it removes the resources, then
        # records their absence, then exits — so a correct `captured_at` falls INSIDE
        # the window and requiring it to postdate `finished_at` would reject exactly
        # the lifecycle being asked for. What it must not do is predate the teardown
        # entirely, which is the backdated file the digest check would otherwise miss.
        started_at = _parse_timestamp(teardown.started_at)
        if started_at is not None and captured_at < started_at:
            raise AssertionError(
                f"the absence observations are dated {verification.get('captured_at')!r}, before the "
                f"fixture's resource teardown even began at {teardown.started_at!r}. Observations recorded "
                "before the removal describe the fixture while it still existed — a prefilled absence "
                "artifact, which is what this check refuses"
            )
        if verification["verified_after_teardown"] is not True:
            raise AssertionError(
                f"'teardown_verification.verified_after_teardown' is "
                f"{verification['verified_after_teardown']!r}; absence observations recorded before "
                "teardown would describe the fixture while it still existed"
            )
        verify_run = self._assert_fixture_identity(
            verification, "teardown_verification", self.config
        )
        if verify_run != capture_run:
            raise AssertionError(
                f"the teardown verification describes run {verify_run!r} but the security capture "
                f"describes {capture_run!r}; the two halves must be about the same fixture run"
            )
        # Baseline isolation, NOT the fixture's own policies. These are different
        # objects with opposite lifecycles, and an earlier revision conflated them into
        # one `isolation_present: true` — which made this check unsatisfiable alongside
        # ledger reconciliation: the fixture's NetworkPolicies are ledger resources, so
        # either a policy was left behind (leaking) or it was removed and isolation was
        # no longer "present". What must survive the fixture is the environment's
        # persistent baseline isolation; what must be gone is every policy this fixture
        # created.
        if verification["baseline_isolation_present"] is not True:
            raise AssertionError(
                f"the environment's persistent baseline isolation is not recorded as still present after "
                f"teardown ({verification['baseline_isolation_present']!r}). Teardown must remove the "
                "fixture's OWN policies without taking the baseline with them: DP-INV-1 requires the "
                "environment to stay isolated after the fixture is gone, and this is the namespace-level "
                "isolation that belongs to the environment rather than to this run"
            )
        if verification["ordinary_flags_off"] is not True:
            raise AssertionError(
                "ordinary gateway/worker/SPA control flags were not recorded as off at verification time"
            )
        if verification["general_flag_enablement"] is not False:
            raise AssertionError(
                f"'teardown_verification.general_flag_enablement' is "
                f"{verification['general_flag_enablement']!r}: the evaluation must not leave the control "
                "flag generally enabled"
            )

        # Reconciliation against the ledger, in BOTH directions. This is what makes
        # completeness a property of what was created rather than of what someone
        # remembered to list: an earlier revision took a caller-keyed map of names to
        # booleans, so omitting a leaked workload passed.
        ledger = {}
        if isinstance(preflight.get("creation_ledger"), list):
            try:
                ledger = self._assert_creation_ledger(preflight["creation_ledger"])
            except AssertionError:
                # W2-01 owns and reports a malformed ledger; failing here too would
                # double-count one defect. An unusable ledger still cannot silently
                # become an empty one, so the emptiness check below catches it.
                ledger = {}
        if not ledger:
            raise PrerequisiteMissingError(
                "the preflight's creation ledger is unavailable, so teardown completeness cannot be "
                "measured against what the fixture actually created. Removal evidence alone only covers "
                "the resources it happens to mention, which is exactly how a leaked resource passes"
            )
        removals = verification["removals"]
        if not isinstance(removals, list) or not removals:
            raise AssertionError(
                f"'teardown_verification.removals' must be a nonempty list of absence observations, got "
                f"{removals!r}"
            )
        observed_identities: dict[str, dict] = {}
        for removal in removals:
            if not isinstance(removal, dict):
                raise AssertionError(
                    f"removal observation {removal!r} must be an object carrying "
                    f"{list(LEDGER_REMOVAL_KEYS)}"
                )
            missing = [key for key in LEDGER_REMOVAL_KEYS if key not in removal]
            if missing:
                raise AssertionError(
                    f"removal observation {removal!r} is missing {sorted(missing)}. 'observed_by' records "
                    "HOW absence was established, without which the claim is not an observation"
                )
            identity = str(removal["identity"])
            if identity not in ledger:
                raise AssertionError(
                    f"the teardown recorded the removal of {identity!r}, which the creation ledger does "
                    "not contain. Removing something this fixture did not create means an unrelated "
                    "resource was in reach of teardown"
                )
            if not removal.get("observed_by"):
                raise AssertionError(
                    f"the removal of {identity!r} records no 'observed_by'; absence has to be established "
                    "by an actual read, and an unattributed claim is the assertion this check replaced"
                )
            observed_identities[identity] = removal
        # Every created resource accounted for. The direction that matters: a leaked
        # resource is one with no absence observation, and only the ledger knows it
        # exists.
        unaccounted = sorted(
            f"{entry.get('kind')}/{entry.get('name')} ({identity})"
            for identity, entry in ledger.items()
            if identity not in observed_identities
        )
        if unaccounted:
            raise AssertionError(
                f"these created fixture resources have no post-teardown absence observation: "
                f"{unaccounted}. A resource nobody looked for is how a control-enabled workload outlives "
                "its evaluation"
            )
        still_present = sorted(
            f"{ledger[identity].get('kind')}/{ledger[identity].get('name')} ({identity})"
            for identity, removal in observed_identities.items()
            if removal.get("absent") is not True
        )
        if still_present:
            raise AssertionError(
                f"these fixture resources were still present after teardown: {still_present}. A fixture "
                "workload left running is a control-enabled pod outliving its evaluation"
            )
        # The ordering WITHIN teardown, now that every removal is reconciled to a
        # ledger entry that says what kind of resource it was.
        self._assert_listener_died_before_policies(ledger, observed_identities)

        # --- (5) the deleted row must not still read as live ---------------------
        # A best-effort corroboration, deliberately asymmetric, and the asymmetry is
        # the point. The authoritative absence evidence is the consistent read in (1);
        # this read cannot CREATE a pass, because after teardown the fixture gateway is
        # itself supposed to be gone and an unreachable or not-found edge is the
        # expected outcome. What it can do is FAIL: a gateway still serving the deleted
        # run as live contradicts the deletion record, and a contradiction between two
        # observations is a finding rather than something to average out.
        #
        # This is also why a not-found is never relabelled as the pass. It is
        # consistent with a correct teardown and equally consistent with a fixture
        # that never existed, so on its own it distinguishes nothing.
        live = self.config.get("live_run_id")
        deleted_live = live and any(
            deletion.event_id == str(live) and deletion.confirmed_absent
            for deletion in cleanup.deletions
        )
        if deleted_live:
            owner_token = (self.config.get("identity_env") or {}).get("owner")
            token = os.environ.get(owner_token) if owner_token else None
            if token:
                for adapter, paths in ADAPTERS.items():
                    state = self.probe.request(
                        "GET", paths["state"].format(run_id=live), role="owner", token=token
                    )
                    if state.status != 200:
                        # Expected: the row is gone, and so may be the fixture edge.
                        continue
                    body = self._body_of(state)
                    if body.get("available") is True or body.get("state") == "running":
                        raise AssertionError(
                            f"{adapter}: after teardown the gateway still reports the deleted run "
                            f"{live!r} as live (available={body.get('available')!r}, "
                            f"state={body.get('state')!r}), but the harness's consistent read confirmed "
                            "the row absent. The deployment and the deletion record disagree"
                        )

        # The wave's inventory again, at the other end of the run. W2-01 asserts it
        # before the checks; asserting it here too is what makes "all ten answered"
        # a property of the REPORT rather than of the manifest constant.
        expected = {spec.check_id for spec in WAVE2_CHECKS}
        if emitted_ids and set(emitted_ids) != expected:
            raise AssertionError(
                f"the result does not carry exactly evaluation #{WAVE_EVALUATIONS[2]}'s check IDs: "
                f"missing {sorted(expected - set(emitted_ids))}, unexpected "
                f"{sorted(set(emitted_ids) - expected)}"
            )

    # ---- wave 3 shared helpers -----------------------------------------

    @staticmethod
    def _assert_command_id_sequence(value: object, *, field: str, subject: str) -> tuple[str, ...]:
        """Read a recorded sequence of command IDs, refusing the shapes that hide a defect.

        Returned as a tuple so callers compare ORDER, which is the only reason
        these are recorded as sequences rather than counts. Two shapes are refused
        outright: a non-list (a comma-joined string compares equal to another
        comma-joined string regardless of how it was built, and cannot be indexed
        to say which pair inverted) and a duplicated ID (the same command appearing
        twice in a handoff order is a replay, and deduplicating it here would
        convert that finding into a passing comparison).
        """
        if not isinstance(value, list) or not value:
            raise AssertionError(
                f"`{subject}.{field}` is {value!r}; expected a non-empty list of command IDs. "
                "Ordering is the property under test and it cannot be read off a scalar"
            )
        ids: list[str] = []
        for index, entry in enumerate(value):
            if not isinstance(entry, str) or not entry.strip():
                raise AssertionError(
                    f"`{subject}.{field}[{index}]` is {entry!r}, not a command ID. An unnamed slot "
                    "makes the sequence uncomparable at exactly the position it matters"
                )
            ids.append(entry.strip())
        duplicated = sorted({entry for entry in ids if ids.count(entry) > 1})
        if duplicated:
            raise AssertionError(
                f"`{subject}.{field}` repeats {duplicated}. A command ID appearing twice is a "
                "replayed command, which is the failure AC-T5 and AC-T7 both forbid — it must not be "
                "collapsed into a set before the order comparison"
            )
        return tuple(ids)

    # ---- W3-06 ---------------------------------------------------------

    def check_w3_06(self) -> None:
        """A mid-tool steer is pending, then acknowledged at handoff (AC-T2, AC-T4).

        Two claims, and the second is the one that is easy to fake. The first is
        that a steer arriving while a tool is running is accepted and held —
        `pending` is the honest answer there, because the SDK has no parked reader
        to hand it to. The second is that the acknowledgement the operator sees
        tracks the SDK handoff rather than the enqueue.

        The latency bound is measured from `handoff_at`, not `accepted_at`, and the
        difference is not a technicality. A steer submitted into a long tool call is
        legitimately pending for minutes, so a bound measured from submission would
        fail a correct run for being patient — and an implementation that
        acknowledged at enqueue would pass it easily. Measured from handoff, the
        bound constrains the only gap that is a defect: the SDK has the input and
        the operator has not been told.

        A marker stamped BEFORE the handoff fails for the same reason rather than
        being tolerated as clock skew. Acknowledging first and delivering afterwards
        is precisely the dishonest ordering AC-T4 exists to forbid, and it is
        indistinguishable from skew by any evidence available here — so the
        conservative reading is the one that does not silently accept it.

        Nothing here asserts the model did what it was told. `delivered` means the
        SDK accepted the bytes; a model that reads the instruction and declines is
        not a delivery failure, and an artifact claiming comprehension is recording
        something no transport observation can establish.
        """
        delivery = self._artifact("steering_delivery")

        command_id = delivery.get("command_id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise AssertionError(
                f"`steering_delivery.command_id` is {command_id!r}; without the ID the operator "
                "submitted, none of the state, log and marker comparisons below identify anything"
            )
        command_id = command_id.strip()

        # --- the submission was actually mid-tool -----------------------------
        # If it was not, AC-T2's subject was never exercised: a steer handed off at
        # an idle boundary is the easy case, and passing it says nothing about the
        # case where there is no parked reader to hand it to.
        if delivery.get("tool_active_at_submission") is not True:
            raise AssertionError(
                "no tool was active when the steer was submitted, so the mid-tool case AC-T2 names was "
                "not exercised. Handoff at an idle boundary is the easy path and demonstrates nothing "
                "about a submission that must wait for one"
            )
        if delivery.get("status_at_submission") != "pending":
            raise AssertionError(
                f"the steer was recorded as {delivery.get('status_at_submission')!r} at submission, "
                "expected 'pending'. Mid-tool there is no parked SDK reader, so any status claiming "
                "delivery at that moment is describing a handoff that could not have happened"
            )

        # --- the three instants -----------------------------------------------
        instants: dict[str, datetime] = {}
        for name in ("accepted_at", "handoff_at", "marker_at"):
            parsed = _parse_timestamp(delivery.get(name))
            if parsed is None:
                raise AssertionError(
                    f"`steering_delivery.{name}` is {delivery.get(name)!r}, which is not an ISO-8601 "
                    "instant. All three are required separately: the bound AC-T4 states is a gap "
                    "between two of them, and a missing one turns it into a different bound"
                )
            instants[name] = parsed
        if instants["handoff_at"] < instants["accepted_at"]:
            raise AssertionError(
                f"the handoff at {delivery.get('handoff_at')!r} precedes acceptance at "
                f"{delivery.get('accepted_at')!r}; a command cannot be delivered before it was received, "
                "so at least one of the two recorded instants describes something other than what it names"
            )
        if instants["marker_at"] < instants["handoff_at"]:
            raise AssertionError(
                f"the live-comment marker at {delivery.get('marker_at')!r} predates the SDK handoff at "
                f"{delivery.get('handoff_at')!r}. Acknowledging before delivering is the dishonest "
                "ordering AC-T4 forbids — the operator is shown a confirmed command that the SDK had "
                "not yet accepted"
            )
        latency = (instants["marker_at"] - instants["handoff_at"]).total_seconds()
        if latency > STEER_MARKER_MAX_LATENCY_SECONDS:
            raise AssertionError(
                f"the marker appeared {latency:.1f}s after the SDK handoff, over the "
                f"{STEER_MARKER_MAX_LATENCY_SECONDS}s bound. The wait before handoff is not counted "
                "against this bound; what is over budget is the interval in which the SDK held the "
                "instruction and the operator could not tell delivery from a dropped command"
            )

        # --- one command, one ID, in both records -----------------------------
        for name in ("state_command_ids", "log_command_ids"):
            ids = self._assert_command_id_sequence(
                delivery.get(name), field=name, subject="steering_delivery"
            )
            if command_id not in ids:
                raise AssertionError(
                    f"command {command_id!r} does not appear in `steering_delivery.{name}` ({list(ids)}). "
                    "State and logs must name the same command the operator submitted, or an operator "
                    "correlating an acknowledgement to their own request has nothing to match on"
                )
        if delivery.get("delivered_at_matches_handoff") is not True:
            raise AssertionError(
                "the recorded `delivered_at` does not match the observed SDK handoff. That field is what "
                "the dashboard renders as the delivery time, so a value taken at enqueue reports a "
                "confirmation that had not happened yet"
            )

        # --- and no claim the transport cannot support -------------------------
        if delivery.get("model_comprehension_claimed") is not False:
            raise AssertionError(
                "the artifact claims the model comprehended or complied with the instruction. `delivered` "
                "is a statement about the SDK accepting bytes; whether the model then acted is not "
                "observable from the transport, and recording it as though it were makes a declined "
                "instruction indistinguishable from a delivered one"
            )

    # ---- W3-07 ---------------------------------------------------------

    def check_w3_07(self) -> None:
        """The bound, the order, and the three ways a command ends unsent (AC-T5, AC-T8).

        The queue is the part of steering an operator interacts with under load,
        and every property here is one whose violation is silent. A cap that admits
        an eleventh command does not error — it just makes the run's instruction
        backlog unbounded. A queue that delivers out of order still reports every
        command delivered. A pending command cancelled by an abort, or lost to an
        expired journal generation, looks from outside exactly like one that is
        still waiting.

        `handoff_order` is compared to `submission_order` element by element rather
        than as sets, because a FIFO that inverts one pair satisfies every
        set-level comparison. And an expired generation must resolve to `unknown`,
        never to a retry: at that point the harness cannot tell whether the SDK got
        the bytes, and the only two options are to say so or to risk delivering a
        mid-run instruction twice.
        """
        queue = self._artifact("steering_queue")

        submitted = self._assert_command_id_sequence(
            queue.get("submission_order"), field="submission_order", subject="steering_queue"
        )
        # --- the cap -----------------------------------------------------------
        accepted = queue.get("accepted_count")
        if accepted != STEER_QUEUE_CAP:
            raise AssertionError(
                f"`accepted_count` is {accepted!r}, expected exactly {STEER_QUEUE_CAP}. The run-level "
                "FIFO is bounded, and a cap that admits one more than it declares is an unbounded "
                "backlog with a number written next to it"
            )
        if len(submitted) != STEER_QUEUE_CAP:
            raise AssertionError(
                f"{len(submitted)} submissions were recorded but the cap is {STEER_QUEUE_CAP}; the "
                "overflow case is only exercised once the queue is actually full, so a short sequence "
                "means the eleventh command was rejected by something other than the bound"
            )
        if queue.get("overflow_status") != 429:
            raise AssertionError(
                f"the over-cap submission returned {queue.get('overflow_status')!r}, expected 429. "
                "Backpressure has to be visible to the caller: a silently dropped instruction is one "
                "the operator believes is queued"
            )

        # --- the order ---------------------------------------------------------
        handed = self._assert_command_id_sequence(
            queue.get("handoff_order"), field="handoff_order", subject="steering_queue"
        )
        if handed != submitted:
            if set(handed) != set(submitted):
                raise AssertionError(
                    f"the handoff set differs from the submission set: delivered-but-never-submitted "
                    f"{sorted(set(handed) - set(submitted))}, submitted-but-never-delivered "
                    f"{sorted(set(submitted) - set(handed))}. Every accepted command reaches the SDK or "
                    "ends in a terminal status naming why; neither is what a missing ID records"
                )
            inverted = next(
                (index for index, (a, b) in enumerate(zip(submitted, handed)) if a != b), 0
            )
            raise AssertionError(
                f"the queue is not FIFO: position {inverted} was submitted as {submitted[inverted]!r} "
                f"but handed off as {handed[inverted]!r}. Mid-run instructions are order-dependent — "
                "'now do X' after 'stop doing Y' is not the same pair reversed — and an out-of-order "
                "delivery still reports every command delivered"
            )

        # --- pause holds, it does not drop -------------------------------------
        paused = self._assert_command_id_sequence(
            queue.get("paused_pending_ids"), field="paused_pending_ids", subject="steering_queue"
        )
        released = self._assert_command_id_sequence(
            queue.get("paused_delivered_after_resume"),
            field="paused_delivered_after_resume",
            subject="steering_queue",
        )
        if released != paused:
            raise AssertionError(
                f"commands submitted while paused were {list(paused)} but {list(released)} were "
                "delivered after resume. A pause is not a discard: every command held at the barrier "
                "has to arrive, in the order it was accepted, once the operator lets the run continue"
            )

        # --- abort cancels rather than delivers --------------------------------
        cancelled = self._assert_command_id_sequence(
            queue.get("abort_cancelled_ids"), field="abort_cancelled_ids", subject="steering_queue"
        )
        delivered_after_abort = sorted(set(cancelled) & set(handed))
        if delivered_after_abort:
            raise AssertionError(
                f"these commands were recorded as cancelled by the abort AND handed to the SDK: "
                f"{delivered_after_abort}. An abort that flushes its queue on the way out delivers "
                "instructions the operator aborted to prevent"
            )

        # --- and the one outcome that must not become a retry ------------------
        if queue.get("expiry_outcome") != "unknown":
            raise AssertionError(
                f"journal expiry or generation loss resolved to {queue.get('expiry_outcome')!r}, "
                "expected 'unknown'. At that point nobody can say whether the SDK received the bytes, "
                "and the two dishonest answers are opposite: 'delivered' claims a handoff nobody "
                "observed, 'pending' invites a replay of an instruction the model may already have"
            )
        if queue.get("replayed_after_unknown") is not False:
            raise AssertionError(
                "a command was replayed after its outcome became unknown. `unknown` exists precisely so "
                "that an ambiguous handoff is reported rather than retried; retrying it is how the model "
                "receives the same mid-run instruction twice"
            )
        if queue.get("authority_revalidated_at_handoff") is not True:
            raise AssertionError(
                "authority was not revalidated immediately before the physical handoff. A command can "
                "sit in the queue for minutes, so a check performed only at submission lets a revoked "
                "or expired authorization reach the model through the delay — which is the bypass the "
                "#5029 delivery authorization exists to close"
            )

    # ---- W3-08 ---------------------------------------------------------

    def check_w3_08(self) -> None:
        """The bytes handed to the SDK carry the trust boundary (AC-S8).

        Operator steering text is untrusted input by the same rule that makes an
        issue body untrusted: it arrives from outside the run and is read by a
        model that acts on what it reads. So what matters is the actual SDK-bound
        payload, not a source-level claim that a wrapper is called somewhere.

        `delimiters_present` on its own is satisfiable by a wrapper appended AFTER
        the raw instruction, which is why `instruction_inside_delimiters` is a
        separate observation and the one that carries the property. A payload with
        the preamble below the instruction has the delimiters and none of the
        containment.

        The other half is attribution. #5029's trusted caller identity is ADP
        framing and belongs outside the envelope; attacker-supplied actor metadata
        inside the instruction must not be promoted into it. If it can be, an
        operator's text can name its own authority.
        """
        boundary = self._artifact("steering_trust_boundary")

        if boundary.get("delimiters_present") is not True:
            raise AssertionError(
                "the SDK-bound steering text carries no trust-boundary delimiters. Unwrapped operator "
                "text reads to the model as ADP's own instruction, which is how a steer becomes an "
                "instruction-injection vector rather than a message to consider"
            )
        if boundary.get("instruction_inside_delimiters") is not True:
            raise AssertionError(
                "the instruction is not inside the trust-boundary delimiters. Delimiters that follow the "
                "raw text — or wrap something else — satisfy `delimiters_present` while containing "
                "nothing, so this is the observation that carries AC-S8 and it is failing"
            )
        attribution = boundary.get("actor_attribution")
        if not isinstance(attribution, str) or not attribution.strip():
            raise AssertionError(
                f"`actor_attribution` is {attribution!r}; the trusted caller identity #5029 established "
                "must accompany the instruction as ADP framing, or the model cannot tell an authorized "
                "operator's steer from arbitrary text that reached the queue"
            )
        if boundary.get("origin_kind") != "human":
            raise AssertionError(
                f"`origin_kind` is {boundary.get('origin_kind')!r}, expected 'human'. Steering is an "
                "operator message; an origin claiming otherwise misrepresents who is speaking in the "
                "transcript the model reasons over"
            )
        if boundary.get("should_query") is not True:
            raise AssertionError(
                "`should_query` is not true for a steering message. Steering exists to make the model "
                "act on the instruction; queued as a non-querying annotation it is filed away until "
                "something else happens to provoke a turn, which is the note semantics, not steering"
            )
        if boundary.get("attacker_actor_metadata_rejected") is not True:
            raise AssertionError(
                "attacker-supplied actor metadata was not rejected. Attribution is ADP's statement about "
                "who called; if text inside the envelope can set it, the envelope's own authority claim "
                "becomes attacker-controlled"
            )
        if boundary.get("raw_instruction_in_system_text") is not False:
            raise AssertionError(
                "the raw instruction appeared in system text. Elevating untrusted operator input to the "
                "one part of the prompt the model treats as its own rules defeats the wrapping entirely — "
                "the delimiters elsewhere do not matter if the text is also present unwrapped"
            )

    # ---- W3-09 ---------------------------------------------------------

    def check_w3_09(self) -> None:
        """The real SDK input stream takes more than one message (AC-T6).

        This is the claim mocks cannot make. A fixture input channel accepts as
        many messages as the test pushes into it by construction; what AC-T6 asks
        is whether the provider's streaming-input mode does, against the pinned SDK,
        for a session that has already started work. So the evidence is recorded
        message and turn counts from a live stream, and #3969 explicitly refuses a
        source grep or a mock-only run as a substitute.

        "At least two later user messages" is the bar because one is ambiguous: a
        single post-initial message is also what a restart with the prompt replayed
        looks like. Two consecutive ones on the same session can only be a stream
        that stayed open.

        Disposal is checked in the same place rather than separately because the
        failure it prevents is a leak that only appears in aggregate — an
        undisposed async generator and an unclosed Query hold a subprocess per
        attempt, and a run that retries a few times exhausts what it is given
        without any single attempt looking wrong.
        """
        stream = self._artifact("steering_input_stream")

        if stream.get("initial_task_consumed") is not True:
            raise AssertionError(
                "the initial task was not consumed from the input stream. Steering shares the channel the "
                "prompt arrives on, so a stream that never delivered the task is not the one the run "
                "uses — and the later messages then say nothing about the production path"
            )
        later = stream.get("later_user_messages")
        if not isinstance(later, int) or later < 2:
            raise AssertionError(
                f"`later_user_messages` is {later!r}; AC-T6 requires at least 2 after the initial task. "
                "One is ambiguous — a replayed prompt on a fresh session looks identical — whereas two "
                "consecutive later messages can only come from a stream that stayed open"
            )
        message_count = stream.get("message_count")
        if not isinstance(message_count, int) or message_count < later + 1:
            raise AssertionError(
                f"`message_count` is {message_count!r} but the stream is recorded as carrying the initial "
                f"task plus {later} later message(s), so it cannot be fewer than {later + 1}. The counts "
                "have to agree or one of them is not counting the stream under test"
            )
        turn_count = stream.get("turn_count")
        if not isinstance(turn_count, int) or turn_count <= 0:
            raise AssertionError(
                f"`turn_count` is {turn_count!r}; a stream that provoked no model turn delivered its "
                "messages nowhere observable, which is indistinguishable from a channel that accepted "
                "them and dropped them"
            )
        for key, consequence in (
            ("generator_disposed", "the async generator was not disposed"),
            ("query_closed", "the Query was not closed"),
        ):
            if stream.get(key) is not True:
                raise AssertionError(
                    f"{consequence} at attempt finalization. Each undisposed attempt holds an SDK "
                    "subprocess, so a run that retries a few times exhausts its resources without any "
                    "single attempt looking wrong — which is why this is checked per attempt rather "
                    "than per run"
                )

        # --- and it has to have been observed, not inferred --------------------
        observed_by = stream.get("observed_by")
        if not isinstance(observed_by, str) or not observed_by.strip():
            raise AssertionError(
                f"`observed_by` is {observed_by!r}; without naming what produced these counts there is "
                "nothing to distinguish a live stream from a static reading of the code"
            )
        lowered = observed_by.lower()
        refused = [
            token
            for token in ("grep", "mock", "stub", "fake", "source read", "code read", "inspection")
            if token in lowered
        ]
        if refused:
            raise AssertionError(
                f"`observed_by` is {observed_by!r}, which names {refused} as the source of these counts. "
                f"AC-T6 is a claim about the real SDK at {EXPECTED_CLAUDE_SDK_VERSION}: a mock accepts "
                "as many messages as it is handed by construction, and a source grep establishes that "
                "the code intends to push them, neither of which is evidence the provider accepted them"
            )

    # ---- W3-11 ---------------------------------------------------------

    def check_w3_11(self) -> None:
        """A retry moves pending input and nothing else (AC-T7).

        An in-process retry replaces the attempt — new Query, new input channel —
        while the run, the session and the operator's queue all continue. That
        makes it the moment steering is most likely to go wrong, in two opposite
        directions: a pending command stranded on the dead attempt is never
        delivered, and a confirmed one reattached to the new attempt is delivered
        twice. `deliveries_of_queued_command` is recorded as an integer because 0
        and 2 are both failures and a boolean would merge them into "not 1".

        The attempt identity has to actually change, or the retry under test did
        not happen and the anti-replay property was never exercised. The session
        has to NOT change, because a retry that starts a new session has discarded
        the context the queued instruction was written about.

        The last two are the honest-degradation cases. A handoff whose outcome
        cannot be determined is `unknown`; retrying it is the replay this check
        forbids elsewhere. And an abort landing during backoff must stop the
        sequence — a next attempt started after the operator aborted is the run
        continuing past its own cancellation.
        """
        retry = self._artifact("steering_retry")

        queued = retry.get("queued_command_id")
        if not isinstance(queued, str) or not queued.strip():
            raise AssertionError(
                f"`queued_command_id` is {queued!r}; without the ID that was pending across the retry "
                "the delivery count below is not attributable to any command"
            )
        deliveries = retry.get("deliveries_of_queued_command")
        if deliveries != 1:
            if deliveries == 0:
                raise AssertionError(
                    f"command {queued.strip()!r} was pending when the attempt was replaced and was never "
                    "delivered. Pending commands reattach to the new attempt; one stranded on the dead "
                    "attempt stays pending for the life of the run, so the operator waits on an "
                    "instruction that can no longer arrive"
                )
            raise AssertionError(
                f"command {queued.strip()!r} was delivered {deliveries!r} times across the retry, "
                "expected exactly 1. A mid-run instruction delivered twice is acted on twice, and the "
                "second copy arrives with no indication it is a repeat"
            )
        replayed = retry.get("confirmed_handoffs_replayed")
        if replayed != 0:
            raise AssertionError(
                f"`confirmed_handoffs_replayed` is {replayed!r}, expected 0. Only PENDING commands cross "
                "a retry: a confirmed handoff has already reached a model, and reattaching it to the new "
                "attempt replays input the run has already acted on"
            )
        if retry.get("session_preserved") is not True:
            raise AssertionError(
                "the session was not preserved across the retry. A new session discards the context the "
                "queued instruction was written about, so even a correctly reattached command is "
                "delivered to a run that has forgotten what it refers to"
            )
        before = retry.get("attempt_id_before")
        after = retry.get("attempt_id_after")
        for name, value in (("attempt_id_before", before), ("attempt_id_after", after)):
            if not isinstance(value, str) or not value.strip():
                raise AssertionError(
                    f"`{name}` is {value!r}; both attempt identities are required, because the property "
                    "under test is which attempt the input resolved against"
                )
        if before.strip() == after.strip():
            raise AssertionError(
                f"the attempt identity did not change across the retry (both {before.strip()!r}). Then no "
                "attempt was replaced, the reattachment path never ran, and the anti-replay property "
                "this check exists for was not exercised"
            )
        if retry.get("ambiguous_handoff_outcome") != "unknown":
            raise AssertionError(
                f"an ambiguous handoff was recorded as {retry.get('ambiguous_handoff_outcome')!r}, "
                "expected 'unknown'. A push that may or may not have reached the departing attempt is "
                "exactly the case where guessing is worse than reporting: 'delivered' claims a handoff "
                "nobody observed and 'pending' schedules the duplicate delivery"
            )
        if retry.get("abort_during_retry_started_next_attempt") is not False:
            raise AssertionError(
                "an abort arriving during retry backoff was followed by another attempt. Backoff is not a "
                "window in which cancellation is deferred; a next attempt started there is the run "
                "continuing past the point the operator stopped it"
            )

    # ---- W4-01 ---------------------------------------------------------

    def check_w4_01(self, *, emitted_ids: tuple[str, ...] = ()) -> None:
        """Wave 4's preflight: is this the build the other nine checks think it is?

        The same role W2-01 plays for wave 2, with two additions that are specific to
        wave 4 being the LAST evaluation:

        1. **Three prior waves, not one.** Wave 4's row closes all four evaluations,
           so waves 1, 2 and 3 must each be accepted, and each acceptance must be
           contained in what is deployed now. Wave 3 being unregistered in this
           revision is a named refusal rather than a shorter prerequisite list — see
           `_assert_prior_wave_accepted`.
        2. **The frontend is a third deployed component.** Wave 4's checks are
           browser checks, so the artifact under evaluation is a served SPA bundle. A
           current gateway behind a stale bundle is the normal half-deployment and no
           check that reads only the two container images can see it. The preflight's
           frontend revision must also agree with the `bundle_revision` the browser
           capture independently recorded: two sources naming the same bundle is
           evidence, and a disagreement names which one is stale.

        The browser identity is asserted here rather than in a browser check because
        it is a statement about authorization, not about the DOM. The capture only
        ever drove one identity, so it cannot establish that ordinary users are still
        gated — and "the fixture owner can drive the controls" plus "everyone else
        can too" is not the state this wave is allowed to be accepted in.
        """
        preflight = self._artifact("wave4_preflight")
        self._assert_fixture_identity(preflight, "wave4_preflight", self.config)

        # (0) What is running, per component. Everything below is relative to it.
        deployed = self._deployed_components(preflight)
        deployed_revisions = {name: entry["revision"] for name, entry in deployed.items()}

        # (1) The frontend, as its own deployed artifact.
        frontend = preflight["frontend"]
        if not isinstance(frontend, dict):
            raise AssertionError(
                f"'frontend' must be an object recording the deployed SPA's identity, got {frontend!r}. "
                "Wave 4's checks are browser checks, so the bundle being served is part of what is under "
                "evaluation rather than context for it"
            )
        missing = [key for key in WAVE4_FRONTEND_KEYS if key not in frontend]
        if missing:
            raise AssertionError(
                f"the deployed frontend identity is missing {sorted(missing)}; without all of "
                f"{list(WAVE4_FRONTEND_KEYS)} the bundle the browser loaded cannot be tied back to "
                "reviewed source"
            )
        frontend_revision = frontend["revision"]
        if not isinstance(frontend_revision, str) or not _GIT_REVISION_RE.match(frontend_revision):
            raise AssertionError(
                f"the frontend revision is {frontend_revision!r}, which is not a full 40-character git "
                "SHA. A branch name names whatever that ref happened to point at, and 'the dashboard is "
                "deployed' has to mean a specific commit"
            )
        # The served-asset evidence, not merely a claimed revision. An operator can
        # record any revision; what establishes which bundle is actually being served
        # is a comparison against the assets retrieved from the deployment.
        served = frontend["served_asset_evidence"]
        if not isinstance(served, dict) or served.get("verified") is not True:
            raise AssertionError(
                f"'frontend.served_asset_evidence' does not record a verified match ({served!r}). A "
                "recorded revision says which commit the operator believes is deployed; only a "
                "comparison against the assets actually retrieved from the deployment establishes it, "
                "and a stale bundle is exactly what this field exists to catch"
            )
        # The frontend must itself be contained in the deployment's other components'
        # history — it is built from the same repository, so a frontend revision that
        # is not an ancestor of the running backend is either from a branch or from
        # the future, and both mean these three artifacts are not one build.
        self._assert_contained_in(
            frontend_revision,
            subject=f"the deployed frontend revision {frontend_revision}",
            deployed_revisions=deployed_revisions,
            hint=(
                "A frontend built from a revision the running backend does not contain is a "
                "half-deployment: the SPA may call fields the gateway does not serve yet."
            ),
        )

        # The two independent records of which bundle was under test must agree.
        capture = self._artifact("browser_control_run")
        captured_bundle = str(capture.get("bundle_revision") or "").strip()
        if not captured_bundle:
            raise AssertionError(
                "the browser capture records no `bundle_revision`, so the preflight's frontend revision "
                "cannot be corroborated. One record naming a bundle is a claim; two independent records "
                "agreeing is evidence"
            )
        if captured_bundle != frontend_revision:
            raise AssertionError(
                f"the browser drove bundle {captured_bundle!r} but the preflight records the deployed "
                f"frontend as {frontend_revision!r}. One of the two is stale, and every wave-4 browser "
                "observation is about whichever bundle the browser actually loaded — so this has to be "
                "resolved rather than reconciled in favour of the preflight"
            )

        # (2) Prior waves, each accepted and each contained in what runs now.
        prior = preflight["prior_waves"]
        if not isinstance(prior, dict):
            raise AssertionError(
                f"'prior_waves' must be an object keyed by wave number, got {prior!r}. A single "
                "'waves_1_3_accepted' boolean cannot say which wave is unaccepted, and wave 3 being "
                "unregistered has a different owner from wave 1 being stale"
            )
        for wave in WAVE4_PRIOR_WAVES:
            record = prior.get(str(wave), prior.get(wave))
            if record is None:
                raise AssertionError(
                    f"no acceptance recorded for wave {wave}. Wave 4's evidence closes all four "
                    f"evaluations, so every earlier wave's acceptance is a prerequisite rather than "
                    f"context"
                )
            self._assert_prior_wave_accepted(
                wave, record, deployed_revisions=deployed_revisions
            )

        # (3) The dashboard story, merged and contained.
        merged = preflight["merged_revisions"]
        if not isinstance(merged, dict):
            raise AssertionError(f"'merged_revisions' must be an object keyed by story, got {merged!r}")
        for story, description in WAVE4_REQUIRED_STORIES.items():
            entry = merged.get(story)
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"no merged revision recorded for {story} ({description}); wave 4 evaluates that "
                    f"story's dashboard, so evidence gathered while it is unmerged describes a build "
                    f"without the feature under test"
                )
            if entry.get("merged") is not True:
                raise AssertionError(
                    f"{story} ({description}) is not recorded as merged: {entry.get('merged')!r}"
                )
            revision = entry.get("revision")
            if not isinstance(revision, str) or not _GIT_REVISION_RE.match(revision):
                raise AssertionError(
                    f"{story}'s merged revision is {revision!r}, which is not a full 40-character git "
                    f"SHA. {description} must be pinned to an exact commit, not to a moving ref"
                )
            self._assert_contained_in(
                revision,
                subject=f"{story}'s merged revision {revision} ({description})",
                deployed_revisions={**deployed_revisions, "frontend": frontend_revision},
                hint="A merged dashboard story that is not in the served bundle is not deployed.",
            )

        # (4) The frontend's gates, read out of their archived run documents.
        #
        # The control-path gates are NOT rechecked here — see the comment on
        # WAVE4_FRONTEND_GATE_RAW_DOCUMENTS. Wave 2's acceptance, asserted above, is
        # what carries them, and it carries them to a stricter standard than a second
        # implementation in this check could.
        gates = preflight["ci_gates"]
        if not isinstance(gates, dict):
            raise AssertionError(f"'ci_gates' must be an object keyed by gate name, got {gates!r}")
        absent = sorted(set(WAVE4_REQUIRED_CI_GATES) - set(gates))
        if absent:
            raise AssertionError(
                f"no result recorded for the required frontend gate(s) {absent}. The required set is "
                f"{list(WAVE4_REQUIRED_CI_GATES)}, named as `.github/workflows/gateway-ci.yml` names "
                "them; a gate absent from the record is indistinguishable from one that was never "
                "required, and any nonempty map of 'passed' values would otherwise demonstrate the "
                "operator's spelling rather than the build's gates"
            )
        for gate in WAVE4_REQUIRED_CI_GATES:
            entry = gates[gate]
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"the {gate!r} gate is recorded as {entry!r}; it must be an object carrying its "
                    "status, run identity, tested revision and the archived run document"
                )
            for key in ("status", "run_id", "run_url", "tested_revision", "raw"):
                if not entry.get(key):
                    raise AssertionError(
                        f"the {gate!r} gate is missing {key!r}. A bare pass/fail cannot say WHICH run "
                        "produced it or WHAT revision it tested, and both are required for it to be "
                        "evidence about this deployment"
                    )
            if entry["status"] != STATUS_PASSED:
                raise AssertionError(
                    f"the {gate!r} gate is recorded as {entry['status']!r} rather than {STATUS_PASSED!r} "
                    f"(run {entry['run_id']!r}); a merge with red required checks is a merge, not a "
                    "verified revision"
                )
            tested = entry["tested_revision"]
            if not isinstance(tested, str) or not _GIT_REVISION_RE.match(tested):
                raise AssertionError(
                    f"the {gate!r} gate records tested revision {tested!r}, which is not a full "
                    "40-character git SHA. Which commit a gate tested is the whole of what makes it "
                    "relevant"
                )
            # The archive, PARSED — not searched and not trusted. This is W2-01's
            # correction applied here rather than re-derived: a substring scan over a
            # dumped body finds the run id, the revision and the job name in a RED
            # document exactly as readily as in a green one, so the conclusion has to be
            # read at its location and the named job's own conclusion with it.
            raw = self._assert_raw_metadata(
                entry["raw"],
                subject=f"the {gate!r} gate",
                expected=WAVE4_FRONTEND_GATE_RAW_DOCUMENTS,
            )
            try:
                run = parse_github_run(raw["run"]["body"], required_job=gate)
            except ProvenanceParseError as error:
                raise AssertionError(
                    f"the {gate!r} gate: the archived run document (retrieved by "
                    f"{raw['run']['command']!r}) does not establish a passing run of that job: {error}"
                ) from error
            if run["run_id"] != str(entry["run_id"]):
                raise AssertionError(
                    f"the {gate!r} gate records run_id {entry['run_id']!r}, but the archived run document "
                    f"reports databaseId {run['run_id']!r}. The recorded field is a summary of that "
                    "response, so a disagreement means the summary describes a different run than the "
                    "one archived"
                )
            # `gateway-ci.yml` uploads no checkout artifact, so the run's head is the
            # only available statement of what it tested. Containment rather than
            # equality, for the reason W2-01 established: a `pull_request` job builds a
            # merge of the head into its base, and that merge CONTAINS the head. This is
            # honestly looser than wave 2's artifact binding, which is why the constant
            # above says so instead of implying otherwise.
            self._assert_contained_in(
                run["head_revision"],
                subject=f"the {gate!r} gate's archived head revision {run['head_revision']}",
                deployed_revisions={"tested_revision": tested},
                hint=(
                    "The gate's recorded subject must contain the run that produced it; a head the "
                    "tested revision does not contain means the record and the run disagree."
                ),
            )
            # A green gate on a revision the deployment does not contain tested
            # different code. This is the "merge went green, then something else
            # shipped" case.
            self._assert_contained_in(
                tested,
                subject=f"the {gate!r} gate's tested revision {tested}",
                deployed_revisions={**deployed_revisions, "frontend": frontend_revision},
                hint="A gate that passed on code the deployment does not contain did not test this build.",
            )

        # (5) Fixture scope: the browser identity is the owner, others still gated.
        identity = preflight["browser_identity"]
        if not isinstance(identity, dict):
            raise AssertionError(
                f"'browser_identity' must be an object describing the identity the browser drove, got "
                f"{identity!r}"
            )
        if identity.get("role") != "owner":
            raise AssertionError(
                f"the browser drove the {identity.get('role')!r} identity, but wave 4's row requires the "
                "isolated fixture browser identity to be the run's OWNER. Observations of what a "
                "non-owner's dashboard renders cannot establish what the owner's does"
            )
        if identity.get("is_run_owner") is not True:
            raise AssertionError(
                f"'browser_identity.is_run_owner' is {identity.get('is_run_owner')!r}; the identity must "
                "be observed to own the run under test, not merely labelled 'owner'"
            )
        if preflight.get("ordinary_users_gated") is not True:
            raise AssertionError(
                f"'ordinary_users_gated' is {preflight.get('ordinary_users_gated')!r}. Wave 4's row "
                "requires ordinary users to remain gated pending acceptance: a dashboard that is live for "
                "everyone before its evaluation passed has shipped an unevaluated control surface"
            )
        if preflight.get("ordinary_flags_off") is not True:
            raise AssertionError(
                f"'ordinary_flags_off' is {preflight.get('ordinary_flags_off')!r}; DP-INV-1 requires the "
                "ordinary gateway/worker/SPA flags to stay off, and a fixture that got its observations by "
                "enabling them more widely produced an invalid pass rather than a weaker one"
            )

        # (6) The report's own inventory: every check this wave declares is present.
        inventory = tuple(emitted_ids)
        expected = tuple(spec.check_id for spec in WAVE_CHECKS[4])
        if inventory and set(inventory) != set(expected):
            raise AssertionError(
                f"this run's check inventory is {sorted(inventory)} but wave 4 declares {sorted(expected)}. "
                "A report missing a check cannot be complete, and one carrying an extra ID is describing "
                "a different wave"
            )

    # ---- wave 4 (#3966): the dashboard's own evidence -------------------
    #
    # These four read the captured browser run. Every assertion below is about
    # something the BROWSER did — a destination it requested, an interval it
    # waited, a string it rendered — because the claims in question are claims
    # about a deployed bundle, and no amount of reading the component source can
    # establish what the deployed bundle did.

    def _browser_run(self) -> dict:
        """The captured Playwright run, with its provenance established first.

        Provenance before content, deliberately: a capture that does not name the
        bundle it drove cannot answer for the deployment, and checking its
        observations first would mean reporting detailed DOM findings about an
        unknown artifact.
        """
        capture = self._artifact("browser_control_run")
        revision = str(capture.get("bundle_revision") or "").strip()
        if not revision:
            raise PrerequisiteMissingError(
                "the browser capture records no `bundle_revision`, so its observations cannot be tied to "
                "a deployed frontend asset. An untraceable capture is indistinguishable from one taken "
                "against a developer's local dev server"
            )
        gateway = str(capture.get("gateway_url") or "").strip()
        if not gateway:
            raise PrerequisiteMissingError(
                "the browser capture records no `gateway_url`; which deployment the browser drove is part "
                "of the observation, not context for it"
            )
        configured = str(self.config.get("gateway_url") or "").strip()
        if configured and gateway.rstrip("/") != configured.rstrip("/"):
            raise AssertionError(
                f"the browser capture drove {gateway!r} but this evaluation's fixture is {configured!r}. "
                "Observations from another deployment cannot answer for this one"
            )
        if not _parse_timestamp(capture.get("captured_at")):
            raise AssertionError(
                f"the browser capture's `captured_at` is {capture.get('captured_at')!r}, which is not an "
                "ISO-8601 instant. Without an orderable time the capture cannot be shown to postdate the "
                "revision it claims to describe"
            )
        if not str(capture.get("spec_digest") or "").strip():
            raise AssertionError(
                "the browser capture records no `spec_digest`, so which scenario produced it is unknown. "
                "A weakened spec and the real one would leave identical evidence"
            )
        return capture

    @staticmethod
    def _zero_render(capture: dict, key: str, label: str) -> None:
        """Assert one fail-closed render produced no controls and sent no commands.

        Both halves matter and neither implies the other: zero nodes with a command
        request means an invisible control still acted, and zero requests with
        rendered nodes means the operator was offered a button that silently did
        nothing. AC-F3 requires both.
        """
        observation = capture.get(key)
        if not isinstance(observation, dict):
            raise AssertionError(
                f"`{key}` must be an object recording what the {label} render produced, got "
                f"{observation!r}"
            )
        nodes = observation.get("control_nodes")
        requests = observation.get("command_requests")
        for name, value in (("control_nodes", nodes), ("command_requests", requests)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise AssertionError(
                    f"`{key}.{name}` is {value!r}, expected a counted integer. A boolean or a missing "
                    "count cannot distinguish 'none' from 'not measured'"
                )
        if nodes != 0:
            raise AssertionError(
                f"with the feature {label}, the browser found {nodes} control node(s) at "
                f"/activity?id=<invocation_id>. AC-F3 requires the controls not to render while loading "
                "or on error either, because a control offered before the flag is known is a control "
                "offered when it might be off"
            )
        if requests != 0:
            raise AssertionError(
                f"with the feature {label}, the browser issued {requests} command request(s). A gated "
                "feature that still reaches the command endpoint is not gated"
            )

    def check_w4_02(self) -> None:
        """AC-F3: the controls stay absent unless the flag is explicitly on.

        The three negative renders are the whole point. "Flag off" is the easy case;
        the two that catch real fail-open bugs are "still loading" and "backend
        error", where an implementation that treats an absent answer as permission
        renders controls that may not be permitted.
        """
        capture = self._browser_run()

        for key, label in (
            ("flag_off", "off"),
            ("flag_loading", "still loading"),
            ("flag_error", "erroring"),
        ):
            self._zero_render(capture, key, label)

        # The positive case: exactly the advertised verbs, no more.
        advertised = capture.get("advertised_capabilities")
        rendered = capture.get("rendered_controls")
        if not isinstance(advertised, dict):
            raise AssertionError(
                f"`advertised_capabilities` must be the capability object the gateway served, got "
                f"{advertised!r}"
            )
        if not isinstance(rendered, list):
            raise AssertionError(
                f"`rendered_controls` must be the list of verbs the browser actually found, got "
                f"{rendered!r}"
            )
        offered = {str(verb) for verb in rendered}
        allowed = {str(verb) for verb, value in advertised.items() if value is True}
        extra = offered - allowed
        if extra:
            raise AssertionError(
                f"the dashboard offered {sorted(extra)}, which this run does not advertise as available "
                f"(advertised: {sorted(allowed)}). Offering an unadvertised verb produces a 501 in the "
                "operator's face and implies a capability the deployment does not have"
            )
        if not offered:
            raise PrerequisiteMissingError(
                "the capture shows no controls rendered in the positive case, so capability gating was "
                "never exercised in the direction that can fail open. A fixture whose run advertises no "
                "verbs cannot evidence AC-F3's positive half"
            )

        for key, who in (
            ("nonowner_submit_blocked", "a non-owner"),
            ("terminal_submit_blocked", "a terminal or unavailable run"),
        ):
            if capture.get(key) is not True:
                raise AssertionError(
                    f"`{key}` is {capture.get(key)!r}: the capture does not establish that {who} cannot "
                    "submit a command. Rendering is not authorization, so this has to be observed at the "
                    "request level rather than inferred from a hidden button"
                )

    def check_w4_04(self) -> None:
        """AC-P4: pause honestly described, from fresh server state.

        The two failures this exists to catch are a UI that claims `paused` when the
        server said `pause_requested`, and one that reports quiescence it never
        observed. Both are cases of the dashboard being more confident than its
        source, which is why the phase sequence has to come from polled server
        state rather than from a snapshot the page already held.
        """
        capture = self._browser_run()

        sequence = capture.get("phase_sequence")
        if not isinstance(sequence, list) or not sequence:
            raise AssertionError(
                f"`phase_sequence` must be the nonempty list of phases the browser rendered in order, "
                f"got {sequence!r}"
            )
        phases = [str(entry) for entry in sequence]
        required = ("running", "pause_requested", "paused", "running")
        position = 0
        for wanted in required:
            try:
                position = phases.index(wanted, position) + 1
            except ValueError:
                raise AssertionError(
                    f"the browser never rendered {wanted!r} in order; observed {phases}. The full "
                    f"{list(required)} transition is what distinguishes a pause that was requested and "
                    "took effect from one the UI merely asserted"
                ) from None

        # The specific lie AC-P4 forbids: `paused` shown before the server said so.
        if phases.index("paused") < phases.index("pause_requested"):
            raise AssertionError(
                f"the dashboard rendered `paused` before `pause_requested` ({phases}). Reaching paused "
                "without passing through requested means the UI decided the run was paused rather than "
                "reporting that it was"
            )

        if capture.get("pause_copy_mentions_spend") is not True:
            raise AssertionError(
                "the pause copy the browser rendered does not mention that spend may continue. AC-P4 "
                "requires it: an operator who reads 'paused' as 'billing stopped' will leave a run "
                "parked for hours believing it costs nothing"
            )

        reason = capture.get("active_tool_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise AssertionError(
                f"`active_tool_reason` is {reason!r}; the capture must record the tool-activity text the "
                "browser rendered, because whether an unobserved count reads as 'unknown' or as 'none' "
                "is the check"
            )
        lowered = reason.lower()
        if "unknown" not in lowered and "none reported" not in lowered:
            raise AssertionError(
                f"the rendered tool-activity text {reason!r} neither reports an unknown count as unknown "
                "nor qualifies a zero as merely reported. An unproven zero reads as 'no tools running', "
                "which is the false quiescence claim AC-P4 forbids"
            )
        if "no tools running" in lowered or "nothing is running" in lowered:
            raise AssertionError(
                f"the rendered tool-activity text {reason!r} asserts quiescence outright. The worker "
                "reports a count it may not have, so the UI can report what it was told and nothing more"
            )

    def check_w4_07(self) -> None:
        """Gate: the live JSON, the frontend types and the backend schema agree.

        Field by field at each level, in the direction that matters: every field the
        BACKEND declares must be present in the LIVE response. The reverse is not a
        failure — a gateway may add a field before the SPA reads it — but a declared
        field the live response omits is an `undefined` in the dashboard, which
        renders as a blank rather than an error.
        """
        owner = self._token("owner")
        run_id = self._require("live_run_id")
        observation = self.probe.request(
            "GET", f"/activity/invocations/{run_id}/agent/state", role="owner", token=owner
        )
        if observation.status != 200:
            raise AssertionError(
                f"GET agent/state returned {observation.status}, expected 200; the live contract cannot "
                "be compared against a response that was not served"
            )
        body = self._body_of(observation)

        # Transcribed from modules/gateway/src/activity/control_schemas.py, which is
        # the authority the frontend service was derived from.
        state_fields = (
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
        missing = [name for name in state_fields if name not in body]
        if missing:
            raise AssertionError(
                f"the live control-state response is missing {missing}, which control_schemas.py "
                f"declares and the frontend destructures"
            )

        capabilities = body.get("capabilities")
        if not isinstance(capabilities, dict):
            raise AssertionError(f"`capabilities` must be an object, got {capabilities!r}")
        verb_missing = [verb for verb in CONTROL_VERBS if verb not in capabilities]
        if verb_missing:
            raise AssertionError(
                f"`capabilities` omits {verb_missing}. An absent verb key is indistinguishable from "
                "`false` to a client that reads it, so the UI cannot tell 'not supported' from "
                "'not answered'"
            )

        # The pod's own coordinates must not appear at any level of the public body.
        # This is the response the browser receives, so a leak here is a leak to the
        # browser regardless of what the UI chooses to render.
        for banned in ("pod_ip", "pod_address", "pod_port", "control_token", "token", "address"):
            if banned in body:
                raise AssertionError(
                    f"the public control-state response carries {banned!r}. control_schemas.py has no "
                    "field for a pod address, port or token precisely so this cannot happen; a browser "
                    "that receives one has it in devtools, in logs and in any error report"
                )

        entries = body.get("commands")
        if not isinstance(entries, list):
            raise AssertionError(f"`commands` must be a list, got {entries!r}")
        entry_fields = ("command_id", "action", "status", "accepted_at", "delivered_at", "reason")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AssertionError(f"`commands[{index}]` must be an object, got {entry!r}")
            absent = [name for name in entry_fields if name not in entry]
            if absent:
                raise AssertionError(
                    f"`commands[{index}]` is missing {absent}; the acknowledgement contract is what the "
                    "dashboard keys its per-command status on"
                )

    def check_w4_08(self) -> None:
        """Gate: the polling lifecycle and the delivery vocabulary, as observed.

        Measured intervals rather than a configured constant. `CONTROL_POLL_MS` in
        the source says what the code intends; only a timed browser run says what
        the deployed bundle does, and the failure modes here — a poll that never
        stops, a retry that never backs off — are invisible to a source read and
        expensive in production.
        """
        capture = self._browser_run()
        intervals = capture.get("poll_intervals_ms")
        if not isinstance(intervals, list) or len(intervals) < 2:
            raise AssertionError(
                f"`poll_intervals_ms` must record at least two measured intervals, got {intervals!r}. "
                "One timestamp is not an interval, and a configured constant is not an observation"
            )
        for value in intervals:
            if not isinstance(value, (int, float)):
                raise AssertionError(f"`poll_intervals_ms` contains {value!r}, expected numbers")
            # Generous tolerance: this is a real browser on a real network, and the
            # claim under test is "about every 2 seconds", not a precise clock.
            if not 1000 <= value <= 4000:
                raise AssertionError(
                    f"a measured poll interval of {value!r}ms is outside the 2-second contract "
                    "(1000-4000ms tolerated). Polling faster multiplies load by every open modal; "
                    "polling slower makes the operator act on stale state"
                )

        for key, what in (
            ("polled_while_hidden", "while the document was hidden"),
            ("polled_after_close", "after the modal closed"),
            ("polled_after_terminal", "after the run reached a terminal state"),
        ):
            value = capture.get(key)
            if value is not False:
                raise AssertionError(
                    f"`{key}` is {value!r}: the capture does not establish that polling stopped {what}. "
                    "It must be an observed `false`, because a poll that continues there bills a "
                    "background tab forever for state nobody is reading"
                )

        backoff = capture.get("backoff_intervals_ms")
        if not isinstance(backoff, list) or len(backoff) < 2:
            raise AssertionError(
                f"`backoff_intervals_ms` must record at least two intervals observed while the endpoint "
                f"was failing, got {backoff!r}"
            )
        if not all(
            type(value) in (int, float) and math.isfinite(value) and value > 0 for value in backoff
        ) or any(
            later < earlier or (later == earlier and earlier < 30000)
            for earlier, later in zip(backoff, backoff[1:])
        ):
            raise AssertionError(
                f"the observed error intervals {backoff!r} do not increase. Retrying a failing control "
                "endpoint at the same rate is what turns one backend problem into a load problem"
            )

        if capture.get("detail_refreshed_after_command") is not True:
            raise AssertionError(
                "the capture does not establish that the invocation detail refreshed after a command. "
                "Without it the modal keeps showing its pre-command snapshot, so the panel and the "
                "record beside it contradict each other"
            )

        steer_statuses = capture.get("steer_status_sequence")
        if isinstance(steer_statuses, list) and steer_statuses:
            rendered = " ".join(str(entry).lower() for entry in steer_statuses)
            if "delivered" in rendered and "pending" not in rendered:
                raise AssertionError(
                    f"the DOM showed a delivered steer with no preceding pending state ({steer_statuses}). "
                    "Labelling an enqueue as delivered tells the operator the agent has the instruction "
                    "when only the queue does"
                )

    # ---- wave 4 consolidation: earlier waves' criteria, rechecked ---------
    #
    # W4-03, W4-05, W4-06 and W4-09 each repeat a family of criteria an earlier
    # wave evidenced. They are NOT re-running those waves' probes: the evidence
    # already exists, and asking the fixture to reproduce a whole abort family
    # would mutate the run the other checks describe.
    #
    # What they establish instead is the thing wave 4's row actually asks and no
    # earlier wave could answer: is that evidence still about the build that is
    # deployed NOW? Two ways for it not to be, both checked in
    # `_assert_evidence_not_stale` — a revision the deployment does not contain,
    # and a timestamp predating a later change to a surface the criteria cover.
    #
    # So a consolidating check can fail for a reason the original wave passed on,
    # which is correct and is the point. "It passed in wave 3" and "it describes
    # what is running" are different claims, and only the second one lets wave 4
    # close.

    def _consolidate(self, check_id: str, *, proofs: tuple[str, ...] = ()) -> dict:
        """The shared spine of the four consolidating checks.

        Each of them needs the same four things established in the same order, and
        the order is load-bearing:

        1. **Identity**, so the artifact is about this fixture and not a previous
           run's. Checked first because every assertion after it is meaningless
           otherwise.
        2. **The evaluation it came from**, so evidence filed against the wrong wave
           cannot be counted toward this one.
        3. **Currency** — contained in the deployment, and not predating a change to
           a surface it covers. This is wave 4's own contribution.
        4. **Coverage**, so every acceptance ID this check carries has a passing
           entry with evidence behind it, and no entry it does not carry.

        The named row-specific proofs come last, via `_assert_named_proofs`: they are
        the individual observations each row lists, and a row that lists four proofs
        must not be satisfiable by one of them.
        """
        source = WAVE4_CONSOLIDATED_SOURCES[check_id]
        payload = self._artifact(source["artifact"])
        self._assert_fixture_identity(payload, source["artifact"], self.config)

        expected_evaluation = WAVE_EVALUATIONS.get(source["wave"])
        recorded = str(payload.get("evaluation") or "")
        if expected_evaluation and recorded != expected_evaluation:
            raise AssertionError(
                f"{check_id}: this evidence names evaluation {recorded!r} but the criteria it carries "
                f"were evidenced by wave {source['wave']} under evaluation {expected_evaluation!r}. "
                f"Evidence attached to a different evaluation cannot be consolidated into this one"
            )

        # Currency needs to know what is deployed, and that lives in the wave's
        # preflight. Reading it here rather than taking a `deployed_revisions`
        # argument keeps each check independently runnable — and an absent preflight
        # makes this not_run, which is right: without knowing what is deployed,
        # staleness is unanswerable rather than false.
        preflight = self._artifact("wave4_preflight")
        deployed = {
            name: entry["revision"]
            for name, entry in self._deployed_components(preflight).items()
        }
        frontend = preflight.get("frontend")
        if isinstance(frontend, dict) and isinstance(frontend.get("revision"), str):
            deployed["frontend"] = frontend["revision"]

        self._assert_evidence_not_stale(payload, check_id, deployed_revisions=deployed)
        self._assert_criteria_cover(payload, check_id, CHECK_ACCEPTANCE_IDS[check_id])
        if proofs:
            self._assert_named_proofs(payload, check_id, proofs)
        return payload

    # ---- W4-03 ---------------------------------------------------------

    def check_w4_03(self) -> None:
        """AC-T1..T8 and AC-S8: steering, as delivered and as still current.

        The browser half is wave 4's own — a 202 to a schema-valid steer, then a
        pending state tied to the command_id and a matching delivery marker — and it
        is asserted here from the capture rather than trusted from the artifact,
        because that is the half a browser can actually observe.

        The rest is wave 3's: FIFO order, retry delivery, the pending cap, the
        SDK-bound text, the fixture pivot and the merged test-PR assertion. Those are
        named individually because the row names them individually; a single
        `steering_proven` boolean cannot say which one was never made, and they do not
        all have the same owner.
        """
        payload = self._consolidate(
            "W4-03",
            proofs=(
                "fifo_order_proven",
                "retry_delivery_proven",
                "pending_cap_proven",
                "sdk_bound_text_proven",
            ),
        )

        # The W3-10 pivot and the merged test PR. Objects rather than booleans: the
        # row demands they EXECUTE, and "true" cannot distinguish an executed pivot
        # from an intention to pivot.
        pivot = payload.get("fixture_pivot")
        if not isinstance(pivot, dict) or pivot.get("executed") is not True:
            raise AssertionError(
                f"W4-03: 'fixture_pivot' does not record an executed pivot ({pivot!r}). W3-10's pivot is "
                "an action taken against the fixture, so a boolean claim that it happened is the "
                "substitute for evidence this check refuses"
            )
        test_pr = payload.get("merged_test_pr")
        if not isinstance(test_pr, dict):
            raise AssertionError(
                f"W4-03: 'merged_test_pr' must be an object identifying the PR the steered agent opened, "
                f"got {test_pr!r}"
            )
        if test_pr.get("merged") is not True or not test_pr.get("url"):
            raise AssertionError(
                f"W4-03: the test PR is recorded as merged={test_pr.get('merged')!r} at "
                f"url={test_pr.get('url')!r}. W3-10 requires a merged PR a reviewer can open: that PR "
                "existing is the end-to-end proof that a steer reached a real agent and changed what it "
                "did"
            )

        # The browser half, from the capture. Observed, not asserted.
        capture = self._browser_run()
        request = capture.get("steer_request")
        if not isinstance(request, dict):
            raise AssertionError(
                f"W4-03: the capture's `steer_request` must record the request the browser sent, got "
                f"{request!r}"
            )
        if request.get("status") != 202:
            raise AssertionError(
                f"W4-03: the browser's steer was answered {request.get('status')!r}, expected 202. AC-T1 "
                "is about an ACCEPTED steer; any other status means the dashboard's own submission path "
                "does not work, whatever the backend suite proves in isolation"
            )
        path = str(request.get("path") or "")
        if not path.endswith("/agent/steer"):
            raise AssertionError(
                f"W4-03: the browser posted its steer to {path!r}, which is not an agent steer route. A "
                "202 from the wrong destination is a 202 from something else"
            )

        statuses = capture.get("steer_status_sequence")
        if not isinstance(statuses, list) or not statuses:
            raise AssertionError(
                f"W4-03: `steer_status_sequence` must be the nonempty list of per-command statuses the "
                f"DOM rendered in order, got {statuses!r}"
            )
        rendered = [str(entry).lower() for entry in statuses]
        if "pending" not in rendered:
            raise AssertionError(
                f"W4-03: the DOM never rendered a pending state for the steer ({statuses}). AC-T1 "
                "requires the pending state tied to the command_id: without it the operator cannot tell "
                "an accepted instruction from a lost one"
            )
        if not any("deliver" in entry for entry in rendered):
            raise AssertionError(
                f"W4-03: the DOM never rendered a delivery marker for the steer ({statuses}). An "
                "instruction that stays pending forever is indistinguishable from one the agent never "
                "received, and AC-T1 is satisfied by the delivery, not by the acknowledgement"
            )
        if rendered.index("pending") > next(
            index for index, entry in enumerate(rendered) if "deliver" in entry
        ):
            raise AssertionError(
                f"W4-03: the DOM showed delivery before pending ({statuses}). The order is the claim: "
                "delivered-then-pending means the UI is not tracking this command's real progress"
            )

    # ---- W4-05 ---------------------------------------------------------

    def check_w4_05(self) -> None:
        """AC-A1..A12: abort, and the row it actually left behind.

        All twelve criteria, consolidated from wave 3 — but the assertion this check
        adds beyond coverage is `completed_at_observed`. The row says "the actual row
        has completed_at", and that wording is doing real work: a dashboard rendering
        a terminal state and a record that IS terminal are different things, and the
        second is what every later cost and stats read depends on.
        """
        payload = self._consolidate(
            "W4-05",
            proofs=(
                "cancel_left_run_untouched",
                "confirmed_abort_terminal",
                "repeat_and_double_abort",
                "stats_writer_assertions",
            ),
        )

        # Exactly one finalized comment. Not "at least one": AC-A6's failure mode is
        # a duplicate, which is a second comment on someone's PR, and >= would accept
        # precisely the thing being forbidden.
        count = payload.get("finalized_comment_count")
        if not isinstance(count, int) or isinstance(count, bool):
            raise AssertionError(
                f"W4-05: 'finalized_comment_count' is {count!r}, expected a counted integer. A boolean "
                "cannot distinguish one finalized comment from several"
            )
        if count != 1:
            raise AssertionError(
                f"W4-05: {count} finalized comment(s) were observed, expected exactly 1. Zero means the "
                "abort left no record on the work it stopped; more than one means a duplicate comment on "
                "a real PR, which is the user-visible defect this criterion exists to prevent"
            )

        renderers = payload.get("aborted_renderers")
        if not isinstance(renderers, dict) or not renderers:
            raise AssertionError(
                f"W4-05: 'aborted_renderers' must be a nonempty object recording each surface that "
                f"renders the aborted state and what it showed, got {renderers!r}. An empty map would "
                "satisfy every per-renderer assertion vacuously"
            )
        unrendered = sorted(name for name, value in renderers.items() if value is not True)
        if unrendered:
            raise AssertionError(
                f"W4-05: {unrendered} did not render the aborted state. An abort the UI shows as failed "
                "or as still running misattributes a deliberate stop to a fault, which is the wrong "
                "signal to every operator reading it"
            )

        # The row itself, read rather than claimed.
        observed = payload.get("completed_at_observed")
        if not isinstance(observed, dict):
            raise AssertionError(
                f"W4-05: 'completed_at_observed' must be an object recording the read of the actual row, "
                f"got {observed!r}. The row's terminal timestamp is the fact this criterion is about, so "
                "a boolean here would be a claim standing in for the read"
            )
        for key in ("run_id", "status", "completed_at"):
            if not observed.get(key):
                raise AssertionError(
                    f"W4-05: 'completed_at_observed.{key}' is missing or empty. The read has to identify "
                    "WHICH row it saw and what that row said, or it cannot be distinguished from a read "
                    "of a different run"
                )
        if str(observed.get("status")) != ABORTED_STATUS:
            raise AssertionError(
                f"W4-05: the observed row's status is {observed.get('status')!r}, expected "
                f"{ABORTED_STATUS!r}. A run the UI calls aborted whose record says otherwise will be "
                "counted as something else by every stats and spend query that reads the record"
            )
        if _parse_timestamp(observed.get("completed_at")) is None:
            raise AssertionError(
                f"W4-05: the observed row's 'completed_at' is {observed.get('completed_at')!r}, which is "
                "not an ISO-8601 instant. An unparseable terminal timestamp is not a terminal timestamp"
            )

    # ---- W4-06 ---------------------------------------------------------

    def check_w4_06(self) -> None:
        """AC-S1..S7: the security matrix, repeated, plus every destination the
        browser actually requested.

        The consolidation half is wave 3's matrix rechecked for currency. Wave 4's own
        addition is the capture: the matrix probes what the GATEWAY does, and this
        check additionally establishes what the BUNDLE does — which destinations it
        chose and what it put in the bodies. A gateway that refuses a direct pod
        request is necessary and not sufficient, because a bundle that tries one has
        already put a pod address into the browser.
        """
        payload = self._consolidate("W4-06", proofs=("non_gateway_probe_blocked",))

        # The row says the bundle scan is supplemental ONLY. Required as an explicit
        # acknowledgement rather than allowed to be absent: a matrix whose evidence
        # is a static scan of the built assets has not probed a deployment at all,
        # and absence would let that pass silently.
        if payload.get("bundle_scan_supplemental") is not True:
            raise AssertionError(
                f"W4-06: 'bundle_scan_supplemental' is {payload.get('bundle_scan_supplemental')!r}. The "
                "wave-4 row admits the bundle scan as supplemental evidence only, so this has to be an "
                "explicit acknowledgement: a static scan cannot establish what a deployment refuses"
            )

        capture = self._browser_run()
        destinations = capture.get("request_destinations")
        if not isinstance(destinations, list) or not destinations:
            raise AssertionError(
                f"W4-06: `request_destinations` must be the nonempty list of every URL the browser "
                f"requested while driving the controls, got {destinations!r}. An empty list means either "
                "nothing was captured or nothing was requested, and both make the per-destination "
                "assertions vacuous"
            )
        gateway = str(self.config.get("gateway_url") or capture.get("gateway_url") or "").rstrip("/")
        offsite = [
            str(url)
            for url in destinations
            if not str(url).startswith(gateway + "/") and str(url) != gateway
        ]
        if offsite:
            raise AssertionError(
                f"W4-06: the browser requested {offsite}, which are not on the gateway {gateway!r}. "
                "AC-S1 requires the controls to target gateway activity routes only — a control request "
                "to anything else is a control path that does not go through the gateway's authorization"
            )
        non_activity = [
            str(url) for url in destinations if "/activity/" not in str(url)
        ]
        if non_activity:
            raise AssertionError(
                f"W4-06: the browser sent control traffic to {non_activity}, which are not activity "
                "routes. Being on the right host is not the same as being on the route whose "
                "authorization was evaluated"
            )

        for key, what in (
            ("request_bodies_contain_pod_address", "a pod address"),
            ("request_bodies_contain_token", "a control token"),
        ):
            value = capture.get(key)
            if value is not False:
                raise AssertionError(
                    f"W4-06: `{key}` is {value!r}: the capture does not establish that no request body "
                    f"carried {what}. It must be an observed `false` — a secret the browser sends is in "
                    "devtools, in proxy logs and in any error report, and 'not measured' is not 'absent'"
                )

        if capture.get("spoofed_identity_rejected") is not True:
            raise AssertionError(
                "W4-06: the capture does not establish that a spoofed identity was rejected. AC-S5 is "
                "about what the deployment does when the browser lies about who it is, which cannot be "
                "inferred from the honest requests succeeding"
            )

    # ---- W4-09 ---------------------------------------------------------

    def check_w4_09(self) -> None:
        """AC-F1, AC-F2: the flag-off comparison rerun, and live stats provenance.

        Two independent questions, deliberately not collapsed:

        * **Parity** — with the flag off and with it on but no command issued, the
          runtime behaves identically. The comparison must be rerun with the FINAL
          code, which is what `_consolidate`'s currency assertions establish.
        * **Provenance** — the stats fields came from the live endpoint, not a mock.
          A schema can match perfectly on fabricated data, so matching keys is not
          evidence of a live read and has to be asserted separately.

        The row also forbids enabling ordinary flags as a testing shortcut. That is
        not a weaker form of a pass: observations obtained that way describe a
        configuration DP-INV-1 forbids, so they are invalid rather than partial.
        """
        payload = self._consolidate("W4-09")

        off = str(payload.get("flag_off_events_digest") or "")
        on = str(payload.get("flag_on_events_digest") or "")
        if not off or not on:
            raise AssertionError(
                f"W4-09: the runtime comparison records digests off={off!r} on={on!r}; both halves are "
                "required, because a comparison with one side missing is not a comparison"
            )
        if off != on:
            raise AssertionError(
                f"W4-09: the flag-off event digest {off!r} differs from the flag-on/no-command digest "
                f"{on!r}. AC-F1 is that enabling the flag without issuing a command changes nothing: a "
                "difference here means the feature alters runtime behaviour for every run merely by "
                "being switched on"
            )
        differing = payload.get("differing_fields")
        if not isinstance(differing, list):
            raise AssertionError(
                f"W4-09: 'differing_fields' must be a list (empty when the runs match), got "
                f"{differing!r}. An absent list cannot be distinguished from an unmeasured one"
            )
        if differing:
            raise AssertionError(
                f"W4-09: the two runs differ in {sorted(str(name) for name in differing)} even though "
                "their digests were recorded as equal. The artifact contradicts itself, so neither half "
                "can be relied on"
            )
        if payload.get("ordinary_flags_off") is not True:
            raise AssertionError(
                f"W4-09: 'ordinary_flags_off' is {payload.get('ordinary_flags_off')!r}. The row forbids "
                "ordinary flag enablement as a testing shortcut: a comparison obtained by switching the "
                "ordinary flags on describes a configuration DP-INV-1 forbids, which makes the "
                "observation invalid rather than merely weaker"
            )

        # Provenance: a live read, from the deployment under evaluation.
        source = payload.get("stats_source")
        if not isinstance(source, dict):
            raise AssertionError(
                f"W4-09: 'stats_source' must be an object recording where these stats came from, got "
                f"{source!r}. 'live' versus 'mocked' is the question, and a schema match cannot answer it"
            )
        if source.get("live") is not True:
            raise AssertionError(
                f"W4-09: 'stats_source.live' is {source.get('live')!r}. The row requires the live stats "
                "schema and provenance: a unit fixture can reproduce every field name of "
                "RunStatsResponse on invented numbers, so the fields matching is not evidence that "
                "anything was served"
            )
        endpoint = str(source.get("endpoint") or "")
        if "agent-run-stats" not in endpoint:
            raise AssertionError(
                f"W4-09: 'stats_source.endpoint' is {endpoint!r}, which is not the agent-run-stats "
                "endpoint. Which URL produced these numbers is part of the provenance claim"
            )
        if source.get("status") != 200:
            raise AssertionError(
                f"W4-09: the live stats read returned {source.get('status')!r}, expected 200. A "
                "non-200 response did not serve the schema being compared"
            )

        # Parity against the CURRENT response model, read from the deployed source
        # rather than transcribed. W2-09's transcription is the right tool for a live
        # probe; here the claim is specifically "the complete CURRENT
        # RunStatsResponse", which a hardcoded list stops being the moment a field is
        # added.
        declared = self._stats_response_fields()
        recorded = payload.get("stats_response_keys")
        if not isinstance(recorded, list):
            raise AssertionError(
                f"W4-09: 'stats_response_keys' must be the list of top-level keys the live response "
                f"carried, got {recorded!r}"
            )
        observed = {str(key) for key in recorded}
        missing = sorted(declared - observed)
        if missing:
            raise AssertionError(
                f"W4-09: the live stats response omits {missing}, which the deployed stats schema "
                "declares. Every one is an `undefined` in the dashboard, which renders as a blank "
                "rather than as an error"
            )
        invented = sorted(observed - declared)
        if invented:
            raise AssertionError(
                f"W4-09: the recorded stats response carries {invented}, which the deployed schema does "
                "not declare. The row forbids invented mock fields specifically: a key no schema "
                "declares is one the capture supplied rather than one the gateway served"
            )

    def _stats_response_fields(self) -> set[str]:
        """The deployed stats response's top-level field names, from its source.

        Parsed out of the model definition rather than listed here, for the reason
        W4-09's row gives: it asks about the "complete current RunStatsResponse", and
        a list written down in the harness stops being current the first time a field
        is added to the model. Parsing keeps the two in step by construction.

        Read with `ast` rather than by importing the gateway, because this script runs
        standalone against a URL and must not acquire the gateway's dependency tree —
        the same constraint that makes the protocol constants mirrored copies.

        An unreadable or unrecognisable source file raises
        :class:`PrerequisiteMissingError`. The harness could not determine what the
        schema declares, so it cannot compare anything against it, and defaulting to
        a hardcoded list would silently reintroduce exactly the staleness this method
        exists to remove.
        """
        path = self._repo_root / "modules/gateway/src/activity/stats_schemas.py"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            raise PrerequisiteMissingError(
                f"cannot read the deployed stats schema at {path}: {exc}. W4-09 compares the live "
                "response against the CURRENT model definition, so without it the comparison is "
                "unanswerable rather than satisfied"
            ) from None
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != STATS_RESPONSE_MODEL:
                continue
            fields = {
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
            }
            if not fields:
                raise PrerequisiteMissingError(
                    f"{STATS_RESPONSE_MODEL} in {path} declares no annotated fields, so there is nothing "
                    "to compare the live response against. An empty expectation would make the parity "
                    "assertion vacuously true"
                )
            return fields
        raise PrerequisiteMissingError(
            f"no {STATS_RESPONSE_MODEL} class found in {path}; the model this check compares against "
            "may have been renamed, and comparing against nothing would pass silently"
        )

    # ---- W4-10 ---------------------------------------------------------

    def check_w4_10(
        self,
        *,
        emitted_ids: tuple[str, ...] = (),
        results_so_far: Sequence[CheckResult] = (),
    ) -> None:
        """The consolidation: all 37 criteria, each owned, current and live where
        required — and a report with nothing missing in it.

        This is the check the wave-4 row makes the condition for closing all four
        evaluations, so it is the one with the most ways to be quietly wrong. Each
        assertion below closes one:

        * **Exactly the 37 IDs**, computed from the four waves' manifests rather than
          compared against a constant. A hardcoded 37 would be satisfiable by editing
          the constant to match whatever the index happened to contain, which is the
          opposite of a consolidation check.
        * **Per-entry owner, evaluation, revision and evidence.** An AC with a status
          and no owner is one nobody can be asked about; one with no revision cannot
          be tested for currency.
        * **Currency, computed.** Every entry's revision must be contained in what is
          deployed. An index of true-but-old observations is the stale-but-passing
          case, and it looks identical to a current one in every status field.
        * **Live where the row demands live.** A unit mock cannot stand in for a named
          live SDK/API/browser check, and that is only checkable because each entry
          records which it was.
        * **No missing, skipped, not-run or FAILED result** — asserted against THIS
          RUN's own check outcomes, not against the index's self-description. An index
          claiming completeness inside a report with three not_runs is exactly the
          fabricated 10/10 the kickoff forbids.

        That last one needs `results_so_far` rather than `emitted_ids`, and the
        difference was a real defect: an ID inventory says which checks answered, never
        what they answered, so reading it alone let W4-10 pass beside a failed sibling.
        The wave's own row makes this check the condition for closing all four
        evaluations, so a consolidation that green-lights a report failing its own gate
        is the exact outcome it exists to prevent.
        """
        index = self._artifact("wave4_evidence_index")
        self._assert_fixture_identity(index, "wave4_evidence_index", self.config)

        compiled_at = _parse_timestamp(index.get("compiled_at"))
        if compiled_at is None:
            raise AssertionError(
                f"W4-10: 'compiled_at' is {index.get('compiled_at')!r}, which is not an ISO-8601 "
                "instant. When the index was compiled is what makes it possible to say whether it "
                "predates a change to the code it indexes"
            )
        compiled_revision = index.get("compiled_revision")
        if not isinstance(compiled_revision, str) or not _GIT_REVISION_RE.match(compiled_revision):
            raise AssertionError(
                f"W4-10: 'compiled_revision' is {compiled_revision!r}, which is not a full 40-character "
                "git SHA"
            )

        preflight = self._artifact("wave4_preflight")
        deployed = {
            name: entry["revision"]
            for name, entry in self._deployed_components(preflight).items()
        }
        frontend = preflight.get("frontend")
        if isinstance(frontend, dict) and isinstance(frontend.get("revision"), str):
            deployed["frontend"] = frontend["revision"]
        self._assert_contained_in(
            compiled_revision,
            subject=f"the evidence index's compiled revision {compiled_revision}",
            deployed_revisions=deployed,
            hint="An index compiled from a revision the deployment does not contain indexes other code.",
        )

        # (1) Exactly the criterion set the manifests declare.
        expected = all_acceptance_ids()
        if len(expected) != WAVE4_TOTAL_ACCEPTANCE_IDS:
            raise AssertionError(
                f"W4-10: the registered waves declare {len(expected)} acceptance criteria but the "
                f"wave-4 row names {WAVE4_TOTAL_ACCEPTANCE_IDS}. The manifests and the evaluation have "
                f"diverged, so consolidating against either would misstate the other. Declared: "
                f"{list(expected)}"
            )
        criteria = index.get("criteria")
        if not isinstance(criteria, dict):
            raise AssertionError(
                f"W4-10: 'criteria' must be an object keyed by acceptance ID, got {criteria!r}"
            )
        recorded = [str(key) for key in criteria]
        duplicates = sorted({key for key in recorded if recorded.count(key) > 1})
        if duplicates:
            raise AssertionError(
                f"W4-10: the index carries duplicate entries for {duplicates}. Two rows for one "
                "criterion let a pass and a fail coexist, and which one a reader believes depends on "
                "ordering"
            )
        present = set(recorded)
        missing = sorted(set(expected) - present)
        unknown = sorted(present - set(expected))
        if missing:
            raise AssertionError(
                f"W4-10: the evidence index has no entry for {missing}. The row requires exactly all "
                f"{WAVE4_TOTAL_ACCEPTANCE_IDS} acceptance IDs, and an absent entry is an unevidenced "
                "criterion — which is precisely what a consolidation is counting"
            )
        if unknown:
            raise AssertionError(
                f"W4-10: the index carries {unknown}, which no registered wave declares. An unknown ID "
                f"inflates the apparent total toward {WAVE4_TOTAL_ACCEPTANCE_IDS} and can therefore "
                "conceal a missing real one"
            )

        # (2) Each entry: owned, evidenced, current, and live where required.
        for acceptance_id in expected:
            entry = criteria[acceptance_id]
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"W4-10: criterion {acceptance_id} is {entry!r}; each entry must be an object "
                    "carrying its owner, evaluation, revision, evidence and liveness"
                )
            absent = [key for key in EVIDENCE_INDEX_ENTRY_KEYS if key not in entry]
            if absent:
                raise AssertionError(
                    f"W4-10: criterion {acceptance_id} is missing {sorted(absent)}. Each of "
                    f"{list(EVIDENCE_INDEX_ENTRY_KEYS)} is a separate way for a green index row to be "
                    "worthless — unowned, unattributed, untestable for currency, or unevidenced"
                )
            if entry.get("status") != STATUS_PASSED:
                raise AssertionError(
                    f"W4-10: criterion {acceptance_id} is {entry.get('status')!r}, not "
                    f"{STATUS_PASSED!r}. All four evaluations may close only with every criterion "
                    "passing, so not_run and skipped keep this check failed rather than partial"
                )
            for key in ("owner", "evaluation", "evidence"):
                value = entry.get(key)
                if not value or (isinstance(value, str) and not value.strip()):
                    raise AssertionError(
                        f"W4-10: criterion {acceptance_id} records an empty {key!r}. A criterion nobody "
                        "owns is nobody's to rerun, and a status with no evidence reference behind it is "
                        "the substitute for evidence this evaluation refuses"
                    )
            revision = entry.get("revision")
            if not isinstance(revision, str) or not _GIT_REVISION_RE.match(revision):
                raise AssertionError(
                    f"W4-10: criterion {acceptance_id} was evidenced at {revision!r}, which is not a "
                    "full 40-character git SHA. Without an exact commit its currency cannot be checked"
                )
            self._assert_contained_in(
                revision,
                subject=f"criterion {acceptance_id}'s evidence revision {revision}",
                deployed_revisions=deployed,
                hint=(
                    "A criterion evidenced at a revision the deployment does not contain was a true "
                    "observation of a build nobody is running, and must be rerun."
                ),
            )
            live = entry.get("live")
            if not isinstance(live, bool):
                raise AssertionError(
                    f"W4-10: criterion {acceptance_id} records live={live!r}, which is not a boolean. "
                    "Whether an observation was live or mocked is the distinction the row turns on, so "
                    "it cannot be left implicit"
                )
            if acceptance_id in LIVE_EVIDENCE_REQUIRED_IDS and live is not True:
                raise AssertionError(
                    f"W4-10: criterion {acceptance_id} is evidenced by a non-live observation "
                    f"(evidence={entry.get('evidence')!r}). Its row names a live SDK, API or browser "
                    "check, and a unit mock reproducing the same shape proves the test double behaves as "
                    "written rather than that the deployment does"
                )

        # (3) The evaluations themselves, each accepted and each current.
        evaluations = index.get("evaluations")
        if not isinstance(evaluations, dict):
            raise AssertionError(
                f"W4-10: 'evaluations' must be an object keyed by wave number recording each "
                f"evaluation's acceptance, got {evaluations!r}"
            )
        for wave in sorted(WAVE_EVALUATIONS):
            if wave == 4:
                # Wave 4 is the run producing this report. An index asserting its own
                # wave's acceptance would be the report certifying itself, and the
                # acceptance it would be claiming is the one this check is part of
                # establishing.
                continue
            record = evaluations.get(str(wave), evaluations.get(wave))
            if record is None:
                raise AssertionError(
                    f"W4-10: no acceptance recorded for wave {wave} (evaluation "
                    f"{WAVE_EVALUATIONS[wave]}). All four evaluations close together on this evidence, "
                    "so an unaccepted earlier wave means there is nothing to close them against"
                )
            self._assert_prior_wave_accepted(wave, record, deployed_revisions=deployed)
        for wave in WAVE4_PRIOR_WAVES:
            if wave not in WAVE_EVALUATIONS:
                raise PrerequisiteMissingError(
                    f"W4-10: wave {wave} has no registered evaluation in this revision, so its criteria "
                    f"are not in the consolidated set and the index cannot be complete. Registered: "
                    f"{sorted(WAVE_EVALUATIONS)}"
                )

        # (4) This run's own report, not the index's description of it.
        #
        # The order matters: everything above could pass on a perfectly-compiled
        # index while this run reported three not_runs, and that combination is the
        # fabricated full report the kickoff names. The inventory is the wave's
        # manifest, passed in by `run_checks`.
        inventory = tuple(emitted_ids)
        declared = tuple(spec.check_id for spec in WAVE_CHECKS[4])
        if inventory and set(inventory) != set(declared):
            raise AssertionError(
                f"W4-10: this run's inventory is {sorted(inventory)} but wave 4 declares "
                f"{sorted(declared)}. A consolidation cannot be complete inside a report that is short a "
                "check, and one carrying an extra ID is describing a different wave"
            )
        uncovered = sorted(
            acceptance_id
            for spec in WAVE_CHECKS[4]
            for acceptance_id in spec.acceptance_ids
            if acceptance_id not in _NON_ACCEPTANCE_ROW_LABELS
            and acceptance_id not in present
        )
        if uncovered:
            raise AssertionError(
                f"W4-10: wave 4's own checks carry {uncovered}, which the index does not record. The "
                "wave cannot consolidate criteria its own manifest asserts but its index omits"
            )

        # (5) And what those checks ANSWERED. The inventory above establishes that
        # every check ran; this establishes that none of them found anything. They are
        # different claims, and a complete index sitting beside a failed sibling check
        # satisfies the first while contradicting the whole point of the second.
        #
        # W4-10 itself is excluded because it has no verdict yet — it is the check
        # making this assertion. Everything else in the wave is fair game, including
        # `skipped`: the row names it explicitly, and a skipped criterion is an
        # unobserved one however it came to be skipped.
        unresolved = sorted(
            f"{result.check_id}={result.status}"
            for result in results_so_far
            if result.check_id != "W4-10" and result.status != STATUS_PASSED
        )
        if unresolved:
            raise AssertionError(
                f"W4-10: this run's other checks did not all pass ({', '.join(unresolved)}). The wave-4 "
                "row makes this consolidation the condition for closing all four evaluations, so it "
                "cannot be satisfied inside a report that does not pass its own gate — however complete "
                "the evidence index is"
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

# S3 provides W2-02, S2 provides W2-03..05 and S5 provides W2-06..09. W2-01 (the
# consolidated preflight) and W2-10 (verified cleanup and the security recheck)
# are delivered by #5825, which closes the last gap that made a complete wave-2
# report unreachable. Every ID in the manifest now has a predicate, so
# PENDING_CHECK_OWNERS is empty for this wave — a NOT RUN from here on names a
# missing *input*, not a missing implementation.
WAVE2_PREDICATES: dict[str, str] = {
    "W2-01": "check_w2_01",
    "W2-02": "check_w2_02",
    "W2-03": "check_w2_03",
    "W2-04": "check_w2_04",
    "W2-05": "check_w2_05",
    "W2-06": "check_w2_06",
    "W2-07": "check_w2_07",
    "W2-08": "check_w2_08",
    "W2-09": "check_w2_09",
    "W2-10": "check_w2_10",
}

# S6 #3965 implements the steering half. The seven IDs absent here — W3-01/02/03/04/
# 05/10/12 — are registered in WAVE3_CHECKS with owners in PENDING_CHECK_OWNERS, so
# they report not_run naming who owes them rather than failing unowned. Adding a
# predicate here without the evidence to support it would be the worse half of that
# trade: a check that runs and passes on an artifact nobody produced.
WAVE3_PREDICATES: dict[str, str] = {
    "W3-06": "check_w3_06",
    "W3-07": "check_w3_07",
    "W3-08": "check_w3_08",
    "W3-09": "check_w3_09",
    "W3-11": "check_w3_11",
}

# Wave 4 includes its preflight, browser checks and consolidated evidence checks.
WAVE4_PREDICATES: dict[str, str] = {
    "W4-01": "check_w4_01",
    "W4-02": "check_w4_02",
    "W4-03": "check_w4_03",
    "W4-04": "check_w4_04",
    "W4-05": "check_w4_05",
    "W4-06": "check_w4_06",
    "W4-07": "check_w4_07",
    "W4-08": "check_w4_08",
    "W4-09": "check_w4_09",
    "W4-10": "check_w4_10",
}

CHECK_PREDICATES: dict[str, str] = {
    **WAVE1_PREDICATES,
    **WAVE2_PREDICATES,
    **WAVE3_PREDICATES,
    **WAVE4_PREDICATES,
}

# Checks whose subject is the teardown itself, so they can only be answered after
# `run_cleanup` has run. Keeping this as an explicit set rather than a naming
# convention means adding a post-cleanup check is a deliberate edit here, and a
# test pins that every member has a predicate — a post-cleanup check that silently
# never ran would be the worst of both worlds.
POST_CLEANUP_CHECK_IDS: frozenset[str] = frozenset({"W2-10"})

# Predicates that need the cleanup record passed in. Distinct from
# POST_CLEANUP_CHECK_IDS only in principle (a check could run after cleanup
# without reading it), but named separately so the argument plumbing is explicit
# rather than inferred from ordering.
CLEANUP_AWARE_CHECK_IDS: frozenset[str] = POST_CLEANUP_CHECK_IDS

# Predicates that need the PRE-teardown security capture. Separate from the cleanup
# record because the two are taken at opposite sides of the teardown boundary, and
# conflating them is what produced the defect where a post-teardown check needed a
# deleted resource to answer.
CAPTURE_AWARE_CHECK_IDS: frozenset[str] = frozenset({"W2-10"})

# Predicates that need the harness's record of INVOKING the fixture's resource
# teardown. Separate from the cleanup record because they are different steps with
# different owners: `run_cleanup` deletes rows the harness itself declared, while the
# resource teardown is the operator's own command that removes pods, queues and
# fixture policies. A check that read only the row record could not tell whether the
# resources had been removed at all — which is how a prefilled absence artifact passed.
TEARDOWN_AWARE_CHECK_IDS: frozenset[str] = frozenset({"W2-10"})

# Predicates that need the wave's full check-ID inventory passed in, because their
# subject includes the report's own completeness.
MANIFEST_AWARE_CHECK_IDS: frozenset[str] = frozenset(
    {"W1-10", "W2-01", "W2-10", "W4-01", "W4-10"}
)

# Predicates that need the OUTCOMES of the checks that already ran in this run, not
# just their IDs. Exactly one check needs this and it is worth being explicit about
# why, because the distinction is what a defect hid behind.
#
# W4-10's row forbids "missing/skipped/not-run result", and the inventory in
# MANIFEST_AWARE_CHECK_IDS carries check IDs only. A check ID set cannot distinguish a
# wave that answered all ten from a wave that answered all ten with three failures, so
# reading the inventory alone let W4-10 pass beside a failed sibling — a consolidation
# certifying a report that does not pass its own gate. The statuses have to be passed
# in for the assertion the docstring describes to be makeable at all.
RESULTS_AWARE_CHECK_IDS: frozenset[str] = frozenset({"W4-10"})


def split_post_cleanup_specs(
    specs: tuple[CheckSpec, ...],
) -> tuple[tuple[CheckSpec, ...], tuple[CheckSpec, ...]]:
    """Partition a wave's specs into before-cleanup and after-cleanup groups.

    Order within each group is preserved, and the two groups concatenated are the
    whole manifest — so the split cannot drop or duplicate a check. The manifest
    guard would catch that anyway, but a partition that silently lost a check
    would be a confusing way to find out.
    """
    before = tuple(spec for spec in specs if spec.check_id not in POST_CLEANUP_CHECK_IDS)
    after = tuple(spec for spec in specs if spec.check_id in POST_CLEANUP_CHECK_IDS)
    return before, after


def run_checks(
    driver: Driver,
    specs: tuple[CheckSpec, ...] = WAVE1_CHECKS,
    *,
    manifest_ids: tuple[str, ...] = (),
    prior_results: Sequence[CheckResult] = (),
    cleanup: CleanupOutcome | None = None,
    capture: SecurityCapture | None = None,
    teardown: ResourceTeardown | None = None,
) -> list[CheckResult]:
    """Execute every predicate, converting outcomes into check results.

    One check's failure never stops the others: a partial report with nine real
    answers and one named failure is far more useful to the operator who has to
    fix it than an abort at the first problem.

    A spec with no predicate is ``not_run`` naming its owning story. That is the
    honest answer for a wave under construction, and it keeps the run nonzero —
    the alternative, dropping the check, would make an incomplete wave produce a
    report that passes its own gate.

    ``manifest_ids`` is the wave's full check-ID inventory, needed by the checks
    whose subject includes the report's own completeness. It defaults to the IDs of
    ``specs`` so a direct caller driving a whole wave gets the right answer, but
    `main` passes the manifest explicitly — because with the post-cleanup split
    ``specs`` is only part of the wave, and a self-completeness check comparing
    against its own partition would always agree with itself.

    ``prior_results`` is the results of any EARLIER partition of the same wave, for
    the checks in RESULTS_AWARE_CHECK_IDS whose subject is the report's own outcomes.
    Without it a consolidation running in the post-cleanup partition would see only
    that partition's results and conclude the wave was clean because it could not see
    the failures.

    ``cleanup`` is the harness's first-hand teardown record, passed to the checks
    that verify it. ``None`` means cleanup has not run, which those checks report
    as ``not_run`` rather than assuming success.

    ``capture`` is the pre-teardown live security capture, taken while the fixture
    still existed. ``None`` means it was never made, which is also ``not_run``: those
    observations cannot be recovered afterwards, because the resources are gone.

    ``teardown`` is the harness's record of invoking the fixture's own resource
    teardown between the capture and these checks. ``None`` means that step never ran,
    so nothing removed the pods, queues or fixture policies — and an absence artifact
    read at that point would necessarily predate the removal it describes.
    """
    inventory = manifest_ids or tuple(spec.check_id for spec in specs)
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
        kwargs: dict[str, object] = {}
        if spec.check_id in MANIFEST_AWARE_CHECK_IDS:
            kwargs["emitted_ids"] = inventory
        if spec.check_id in RESULTS_AWARE_CHECK_IDS:
            # The outcomes recorded SO FAR, which for W4-10 is the other nine: it is
            # last in the manifest, and a check cannot be handed its own verdict
            # before it has one. A wave-4 run split across partitions passes the
            # earlier partition's results in through `prior_results`, so the
            # consolidation still sees the whole wave.
            kwargs["results_so_far"] = tuple(prior_results) + tuple(results)
        if spec.check_id in CLEANUP_AWARE_CHECK_IDS:
            kwargs["cleanup"] = cleanup
        if spec.check_id in CAPTURE_AWARE_CHECK_IDS:
            kwargs["capture"] = capture
        if spec.check_id in TEARDOWN_AWARE_CHECK_IDS:
            kwargs["teardown"] = teardown
        try:
            method(**kwargs)
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
        # Drained the same way, and for the same reason: attributing one check's
        # computed ancestry to the next one would make the evidence file wrong about
        # which check established what.
        result.ancestry = list(driver._ancestry)  # noqa: SLF001
        driver._ancestry = []  # noqa: SLF001
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


def capture_security_observations(driver: Driver, config: dict) -> SecurityCapture:
    """Read the live capability surface WHILE the fixture still exists.

    Called by `main` before `run_cleanup`. It exists because of an ordering defect:
    the question "does this deployment still refuse the unimplemented verbs?" can only
    be answered while there is a deployment to ask, and an earlier revision asked it
    after teardown about a row teardown had just deleted — so a correct teardown
    produced a not-found and W2-10 reported NOT RUN.

    Two observations per adapter, both pre-teardown:

    1. the state read's capability map — what the deployment says it supports;
    2. for each verb that map reports FALSE, an authorized owner's POST and the status
       it came back with — what the deployment does when asked anyway.

    (2) is confined to the verbs (1) reports unavailable, which is what keeps this
    read-only in effect: an unimplemented verb is refused before anything happens to
    the run, while posting `pause` to a build that implements it would mutate the
    fixture the other checks are still describing.

    Observations are recorded raw (status, capability map, per-verb status, transport
    error) rather than reduced to a verdict, so the evidence a reviewer reads is what
    was actually returned. Failures here are recorded, never raised: a capture that
    could not complete must make W2-10 NOT RUN or FAIL, not abort the run before
    cleanup — the fixture must come down either way.
    """
    notes: list[str] = []
    adapters: list[LiveCapabilityCapture] = []
    run_id = str(config.get("live_run_id") or "")
    command_id = str(config.get("command_id") or "")
    identity_env = (config.get("identity_env") or {}).get("owner")
    token = os.environ.get(identity_env) if identity_env else None
    if not run_id or not token:
        return SecurityCapture(
            ok=False,
            run_id=run_id,
            adapters=[],
            notes=[
                (
                    "cannot capture the live capability surface: "
                    f"live_run_id={'set' if run_id else 'missing'}, "
                    f"owner token={'set' if token else 'unavailable'}"
                )
            ],
        )

    ok = True
    for adapter, paths in ADAPTERS.items():
        observation = driver.probe.request(
            "GET", paths["state"].format(run_id=run_id), role="owner", token=token
        )
        body = observation.body if isinstance(observation.body, dict) else {}
        capabilities = body.get("capabilities")
        if observation.status != 200 or not isinstance(capabilities, dict):
            ok = False
            capabilities = capabilities if isinstance(capabilities, dict) else {}
            notes.append(
                f"{adapter}: pre-teardown capability read returned {observation.status!r} "
                f"(error={observation.error!r})"
            )
        else:
            notes.append(f"{adapter}: captured capability surface at status 200")

        refusals: dict[str, int | None] = {}
        if command_id:
            for verb in CONTROL_VERBS:
                if capabilities.get(verb) is not False:
                    # Either implemented (posting it would act on the run) or not
                    # described at all (W2-05 owns that gap). Neither is this
                    # capture's subject.
                    continue
                attempt = driver.probe.request(
                    "POST",
                    paths["verb"].format(run_id=run_id, verb=verb),
                    role="owner",
                    token=token,
                    json_body=valid_command_body(verb, command_id),
                )
                refusals[verb] = attempt.status
        else:
            ok = False
            notes.append(
                f"{adapter}: no 'command_id' configured, so no authorized attempt at an "
                "unsupported verb could be made"
            )
        adapters.append(
            LiveCapabilityCapture(
                adapter=adapter,
                run_id=run_id,
                status=observation.status,
                capabilities=capabilities,
                refusals=refusals,
                error=observation.error,
            )
        )
    return SecurityCapture(ok=ok, run_id=run_id, adapters=adapters, notes=notes)


def file_digest(path: Path | None) -> str | None:
    """``sha256:`` digest of a file's bytes, or ``None`` if it is not readable.

    Used to tell a freshly written artifact from one that was already on disk before
    the event it describes. Content rather than mtime: an mtime is trivially touched
    and says nothing about whether the observations changed, whereas an unchanged
    digest across a removal means the file describes state read before it.
    """
    if path is None or not path.is_file():
        return None
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _default_git_runner(argv: list[str]):  # noqa: ANN202
    """Run one read-only git query in the checkout, without a shell."""
    return subprocess.run(  # noqa: S603 - fixed argv, no operator input reaches the command name
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def git_ancestry(
    ancestor: str, descendant: str, *, repo: Path | None = None, runner=None
) -> GitAncestry:  # noqa: ANN001
    """Ask git, here and now, whether one revision is contained in another.

    Root's review named ``is_ancestor: true`` as an assertion presented as evidence:
    the operator writes the conclusion the check exists to reach, so an invented pair
    of well-formed SHAs passed. The fix is not a better-shaped claim — it is to stop
    reading the answer out of the artifact and compute it.

    This is the one provenance link the harness can establish entirely first-hand. It
    needs no cloud credential and no network: the repository the harness is running
    out of already contains the commit graph, so ``git merge-base --is-ancestor`` and
    ``git cat-file -e`` answer both halves — does this revision EXIST, and is it
    contained in what is deployed. An invented SHA fails at the first of those, which
    is precisely the "internally consistent but invented" case that used to pass.

    Both commits must be present locally for the answer to mean anything. A shallow
    or stale clone genuinely cannot answer, and that is reported as
    ``available=False`` with the reason — a missing capability, which the caller turns
    into ``not_run``, never into a pass. The distinction matters: "git says no" is a
    failed evaluation, "git could not be asked" is an unrun one, and collapsing them
    would either fail correct deployments or pass unverified ones.
    """
    run = runner or _default_git_runner
    base = ["git"] if repo is None else ["git", "-C", str(repo)]
    for label, revision in (("ancestor", ancestor), ("descendant", descendant)):
        if not isinstance(revision, str) or not _GIT_REVISION_RE.match(revision):
            return GitAncestry(
                ancestor=str(ancestor),
                descendant=str(descendant),
                available=False,
                is_ancestor=None,
                reason=f"the {label} revision {revision!r} is not a full 40-character git SHA",
            )
    try:
        for label, revision in (("ancestor", ancestor), ("descendant", descendant)):
            exists = run([*base, "cat-file", "-e", f"{revision}^{{commit}}"])
            if exists.returncode != 0:
                return GitAncestry(
                    ancestor=ancestor,
                    descendant=descendant,
                    available=False,
                    is_ancestor=None,
                    reason=(
                        f"the {label} revision {revision} is not a commit in this checkout, so git "
                        "cannot be asked about it. Either it does not exist anywhere — an invented "
                        "revision, which is the case this check exists to catch — or this clone is "
                        "too shallow to contain it; fetch it and re-run"
                    ),
                )
        result = run([*base, "merge-base", "--is-ancestor", ancestor, descendant])
    except Exception as exc:  # noqa: BLE001 - an unavailable git is not_run, never a pass
        return GitAncestry(
            ancestor=ancestor,
            descendant=descendant,
            available=False,
            is_ancestor=None,
            reason=f"git could not be run: {type(exc).__name__}: {exc}",
        )
    # `--is-ancestor` communicates through the exit status: 0 yes, 1 no, anything
    # else is git failing rather than answering. Treating a 128 as "not an ancestor"
    # would turn a broken checkout into a reported defect in the deployment.
    if result.returncode not in (0, 1):
        return GitAncestry(
            ancestor=ancestor,
            descendant=descendant,
            available=False,
            is_ancestor=None,
            reason=(
                f"git merge-base --is-ancestor exited {result.returncode}: "
                f"{(result.stderr or '').strip()!r}"
            ),
        )
    return GitAncestry(
        ancestor=ancestor,
        descendant=descendant,
        available=True,
        is_ancestor=result.returncode == 0,
        reason=None,
    )


def run_resource_teardown(
    config: dict, artifacts: ArtifactStore | None = None, *, runner=None
) -> ResourceTeardown:  # noqa: ANN001
    """Invoke the fixture's own resource teardown, between capture and verification.

    The executable seam finding 2 of root's review requires. Before this existed the
    published command captured live state, deleted rows, and then read an artifact that
    already claimed the pods and queues were gone — so no sequential operator run could
    produce that artifact honestly at that point, and the only way to have it was to
    write it before teardown happened.

    What this does NOT do is delete resources itself. The harness has no cluster access
    and must not acquire any: a read-only evaluator that could delete workloads is a
    much larger blast radius than the thing it verifies, and the deletion logic belongs
    to the fixture scripts (#3968) that created the resources. What the harness
    contributes is the ORDERING and the first-hand record that the step ran here — the
    part an artifact cannot establish about itself.

    The command is taken from the fixture config's ``resource_teardown``. It is passed
    as an argv LIST and run without a shell, so the fixture config cannot smuggle a
    shell pipeline through it. Absent → ``configured=False``, which makes W2-10
    ``not_run``: the lifecycle was never executed.

    Before invoking, it digests the post-teardown absence artifact as it stands on
    disk. W2-10 compares that against what it actually reads, so an artifact
    unchanged across the teardown is refused as prefilled. That comparison is
    first-hand: it does not depend on any timestamp the operator wrote.

    Failures are recorded rather than raised. `main` calls this from a `finally` chain,
    and an exception here would abandon the row cleanup that still has to happen.
    """
    # Snapshot BEFORE anything else, including before the validity checks below, so
    # that every return path carries it and no error path silently loses the one
    # observation that establishes freshness.
    verification_path = (
        artifacts.resolve("teardown_verification") if artifacts is not None else None
    )
    before_digest = file_digest(verification_path)
    present_before = before_digest is not None

    def record(**fields) -> ResourceTeardown:
        """A teardown record with the pre-invocation snapshot already filled in."""
        return ResourceTeardown(
            verification_present_before=present_before,
            verification_digest_before=before_digest,
            **fields,
        )

    declared = config.get("resource_teardown")
    if not declared:
        return record(
            configured=False,
            invoked=False,
            ok=False,
            exit_code=None,
            started_at=None,
            finished_at=None,
            stdout_digest=None,
            notes=[
                (
                    "no 'resource_teardown' command declared in the fixture config, so the harness "
                    "could not tear the fixture's resources down between capturing live state and "
                    "verifying absence. Without it the absence artifact would have to predate the "
                    "removal it describes"
                )
            ],
        )
    if not isinstance(declared, list) or not all(
        isinstance(part, str) and part for part in declared
    ):
        return record(
            configured=True,
            invoked=False,
            ok=False,
            exit_code=None,
            started_at=None,
            finished_at=None,
            stdout_digest=None,
            notes=[
                (
                    f"'resource_teardown' must be a nonempty list of argv strings, got {declared!r}. It "
                    "is run without a shell, so a single string cannot be accepted: it would either be "
                    "treated as one filename or require the shell this deliberately avoids"
                )
            ],
        )

    timeout = config.get("resource_teardown_timeout_seconds") or 600
    started_at = _now()
    notes: list[str] = []
    if present_before:
        notes.append(
            "the post-teardown absence artifact already existed before teardown was invoked; its "
            "content must have changed by the time it is read, or it describes the fixture while it "
            "still existed"
        )
    try:
        completed = (runner or _default_teardown_runner)(declared, timeout)
    except Exception as exc:  # noqa: BLE001 - a failed teardown must not skip row cleanup
        notes.append(f"the resource teardown command could not be run: {type(exc).__name__}: {exc}")
        return record(
            configured=True,
            invoked=True,
            ok=False,
            exit_code=None,
            started_at=started_at,
            finished_at=_now(),
            stdout_digest=None,
            notes=notes,
        )
    finished_at = _now()
    exit_code = completed.returncode
    output = (completed.stdout or "") + (completed.stderr or "")
    # A digest rather than the output itself. The command's stdout is a plausible place
    # for a token or an ARN to surface, and this record goes into the evidence file;
    # a digest still lets a reviewer tie the recorded run to the operator's own log.
    stdout_digest = (
        "sha256:" + hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
        if output
        else None
    )
    ok = exit_code == 0
    if ok:
        notes.append(f"resource teardown exited 0 ({started_at} → {finished_at})")
    else:
        notes.append(
            f"resource teardown exited {exit_code}; the fixture's resources are not established as "
            "removed, and a nonzero teardown is a failed evaluation rather than something to note"
        )
    return record(
        configured=True,
        invoked=True,
        ok=ok,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        stdout_digest=stdout_digest,
        notes=notes,
    )


def _default_teardown_runner(argv: list[str], timeout: int):  # noqa: ANN202
    """Run the operator's teardown command, without a shell."""
    return subprocess.run(  # noqa: S603 - argv list from the operator's own fixture config, shell=False
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_cleanup(config: dict, dynamodb_client) -> CleanupOutcome:  # noqa: ANN001
    """Delete exactly the fixture rows, then confirm absence with a consistent read.

    Bounded by construction: it deletes only the ``(event_id, arrived_at)`` pairs
    the config names. §7 forbids purging a shared queue or deleting ordinary
    objects, so there is no scan, no prefix and no wildcard here — an item this
    function was not told about cannot be reached by it.

    Returns a :class:`CleanupOutcome` rather than a bare ``(ok, notes)`` pair
    because W2-10 has to *observe* this work rather than be told it happened. The
    per-row record is the difference between "the operator asserted cleanup
    succeeded" and "the harness deleted this exact pair with both key halves and a
    consistent read came back empty" — and only the second can fail when cleanup
    did not actually happen.
    """
    notes: list[str] = []
    deletions: list[RowDeletion] = []
    items = config.get("cleanup_items") or []
    if not items:
        # Still `ok`: a wave that created no synthetic rows has nothing to remove,
        # and failing here would make the wave-1 fixture unrunnable. W2-01 is what
        # refuses an *undeclared* cleanup configuration, and W2-10 is what refuses
        # a cleanup record that does not cover the rows this wave seeded — so an
        # empty record cannot be mistaken for a completed teardown.
        return CleanupOutcome(
            ok=True, notes=["no fixture rows declared for cleanup"], deletions=[], declared_items=0
        )
    if dynamodb_client is None:
        return CleanupOutcome(
            ok=False,
            notes=["no DynamoDB client available to run cleanup"],
            deletions=[],
            declared_items=len(items),
        )

    ok = True
    table = config.get("invocation_table")
    for item in items:
        # A non-object entry is treated as a malformed declaration rather than
        # allowed to raise. `main` calls this function from a `finally` block, so an
        # exception here would abandon teardown for every REMAINING row and leave
        # control-enabled fixture resources running — a malformed config entry must
        # not be able to cause that. Recorded as a refusal, which W2-10 then fails on.
        event_id = item.get("event_id") if isinstance(item, dict) else None
        arrived_at = item.get("arrived_at") if isinstance(item, dict) else None
        if not event_id or not arrived_at:
            ok = False
            notes.append(f"cleanup item {item!r} lacks event_id/arrived_at; refusing a partial-key delete")
            # Recorded, not skipped: a refused partial key is exactly the state
            # W2-10 must be able to see, and an omitted record would read as a row
            # nobody asked about.
            deletions.append(
                RowDeletion(
                    event_id=str(event_id or ""),
                    arrived_at=str(arrived_at or ""),
                    both_keys_present=False,
                    deleted=False,
                    confirmed_absent=False,
                    error="partial key; refused",
                )
            )
            continue
        key = {"event_id": {"S": str(event_id)}, "arrived_at": {"S": str(arrived_at)}}
        try:
            # ALL_OLD so the record can distinguish a removed row from a no-op on a
            # key that was never there; see RowDeletion.existed.
            removed = dynamodb_client.delete_item(
                TableName=table, Key=key, ReturnValues="ALL_OLD"
            )
            existed = bool(removed.get("Attributes"))
            remaining = dynamodb_client.get_item(
                TableName=table, Key=key, ConsistentRead=True
            ).get("Item")
        except Exception as exc:  # noqa: BLE001
            ok = False
            notes.append(f"cleanup failed for {event_id}/{arrived_at}: {type(exc).__name__}: {exc}")
            deletions.append(
                RowDeletion(
                    event_id=str(event_id),
                    arrived_at=str(arrived_at),
                    both_keys_present=True,
                    deleted=False,
                    confirmed_absent=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        if remaining:
            ok = False
            notes.append(f"{event_id}/{arrived_at} still present after delete (consistent read)")
            deletions.append(
                RowDeletion(
                    event_id=str(event_id),
                    arrived_at=str(arrived_at),
                    both_keys_present=True,
                    deleted=True,
                    confirmed_absent=False,
                    existed=existed,
                    error="still present after delete (consistent read)",
                )
            )
        else:
            if existed:
                notes.append(
                    f"removed {event_id}/{arrived_at}; consistent read confirms absence"
                )
            else:
                # Not an error: the row may have expired by TTL, or a previous run
                # may have removed it. But it is NOT evidence that this harness tore
                # a fixture down, so it is reported in the words of what was seen.
                notes.append(
                    f"{event_id}/{arrived_at} was already absent; delete removed nothing "
                    "(check the table and key format if this was unexpected)"
                )
            deletions.append(
                RowDeletion(
                    event_id=str(event_id),
                    arrived_at=str(arrived_at),
                    both_keys_present=True,
                    deleted=True,
                    confirmed_absent=True,
                    existed=existed,
                )
            )
    return CleanupOutcome(ok=ok, notes=notes, deletions=deletions, declared_items=len(items))


def summarize_fixture_cleanup(
    cleanup: CleanupOutcome,
    teardown: ResourceTeardown | None,
    results: list[CheckResult],
    *,
    resource_teardown_expected: bool,
    verification_ids: tuple[str, ...] = tuple(sorted(POST_CLEANUP_CHECK_IDS)),
) -> FixtureCleanup:
    """Reduce the run's cleanup records into the one boolean the report publishes.

    Root reproduced the defect this closes: with a teardown runner that raises,
    ``resource_teardown.ok`` was false and all three fixture resources were still
    present, yet the summary line and ``result.json`` both said ``cleanup_ok=true``
    because that field carried only ``CleanupOutcome.ok`` — the rows. An operator
    scanning for whether the environment was left clean would have read a green
    field over a fixture that was never torn down.

    The three components are kept separate in the record rather than collapsed,
    because "the rows went but the pods did not" and "nothing went at all" need
    different responses from whoever reads the evidence, and an aggregate that
    erases the difference just moves the reporting problem one level down.

    ``absence_verified`` comes from the verification check's own status rather than
    from a second copy of its logic here. Two implementations of "were the resources
    gone?" would be free to disagree, and the report would then contain its own
    contradiction — which is the class of defect being fixed, not a new place to
    risk it. A wave with no such check (wave 1 declares none) has nothing to verify
    and nothing to withhold, so its absence is not treated as a failure.

    ``resource_teardown_expected`` is what keeps this from silently redefining
    cleanup for wave 1. Wave 1 creates no cluster resources and runs no teardown
    seam, so requiring one would turn every currently-passing wave-1 run into a
    cleanup failure — a behavior change the review explicitly did not ask for and
    the issue forbids. The caller passes whether this wave has a post-cleanup
    verification phase at all, so the stricter definition applies exactly where
    there are resources to be strict about.

    Nothing here changes an exit code on its own: the run already exits nonzero on
    a failed teardown through W2-10. What changes is that the reported field agrees
    with it.
    """
    notes: list[str] = []
    rows_ok = cleanup.ok
    if not rows_ok:
        notes.append("the declared fixture rows are not all confirmed deleted")

    # Teardown: unconfigured and failed are different, and both withhold. An
    # unconfigured seam means the resources were never removed by this run at all —
    # weaker than a failure, not stronger.
    if not resource_teardown_expected:
        resources_ok = True
    elif teardown is None or not teardown.configured:
        resources_ok = False
        notes.append(
            "no resource teardown was configured, so this run did not remove the fixture's pods, "
            "queues or policies"
        )
    elif not teardown.invoked:
        resources_ok = False
        notes.append("the configured resource teardown was never invoked")
    elif not teardown.ok:
        resources_ok = False
        notes.append(
            f"the fixture's resource teardown reported failure (exit {teardown.exit_code!r})"
        )
    else:
        resources_ok = True

    # Every post-cleanup verification this revision declares, not one hardcoded ID.
    #
    # The ID used to be the literal "W2-10". That was correct for two waves and a
    # latent false green for any third: `resource_teardown_expected` is derived from
    # whether the wave HAS post-cleanup checks, so a wave whose verification check is
    # called something else would set it to True, find no "W2-10" among the results,
    # take the `None` branch meaning "this wave declares none" — and publish
    # absence_verified=True with nothing having verified absence. The two facts have
    # to come from the same source to stay consistent, so they now both derive from
    # POST_CLEANUP_CHECK_IDS.
    present = [result for result in results if result.check_id in verification_ids]
    if not present:
        # No post-cleanup verification ran to be consulted (wave 1 declares none), so
        # there is no unestablished absence to withhold on.
        absence_verified = True
    else:
        unproven = [result for result in present if result.status != STATUS_PASSED]
        absence_verified = not unproven
        for result in unproven:
            notes.append(
                f"{result.check_id} did not verify the fixture's absence "
                f"(status={result.status})"
            )

    return FixtureCleanup(
        ok=rows_ok and resources_ok and absence_verified,
        rows_ok=rows_ok,
        resources_ok=resources_ok,
        absence_verified=absence_verified,
        notes=notes,
    )


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

    expected_ids = tuple(spec.check_id for spec in specs)
    pre_cleanup_specs, post_cleanup_specs = split_post_cleanup_specs(specs)

    # Cleanup in `finally`: the failure path is exactly when a fixture is most
    # likely to be left with a live listener, which is the state DP-INV-1 forbids.
    # Raw evidence is written before cleanup runs (§7: "preserve raw evidence first").
    #
    # The two-phase split is what makes W2-10 a *verified* cleanup check rather than
    # a claim: a cleanup predicate that ran inside the pass above would necessarily
    # execute BEFORE the deletions it describes, so it could only ever report an
    # operator's assertion. Running it after `run_cleanup` lets it read the harness's
    # own first-hand record. `finally` still covers the whole of it, so an exception
    # in the checks cannot skip either cleanup or its verification.
    results: list[CheckResult] = []
    capture: SecurityCapture | None = None
    teardown: ResourceTeardown | None = None
    try:
        results = run_checks(driver, pre_cleanup_specs, manifest_ids=expected_ids)
    finally:
        # Capture BEFORE teardown, and inside `finally` so a check raising cannot skip
        # it. This ordering is the correction root's review demanded: these are live
        # reads of a running deployment, and after `run_cleanup` there is no deployment
        # and no row to read — an earlier revision made them afterwards, so a CORRECT
        # teardown produced a not-found and W2-10 reported NOT RUN. Observations that
        # cannot be recovered have to be taken while they still exist.
        #
        # Wrapped in its own try/finally because it now sits between the checks and
        # teardown: `capture_security_observations` records ordinary failures rather
        # than raising, but a KeyboardInterrupt or any other BaseException during it
        # must not become the reason the fixture is never torn down. Teardown is the
        # one step that has to survive every failure mode, including a failure of the
        # step added in front of it.
        try:
            if post_cleanup_specs:
                capture = capture_security_observations(driver, config)
                for note in capture.notes:
                    logger.info("capture: %s", note)
                # The executable teardown seam. It runs HERE — after the live
                # observations, before row cleanup and before W2-10 — because that is
                # the only point at which the absence artifact can honestly be written:
                # the resources are gone by the end of it, and the check that reads it
                # has not run yet. Both steps sit inside this `try` so that a failure of
                # either still reaches the `finally` below: they were added in front of
                # row cleanup, and adding a step in front of the one thing that must
                # always happen must not create a new way for it not to.
                teardown = run_resource_teardown(config, artifacts)
                for note in teardown.notes:
                    logger.info("resource teardown: %s", note)
        finally:
            cleanup = run_cleanup(config, dynamodb)
        cleanup_notes = list(cleanup.notes)
        for note in cleanup_notes:
            logger.info("cleanup: %s", note)
        if post_cleanup_specs:
            results.extend(
                run_checks(
                    driver,
                    post_cleanup_specs,
                    manifest_ids=expected_ids,
                    # The earlier partition's outcomes, so a consolidation running here
                    # sees the whole wave rather than only this partition. Without it a
                    # results-aware check in the post-cleanup group would conclude the
                    # wave was clean because the failures were before its horizon.
                    prior_results=results,
                    cleanup=cleanup,
                    capture=capture,
                    teardown=teardown,
                )
            )
        # The reported `cleanup_ok` is computed HERE, after the verification check has
        # run, because that is the first point at which the whole fixture's disposal is
        # known. Reading `cleanup.ok` at the old site — before the post-cleanup checks —
        # is what let a run with three resources still present publish
        # `cleanup_ok=true`: at that moment the only thing that had been established
        # was that the rows were gone.
        fixture_cleanup = summarize_fixture_cleanup(
            cleanup,
            teardown,
            results,
            resource_teardown_expected=bool(post_cleanup_specs),
        )
        cleanup_ok = fixture_cleanup.ok
        cleanup_notes.extend(fixture_cleanup.notes)
        for note in fixture_cleanup.notes:
            logger.error("fixture cleanup: %s", note)
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing the client must not mask a result
            pass

    try:
        assert_check_manifest(results, expected_ids)
    except EvalPreconditionError as exc:
        logger.error("manifest error: %s", exc)
        return EXIT_PRECONDITION

    report = build_report(
        config, results, cleanup_ok=cleanup_ok, wave=args.wave, expected_ids=expected_ids
    )
    report["cleanup_notes"] = redact(cleanup_notes)
    # `cleanup` stays the ROW-specific record, under its own name, so nothing that
    # reads it loses the per-row DeleteItem/consistent-read detail. What changed is
    # that the top-level `cleanup_ok` no longer comes from it alone.
    report["cleanup"] = redact(cleanup.to_evidence())
    # The breakdown behind `cleanup_ok`, so a reader can tell "the rows went but the
    # pods did not" from "nothing went at all" without re-deriving it from three
    # other sections of the report.
    report["fixture_cleanup"] = redact(fixture_cleanup.to_evidence())
    # The pre-teardown observations travel with the report, raw. A reviewer reading
    # W2-10's verdict must be able to see the statuses and capability maps it was
    # reached from, at the point in the run where they were still observable — the
    # verdict alone is the thing this evaluation is trying not to take on trust.
    if capture is not None:
        report["security_capture"] = redact(capture.to_evidence())
    # The teardown record travels too, for the same reason: W2-10's verdict rests on
    # the harness having invoked the fixture's teardown at a specific point in the
    # run, and a reviewer has to be able to see that it did rather than infer it.
    if teardown is not None:
        report["resource_teardown"] = redact(teardown.to_evidence())
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

    # EXIT_CLEANUP keeps its established meaning: the harness's OWN row deletions did
    # not complete, which is the one cleanup failure no check can report because it is
    # the harness's own action. The broader fixture-cleanup verdict does not get its own
    # exit code, and does not need one — `resources_ok` and `absence_verified` are
    # derived from exactly the conditions W2-10 asserts, so either being false means
    # W2-10 did not pass, which already makes this an EXIT_CHECKS_FAILED run. Promoting
    # them to EXIT_CLEANUP would relabel a check failure that root confirmed was already
    # reported correctly.
    if not fixture_cleanup.rows_ok:
        logger.error(
            "row cleanup did not complete: a fixture left with a control listener enabled is the state "
            "DP-INV-1 forbids, so this is a failed evaluation even if every check passed."
        )
        return EXIT_CLEANUP
    if not cleanup_ok:
        logger.error(
            "the fixture is not established as fully cleaned up (rows_ok=%s, resources_ok=%s, "
            "absence_verified=%s). Its resources may still be running; check them before reusing the "
            "environment.",
            fixture_cleanup.rows_ok,
            fixture_cleanup.resources_ok,
            fixture_cleanup.absence_verified,
        )
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
