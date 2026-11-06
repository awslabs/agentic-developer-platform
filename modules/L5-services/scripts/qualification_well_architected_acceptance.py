"""Evaluate the original WA catalog from collected facts, never from pass flags."""

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "docs/l5-services/well-architected/story-specs.json"
SCOPE = "well_architected_release_acceptance"
LIVE_CHECKS = (
    "same-artifacts", "runtime-revisions", "schema-compatible", "provider-eligibility-qualified",
    "source-only-profile", "native-profile-scope-claim", "exhaustive-source-coverage",
    "native-recommendations", "uncertain-generation-recovery", "source-retention", "ui-cli-mcp-parity",
    "periodic-admission", "context-reuse", "two-non-adp-projects", "owned-cleanup",
)
NEXT = {
    "provider-eligibility-qualified": "Read the current selected provider identity and eligible source-only Profile through Product authority.",
    "source-only-profile": "Independently read the selected Profile and confirm its admitted source-only configuration.",
    "exhaustive-source-coverage": "Verify every admitted source file against the pinned archive and source snapshots; retain the provider-ingestion limitation.",
    "native-recommendations": "Read terminal native recommendations and verify their exact generation and source comparison citations.",
    "same-artifacts": "Read the actual package, gateway, worker, CLI and frontend digests before and after both onboardings.",
    "runtime-revisions": "Verify the installed selected service against the immutable release manifest.",
    "schema-compatible": "Read installed migration heads and retain compatibility and rollback observations for existing service rows.",
    "native-profile-scope-claim": "Read the retained native Profile claim through the current owner-authorized native observation API.",
    "uncertain-generation-recovery": "Exercise the admitted lost-response, restart and local-timeout cases; retain original intent and independent Start-call evidence.",
    "source-retention": "Observe sources during remote uncertainty and retained results after terminal owned-only cleanup.",
    "ui-cli-mcp-parity": "Complete the installed configuration, check, replay and result journeys. UI requires a manual journey while browser automation is prohibited.",
    "periodic-admission": "Exercise the explicitly enabled and disabled schedule, duplicate event and revoked-authority cases through common admission.",
    "context-reuse": "Collect both consumer acknowledgements, later canonical-task usage and foreign/revoked-source denial at the same result revision.",
    "two-non-adp-projects": "Verify distinct IaC/goals and unchanged installed release digests across both selected non-ADP projects.",
    "owned-cleanup": "Clean only explicitly selected terminal receipt-owned resources, then independently verify absence and adopted/result retention.",
}


REMAINING = {
    "schema-compatible": ["installed migration compatibility before/after", "retained-row-safe rollback proof"],
    "uncertain-generation-recovery": ["lost-Start-response trace", "worker restart trace", "local-timeout/remote-running trace", "independent Start call count"],
    "source-retention": ["source object/version presence during remote uncertainty", "reader revocation denial", "terminal proof and policy-safe hold release"],
    "ui-cli-mcp-parity": ["manual installed UI configure/check/replay/result journey", "installed API/CLI/MCP full journeys at exact release digests"],
    "periodic-admission": ["enabled tick admission", "disabled tick refusal", "duplicate event deduplication", "revoked-authority refusal"],
    "context-reuse": ["both consumer receipts at one result revision", "completed later canonical task and linked usage receipts", "foreign-reader denial", "source revocation denial", "independent provider call count before/after reuse"],
    "two-non-adp-projects": ["independent non-ADP project provenance", "pinned distinct IaC and goals", "three unchanged installed artifact checkpoints"],
}


def catalog():
    document = json.loads(CATALOG.read_text())
    stories = [(story["alias"] + "/" + check["id"], check) for story in document["stories"] for check in story["checks"]]
    return stories, "sha256:" + sha256(CATALOG.read_bytes()).hexdigest()


