#!/usr/bin/env python3
"""Assemble pause_boundary / pause_resume / pause_expiry from live-SDK experiments.

Issue #3968 / epic #3959, Wave 2 checks W2-03, W2-04, W2-05.

Input:  the JSON emitted by `npx ts-node src/control-runtime.integration.ts --json`
        (real Claude `query()` runs under bypassPermissions through the neutral
        PauseGate coordinator, with production spill hooks composed).
Output: the three artifact files `agent-control-eval.py` reads.

The one rule this file exists to enforce: **copy measured values only.** Where an
experiment did not measure a field the harness requires, write `null` and report
it as unmeasured. A default that happens to be the value the check wants to see
would turn a missing observation into a passing one, which is the specific
failure mode the whole evaluation is built to prevent.

Exit 0 = every required field was populated from a measurement.
Exit 1 = artifacts written, but one or more required fields are unmeasured
         (named on stderr). The harness will fail the owning check, correctly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# The harness's required keys, duplicated here deliberately: this script must be
# reviewable on its own, and a silent import of the 2881-line harness would make
# a schema drift invisible. A mismatch is caught by --verify-against.
PAUSE_BOUNDARY_KEYS = (
    "adapter_id", "sdk_version", "permission_mode", "spill_hooks_composed",
    "requested", "held_interval", "tool_coverage", "confirmed", "degraded",
)
PAUSE_RESUME_KEYS = (
    "released_count", "session_id_before", "session_id_after", "attempt_id_before",
    "attempt_id_after", "interrupt_called", "initial_prompt_replayed",
    "prior_history_preserved", "task_completed", "held_tools_admitted_after_resume",
    "races",
)
PAUSE_EXPIRY_KEYS = (
    "auto_resumed", "annotation_count", "extra_assistant_turn", "neutral_annotation",
    "resolved_before_release", "pod_killed", "idle_retry_fired", "exit_watchdog_fired",
    "heartbeats_during_pause", "paused_distinguishable_from_stalled",
    "spill_output_preserved", "held_hook_timeout", "deadline_clamp", "cancellation",
)

# Experiment name fragments → their role. Matched on a fragment because the
# experiment titles carry acceptance IDs that may be re-worded.
BARRIER = "barrier blocks"
RESUME = "resume continues the same execution"
HOOK_HOLD = "hook may hold a tool"
SPILL = "production spill"
HOOK_TIMEOUT = "hook timeout reports unavailable"
ACTIVE_TOOL = "active MCP call settles"
OPAQUE_TOOL = "returned MCP tool cannot certify"

# Tool shapes the barrier experiment is run against, mapped to the coverage keys
# W2-03 requires. `long_running_bash` is satisfied by the background_bash shape's
# long-held Bash call; delegation covers the sub-agent case.
SHAPE_TO_COVERAGE = {
    "background_bash": ("long_running_bash", "background_task"),
    "delegation": ("delegated_task",),
}

UNMEASURED: list[str] = []


def unmeasured(path: str) -> None:
    UNMEASURED.append(path)


def find(reports: list[dict], fragment: str) -> dict | None:
    for report in reports:
        if fragment in report.get("name", ""):
            return report
    return None


def find_all(reports: list[dict], fragment: str) -> list[dict]:
    return [r for r in reports if fragment in r.get("name", "")]


def art(report: dict | None) -> dict:
    return (report or {}).get("artifact") or {}


def build_pause_boundary(reports: list[dict], sdk_version: str) -> dict[str, Any]:
    """W2-03 / AC-P1.

    The four barrier experiments each hold one tool shape. The harness wants ONE
    held_interval whose four counters are all zero, plus coverage across the hard
    shapes. We therefore take the interval from the shape that exercised the most
    demanding case (background_bash) and require every shape's interval to agree,
    rather than silently picking whichever one passed.
    """
    barriers = find_all(reports, BARRIER)
    if not barriers:
        unmeasured("pause_boundary.held_interval (no barrier experiment ran)")
        return {key: None for key in PAUSE_BOUNDARY_KEYS}

    by_shape = {art(b).get("requested_tool_shape") or "write": b for b in barriers}

    # Prefer the hardest shape actually measured, so the recorded interval is not
    # the easiest one available.
    preferred = next(
        (by_shape[s] for s in ("background_bash", "delegation", "service", "write") if s in by_shape),
        barriers[0],
    )
    primary = art(preferred)

    held_src = primary.get("held_interval") or {}
    held: dict[str, Any] = {}
    for key in ("duration_ms", "new_admissions", "fixture_writes",
                "fixture_service_calls", "task_output_bytes", "observed_by"):
        value = held_src.get(key)
        if value is None:
            unmeasured(f"pause_boundary.held_interval.{key}")
        held[key] = value

    # Coverage: only assert a shape True when that shape's experiment actually
    # parked a tool with no side effects. `ok` is the experiment's own verdict.
    coverage: dict[str, Any] = {}
    for shape, keys in SHAPE_TO_COVERAGE.items():
        report = by_shape.get(shape)
        for key in keys:
            if report is None:
                unmeasured(f"pause_boundary.tool_coverage.{key} (no '{shape}' experiment)")
                coverage[key] = None
            else:
                coverage[key] = bool(report.get("ok"))

    # Degradation: two distinct honest-failure modes, from two distinct
    # experiments. `untracked_activity` is the detached-work case (a returned tool
    # whose work continues); `hook_timeout` is the CLI abandoning the held hook.
    degraded: dict[str, Any] = {}
    opaque = find(reports, OPAQUE_TOOL) or find(reports, ACTIVE_TOOL)
    if opaque is None:
        unmeasured("pause_boundary.degraded.untracked_activity")
        degraded["untracked_activity"] = None
    else:
        blob = art(opaque)
        state = blob.get("phase_while_detached_work_continues") or blob.get("phase_during_tool")
        degraded["untracked_activity"] = {
            "state": state,
            "reason": (
                "background/detached work is untracked (background_work_count="
                f"{blob.get('background_work_count')!r}), so external quiescence cannot be "
                "certified and the pause stays requested rather than confirmed"
            ),
            "observed_outcome": blob.get("pause_outcome") or blob.get("pause_outcome_after_tool"),
            "external_quiescence_observed": blob.get("external_quiescence_observed"),
        }
        if state is None:
            unmeasured("pause_boundary.degraded.untracked_activity.state")

    timeout = find(reports, HOOK_TIMEOUT)
    if timeout is None:
        unmeasured("pause_boundary.degraded.hook_timeout")
        degraded["hook_timeout"] = None
    else:
        blob = art(timeout)
        state = blob.get("phase_after_timeout")
        degraded["hook_timeout"] = {
            "state": state,
            "reason": (
                "the CLI abandoned the held PreToolUse hook after "
                f"{blob.get('matcher_timeout_seconds')!r}s, so admission is no longer closed; "
                "the adapter reported unavailable rather than paused"
            ),
            "unavailable_event_observed": blob.get("unavailable_event_observed"),
            "fixture_write_after_timeout": blob.get("fixture_write_after_timeout"),
        }
        if state is None:
            unmeasured("pause_boundary.degraded.hook_timeout.state")

    spill = find(reports, SPILL)
    if spill is None:
        unmeasured("pause_boundary.spill_hooks_composed")
        composed: Any = None
    else:
        # True only because the spill experiment drove a real tool through the
        # production hook factory with the pause hooks composed in.
        composed = art(spill).get("production_hook_factory") == "createWorkerToolHooks"

    requested = primary.get("requested") or {}
    if requested.get("admission_closed") is None:
        unmeasured("pause_boundary.requested.admission_closed")

    confirmed = primary.get("confirmed") or {}
    for key in ("state", "active_tool_count"):
        if confirmed.get(key) is None:
            unmeasured(f"pause_boundary.confirmed.{key}")

    return {
        "adapter_id": primary.get("adapter_id"),
        "sdk_version": primary.get("sdk_version") or sdk_version,
        "permission_mode": primary.get("permission_mode"),
        "spill_hooks_composed": composed,
        "requested": requested,
        "held_interval": held,
        "tool_coverage": coverage,
        "confirmed": confirmed,
        "degraded": degraded,
        # Provenance so a reader can trace each value back to its experiment.
        "_source_experiments": {
            "primary_shape": primary.get("requested_tool_shape"),
            "shapes_measured": sorted(by_shape),
            "all_barrier_experiments_passed": all(b.get("ok") for b in barriers),
        },
    }


def build_pause_resume(reports: list[dict]) -> dict[str, Any]:
    """W2-04 / AC-P2. Near 1:1 with the resume experiment's own artifact."""
    report = find(reports, RESUME)
    if report is None:
        unmeasured("pause_resume (no resume experiment ran)")
        return {key: None for key in PAUSE_RESUME_KEYS}

    blob = art(report)
    out: dict[str, Any] = {}
    for key in PAUSE_RESUME_KEYS:
        if key == "races":
            continue
        value = blob.get(key)
        if value is None:
            unmeasured(f"pause_resume.{key}")
        out[key] = value

    # `races` must carry two named sub-cases, each with serialized/errored. The
    # experiment records them; anything absent is reported rather than defaulted.
    races_src = blob.get("races")
    races: dict[str, Any] = {}
    if not isinstance(races_src, dict):
        unmeasured("pause_resume.races")
        races = {"resume_before_pause": None, "repeated_resume": None}
    else:
        for case in ("resume_before_pause", "repeated_resume"):
            entry = races_src.get(case)
            if not isinstance(entry, dict):
                unmeasured(f"pause_resume.races.{case}")
                races[case] = None
                continue
            for field in ("serialized", "errored"):
                if entry.get(field) is None:
                    unmeasured(f"pause_resume.races.{case}.{field}")
            races[case] = entry
    out["races"] = races
    out["_source_experiment"] = report.get("name")
    return out


