"""Local scenario-oracle fixtures; deployed connector discovery is not yet available."""

from copy import deepcopy

import pytest


def assert_two_provider_reuse(readback, connector_pages, gateway_receipts, bindings):
    providers = {"github": "repository", "jira": "tracker"}
    assert all(set(collection) == set(providers) for collection in (readback, connector_pages, gateway_receipts, bindings))
    for provider, kind in providers.items():
        source = readback[provider]
        namespace = bindings[provider]["namespace"]
        assert source["namespace"] == namespace and source["items"]
        native_ids = [str(item["id"]) for item in source["items"]]
        assert all(native_ids) and len(native_ids) == len(set(native_ids))
        if provider == "github":
            assert all(item["owner"]["login"] == namespace for item in source["items"])
        expected = {(kind, namespace, native_id) for native_id in native_ids}

        connector_items = []
        for index, page in enumerate(connector_pages[provider]):
            last = index == len(connector_pages[provider]) - 1
            assert page["contract_version"] == 1 and len(page["items"]) <= 100
            assert page["coverage"] == ("complete" if last else "partial")
            assert bool(page.get("next_cursor")) != last
            connector_items.extend(page["items"])
        assert all(
            item["provenance"] == "observation_only" and item["source_revision"].startswith("observation-sha256:")
            for item in connector_items
        )

        gateway_items = []
        for index, receipt in enumerate(gateway_receipts[provider]):
            last = index == len(gateway_receipts[provider]) - 1
            assert receipt["state"] == "succeeded" and receipt["coverage"] == ("complete" if last else "partial")
            assert bool(receipt.get("cursor")) != last
            gateway_items.extend(receipt.get("resources", []))
        assert all(item["binding_id"] == bindings[provider]["id"] for item in gateway_items)

        for items in (connector_items, gateway_items):
            identities = [(item["kind"], item["namespace"], item["native_id"]) for item in items]
            assert len(identities) == len(expected) and set(identities) == expected


@pytest.fixture
def two_provider_readback():
    readback = {
        "github": {
            "namespace": "team",
            "items": [{"id": 35, "owner": {"login": "team"}}, {"id": 36, "owner": {"login": "team"}}],
        },
        "jira": {"namespace": "registered-instance", "items": [{"id": 12, "name": "Planning"}]},
    }
    bindings = {
        "github": {"id": "github-binding", "namespace": "team"},
        "jira": {"id": "jira-binding", "namespace": "registered-instance"},
    }

    def connector_item(kind, namespace, native_id):
        return {
            "kind": kind,
            "namespace": namespace,
            "native_id": native_id,
            "provenance": "observation_only",
            "source_revision": "observation-sha256:" + "a" * 64,
        }

    def gateway_item(kind, namespace, native_id, binding_id):
        return {"kind": kind, "namespace": namespace, "native_id": native_id, "binding_id": binding_id}

    connector_pages = {
        "github": [
            {"contract_version": 1, "coverage": "partial", "next_cursor": "2", "items": [connector_item("repository", "team", "35")]},
            {"contract_version": 1, "coverage": "complete", "items": [connector_item("repository", "team", "36")]},
        ],
        "jira": [{"contract_version": 1, "coverage": "complete", "items": [connector_item("tracker", "registered-instance", "12")]}],
    }
    gateway_receipts = {
        "github": [
            {"state": "succeeded", "coverage": "partial", "cursor": "first-operation", "resources": [gateway_item("repository", "team", "35", "github-binding")]},
            {"state": "succeeded", "coverage": "complete", "resources": [gateway_item("repository", "team", "36", "github-binding")]},
        ],
        "jira": [
            {"state": "succeeded", "coverage": "complete", "resources": [gateway_item("tracker", "registered-instance", "12", "jira-binding")]}
        ],
    }
    return readback, connector_pages, gateway_receipts, bindings


def test_local_two_provider_readback_oracle(two_provider_readback):
    original = deepcopy(two_provider_readback)
    assert_two_provider_reuse(*two_provider_readback)
    assert two_provider_readback == original


