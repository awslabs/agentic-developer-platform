"""Managed dedicated source fields and recovered phase identity fixtures."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError, link_phases
from superplane_acceptance.demo1_provider import observe_provider
from superplane_acceptance.demo1_report import assemble_report


def identifier(number: int) -> str:
    return str(UUID(int=number))


@pytest.fixture
def selection() -> dict:
    return {
        "version": "demo1-v1",
        "release_source": "a" * 40,
        "image_digest": "b" * 64,
        "schema_revision": "013",
        "connection_id": identifier(1),
        "role": "ExampleObserver",
        "account": "123456789012",
        "region": "us-east-1",
        "org_id": identifier(2),
        "requester_id": identifier(3),
        "approver_id": identifier(4),
        "workspace_name": "example-workspace",
        "mode": "managed",
        "cluster_placement": "dedicated",
        "request_id": identifier(5),
        "plan_revision": "c" * 64,
        "budget_usd": "10.00",
        "authorized_at": "2026-10-05T10:00:00+00:00",
        "deadline": "2026-10-05T12:00:00+00:00",
        "cleanup_owner": "example-operator",
        "recovery_checkpoint": "after-bootstrap",
        "survivors": ["example-shared-vpc"],
    }


@pytest.fixture
def phase(selection: dict) -> dict:
    return {
        "workspace_id": identifier(6),
        "request_id": selection["request_id"],
        "phase_name": "creation",
        "admitted_at": "2026-10-05T10:55:00+00:00",
        "operation_id": identifier(7),
        "approval_id": identifier(8),
        "plan_revision": selection["plan_revision"],
        "artifact_digest": "d" * 64,
        "attempt_id": "attempt-1",
        "fence": "fence-1",
        "observed_at": "2026-10-05T11:00:00+00:00",
        "source": "fixture",
        "state": "observed",
        "approval": {
            "approval_id": identifier(8),
            "requester": selection["requester_id"],
            "approvers": [selection["approver_id"]],
            "result": "allowed-once",
            "decided_by": selection["approver_id"],
            "decided_at": "2026-10-05T10:45:00+00:00",
            "expires_at": "2026-10-05T11:00:00+00:00",
            "revoked": False,
        },
    }


def test_selection_links_single_workspace_and_original_request_across_retry(
    selection, phase
):
    selected = DemoInput.parse(selection)
    retry = {
        **phase,
        "attempt_id": "attempt-2",
        "fence": "fence-2",
        "observed_at": "2026-10-05T11:01:00+00:00",
        "state": "unknown",
    }
    linked = link_phases(selected, [phase, retry])
    assert {item.workspace_id for item in linked} == {identifier(6)}
    assert {item.request_id for item in linked} == {selected.request_id}
    assert [item.state for item in linked] == ["observed", "unknown"]
    assert all(item.source == "fixture" for item in linked)


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("mode", "adopt"),
        ("cluster_placement", "shared"),
        ("budget_usd", "NaN"),
        ("budget_usd", "0"),
        ("deadline", "2026-10-05T09:00:00+00:00"),
        ("approver_id", identifier(3)),
        ("survivors", []),
    ],
)
def test_selection_refuses_unsafe_inputs_without_echo(selection, field, invalid):
    selection[field] = invalid
    with pytest.raises(EvidenceError) as error:
        DemoInput.parse(selection)
    assert "123456789012" not in str(error.value)


def test_phase_refuses_replayed_and_foreign_request(selection, phase):
    selected = DemoInput.parse(selection)
    with pytest.raises(EvidenceError, match="request or plan"):
        link_phases(selected, [{**phase, "request_id": identifier(11)}])
    with pytest.raises(EvidenceError, match="replayed"):
        link_phases(selected, [phase, phase])


def test_phase_outside_authorized_window_never_links(selection, phase):
    with pytest.raises(EvidenceError, match="window"):
        link_phases(
            DemoInput.parse(selection),
            [{**phase, "observed_at": "2026-10-06T11:00:00+00:00"}],
        )


def inventory(query, **overrides):
    return {
        "connection_id": query.connection_id,
        "role": query.role,
        "account": query.account,
        "region": query.region,
        "workspace_id": query.workspace_id,
        "status": "complete",
        "owned_present": [],
        "survivors_present": list(query.expected_survivors),
        "cost_usd": "1.25",
        "observed_at": "2026-10-05T11:30:00+00:00",
        **overrides,
    }


def test_provider_reader_queries_exact_selected_target_and_returns_only_diagnostic(
    selection,
):
    selected = DemoInput.parse(selection)
    seen = []

    def read(query):
        seen.append(query)
        return inventory(query)

    assessment = observe_provider(
        selected,
        identifier(6),
        ("example-owned-cluster",),
        SimpleNamespace(read_inventory=read),
    )
    assert len(seen) == 1
    assert (seen[0].account, seen[0].connection_id, seen[0].role, seen[0].region) == (
        selected.account,
        selected.connection_id,
        selected.role,
        selected.region,
    )
    assert seen[0].expected_survivors == selected.survivors
    assert (assessment.cleanup, assessment.survivors, assessment.cost) == (
        "BLOCKED",
        "BLOCKED",
        "BLOCKED",
    )
    assert assessment.origin == "fixture"


def test_provider_refuses_missing_owned_baseline_before_read(selection):
    def never_read(query):
        pytest.fail("must not query without a creation inventory")

    with pytest.raises(EvidenceError, match="baseline"):
        observe_provider(
            DemoInput.parse(selection),
            identifier(6),
            (),
            SimpleNamespace(read_inventory=never_read),
        )


def test_provider_cannot_upgrade_unregistered_origin(selection):
    with pytest.raises(EvidenceError, match="unregistered"):
        observe_provider(
            DemoInput.parse(selection),
            identifier(6),
            ("example-owned-cluster",),
            SimpleNamespace(read_inventory=lambda query: inventory(query)),
            origin="live",
        )


def test_linked_report_preserves_phase_lineage_without_live_claim(selection, phase):
    retry = {
        **phase,
        "phase_name": "bootstrap",
        "operation_id": identifier(9),
        "approval_id": identifier(10),
        "approval": {**phase["approval"], "approval_id": identifier(10)},
        "artifact_digest": "e" * 64,
        "attempt_id": "attempt-2",
        "fence": "fence-2",
        "state": "unknown",
        "observed_at": "2026-10-05T11:01:00+00:00",
    }
    report = assemble_report(
        DemoInput.parse(selection),
        [phase, retry],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(read_inventory=lambda query: inventory(query)),
    )
    assert report["version"] == "demo1-report-v1"
    assert report["evidence_mode"] == "offline-fixture"
    assert report["live_acceptance"] is False
    assert report["overall"] == "BLOCKED"
    assert len(report["phases"]) == 2
    assert report["phases"][0]["operation_ref"] != report["phases"][1]["operation_ref"]
    assert report["phases"][0]["approval_ref"] != report["phases"][1]["approval_ref"]
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
    assert report["checks"]["serving"]["status"] == "NOT RUN"
    serialized = str(report)
    for private in (
        selection["account"],
        selection["role"],
        selection["connection_id"],
        phase["workspace_id"],
        phase["operation_id"],
        phase["approval_id"],
    ):
        assert private not in serialized


def test_unlinked_phase_stops_provider_reader_and_reports_fail(selection, phase):
    def never_read(query):
        pytest.fail("reader must not run after invalid phase evidence")

    report = assemble_report(
        DemoInput.parse(selection),
        [{**phase, "workspace_id": identifier(21)}, phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(read_inventory=never_read),
    )
    assert report["overall"] == "FAIL"
    assert report["checks"]["creation"]["status"] == "FAIL"
    assert report["checks"]["cleanup"]["status"] == "NOT RUN"


def test_fabricated_domain_source_does_not_upgrade_fixture_to_live(selection, phase):
    purported_domain_record = {**phase, "source": "domain", "state": "observed"}
    report = assemble_report(DemoInput.parse(selection), [purported_domain_record])
    assert report["phases"][0]["source"] == "domain"
    assert report["evidence_mode"] == "offline-fixture"
    assert report["live_acceptance"] is False
    assert report["checks"]["creation"]["status"] == "BLOCKED"
    assert report["overall"] == "BLOCKED"


@pytest.mark.parametrize(
    "fabrication",
    [
        {"artifact_digest": "forged"},
        {"source": "unauthenticated-controller"},
        {"passed": True},
    ],
)
def test_malformed_or_self_declared_authority_fails_before_observation(
    selection,
    phase,
    fabrication,
):
    def never_read(query):
        pytest.fail("invalid evidence must not trigger a provider read")

    report = assemble_report(
        DemoInput.parse(selection),
        [{**phase, **fabrication}],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(read_inventory=never_read),
    )
    assert report["overall"] == "FAIL"
    assert report["checks"]["cleanup"]["status"] == "NOT RUN"
    assert "forged" not in str(report)


@pytest.mark.parametrize(
    "observation",
    [
        {"observed_at": "2026-10-05T09:59:59+00:00"},
        {"request_id": identifier(43)},
        {"plan_revision": "f" * 64},
    ],
)
def test_stale_or_foreign_phase_never_counts_as_creation(selection, phase, observation):
    report = assemble_report(DemoInput.parse(selection), [{**phase, **observation}])
    assert report["overall"] == "FAIL"
    assert report["checks"]["creation"]["status"] == "FAIL"
    assert report["workspace_ref"] is None


@pytest.mark.parametrize(
    "swapped",
    [
        {"account": "000000000001"},
        {"role": "OtherObserver"},
        {"region": "us-west-2"},
        {"connection_id": identifier(44)},
        {"workspace_id": identifier(45)},
    ],
)
def test_provider_swapped_target_or_credential_refuses_cleanup(
    selection, phase, swapped
):
    report = assemble_report(
        DemoInput.parse(selection),
        [phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(
            read_inventory=lambda query: inventory(query, **swapped)
        ),
    )
    assert report["overall"] == "FAIL"
    assert report["checks"]["cleanup"]["status"] == "FAIL"
    assert report["checks"]["survivors"]["status"] == "FAIL"
    assert report["live_acceptance"] is False
    assert all(str(value) not in str(report) for value in swapped.values())


def test_stale_provider_observation_refuses_cleanup(selection, phase):
    report = assemble_report(
        DemoInput.parse(selection),
        [phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(
            read_inventory=lambda query: inventory(
                query,
                observed_at="2026-10-05T09:59:59+00:00",
            )
        ),
    )
    assert report["overall"] == "FAIL"
    assert report["checks"]["cleanup"]["status"] == "FAIL"


@pytest.mark.parametrize(
    "approval_change",
    [
        {"expires_at": "2026-10-05T10:54:59+00:00"},
        {"decided_at": "2026-10-05T10:56:00+00:00"},
        {"result": "pending"},
        {"revoked": True},
        {"decided_by": identifier(51)},
    ],
)
def test_expired_or_unapproved_admission_never_counts_as_creation(
    selection,
    phase,
    approval_change,
):
    record = {**phase, "approval": {**phase["approval"], **approval_change}}
    report = assemble_report(DemoInput.parse(selection), [record])
    assert report["overall"] == "FAIL"
    assert report["checks"]["creation"]["status"] == "FAIL"
    assert report["live_acceptance"] is False


def test_approval_may_expire_after_admission_without_erasing_original_decision(
    selection,
    phase,
):
    record = {**phase, "observed_at": "2026-10-05T11:30:00+00:00"}
    report = assemble_report(DemoInput.parse(selection), [record])
    assert report["checks"]["creation"]["status"] == "BLOCKED"
    assert report["overall"] == "BLOCKED"


def test_duplicate_create_operation_with_same_request_is_refused(selection, phase):
    duplicate = {
        **phase,
        "operation_id": identifier(52),
        "approval_id": identifier(53),
        "approval": {**phase["approval"], "approval_id": identifier(53)},
        "attempt_id": "attempt-2",
        "fence": "fence-2",
        "observed_at": "2026-10-05T11:01:00+00:00",
    }
    report = assemble_report(DemoInput.parse(selection), [phase, duplicate])
    assert report["overall"] == "FAIL"
    assert "duplicate operation" in report["checks"]["creation"]["detail"]


@pytest.mark.parametrize(
    "changed",
    [
        {"attempt_id": "attempt-2"},
        {"fence": "fence-2"},
        {"artifact_digest": "e" * 64, "attempt_id": "attempt-2", "fence": "fence-2"},
        {"approval_id": identifier(54), "attempt_id": "attempt-2", "fence": "fence-2"},
    ],
)
def test_replayed_fence_or_changed_authority_on_retry_refused(
    selection, phase, changed
):
    retry = {**phase, **changed, "observed_at": "2026-10-05T11:01:00+00:00"}
    if "approval_id" in changed:
        retry["approval"] = {**phase["approval"], "approval_id": changed["approval_id"]}
    report = assemble_report(DemoInput.parse(selection), [phase, retry])
    assert report["overall"] == "FAIL"
    assert report["checks"]["recovery"]["status"] == "NOT RUN"


def test_lost_response_requires_original_operation_and_stays_blocked(selection, phase):
    uncertain = {**phase, "state": "unknown"}
    retry = {
        **uncertain,
        "attempt_id": "attempt-2",
        "fence": "fence-2",
        "observed_at": "2026-10-05T11:01:00+00:00",
    }
    selected = DemoInput.parse(selection)
    first = assemble_report(selected, [uncertain])
    resumed = assemble_report(selected, [uncertain, retry])
    assert first["request_ref"] == resumed["request_ref"]
    assert first["workspace_ref"] == resumed["workspace_ref"]
    assert first["phases"][0]["operation_ref"] == resumed["phases"][1]["operation_ref"]
    assert first["checks"]["creation"]["status"] == "BLOCKED"
    assert resumed["checks"]["recovery"]["status"] == "BLOCKED"
    assert resumed["overall"] == "BLOCKED"


@pytest.mark.parametrize(
    "provider_change,failed_check",
    [
        ({"owned_present": ["example-owned-cluster"]}, "cleanup"),
        ({"survivors_present": []}, "survivors"),
        ({"cost_usd": "10.01"}, "cost"),
    ],
)
def test_partial_cleanup_or_budget_excess_refutes_provider_claim(
    selection,
    phase,
    provider_change,
    failed_check,
):
    report = assemble_report(
        DemoInput.parse(selection),
        [phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(
            read_inventory=lambda query: inventory(query, **provider_change)
        ),
    )
    assert report["overall"] == "FAIL"
    assert report["checks"][failed_check]["status"] == "FAIL"
    assert report["live_acceptance"] is False


@pytest.mark.parametrize("status", ["denied", "incomplete"])
def test_denied_or_incomplete_inventory_cannot_establish_absence(
    selection, phase, status
):
    report = assemble_report(
        DemoInput.parse(selection),
        [phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(
            read_inventory=lambda query: inventory(
                query,
                status=status,
                owned_present=[],
                survivors_present=list(query.expected_survivors),
                cost_usd="0",
            )
        ),
    )
    assert report["overall"] == "BLOCKED"
    assert {
        report["checks"][key]["status"]
        for key in (
            "cleanup",
            "survivors",
            "cost",
        )
    } == {"BLOCKED"}


@pytest.mark.parametrize("cost", [None, "0"])
def test_unknown_or_synthetic_zero_cost_cannot_approve_cleanup(selection, phase, cost):
    report = assemble_report(
        DemoInput.parse(selection),
        [phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(
            read_inventory=lambda query: inventory(query, cost_usd=cost)
        ),
    )
    assert report["checks"]["cost"]["status"] == "BLOCKED"
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
    assert report["overall"] == "BLOCKED"
    if cost is None:
        assert "unknown" in report["checks"]["cost"]["detail"]


def test_unavailable_provider_cannot_turn_missing_inventory_into_absence(
    selection, phase
):
    def unavailable(query):
        raise PermissionError("private provider refusal detail")

    report = assemble_report(
        DemoInput.parse(selection),
        [phase],
        expected_owned=("example-owned-cluster",),
        reader=SimpleNamespace(read_inventory=unavailable),
    )
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
    assert "private provider refusal detail" not in str(report)


def test_batch_only_or_self_declared_serving_never_establishes_endpoint(
    selection, phase
):
    batch_only = assemble_report(DemoInput.parse(selection), [phase])
    assert batch_only["checks"]["serving"]["status"] == "NOT RUN"
    assert batch_only["overall"] == "BLOCKED"
    fabricated = assemble_report(
        DemoInput.parse(selection),
        [
            {**phase, "serving_endpoint_authenticated": True},
        ],
    )
    assert fabricated["overall"] == "FAIL"
    assert fabricated["checks"]["serving"]["status"] == "NOT RUN"


def test_no_provider_inventory_or_owned_baseline_never_claims_cleanup(selection, phase):
    selected = DemoInput.parse(selection)
    missing = assemble_report(selected, [phase])
    assert missing["checks"]["cleanup"]["status"] == "NOT RUN"
    invalid = assemble_report(
        selected,
        [phase],
        reader=SimpleNamespace(read_inventory=lambda query: inventory(query)),
    )
    assert invalid["overall"] == "FAIL"
    assert invalid["checks"]["cleanup"]["status"] == "FAIL"


def test_one_approval_cannot_authorize_distinct_phase_operations(selection, phase):
    bootstrap = {
        **phase,
        "phase_name": "bootstrap",
        "operation_id": identifier(9),
        "artifact_digest": "e" * 64,
        "attempt_id": "attempt-2",
        "fence": "fence-2",
        "observed_at": "2026-10-05T11:01:00+00:00",
    }
    with pytest.raises(EvidenceError, match="approval reused"):
        link_phases(DemoInput.parse(selection), [phase, bootstrap])

    def must_not_observe(query):
        pytest.fail("provider observation must follow valid phase authorization")

    report = assemble_report(
        DemoInput.parse(selection),
        [phase, bootstrap],
        reader=SimpleNamespace(read_inventory=must_not_observe),
    )
    assert report["checks"]["creation"]["status"] == "FAIL"
