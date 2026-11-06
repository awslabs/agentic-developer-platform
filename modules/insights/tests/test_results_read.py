"""Run-owned local HTTP checks for the authorized I22 result view."""

import json
import pathlib
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from insights import results


TIME = "2026-10-19T12:00:00Z"
LATER = "2026-10-21T12:00:00Z"


def reference(kind, identifier):
    return {"tenant_id": "example-tenant", "project_id": "example-project",
            "source_connection_id": "example-source", "record_type": kind,
            "record_id": identifier, "record_revision": "example-revision-1"}


def relationship(kind, identifier, source, target, *, start="2026-10-18T00:00:00Z", recorded=TIME, mapping="example-map-1"):
    return {"relationship_type": kind, "relationship_id": identifier,
            "relationship_revision": "example-revision-1", "source_connection_id": "example-source",
            "from_ref": source, "to_ref": target, "valid_from": start,
            "recorded_at": recorded, "mapping_version": mapping}


def fixture():
    product = reference("product", "example-product")
    other = reference("product", "other-product")
    work = {"subject_ref": reference("work", "example-work"), "kind": "work",
            "effective_at": TIME, "recorded_at": TIME}
    cost = {"subject_ref": reference("cost", "example-cost"), "kind": "cost", "amount": "10.00",
            "currency": "USD", "effective_at": TIME, "recorded_at": TIME}
    newer = {"subject_ref": reference("work", "example-new-work"), "kind": "work",
             "effective_at": LATER, "recorded_at": LATER}
    foreign = {"subject_ref": reference("work", "other-work"), "kind": "work",
               "effective_at": TIME, "recorded_at": TIME}
    links = [relationship("work_product", "work-link", work["subject_ref"], product),
             relationship("work_product", "cost-link", cost["subject_ref"], product),
             relationship("work_product", "new-link", newer["subject_ref"], product,
                          start="2026-10-20T00:00:00Z", recorded=LATER, mapping="example-map-2"),
             relationship("work_product", "other-link", foreign["subject_ref"], other),
             relationship("portfolio_product", "old-portfolio", reference("portfolio", "example-old"), product),
             relationship("portfolio_product", "new-portfolio", reference("portfolio", "example-new"), product,
                          start="2026-10-20T00:00:00Z", recorded=LATER, mapping="example-map-2")]
    return {"status": "complete", "dataset_version": "example-dataset-1",
            "mapping_versions": ["example-map-1", "example-map-2"],
            "facts": [work, cost, newer, foreign], "relationships": links, "allocations": []}


def shared_policy(snapshot):
    references = {}
    for key in results.snapshot_references(snapshot):
        references[key] = dict(zip(results.projection.REFERENCE_FIELDS, key))
    dependencies = list(references.values())
    return {
        "schema_version": "results-policy/1.0", "tenant_id": "example-tenant",
        "project_id": "example-project", "dataset_version": snapshot["dataset_version"],
        "snapshot_sha256": results.snapshot_digest(snapshot), "policy_epoch": 1,
        "envelopes": [{
            "envelope_version": 1, "identity": dependencies[0], "realm_id": "example-realm",
            "realm_revision": 1, "descriptor_digest": "a" * 64,
            "audience": "shared", "dependency_identities": dependencies,
            "dependency_versions": [{"identity": identity, "revision": 1, "metadata_digest": "b" * 64}
                                    for identity in dependencies],
            "required_policies": [
                {"ref": name, "revision": 1, "visibility": "shared", "audience_expression": "current:project-readers"}
                for name in ("source-policy", "evidence-policy")],
        }],
    }