@pytest.mark.parametrize("change", ["missing_provider", "foreign_namespace", "missing_record", "duplicate_record", "false_complete", "wrong_binding", "unavailable"])
def test_local_two_provider_oracle_rejects_false_green(two_provider_readback, change):
    readback, connector_pages, gateway_receipts, bindings = deepcopy(two_provider_readback)
    if change == "missing_provider":
        del readback["jira"]
    elif change == "foreign_namespace":
        connector_pages["github"][0]["items"][0]["namespace"] = "foreign-team"
    elif change == "missing_record":
        gateway_receipts["github"][1]["resources"] = []
    elif change == "duplicate_record":
        gateway_receipts["github"][1]["resources"][0]["native_id"] = "35"
    elif change == "false_complete":
        connector_pages["github"][0]["coverage"] = "complete"
    elif change == "wrong_binding":
        gateway_receipts["jira"][0]["resources"][0]["binding_id"] = "github-binding"
    else:
        gateway_receipts["jira"][0]["coverage"] = "unavailable"
    with pytest.raises(AssertionError):
        assert_two_provider_reuse(readback, connector_pages, gateway_receipts, bindings)


def assert_credential_lifecycle(grants, events):
    assert len(grants) == 2
    assert len({grant["tenant_id"] for grant in grants.values()}) == 1
    for field in ("project_id", "handle", "namespace"):
        assert len({grant[field] for grant in grants.values()}) == 2
    assert len(events) >= 2 and [event["phase"] for event in events[:2]] == ["start", "start"]
    assert {event["operation_id"] for event in events[:2]} == set(grants)
    assert all(events[index]["project_id"] == grants[events[index]["operation_id"]]["project_id"] for index in (0, 1))

    generations = {operation_id: grant["generation"] for operation_id, grant in grants.items()}
    revoked = set()
    denials = set()
    admitted = set()
    lifecycle = set()
    for event in events[2:]:
        operation_id = event["operation_id"]
        if event["phase"] == "rotate":
            assert operation_id in grants and operation_id not in revoked
            assert event["generation"] == generations[operation_id] + 1
            generations[operation_id] = event["generation"]
            lifecycle.add("rotate")
            continue
        if event["phase"] == "revoke":
            assert operation_id in grants
            revoked.add(operation_id)
            lifecycle.add("revoke")
            continue
        assert event["phase"] in ("dispatch", "page")
        grant = grants.get(operation_id)
        if grant is None:
            reason = "operation"
        elif event["project_id"] != grant["project_id"]:
            reason = "project"
        elif event["workload"] != grant["workload"]:
            reason = "workload"
        elif event["handle"] != grant["handle"]:
            reason = "handle"
        elif event["namespace"] != grant["namespace"]:
            reason = "namespace"
        elif operation_id in revoked:
            reason = "revoked"
        elif event["generation"] != generations[operation_id]:
            reason = "generation"
        else:
            reason = None
        if reason:
            denials.add(reason)
            assert event["coverage"] == "denied" and not event["records"]
            assert event["provider_calls"] == (1 if event["phase"] == "page" else 0)
        else:
            admitted.add(operation_id)
            assert event["coverage"] == "complete" and event["provider_calls"] == 1 and event["records"]
            assert all(
                record["project_id"] == grant["project_id"]
                and record["namespace"] == grant["namespace"]
                and record["native_id"]
                and not set(record) & {"secret", "handle", "credential_ref", "token", "authorization"}
                for record in event["records"]
            )
    assert lifecycle == {"rotate", "revoke"} and admitted == set(grants)
    assert denials == {"operation", "project", "workload", "handle", "namespace", "generation", "revoked"}


@pytest.fixture
def credential_lifecycle_trace():
    grants = {
        "operation-a": {"tenant_id": "tenant-fixture", "project_id": "project-a", "handle": "handle-a", "namespace": "team-a", "workload": "connector-api", "generation": 1},
        "operation-b": {"tenant_id": "tenant-fixture", "project_id": "project-b", "handle": "handle-b", "namespace": "team-b", "workload": "connector-api", "generation": 1},
    }

    def action(operation_id, phase, *, project_id=None, **changes):
        grant = grants[operation_id]
        return {
            "phase": phase,
            "operation_id": operation_id,
            "project_id": project_id or grant["project_id"],
            "handle": grant["handle"],
            "namespace": grant["namespace"],
            "workload": grant["workload"],
            "generation": grant["generation"],
            "coverage": "complete",
            "provider_calls": 1,
            "records": [{"project_id": grant["project_id"], "namespace": grant["namespace"], "native_id": operation_id}],
            **changes,
        }

    def denied(operation_id, phase, **changes):
        return {**action(operation_id, phase, **changes), "coverage": "denied", "provider_calls": 1 if phase == "page" else 0, "records": []}

    events = [
        {"phase": "start", "operation_id": "operation-a", "project_id": "project-a"},
        {"phase": "start", "operation_id": "operation-b", "project_id": "project-b"},
        action("operation-a", "dispatch"),
        action("operation-b", "dispatch"),
        denied("operation-a", "dispatch", project_id="project-b"),
        denied("operation-a", "dispatch", workload="foreign-workload"),
        denied("operation-a", "dispatch", handle="handle-b"),
        denied("operation-a", "dispatch", namespace="team-b"),
        {**denied("operation-a", "dispatch"), "operation_id": "unissued-operation"},
        {"phase": "rotate", "operation_id": "operation-a", "generation": 2},
        denied("operation-a", "page"),
        action("operation-a", "page", generation=2),
        {"phase": "revoke", "operation_id": "operation-b"},
        denied("operation-b", "page"),
    ]
    return grants, events


