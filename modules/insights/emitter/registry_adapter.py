"""Scoped Profile and approved-catalog readback for Insights registry facts."""

import hashlib
import json
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class AdmissionError(ValueError):
    pass


class CatalogRevisionUnavailable(AdmissionError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class NoCatalogRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class CatalogAPI:
    """Read exact catalog records; source event provenance is not in this API."""

    def __init__(self, base_url, headers, opener=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("Catalog readback requires an HTTPS base without credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.opener = opener or build_opener(NoCatalogRedirect()).open

    def get_record(self, reference):
        if not RegistryAdapter._qualified_ref(reference):
            raise AdmissionError("catalog requires an immutable qualified reference")
        kind = reference["record_type"]
        if kind not in ("portfolio", "initiative", "contribution"):
            raise AdmissionError("unsupported catalog record type")
        if reference["record_id"] in (".", "..") or reference["project_id"] in (".", ".."):
            raise AdmissionError("invalid catalog path identifier")
        record_id = quote(reference["record_id"], safe="")
        if kind == "contribution":
            path = "/projects/{}/contributions/{}".format(quote(reference["project_id"], safe=""), record_id)
        else:
            path = "/{}/{}".format("portfolios" if kind == "portfolio" else "initiatives", record_id)
        request = Request(self.base_url + path, headers=self.headers())
        try:
            with self.opener(request, timeout=30) as response:
                record = json.load(response)
        except HTTPError as error:
            if error.code in (404, 410):
                raise CatalogRevisionUnavailable(
                    "snapshot_expired" if error.code == 410 else "revision_unresolvable"
                ) from None
            raise
        except ValueError:
            raise CatalogRevisionUnavailable("revision_unresolvable") from None
        required = {"tenant_id", "id", "revision", "source_connection_id", "recorded_at", "data"}
        if (not isinstance(record, dict) or not required <= record.keys()
                or not isinstance(record["data"], dict)
                or any(not isinstance(record[field], str) or not record[field]
                       for field in required - {"data"})):
            raise CatalogRevisionUnavailable("revision_unresolvable")
        if (record["tenant_id"] != reference["tenant_id"]
                or record["source_connection_id"] != reference["source_connection_id"]
                or record["id"] != reference["record_id"]
                or (kind == "contribution" and record.get("project_id") != reference["project_id"])):
            raise AdmissionError("catalog record outside authorized scope")
        if record["revision"] != reference["record_revision"]:
            raise CatalogRevisionUnavailable("revision_unresolvable")
        try:
            if not record["recorded_at"].endswith("Z") or datetime.fromisoformat(record["recorded_at"]).tzinfo is None:
                raise ValueError("invalid recorded time")
        except ValueError:
            raise CatalogRevisionUnavailable("revision_unresolvable") from None
        RegistryAdapter._scope(record, reference["tenant_id"], reference["project_id"])
        return record


class ProfileAPI:
    def __init__(self, base_url, headers, opener=urlopen):
        if not base_url.startswith("https://"):
            raise ValueError("Profile readback requires HTTPS")
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.opener = opener

    def _get(self, product_id, project_id, suffix):
        path = "/v1/products/{}/projects/{}/{}".format(
            quote(product_id, safe=""), quote(project_id, safe=""), suffix
        )
        request = Request(self.base_url + path, headers=self.headers())
        with self.opener(request) as response:
            return json.load(response)

    def list_changes(self, product_id, project_id, cursor=None):
        suffix = "changes" + ("?" + urlencode({"cursor": cursor}) if cursor else "")
        return self._get(product_id, project_id, suffix)

    def get_revision(self, product_id, project_id, revision):
        return self._get(product_id, project_id, "revisions/" + quote(str(revision), safe=""))


class RegistryAdapter:
    def __init__(self, profile, catalog, authorize, sink, mapping_version, clock=None):
        self.profile = profile
        self.catalog = catalog
        self.authorize = authorize
        self.sink = sink
        self.mapping_version = mapping_version
        self.clock = clock or (lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    def _authorized(self, tenant_id, project_id, publisher_id, source_connection_id):
        if not self.authorize(tenant_id, project_id, publisher_id, source_connection_id):
            raise AdmissionError("source grant unavailable")

    @staticmethod
    def _scope(value, tenant_id, project_id):
        if isinstance(value, dict):
            fields = {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
            if fields <= value.keys() and (value["tenant_id"], value["project_id"]) != (tenant_id, project_id):
                raise AdmissionError("reference outside authorized scope")
            for child in value.values():
                RegistryAdapter._scope(child, tenant_id, project_id)
        elif isinstance(value, list):
            for child in value:
                RegistryAdapter._scope(child, tenant_id, project_id)

    @staticmethod
    def _qualified_ref(reference):
        fields = {"tenant_id", "project_id", "source_connection_id",
                  "record_type", "record_id", "record_revision"}
        return (isinstance(reference, dict) and fields == reference.keys()
                and all(isinstance(reference[field], str) and reference[field] for field in fields)
                and reference["record_revision"] not in ("latest", "null"))

    def _authorize_refs(self, value, tenant_id, project_id, publisher_id):
        if isinstance(value, dict):
            fields = {"tenant_id", "project_id", "source_connection_id",
                      "record_type", "record_id", "record_revision"}
            if fields <= value.keys():
                self._authorized(tenant_id, project_id, publisher_id,
                                 value["source_connection_id"])
            for child in value.values():
                self._authorize_refs(child, tenant_id, project_id, publisher_id)
        elif isinstance(value, list):
            for child in value:
                self._authorize_refs(child, tenant_id, project_id, publisher_id)

    def _fact(self, *, tenant_id, project_id, publisher_id, source_connection_id,
              event_id, event_time, effective_at, recorded_at, record_type,
              subject_ref, source_ref, source_revisions, snapshot, mapping_version,
              relationship_version=None):
        identity = [tenant_id, project_id, source_connection_id, event_id,
                    record_type, subject_ref["record_id"], subject_ref["record_revision"], mapping_version]
        normalized_revision = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        fact = {
            "schema_version": "registry/1.0",
            "tenant_id": tenant_id,
            "project_id": project_id,
            "publisher_id": publisher_id,
            "source_connection_id": source_connection_id,
            "source_event_id": event_id,
            "source_event_time": event_time,
            "effective_at": effective_at,
            "recorded_at": recorded_at,
            "ingested_at": self.clock(),
            "mapping_version": mapping_version,
            "normalized_revision": normalized_revision,
            "record_type": record_type,
            "subject_ref": subject_ref,
            "source_ref": source_ref,
            "source_revisions": source_revisions,
            "source_snapshot": snapshot,
        }
        if relationship_version is not None:
            fact["relationship_version"] = relationship_version
        self._scope(fact, tenant_id, project_id)
        self._authorized(tenant_id, project_id, publisher_id, source_connection_id)
        self._authorize_refs(fact, tenant_id, project_id, publisher_id)
        existing = self.sink.admit(normalized_revision, fact)
        if existing is not None:
            immutable = lambda value: {key: item for key, item in value.items()
                                       if key not in ("ingested_at", "publisher_id")}
            if immutable(existing) != immutable(fact):
                self._quarantine(tenant_id, project_id, event_id,
                                 "conflicting_immutable_revision")
                raise AdmissionError("conflicting immutable revision")
            return existing
        return fact

    def _quarantine(self, tenant_id, project_id, event_id, reason):
        self.sink.quarantine({"tenant_id": tenant_id, "project_id": project_id,
                              "source_event_id": event_id, "reason": reason})

    def _read_revision(self, tenant_id, project_id, product_id, change):
        try:
            return self.profile.get_revision(product_id, project_id, change["revision"])
        except HTTPError as error:
            if error.code not in (404, 410):
                raise
            self._quarantine(tenant_id, project_id, change["event_id"],
                             "snapshot_expired" if error.code == 410 else "revision_unresolvable")
            return None

    def ingest_profile(self, tenant_id, project_id, product_id, publisher_id, cursor=None):
        self._authorized(tenant_id, project_id, publisher_id, "adp.profile")
        page = self.profile.list_changes(product_id, project_id, cursor)
        facts = []
        for change in sorted(page["items"], key=lambda item: item["revision"]):
            if (change["tenant_id"], change["project_id"], change["product_id"]) != (tenant_id, project_id, product_id):
                raise AdmissionError("change outside authorized scope")
            self._scope(change, tenant_id, project_id)
            self._authorized(tenant_id, project_id, publisher_id, "adp.profile")
            snapshot = self._read_revision(tenant_id, project_id, product_id, change)
            if snapshot is None:
                continue
            if (snapshot["tenant_id"], snapshot["project_id"], snapshot["product_id"], snapshot["revision"]) != (tenant_id, project_id, product_id, change["revision"]):
                raise AdmissionError("revision outside authorized scope")
            self._scope(snapshot, tenant_id, project_id)
            if "product" not in snapshot or "project" not in snapshot:
                self._quarantine(tenant_id, project_id, change["event_id"], "revision_unresolvable")
                continue
            source_ref = {
                "tenant_id": tenant_id, "project_id": project_id,
                "source_connection_id": "adp.profile", "record_type": "change",
                "record_id": change["event_id"], "record_revision": str(change["revision"]),
            }
            fields = dict(tenant_id=tenant_id, project_id=project_id, publisher_id=publisher_id,
                          source_connection_id="adp.profile", event_id=change["event_id"],
                          event_time=change["recorded_at"], effective_at=change["recorded_at"],
                          recorded_at=change["recorded_at"], source_ref=source_ref,
                          source_revisions=change["source_revisions"], mapping_version=self.mapping_version)
            if any(not isinstance(version, dict) or not {
                "relationship_type", "relationship_id", "relationship_revision",
                "valid_from", "mapping_version", "from_ref", "to_ref",
            } <= version.keys() or version["relationship_type"] != "product_project"
                   for version in change["relationship_versions"]):
                self._quarantine(tenant_id, project_id, change["event_id"], "unresolvable_relationship")
                continue
            for record_type, record_id in (("product", product_id), ("project", project_id)):
                subject_ref = {**source_ref, "record_type": record_type,
                               "record_id": record_id, "record_revision": change["event_id"]}
                facts.append(self._fact(**fields, record_type=record_type,
                                        subject_ref=subject_ref, snapshot={record_type: snapshot[record_type]}))
            for version in change["relationship_versions"]:
                subject_ref = {**source_ref, "record_type": "relationship",
                               "record_id": version["relationship_id"],
                               "record_revision": version["relationship_revision"]}
                facts.append(self._fact(**{**fields, "effective_at": version["valid_from"]},
                                        record_type="relationship", subject_ref=subject_ref,
                                        snapshot={}, relationship_version=version))
        return facts, page["next_cursor"]

    def ingest_catalog(self, reference, publisher_id):
        tenant_id, project_id = reference["tenant_id"], reference["project_id"]
        connection = reference["source_connection_id"]
        self._scope(reference, tenant_id, project_id)
        self._authorized(tenant_id, project_id, publisher_id, connection)
        if self.catalog is None:
            raise AdmissionError("approved catalog readback unavailable")
        try:
            readback = self.catalog.readback(reference)
        except CatalogRevisionUnavailable as error:
            self._quarantine(tenant_id, project_id, reference["record_id"], error.reason)
            return None
        except HTTPError as error:
            if error.code not in (404, 410):
                raise
            self._quarantine(tenant_id, project_id, reference["record_id"],
                             "snapshot_expired" if error.code == 410 else "revision_unresolvable")
            return None
        required = {"subject_ref", "source_ref", "source_revisions", "source_event_id",
                    "source_event_time", "effective_at", "recorded_at", "record_type",
                    "source_snapshot", "mapping_version"}
        if not isinstance(readback, dict) or not required <= readback.keys():
            self._quarantine(tenant_id, project_id, reference["record_id"], "revision_unresolvable")
            return None
        if readback["record_type"] not in ("context_record", "context_relationship"):
            self._quarantine(tenant_id, project_id, reference["record_id"], "unsupported_record_type")
            return None
        timestamps = ("source_event_time", "effective_at", "recorded_at")
        if (not isinstance(readback["source_snapshot"], dict)
                or not self._qualified_ref(readback["source_ref"])
                or not isinstance(readback["source_revisions"], list)
                or any(not self._qualified_ref(item) for item in readback["source_revisions"])
                or any(not isinstance(readback[field], str) or not readback[field]
                       for field in ("source_event_id", "source_event_time", "effective_at",
                                     "recorded_at", "mapping_version"))):
            self._quarantine(tenant_id, project_id, reference["record_id"], "revision_unresolvable")
            return None
        try:
            if any(not readback[field].endswith("Z") or datetime.fromisoformat(readback[field]).tzinfo is None
                   for field in timestamps):
                raise ValueError("invalid source timestamp")
        except ValueError:
            self._quarantine(tenant_id, project_id, reference["record_id"], "revision_unresolvable")
            return None
        if readback["subject_ref"] != reference:
            raise AdmissionError("catalog returned a different revision")
        self._scope(readback, tenant_id, project_id)
        self._authorized(tenant_id, project_id, publisher_id, connection)
        return self._fact(tenant_id=tenant_id, project_id=project_id,
                          publisher_id=publisher_id, source_connection_id=connection,
                          event_id=readback["source_event_id"], event_time=readback["source_event_time"],
                          effective_at=readback["effective_at"], recorded_at=readback["recorded_at"],
                          record_type=readback["record_type"], subject_ref=reference,
                          source_ref=readback["source_ref"], source_revisions=readback["source_revisions"],
                          snapshot=readback["source_snapshot"], mapping_version=readback["mapping_version"],
                          relationship_version=readback.get("relationship_version"))
