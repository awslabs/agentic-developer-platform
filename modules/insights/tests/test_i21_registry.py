"""Offline integration fixtures for scoped registry and catalog readback."""

import importlib.util
import io
import json
import copy
import pathlib
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock


ADAPTER_PATH = pathlib.Path(__file__).resolve().parents[1] / "emitter/registry_adapter.py"
spec = importlib.util.spec_from_file_location("insights_registry_adapter", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

TENANT = "example-tenant"
PROJECT = "example-project"
PRODUCT = "example-product"
TIME = "2026-10-19T10:00:01Z"


def reference(record_type, record_id, revision, connection="adp.profile"):
    return dict(tenant_id=TENANT, project_id=PROJECT,
                source_connection_id=connection, record_type=record_type,
                record_id=record_id, record_revision=revision)


class ProfileFixture:
    def __init__(self):
        self.product_ref = reference("product", PRODUCT, "example-change-1")
        self.project_ref = reference("project", PROJECT, "example-change-1")
        self.relationship = {
            "relationship_id": "example-binding", "relationship_type": "product_project",
            "relationship_revision": "1", "mapping_version": "profile-registry-1",
            "from_ref": self.product_ref, "to_ref": self.project_ref,
            "source_refs": [self.product_ref, self.project_ref],
            "valid_from": "2026-10-18T10:00:00Z", "recorded_at": TIME,
        }
        self.change = dict(tenant_id=TENANT, project_id=PROJECT, product_id=PRODUCT,
                           event_id="example-change-1", revision=1, recorded_at=TIME,
                           source_revisions=[self.product_ref],
                           relationship_versions=[self.relationship])
        self.revision = dict(tenant_id=TENANT, project_id=PROJECT, product_id=PRODUCT,
                             revision=1, product={"name": "Example"},
                             project={"id": PROJECT}, progress={"current": True},
                             context_status="unavailable")
        self.calls = []

    def list_changes(self, product_id, project_id, cursor=None):
        self.calls.append(("listChanges", product_id, project_id, cursor))
        return {"items": [self.change], "next_cursor": None}

    def get_revision(self, product_id, project_id, revision):
        self.calls.append(("getRevision", product_id, project_id, revision))
        return self.revision


class MemorySink:
    def __init__(self):
        self.rows = {}
        self.quarantined = []

    def admit(self, key, fact):
        if key in self.rows:
            return copy.deepcopy(self.rows[key])
        self.rows[key] = copy.deepcopy(fact)
        return None

    def quarantine(self, reason):
        self.quarantined.append(reason)


class RegistryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.profile = ProfileFixture()
        self.sink = MemorySink()
        self.grants = Mock(return_value=True)
        self.registry = adapter.RegistryAdapter(
            self.profile, None, self.grants, self.sink,
            "profile-registry-1", clock=lambda: "2026-10-20T00:00:00Z"
        )

    def test_scoped_profile_and_catalog_readback_preserve_original_refs(self):
        facts, cursor = self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertIsNone(cursor)
        self.assertEqual([item["record_type"] for item in facts], ["product", "project", "relationship"])
        self.assertEqual(self.profile.calls, [
            ("listChanges", PRODUCT, PROJECT, None), ("getRevision", PRODUCT, PROJECT, 1)
        ])
        self.assertEqual(facts[0]["subject_ref"], self.profile.product_ref)
        self.assertEqual(facts[0]["source_revisions"], [self.profile.product_ref])
        self.assertEqual(facts[0]["source_ref"], reference("change", "example-change-1", "1"))
        self.assertEqual(facts[0]["source_snapshot"], {"product": {"name": "Example"}})
        self.assertEqual(facts[0]["source_event_time"], TIME)
        self.assertEqual(facts[0]["ingested_at"], "2026-10-20T00:00:00Z")
        self.assertEqual(facts[2]["relationship_version"], self.profile.relationship)
        self.assertEqual(facts[2]["effective_at"], "2026-10-18T10:00:00Z")
        catalog_ref = reference("portfolio", "example-portfolio", "2", "approved.catalog")
        readback = {
            "subject_ref": catalog_ref, "source_ref": reference("change", "catalog-event", "2", "approved.catalog"),
            "source_revisions": [catalog_ref], "source_event_id": "catalog-event",
            "source_event_time": TIME, "effective_at": "2026-10-18T10:00:00Z",
            "recorded_at": TIME, "mapping_version": "catalog-2", "record_type": "context_record",
            "source_snapshot": {"name": "Example Portfolio"},
        }
        catalog = Mock()
        catalog.readback.return_value = readback
        self.registry.catalog = catalog
        fact = self.registry.ingest_catalog(catalog_ref, "example-publisher")
        catalog.readback.assert_called_once_with(catalog_ref)
        self.assertEqual(fact["source_ref"], readback["source_ref"])
        self.assertEqual(fact["source_event_time"], TIME)
        self.assertEqual(len(self.sink.rows), 4)
        self.assertGreaterEqual(self.grants.call_count, 4)
        self.grants.assert_any_call(TENANT, PROJECT, "example-publisher", "approved.catalog")

    def test_replay_and_out_of_order_events_do_not_duplicate_original_facts(self):
        first, _ = self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        previous = copy.deepcopy(self.sink.rows)
        second_change = {**self.profile.change, "event_id": "example-change-2",
                         "revision": 2, "relationship_versions": [], "source_revisions": []}
        self.profile.list_changes = Mock(return_value={
            "items": [second_change, self.profile.change], "next_cursor": "next-page"
        })
        self.profile.get_revision = Mock(side_effect=lambda product, project, revision: {
            **self.profile.revision, "revision": revision
        })
        restarted = adapter.RegistryAdapter(self.profile, None, self.grants, self.sink,
                                             "profile-registry-1", clock=lambda: "2026-10-21T00:00:00Z")
        replay, cursor = restarted.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual(cursor, "next-page")
        self.assertEqual([fact["source_event_id"] for fact in replay],
                         ["example-change-1"] * 3 + ["example-change-2"] * 2)
        self.assertEqual(replay[:3], first)
        self.assertEqual(len(self.sink.rows), 5)
        for key, fact in previous.items():
            self.assertEqual(self.sink.rows[key], fact)
        self.assertEqual(self.sink.quarantined, [])

    def test_expired_and_unresolvable_revisions_quarantine_without_reconstruction(self):
        for status, reason in ((410, "snapshot_expired"), (404, "revision_unresolvable")):
            with self.subTest(status=status):
                self.profile.get_revision = Mock(side_effect=HTTPError(
                    "https://gateway.example/revisions/1", status, "unavailable", {}, None
                ))
                facts, _ = self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
                self.assertEqual(facts, [])
                self.assertEqual(self.sink.quarantined[-1]["reason"], reason)
                self.assertEqual(self.sink.rows, {})
        self.profile.get_revision = Mock(return_value={
            key: value for key, value in self.profile.revision.items() if key != "project"
        })
        self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual(self.sink.quarantined[-1]["reason"], "revision_unresolvable")
        self.assertEqual(self.sink.rows, {})
        self.profile.get_revision = Mock(side_effect=HTTPError(
            "https://gateway.example/revisions/1", 503, "unavailable", {}, None
        ))
        with self.assertRaises(HTTPError):
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")

    def test_mapping_changes_create_new_revisions_and_conflicts_keep_original(self):
        original, _ = self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        changed = adapter.RegistryAdapter(self.profile, None, self.grants, self.sink,
                                          "profile-registry-2", clock=lambda: "2026-10-21T00:00:00Z")
        remapped, _ = changed.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual(len(self.sink.rows), 6)
        self.assertTrue(all(old["normalized_revision"] != new["normalized_revision"]
                            for old, new in zip(original, remapped)))
        self.assertEqual([item["subject_ref"] for item in original],
                         [item["subject_ref"] for item in remapped])
        original_facts = copy.deepcopy(self.sink.rows)
        self.profile.revision["product"]["name"] = "Different bytes"
        with self.assertRaisesRegex(adapter.AdmissionError, "conflicting immutable revision"):
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual(self.sink.rows, original_facts)
        self.assertEqual(self.sink.quarantined[-1]["reason"], "conflicting_immutable_revision")

    def test_wrong_publisher_and_foreign_scope_fail_without_readback_or_leakage(self):
        self.grants.return_value = False
        with self.assertRaisesRegex(adapter.AdmissionError, "source grant unavailable"):
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "wrong-publisher")
        self.assertEqual(self.profile.calls, [])
        self.assertEqual(self.sink.rows, {})

        self.grants.return_value = True
        self.profile.change["tenant_id"] = "foreign-tenant"
        with self.assertRaises(adapter.AdmissionError) as denied:
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertNotIn("foreign-tenant", str(denied.exception))
        self.assertEqual([call[0] for call in self.profile.calls], ["listChanges"])
        self.assertEqual(self.sink.rows, {})

        self.profile.calls.clear()
        self.profile.change["tenant_id"] = TENANT
        self.profile.change["project_id"] = "foreign-project"
        with self.assertRaisesRegex(adapter.AdmissionError, "outside authorized scope"):
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual([call[0] for call in self.profile.calls], ["listChanges"])
        self.assertEqual(self.sink.rows, {})

    def test_revoked_grant_before_emit_and_nested_source_grants_fail_closed(self):
        self.grants.side_effect = [True, True, False]
        with self.assertRaisesRegex(adapter.AdmissionError, "source grant unavailable"):
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual([call[0] for call in self.profile.calls], ["listChanges", "getRevision"])
        self.assertEqual(self.sink.rows, {})
        self.grants.side_effect = lambda tenant, project, publisher, source: source != "revoked.connector"
        self.profile.change["source_revisions"] = [
            reference("work", "example-work", "1", "revoked.connector")
        ]
        with self.assertRaisesRegex(adapter.AdmissionError, "source grant unavailable"):
            self.registry.ingest_profile(TENANT, PROJECT, PRODUCT, "example-publisher")
        self.assertEqual(self.sink.rows, {})

    def test_catalog_foreign_nested_ref_is_not_emitted(self):
        catalog_ref = reference("portfolio", "example-portfolio", "1", "approved.catalog")
        foreign_ref = {**catalog_ref, "project_id": "foreign-project"}
        catalog = Mock()
        catalog.readback.return_value = {"subject_ref": catalog_ref,
                                         "source_ref": catalog_ref,
                                         "source_event_id": "catalog-event",
                                         "source_event_time": TIME, "effective_at": TIME,
                                         "recorded_at": TIME, "record_type": "context_record",
                                         "source_snapshot": {"nested": [foreign_ref]}, "mapping_version": "catalog-1",
                                         "source_revisions": [catalog_ref]}
        self.registry.catalog = catalog
        with self.assertRaisesRegex(adapter.AdmissionError, "reference outside authorized scope"):
            self.registry.ingest_catalog(catalog_ref, "example-publisher")
        self.assertEqual(self.sink.rows, {})
        self.grants.return_value = False
        catalog.readback.reset_mock()
        with self.assertRaisesRegex(adapter.AdmissionError, "source grant unavailable"):
            self.registry.ingest_catalog(catalog_ref, "wrong-publisher")
        catalog.readback.assert_not_called()

    def test_catalog_rejects_untyped_or_unversioned_historical_readback(self):
        catalog_ref = reference("portfolio", "example-portfolio", "1", "approved.catalog")
        record = {
            "subject_ref": catalog_ref, "source_ref": catalog_ref,
            "source_revisions": [catalog_ref], "source_event_id": "example-event",
            "source_event_time": TIME, "effective_at": TIME, "recorded_at": TIME,
            "record_type": "context_record", "source_snapshot": {"name": "Example"},
            "mapping_version": "catalog-1",
        }
        self.registry.catalog = Mock()
        invalid_records = (
            ({**record, "record_type": "portfolio"}, "unsupported_record_type"),
            ({key: value for key, value in record.items() if key != "source_event_time"}, "revision_unresolvable"),
            ({**record, "source_event_time": "unknown"}, "revision_unresolvable"),
            ({**record, "source_revisions": [{"source_connection_id": "adp.profile"}]}, "revision_unresolvable"),
        )
        for invalid, reason in invalid_records:
            with self.subTest(reason=reason, invalid=invalid):
                self.registry.catalog.readback.return_value = invalid
                self.assertIsNone(self.registry.ingest_catalog(catalog_ref, "example-publisher"))
                self.assertEqual(self.sink.quarantined[-1]["reason"], reason)
                self.assertEqual(self.sink.rows, {})

    def test_raw_v1_task_still_requires_story_and_nested_refs_stay_scoped(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "schemas"
        task = json.loads((root / "lineage/v1/task.schema.json").read_text())
        self.assertIn("story_id", task["required"])
        foreign = {"relationship_version": {"source_refs": [
            reference("task", "example-task", "1") | {"project_id": "foreign-project"}
        ]}}
        with self.assertRaisesRegex(adapter.AdmissionError, "reference outside authorized scope"):
            adapter.RegistryAdapter._scope(foreign, TENANT, PROJECT)

    def test_profile_api_uses_scoped_gateway_endpoints(self):
        paths = []

        def open_request(request):
            paths.append((request.full_url, request.get_header("Authorization")))
            return io.BytesIO(b'{"items": [], "next_cursor": null}')

        api = adapter.ProfileAPI("https://gateway.example", lambda: {"Authorization": "Bearer example"}, open_request)
        self.assertEqual(api.list_changes(PRODUCT, PROJECT, "next page")["items"], [])
        self.assertEqual(api.get_revision(PRODUCT, PROJECT, 7)["next_cursor"], None)
        self.assertEqual(paths, [
            ("https://gateway.example/v1/products/example-product/projects/example-project/changes?cursor=next+page", "Bearer example"),
            ("https://gateway.example/v1/products/example-product/projects/example-project/revisions/7", "Bearer example"),
        ])
        with self.assertRaises(ValueError):
            adapter.ProfileAPI("http://gateway.example", lambda: {})


def insights_admitted_adapter():
    case = RegistryAdapterTests("test_scoped_profile_and_catalog_readback_preserve_original_refs")
    case.setUp()
    case.test_scoped_profile_and_catalog_readback_preserve_original_refs()


insights_admitted_adapter.__test__ = True


def insights_replay_quarantine():
    for name in (
        "test_replay_and_out_of_order_events_do_not_duplicate_original_facts",
        "test_expired_and_unresolvable_revisions_quarantine_without_reconstruction",
        "test_catalog_rejects_untyped_or_unversioned_historical_readback",
        "test_mapping_changes_create_new_revisions_and_conflicts_keep_original",
    ):
        case = RegistryAdapterTests(name)
        case.setUp()
        getattr(case, name)()


insights_replay_quarantine.__test__ = True


def insights_admission_denial():
    for name in (
        "test_wrong_publisher_and_foreign_scope_fail_without_readback_or_leakage",
        "test_revoked_grant_before_emit_and_nested_source_grants_fail_closed",
        "test_catalog_foreign_nested_ref_is_not_emitted",
        "test_raw_v1_task_still_requires_story_and_nested_refs_stay_scoped",
    ):
        case = RegistryAdapterTests(name)
        case.setUp()
        getattr(case, name)()


insights_admission_denial.__test__ = True


if __name__ == "__main__":
    unittest.main()
