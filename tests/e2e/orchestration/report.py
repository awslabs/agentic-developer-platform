"""Fail-closed Q2 reports. A passing process is not acceptance evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tests.e2e.orchestration.scenarios.definitions import CRITERIA, DEFINITION_HASH


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Artifact(Strict):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(min_length=1, max_length=2048)
    observed_at: str


class Result(Strict):
    id: str
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    detail: str = Field(min_length=1, max_length=4000)
    evidence: dict[str, Artifact] = Field(default_factory=dict)
    run_ids: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)


class Intervention(Strict):
    at: str
    actor: str = Field(min_length=1)
    kind: Literal[
        "planned_gate",
        "planned_setup",
        "required_review",
        "fault",
        "cleanup",
        "coordinator_retrigger",
        "unplanned",
    ]
    target: str = Field(min_length=1)
    evidence: Artifact


class ScenarioReport(Strict):
    schema_version: Literal[1] = 1
    qualification_id: str
    definition_hash: str
    manifest_hash: str
    live: bool
    started_at: str
    completed_at: str
    versions: dict[str, str]
    checkout_revision: str | None = None
    # Actual server-issued provenance, not a copy of the requested policy.
    policy_id: str | None = None
    policy_hash: str | None = None
    plan_version: int | None = None
    pull_requests: list[dict] = Field(default_factory=list)
    deployments: list[dict] = Field(default_factory=list)
    results: list[Result]
    spend_usd: float | None
    cleanup_inventory: list[dict]
    interventions: list[Intervention]
    interventions_complete: bool
    planned_gates: list[str]


def timestamp(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return moment


def check_artifact(artifact, root, started, completed):
    path = Path(artifact.path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("artifact path escapes qualification")
    base = Path(root).resolve()
    source = base / path
    if (
        not source.resolve().is_relative_to(base)
        or source.is_symlink()
        or any(p.is_symlink() for p in source.parents if p != base)
    ):
        raise ValueError("artifact symlink/escape")
    if not source.is_file() or not 0 < source.stat().st_size <= 8 * 1024 * 1024:
        raise ValueError("artifact missing or oversized")
    if hashlib.sha256(source.read_bytes()).hexdigest() != artifact.sha256:
        raise ValueError("artifact hash mismatch")
    if not started <= timestamp(artifact.observed_at) <= completed:
        raise ValueError("artifact outside run interval")


def assess(report, *, config, inventory, manifest_hash=None):
    """Validate the whole mandatory matrix, returning reasons preventing PASS.

    This validator verifies accounting/integrity. Scenario assertions establish
    semantics from service observations; JSON authored by a caller is not an
    authenticated E1 receipt (evaluation_receipt.py owns that separate contract).
    """
    report = ScenarioReport.model_validate(report)
    errors = []
    if report.qualification_id != inventory.qualification_id:
        errors.append("foreign qualification inventory")
    if report.definition_hash != DEFINITION_HASH:
        errors.append("scenario definition changed")
    if manifest_hash is not None and report.manifest_hash != manifest_hash:
        errors.append("accepted manifest changed")
    if not report.live:
        errors.append("non-live simulation")
    elif not report.checkout_revision:
        errors.append("actual checkout revision missing")
    if report.versions != config.versions:
        errors.append("actual engine/worker/harness revision mismatch")
    started, completed = timestamp(report.started_at), timestamp(report.completed_at)
    if (
        completed < started
        or (completed - started).total_seconds() > config.max_duration_seconds
    ):
        errors.append("duration bound exceeded")
    if report.spend_usd is None or not 0 <= report.spend_usd <= config.max_usd:
        errors.append("spend unknown or exceeded")
    if not report.policy_id or not report.policy_hash or not report.plan_version:
        errors.append("actual accepted policy provenance missing")
    if report.planned_gates != ["release", "refusal"]:
        errors.append("planned gate inventory changed")
    try:
        from .scenarios.delivery import assert_review_repair
        from .scenarios.manifest import load_manifest

        manifest, _ = load_manifest(config)
        assert_review_repair(report.pull_requests, manifest.required_checks)
        assert len(report.deployments) == 2
        for row, deployment in zip(report.pull_requests, report.deployments):
            pr = row["pr"]
            assert pr["base"]["repo"]["full_name"] == config.repository
            assert deployment["source_revision"] == pr["merge_commit_sha"]
            runtime = deployment["runtime"]
            assert runtime["actual_revision"] == pr["merge_commit_sha"]
            assert runtime["account_id"] == config.expected_account_id
            assert runtime["scope"] == "ready deployed replicas"
            assert runtime["ready_replicas"] > 0 and runtime["pods"]
            assert (
                runtime["digest"].startswith("sha256:") and len(runtime["digest"]) == 71
            )
            assert {r["path"] for r in deployment["workflows"]} == set(
                manifest.deployment_workflows
            )
            assert all(
                r["head_sha"] == pr["merge_commit_sha"] and r["conclusion"] == "success"
                for r in deployment["workflows"]
            )
    except (AssertionError, ValueError, KeyError, TypeError, OSError):
        errors.append(
            "PR/head/review/merge/deployment provenance incomplete or inconsistent"
        )
    ids = [r.id for r in report.results]
    if len(ids) != len(set(ids)) or set(ids) != {c.id for c in CRITERIA}:
        errors.append("mandatory criterion matrix missing, duplicated or extended")
    required = {c.id: set(c.evidence) for c in CRITERIA}
    artifacts = []
    for result in report.results:
        if result.status != "PASS":
            errors.append(f"{result.id}: {result.status}")
        elif not required.get(result.id, set()) <= result.evidence.keys():
            errors.append(f"{result.id}: mandatory evidence missing")
        artifacts.extend(result.evidence.values())
    if not report.interventions_complete:
        errors.append("intervention accounting incomplete")
    for entry in report.interventions:
        if entry.kind in {"coordinator_retrigger", "unplanned"}:
            errors.append("unattended completion violated")
        if entry.kind == "planned_gate" and entry.target not in report.planned_gates:
            errors.append("unplanned human gate")
        if not started <= timestamp(entry.at) <= completed:
            errors.append("intervention outside run")
        artifacts.append(entry.evidence)
    if report.cleanup_inventory != [r.to_json() for r in inventory.fixtures]:
        errors.append("cleanup inventory incomplete")
    if not report.cleanup_inventory:
        errors.append("no isolated fixtures")
    if len(artifacts) > 256:
        errors.append("artifact count exceeded")
    else:
        for artifact in artifacts:
            try:
                check_artifact(artifact, inventory.path.parent, started, completed)
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
    return report, list(dict.fromkeys(errors))


def write_report(report, *, config, inventory, manifest_hash=None):
    report, errors = assess(
        report, config=config, inventory=inventory, manifest_hash=manifest_hash
    )
    document = report.model_dump(mode="json")
    document.update(overall="PASS" if not errors else "INCOMPLETE", blockers=errors)
    base = inventory.path.parent
    (base / "report.json").write_text(json.dumps(document, indent=2) + "\n")
    lines = [
        f"Qualification {report.qualification_id}: {document['overall']}",
        "",
        f"Live: {report.live}",
        f"Definition: {report.definition_hash}",
        f"Manifest: {report.manifest_hash}",
        "",
        f"Versions: {json.dumps(report.versions, sort_keys=True)}",
        f"Checkout: {report.checkout_revision or 'UNKNOWN'}",
        f"Policy: {report.policy_id or 'UNKNOWN'} / {report.policy_hash or 'UNKNOWN'}; plan {report.plan_version}",
        f"Observed: {report.started_at} to {report.completed_at}",
        "",
        "| Criterion | Result | Detail | Evidence |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {r.id} | {r.status} | {r.detail.replace('|', '/').replace(chr(10), ' ')} | "
        + ", ".join(
            f"[{kind}]({a.path}) (`{a.sha256}`)" for kind, a in r.evidence.items()
        )
        + " |"
        for r in report.results
    )
    for row in report.pull_requests:
        pr = row.get("pr", {})
        lines.append(
            f"PR {pr.get('html_url', pr.get('number', 'UNKNOWN'))}: head `{pr.get('head', {}).get('sha')}`; merge `{pr.get('merge_commit_sha')}`"
        )
    lines.extend(
        f"- {i.at}: {i.kind}, {i.actor}, {i.target}; [evidence]({i.evidence.path})"
        for i in report.interventions
    )
    lines.extend(
        [
            "",
            f"Spend: {report.spend_usd if report.spend_usd is not None else 'UNKNOWN'} USD",
            f"Inventory: inventory.json ({len(report.cleanup_inventory)} records)",
            f"Interventions: {len(report.interventions)}; complete={report.interventions_complete}",
            "",
        ]
    )
    lines.extend(f"- {error}" for error in errors)
    (base / "summary.md").write_text("\n".join(lines) + "\n")
    return document


def now():
    return datetime.now(UTC).isoformat()
