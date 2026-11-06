#!/usr/bin/env python3
"""Operate scoped L5 services through the Product gateway."""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common


def parser():
    root = common.Parser(prog="adp service")
    groups = root.add_subparsers(dest="category", required=True, parser_class=common.Parser)

    def command(group, name, *, input_file=False, key=False, match=False, none_match=False, target=None):
        leaf = group.add_parser(name)
        leaf.add_argument("--product", required=True)
        leaf.add_argument("--project", required=True)
        leaf.add_argument("--service", choices=("security", "devops", "finops", "transform", "well_architected"), default="security")
        if target:
            leaf.add_argument("--" + target.replace("_", "-"), required=True)
        if input_file:
            leaf.add_argument("--input", required=True)
        if key:
            leaf.add_argument("--idempotency-key", required=True)
        if match:
            leaf.add_argument("--if-match", required=not none_match)
        if none_match:
            leaf.add_argument("--if-none-match", action="store_true")
        leaf.add_argument("--json", action="store_true")

    for name in ("list", "options", "check"):
        command(groups, name)
    command(groups, "evidence-register", target="request_id")
    command(groups, "evidence-receipt", target="receipt_id")
    config = groups.add_parser("config").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    for name, opts in (("show", {}), ("history", {}), ("version", {"target": "revision"}),
                       ("update", {"input_file": True, "key": True, "match": True, "none_match": True}),
                       ("refresh", {"input_file": True, "key": True, "match": True})):
        command(config, name, **opts)
    binding = groups.add_parser("binding").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    for name, opts in (("list", {}), ("show", {"target": "binding_id"}),
                       ("status", {"target": "operation_id"}),
                       ("create", {"input_file": True, "key": True}), ("link", {"input_file": True, "key": True}),
                       *((action, {"input_file": True, "key": True, "match": True}) for action in
                         ("check", "update", "suspend", "resume", "unlink", "cleanup")),
                       ("reconcile", {"target": "operation_id", "input_file": True, "key": True})):
        command(binding, name, **opts)
    for name, opts in (("run", {"input_file": True, "key": True}),
                       ("runs", {}), ("status", {"target": "run_id"}),
                       ("results", {}), ("findings", {"target": "result_id"})):
        command(groups, name, **opts)
    for name in ("runs", "results"):
        leaf = groups.choices[name]
        leaf.add_argument("--limit", type=int, default=25)
        leaf.add_argument("--cursor")
    wait = groups.add_parser("wait")
    wait.add_argument("--product", required=True)
    wait.add_argument("--project", required=True)
    wait.add_argument("--service", choices=("security", "devops", "finops", "transform", "well_architected"), default="security")
    wait.add_argument("--run-id", required=True)
    wait.add_argument("--wait-seconds", type=int, required=True)
    wait.add_argument("--poll-seconds", type=int, default=5)
    wait.add_argument("--json", action="store_true")
    for name in ("cancel", "reconcile", "retry"):
        command(groups, name, target="run_id", input_file=True, key=True)
    result = groups.add_parser("result").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    command(result, "publication", target="result_id")
    command(result, "show", target="result_id")
    command(binding, "events", target="binding_id")
    triage = groups.add_parser("triage").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    for action, options in (("set", {"input_file": True, "match": True}), ("history", {})):
        command(triage, action, **options)
        leaf = triage.choices[action]
        leaf.add_argument("--result-id", required=True)
        leaf.add_argument("--finding-id", required=True)
    command(groups, "compare", input_file=True)
    publication = groups.add_parser("publication").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    command(publication, "show", target="publication_id")
    command(publication, "retry", target="publication_id", input_file=True, key=True)
    prevention = groups.add_parser("prevention").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    command(prevention, "status", target="binding_id")
    command(prevention, "configure", target="binding_id", input_file=True, key=True, match=True)
    artifact = groups.add_parser("artifact").add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    command(artifact, "show", target="artifact_id")
    command(artifact, "download", target="artifact_id")
    artifact.choices["download"].add_argument("--output", required=True)
    for leaf in (groups.choices["check"], groups.choices["run"], config.choices["update"]):
        leaf.add_argument("--observation-effect", help="Prepared original-check challenge for a durable partial request effect")
    return root


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
        raise common.CliError("Use exact Product, Project and resource identifiers.", "usage_error", 1)
    return quote(value, safe="")


