"""Offline registry lineage checks; live acceptance needs deployed artifacts."""

import copy
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import Mock


TESTS = pathlib.Path(__file__).resolve().parent


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(name, TESTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile_fixture = load_fixture("test_i21_registry")
catalog_fixture = load_fixture("test_i22_registry")


def registry_lineage_fixture():
    profile = profile_fixture.ProfileFixture()
    second_change = copy.deepcopy(profile.change)
    second_change.update(event_id="example-change-2", revision=2,
                         recorded_at="2026-10-21T00:00:00Z")
    second_change["source_revisions"] = [
        profile_fixture.reference("product", profile_fixture.PRODUCT, "example-change-2")
    ]
    second_change["relationship_versions"][0].update(
        relationship_revision="2", recorded_at="2026-10-21T00:00:00Z",
        valid_from="2026-10-21T00:00:00Z",
    )
    profile.list_changes = Mock(return_value={"items": [second_change, profile.change],
                                                  "next_cursor": None})
    profile.get_revision = Mock(side_effect=lambda product, project, revision: {
        **profile.revision, "revision": revision,
        "product": {"name": "Renamed Product" if revision == 2 else "Example"},
    })
    product = catalog_fixture.reference("product", profile_fixture.PRODUCT,
                                         source="adp.profile")
    work = catalog_fixture.native("work", "example-work")
    old_portfolio = catalog_fixture.reference("portfolio", "example-old")
    new_portfolio = catalog_fixture.reference("portfolio", "example-new")
    links = [
        catalog_fixture.link("work_product", "example-work-product",
                             work["subject_ref"], product),
        catalog_fixture.link("portfolio_product", "example-old-membership",
                             old_portfolio, product, end="2026-10-20T00:00:00Z"),
        catalog_fixture.link("portfolio_product", "example-new-membership",
                             new_portfolio, product, recorded="2026-10-21T00:00:00Z",
                             start="2026-10-20T00:00:00Z"),
    ]
    catalog = catalog_fixture.CatalogFixture(links)
    sink = profile_fixture.MemorySink()
    grants = Mock(return_value=True)
    registry = profile_fixture.adapter.RegistryAdapter(
        profile, catalog, grants, sink, "profile-registry-1",
        clock=lambda: "2026-10-22T00:00:00Z",
    )
    profile_facts, cursor = registry.ingest_profile(
        profile_fixture.TENANT, profile_fixture.PROJECT,
        profile_fixture.PRODUCT, "example-publisher",
    )
    for record in catalog.snapshots.values():
        registry.ingest_catalog(record["subject_ref"], "example-publisher")
    return profile, catalog, sink, grants, work, profile_facts, cursor


def offline_registry_lineage():
    profile, catalog, sink, grants, work, profile_facts, cursor = registry_lineage_fixture()
    assert cursor is None
    assert profile.get_revision.call_count == 2
    assert [fact["source_event_id"] for fact in profile_facts] == [
        "example-change-1", "example-change-1", "example-change-1",
        "example-change-2", "example-change-2", "example-change-2",
    ]
    assert profile_facts[0]["source_ref"] == profile_fixture.reference(
        "change", "example-change-1", "1"
    )
    assert profile_facts[3]["source_revisions"] == [profile_fixture.reference(
        "product", profile_fixture.PRODUCT, "example-change-2"
    )]
    assert profile_facts[3]["source_snapshot"] == {"product": {"name": "Renamed Product"}}
    stored = copy.deepcopy(tuple(sink.rows.values()))
    assert len(stored) == 9
    assert not sink.quarantined
    assert {record["source_event_id"] for record in stored if record["record_type"] ==
            "context_relationship"} == {
        "example-work-product-r1-event", "example-old-membership-r1-event",
        "example-new-membership-r1-event",
    }
    for record in catalog.snapshots.values():
        matched = [row for row in stored if row["subject_ref"] == record["subject_ref"]]
        assert len(matched) == 1
        assert matched[0]["source_ref"] == record["source_ref"]
        assert matched[0]["relationship_version"] == record["relationship_version"]
    admitted = [record["relationship_version"] for record in stored
                if record["record_type"] == "context_relationship"]
    inputs = dict(tenant_id=profile_fixture.TENANT,
                  project_ids=[profile_fixture.PROJECT],
                  can_read_project=lambda tenant, project: (
                      tenant, project) == (profile_fixture.TENANT, profile_fixture.PROJECT))
    historical = catalog_fixture.projection.query_authorized_projection(
        [work], admitted, [], **inputs,
    )
    current = catalog_fixture.projection.query_authorized_projection(
        [work], admitted, [], regroup_at="2026-10-22T00:00:00Z", **inputs,
    )
    assert historical["contexts"][0]["portfolio_ids"] == ["example-old"]
    assert current["contexts"][0]["portfolio_ids"] == ["example-new"]
    assert historical["contexts"][0]["subject_ref"] == work["subject_ref"]
    assert current["contexts"][0]["subject_ref"] == work["subject_ref"]
    assert all(record["project_id"] == profile_fixture.PROJECT for record in stored)
    assert grants.call_count >= len(stored)


def offline_registry_lineage_scenario(source_revision, as_of):
    if source_revision != catalog_fixture.SOURCE_REVISION:
        raise ValueError("SCN-01 requires the pinned offline source revision")
    if as_of != "2026-10-22T00:00:00Z":
        raise ValueError("SCN-01 requires the pinned offline as-of clock")
    offline_registry_lineage()
    return {"scenario_id": "SCN-01", "run_id": "example-owned-v13-lineage",
            "fixture_kind": "in_memory", "source_revision": source_revision,
            "as_of": as_of, "status": "fixture_only", "live_acceptance": False}


def overlapping_fixture():
    profile, catalog, sink, grants, work, _, _ = registry_lineage_fixture()
    product = catalog_fixture.reference("product", profile_fixture.PRODUCT, source="adp.profile")
    cost = catalog_fixture.native("cost", "example-cost", amount="10.00", currency="USD")
    extra = catalog_fixture.CatalogFixture([
        catalog_fixture.link("work_product", "example-cost-product", cost["subject_ref"], product),
        catalog_fixture.link("portfolio_product", "example-shared-membership",
                             catalog_fixture.reference("portfolio", "example-shared"), product),
    ])
    catalog.snapshots.update(extra.snapshots)
    registry = profile_fixture.adapter.RegistryAdapter(
        profile, catalog, grants, sink, "profile-registry-1",
        clock=lambda: "2026-10-22T00:00:00Z")
    for record in extra.snapshots.values():
        registry.ingest_catalog(record["subject_ref"], "example-publisher")
    return registry, profile, catalog, sink, grants, work, cost


def offline_lineage_deduplication():
    registry, profile, catalog, sink, _, work, cost = overlapping_fixture()
    original_work = copy.deepcopy(work)
    original_cost = copy.deepcopy(cost)
    accepted = copy.deepcopy(sink.rows)
    replay, cursor = registry.ingest_profile(
        profile_fixture.TENANT, profile_fixture.PROJECT, profile_fixture.PRODUCT,
        "example-publisher")
    assert cursor is None
    assert len(replay) == 6
    for record in catalog.snapshots.values():
        registry.ingest_catalog(record["subject_ref"], "example-publisher")
    assert sink.rows == accepted
    assert not sink.quarantined
    admitted = [copy.deepcopy(record["relationship_version"]) for record in sink.rows.values()
                if record["record_type"] == "context_relationship"]
    inputs = dict(tenant_id=profile_fixture.TENANT,
                  project_ids=[profile_fixture.PROJECT],
                  can_read_project=lambda tenant, project: (
                      tenant, project) == (profile_fixture.TENANT, profile_fixture.PROJECT))
    historical = catalog_fixture.projection.query_authorized_projection(
        [work, cost], admitted, [], **inputs)
    current = catalog_fixture.projection.query_authorized_projection(
        [work, cost], admitted, [], regroup_at="2026-10-22T00:00:00Z", **inputs)
    for result, memberships, grouping in (
        (historical, ["example-old", "example-shared"], "historical"),
        (current, ["example-new", "example-shared"], "current_regrouping"),
    ):
        assert result["work_count"] == 1
        assert result["cost_by_currency"] == {"USD": Decimal("10.00")}
        assert result["unallocated_by_currency"] == {"USD": Decimal("10.00")}
        assert result["coverage"] == "complete"
        assert result["grouping"] == grouping
        assert len(result["contexts"]) == 2
        assert all(context["portfolio_ids"] == memberships for context in result["contexts"])
        assert {context["subject_ref"]["record_id"] for context in result["contexts"]} == {
            "example-work", "example-cost"}
    assert work == original_work and cost == original_cost
    assert profile.get_revision.call_count == 4


def offline_lineage_deduplication_scenario(source_revision, historical_as_of, regroup_at):
    if source_revision != catalog_fixture.SOURCE_REVISION:
        raise ValueError("SCN-02 requires the pinned offline source revision")
    if historical_as_of != catalog_fixture.EVENT_TIME:
        raise ValueError("SCN-02 requires the pinned historical clock")
    if regroup_at != "2026-10-22T00:00:00Z":
        raise ValueError("SCN-02 requires the pinned regrouping clock")
    offline_lineage_deduplication()
    return {"scenario_id": "SCN-02", "run_id": "example-owned-v13-deduplication",
            "fixture_kind": "in_memory", "source_revision": source_revision,
            "historical_as_of": historical_as_of, "regroup_at": regroup_at,
            "ledger_fact": {"amount": "10.00", "currency": "USD"},
            "status": "fixture_only", "live_acceptance": False}


def offline_lineage_invalid_source(testcase):
    registry, _, catalog, sink, grants, _, _ = overlapping_fixture()
    stored = copy.deepcopy(sink.rows)
    record = next(iter(catalog.snapshots.values()))
    bad_revision = copy.deepcopy(record)
    bad_revision["subject_ref"]["record_revision"] = "wrong-revision"
    catalog.readback = Mock(return_value=bad_revision)
    with testcase.assertRaisesRegex(profile_fixture.adapter.AdmissionError,
                                    "catalog returned a different revision"):
        registry.ingest_catalog(record["subject_ref"], "example-publisher")
    assert sink.rows == stored
    catalog.readback = Mock(side_effect=profile_fixture.adapter.CatalogRevisionUnavailable(
        "revision_unresolvable"))
    assert registry.ingest_catalog(record["subject_ref"], "example-publisher") is None
    assert sink.quarantined[-1]["reason"] == "revision_unresolvable"
    grants.return_value = False
    with testcase.assertRaisesRegex(profile_fixture.adapter.AdmissionError,
                                    "source grant unavailable"):
        registry.ingest_catalog(record["subject_ref"], "wrong-publisher")
    assert catalog.readback.call_count == 1
    assert sink.rows == stored


def offline_lineage_access():
    _, _, _, sink, _, work, cost = overlapping_fixture()
    revoked_project = "example-revoked-project"
    hidden = catalog_fixture.native("cost", "example-private-cost", amount="999.00", currency="USD")
    hidden["subject_ref"]["project_id"] = revoked_project
    private_product = catalog_fixture.reference("product", "example-private-product",
                                                 project=revoked_project, source="adp.profile")
    private_link = catalog_fixture.link("work_product", "example-private-link",
                                        hidden["subject_ref"], private_product)
    private_sink = profile_fixture.MemorySink()
    private_catalog = catalog_fixture.CatalogFixture([private_link])
    private_registry = profile_fixture.adapter.RegistryAdapter(
        None, private_catalog, Mock(return_value=True), private_sink, "profile-registry-1",
        clock=lambda: "2026-10-22T00:00:00Z")
    for record in private_catalog.snapshots.values():
        private_registry.ingest_catalog(record["subject_ref"], "example-publisher")
    links = [copy.deepcopy(row["relationship_version"]) for row in
             (*sink.rows.values(), *private_sink.rows.values())
             if row["record_type"] == "context_relationship"]
    permissions = {profile_fixture.PROJECT, revoked_project}
    can_read = Mock(side_effect=lambda tenant, project: (
        tenant == profile_fixture.TENANT and project in permissions))

    def query():
        return catalog_fixture.projection.query_authorized_projection(
            [work, cost, hidden], links, [], tenant_id=profile_fixture.TENANT,
            project_ids=[profile_fixture.PROJECT, revoked_project], can_read_project=can_read)

    assert query()["cost_by_currency"] == {"USD": Decimal("1009.00")}
    permissions.remove(revoked_project)
    result = query()
    assert result["coverage"] == "incomplete"
    assert result["work_count"] == 1
    assert result["cost_by_currency"] == {"USD": Decimal("10.00")}
    assert all(secret not in str(result) for secret in (
        revoked_project, "example-private", "999.00"))
    assert len(private_sink.rows) == 1 and not private_sink.quarantined
    assert can_read.call_count >= 4
    return sink, work, cost


def offline_lineage_denial_and_receipt(testcase):
    sink, work, cost = offline_lineage_access()
    service = load_fixture("test_i23_registry")
    snapshot = {
        "status": "complete", "dataset_version": "example-v13-dataset",
        "mapping_versions": ["approved-1"], "facts": [work, cost],
        "relationships": [copy.deepcopy(row["relationship_version"]) for row in sink.rows.values()
                          if row["record_type"] == "context_relationship"],
        "contributions": [], "allocations": [],
    }
    read_snapshot = Mock(return_value=snapshot)
    permissions = {profile_fixture.PROJECT}
    reader = service.results.ResultsReader(
        read_snapshot, lambda tenant, project, viewer: (
            tenant == profile_fixture.TENANT and project in permissions
            and viewer == "example-viewer"), "example-signing-secret",
        read_policy=lambda tenant, project: service.qualified_shared_policy(snapshot),
        can_read_policy=lambda tenant, project, viewer, policy: (
            tenant == profile_fixture.TENANT and project in permissions
            and viewer == "example-viewer"),
    )
    query = dict(tenant_id=profile_fixture.TENANT, project_id=profile_fixture.PROJECT,
                 viewer_id="example-viewer", product_id=profile_fixture.PRODUCT,
                 as_of="2026-10-22T00:00:00Z")
    assert reader.read(**query)["cost_by_currency"] == {"USD": "10.00"}
    assert read_snapshot.call_count == 1
    permissions.clear()
    with testcase.assertRaises(service.results.ReadError) as denied:
        reader.read(**query)
    assert (denied.exception.status, denied.exception.code) == (404, "not_found")
    app = service.results.ResultsApp(reader, lambda environ: (
        profile_fixture.TENANT, "example-viewer"))
    statuses = []
    response = app({"REQUEST_METHOD": "GET",
                    "PATH_INFO": "/api/engineering-insights/v1/projects/" + profile_fixture.PROJECT,
                    "QUERY_STRING": "view=business_context&product_id=example-product&as_of=2026-10-22T00%3A00%3A00Z"},
                   lambda status, headers: statuses.append(status))
    assert statuses == ["404 Not Found"]
    assert json.loads(b"".join(response)) == {
        "error": {"code": "not_found", "message": "Result unavailable"}}
    assert read_snapshot.call_count == 1
    release = load_fixture("test_release_contract")
    image = "registry.example/insights@sha256:" + "a" * 64
    env = release.ENV | {"ADP_INPUT_STAGE": "emitter", "ADP_INPUT_IMAGE": image}
    context, receipt = release.evidence_module.evidence(env, release.read)
    assert context["inputs"]["stage"] == "emitter"
    assert receipt["image_digest"] == "sha256:" + "a" * 64
    assert receipt["run_attempt"] == context["run_attempt"]
    with testcase.assertRaisesRegex(ValueError, "immutable"):
        release.evidence_module.evidence(env | {"ADP_INPUT_IMAGE": "registry.example/insights:latest"},
                                         release.read)


def offline_lineage_revoked_export_attempts(testcase):
    registry, _, catalog, sink, grants, _, _ = overlapping_fixture()
    stored = copy.deepcopy(sink.rows)
    source = next(iter(catalog.snapshots.values()))["subject_ref"]
    grants.return_value = False
    with testcase.assertRaisesRegex(profile_fixture.adapter.AdmissionError,
                                    "source grant unavailable"):
        registry.ingest_catalog(source, "example-publisher")
    assert sink.rows == stored
    grants.return_value = True
    catalog.readback = Mock(side_effect=profile_fixture.adapter.CatalogRevisionUnavailable(
        "snapshot_expired"))
    assert registry.ingest_catalog(source, "example-publisher") is None
    assert sink.quarantined == [{"tenant_id": profile_fixture.TENANT,
                                 "project_id": profile_fixture.PROJECT,
                                 "source_event_id": source["record_id"],
                                 "reason": "snapshot_expired"}]
    rejected = copy.deepcopy(sink.quarantined)
    grants.return_value = False
    with testcase.assertRaises(profile_fixture.adapter.AdmissionError):
        registry.ingest_catalog(source, "example-publisher")
    assert sink.quarantined == rejected and sink.rows == stored
    assert catalog.readback.call_count == 1

    service = load_fixture("test_i23_registry")
    read_snapshot = Mock(side_effect=AssertionError("revoked read reached snapshot"))
    reader = service.results.ResultsReader(
        read_snapshot, lambda tenant, project, viewer: False, "example-signing-secret")
    app = service.results.ResultsApp(reader, lambda environ: (
        profile_fixture.TENANT, "example-viewer"))
    for method, path in (
        ("GET", "/api/engineering-insights/v1/projects/" + profile_fixture.PROJECT),
        ("POST", "/api/engineering-insights/v1/projects/" + profile_fixture.PROJECT + "/export"),
    ):
        statuses = []
        response = app({"REQUEST_METHOD": method, "PATH_INFO": path,
                        "QUERY_STRING": "view=business_context&product_id=example-product&as_of=2026-10-22T00%3A00%3A00Z"},
                       lambda status, headers: statuses.append(status))
        assert statuses == ["404 Not Found"]
        assert json.loads(b"".join(response)) == {
            "error": {"code": "not_found", "message": "Result unavailable"}}
    read_snapshot.assert_not_called()


def offline_insights_cli_checkpoint():
    service = load_fixture("test_i23_registry")
    snapshot, work, _ = service.shared_snapshot()
    as_of = catalog_fixture.EVENT_TIME
    portfolios = ("example-commerce", "example-shared")
    state = {"snapshot": snapshot, "granted": True}
    with service.installed_gateway(snapshot, state) as (binary, environment, token, gateway_url):
        installed = binary / "adp-insights.py"
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == hashlib.sha256(
            (service.CLI / "adp-insights.py").read_bytes()).hexdigest()
        result = service.read_result(binary, environment, token, gateway_url, as_of, portfolios)
        assert result["coverage"] == "complete"
        assert result["grouping"] == "historical"
        assert result["work_count"] == 1
        assert result["cost_by_currency"] == {"USD": "10.00"}
        assert result["unallocated_by_currency"] == {"USD": "10.00"}
        assert result["dataset_version"] == snapshot["dataset_version"]
        assert result["mapping_versions"] == ["approved-1"]
        assert len(result["contexts"]) == 2
        assert {entry["subject_ref"]["record_id"] for entry in result["contexts"]} == {
            "example-work", "example-cost"}
        assert all(entry["portfolio_ids"] == list(portfolios) for entry in result["contexts"])
        work_context = next(entry for entry in result["contexts"]
                            if entry["subject_ref"] == work["subject_ref"])
        expected_product = catalog_fixture.reference("product", "example-product",
                                                     source="adp.profile")
        assert work_context["product_refs"]
        assert all(ref == expected_product for ref in work_context["product_refs"])
        assert work_context["contribution_refs"] == [
            snapshot["contributions"][0]["subject_ref"]]
        assert snapshot["contributions"][0]["source_event_id"] == "example-contribution-event"
        assert service.LocalGateway.requests and len(service.LocalGateway.requests) == 2
        assert all(auth == f"Bearer {token}" for _, auth in service.LocalGateway.requests)

    gateway_workflow = (TESTS.parents[2] / ".github/workflows/gateway-deploy.yml").read_text()
    dockerfile = (TESTS.parents[2] / "modules/gateway/Dockerfile").read_text()
    routes = (TESTS.parents[2] / "modules/gateway/src/insights_results/routes.py").read_text()
    assert "'modules/insights/results.py'" in gateway_workflow
    assert "'modules/insights/projection.py'" in gateway_workflow
    assert "from contracts.insights import results" in dockerfile
    assert "results.ResultsReader" in routes


def offline_insights_cli_revocation():
    service = load_fixture("test_i23_registry")
    snapshot, _, _ = service.shared_snapshot()
    state = {"snapshot": snapshot, "granted": True}
    with service.installed_gateway(snapshot, state) as (binary, environment, token, gateway_url):
        before = service.read_result(
            binary, environment, token, gateway_url, catalog_fixture.EVENT_TIME,
            ("example-commerce", "example-shared"))
        assert before["cost_by_currency"] == {"USD": "10.00"}
        assert before["work_count"] == 1
        state["granted"] = False
        for project in (catalog_fixture.PROJECT, "example-other-project"):
            exit_code, cli = service.error_command(binary, environment, token, project)
            status, response = service.error_readback(gateway_url, token, project)
            assert exit_code == 5
            assert cli["status"] == "failed"
            assert cli["error"]["code"] == "not_found"
            assert cli["detail"] == {}
            assert (status, response) == (
                404, {"error": {"code": "not_found", "message": "Result unavailable"}})
            assert all(secret not in json.dumps(cli) for secret in (
                "example-work", "example-cost", "example-commerce", "example-shared",
                "10.00", "work_count", "source_connection_id", token))
        assert len(service.LocalGateway.requests) == 6
        assert all(auth == f"Bearer {token}" for _, auth in service.LocalGateway.requests)


def offline_insights_cli_scenario(source_revision, as_of, portfolios):
    if source_revision != catalog_fixture.SOURCE_REVISION:
        raise ValueError("SCN-04 requires the pinned offline source revision")
    if as_of != catalog_fixture.EVENT_TIME:
        raise ValueError("SCN-04 requires the pinned offline as-of clock")
    if portfolios != ("example-commerce", "example-shared"):
        raise ValueError("SCN-04 requires the overlapping offline Portfolios")
    offline_insights_cli_checkpoint()
    offline_insights_cli_revocation()
    return {"scenario_id": "SCN-04", "run_id": "example-owned-v13-cli",
            "fixture_kind": "local_read_service", "source_revision": source_revision,
            "as_of": as_of, "portfolios": portfolios, "status": "fixture_only",
            "installed_cli_checksum_checked": True, "independent_local_readback": True,
            "revoked_read_denied": True, "deployed_receipts_verified": False,
            "live_cleanup_verified": False, "live_acceptance": False}


def offline_lineage_access_scenario(source_revision, as_of, projects, testcase):
    if source_revision != catalog_fixture.SOURCE_REVISION:
        raise ValueError("SCN-03 requires the pinned offline source revision")
    if as_of != "2026-10-22T00:00:00Z":
        raise ValueError("SCN-03 requires the pinned offline as-of clock")
    if projects != (profile_fixture.PROJECT, "example-revoked-project"):
        raise ValueError("SCN-03 requires the two scoped offline Projects")
    with tempfile.TemporaryDirectory(prefix="example-v13-scn-03-") as directory:
        owned_manifest = pathlib.Path(directory) / "owned-fixture.json"
        owned_manifest.write_text(json.dumps({
            "run_id": "example-owned-v13-access", "projects": projects,
            "cleanup_scope": "local_fixture_only"}))
        offline_lineage_denial_and_receipt(testcase)
        offline_lineage_revoked_export_attempts(testcase)
        assert owned_manifest.is_file()
    assert not owned_manifest.exists()
    return {"scenario_id": "SCN-03", "run_id": "example-owned-v13-access",
            "fixture_kind": "in_memory", "source_revision": source_revision,
            "as_of": as_of, "projects": projects, "status": "fixture_only",
            "local_cleanup": "complete", "live_cleanup_verified": False,
            "emitter_receipt_kind": "synthetic", "live_acceptance": False}


class V13OfflineScenarios(unittest.TestCase):
    def test_registry_lineage(self):
        offline_registry_lineage()

    def test_scn_01_offline_selector(self):
        report = offline_registry_lineage_scenario(
            catalog_fixture.SOURCE_REVISION, "2026-10-22T00:00:00Z")
        self.assertEqual(report, {
            "scenario_id": "SCN-01", "run_id": "example-owned-v13-lineage",
            "fixture_kind": "in_memory", "source_revision": catalog_fixture.SOURCE_REVISION,
            "as_of": "2026-10-22T00:00:00Z", "status": "fixture_only",
            "live_acceptance": False,
        })
        with self.assertRaisesRegex(ValueError, "source revision"):
            offline_registry_lineage_scenario("stale-revision", "2026-10-22T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "as-of clock"):
            offline_registry_lineage_scenario(catalog_fixture.SOURCE_REVISION,
                                              "2026-10-21T00:00:00Z")

    def test_lineage_deduplication(self):
        offline_lineage_deduplication()

    def test_scn_02_offline_selector(self):
        arguments = (catalog_fixture.SOURCE_REVISION, catalog_fixture.EVENT_TIME,
                     "2026-10-22T00:00:00Z")
        report = offline_lineage_deduplication_scenario(*arguments)
        self.assertEqual(report, {
            "scenario_id": "SCN-02", "run_id": "example-owned-v13-deduplication",
            "fixture_kind": "in_memory", "source_revision": catalog_fixture.SOURCE_REVISION,
            "historical_as_of": catalog_fixture.EVENT_TIME,
            "regroup_at": "2026-10-22T00:00:00Z",
            "ledger_fact": {"amount": "10.00", "currency": "USD"},
            "status": "fixture_only", "live_acceptance": False,
        })
        for index, value, error in (
            (0, "stale-revision", "source revision"),
            (1, "2026-10-21T00:00:00Z", "historical clock"),
            (2, "2026-10-23T00:00:00Z", "regrouping clock"),
        ):
            altered = list(arguments)
            altered[index] = value
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, error):
                offline_lineage_deduplication_scenario(*altered)

    def test_lineage_invalid_source(self):
        offline_lineage_invalid_source(self)

    def test_scn_03_offline_selector(self):
        arguments = (catalog_fixture.SOURCE_REVISION, "2026-10-22T00:00:00Z",
                     (profile_fixture.PROJECT, "example-revoked-project"))
        report = offline_lineage_access_scenario(*arguments, self)
        self.assertEqual(report, {
            "scenario_id": "SCN-03", "run_id": "example-owned-v13-access",
            "fixture_kind": "in_memory", "source_revision": catalog_fixture.SOURCE_REVISION,
            "as_of": "2026-10-22T00:00:00Z", "projects": arguments[2],
            "status": "fixture_only", "local_cleanup": "complete",
            "live_cleanup_verified": False, "emitter_receipt_kind": "synthetic",
            "live_acceptance": False,
        })
        for index, value, error in (
            (0, "stale-revision", "source revision"),
            (1, "2026-10-21T00:00:00Z", "as-of clock"),
            (2, (profile_fixture.PROJECT,), "two scoped"),
        ):
            altered = list(arguments)
            altered[index] = value
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, error):
                offline_lineage_access_scenario(*altered, self)

    def test_lineage_access(self):
        offline_lineage_access()

    def test_lineage_denial_and_receipt(self):
        offline_lineage_denial_and_receipt(self)

    def test_lineage_revoked_export_attempts(self):
        offline_lineage_revoked_export_attempts(self)

    def test_insights_cli_checkpoint(self):
        offline_insights_cli_checkpoint()

    def test_insights_cli_revocation(self):
        offline_insights_cli_revocation()

    def test_scn_04_offline_selector(self):
        arguments = (catalog_fixture.SOURCE_REVISION, catalog_fixture.EVENT_TIME,
                     ("example-commerce", "example-shared"))
        self.assertEqual(offline_insights_cli_scenario(*arguments), {
            "scenario_id": "SCN-04", "run_id": "example-owned-v13-cli",
            "fixture_kind": "local_read_service", "source_revision": arguments[0],
            "as_of": arguments[1], "portfolios": arguments[2],
            "status": "fixture_only", "installed_cli_checksum_checked": True,
            "independent_local_readback": True, "revoked_read_denied": True,
            "deployed_receipts_verified": False, "live_cleanup_verified": False,
            "live_acceptance": False,
        })
        for index, value, error in (
            (0, "stale-revision", "source revision"),
            (1, "2026-10-21T00:00:00Z", "as-of clock"),
            (2, ("example-commerce",), "overlapping offline Portfolios"),
        ):
            altered = list(arguments)
            altered[index] = value
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, error):
                offline_insights_cli_scenario(*altered)