def build_pause_expiry(reports: list[dict], measured: Any = None) -> dict[str, Any]:
    """W2-05 / AC-P3, AC-P5, AC-P6.

    New producers emit the assembled measurements at the top level. Preserve those
    observations exactly, including false, zero and unknown values. Legacy inputs
    still get the partial artifact derived below; missing observations never pass.
    """
    if measured is not None:
        if not isinstance(measured, dict):
            raise ValueError("pause_expiry must be a JSON object or null")
        out = dict(measured)
        for key in PAUSE_EXPIRY_KEYS:
            if out.get(key) is None:
                out[key] = None
                unmeasured(f"pause_expiry.{key}")
        return out
    out: dict[str, Any] = {}

    hook = find(reports, HOOK_TIMEOUT)
    hold = find(reports, HOOK_HOLD)
    if hook is None:
        unmeasured("pause_expiry.held_hook_timeout")
        out["held_hook_timeout"] = None
    else:
        blob = art(hook)
        hold_blob = art(hold)
        # The two bounds come from the experiments that observed them: the hook's
        # own declared bound and the pause budget it must outlive.
        out["held_hook_timeout"] = {
            "exercised": blob.get("exercised", True) is not False,
            "state": blob.get("phase_after_timeout"),
            "reason": (
                "the CLI abandoned the held hook after "
                f"{blob.get('matcher_timeout_seconds')!r}s; the pause degraded to unavailable"
            ),
            "hook_timeout_seconds": hold_blob.get("hook_timeout_seconds")
            or blob.get("production_matcher_timeout_seconds"),
            "pause_budget_seconds": hold_blob.get("pause_budget_seconds"),
            "safety_release_used": blob.get("safety_release_used"),
        }
        for key in ("state", "hook_timeout_seconds", "pause_budget_seconds"):
            if out["held_hook_timeout"].get(key) is None:
                unmeasured(f"pause_expiry.held_hook_timeout.{key}")

    spill = find(reports, SPILL)
    if spill is None:
        unmeasured("pause_expiry.spill_output_preserved")
        out["spill_output_preserved"] = None
    else:
        blob = art(spill)
        # Only True when the spilled locator actually survived the hold.
        preserved = (
            blob.get("spill_readback_matches_tool_response") is True
            and blob.get("updated_tool_output_contains_locator") is True
            and blob.get("no_write_during_hold") is True
        )
        out["spill_output_preserved"] = preserved

    # Fields no current experiment measures. Null + named, never defaulted.
    for key in (
        "auto_resumed", "annotation_count", "extra_assistant_turn", "neutral_annotation",
        "resolved_before_release", "pod_killed", "idle_retry_fired", "exit_watchdog_fired",
        "heartbeats_during_pause", "paused_distinguishable_from_stalled",
        "deadline_clamp", "cancellation",
    ):
        if key not in out:
            out[key] = None
            unmeasured(f"pause_expiry.{key} (no experiment measures this yet)")

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, help="experiment JSON from control-runtime.integration.ts")
    parser.add_argument("--out-dir", required=True, help="evidence artifacts directory")
    parser.add_argument("--pod-observation", type=Path,
                        help="operator-collected before/after Kubernetes snapshots, events and result receipt")
    args = parser.parse_args()

    raw_path = Path(args.raw)
    if not raw_path.is_file():
        print(f"FAIL: raw experiment JSON not found: {raw_path}", file=sys.stderr)
        return 2
    data = json.loads(raw_path.read_text())
    reports = data.get("reports") or []
    if not reports:
        print("FAIL: the experiment JSON contains no reports", file=sys.stderr)
        return 2
    sdk_version = data.get("sdk_version") or ""

    if args.pod_observation:
        from lib.pod_survival import observe_survival
        observation_bytes = args.pod_observation.read_bytes()
        survival = observe_survival(json.loads(observation_bytes))
        measured = data.get("pause_expiry")
        if not isinstance(measured, dict):
            raise ValueError("pod survival requires the measured pause_expiry producer")
        measured["pod_killed"] = survival["pod_killed"]
        measured["launcher_observation"] = {
            **survival, "source_sha256": hashlib.sha256(observation_bytes).hexdigest(),
            "source_path": str(args.pod_observation),
        }
        measured["missing_launcher_inputs"] = [
            item for item in measured.get("missing_launcher_inputs", [])
            if (item.get("field") if isinstance(item, dict) else item) != "pod_killed"
        ]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {
        "pause_boundary.json": build_pause_boundary(reports, sdk_version),
        "pause_resume.json": build_pause_resume(reports),
        "pause_expiry.json": build_pause_expiry(reports, data.get("pause_expiry")),
    }
    for name, payload in artifacts.items():
        payload = dict(payload)
        payload["_provenance"] = {
            "raw_experiment_file": str(raw_path),
            "sdk_version": sdk_version,
            "observed_models": data.get("observed_models"),
            "experiments_total": len(reports),
            "experiments_passed": sum(1 for r in reports if r.get("ok")),
            "assembled_by": "platform/scripts/operator/wave2/21-assemble-pause-artifacts.py",
        }
        (out_dir / name).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"ok   wrote {out_dir / name}")

    failed = [r.get("name") for r in reports if not r.get("ok")]
    if failed:
        print(f"\nNOTE: {len(failed)} experiment(s) did not pass; their real values were", file=sys.stderr)
        print("      recorded as observed. The harness will judge them:", file=sys.stderr)
        for name in failed:
            print(f"        - {name}", file=sys.stderr)

    if UNMEASURED:
        print(
            f"\nUNMEASURED REQUIRED FIELDS ({len(UNMEASURED)}) — written as null, so the owning\n"
            "check will FAIL rather than pass on a fabricated value:",
            file=sys.stderr,
        )
        for path in UNMEASURED:
            print(f"  - {path}", file=sys.stderr)
        print(
            "\nTo close these, add the missing experiment(s) to\n"
            "  modules/agent-factory/agent/src/control-runtime.integration.ts\n"
            "(a shortened-pause-budget expiry run measuring auto-resume, the single neutral\n"
            "annotation, heartbeats, the deadline clamp and cancellation-without-admission)\n"
            "and re-run 20-collect-pause-evidence.sh. Do NOT hand-edit these artifacts.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
