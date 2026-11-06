"""Original periodic criterion requires collected cases, native correlation and installed source."""

from copy import deepcopy
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_periodic as periodic
import qualification_well_architected_acceptance as acceptance

START = "2026-10-31T10:00:00+00:00"
END = "2026-10-31T10:02:00+00:00"
AT = "2026-10-31T10:01:00+00:00"
CASE = {"scope": {"tenant_id": "tenant", "product_id": "product", "project_id": "project"},
        "binding_id": "binding", "provider_identity": {"account_id": "123456789012", "region": "us-east-1", "role_arn": "role-example"},
        "sources": [{"record_id": "source-example", "record_revision": "a" * 40}]}


def interval(kind):
    scope = {**CASE["scope"], "binding_id": CASE["binding_id"], "provider_identity": CASE["provider_identity"],
             "configuration_revision": 1, "configuration_hash": "a" * 64, "native_resource_ref": {"record_id": "profile-example"}}
    settings = {"enabled": kind != "disabled", "source_refs": CASE["sources"], "triggers": [
        {"trigger_id": "tick", "enabled": True, "kind": "schedule", "schedule": "* * * * *"}]}
    runtime = {"cursor": {"initialized_at": START, "next_due_at": None, "triggers": {
        "tick": {"reason": "trigger_disabled" if kind == "disabled" else None, "next_due_at": None}}},
        "events": [], "event_history": [], "runs": [], "work": [], "transport": []}
    native = {"source": "independent-native-read", "profile_arn": "profile-example", "contexts": [],
              "aggregation_configuration": [], "generations": []}
    first = {"contract": "wa-periodic-checkpoint/v1", "source": "authenticated-periodic-readback", "scope": scope,
             "selection": {"binding_id": "binding", "trigger_id": "tick"}, "settings": settings,
             "read_started_at": START, "observed_at": START, "runtime": runtime, "native": native, "execution_authorized": False}
    last = deepcopy(first)
    last.update(read_started_at=END, observed_at=END)
    audit = {"source": "independent-cloudtrail-read", "start": START, "end": END, "events": [],
             "pagination_complete": True, "delivery_complete": False}
    if kind != "disabled":
        reason = {"revoked": "delegation_denied", "budget": "budget_exhausted"}.get(kind)
        event = {"configuration_revision": 1, "event_id": "event", "run_id": None if reason else "run",
                 "state": "rejected" if reason else "admitted", "reason": reason}
        last["runtime"]["events"] = [event]
        last["runtime"]["event_history"] = [
            {"observation_id": "arrival", "kind": "received", "observed_at": AT, "state": "pending", "reason": None},
            {"observation_id": "decision", "kind": "decision", "observed_at": AT, "state": event["state"], "reason": reason}]
    if kind in {"enabled", "duplicate"}:
        facts = {"contract": "wa-start-transport/v1", "request_hash": "a" * 64, "name": "adp-wa-name",
                 "http_status": 200, "provider_request_id": "provider-request", "generation_id": "generation",
                 "response_profile_arn": "profile-example", "response_name": "adp-wa-name"}
        last["runtime"]["runs"] = [{"run_id": "run", "native_job_ref": {"generation_id": "generation"}}]
        last["runtime"]["work"] = [{"run_id": "run", "work_id": "work"}]
        last["runtime"]["transport"] = [
            {"observation_id": "before", "send_id": "send", "phase": "before_send", "run_id": "run", "observed_at": AT, "facts": facts},
            {"observation_id": "after", "send_id": "send", "phase": "after_call", "run_id": "run", "observed_at": AT, "facts": facts}]
        audit["events"] = [{"request_id": "provider-request", "name": "adp-wa-name", "error_code": None}]
        last["native"]["generations"] = [{"id": "generation", "name": "adp-wa-name"}]
        last["native"]["generation_details"] = [{"id": "generation", "name": "adp-wa-name", "profile_arn": "profile-example"}]
        if kind == "duplicate":
            last["runtime"]["event_history"].append({"observation_id": "duplicate", "kind": "duplicate", "observed_at": AT, "state": "admitted"})
    return {"contract": "wa-periodic-interval/v1", "before": {"checkpoint_id": "before", "snapshot": first, "snapshot_sha256": periodic.digest(first)},
            "after": {"checkpoint_id": "after", "snapshot": last, "snapshot_sha256": periodic.digest(last)},
            "native_audit": audit, "execution_authorized": False, "native_qualified": False}


def rehash(document):
    for field in ("before", "after"):
        document[field]["snapshot_sha256"] = periodic.digest(document[field]["snapshot"])


@pytest.mark.parametrize("kind", sorted(periodic.CASES))
def test_actual_original_cases_can_pass_but_zero_claim_is_only_selected_adp_occurrence(kind):
    result = periodic.evaluate_interval(kind, interval(kind), CASE, instrumented_release=True)
    assert result["outcome"] == "passed", result
    assert result["adp_start_count"] == (1 if kind in {"enabled", "duplicate"} else 0)
    assert result["accountwide_native_zero_proved"] is False
    assert "cloudtrail_delivery_is_delayed" in result["limitations"]


