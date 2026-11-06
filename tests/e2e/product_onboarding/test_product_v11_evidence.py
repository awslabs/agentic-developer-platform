"""Synthetic V11 packet contracts; these are not deployed B09 acceptance."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v11_evidence import CASES, SCHEMA, build_packet
from tests.e2e.product_onboarding.test_product_v02_evidence import sample_packet


def sample_v11_packet():
    inputs = sample_packet()
    run_id = inputs["run_id"]
    template = inputs["cases"]["live_browser_create"]
    inputs["report_artifact"] = "private/v11/contribution"
    inputs["deployment"]["observed"].update(product_onboarding_enabled=True,
                                               requirements_revision=inputs["identities"]["requirements_revision"])
    inputs["cases"] = {}
    readbacks = {}
    for name, (criterion, scenario, selector) in CASES.items():
        case = deepcopy(template)
        case.update(ac_id=criterion, scenario_id=scenario, test_ref=selector,
                    source_revision=inputs["identities"]["source_revision"],
                    requirements_revision=inputs["identities"]["requirements_revision"],
                    frontend_release_ref=inputs["identities"]["frontend_release_ref"],
                    api_image_digest=inputs["identities"]["api_image_digest"],
                    schema_head=inputs["identities"]["schema_head"],
                    evidence_ref="private/ui/" + name, report_ref="private/scenario/" + name,
                    readback_ref="private/api/" + name)
        case["attempts"][0]["attempt_id"] = "42/1/" + name
        case["attempts"][0]["evidence_ref"] = "private/attempt/" + name
        inputs["cases"][name] = case
        readbacks[name] = case["readback_ref"]
    inputs["surfaces"]["api"]["case_readback_refs"] = readbacks
    inputs["surfaces"]["cli"].update(
        status="pass", command="adp onboard start --json", stdout_ref="private/cli/stdout",
        stderr_ref="private/cli/stderr", exit_code=0,
    )
    inputs["cases"]["v11_resume"].update(
        session_id="example-session", operation_ids=[], cli_event_ref="private/cli/start",
        peer_availability={
            peer: {"status": "unavailable", "reason": "integration_deferred",
                   "message": f"{label} integration is unavailable. Your saved Product remains available."}
            for peer, label in (("context", "Product Context"), ("l5", "L5 services"))
        },
    )
    return inputs


def test_v11_contribution_is_versioned_and_never_claims_b09_acceptance():
    inputs = sample_v11_packet()
    original = deepcopy(inputs)
    packet = build_packet(inputs)
    assert packet["status"] == "contribution_ready" and packet["issues"] == []
    assert packet["evidence_schema"] == SCHEMA and packet["bolt_id"] == "B09"
    assert packet["combined_status"] == "pending_v10_and_human_decision"
    assert packet["packet_revision"].startswith("sha256:")
    assert build_packet(inputs) == packet and inputs == original
    inputs["cases"]["v11_release"]["actual"] = "A different observation"
    assert build_packet(inputs)["packet_revision"] != packet["packet_revision"]


@pytest.mark.parametrize("change", [
    lambda data: data["cases"].pop("v11_release"),
    lambda data: data["cases"]["v11_idea"].update(status="skip"),
    lambda data: data["cases"]["v11_resume"]["attempts"][0].update(result="fail"),
    lambda data: data["cases"]["v11_resume"].update(requirements_revision="f" * 40),
    lambda data: data["cases"]["v11_release"].update(report_ref=data["cases"]["v11_release"]["readback_ref"]),
    lambda data: data["cases"]["v11_resume"].update(peer_availability={}),
    lambda data: data["cases"]["v11_resume"]["peer_availability"]["context"].update(status="complete"),
    lambda data: data["cases"]["v11_resume"]["peer_availability"]["l5"].update(receipt="invented"),
    lambda data: data["cases"]["v11_resume"].update(operation_ids=["queued-peer"]),
    lambda data: data["cases"]["v11_resume"].update(cli_event_ref=""),
    lambda data: data["surfaces"]["cli"].update(status="artifact_only"),
    lambda data: data["surfaces"]["cli"].update(cli_checksum="0" * 64),
    lambda data: data["surfaces"]["api"].update(status="skip"),
    lambda data: data["deployment"]["observed"].update(product_onboarding_enabled=False),
    lambda data: data["deployment"]["observed"].update(requirements_revision="e" * 40),
    lambda data: data["cleanup"].update(owned_absent=False),
    lambda data: data["identities"].update(source_revision="stale"),
])
def test_v11_packet_rejects_missing_stale_or_fabricated_evidence(change):
    inputs = sample_v11_packet()
    change(inputs)
    packet = build_packet(inputs)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert packet["combined_status"] == "pending_v10_and_human_decision"


def test_v11_packet_preserves_failed_and_skipped_attempts_before_retry():
    inputs = sample_v11_packet()
    attempts = inputs["cases"]["v11_release"]["attempts"]
    failed = deepcopy(attempts[0])
    failed.update(attempt_id="41/1/v11_release", workflow_run_id=41, result="fail")
    skipped = deepcopy(failed)
    skipped.update(attempt_id="41/2/v11_release", workflow_attempt=2, result="skip", reason="Fixture unavailable")
    attempts[:0] = [failed, skipped]
    packet = build_packet(inputs)
    assert packet["status"] == "contribution_ready"
    assert [attempt["result"] for attempt in packet["cases"]["v11_release"]["attempts"]] == ["fail", "skip", "pass"]
    del inputs["cases"]["v11_release"]["attempts"][1]["reason"]
    assert build_packet(inputs)["status"] == "incomplete"


def test_v11_review_mapping_requires_v10_and_one_combined_decision():
    from tests.e2e.product_onboarding.product_v11_evidence import review_mapping

    contribution = build_packet(sample_v11_packet())
    mapped = review_mapping(contribution)
    assert mapped == review_mapping(contribution)
    assert mapped["bolt_id"] == "B09" and mapped["status"] == "pending_combination"
    assert mapped["contributions"]["V10"] == {"issue": 8267, "status": "required"}
    assert mapped["contributions"]["V11"] == {
        "issue": 8268, "status": "contribution_ready",
        "packet_revision": contribution["packet_revision"],
        "plan_revision": contribution["plan_revision"],
        "source_revision": contribution["identities"]["source_revision"],
        "requirements_revision": contribution["identities"]["requirements_revision"],
    }
    assert mapped["review"]["ac_id"] == "AC-04"
    assert mapped["review"]["scenario_id"] == "SCN-04"
    assert mapped["review"]["status"] == "not_ready"
    assert mapped["review"]["packet_revision"] is None and mapped["review"]["plan_revision"] is None
    assert "decision" not in mapped


@pytest.mark.parametrize("change", [
    lambda packet: packet.update(status="review_ready"),
    lambda packet: packet.update(packet_revision="sha256:" + "f" * 64),
    lambda packet: packet.update(combined_status="accepted"),
    lambda packet: packet["cases"]["v11_resume"]["peer_availability"]["l5"].update(receipt="invented"),
    lambda packet: packet["cases"]["v11_release"]["attempts"][-1].update(result="skip"),
    lambda packet: packet["identities"].update(requirements_revision="f" * 40),
    lambda packet: packet["cleanup"].update(owned_absent=False),
    lambda packet: packet.update(review={"status": "accepted"}),
])
def test_v11_review_mapping_rejects_tampered_contribution(change):
    from tests.e2e.product_onboarding.product_v11_evidence import review_mapping

    contribution = build_packet(sample_v11_packet())
    change(contribution)
    with pytest.raises(ValueError, match="verified V11 contribution"):
        review_mapping(contribution)


def test_v11_review_mapping_refuses_incomplete_or_missing_contribution():
    from tests.e2e.product_onboarding.product_v11_evidence import review_mapping

    inputs = sample_v11_packet()
    inputs["cases"].pop("v11_resume")
    with pytest.raises(ValueError, match="verified V11 contribution"):
        review_mapping(build_packet(inputs))
    with pytest.raises(ValueError, match="verified V11 contribution"):
        review_mapping(None)
