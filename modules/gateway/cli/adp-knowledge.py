#!/usr/bin/env python3
"""Knowledge registry and bounded indexing views over canonical APIs."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

ASSETS = "/api/agent-context/assets"
TERMINAL = {"indexed", "completed", "ready", "failed", "error", "removed"}


def parser():
    root = common.Parser(prog="adp")
    areas = root.add_subparsers(dest="area", required=True)
    commands = areas.add_parser("knowledge").add_subparsers(dest="action", required=True)
    for name in ("add", "list", "show", "delete", "status", "watch", "reindex"):
        p = commands.add_parser(name)
        p.add_argument("--json", action="store_true")
        if name in {"show", "delete", "status", "watch", "reindex"}:
            p.add_argument("asset_id")
        if name == "add":
            p.add_argument("--file", required=True, help="JSON AssetCreateRequest; source credentials are refused")
            p.add_argument("--key", required=True, help="Stable UUID identifying this registration")
        if name == "list":
            p.add_argument("--scope", choices=["personal", "tenant"])
            p.add_argument("--type", choices=["repo", "url", "doc"])
            p.add_argument("--status")
            p.add_argument("--page", type=int, default=1)
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20)
        if name in {"add", "delete", "reindex"}:
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
        if name == "reindex":
            p.add_argument("--key", required=True, help="Stable UUID; reuse it after a lost response")
        if name == "watch":
            p.add_argument("--timeout", type=int, choices=range(1, 3601), default=300)
            p.add_argument("--interval", type=int, choices=range(1, 61), default=5)
    bulk = commands.add_parser("bulk").add_subparsers(dest="bulk_action", required=True)
    p = bulk.add_parser("preview")
    p.add_argument("--file", required=True, help='JSON object: {"scope":"personal", "items":[...]}')
    p.add_argument("--json", action="store_true")
    p = bulk.add_parser("commit")
    p.add_argument("--preview-id", required=True)
    p.add_argument("--expect-hash", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    admin = areas.add_parser("admin").add_subparsers(dest="admin_area", required=True)
    indexing = admin.add_parser("indexing").add_subparsers(dest="action", required=True)
    for name in ("list", "show"):
        p = indexing.add_parser(name)
        p.add_argument("--json", action="store_true")
        if name == "show":
            p.add_argument("--run", required=True)
        else:
            p.add_argument("--page", type=int, default=1)
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20)
    return root


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise common.CliError("Use a UUID identifier.", "usage_error", 1) from None


def source_item(item):
    if not isinstance(item, dict) or item.get("asset_type") not in {"repo", "url", "doc"}:
        raise common.CliError("Use repo, url or doc asset objects.", "usage_error", 1)
    source = item.get("source_ref")
    if not isinstance(source, str) or not source or len(source) > 2048:
        raise common.CliError("Each asset requires a source_ref.", "usage_error", 1)
    parsed = urllib.parse.urlsplit(source)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in source):
        raise common.CliError("Source references cannot contain credentials, queries, fragments or whitespace.", "unsafe_source", 1)
    if parsed.scheme not in {"https", "s3"} or not parsed.hostname:
        raise common.CliError("Use a credential-free HTTPS or S3 source reference.", "unsafe_source", 1)
    return item


def load_file(filename):
    path = Path(filename)
    with path.open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise common.CliError("Source file exceeds 1 MiB.", "usage_error", 1)
    return json.loads(raw)


def safe(value):
    """Whitelist registry/progress fields; free-text errors and metadata may hold secrets."""
    scalar = {
        "id",
        "asset_id",
        "duration_ms",
        "verified_at",
        "attempts",
        "total_repos",
        "repos_verified",
        "repos_failed",
        "repos_partial",
        "failed_stages",
        "drift_count",
        "fully_verified_pct",
        "asset_type",
        "status",
        "run_id",
        "run_status",
        "run_started_at",
        "stage",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "retry_count",
        "repo_found",
        "total",
        "page",
        "page_size",
        "has_more",
        "created",
        "skipped_duplicates",
        "quota_ok",
        "total_lines",
        "parsed",
        "skipped_comments",
        "used",
        "limit",
    }
    nested = {"summary", "items", "assets", "stages", "quota", "quota_after", "repos", "urls", "docs"}
    if isinstance(value, list):
        return [safe(row) for row in value]
    if not isinstance(value, dict):
        return None
    output = {key: val for key, val in value.items() if key in scalar and (val is None or isinstance(val, (str, int, float, bool)))}  # noqa: UP038 -- Python 3.9 CLI
    for key in nested & value.keys():
        output[key] = safe(value[key])
    for key in ("source_ref", "artifact_ref"):
        if value.get(key):
            output[key + "_sha256"] = digest(value[key])
    for key in ("error", "last_error", "status_detail"):
        if value.get(key):
            output[key + "_present"] = True
    return output


def invalid_response(message="Malformed knowledge response.", *, mutation=False):
    raise common.CliError(message, "unknown_mutation_outcome" if mutation else "invalid_response", 4 if mutation else 5)


def response_id(value, expected=None, *, mutation=False):
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        invalid_response("Response lacks a valid resource identifier.", mutation=mutation)
    if expected is not None and canonical != expected:
        invalid_response("Response names another resource; inspect the requested resource.", mutation=mutation)
    return canonical


def timestamp(value, *, mutation=False):
    try:
        if not isinstance(value, str):
            raise ValueError
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        invalid_response("Response timestamp is malformed.", mutation=mutation)


def asset_response(value, expected=None, *, source=None, mutation=False):
    if not isinstance(value, dict):
        invalid_response(mutation=mutation)
    response_id(value.get("id"), expected, mutation=mutation)
    if (
        value.get("asset_type") not in {"repo", "url", "doc"}
        or value.get("status") not in {"registered", "queued", "indexing", "indexed", "failed", "removed"}
        or not isinstance(value.get("source_ref"), str)
        or not value["source_ref"]
        or not isinstance(value.get("created_at"), str)
    ):
        invalid_response(mutation=mutation)
    timestamp(value["created_at"], mutation=mutation)
    if source is not None and any(value.get(key) != source.get(key) for key in ("asset_type", "source_ref")):
        invalid_response("Registration acknowledgement names another source.", mutation=mutation)
    return value


def run_response(value, expected=None):
    if not isinstance(value, dict):
        invalid_response("Malformed indexing run response.")
    run = response_id(value.get("run_id" if expected else "id"), expected)
    if not isinstance(value.get("status"), str) or not value["status"] or not isinstance(value.get("started_at"), str):
        invalid_response("Indexing run lacks status or start time.")
    timestamp(value["started_at"])
    if expected is not None:
        if not isinstance(value.get("stages"), list):
            invalid_response("Indexing run lacks stages.")
        for stage in value["stages"]:
            if not isinstance(stage, dict) or not isinstance(stage.get("stage"), str) or not isinstance(stage.get("status"), str):
                invalid_response("Indexing stage is malformed.")
            response_id(stage.get("run_id"), run)
    return value


def page_response(value, page, page_size, validate_item):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("items"), list)
        or type(value.get("total")) is not int
        or value["total"] < 0
        or type(value.get("has_more")) is not bool
        or type(value.get("page")) is not int
        or type(value.get("page_size")) is not int
        or value.get("page") != page
        or value.get("page_size") != page_size
    ):
        invalid_response("Malformed knowledge pagination response.")
    if (
        len(value["items"]) > page_size
        or len(value["items"]) > value["total"]
        or value["has_more"] != ((page - 1) * page_size + page_size < value["total"])
    ):
        invalid_response("Knowledge pagination is inconsistent.")
    for item in value["items"]:
        validate_item(item)
    return value


def context(client):
    return {"gateway": client.base, **common.authenticated_scope()}


def receipt_path(key):
    directory = common.private_directory(common.state_dir() / "knowledge")
    return directory / (identifier(key) + ".json")


def mutation(client, key, body, method, path):
    """Local at-most-once receipt; canonical source dedup protects other clients."""
    target = receipt_path(key)
    binding = {"context": context(client), "method": method, "path": path, "body": body}
    with common.file_lock(target.with_suffix(".lock"), "Knowledge operation is already running."):
        if target.exists():
            previous = common.read_private_json(target)
            if previous.get("hash") != digest(binding):
                raise common.CliError("Request key belongs to different inputs or scope.", "stale_revision", 4)
            if previous.get("result") is not None:
                return previous["result"]
            raise common.CliError(
                "Previous outcome is unknown; inspect the asset list/status before another operation.", "unknown_mutation_outcome", 4
            )
        record = {"hash": digest(binding), "state": "pending"}
        common.write_json(target, record)
        result = client.request(method, path, body)
        asset_response(result, source=body, mutation=True)
        record.update(state="acknowledged", result=safe(result))
        common.write_json(target, record)
        return record["result"]


def status_result(value, expected=None):
    if not isinstance(value, dict) or not isinstance(value.get("stages"), list) or not value.get("asset_id"):
        raise common.CliError("Indexing status is unavailable or malformed.", "dependency_pending", 4)
    if not all(isinstance(stage, dict) and isinstance(stage.get("status"), str) for stage in value["stages"]):
        raise common.CliError("Indexing stages are malformed.", "dependency_pending", 4)
    response_id(value.get("asset_id"), expected)
    if value.get("status") not in {"registered", "queued", "indexing", "indexed", "failed", "removed"}:
        invalid_response("Asset status is missing or unknown.")
    if value.get("run_id") is not None:
        response_id(value["run_id"])
    # The registry can be indexed while the latest run still failed. Do not infer
    # usability from registration, queueing, missing stages or a missing backend.
    failed = (
        value.get("status") in {"failed", "error"}
        or value.get("run_status") in {"failed", "error"}
        or any(stage.get("status") in {"failed", "error"} for stage in value["stages"])
    )
    usable = (
        value.get("status") in {"indexed", "completed", "ready"}
        and value.get("run_status") in {"completed", "succeeded", "success", "verified"}
        and bool(value.get("run_id"))
        and bool(value["stages"])
        and all(stage.get("status") in {"completed", "succeeded", "success", "verified", "skipped"} for stage in value["stages"])
    )
    detail = safe(value)
    detail["usable"] = usable
    detail["indexing_evidence_available"] = bool(value.get("run_id") and value["stages"])
    return common.envelope("failed" if failed else "ok" if usable else "pending", "knowledge status", detail)


def execute(args, client):
    if args.area == "admin":
        path = "/admin/indexing/runs"
        if args.action == "show":
            path += "/" + identifier(args.run)
        else:
            if args.page < 1:
                raise common.CliError("Page must be positive.", "usage_error", 1)
            path += "?" + urllib.parse.urlencode({"page": args.page, "page_size": args.page_size})
        value = client.request("GET", path)
        if args.action == "show":
            run_response(value, identifier(args.run))
        else:
            page_response(value, args.page, args.page_size, run_response)
        return common.envelope("ok", "admin indexing " + args.action, safe(value))
    action = args.action
    if action == "bulk":
        return bulk(args, client)
    if action == "list":
        if args.page < 1:
            raise common.CliError("Page must be positive.", "usage_error", 1)
        params = {"page": args.page, "page_size": args.page_size, "scope": args.scope, "asset_type": args.type, "status": args.status}
        value = client.request("GET", ASSETS + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}))
        page_response(value, args.page, args.page_size, asset_response)
        return common.envelope("ok", "knowledge list", safe(value))
    if action == "add":
        body = source_item(load_file(args.file))
        identifier(args.key)
        if args.dry_run or not args.yes:
            return common.envelope(
                "preview", "knowledge add", {"source_sha256": digest(body), "dispatch": "registration may enqueue indexing; pass --yes"}
            )
        common.ensure_can_mutate("knowledge.asset.write")
        value = mutation(client, args.key, body, "POST", ASSETS)
        return common.envelope("pending", "knowledge add", value, "Use knowledge status; registration is not retrieval readiness.")
    asset = identifier(args.asset_id)
    path = ASSETS + "/" + asset
    if action == "show":
        return common.envelope("ok", "knowledge show", safe(asset_response(client.request("GET", path), asset)))
    if action in {"status", "watch"}:
        deadline = time.monotonic() + (args.timeout if action == "watch" else 0)
        while True:
            remaining = max(1, deadline - time.monotonic()) if action == "watch" else 120
            value = client.request("GET", path + "/status", timeout=min(120, remaining))
            result = status_result(value, asset)
            if action == "status" or result["status"] != "pending" or time.monotonic() >= deadline:
                return result
            common.emit(result, args.json)
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    if args.dry_run or not args.yes:
        return common.envelope(
            "preview",
            "knowledge " + action,
            {
                "asset_id": asset,
                "effect": "soft removal; index artifacts retained" if action == "delete" else "enqueue a new indexing attempt",
                "confirm": "--yes",
            },
        )
    common.ensure_can_mutate("knowledge.asset.write")
    if action == "delete":
        if client.request("DELETE", path) != {}:
            invalid_response("Malformed removal acknowledgement; inspect asset state.", mutation=True)
        return common.envelope("ok", "knowledge delete", {"asset_id": asset, "soft_deleted": True, "index_artifacts_retained": True})
    key = identifier(args.key)
    value = client.request("POST", path + "/reindex?" + urllib.parse.urlencode({"request_id": key}))
    asset_response(value, asset, mutation=True)
    return common.envelope("pending", "knowledge reindex", safe(value), "Reuse the same --key after an uncertain response; inspect knowledge status.")


def bulk(args, client):
    if args.bulk_action == "preview":
        body = load_file(args.file)
        if (
            not isinstance(body, dict)
            or body.get("scope") not in {"personal", "tenant"}
            or not isinstance(body.get("items"), list)
            or not 1 <= len(body["items"]) <= 500
        ):
            raise common.CliError("Use a scope and 1 to 500 items.", "usage_error", 1)
        for item in body["items"]:
            source_item(item)
        preview = client.request("POST", ASSETS + "/bulk/preview-json", body)
        if (
            not isinstance(preview, dict)
            or not isinstance(preview.get("valid"), list)
            or not isinstance(preview.get("rejected"), list)
            or type(preview.get("quota_ok")) is not bool
            or not isinstance(preview.get("duplicates"), list)
            or not all(isinstance(item, dict) for item in preview["valid"])
        ):
            raise common.CliError("Malformed preview response.", "invalid_response", 5)
        items = [{k: v for k, v in item.items() if k in {"asset_type", "source_ref", "display_name", "tags"}} for item in preview["valid"]]
        for item in items:
            source_item(item)
        exact = {"scope": body["scope"], "items": items}
        key = str(uuid.uuid4())
        record = {
            "context": context(client),
            "body": exact,
            "hash": digest(exact),
            "created": time.time(),
            "committable": preview["quota_ok"] and not preview["rejected"] and bool(items),
        }
        common.write_json(receipt_path(key), record)
        detail = {
            "preview_id": key,
            "hash": record["hash"],
            "committable": record["committable"],
            "valid": len(items),
            "rejected": len(preview["rejected"]),
            "duplicates": len(preview.get("duplicates", [])),
            "quota_ok": preview["quota_ok"],
            "sources": [{"asset_type": item["asset_type"], "source_sha256": digest(item["source_ref"])} for item in items],
            "validation": "Final access/source admission runs at commit; no server writes in preview.",
        }
        return common.envelope("preview", "knowledge bulk preview", detail)
    target = receipt_path(args.preview_id)
    with common.file_lock(target.with_suffix(".lock"), "Bulk commit is already running."):
        record = common.read_private_json(target)
        if record.get("context") != context(client) or record.get("hash") != args.expect_hash or digest(record.get("body")) != args.expect_hash:
            raise common.CliError("Preview hash or authenticated scope changed.", "stale_revision", 4)
        if record.get("result") is not None:
            return common.envelope("pending", "knowledge bulk commit", record["result"])
        if record.get("attempted"):
            raise common.CliError("Commit outcome is unknown; inspect assets. This preview will not dispatch twice.", "unknown_mutation_outcome", 4)
        if not record.get("committable") or time.time() - record["created"] > 3600:
            raise common.CliError("Preview expired or contains rejected/over-quota items.", "stale_revision", 4)
        if args.dry_run or not args.yes:
            return common.envelope("preview", "knowledge bulk commit", {"hash": args.expect_hash, "confirm": "--yes"})
        common.ensure_can_mutate("knowledge.asset.write")
        record["attempted"] = True
        common.write_json(target, record)
        result = client.request("POST", ASSETS + "/bulk/commit", record["body"])
        if not isinstance(result, dict) or not isinstance(result.get("assets"), list):
            raise common.CliError("Malformed commit acknowledgement; inspect assets.", "unknown_mutation_outcome", 4)
        if (
            type(result.get("created")) is not int
            or type(result.get("skipped_duplicates")) is not int
            or result["created"] != len(result["assets"])
            or result["skipped_duplicates"] < 0
            or result["created"] + result["skipped_duplicates"] != len(record["body"]["items"])
        ):
            invalid_response("Bulk acknowledgement counts do not match the reviewed batch.", mutation=True)
        sources = {(item["asset_type"], item["source_ref"]) for item in record["body"]["items"]}
        ids = set()
        for asset in result["assets"]:
            asset_response(asset, mutation=True)
            if (asset["asset_type"], asset["source_ref"]) not in sources or asset["id"] in ids:
                invalid_response("Bulk acknowledgement contains another source or duplicate asset.", mutation=True)
            ids.add(asset["id"])
        record["result"] = safe(result)
        common.write_json(target, record)
        return common.envelope(
            "pending", "knowledge bulk commit", record["result"], "Inspect each asset status; bulk registration is not indexing completion."
        )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    args = None
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, common.Api()), as_json)
    except KeyboardInterrupt:
        mutation = (
            args is not None
            and args.area == "knowledge"
            and (args.action in {"add", "delete", "reindex"} or (args.action == "bulk" and args.bulk_action == "commit"))
            and args.yes
            and not args.dry_run
        )
        if mutation:
            result = common.envelope(
                "pending",
                "knowledge " + args.action,
                {"outcome": "unknown", "mutation_may_have_completed": True},
                "Inspect asset state and retain the same operation key or preview receipt before retrying.",
            )
            result["error"] = {"code": "unknown_mutation_outcome", "message": "Interrupted before the mutation outcome was confirmed."}
        elif args is not None and args.area == "knowledge" and args.action == "watch":
            result = common.envelope("pending", "knowledge watch", {"detached": True, "indexing_continues": True})
        else:
            result = common.envelope("pending", "knowledge", {"interrupted": True})
        common.emit(result, as_json)
        return 130
    except (common.CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, "knowledge", as_json)


if __name__ == "__main__":
    raise SystemExit(main())