def test_local_credential_lifecycle_controls(credential_lifecycle_trace):
    original = deepcopy(credential_lifecycle_trace)
    assert_credential_lifecycle(*credential_lifecycle_trace)
    assert credential_lifecycle_trace == original


@pytest.mark.parametrize("index,change", [
    (0, {"operation_id": "operation-b"}),
    (3, {"records": [{"project_id": "project-a", "namespace": "team-b", "native_id": "foreign"}]}),
    (3, {"records": [{"project_id": "project-b", "namespace": "team-b", "native_id": "b", "credential_ref": "fixture-ref"}]}),
    (4, {"coverage": "complete"}),
    (5, {"provider_calls": 1}),
    (6, {"records": [{"native_id": "leaked"}]}),
    (9, {"generation": 1}),
    (10, {"coverage": "complete"}),
    (12, {"operation_id": "operation-a"}),
    (13, {"coverage": "complete"}),
])
def test_local_credential_lifecycle_controls_reject_false_green(credential_lifecycle_trace, index, change):
    grants, events = deepcopy(credential_lifecycle_trace)
    events[index].update(change)
    with pytest.raises(AssertionError):
        assert_credential_lifecycle(grants, events)


def assert_bounded_discovery(binding, provider_ids, attempts):
    assert binding["id"] and binding["namespace"] and binding["generation"] >= 1
    assert provider_ids and len(provider_ids) == len(set(provider_ids))
    assert len(attempts) == 3 and len({attempt["process_id"] for attempt in attempts}) >= 2
    assert len({attempt["operation_id"] for attempt in attempts}) == len(attempts)
    expected_cursor = 1
    admitted = []
    parent = None
    for attempt in attempts:
        assert attempt["parent_cursor"] == parent and attempt["request_cursor"] == expected_cursor
        assert 0 < attempt["elapsed_seconds"] <= 30 and 1 <= len(attempt["calls"]) <= 5
        resources = []
        unavailable = False
        for call in attempt["calls"]:
            assert call["cursor"] == expected_cursor and 1 <= call["limit"] <= 100
            assert len(call["items"]) <= call["limit"]
            if call["coverage"] == "unavailable":
                assert not call["items"] and not call.get("next_cursor")
                unavailable = True
                break
            assert call["coverage"] in ("partial", "complete")
            next_cursor = call.get("next_cursor")
            assert (call["coverage"] == "partial") == bool(next_cursor)
            if next_cursor:
                assert set(next_cursor) == {"contract_version", "cursor", "binding_id", "generation", "namespace"}
                assert next_cursor["contract_version"] == 1 and isinstance(next_cursor["cursor"], str) and next_cursor["cursor"]
                assert next_cursor != expected_cursor
                assert (next_cursor["binding_id"], next_cursor["generation"], next_cursor["namespace"]) == (
                    binding["id"], binding["generation"], binding["namespace"]
                )
            expected_cursor = next_cursor or 1
            resources.extend(call["items"])
        receipt = attempt["receipt"]
        assert receipt == attempt["readback"]
        assert receipt["coverage"] == ("unavailable" if unavailable else "partial" if expected_cursor != 1 else "complete")
        assert receipt["page"] == expected_cursor and bool(receipt.get("cursor")) == (expected_cursor != 1)
        assert receipt["resources"] == resources
        assert all(item["binding_id"] == binding["id"] and item["namespace"] == binding["namespace"] for item in resources)
        admitted.extend(item["native_id"] for item in resources)
        if expected_cursor != 1:
            assert receipt["cursor"] == attempt["operation_id"]
            parent = attempt["operation_id"]
    assert expected_cursor == 1 and admitted == provider_ids


