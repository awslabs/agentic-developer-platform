#!/usr/bin/env python3
"""Offline PMM-09 evidence validator. It never invokes a model or flips posture."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "matrix.json"
STATUSES = {"pass", "fail", "blocked", "not_run"}
MULTI_INVOCATION_CELLS = {"L1", "L2"}
CHAIN_CELLS = {"L17", "L18", "L21"}
ENFORCING_CELLS = {"L23", "L24"}
SHADOW_MARKER = "PMM09_MODEL_SHADOW "
COMMON_FIELDS = {
    "account_id",
    "region",
    "timestamp_utc",
    "persona",
    "principal_kind",
    "principal_id",
    "tenant_id",
    "surface",
    "requested_model",
    "resolved_model",
    "resolution_source",
    "policy_revision",
    "posture_revision",
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level value must be an object")
    return value


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    cells = manifest.get("cells")
    if manifest.get("schema_version") != 1 or not isinstance(cells, list):
        return ["manifest must have schema_version=1 and a cells array"]
    ids = [cell.get("id") for cell in cells if isinstance(cell, dict)]
    required_ids = {f"L{number}" for number in range(1, 26)}
    if len(cells) != 25 or set(ids) != required_ids or len(ids) != len(set(ids)):
        errors.append("manifest must contain each cell L1..L25 exactly once")
    for cell in cells:
        if not isinstance(cell, dict):
            errors.append("every matrix cell must be an object")
            continue
        kinds = cell.get("evidence_kinds")
        if (
            not isinstance(kinds, list)
            or not kinds
            or set(kinds) - {"A", "B", "C", "D"}
        ):
            errors.append(f"{cell.get('id', '?')}: invalid evidence_kinds")
        if (
            "B" in (kinds or [])
            and cell.get("id") != "L8"
            and not cell.get("reason_code")
        ):
            errors.append(
                f"{cell.get('id', '?')}: refusal cell has no fixed reason_code"
            )
    return errors


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _require(
    cell_id: str,
    value: dict[str, Any],
    fields: set[str],
    errors: list[str],
    label: str = "evidence",
) -> None:
    missing = sorted(field for field in fields if not _present(value.get(field)))
    if missing:
        errors.append(f"{cell_id}: {label} missing {', '.join(missing)}")


def _require_keys(
    cell_id: str, value: dict[str, Any], fields: set[str], errors: list[str]
) -> None:
    """Require common fields while allowing outcome-specific null values."""
    missing = sorted(field for field in fields if field not in value)
    if missing:
        errors.append(f"{cell_id}: evidence missing {', '.join(missing)}")


def validate_kind_a(cell_id: str, evidence: dict[str, Any], errors: list[str]) -> None:
    invocation = evidence.get("invocation")
    if not isinstance(invocation, dict):
        errors.append(f"{cell_id}: kind A requires invocation evidence")
        return
    _require(
        cell_id,
        invocation,
        {
            "provider_request_id",
            "model_output_sha256",
            "usage_row_id",
            "agent_run_id",
            "cost_usd",
            "input_tokens",
            "output_tokens",
        },
        errors,
        "invocation",
    )
    digest = invocation.get("model_output_sha256")
    if _present(digest) and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        errors.append(f"{cell_id}: model_output_sha256 must be a lowercase SHA-256")
    if invocation.get("real_model_output") is not True:
        errors.append(
            f"{cell_id}: kind A must explicitly assert real_model_output=true"
        )
    if (
        not isinstance(evidence.get("resolved_model"), str)
        or not evidence["resolved_model"].strip()
    ):
        errors.append(f"{cell_id}: kind A requires a resolved_model")
    for field in ("provider_request_id", "usage_row_id", "agent_run_id"):
        if not isinstance(invocation.get(field), str) or not invocation[field].strip():
            errors.append(f"{cell_id}: invocation {field} must be a nonempty string")
    for field in ("input_tokens", "output_tokens"):
        if type(invocation.get(field)) is not int or invocation[field] < 0:
            errors.append(f"{cell_id}: {field} must be a nonnegative integer")
    try:
        cost = Decimal(str(invocation.get("cost_usd")))
        if (
            not cost.is_finite()
            or cost < 0
            or isinstance(invocation.get("cost_usd"), bool)
        ):
            raise ValueError("invalid cost")
    except (InvalidOperation, ValueError):
        errors.append(f"{cell_id}: cost_usd must be a finite nonnegative amount")


def validate_kind_b(
    cell: dict[str, Any], evidence: dict[str, Any], errors: list[str]
) -> None:
    cell_id = cell["id"]
    if cell_id == "L8":
        observation = evidence.get("non_billable_observation")
        if not isinstance(observation, dict):
            errors.append("L8: requires non_billable_observation evidence")
            return
        _require(
            cell_id,
            observation,
            {"request_trace_id", "usage_query_id", "provider_log_query_id"},
            errors,
            "non_billable_observation",
        )
        if (
            observation.get("scope_selector_rendered") is not False
            or observation.get("self_rows_only") is not True
        ):
            errors.append("L8: must prove no scope selector and self-only requests")
        if any(
            type(observation.get(field)) is not int or observation[field] != 0
            for field in ("usage_rows", "provider_invocations")
        ):
            errors.append(
                "L8: observation must prove zero usage rows and zero provider invocations"
            )
        return
    refusal = evidence.get("refusal")
    if not isinstance(refusal, dict):
        errors.append(f"{cell_id}: kind B requires refusal evidence")
        return
    _require(
        cell_id,
        refusal,
        {
            "reason_code",
            "requester_delivery_id",
            "usage_query_id",
            "provider_log_query_id",
        },
        errors,
        "refusal",
    )
    if refusal.get("reason_code") != cell.get("reason_code"):
        errors.append(f"{cell_id}: expected reason_code {cell.get('reason_code')!r}")
    if any(
        type(refusal.get(field)) is not int or refusal[field] != 0
        for field in ("usage_rows", "provider_invocations")
    ):
        errors.append(
            f"{cell_id}: refusal must prove zero usage rows and zero provider invocations"
        )
    if _present(refusal.get("provider_request_id")):
        errors.append(f"{cell_id}: refusal cannot carry a provider request ID")
    if cell_id == "L11":
        surface_codes = refusal.get("surface_reason_codes")
        expected = cell.get("reason_code")
        if (
            not isinstance(surface_codes, dict)
            or surface_codes.get("ui") != expected
            or surface_codes.get("cli") != expected
        ):
            errors.append("L11: UI and CLI must expose the same fixed reason code")


def validate_kind_c(cell_id: str, evidence: dict[str, Any], errors: list[str]) -> None:
    change = evidence.get("configuration_change")
    if not isinstance(change, dict):
        errors.append(f"{cell_id}: kind C requires configuration_change evidence")
        return
    _require(
        cell_id,
        change,
        {"api_response_id", "audit_row_id", "stored_revision"},
        errors,
        "configuration_change",
    )
    if type(change.get("stored_revision")) is not int or change["stored_revision"] < 1:
        errors.append(f"{cell_id}: stored_revision must be a positive integer")


def validate_common(cell, value, deployment, errors):
    cell_id = cell["id"]
    _require_keys(cell_id, value, COMMON_FIELDS, errors)
    for field in (
        "account_id",
        "region",
        "persona",
        "principal_id",
        "tenant_id",
        "policy_revision",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"{cell_id}: {field} must be a nonempty string")
    for field in ("account_id", "region"):
        if value.get(field) != deployment.get(field):
            errors.append(f"{cell_id}: {field} differs from deployment")
    if value.get("surface") != cell["surface"]:
        errors.append(f"{cell_id}: surface does not match matrix")
    expected = (
        "service_account"
        if cell["principal"]
        in {"service_sigv4", "service_cognito_m2m", "scheduled_service"}
        or (cell["principal"] == "human_admin" and "A" in cell["evidence_kinds"])
        else "human"
    )
    if cell["principal"] != "mixed" and value.get("principal_kind") != expected:
        errors.append(f"{cell_id}: canonical principal_kind must be {expected}")
    if type(value.get("posture_revision")) is not int or value["posture_revision"] < 1:
        errors.append(f"{cell_id}: posture_revision must be a positive integer")
    try:
        timestamp = datetime.fromisoformat(
            value.get("timestamp_utc", "").replace("Z", "+00:00")
        )
        if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            raise ValueError("not UTC")
    except (ValueError, TypeError, AttributeError):
        errors.append(f"{cell_id}: timestamp_utc must be an ISO UTC timestamp")


def _distinct_invocations(cell_id, rows, errors):
    for field in ("agent_run_id", "usage_row_id", "provider_request_id"):
        values = [row.get(field) for row in rows]
        if any(not isinstance(v, str) or not v for v in values) or len(
            set(str(v) for v in values)
        ) != len(values):
            errors.append(f"{cell_id}: distinct {field} required for every observation")


def validate_multi_invocation(cell, evidence, errors):
    cell_id = cell["id"]
    entries = evidence.get("invocations")
    if not isinstance(entries, list) or len(entries) < 3:
        errors.append(
            f"{cell_id}: multi-observation cell requires an invocations list with at least 3 entries"
        )
        return
    rows = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"{cell_id}: invocations[{index}] is not an object")
            continue
        rows.append(entry)
        _require(cell_id, entry, {"persona", "resolved_model"}, errors)
        # Each row supplies its OWN call evidence; a top-level invocation cannot fill holes.
        validate_kind_a(cell_id, {**evidence, **entry, "invocation": entry}, errors)
        for field in ("tenant_id", "principal_kind", "principal_id"):
            if entry.get(field) != evidence.get(field):
                errors.append(f"{cell_id}: observation {field} differs from owner")
    _distinct_invocations(cell_id, rows, errors)
    if len({str(row.get("persona")) for row in rows}) < 3:
        errors.append(f"{cell_id}: invocations must cover at least 3 distinct personas")
    if len({str(row.get("resolved_model")) for row in rows}) < 3:
        errors.append(
            f"{cell_id}: invocations must cover at least 3 distinct resolved models"
        )


def validate_chain(cell, evidence, errors):
    cell_id = cell["id"]
    chain = evidence.get("chain")
    if not isinstance(chain, dict):
        errors.append(f"{cell_id}: chain cell requires a chain evidence object")
        return
    _require(
        cell_id,
        chain,
        {
            "chain_id",
            "root_invocation_id",
            "snapshot_digest",
            "root_principal_kind",
            "root_principal_id",
        },
        errors,
    )
    digest = chain.get("snapshot_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        errors.append(f"{cell_id}: chain snapshot_digest must be a SHA-256")
    if chain.get("root_principal_kind") != evidence.get("principal_kind") or chain.get(
        "root_principal_id"
    ) != evidence.get("principal_id"):
        errors.append(
            f"{cell_id}: chain root must retain the canonical preference owner"
        )
    hops = chain.get("hops")
    if not isinstance(hops, list) or len(hops) < 2:
        errors.append(f"{cell_id}: chain requires at least 2 hops")
        return
    rows = []
    previous = None
    for index, hop in enumerate(hops):
        if not isinstance(hop, dict):
            errors.append(f"{cell_id}: hops[{index}] is not an object")
            continue
        invocation = hop.get("invocation", hop)
        if not isinstance(invocation, dict):
            errors.append(f"{cell_id}: hop requires invocation evidence")
            continue
        rows.append(invocation)
        _require(
            cell_id, hop, {"persona", "resolved_model", "resolution_source"}, errors
        )
        validate_kind_a(cell_id, {**evidence, **hop, "invocation": invocation}, errors)
        for field in ("tenant_id", "principal_kind", "principal_id"):
            if hop.get(field) != evidence.get(field):
                errors.append(f"{cell_id}: hop {field} differs from owner")
        if hop.get("snapshot_digest") != digest or hop.get("chain_id") != chain.get(
            "chain_id"
        ):
            errors.append(f"{cell_id}: hop snapshot/chain differs from root")
        if "parent_invocation_id" not in hop or hop["parent_invocation_id"] != previous:
            errors.append(f"{cell_id}: hops must form ordered parent/child lineage")
        if index == 0 and invocation.get("agent_run_id") != chain.get(
            "root_invocation_id"
        ):
            errors.append(f"{cell_id}: first hop must be the root invocation")
        previous = invocation.get("agent_run_id")
    _distinct_invocations(cell_id, rows, errors)
    if len({str(h.get("persona")) for h in hops if isinstance(h, dict)}) < 2:
        errors.append(f"{cell_id}: chain must include distinct personas")
    if cell_id == "L21" and all(isinstance(h, dict) for h in hops):
        root = hops[0]
        if (
            root.get("has_direct_override") is not True
            or root.get("resolution_source") != "explicit-direct"
        ):
            errors.append(
                f"{cell_id}: root hop must have has_direct_override=true and explicit-direct source"
            )
        if not any(h.get("has_direct_override") is False for h in hops[1:]):
            errors.append(
                f"{cell_id}: at least one descendant hop must lack has_direct_override"
            )
        for hop in hops[1:]:
            if (
                hop.get("has_direct_override") is not False
                or hop.get("resolution_source")
                not in ("principal-mapping", "system-default")
                or hop.get("resolved_model") == root.get("resolved_model")
            ):
                errors.append(
                    f"{cell_id}: every descendant must independently resolve a different model without the direct override"
                )


def validate_shadow(
    manifest: dict[str, Any], evidence: dict[str, Any], errors: list[str]
) -> None:
    shadow = evidence.get("shadow_comparison")
    if not isinstance(shadow, dict):
        errors.append("L22: kind D requires shadow_comparison evidence")
        return
    minimum = shadow.get("minimum_observations_per_path")
    if type(minimum) is not int or minimum < 1:
        errors.append("L22: minimum_observations_per_path must be positive")
        return
    observations = shadow.get("observations")
    if not isinstance(observations, list):
        errors.append("L22: observations must be an array")
        return
    required = set(manifest["required_shadow_paths"])
    counts = {path: 0 for path in required}
    seen = set()
    for index, row in enumerate(observations):
        if not isinstance(row, dict):
            errors.append(f"L22: observation {index} is not an object")
            continue
        path = row.get("dispatch_path")
        if not isinstance(path, str) or path not in required:
            errors.append(f"L22: observation {index} has unknown dispatch_path")
            continue
        identity = (
            row.get("tenant_id"),
            row.get("invocation_id"),
            row.get("attempt"),
            row.get("model_decision_id"),
        )
        valid_identity = (
            all(
                isinstance(v, str) and v
                for v in (identity[0], identity[1], identity[3])
            )
            and type(identity[2]) is int
            and identity[2] >= 1
            and re.fullmatch(r"[0-9a-f]{64}", identity[3]) is not None
        )
        if not valid_identity or identity in seen:
            errors.append(f"L22: observation {index} lacks a distinct launch identity")
        else:
            seen.add(identity)
            counts[path] += 1
        if (
            row.get("runtime_posture") != "report_only"
            or row.get("posture_verified") is not True
            or row.get("phase") != "sdk_admission"
        ):
            errors.append(
                f"L22: observation {index} must record fresh verified report-only SDK admission"
            )
        if row.get("policy_status") != "proposed":
            errors.append(f"L22: observation {index} has no issued proposal")
        if row.get("actual_model") != row.get("legacy_model"):
            errors.append(f"L22: observation {index} changed the actual SDK model")
        if row.get("admission_refusal") is not False:
            errors.append(
                f"L22: observation {index} mixes an admission refusal into selection shadow data"
            )
        mapping_exists = row.get("mapping_exists")
        if not isinstance(mapping_exists, bool):
            errors.append(f"L22: observation {index} must state mapping_exists")
            continue
        differs = row.get("legacy_model") != row.get("proposed_model")
        source = row.get("resolution_source")
        if source not in (
            "principal-mapping",
            "explicit-direct",
            "system-default",
        ) or mapping_exists != (source == "principal-mapping"):
            errors.append(
                f"L22: observation {index} has inconsistent resolution source"
            )
        if differs and source not in ("principal-mapping", "explicit-direct"):
            errors.append(f"L22: observation {index} is an unexplained divergence")
        _require(
            "L22",
            row,
            {
                "legacy_model",
                "proposed_model",
                "persona",
                "principal_kind",
                "tenant_id",
                "policy_revision",
                "principal_id",
                "snapshot_digest",
                "posture_revision",
            },
            errors,
            f"observation {index}",
        )
    uncovered = sorted(path for path, count in counts.items() if count < minimum)
    if uncovered:
        errors.append(f"L22: uncovered shadow paths: {', '.join(uncovered)}")
    rejected = shadow.get("rejected_events")
    if not isinstance(rejected, list):
        errors.append("L22: rejected_events must be an explicit array")
    elif rejected:
        errors.append(
            f"L22: {len(rejected)} rejected event(s) in shadow data;"
            " unmapped dispatch paths must be resolved before assessment passes"
        )


def shadow_path(event: dict[str, Any]) -> str | None:
    """Map trusted worker channel/trigger fields to the approved path names."""
    channel = event.get("channel")
    trigger = event.get("trigger")
    if not isinstance(channel, str) or not isinstance(trigger, str):
        return None
    if channel == "gitlab":
        return "gitlab"
    if trigger == "eventbridge" or channel in {"schedule", "eventbridge"}:
        return "scheduled_service_account"
    if trigger == "agent_trigger":
        return "agent_to_agent"
    if trigger in {"orchestration", "orchestration_loop"}:
        return "orchestration_loop"
    if trigger in {"workflow_dispatch", "arc_workflow"}:
        return "arc_workflow"
    if channel in {"chat", "ui"}:
        return "chat_ui"
    if channel == "cli":
        return "cli"
    if trigger in {"issue_labeled", "label"}:
        return "label_dispatch"
    if channel == "github" and trigger in {"mention", "issue_comment"}:
        return "github_mention"
    return None


def shadow_report(path: Path) -> dict[str, Any]:
    """Convert one-line worker events into the L22 observation shape."""
    observations: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text().splitlines(), start=1):
        payload = raw.partition(SHADOW_MARKER)[2] if SHADOW_MARKER in raw else raw
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            rejected.append({"line": line_number, "reason": "invalid_json"})
            continue
        if (
            not isinstance(event, dict)
            or event.get("event") != "persona_model_shadow_comparison"
        ):
            rejected.append({"line": line_number, "reason": "wrong_event"})
            continue
        dispatch_path = shadow_path(event)
        if dispatch_path is None:
            rejected.append(
                {
                    "line": line_number,
                    "reason": "unmapped_path",
                    "channel": event.get("channel"),
                    "trigger": event.get("trigger"),
                }
            )
            continue
        observations.append(
            {
                "dispatch_path": dispatch_path,
                "mapping_exists": event.get("mapping_exists"),
                "legacy_model": event.get("legacy_model"),
                "proposed_model": event.get("proposed_model"),
                "admission_refusal": event.get("admission_refusal", False),
                "persona": event.get("persona"),
                "principal_kind": event.get("principal_kind"),
                "tenant_id": event.get("tenant_id"),
                "policy_revision": event.get("policy_revision"),
                **{
                    key: event.get(key)
                    for key in (
                        "principal_id",
                        "snapshot_digest",
                        "posture_revision",
                        "runtime_posture",
                        "posture_verified",
                        "invocation_id",
                        "attempt",
                        "model_decision_id",
                        "phase",
                        "actual_model",
                        "resolution_source",
                        "policy_status",
                    )
                },
            }
        )
    return {
        "minimum_observations_per_path": 1,
        "observations": observations,
        "rejected_events": rejected,
    }


def validate_safety(evidence: dict[str, Any], errors: list[str]) -> None:
    safety = evidence.get("safety")
    if not isinstance(safety, dict):
        errors.append("safety block is required")
        return
    if safety.get("enforcement_authorized") is not False:
        errors.append("this harness accepts only enforcement_authorized=false")
    if safety.get("posture_at_collection") != "report_only":
        errors.append(
            "this harness accepts evidence collected in report_only posture only"
        )
    if (
        type(safety.get("probe_spend_ceiling_usd")) not in (int, float)
        or safety["probe_spend_ceiling_usd"] != 0
    ):
        errors.append("paid probing is not authorized by this harness")
    flags = evidence.get("feature_flags")
    if not isinstance(flags, dict):
        errors.append("feature_flags block is required")
    else:
        if flags.get("persona_model_posture") != "report_only":
            errors.append("persona_model_posture must remain report_only")
        if flags.get("model_probe_enabled") is not False:
            errors.append(
                "model_probe_enabled must remain false without spend approval"
            )
        if flags.get("agent_models_ui_enabled") is not False:
            errors.append(
                "agent_models_ui_enabled must remain false before readiness approval"
            )


def validate_deployment(evidence: dict[str, Any], errors: list[str]) -> None:
    deployment = evidence.get("deployment")
    if not isinstance(deployment, dict):
        errors.append("deployment block is required")
        return
    _require(
        "deployment",
        deployment,
        {
            "environment",
            "account_id",
            "region",
            "git_revision",
            "gateway_image_digest",
            "worker_image_digest",
            "webhook_version",
            "deploy_state_sha256",
        },
        errors,
    )
    if evidence.get("source_revision") != deployment.get("git_revision"):
        errors.append("deployment revision must match source_revision")
    for field in ("gateway_image_digest", "worker_image_digest"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(deployment.get(field, ""))):
            errors.append(f"deployment: {field} must be an image digest")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(deployment.get("deploy_state_sha256", ""))
    ):
        errors.append("deployment: deploy_state_sha256 must be a SHA-256")
    if not re.fullmatch(r"[0-9]{12}", str(deployment.get("account_id", ""))):
        errors.append("deployment: account_id must contain 12 digits")
    if deployment.get("mixed_version_nodes") is not False:
        errors.append("deployment: mixed_version_nodes must be explicitly false")
    health = deployment.get("health")
    if not isinstance(health, dict) or not all(
        health.get(key) is True
        for key in (
            "frontend_200",
            "gateway_200",
            "gateway_pods_running",
            "rds_available",
            "worker_spawned",
        )
    ):
        errors.append("deployment: every required health assertion must be true")


def assess(manifest: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    errors = validate_manifest(manifest)
    if evidence.get("schema_version") != 1 or evidence.get("story") != 5427:
        errors.append("evidence must have schema_version=1 and story=5427")
    for field in ("source_revision", "evidence_schema_revision"):
        if not _present(evidence.get(field)):
            errors.append(f"evidence must include {field}")
    if evidence.get("evidence_schema_revision") != "1.1":
        errors.append("evidence_schema_revision must be 1.1")
    revision = evidence.get("source_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        errors.append("source_revision must be an exact commit SHA")
    validate_safety(evidence, errors)
    validate_deployment(evidence, errors)
    provided = evidence.get("cells")
    if not isinstance(provided, dict):
        provided = {}
        errors.append("cells must be an object keyed by L1..L25")
    expected = {cell["id"]: cell for cell in manifest["cells"]}
    unknown = sorted(set(provided) - set(expected))
    if unknown:
        errors.append(f"unknown matrix cells: {', '.join(unknown)}")
    results: list[dict[str, Any]] = []
    for cell_id in sorted(expected, key=lambda item: int(item[1:])):
        cell = expected[cell_id]
        value = provided.get(cell_id, {"status": "not_run"})
        if not isinstance(value, dict) or value.get("status") not in STATUSES:
            errors.append(f"{cell_id}: invalid status")
            value = {"status": "fail"}
        status = value.get("status")
        before = len(errors)
        if status == "pass" and cell_id in ENFORCING_CELLS:
            errors.append(
                f"{cell_id}: structurally unpassable in the non-enforcing"
                " harness; requires an explicitly authorized enforcing assessor"
            )
        elif status == "pass":
            validate_common(cell, value, evidence.get("deployment") or {}, errors)
            if cell_id in MULTI_INVOCATION_CELLS:
                validate_multi_invocation(cell, value, errors)
            elif cell_id in CHAIN_CELLS:
                validate_chain(cell, value, errors)
            else:
                for kind in cell["evidence_kinds"]:
                    if kind == "A":
                        validate_kind_a(cell_id, value, errors)
            for kind in cell["evidence_kinds"]:
                if kind == "B":
                    validate_kind_b(cell, value, errors)
                elif kind == "C":
                    validate_kind_c(cell_id, value, errors)
                elif kind == "D":
                    validate_shadow(manifest, evidence, errors)
        elif status == "blocked" and not _present(value.get("blocker")):
            errors.append(f"{cell_id}: blocked status requires blocker")
        results.append(
            {
                "id": cell_id,
                "status": "fail"
                if len(errors) > before and status == "pass"
                else status,
            }
        )
    statuses = {row["id"]: row for row in results}
    if all(statuses[cell]["status"] == "pass" for cell in ("L1", "L2")):
        first = {
            (row["persona"], row["resolved_model"])
            for row in provided["L1"]["invocations"]
        }
        second = {
            (row["persona"], row["resolved_model"])
            for row in provided["L2"]["invocations"]
        }
        if first != second or any(
            provided["L1"].get(field) != provided["L2"].get(field)
            for field in ("tenant_id", "principal_id")
        ):
            errors.append("L2: CLI mappings must match L1 for the same canonical owner")
            statuses["L2"]["status"] = "fail"
    totals = {
        status: sum(result["status"] == status for result in results)
        for status in STATUSES
    }
    complete = False  # L23/L24 require a separately reviewed enforcing assessor.
    return {
        "schema_version": 1,
        "story": 5427,
        "complete": complete,
        "enforcement_ready": False,
        "totals": totals,
        "results": results,
        "errors": errors,
        "manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "notice": "Offline, non-enforcing assessment only; it cannot authorize spend, deployment, or a posture flip.",
    }


def template(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "story": 5427,
        "source_revision": "",
        "evidence_schema_revision": "1.1",
        "safety": {
            "enforcement_authorized": False,
            "posture_at_collection": "report_only",
            "probe_spend_ceiling_usd": 0,
        },
        "feature_flags": {
            "persona_model_posture": "report_only",
            "model_probe_enabled": False,
            "agent_models_ui_enabled": False,
        },
        "deployment": {},
        "cells": {cell["id"]: {"status": "not_run"} for cell in manifest["cells"]},
        "shadow_comparison": {
            "minimum_observations_per_path": 1,
            "observations": [],
            "rejected_events": [],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-manifest")
    sub.add_parser("template")
    shadow_parser = sub.add_parser("shadow-report")
    shadow_parser.add_argument("events", type=Path)
    assess_parser = sub.add_parser("assess")
    assess_parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    manifest = load_json(MANIFEST_PATH)
    if args.command == "validate-manifest":
        errors = validate_manifest(manifest)
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
        return int(bool(errors))
    if args.command == "template":
        print(json.dumps(template(manifest), indent=2))
        return 0
    if args.command == "shadow-report":
        report = shadow_report(args.events)
        print(json.dumps(report, indent=2))
        return int(bool(report["rejected_events"]))
    report = assess(manifest, load_json(args.evidence))
    print(json.dumps(report, indent=2))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    sys.exit(main())
