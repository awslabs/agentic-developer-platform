#!/usr/bin/env python3
"""Resume saved Product onboarding through the gateway Product API."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common


def parser():
    root = common.Parser(prog="adp onboard")
    commands = root.add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    for name in ("start", "resume", "step", "guide", "retry", "unlink"):
        command = commands.add_parser(name)
        command.add_argument("--product", required=True)
        command.add_argument("--project", required=True)
        command.add_argument("--session")
        command.add_argument("--revision", type=int)
        command.add_argument("--idempotency-key")
        command.add_argument("--json", action="store_true")
        if name == "retry":
            command.add_argument("--operation", required=True)
        if name == "unlink":
            command.add_argument("--binding", required=True)
        if name in ("start", "step", "guide"):
            command.add_argument("--input")
        if name == "guide":
            command.add_argument("--mode", choices=("deterministic", "model", "financial"))
        if name == "step":
            command.add_argument("--section", required=True, choices=("information", "basic", "advanced", "financials"))
    return root


def usage(message):
    raise common.CliError(message, "usage_error", 1)


def identifier(value):
    if not isinstance(value, str) or len(value) > 255 or not re.fullmatch(r"\S(?:.*\S)?", value) or value in {".", ".."} or "/" in value:
        usage("Product, Project and session must be exact IDs.")
    return quote(value, safe="")


def input_body(source):
    try:
        value = json.loads(sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        usage("--input must be a readable UTF-8 JSON object.")
    if not isinstance(value, dict):
        usage("--input must contain a JSON object.")
    return value


def mutation_headers(args):
    if args.revision is None or not 1 <= args.revision <= 9007199254740991:
        usage("--revision must be the positive current profile revision.")
    if args.idempotency_key is None or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", args.idempotency_key):
        usage("--idempotency-key must contain 16–128 letters, digits, _ or -.")
    return {"Idempotency-Key": args.idempotency_key, "If-Profile-Revision": str(args.revision)}


def execute(args, client):
    path = "/v1/products/" + identifier(args.product) + "/projects/" + identifier(args.project) + "/onboarding-sessions"
    if args.verb == "resume" and args.session:
        if args.revision is not None or args.idempotency_key is not None:
            usage("Resume by --session needs neither --revision nor --idempotency-key.")
        result = client.request("GET", path + "/" + identifier(args.session))
    elif args.verb in ("start", "resume"):
        if args.verb == "start" and args.session:
            usage("Start does not accept --session; use resume --session for a read.")
        headers = mutation_headers(args)
        body = input_body(args.input) if args.verb == "start" and args.input else {}
        common.ensure_can_mutate("product.update", request=client.request)
        result = client.request("POST", path, body, extra_headers=headers)
        result = client.request("GET", path + "/" + identifier(result["id"]))
    elif args.verb in ("retry", "unlink"):
        if not args.session:
            usage("Retry and unlink require --session.")
        headers = mutation_headers(args)
        session_path = path + "/" + identifier(args.session)
        saved = client.request("GET", session_path)
        if args.verb == "retry":
            identifier(args.operation)
            attempt = next((item for item in saved.get("discoveries", []) if item.get("id") == args.operation), None)
            if not attempt or attempt.get("coverage") == "denied" or not attempt.get("cursor") or not attempt.get("binding_ids"):
                usage("Only an authorized, resumable discovery can be retried.")
            if attempt.get("adapter") == "devlake":
                try:
                    flags = client.request("GET", "/features")["features"]
                except (common.CliError, KeyError, TypeError):
                    flags = {}
                if not isinstance(flags, dict) or flags.get("devlake_connectors") is not True:
                    raise common.CliError("Optional connectors are not available on this server. Nothing was sent.", "feature_disabled", 5)
            body = {"binding_ids": attempt["binding_ids"], "adapter": attempt["adapter"], "cursor": attempt["cursor"]}
            common.ensure_can_mutate("product.update", request=client.request)
            client.request("POST", session_path + "/discoveries", body, extra_headers=headers)
        else:
            binding_id = identifier(args.binding)
            common.ensure_can_mutate("product.update", request=client.request)
            client.request("DELETE", path.rsplit("/onboarding-sessions", 1)[0] + "/bindings/" + binding_id, extra_headers=headers)
        result = client.request("GET", session_path)
    elif args.verb == "guide":
        if not args.session or not args.input:
            usage("Guide requires --session and --input.")
        headers = mutation_headers(args)
        body = input_body(args.input)
        if args.mode is not None:
            if "mode" in body and body["mode"] != args.mode:
                usage("--mode must match the mode in --input.")
            body["mode"] = args.mode
        common.ensure_can_mutate("product.update", request=client.request)
        result = client.request("POST", path + "/" + identifier(args.session) + "/guidance", body, extra_headers=headers)
    else:
        if not args.session or not args.input:
            usage("Step requires --session and --input.")
        headers = mutation_headers(args)
        body = input_body(args.input)
        common.ensure_can_mutate("product.update", request=client.request)
        result = client.request("PUT", path + "/" + identifier(args.session) + "/steps/" + args.section, body, extra_headers=headers)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("revision"), int)
        or (args.verb != "guide" and not isinstance(result.get("id"), str))
    ):
        raise common.CliError("Malformed onboarding session response.", "invalid_response", 5)
    return common.envelope("ok", "onboard " + args.verb, result)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, common.Api()), args.json)
    except (common.CliError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, common.CliError) and exc.code == "unknown_mutation_outcome":
            exc = common.CliError(
                "Onboarding write outcome is unknown. Read the session first, then retry with the same key, revision and input.",
                "unknown_mutation_outcome",
                4,
            )
        return common.report_error(exc, "onboard", "--json" in argv)


if __name__ == "__main__":
    sys.exit(main())