@pytest.fixture
def bounded_discovery_trace():
    binding = {"id": "fixture-binding", "namespace": "team", "generation": 1}
    continuation = {"contract_version": 1, "cursor": "page-two", "binding_id": binding["id"], "generation": 1, "namespace": "team"}

    def item(native_id):
        return {"binding_id": binding["id"], "namespace": binding["namespace"], "native_id": native_id}

    def attempt(operation_id, process_id, parent, cursor, coverage, items, next_cursor):
        receipt = {"coverage": coverage, "resources": items, "page": next_cursor or cursor}
        if coverage == "complete":
            receipt["page"] = 1
        else:
            receipt["cursor"] = operation_id
        return {
            "operation_id": operation_id,
            "process_id": process_id,
            "parent_cursor": parent,
            "request_cursor": cursor,
            "elapsed_seconds": 2,
            "calls": [{
                "cursor": cursor,
                "limit": 100,
                "coverage": coverage,
                "items": items,
                "next_cursor": next_cursor if coverage == "partial" else None,
            }],
            "receipt": receipt,
            "readback": deepcopy(receipt),
        }

    first = attempt("operation-one", "worker-one", None, 1, "partial", [item("35")], continuation)
    throttled = attempt("operation-two", "worker-two", first["operation_id"], continuation, "unavailable", [], None)
    resumed = attempt("operation-three", "worker-two", throttled["operation_id"], continuation, "complete", [item("36")], None)
    return binding, ["35", "36"], [first, throttled, resumed]


def test_local_bounded_restart_oracle(bounded_discovery_trace):
    original = deepcopy(bounded_discovery_trace)
    assert_bounded_discovery(*bounded_discovery_trace)
    assert bounded_discovery_trace == original


@pytest.mark.parametrize("change", [
    "skipped_page", "foreign_namespace", "stale_generation", "missing_readback", "admitted_throttle",
    "false_complete", "oversize_page", "too_many_calls", "slow_attempt", "duplicate_record",
    "missing_record", "replayed_operation", "repeated_cursor", "unavailable_healthy",
])
def test_local_bounded_restart_oracle_rejects_false_green(bounded_discovery_trace, change):
    binding, provider_ids, attempts = deepcopy(bounded_discovery_trace)
    first, throttled, resumed = attempts
    if change == "skipped_page":
        resumed["request_cursor"] = 1
    elif change == "foreign_namespace":
        first["calls"][0]["next_cursor"]["namespace"] = "other-team"
    elif change == "stale_generation":
        binding["generation"] = 2
    elif change == "missing_readback":
        throttled["readback"]["page"] = 1
    elif change == "admitted_throttle":
        throttled["receipt"]["resources"] = [first["receipt"]["resources"][0]]
    elif change == "false_complete":
        first["receipt"]["coverage"] = "complete"
    elif change == "oversize_page":
        first["calls"][0]["limit"] = 101
    elif change == "too_many_calls":
        first["calls"] = first["calls"] * 6
    elif change == "slow_attempt":
        first["elapsed_seconds"] = 31
    elif change == "duplicate_record":
        resumed["calls"][0]["items"][0]["native_id"] = "35"
    elif change == "missing_record":
        provider_ids.append("37")
    elif change == "replayed_operation":
        resumed["operation_id"] = throttled["operation_id"]
    elif change == "repeated_cursor":
        first["calls"].append({**first["calls"][0], "cursor": first["calls"][0]["next_cursor"]})
    else:
        throttled["receipt"]["coverage"] = "complete"
    with pytest.raises(AssertionError):
        assert_bounded_discovery(binding, provider_ids, attempts)


