"""Version the V11 UI contribution; B09 still needs V10 and a human decision."""

from __future__ import annotations

import hashlib
import json

from tests.e2e.product_onboarding.product_v02_evidence import build_packet as build_product_packet

SCHEMA = "product-v11/v1"
CASES = {
    **{
        f"v11_{starting_point}": (
            "AC-01", "SCN-01",
            f"tests/e2e/new_ui/test_product_profile.py::test_product_full_journey[{starting_point}]",
        )
        for starting_point in ("idea", "existing_assets", "running_product")
    },
    "v11_resume": ("AC-02", "SCN-02", "tests/e2e/new_ui/test_product_resume.py::test_v11_cross_client_unavailable"),
    "v11_release": ("AC-03", "SCN-03", "tests/e2e/new_ui/test_product_profile.py::test_v11_release_regressions"),
}


def build_packet(inputs):
    packet = build_product_packet(inputs, case_specs=CASES, require_traces=False)
    issues = packet["issues"]
    observed = packet.get("deployment") or {}
    observed = observed.get("observed") if isinstance(observed, dict) else {}
    if not isinstance(observed, dict) or observed.get("product_onboarding_enabled") is not True:
        issues.append("V11 requires independently observed onboarding admission")
    identities = packet.get("identities") if isinstance(packet.get("identities"), dict) else {}
    if not isinstance(observed, dict) or observed.get("requirements_revision") != identities.get("requirements_revision"):
        issues.append("V11 observed requirements differ from the pinned contract")
    cases = packet.get("cases") or {}
    for name in CASES:
        case = cases.get(name) if isinstance(cases, dict) else None
        if not isinstance(case, dict):
            continue
        for key in ("source_revision", "requirements_revision", "frontend_release_ref", "api_image_digest", "schema_head"):
            if not case.get(key) or case[key] != identities.get(key):
                issues.append(f"{name} {key} differs from the pinned release")
        if (not isinstance(case.get("report_ref"), str) or not case["report_ref"]
                or case["report_ref"] in (case.get("readback_ref"), case.get("evidence_ref"))):
            issues.append(f"{name} needs an independent scenario report")
    resume = cases.get("v11_resume") if isinstance(cases, dict) else None
    if not isinstance(resume, dict):
        resume = {}
    states = resume.get("peer_availability")
    if not isinstance(states, dict) or set(states) != {"context", "l5"}:
        issues.append("V11 must show both optional peers as unavailable")
    else:
        for peer, label in (("context", "Product Context"), ("l5", "L5 services")):
            state = states[peer]
            if (not isinstance(state, dict) or set(state) != {"status", "reason", "message"}
                    or state.get("status") != "unavailable" or state.get("reason") != "integration_deferred"
                    or not isinstance(state.get("message"), str) or label not in state["message"]
                    or "saved Product remains available" not in state["message"]):
                issues.append(f"V11 {peer} must not claim peer dispatch, a receipt or a result")
    cli = (packet.get("surfaces") or {}).get("cli") if isinstance(packet.get("surfaces"), dict) else None
    if (not isinstance(cli, dict) or cli.get("status") != "pass"
            or cli.get("cli_checksum") != identities.get("cli_checksum")
            or cli.get("source_revision") != identities.get("source_revision")
            or resume.get("operation_ids") != []
            or not isinstance(resume.get("session_id"), str) or not resume["session_id"]
            or not isinstance(resume.get("cli_event_ref"), str) or not resume["cli_event_ref"]
            or resume.get("cli_event_ref") == resume.get("readback_ref")):
        issues.append("V11 needs verified installed CLI resume, no queued peer work and independent session evidence")
    packet["evidence_schema"] = SCHEMA
    packet["bolt_id"] = "B09"
    packet["contribution"] = "V11"
    packet["combined_status"] = "pending_v10_and_human_decision"
    packet["status"] = "contribution_ready" if not issues else "incomplete"
    packet.pop("packet_revision")
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return packet


def review_mapping(contribution):
    """Identify the V11 evidence for B09 without posing it as a combined packet or decision."""
    if (not isinstance(contribution, dict) or contribution.get("status") != "contribution_ready"
            or build_packet(contribution) != contribution):
        raise ValueError("Only a verified V11 contribution can be mapped to B09 review")
    identities = contribution["identities"]
    return {
        "bolt_id": "B09", "status": "pending_combination",
        "contributions": {
            "V10": {"issue": 8267, "status": "required"},
            "V11": {
                "issue": 8268, "status": "contribution_ready",
                "packet_revision": contribution["packet_revision"],
                "plan_revision": contribution["plan_revision"],
                "source_revision": identities["source_revision"],
                "requirements_revision": identities["requirements_revision"],
            },
        },
        "review": {
            "ac_id": "AC-04", "scenario_id": "SCN-04",
            "procedure_ref": (
                "docs/delivery-flow/plans/product-onboarding-8141/human-checks.md"
                "#record-one-decision-using-the-existing-verification-stories"
            ),
            "status": "not_ready", "packet_revision": None, "plan_revision": None,
        },
    }
