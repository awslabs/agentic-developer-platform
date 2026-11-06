"""Exact-revision catalog HTTP reads without fabricated event provenance."""

import copy
import importlib.util
import io
import json
import pathlib
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock


ADAPTER_PATH = pathlib.Path(__file__).resolve().parents[1] / "emitter/registry_adapter.py"
spec = importlib.util.spec_from_file_location("insights_registry_adapter", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class CatalogClientTests(unittest.TestCase):
    def setUp(self):
        self.reference = {
            "tenant_id": "example-tenant", "project_id": "example-project",
            "source_connection_id": "adp.catalog", "record_type": "portfolio",
            "record_id": "example-portfolio", "record_revision": "2",
        }
        self.record = {
            "tenant_id": "example-tenant", "source_connection_id": "adp.catalog",
            "id": "example-portfolio", "revision": "2", "recorded_at": "2026-10-19T00:00:00Z",
            "data": {"name": "Example", "owner_ref": {"kind": "user", "id": "example-owner"},
                     "lifecycle_status": "active"},
        }
        self.requests = []
        self.headers = Mock(return_value={"Authorization": "Bearer example-session"})

        def open_record(request, *, timeout):
            self.requests.append((request, timeout))
            return io.BytesIO(json.dumps(self.record).encode())

        self.client = adapter.CatalogAPI("https://gateway.example/api/v1/catalog", self.headers, open_record)

    def test_documented_routes_preserve_record_and_refresh_headers(self):
        for kind, path in (("portfolio", "/portfolios/example-portfolio"),
                           ("initiative", "/initiatives/example-portfolio"),
                           ("contribution", "/projects/example-project/contributions/example-portfolio")):
            with self.subTest(kind=kind):
                if kind == "contribution":
                    self.record["project_id"] = self.reference["project_id"]
                result = self.client.get_record({**self.reference, "record_type": kind})
                self.assertEqual(result, self.record)
                self.assertNotIn("source_event_id", result)
                request, timeout = self.requests[-1]
                self.assertEqual(request.full_url, "https://gateway.example/api/v1/catalog" + path)
                self.assertEqual(request.get_header("Authorization"), "Bearer example-session")
                self.assertEqual(timeout, 30)
        self.assertEqual(self.headers.call_count, 3)

    def test_identifiers_are_encoded_not_interpreted_as_routes(self):
        self.reference["record_id"] = self.record["id"] = "example/id?revision=1"
        self.client.get_record(self.reference)
        self.assertEqual(self.requests[0][0].full_url,
                         "https://gateway.example/api/v1/catalog/portfolios/example%2Fid%3Frevision%3D1")

    def test_changed_revision_never_returns_current_content(self):
        self.record["revision"] = "3"
        with self.assertRaises(adapter.CatalogRevisionUnavailable) as caught:
            self.client.get_record(self.reference)
        self.assertEqual(caught.exception.reason, "revision_unresolvable")
        self.assertEqual(len(self.requests), 1)

    def test_admission_quarantines_missing_provenance_and_changed_revisions(self):
        sink = Mock()
        registry = adapter.RegistryAdapter(None, Mock(readback=self.client.get_record),
                                           Mock(return_value=True), sink, "example-mapping")
        self.assertIsNone(registry.ingest_catalog(self.reference, "example-publisher"))
        sink.admit.assert_not_called()
        sink.quarantine.assert_called_once_with({
            "tenant_id": "example-tenant", "project_id": "example-project",
            "source_event_id": "example-portfolio", "reason": "revision_unresolvable",
        })
        sink.reset_mock()
        self.record["revision"] = "3"
        self.assertIsNone(registry.ingest_catalog(self.reference, "example-publisher"))
        sink.admit.assert_not_called()
        self.assertEqual(sink.quarantine.call_args.args[0]["reason"], "revision_unresolvable")

    def test_foreign_identity_and_nested_scope_are_denied_without_payload(self):
        original = copy.deepcopy(self.record)
        for field in ("tenant_id", "source_connection_id", "id"):
            with self.subTest(field=field):
                self.record = {**original, field: "hidden-value"}
                with self.assertRaisesRegex(adapter.AdmissionError, "outside authorized scope") as caught:
                    self.client.get_record(self.reference)
                self.assertNotIn("hidden-value", str(caught.exception))
        self.record = {**original, "data": {"source_ref": {**self.reference, "project_id": "hidden-project"}}}
        with self.assertRaisesRegex(adapter.AdmissionError, "outside authorized scope"):
            self.client.get_record(self.reference)
        self.record = {**original, "project_id": "hidden-project"}
        with self.assertRaisesRegex(adapter.AdmissionError, "outside authorized scope"):
            self.client.get_record({**self.reference, "record_type": "contribution"})

    def test_unavailable_and_expired_have_reasoned_errors(self):
        for status, reason in ((404, "revision_unresolvable"), (410, "snapshot_expired")):
            with self.subTest(status=status):
                self.client.opener = Mock(side_effect=HTTPError("https://gateway.example", status, "hidden", {}, None))
                with self.assertRaises(adapter.CatalogRevisionUnavailable) as caught:
                    self.client.get_record(self.reference)
                self.assertEqual(str(caught.exception), reason)
        for status in (401, 403, 503):
            with self.subTest(status=status):
                self.client.opener = Mock(side_effect=HTTPError("https://gateway.example", status, "unavailable", {}, None))
                with self.assertRaises(HTTPError) as caught:
                    self.client.get_record(self.reference)
                self.assertEqual(caught.exception.code, status)

    def test_malformed_records_are_unresolvable(self):
        original = copy.deepcopy(self.record)
        for record in (None, [], {}, {**original, "revision": 2}, {**original, "data": []},
                       {**original, "recorded_at": "yesterday"}, {**original, "recorded_at": "2026-10-19T00:00:00"}):
            with self.subTest(record=record):
                self.record = record
                with self.assertRaises(adapter.CatalogRevisionUnavailable):
                    self.client.get_record(self.reference)
        self.client.opener = Mock(return_value=io.BytesIO(b"not json"))
        with self.assertRaises(adapter.CatalogRevisionUnavailable):
            self.client.get_record(self.reference)

    def test_invalid_reference_is_rejected_before_network(self):
        for changes in ({"record_revision": "latest"}, {"record_type": "relationship"},
                        {"record_id": ".."}, {"project_id": "."}, {"extra": "value"}):
            with self.subTest(changes=changes):
                with self.assertRaises(adapter.AdmissionError):
                    self.client.get_record({**self.reference, **changes})
        self.assertEqual(self.requests, [])
        self.headers.assert_not_called()

    def test_https_base_and_redirect_refusal_protect_authorization(self):
        for base in ("http://gateway.example", "https://example:secret@gateway.example",
                     "https://gateway.example?redirect=other", "https://gateway.example#fragment", "https://"):
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    adapter.CatalogAPI(base, self.headers)
        client = adapter.CatalogAPI("https://gateway.example/api/v1/catalog", self.headers)
        redirect = next(handler for handler in client.opener.__self__.handlers
                        if isinstance(handler, adapter.NoCatalogRedirect))
        self.assertIsNone(redirect.redirect_request(None, None, 302, "Found", {}, "https://other.example"))


if __name__ == "__main__":
    unittest.main()