def input_json(filename):
    try:
        path = Path(filename)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("file too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise common.CliError("--input must name a readable JSON object of at most 1 MiB.", "usage_error", 1) from None
    if not isinstance(value, dict):
        raise common.CliError("--input must contain a JSON object.", "usage_error", 1)
    return value


def headers(args):
    result = {}
    key = getattr(args, "idempotency_key", None)
    if key is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise common.CliError("--idempotency-key requires 16–128 letters, digits, _ or -.", "usage_error", 1)
        result["Idempotency-Key"] = key
    match = getattr(args, "if_match", None)
    if getattr(args, "if_none_match", False):
        if match is not None:
            raise common.CliError("Use --if-match or --if-none-match, not both.", "usage_error", 1)
        result["If-None-Match"] = "*"
    elif match is not None:
        if not match.strip():
            raise common.CliError("--if-match requires a nonempty resource ETag.", "usage_error", 1)
        result["If-Match"] = match
    elif hasattr(args, "if_match"):
        raise common.CliError("An update requires --if-match or --if-none-match.", "usage_error", 1)
    return result


def page(args):
    if not 1 <= args.limit <= 100:
        raise common.CliError("--limit must be between 1 and 100.", "usage_error", 2)
    if args.cursor is not None and (not args.cursor or len(args.cursor) > 2048):
        raise common.CliError("--cursor must be a server-issued token of at most 2048 characters.", "usage_error", 2)
    values = {"limit": args.limit}
    if args.cursor is not None:
        values["cursor"] = args.cursor
    return "?" + urlencode(values)


FINOPS_RUN_STATES = {
    "admitted", "materializing", "queued", "submitting", "running", "collecting",
    "succeeded", "partial", "failed", "cancel_requested", "cancelled",
    "outcome_unknown", "expired", "blocked",
}


def transform_verification(args, client, remaining):
    base = ("/v1/products/" + identifier(args.product) + "/projects/" + identifier(args.project)
            + "/services/transform/results")
    cursor = None
    deadline = time.monotonic() + remaining
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        page_path = base + "?" + urlencode({"limit": 100, **({"cursor": cursor} if cursor else {})})
        page = client.request("GET", page_path, timeout=min(remaining, 10))
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise common.CliError("The server returned an invalid result page.", "invalid_response", 5)
        for item in page["items"]:
            if not isinstance(item, dict):
                raise common.CliError("The server returned an invalid result page.", "invalid_response", 5)
            if item.get("run_id") != args.run_id:
                continue
            result_id = item.get("result_id")
            if not isinstance(result_id, str):
                raise common.CliError("The server returned an invalid result reference.", "invalid_response", 5)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            result = client.request("GET", base + "/" + identifier(result_id), timeout=min(remaining, 10))
            if not isinstance(result, dict) or result.get("run_id") != args.run_id or result.get("result_id") != result_id:
                raise common.CliError("The server returned a mismatched result.", "invalid_response", 5)
            return result
        cursor = page.get("next_cursor")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise common.CliError("The server returned an invalid result cursor.", "invalid_response", 5)


def transform_outcome(receipt, result, args):
    scope = result.get("scope")
    run_scope = receipt["scope"]
    if (result.get("service_id") != "transform" or not isinstance(scope, dict)
            or scope.get("product_id") != args.product or scope.get("project_id") != args.project
            or scope.get("tenant_id") != run_scope.get("tenant_id")
            or scope.get("relationship_revision") != run_scope.get("relationship_revision")):
        raise common.CliError("The server returned a mismatched scoped result.", "invalid_response", 5)
    before, after = result.get("tests_before"), result.get("tests_after")
    if (result.get("verification_status") == "succeeded"
            and result.get("scope_status") == "passed"
            and isinstance(before, list) and before and isinstance(after, list) and after
            and all(isinstance(check, dict) and check.get("status") == "passed" for check in before + after)):
        return "ok"
    if result.get("verification_status") in {"partial", "failed"} or any(
            isinstance(check, dict) and check.get("status") == "failed"
            for checks in (before, after) if isinstance(checks, list) for check in checks):
        return "failed"
    return "unknown"


def wait_for_run(args, client, path):
    if not 1 <= args.wait_seconds <= 3600 or not 1 <= args.poll_seconds <= 60:
        raise common.CliError("Wait must be 1–3600 seconds; poll interval must be 1–60 seconds.", "usage_error", 2)
    deadline = time.monotonic() + args.wait_seconds
    receipt = {"run_id": args.run_id}
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return common.envelope("pending", "service wait", receipt, "Read status using the same run ID; do not submit again.")
            receipt = client.request("GET", path, timeout=min(remaining, 10))
            if not isinstance(receipt, dict) or receipt.get("run_id") != args.run_id or not isinstance(receipt.get("state"), str):
                raise common.CliError("The server returned an invalid run receipt.", "invalid_response", 5)
            state = receipt["state"]
            ensure_review_scope(args, receipt)
            if args.service in {"finops", "transform", "well_architected"}:
                scope = receipt.get("scope")
                if (state not in FINOPS_RUN_STATES or not isinstance(scope, dict)
                        or scope.get("product_id") != args.product or scope.get("project_id") != args.project):
                    raise common.CliError("The server returned an invalid scoped run receipt.", "invalid_response", 5)
            if args.service in {"transform", "well_architected"} and (state == "outcome_unknown" or receipt.get("provider_state") == "unknown"):
                response = common.envelope("unknown", "service wait", receipt,
                                           "Read status or reconcile the same run ID; do not submit again.")
                response["error"] = {"code": "run_outcome_unknown", "message": "The run outcome is unknown."}
                return response
            if state == "succeeded":
                if args.service == "transform":
                    result = transform_verification(args, client, max(0, deadline - time.monotonic()))
                    if result is not None:
                        outcome = transform_outcome(receipt, result, args)
                        response = common.envelope(outcome, "service wait", receipt)
                        response["verification"] = result
                        if outcome != "ok":
                            response["error"] = {"code": "verification_" + outcome,
                                                 "message": "Independent patch verification did not pass."}
                        return response
                else:
                    return common.envelope("ok", "service wait", receipt)
            if state in {"partial", "failed", "blocked", "cancelled", "expired"}:
                response = common.envelope("failed", "service wait", receipt)
                response["error"] = {"code": "run_" + state, "message": "The run ended without a complete success."}
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return common.envelope("pending", "service wait", receipt, "Read status using the same run ID; do not submit again.")
            time.sleep(min(args.poll_seconds, remaining))
    except KeyboardInterrupt:
        return common.envelope("pending", "service wait", receipt,
                               "Watch stopped; hosted work continues. Read status using the same run ID; do not submit again.")


def download_artifact(args, client, path):
    access = client.request("GET", path)
    download_path = path + "/content"
    expected = access.get("artifact", {}).get("sha256") if isinstance(access, dict) else None
    if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or access.get("download_path") != download_path):
        raise common.CliError("The server returned an invalid artifact reference.", "invalid_response", 5)
    destination = Path(args.output)
    if destination.exists():
        raise common.CliError("Output already exists; choose a new path.", "usage_error", 2)
    request = urllib.request.Request(client.base + download_path,
                                     headers={"Authorization": "Bearer " + common.access_token()}, method="GET")
    try:
        with client.opener.open(request, timeout=30) as response:
            header_digest = response.headers.get("X-Adp-Content-Sha256")
            if response.status != 200 or header_digest != expected:
                raise common.CliError("Artifact checksum metadata is invalid.", "invalid_response", 5)
            checksum = hashlib.sha256()
            size = 0
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := response.read(65536):
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise common.CliError("Artifact exceeds the 64 MiB download limit.", "invalid_response", 5)
                        checksum.update(chunk)
                        output.write(chunk)
                if checksum.hexdigest() != expected:
                    raise common.CliError("Artifact checksum did not match the receipt.", "invalid_response", 5)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
    except urllib.error.HTTPError as error:
        code = "access_denied" if error.code == 403 else "not_found" if error.code == 404 else "gateway_unavailable"
        raise common.CliError("Artifact download was refused; check access and status.", code,
                              3 if error.code == 403 else 5, status_code=error.code) from None
    except (OSError, urllib.error.URLError, TimeoutError):
        raise common.CliError("Artifact download did not finish; check access and retry.", "gateway_unavailable", 5) from None
    return {"path": str(destination), "sha256": expected, "bytes": size}


