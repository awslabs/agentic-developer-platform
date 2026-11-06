"""Bounded HTTP view over an authorized, published I22 projection snapshot."""

import hashlib
import hmac
import json
import re
from datetime import datetime
from urllib.parse import parse_qs

from . import projection


ROUTE = re.compile(r"^/engineering-insights/v1/projects/([^/]+)/results$")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
STATUS = {400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict", 501: "Not Implemented"}


class ReadError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def _instant(value):
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ReadError(400, "invalid_request")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReadError(400, "invalid_request") from None
    if parsed.tzinfo is None:
        raise ReadError(400, "invalid_request")
    return parsed


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ReadError(400, "invalid_request")
    return value


def _ref_key(reference):
    return tuple(reference[field] for field in projection.REFERENCE_FIELDS)


def policy_dependencies(policy, tenant_id, project_id):
    if not isinstance(policy, dict) or set(policy) != {
        "schema_version", "tenant_id", "project_id", "dataset_version", "snapshot_sha256", "policy_epoch", "envelopes"
    } or policy["schema_version"] != "results-policy/1.0" or (
        policy["tenant_id"], policy["project_id"]
    ) != (tenant_id, project_id):
        raise ReadError(409, "projection_incomplete")
    if (not isinstance(policy["dataset_version"], str) or not policy["dataset_version"]
            or not isinstance(policy["snapshot_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", policy["snapshot_sha256"])
            or type(policy["policy_epoch"]) is not int or policy["policy_epoch"] < 1
            or not isinstance(policy["envelopes"], list) or not 1 <= len(policy["envelopes"]) <= 100):
        raise ReadError(409, "projection_incomplete")
    realm = None
    identities = set()
    for envelope in policy["envelopes"]:
        if not isinstance(envelope, dict) or envelope.get("envelope_version") != 1 or envelope.get("audience") != "shared":
            raise ReadError(409, "projection_incomplete")
        required = envelope.get("required_policies")
        dependencies = envelope.get("dependency_identities")
        versions = envelope.get("dependency_versions")
        if (not isinstance(required, list) or not 2 <= len(required) <= 100
                or not isinstance(dependencies, list) or not 1 <= len(dependencies) <= 100
                or not isinstance(versions, list) or len(versions) != len(dependencies)):
            raise ReadError(409, "projection_incomplete")
        current_realm = (envelope.get("realm_id"), envelope.get("realm_revision"), envelope.get("descriptor_digest"))
        if (not isinstance(current_realm[0], str) or not current_realm[0]
                or type(current_realm[1]) is not int or current_realm[1] < 1
                or not isinstance(current_realm[2], str) or not re.fullmatch(r"[0-9a-f]{64}", current_realm[2])
                or realm is not None and realm != current_realm):
            raise ReadError(409, "projection_incomplete")
        realm = current_realm
        expressions = set()
        for requirement in required:
            if (not isinstance(requirement, dict)
                    or set(requirement) != {"ref", "revision", "visibility", "audience_expression"}
                    or requirement["visibility"] != "shared" or type(requirement["revision"]) is not int
                    or requirement["revision"] < 1 or not isinstance(requirement["ref"], str)
                    or not requirement["ref"] or not isinstance(requirement["audience_expression"], str)
                    or not requirement["audience_expression"]):
                raise ReadError(409, "projection_incomplete")
            expressions.add(requirement["audience_expression"])
        if expressions != {"current:project-readers"}:
            raise ReadError(409, "projection_incomplete")
        for identity, version in zip(dependencies, versions):
            if (not isinstance(identity, dict) or set(identity) != set(projection.REFERENCE_FIELDS)
                    or any(not isinstance(value, str) or not value for value in identity.values())
                    or (identity["tenant_id"], identity["project_id"]) != (tenant_id, project_id)
                    or not isinstance(version, dict) or version.get("identity") != identity
                    or type(version.get("revision")) is not int or version["revision"] < 1
                    or not isinstance(version.get("metadata_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", version["metadata_digest"])):
                raise ReadError(409, "projection_incomplete")
            identities.add(_ref_key(identity))
        if envelope.get("identity") not in dependencies:
            raise ReadError(409, "projection_incomplete")
    if len(identities) > 100:
        raise ReadError(409, "projection_incomplete")
    return identities


def snapshot_references(value):
    if isinstance(value, dict):
        if set(projection.REFERENCE_FIELDS) <= value.keys():
            yield _ref_key(value)
        for item in value.values():
            yield from snapshot_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from snapshot_references(item)


def snapshot_digest(snapshot):
    try:
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise ReadError(409, "projection_incomplete") from None
    return hashlib.sha256(canonical).hexdigest()


class ResultsReader:
    def __init__(self, read_snapshot, can_read_project, cursor_secret, *, read_policy=None, can_read_policy=None):
        self.read_snapshot = read_snapshot
        self.can_read_project = can_read_project
        self.read_policy = read_policy
        self.can_read_policy = can_read_policy
        self.cursor_secret = cursor_secret.encode()
        if not self.cursor_secret:
            raise ValueError("cursor signing secret required")

    def _cursor(self, position, scope):
        data = str(position)
        signature = hmac.new(self.cursor_secret, (data + json.dumps(scope)).encode(), hashlib.sha256).hexdigest()
        return data + "." + signature

    def _position(self, cursor, scope):
        if cursor is None:
            return 0
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ReadError(400, "invalid_request")
        try:
            encoded, signature = cursor.split(".")
            if not re.fullmatch(r"[0-9]+", encoded) or not re.fullmatch(r"[0-9a-f]{64}", signature):
                raise ValueError
            expected = hmac.new(self.cursor_secret, (encoded + json.dumps(scope)).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            position = int(encoded)
            if position < 0:
                raise ValueError
            return position
        except (ValueError, TypeError, UnicodeError):
            raise ReadError(400, "invalid_request") from None

    def read(self, *, tenant_id, viewer_id, project_id, product_id, as_of, portfolio_ids=(), limit=100, cursor=None):
        project_id = _identifier(project_id)
        product_id = _identifier(product_id)
        portfolio_ids = tuple(_identifier(portfolio_id) for portfolio_id in portfolio_ids)
        if len(portfolio_ids) > 20 or len(set(portfolio_ids)) != len(portfolio_ids) or type(limit) is not int or not 1 <= limit <= 100:
            raise ReadError(400, "invalid_request")
        effective = _instant(as_of)
        def require_project_access():
            if not self.can_read_project(tenant_id, project_id, viewer_id):
                raise ReadError(404, "not_found")

        require_project_access()
        if self.read_policy is None or self.can_read_policy is None:
            raise ReadError(409, "projection_incomplete")
        policy = self.read_policy(tenant_id, project_id)
        policy_dependencies(policy, tenant_id, project_id)
        if not self.can_read_policy(tenant_id, project_id, viewer_id, policy):
            raise ReadError(404, "not_found")
        snapshot = self.read_snapshot(tenant_id, project_id)
        require_project_access()
        if snapshot is None:
            raise ReadError(409, "projection_pending")
        if not isinstance(snapshot, dict):
            raise ReadError(409, "projection_incomplete")
        if snapshot.get("status") == "pending":
            raise ReadError(409, "projection_pending")
        if snapshot.get("status") != "complete":
            raise ReadError(409, "projection_incomplete")
        if (not all(isinstance(snapshot.get(field), list) for field in ("facts", "relationships"))
                or not all(isinstance(snapshot.get(field, []), list) for field in ("allocations", "contributions"))):
            raise ReadError(409, "projection_incomplete")

        version = snapshot.get("dataset_version")
        mappings = snapshot.get("mapping_versions")
        if not isinstance(version, str) or not version or version in ("latest", "null") or not isinstance(mappings, list) or not mappings or any(
                not isinstance(mapping, str) or not mapping or mapping in ("latest", "null") for mapping in mappings):
            raise ReadError(409, "projection_incomplete")
        if (version != policy["dataset_version"] or snapshot_digest(snapshot) != policy["snapshot_sha256"]
                or not set(snapshot_references(snapshot)) <= policy_dependencies(policy, tenant_id, project_id)):
            raise ReadError(409, "projection_incomplete")
        if "selection_as_of" in snapshot:
            try:
                selected_at = _instant(snapshot["selection_as_of"])
            except ReadError:
                raise ReadError(409, "projection_incomplete") from None
            if effective != selected_at:
                raise ReadError(409, "projection_incomplete")
        policy_digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        scope = ["results/1.0", "business_context", tenant_id, viewer_id, project_id,
                 product_id, as_of, sorted(portfolio_ids), limit, version, sorted(mappings),
                 policy["policy_epoch"], policy_digest]
        position = self._position(cursor, scope)
        allowed = lambda tenant, project: self.can_read_project(tenant, project, viewer_id)
        facts = [fact for fact in snapshot["facts"] if _instant(fact["effective_at"]) <= effective
                 and _instant(fact["recorded_at"]) <= effective]
        relationships = [link for link in snapshot["relationships"] if _instant(link["recorded_at"]) <= effective]
        contributions = [item for item in snapshot.get("contributions", ()) if _instant(item["recorded_at"]) <= effective]
        args = dict(tenant_id=tenant_id, project_ids=[project_id], can_read_project=allowed, contributions=contributions)
        full = projection.query_authorized_projection(facts, relationships, snapshot.get("allocations", ()), **args)
        if full["coverage"] != "complete":
            require_project_access()
            raise ReadError(409, "projection_incomplete")
        selected = {_ref_key(context["subject_ref"]) for context in full["contexts"]
                    if any(ref["record_type"] == "product" and ref["record_id"] == product_id
                           for ref in context["product_refs"])}
        facts = [fact for fact in facts if _ref_key(fact["subject_ref"]) in selected]
        filtered = projection.query_authorized_projection(facts, relationships, snapshot.get("allocations", ()),
                                                           portfolio_ids=portfolio_ids, **args)
        if filtered["coverage"] != "complete":
            require_project_access()
            raise ReadError(409, "projection_incomplete")
        contexts = sorted(filtered["contexts"], key=lambda item: _ref_key(item["subject_ref"]))
        if position >= len(contexts) and position != 0:
            raise ReadError(400, "invalid_request")
        page = contexts[position:position + limit]
        included = {_ref_key(context["subject_ref"]) for context in page}
        allocations = [entry for entry in filtered["allocation_versions"] if _ref_key(entry["source_ref"]) in included]
        if len(allocations) > 100:
            raise ReadError(409, "projection_incomplete")
        relevant = {link["mapping_version"] for context in filtered["contexts"]
                    for link in context["relationship_versions"]}
        relevant.update(item["mapping_version"] for item in contributions
                        if any(item["subject_ref"] == ref for context in filtered["contexts"]
                               for ref in context["contribution_refs"]))
        if not all(mapping in mappings for mapping in relevant):
            raise ReadError(409, "projection_incomplete")
        def money(amounts):
            return {currency: format(amount, "f") for currency, amount in amounts.items()}
        end = position + len(page)
        require_project_access()
        if not self.can_read_policy(tenant_id, project_id, viewer_id, policy):
            raise ReadError(404, "not_found")
        return {
            "schema_version": "results/1.0", "view": "business_context", "project_id": project_id,
            "product_id": product_id, "as_of": as_of, "dataset_version": version,
            "mapping_versions": sorted(relevant) or mappings, "coverage": "complete", "grouping": filtered["grouping"],
            "work_count": filtered["work_count"], "cost_by_currency": money(filtered["cost_by_currency"]),
            "allocated_by_currency": money(filtered["allocated_by_currency"]),
            "unallocated_by_currency": money(filtered["unallocated_by_currency"]),
            "contexts": page, "associations": [item for item in filtered["associations"]
                                                if _ref_key(item["subject_ref"]) in included],
            "allocation_versions": allocations,
            "next_cursor": self._cursor(end, scope) if end < len(contexts) else None,
        }


class ResultsApp:
    def __init__(self, reader, authenticate):
        self.reader = reader
        self.authenticate = authenticate

    def __call__(self, environ, start_response):
        match = ROUTE.fullmatch(environ.get("PATH_INFO", ""))
        if not match or environ.get("REQUEST_METHOD") != "GET":
            start_response("404 Not Found", [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
            return [b'{"error":{"code":"not_found","message":"Result unavailable"}}']
        try:
            identity = self.authenticate(environ)
            if identity is None:
                raise ReadError(401, "unauthenticated")
            params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            if set(params) - {"view", "product_id", "as_of", "portfolio_id", "limit", "cursor"}:
                raise ReadError(400, "invalid_request")
            def single(key, default=None):
                values = params.get(key, [default])
                if len(values) != 1:
                    raise ReadError(400, "invalid_request")
                return values[0]
            if single("view") != "business_context":
                raise ReadError(501, "unsupported_capability")
            try:
                limit = int(single("limit", "100"))
            except (TypeError, ValueError):
                raise ReadError(400, "invalid_request") from None
            result = self.reader.read(tenant_id=identity[0], viewer_id=identity[1], project_id=match.group(1),
                                      product_id=single("product_id"), as_of=single("as_of"),
                                      portfolio_ids=params.get("portfolio_id", ()), limit=limit, cursor=single("cursor"))
            status, body = "200 OK", result
        except ReadError as error:
            status = f"{error.status} {STATUS[error.status]}"
            body = {"error": {"code": error.code, "message": "Result unavailable"}}
        start_response(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
        return [json.dumps(body, separators=(",", ":")).encode()]