class LocalResultsAPITest(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture()
        self.granted = True
        self.snapshot_reads = []

        def load(tenant, project):
            self.snapshot_reads.append((tenant, project))
            return self.snapshot

        def can_read(tenant, project, viewer):
            return (self.granted and tenant == "example-tenant"
                    and project == "example-project" and viewer == "example-viewer")

        self.policy = shared_policy(self.snapshot)
        self.source_granted = True
        self.reader = results.ResultsReader(
            load, can_read, "example-cursor-secret",
            read_policy=lambda tenant, project: self.policy,
            can_read_policy=lambda tenant, project, viewer, policy: self.source_granted,
        )
        app = results.ResultsApp(self.reader, lambda environ: ("example-tenant", "example-viewer")
                                 if environ.get("HTTP_AUTHORIZATION") == "Bearer example-session" else None)
        self.server = make_server("127.0.0.1", 0, app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, project_id="example-project", **params):
        query = urlencode({"view": "business_context", "product_id": "example-product",
                           "as_of": TIME, **params}, doseq=True)
        url = f"http://127.0.0.1:{self.server.server_port}/engineering-insights/v1/projects/{project_id}/results?{query}"
        request = Request(url, headers={"Authorization": "Bearer example-session"})
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_foreign_and_revoked_project_cannot_reveal_snapshot(self):
        status, authorized = self.request()
        self.assertEqual(status, 200)
        self.assertIn("example-work", str(authorized))
        reads = len(self.snapshot_reads)
        foreign_status, foreign = self.request(project_id="foreign-project")
        self.granted = False
        revoked_status, revoked = self.request()
        self.assertEqual((foreign_status, revoked_status), (404, 404))
        self.assertEqual(foreign, {"error": {"code": "not_found", "message": "Result unavailable"}})
        self.assertEqual(foreign, revoked)
        self.assertEqual(len(self.snapshot_reads), reads)
        self.assertNotIn("example-work", json.dumps(revoked))
        self.assertNotIn("example-source", json.dumps(revoked))

        self.granted = True
        def revoke_during_read(tenant, project):
            self.granted = False
            return self.snapshot
        self.reader.read_snapshot = revoke_during_read
        status, mid_read = self.request()
        self.assertEqual(status, 404)
        self.assertEqual(mid_read, foreign)
        def visible_snapshot(tenant, project):
            return self.snapshot
        self.reader.read_snapshot = visible_snapshot
        checks = []
        def revoke_during_projection(tenant, project, viewer):
            checks.append(True)
            return len(checks) < 3
        self.reader.can_read_project = revoke_during_projection
        status, mid_projection = self.request()
        self.assertEqual(status, 404)
        self.assertEqual(mid_projection, foreign)

    def test_unsupported_and_unpublished_snapshots_have_no_result_body(self):
        status, unsupported = self.request(view="other")
        self.assertEqual(status, 501)
        self.assertEqual(unsupported, {"error": {"code": "unsupported_capability", "message": "Result unavailable"}})
        self.assertEqual(self.snapshot_reads, [])
        for snapshot, expected in (
            (None, "projection_pending"),
            ({"status": "pending"}, "projection_pending"),
            ({"status": "incomplete", "facts": [], "relationships": []}, "projection_incomplete"),
            ({"status": "complete", "dataset_version": "example-version",
              "mapping_versions": ["example-map-1"], "relationships": []}, "projection_incomplete"),
        ):
            with self.subTest(expected=expected, snapshot=snapshot):
                self.snapshot = snapshot
                status, error = self.request()
                self.assertEqual(status, 409)
                self.assertEqual(error, {"error": {"code": expected, "message": "Result unavailable"}})
                self.assertNotIn("work_count", error)
                self.assertNotIn("example-source", str(error))

    def test_authorized_api_read_filters_product_and_as_of(self):
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(result["dataset_version"], "example-dataset-1")
        self.assertEqual(result["mapping_versions"], ["example-map-1"])
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["work_count"], 1)
        self.assertEqual(result["cost_by_currency"], {"USD": "10.00"})
        self.assertEqual({item["subject_ref"]["record_id"] for item in result["contexts"]},
                         {"example-work", "example-cost"})
        self.assertEqual({item["portfolio_ids"][0] for item in result["contexts"]}, {"example-old"})
        self.assertNotIn("other-work", str(result))
        status, later = self.request(as_of=LATER)
        self.assertEqual(status, 200)
        self.assertEqual(later["work_count"], 2)
        self.assertEqual(later["mapping_versions"], ["example-map-1", "example-map-2"])
        self.assertEqual({item["subject_ref"]["record_id"] for item in later["contexts"]},
                         {"example-work", "example-cost", "example-new-work"})
        self.assertEqual({item["effective_at"] for item in later["contexts"]}, {TIME, LATER})

    def test_api_rejects_timestamps_outside_rfc3339_before_snapshot_read(self):
        for as_of in (
            "20261019T120000Z",
            "2026-10-19 12:00:00Z",
            "2026-W43-1T12:00:00Z",
            "2026-10-19T12:00Z",
            "2026-10-19T12:00:00,5Z",
            "2026-10-19T12:00:00",
            "2026-10-19T12:00:00+0000",
            "2026-10-19T12:00:00+00:00:30",
            "2026-10-19T12:00:00+00:60",
            "2026-10-19T12:00:00+24:00",
            "2026-02-30T12:00:00Z",
            "2026-10-19T24:00:00Z",
            "2026-10-19T12:00:00Z\n",
        ):
            with self.subTest(as_of=as_of):
                status, error = self.request(as_of=as_of)
                self.assertEqual(status, 400)
                self.assertEqual(error, {"error": {"code": "invalid_request", "message": "Result unavailable"}})
        self.assertEqual(self.snapshot_reads, [])

    def test_api_accepts_rfc3339_offsets_and_fractional_seconds(self):
        for as_of in (
            TIME,
            "2026-10-19T13:30:00+01:30",
            "2026-10-19T08:30:00-03:30",
            "2026-10-19T12:00:00.123456789Z",
            "2026-10-19T13:30:00.123456789+01:30",
        ):
            with self.subTest(as_of=as_of):
                status, result = self.request(as_of=as_of)
                self.assertEqual(status, 200)
                self.assertEqual(result["as_of"], as_of)
                self.assertEqual(result["work_count"], 1)
                self.assertEqual(result["cost_by_currency"], {"USD": "10.00"})

    def test_shared_native_facts_are_returned_once_with_historical_membership(self):
        work, cost = self.snapshot["facts"][:2]
        self.snapshot["facts"].extend([work.copy(), cost.copy()])
        product = reference("product", "example-product")
        self.snapshot["relationships"].extend([
            relationship("portfolio_product", "shared-portfolio",
                         reference("portfolio", "example-shared"), product),
            {**relationship("portfolio_product", "old-portfolio",
                            reference("portfolio", "example-old"), product,
                            recorded=LATER, mapping="example-map-2"),
             "relationship_revision": "example-revision-2", "valid_to": "2026-10-20T00:00:00Z"},
        ])
        self.policy = shared_policy(self.snapshot)
        filters = {"portfolio_id": ["example-old", "example-shared"]}
        for as_of, expected_work_count, expected_mappings in (
            (TIME, 1, ["example-map-1"]),
            (LATER, 2, ["example-map-1", "example-map-2"]),
        ):
            with self.subTest(as_of=as_of):
                status, result = self.request(as_of=as_of, **filters)
                self.assertEqual(status, 200)
                self.assertEqual(result["coverage"], "complete")
                self.assertEqual(result["dataset_version"], "example-dataset-1")
                self.assertEqual(result["mapping_versions"], expected_mappings)
                self.assertEqual(result["work_count"], expected_work_count)
                self.assertEqual(result["cost_by_currency"], {"USD": "10.00"})
                native_refs = [item["subject_ref"] for item in result["contexts"]]
                self.assertEqual(len(native_refs), len({tuple(ref.items()) for ref in native_refs}))
                self.assertEqual(sum(ref["record_id"] == "example-cost" for ref in native_refs), 1)
                association_refs = [item["subject_ref"] for item in result["associations"]]
                self.assertCountEqual(association_refs, native_refs)
                if as_of == LATER:
                    newer = next(item for item in result["contexts"]
                                 if item["subject_ref"]["record_id"] == "example-new-work")
                    self.assertEqual(newer["portfolio_ids"], ["example-new", "example-shared"])
                self.assertEqual(next(ref for ref in native_refs if ref["record_id"] == "example-work"),
                                 work["subject_ref"])
                historical = next(item for item in result["contexts"]
                                  if item["subject_ref"] == work["subject_ref"])
                self.assertEqual(historical["portfolio_ids"], ["example-old", "example-shared"])
                self.assertEqual({link["mapping_version"] for link in historical["relationship_versions"]},
                                 {"example-map-1"})

    def test_bounded_pages_reject_cursor_for_other_filters(self):
        status, first = self.request(limit=1)
        self.assertEqual(status, 200)
        self.assertEqual(len(first["contexts"]), 1)
        self.assertEqual(first["cost_by_currency"], {"USD": "10.00"})
        status, second = self.request(limit=1, cursor=first["next_cursor"])
        self.assertEqual(status, 200)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(len(second["contexts"]), 1)
        self.assertNotEqual(first["contexts"], second["contexts"])
        status, error = self.request(limit=1, cursor=first["next_cursor"], portfolio_id=["example-new"])
        self.assertEqual((status, error["error"]["code"]), (400, "invalid_request"))


    def test_cursor_cannot_cross_policy_epoch_or_dependency_revision(self):
        status, first = self.request(limit=1)
        self.assertEqual(status, 200)
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        self.policy["policy_epoch"] += 1
        status, denied = self.request(limit=1, cursor=cursor)
        self.assertEqual((status, denied["error"]["code"]), (400, "invalid_request"))
        status, changed = self.request(limit=1)
        self.assertEqual(status, 200)
        self.assertNotEqual(changed["next_cursor"], cursor)
        self.policy["envelopes"][0]["dependency_versions"][0]["revision"] += 1
        status, denied = self.request(limit=1, cursor=changed["next_cursor"])
        self.assertEqual((status, denied["error"]["code"]), (400, "invalid_request"))

    def test_cursor_cannot_cross_viewer_or_use_pre_policy_signature(self):
        status, first = self.request(limit=1)
        self.assertEqual(status, 200)
        self.reader.can_read_project = lambda tenant, project, viewer: True
        with self.assertRaises(results.ReadError) as denied:
            self.reader.read(
                tenant_id="example-tenant", viewer_id="other-viewer", project_id="example-project",
                product_id="example-product", as_of=TIME, limit=1, cursor=first["next_cursor"],
            )
        self.assertEqual(denied.exception.status, 400)
        old_scope = ["example-tenant", "example-viewer", "example-project", "example-product",
                     TIME, [], 1, "example-dataset-1"]
        status, error = self.request(limit=1, cursor=self.reader._cursor(1, old_scope))
        self.assertEqual((status, error["error"]["code"]), (400, "invalid_request"))

    def test_caller_paths_and_unbounded_cursors_are_invalid(self):
        for parameters in (
            {"project_id": ".."},
            {"product_id": "other.product"},
            {"portfolio_id": ["../foreign"]},
            {"path": "results/v1/foreign-project/published.json"},
            {"cursor": "0." + "a" * 513},
            {"limit": 101},
            {"limit": 0},
        ):
            with self.subTest(parameters=parameters):
                status, error = self.request(**parameters)
                self.assertEqual((status, error["error"]["code"]), (400, "invalid_request"))


    def test_revocation_after_load_denies_response_and_old_page(self):
        status, first = self.request(limit=1)
        self.assertEqual(status, 200)
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        load = self.reader.read_snapshot

        def revoke_after_load(tenant, project):
            snapshot = load(tenant, project)
            self.source_granted = False
            return snapshot

        self.reader.read_snapshot = revoke_after_load
        status, denied = self.request()
        self.assertEqual((status, denied["error"]["code"]), (404, "not_found"))
        self.assertNotIn("example-source", json.dumps(denied))
        self.reader.read_snapshot = load
        reads = len(self.snapshot_reads)
        status, old_page = self.request(limit=1, cursor=cursor)
        self.assertEqual((status, old_page["error"]["code"]), (404, "not_found"))
        self.assertEqual(len(self.snapshot_reads), reads)
        self.source_granted = True
        status, current_page = self.request(limit=1, cursor=cursor)
        self.assertEqual(status, 200)
        self.assertEqual(len(current_page["contexts"]), 1)


    def test_snapshot_bytes_must_match_published_policy(self):
        self.snapshot["facts"][1]["amount"] = "999.00"
        status, denied = self.request()
        self.assertEqual((status, denied["error"]["code"]), (409, "projection_incomplete"))
        self.assertNotIn("999.00", json.dumps(denied))
        self.policy = shared_policy(self.snapshot)
        status, current = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(current["cost_by_currency"], {"USD": "999.00"})

    def test_missing_or_private_policy_fails_closed_without_loading_snapshot(self):
        self.policy = None
        status, missing = self.request()
        self.assertEqual((status, missing["error"]["code"]), (409, "projection_incomplete"))
        self.assertEqual(self.snapshot_reads, [])
        self.policy = shared_policy(self.snapshot)
        self.policy["envelopes"][0]["required_policies"][0]["visibility"] = "private"
        status, private = self.request()
        self.assertEqual((status, private["error"]["code"]), (409, "projection_incomplete"))
        self.assertEqual(self.snapshot_reads, [])
        self.assertNotIn("example-source", json.dumps(private))
        self.policy = shared_policy(self.snapshot)
        self.policy["envelopes"][0]["required_policies"][0]["audience_expression"] = "current:team-readers"
        status, restricted = self.request()
        self.assertEqual((status, restricted["error"]["code"]), (409, "projection_incomplete"))
        self.assertEqual(self.snapshot_reads, [])

    def test_denied_source_policy_withholds_complete_totals(self):
        self.source_granted = False
        status, denial = self.request()
        self.assertEqual((status, denial["error"]["code"]), (404, "not_found"))
        self.assertEqual(self.snapshot_reads, [])
        self.assertNotIn("example-cost", json.dumps(denial))
        self.source_granted = True
        status, report = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(report["cost_by_currency"], {"USD": "10.00"})


    def test_policy_must_cover_every_snapshot_dependency(self):
        self.policy["envelopes"][0]["dependency_identities"].pop()
        self.policy["envelopes"][0]["dependency_versions"].pop()
        status, error = self.request()
        self.assertEqual((status, error["error"]["code"]), (409, "projection_incomplete"))
        self.assertNotIn("example-source", json.dumps(error))

    def test_policy_must_match_dataset_version_and_tenant(self):
        self.policy["dataset_version"] = "old-version"
        status, error = self.request()
        self.assertEqual((status, error["error"]["code"]), (409, "projection_incomplete"))
        self.policy = shared_policy(self.snapshot)
        self.policy["envelopes"][0]["dependency_identities"][0]["tenant_id"] = "foreign-tenant"
        status, error = self.request()
        self.assertEqual((status, error["error"]["code"]), (409, "projection_incomplete"))


if __name__ == "__main__":
    unittest.main()