def assert_deployment_gate(evidence):
    required = {(surface, flag) for surface in ("gateway", "go", "broker") for flag in (None, "false", "invalid")}
    disabled = evidence["disabled"]
    assert {(item["surface"], item["flag"]) for item in disabled} == required and len(disabled) == len(required)
    for item in disabled:
        assert item["code"] == "feature_disabled"
        assert (item["provider_requests"], item["new_jobs"], item["imported_records"]) == (0, 0, 0)

    admission = evidence["admission"]
    assert admission["surface"] == "gateway" and admission["flag"] == "true"
    assert admission["module_enabled"] is False and admission["code"] == "feature_disabled"
    unavailable = evidence["unauthorized"]
    assert unavailable["surface"] == "gateway" and unavailable["flag"] == "true"
    assert unavailable["module_enabled"] is True and unavailable["authority"] is False
    assert unavailable["code"] == "connector_unavailable"
    foreign = evidence["foreign_actor"]
    assert foreign["surface"] == "gateway" and foreign["code"] == "not_found"
    for item in (admission, unavailable, foreign):
        assert (item["provider_requests"], item["new_jobs"], item["imported_records"]) == (0, 0, 0)

    pending = evidence["pending"]
    assert {item["disabled_by"] for item in pending} == {"master", "admission"} and len(pending) == 2
    for item in pending:
        assert item["provider_requests_before"] == item["provider_requests_after"] == 1
        assert item["operation_state"] == "cancelled" and item["coverage"] == "denied"
        assert not item["resources"] and not item.get("cursor") and item["imported_records"] == 0

    native = evidence["native"]
    assert native["adapter"] == "native" and native["flag"] is None
    assert native["provider_requests"] == 1 and native["coverage"] == "complete" and native["resources"] == ["native-repository"]


@pytest.fixture
def deployment_gate_trace():
    def refusal(surface, flag, code="feature_disabled"):
        return {"surface": surface, "flag": flag, "code": code, "provider_requests": 0, "new_jobs": 0, "imported_records": 0}

    disabled = [
        refusal(surface, flag)
        for surface in ("gateway", "go", "broker")
        for flag in (None, "false", "invalid")
    ]
    pending = [
        {"disabled_by": gate, "provider_requests_before": 1, "provider_requests_after": 1,
         "operation_state": "cancelled", "coverage": "denied", "resources": [], "cursor": None, "imported_records": 0}
        for gate in ("master", "admission")
    ]
    return {
        "disabled": disabled,
        "admission": {**refusal("gateway", "true"), "module_enabled": False},
        "unauthorized": {**refusal("gateway", "true", "connector_unavailable"), "module_enabled": True, "authority": False},
        "foreign_actor": refusal("gateway", "false", "not_found"),
        "pending": pending,
        "native": {"adapter": "native", "flag": None, "provider_requests": 1, "coverage": "complete", "resources": ["native-repository"]},
    }


def test_local_deployment_gate_controls(deployment_gate_trace):
    original = deepcopy(deployment_gate_trace)
    assert_deployment_gate(deployment_gate_trace)
    assert deployment_gate_trace == original


@pytest.mark.parametrize("change", [
    "missing_surface", "missing_flag", "false_healthy", "provider_request", "new_job", "imported_record",
    "flag_grants_access", "foreign_actor_leaks", "pending_page_admitted", "pending_cursor", "late_provider_request",
    "pending_import", "native_disabled",
])
def test_local_deployment_gate_rejects_false_green(deployment_gate_trace, change):
    evidence = deepcopy(deployment_gate_trace)
    if change == "missing_surface":
        evidence["disabled"] = [item for item in evidence["disabled"] if item["surface"] != "broker"]
    elif change == "missing_flag":
        evidence["disabled"].pop()
    elif change == "false_healthy":
        evidence["disabled"][0]["code"] = "healthy"
    elif change == "provider_request":
        evidence["disabled"][1]["provider_requests"] = 1
    elif change == "new_job":
        evidence["admission"]["new_jobs"] = 1
    elif change == "imported_record":
        evidence["unauthorized"]["imported_records"] = 1
    elif change == "flag_grants_access":
        evidence["unauthorized"]["code"] = "complete"
    elif change == "foreign_actor_leaks":
        evidence["foreign_actor"]["code"] = "feature_disabled"
    elif change == "pending_page_admitted":
        evidence["pending"][0]["resources"] = ["late-repository"]
    elif change == "pending_cursor":
        evidence["pending"][1]["cursor"] = "late-page"
    elif change == "late_provider_request":
        evidence["pending"][0]["provider_requests_after"] = 2
    elif change == "pending_import":
        evidence["pending"][1]["imported_records"] = 1
    else:
        evidence["native"]["coverage"] = "feature_disabled"
    with pytest.raises(AssertionError):
        assert_deployment_gate(evidence)