@pytest.mark.parametrize("change", ["uninstalled", "lost_response", "audit_delayed", "extra_send", "native_identity", "old_baseline", "no_duplicate"])
def test_attempts_and_missing_independent_facts_cannot_complete_periodic_case(change):
    document = interval("duplicate")
    after = document["after"]["snapshot"]["runtime"]
    if change == "lost_response":
        after["transport"].pop()
    elif change == "audit_delayed":
        document["native_audit"]["events"] = []
    elif change == "extra_send":
        after["transport"].append({**after["transport"][0], "observation_id": "extra", "send_id": "extra"})
    elif change == "native_identity":
        document["after"]["snapshot"]["native"]["generations"] = []
    elif change == "old_baseline":
        document["before"]["snapshot"]["runtime"]["runs"] = [{"run_id": "old"}]
    elif change == "no_duplicate":
        after["event_history"].pop()
    rehash(document)
    assert periodic.evaluate_interval("duplicate", document, CASE, instrumented_release=change != "uninstalled")["outcome"] == "incomplete"


@pytest.mark.parametrize("kind", ["disabled", "revoked", "budget"])
def test_no_event_or_no_native_audit_alone_is_never_zero_proof(kind):
    document = interval(kind)
    document["after"]["snapshot"]["runtime"]["runs"] = [{"run_id": "unexplained"}]
    rehash(document)
    assert periodic.evaluate_interval(kind, document, CASE, instrumented_release=True)["outcome"] == "incomplete"
    document = interval(kind)
    document["native_audit"] = {"source": "unavailable", "code": "periodic_native_audit_disabled"}
    assert "periodic_native_audit_disabled" in periodic.evaluate_interval(kind, document, CASE, instrumented_release=True)["blocker_codes"]


def test_collectors_only_accept_ids_and_revalidate_authenticated_scoped_checkpoints():
    selectors = [{"case": kind, "before_id": "before", "after_id": "after"} for kind in sorted(periodic.CASES)]
    responses = iter(interval(kind) for kind in sorted(periodic.CASES))

    def respond(request):
        assert request.method == "POST" and request.url.path.endswith("/periodic-intervals/read")
        return httpx.Response(200, json=next(responses))

    def connection(case):
        return httpx.Client(base_url="https://gateway.example.org", transport=httpx.MockTransport(respond))

    result = periodic.collect({**CASE, "qualification_periodic": selectors}, connection)
    assert len(result["intervals"]) == 5
    bad = deepcopy(selectors)
    bad[0]["provider_count"] = 0
    with pytest.raises(ValueError, match="facts or counts"):
        periodic.collect({**CASE, "qualification_periodic": bad}, connection)


def test_complete_periodic_facts_map_only_original_wa06_ac_scenario_and_live_check():
    native = {**CASE, "run_id": "native", "result_revision": 1, "result": {"scope": CASE["scope"]}}
    context = {"source_revision": "a" * 40}
    installed = {"source": "independent-installed-read", "source_revision": context["source_revision"], "snapshot_sha256": ["a", "b", "c"]}
    installed["observation_interval"] = {"start": START, "end": END}
    evidence = {"source": "authenticated-periodic-readback", "intervals": [{"case": kind, "document": interval(kind)} for kind in sorted(periodic.CASES)]}
    observed = {"run_id": "native", "result_revision": 1, "scope": CASE["scope"], "periodic": evidence}
    from qualification_installed_artifacts import case_identity
    installed["case_identities"] = [case_identity(native), case_identity(native)]
    report = acceptance.evaluate([native, native], [observed, observed], context, installed=installed)
    for section, identifier in (("story_criteria", "WA06/AC-02"), ("scenarios", "WA06/SCN-02"), ("live_checks", "periodic-admission")):
        assert next(row for row in report[section] if row["id"] == identifier)["outcome"] == "passed"
    assert report["outcome"] == "incomplete"
    assert next(row for row in report["live_checks"] if row["id"] == "uncertain-generation-recovery")["outcome"] == "incomplete"


def test_cleanup_recollection_ignores_only_audit_read_time_and_preserves_audit_facts():
    from qualification_well_architected_release import stable_cleanup
    document = {"schema_version": 1, "context": {"source_revision": "a" * 40},
                "native_observation_sha256": "b" * 64,
                "evaluations": [{"periodic": {"intervals": [{"case": "enabled", "document": interval("enabled")}]}}]}
    audit = document["evaluations"][0]["periodic"]["intervals"][0]["document"]["native_audit"]
    audit["observed_at"] = START
    recollected = deepcopy(document)
    fresh_audit = recollected["evaluations"][0]["periodic"]["intervals"][0]["document"]["native_audit"]
    fresh_audit["observed_at"] = END
    assert stable_cleanup(document) == stable_cleanup(recollected)
    assert audit["observed_at"] == START and fresh_audit["observed_at"] == END
    fresh_audit["events"][0]["request_id"] = "another-request"
    assert stable_cleanup(document) != stable_cleanup(recollected)