def evaluate(cases, observations, context, integration=None, installed=None, compatibility=None):
    """Native cases have already passed independent source/provider verification.

    A current-state snapshot is deliberately insufficient evidence for a fault
    injection, a historical cleanup, an installed UI journey or provider charges.
    Partial progress is reported under the original IDs and cannot pass delivery.
    """
    if len(cases) != 2 or len(observations) != 2:
        raise ValueError("two independently collected WA cases required")
    if compatibility is not None:
        from qualification_well_architected_compatibility import LEGACY_LIMITATIONS, LIMITATIONS
        if (compatibility.get("contract") != "wa-installed-compatibility-progress/v1"
                or compatibility.get("source_revision") != context["source_revision"]
                or compatibility.get("environment") != context.get("environment")
                or compatibility.get("id") != "WA07/AC-01" or compatibility.get("scenario") != "WA07/SCN-01"
                or compatibility.get("outcome") != "incomplete" or compatibility.get("native_qualified") is not False
                or compatibility.get("limitations") not in (LIMITATIONS, LEGACY_LIMITATIONS)):
            raise ValueError("compatibility progress cannot promote unobserved original acceptance")
    for case, observed in zip(cases, observations, strict=True):
        if (observed["run_id"] != case["run_id"] or observed["result_revision"] != case["result_revision"]
                or observed["scope"] != case["result"]["scope"]):
            raise ValueError("acceptance observations refer to another case")
    available = {"provider-eligibility-qualified", "source-only-profile", "exhaustive-source-coverage", "native-recommendations"}
    if installed is not None:
        from qualification_installed_artifacts import case_identity
        if (installed.get("source") != "independent-installed-read"
                or installed.get("source_revision") != context["source_revision"]
                or installed.get("case_identities") != [case_identity(case) for case in cases]
                or len(installed.get("snapshot_sha256", [])) != 3):
            raise ValueError("installed artifact observations differ from the two selected native cases")
        available.update({"same-artifacts", "runtime-revisions"})
        if any("portability" in item for item in observations):
            from qualification_well_architected_portability import verify
            verify(cases, observations, installed, context)
            available.add("two-non-adp-projects")
    if all(item.get("runtime", {}).get("profile_claim_matches") is True for item in observations):
        available.add("native-profile-scope-claim")
    if (any(item.get("cleanup", {}).get("owned_absent") is True
            and item["cleanup"].get("source") == "independent-native-read"
            and item["cleanup"].get("result_retained") is True for item in observations)
            and any(item.get("adopted_profile_unchanged") is True for item in observations)):
        available.add("owned-cleanup")
    # Even WA04/AC-01 requires dispatch timing and one native Start. A terminal
    # result and a durable attempt are useful progress, not that whole proof.
    passed_stories = set()
    integration_stories = set()
    from qualification_well_architected_reuse_acceptance import evaluate as evaluate_reuse
    reuse_progress = [evaluate_reuse(case, observed, installed_verified="same-artifacts" in available)
                      for case, observed in zip(cases, observations, strict=True)]
    if any(progress["outcome"] == "passed" for progress in reuse_progress):
        passed_stories.add("WA05/AC-03")
    reuse_effect_progress = []
    for case, observed, reuse in zip(cases, observations, reuse_progress, strict=True):
        if "reuse_interval" in observed:
            from qualification_well_architected_reuse_intervals import evaluate as evaluate_reuse_interval
            progress = evaluate_reuse_interval(observed["reuse_interval"], case, observed.get("clients", {}), context, installed)
            reuse_effect_progress.append(progress)
            if reuse["outcome"] == "passed" and progress["outcome"] == "passed":
                available.add("context-reuse")
    periodic_progress = []
    for case, observed in zip(cases, observations, strict=True):
        if "periodic" in observed:
            from qualification_well_architected_periodic import evaluate as evaluate_periodic
            selected = {"scope": case["result"]["scope"], "binding_id": case["binding_id"],
                        "provider_identity": case["provider_identity"], "sources": case["sources"]}
            progress = evaluate_periodic(observed["periodic"], selected, context, installed)
            periodic_progress.append(progress)
            if progress["outcome"] == "passed":
                available.add("periodic-admission")
                passed_stories.add("WA06/AC-02")
    if integration is not None:
        from qualification_well_architected_integrations import SELECTORS, selector_digest

        if (integration.get("source_revision") != context["source_revision"]
                or integration.get("selectors_sha256") != selector_digest()
                or len(integration.get("criteria", [])) != len(SELECTORS)
                or {row.get("id") for row in integration.get("criteria", [])} != set(SELECTORS)):
            raise ValueError("integration evidence differs from the original selected source criteria")
        integration_stories = {row["id"] for row in integration["criteria"]
                               if row.get("outcome") == "passed" and row.get("evidence_verified") is True}
        passed_stories.update(integration_stories)
    stories, revision = catalog()
    unresolved = []

    def row(identifier, passed, code=None):
        result = {"id": identifier, "outcome": "passed" if passed else "incomplete",
                  "evidence_verified": passed, "observation": "independent-live",
                  "verification_layer": "installed-product-and-native",
                  "evidence_ref": "native-observations.json"}
        if not passed:
            result["blocker_code"] = code or "scenario_not_exercised"
        return result

    story_rows = [row(identifier, identifier in passed_stories) for identifier, _ in stories]
    scenario_rows = [row(identifier.replace("/AC-", "/SCN-"), identifier in passed_stories) for identifier, _ in stories]
    for record in [*story_rows, *scenario_rows]:
        if record["id"].replace("/SCN-", "/AC-") in integration_stories:
            record.update(observation="executed-integration", verification_layer="source-integration", evidence_ref="integration-results.xml")
    epic_rows = []
    for number in range(1, 9):
        identifier = f"EC-{number:02d}"
        children = [name for name, check in stories if identifier in check["ec"]]
        epic_rows.append(row(identifier, bool(children) and all(child in passed_stories for child in children)))
    checks = []
    for identifier in LIVE_CHECKS:
        passed = identifier in available
        checks.append(row(identifier, passed, "installed_case_not_exercised"))
        if not passed:
            unresolved.append({"code": identifier + ":not-observed", "next_action": NEXT[identifier],
                               "required_evidence": REMAINING.get(identifier, [NEXT[identifier]])})
    progress = []
    for observed in observations:
        clients = observed.get("clients")
        if clients:
            consumers = clients["publication"].get("consumers", [])
            progress.append({
                "run_id": observed["run_id"], "result_revision": observed["result_revision"],
                "read_clients": sorted(clients["clients"]),
                "consumer_acknowledgements": [item["destination"] for item in consumers
                    if item.get("state") == "published" and item.get("projection_verified_on_ack") is True],
                "later_task_receipt_observed": "usage" in clients,
                "canonical_completed_task_observed": clients.get("usage", {}).get("task", {}).get("status") == "completed",
                "retained_result_consent_observed": clients.get("usage", {}).get("retained_authorization") is not None,
                "source_revocation_observed": clients.get("source_revocation", {}).get("source_revocation_observed") is True,
                "provider_effect_count_observed": type(clients.get("usage", {}).get("provider_effect_count")) is int,
                "selected_reader_denied": clients.get("denial", {}).get("status") == 404,
            })
    runtime_progress = [{"run_id": item["run_id"], "result_revision": item["result_revision"],
                         "durable_attempts_observed": len(item.get("runtime", {}).get("attempts", [])),
                         "collection_complete": item.get("runtime", {}).get("collection", {}).get("complete") is True
                            if isinstance(item.get("runtime", {}).get("collection"), dict) else False}
                        for item in observations]
    completed = all(record["outcome"] == "passed" for record in [*epic_rows, *story_rows, *scenario_rows, *checks])
    limitations = ["provider_source_coverage_unverified"]
    if "ui-cli-mcp-parity" not in available:
        limitations.append("ui_not_exercised")
    return {
        "schema_version": 1, **context, "requirements_revision": revision,
        "qualification_scope": SCOPE, "observed_at": datetime.now(UTC).isoformat(),
        "outcome": "passed" if completed else "incomplete", "live_acceptance": "passed" if completed else "incomplete",
        "limitations": limitations,
        "epic_criteria": epic_rows, "story_criteria": story_rows, "scenarios": scenario_rows,
        "live_checks": checks, "cleanup": {"outcome": "passed" if "owned-cleanup" in available else "incomplete"},
        "unresolved": unresolved, "client_progress": progress, "runtime_progress": runtime_progress,
        "installed_progress": installed,
        "compatibility_progress": compatibility,
        "periodic_progress": periodic_progress,
        "reuse_progress": reuse_progress,
        "reuse_effect_progress": reuse_effect_progress,
    }


def validate_catalog(report):
    """An emitter cannot omit required rows or smuggle a partial catalog as pass."""
    stories, revision = catalog()
    expected = {"epic_criteria": {f"EC-{number:02d}" for number in range(1, 9)},
                "story_criteria": {identifier for identifier, _ in stories},
                "scenarios": {identifier.replace("/AC-", "/SCN-") for identifier, _ in stories},
                "live_checks": set(LIVE_CHECKS)}
    if report.get("requirements_revision") != revision:
        raise ValueError("WA report does not use the reviewed acceptance catalog")
    for section, identifiers in expected.items():
        rows = report.get(section)
        if (not isinstance(rows, list) or len(rows) != len(identifiers)
                or any(not isinstance(row, dict) for row in rows)
                or {row.get("id") for row in rows} != identifiers):
            raise ValueError("WA report omits or changes an original acceptance identifier")