def ensure_review_scope(args, payload):
    if args.service != "well_architected":
        return
    is_history = args.category == "config" and args.verb == "history"
    is_collection = args.category in {"runs", "results"} or is_history or (args.category == "binding" and args.verb == "list")
    is_detail = args.category in {"run", "status", "wait", "cancel", "reconcile", "result"} or (
        args.category == "config" and not is_history) or (args.category == "binding" and args.verb == "show")
    if not is_collection and not is_detail:
        return

    def scoped(item):
        scope = item.get("scope") if isinstance(item, dict) else None
        return (isinstance(scope, dict) and isinstance(scope.get("tenant_id"), str) and bool(scope["tenant_id"])
                and scope.get("product_id") == args.product and scope.get("project_id") == args.project)

    if is_collection:
        items = payload.get("items") if isinstance(payload, dict) else None
        valid = isinstance(items, list) and all(scoped(item) for item in items)
    else:
        valid = scoped(payload)
    if not valid:
        raise common.CliError("The server returned a mismatched Product review scope.", "invalid_response", 5)


def execute(args, client):
    base = "/v1/products/" + identifier(args.product) + "/projects/" + identifier(args.project)
    path = base + ("/services" if args.category == "list" else "/services/" + args.service)
    method, body = "GET", None
    if args.category == "options":
        path += "/configuration/options"
    elif args.category == "evidence-register":
        if args.service != "security":
            raise common.CliError("Evidence registration requires --service security.", "usage_error", 2)
        path += "/release-evidence/requests/" + identifier(args.request_id) + "/credentials"
        method, body = "POST", {"slot": "cli"}
    elif args.category == "evidence-receipt":
        if args.service != "security":
            raise common.CliError("Evidence receipts require --service security.", "usage_error", 2)
        path += "/release-evidence/edits/" + identifier(args.receipt_id)
    elif args.category == "check":
        path += "/configuration/check"
        method = "POST"
    elif args.category == "config":
        path += "/configuration"
        if args.verb == "history":
            path += "/versions"
        elif args.verb == "version":
            if not args.revision.isdecimal() or int(args.revision) < 1:
                raise common.CliError("--revision must be positive.", "usage_error", 1)
            path += "/versions/" + args.revision
        elif args.verb == "update":
            method, body = "PUT", input_json(args.input)
        elif args.verb == "refresh":
            method, body = "POST", input_json(args.input)
            path += "/refresh"
    elif args.category == "prevention":
        if args.service != "devops":
            raise common.CliError("Prevention requires --service devops.", "usage_error", 2)
        path += "/bindings/" + identifier(args.binding_id) + "/prevention"
        if args.verb == "configure":
            method, body = "POST", input_json(args.input)
    elif args.category in {"run", "runs", "status", "wait", "cancel", "reconcile", "retry",
                           "results", "result", "findings", "triage", "compare", "publication", "artifact"}:
        if args.service == "finops" and args.category in {"findings", "triage", "compare", "artifact"}:
            raise common.CliError("This operation is unavailable for FinOps.", "unsupported_operation", 5)
        if args.service == "transform" and args.category == "publication":
            raise common.CliError(
                "Transform PR publication is unavailable: no separately authorized SCM handoff. Inspection never publishes.",
                "capability_unavailable", 5,
            )
        if args.service == "transform" and args.category in {"findings", "triage", "compare"}:
            raise common.CliError("This operation is unavailable for Transform.", "unsupported_operation", 5)
        if args.category == "retry" and args.service != "security":
            raise common.CliError("Run retry is unavailable for this service; reconcile the original run.",
                                  "unsupported_operation", 5)
        if args.category == "run":
            path += "/runs"
            method, body = "POST", input_json(args.input)
            if args.service in {"devops", "finops", "transform", "well_architected"} and body.get("service_id") != args.service:
                raise common.CliError("Run input service_id must match --service.", "usage_error", 2)
        elif args.category == "runs":
            path += "/runs" + page(args)
        elif args.category in {"status", "wait", "cancel", "reconcile", "retry"}:
            path += "/runs/" + identifier(args.run_id)
            if args.category == "wait":
                return wait_for_run(args, client, path)
            if args.category in {"cancel", "reconcile", "retry"}:
                path += "/" + args.category
                method, body = "POST", input_json(args.input)
        elif args.category == "results":
            path += "/results" + page(args)
        elif args.category == "result":
            path += "/results/" + identifier(args.result_id)
            if args.verb == "publication":
                detail = client.request("GET", path)
                if not isinstance(detail, dict) or detail.get("result_id") != args.result_id:
                    raise common.CliError("The server returned an invalid result.", "invalid_response", 5)
                ensure_review_scope(args, detail)
                status = client.request("GET", path + "/publication-status")
                if (not isinstance(status, dict) or status.get("result_id") != args.result_id
                        or status.get("revision") != detail.get("revision")
                        or not isinstance(status.get("consumers"), list)):
                    raise common.CliError("The server returned a mismatched publication status.", "invalid_response", 5)
                return common.envelope("ok", "service result publication", status)
        elif args.category == "findings":
            path += "/results/" + identifier(args.result_id) + "/findings"
        elif args.category == "triage":
            path += "/results/" + identifier(args.result_id) + "/findings/" + identifier(args.finding_id) + "/triage"
            if args.verb == "set":
                method, body = "PUT", input_json(args.input)
        elif args.category == "compare":
            path += "/comparisons"
            method, body = "POST", input_json(args.input)
        elif args.category == "publication":
            path += "/publications/" + identifier(args.publication_id)
            if args.verb == "retry":
                path += "/retry"
                method, body = "POST", input_json(args.input)
        elif args.category == "artifact":
            path += "/artifacts/" + identifier(args.artifact_id)
            if args.verb == "download":
                return common.envelope("ok", "service artifact download", download_artifact(args, client, path))
    elif args.category == "binding":
        if args.verb == "events":
            path += "/bindings/" + identifier(args.binding_id) + "/events"
        elif args.verb in ("list", "show"):
            path += "/bindings"
            if args.verb == "show":
                path += "/" + identifier(args.binding_id)
        elif args.verb == "status":
            path += "/binding-operations/" + identifier(args.operation_id)
        elif args.verb == "reconcile":
            path += "/binding-operations/" + identifier(args.operation_id) + "/reconcile"
            method, body = "POST", input_json(args.input)
        else:
            path += "/binding-operations"
            method, body = "POST", input_json(args.input)
            if body.get("action") != args.verb:
                raise common.CliError("Binding input action must match the command.", "usage_error", 1)
    request_headers = headers(args) if method != "GET" else None
    effect = getattr(args, "observation_effect", None)
    if effect is not None:
        if re.fullmatch(r"[a-f0-9-]{36}", effect) is None:
            raise common.CliError("--observation-effect must name a prepared challenge.", "usage_error", 1)
        request_headers["X-ADP-Observation-Effect"] = effect
    if hasattr(client, "last_etag") and method != "GET" and args.category not in {"check", "compare"}:
        common.ensure_can_mutate("service.mutate", refresh=True, request=client.request, require_known=True)
    result = (client.request(method, path, body, extra_headers=request_headers) if method != "GET" else
              client.request(method, path))
    if not isinstance(result, dict):
        raise common.CliError("The server returned an invalid service response.", "invalid_response", 5)
    ensure_review_scope(args, result)
    etag = getattr(client, "last_etag", None)
    if hasattr(client, "last_etag") and (
        args.category == "config" and args.verb != "history"
        or args.category == "binding" and args.verb == "show"
        or args.category == "triage"
    ) and not etag:
        raise common.CliError("The server omitted the required resource ETag.", "invalid_response", 5)
    if args.service in {"transform", "well_architected"} and args.category == "run":
        scope = result.get("scope")
        if (not isinstance(result.get("run_id"), str) or not isinstance(scope, dict)
                or scope.get("product_id") != args.product or scope.get("project_id") != args.project):
            raise common.CliError("The server returned an invalid scoped run receipt.", "invalid_response", 5)
    response = common.envelope("accepted" if args.service in {"transform", "well_architected"} and args.category == "run" else "ok",
                               "service " + args.category + (" " + args.verb if hasattr(args, "verb") else ""), result,
                               "Run accepted, not verified. Use service status or wait to read the original run."
                               if args.service == "well_architected" and args.category == "run" else
                               "Run accepted, not verified. Use service wait for independent checks."
                               if args.service == "transform" and args.category == "run" else None)
    if etag:
        response["etag"] = etag
    return response


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        response = execute(args, common.Api())
        emitted = common.emit(response, args.json)
        if not args.json and response.get("etag"):
            print("ETag:", response["etag"])
        if args.category == "wait" and response["status"] in {"pending", "failed", "unknown"}:
            return {"pending": 7, "failed": 6, "unknown": 8}[response["status"]]
        return emitted
    except common.CliError as error:
        common.report_error(error, "service", "--json" in argv, include_http_status=True)
        if error.code in {"usage_error", "validation_error"}:
            return 2
        if error.status_code in {409, 412}:
            return 4
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
