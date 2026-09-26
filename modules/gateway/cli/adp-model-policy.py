#!/usr/bin/env python3
"""Platform-admin model defaults and authoritative runtime-posture rollback."""

from __future__ import annotations

import http.client
import sys
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

POSTURES = ("disabled", "report_only", "enforcing")


def parser():
    root = common.Parser(prog="adp admin models")
    families = root.add_subparsers(dest="family", required=True)
    for family in ("default", "posture"):
        actions = families.add_parser(family).add_subparsers(dest="action", required=True)
        for action in ("show", "set") if family == "default" else ("show", "set", "rollback"):
            p = actions.add_parser(action)
            p.add_argument("--compatibility-class", required=True)
            p.add_argument("--json", action="store_true")
            if action == "show":
                continue
            p.add_argument("--expect-version", type=int)
            p.add_argument("--operation-id")
            p.add_argument("--reason")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--yes", action="store_true")
            if family == "default":
                p.add_argument("--model", required=True)
            elif action == "set":
                p.add_argument("--posture", choices=POSTURES, required=True)
            else:
                p.add_argument("--to-version", type=int, required=True)
    return root


def segment(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact compatibility class or canonical model identifier.", "usage_error", 1)
    return quote(value, safe="")


def state(value, family, compatibility_class):
    revision = "revision" if family == "default" else "posture_revision"
    if (
        not isinstance(value, dict)
        or value.get("compatibility_class") != compatibility_class
        or type(value.get(revision)) is not int
        or value[revision] < 1
    ):
        raise common.CliError("Malformed model policy target or revision.", "invalid_response", 5)
    if family == "default":
        for key in ("candidate_default_model_id", "active_default_model_id", "harness_contract_revision"):
            if key not in value or value[key] is not None and (not isinstance(value[key], str) or not value[key]):
                raise common.CliError("Malformed model default.", "invalid_response", 5)
    elif (
        value.get("posture") not in POSTURES
        or not isinstance(value.get("supported_postures"), list)
        or not all(isinstance(item, str) for item in value["supported_postures"])
        or set(value["supported_postures"]) != set(POSTURES)
        or type(value.get("propagation_bound_seconds")) is not int
        or value["propagation_bound_seconds"] < 0
    ):
        raise common.CliError("Malformed runtime posture.", "invalid_response", 5)
    return value


class Client:
    def __init__(self):
        self.api = common.Api()
        self.token = common.access_token()

    def request(self, method, path, body=None):
        return self.api.request(method, path, body, token=self.token, timeout=30)


def execute(args, client):
    selected = segment(args.compatibility_class)
    path = "/admin/persona-models/" + args.family + "/" + selected
    command = "adp admin models " + args.family + " " + args.action
    before = state(client.request("GET", path), args.family, args.compatibility_class)
    if args.action == "show":
        return common.envelope("ok", command, before, "Stored policy is not evidence of a worker's actual model selection.")
    revision_key = "revision" if args.family == "default" else "posture_revision"
    body = {"expected_revision": args.expect_version, "reason": args.reason}
    preview = dict(current=before, effect="Inherited selections use canonical precedence; explicit preferences and profiles are not rewritten.")
    if args.family == "default":
        segment(args.model)
        promotion = client.request("GET", path + "/preview?" + urlencode({"canonical_model_id": args.model}))
        if (
            not isinstance(promotion, dict)
            or promotion.get("compatibility_class") != args.compatibility_class
            or promotion.get("canonical_model_id") != args.model
            or type(promotion.get("ready")) is not bool
        ):
            raise common.CliError("Malformed default promotion preview.", "invalid_response", 5)
        state(promotion.get("current"), "default", args.compatibility_class)
        preview["promotion"] = promotion
        body["canonical_model_id"] = args.model
    else:
        preview["default"] = state(client.request("GET", "/admin/persona-models/default/" + selected), "default", args.compatibility_class)
        if args.action == "rollback":
            if args.to_version < 1:
                raise common.CliError("--to-version must be positive.", "usage_error", 1)
            history = client.request("GET", path + "/history/" + str(args.to_version))
            if (
                not isinstance(history, dict)
                or history.get("compatibility_class") != args.compatibility_class
                or history.get("posture_revision") != args.to_version
                or history.get("posture") not in POSTURES
                or not isinstance(history.get("audit_id"), str)
                or not history["audit_id"]
            ):
                raise common.CliError("Malformed authoritative posture history.", "invalid_response", 5)
            preview["historical"] = history
            body["historical_revision"] = args.to_version
        else:
            body["posture"] = args.posture
    if args.dry_run:
        preview.update(expected_version=before[revision_key], requested=body)
        return common.envelope("dry_run", command, preview, "No policy changed. Missing probe evidence is not proof a model can run.")
    if (
        not args.yes
        or type(args.expect_version) is not int
        or args.expect_version < 1
        or not args.operation_id
        or not args.reason
        or not args.reason.strip()
        or len(args.reason) > 512
    ):
        raise common.CliError(
            "Review --dry-run, then supply --yes --expect-version VERSION --operation-id UUID --reason TEXT.", "confirmation_required", 1
        )
    try:
        body["operation_id"] = str(UUID(args.operation_id))
    except (ValueError, TypeError):
        raise common.CliError(
            "--operation-id must be a UUID; reuse it with exactly the same request after unknown delivery.", "usage_error", 1
        ) from None
    common.ensure_can_mutate("models.policy.write", request=client.api.request, token=client.token)
    mutation_path = path + "/rollback" if args.action == "rollback" else path
    method = "POST" if args.action == "rollback" else "PUT"
    try:
        ack = state(client.request(method, mutation_path, body), args.family, args.compatibility_class)
        expected_value = args.model if args.family == "default" else preview["historical"]["posture"] if args.action == "rollback" else args.posture
        value_key = "active_default_model_id" if args.family == "default" else "posture"
        if ack[revision_key] != args.expect_version + 1 or ack[value_key] != expected_value:
            raise common.CliError("Policy acknowledgement does not match the reviewed operation.", "invalid_response", 5)
    except common.CliError as exc:
        if exc.code not in {"unknown_mutation_outcome", "invalid_response"} and not (exc.status_code and exc.status_code >= 500):
            raise
        return common.envelope(
            "pending",
            command,
            dict(operation_id=body["operation_id"], outcome="unknown"),
            "Retry the identical operation ID and request to recover its authoritative result.",
        )
    except (http.client.HTTPException, OSError, ValueError):
        return common.envelope(
            "pending",
            command,
            dict(operation_id=body["operation_id"], outcome="unknown"),
            "Retry the identical operation ID and request; do not invent a new operation.",
        )
    try:
        observed = state(client.request("GET", path), args.family, args.compatibility_class)
    except (common.CliError, http.client.HTTPException, OSError, ValueError):
        observed = None
    return common.envelope(
        "ok" if observed == ack else "pending",
        command,
        dict(operation_id=body["operation_id"], operation_result=ack, current=observed, current_matches_operation=observed == ack),
        "Replay returns the original result and never overwrites a later writer. Saved policy is not real-inference evidence.",
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, Client()), args.json)
    except (common.CliError, OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        return common.report_error(exc, "adp admin models", "--json" in argv)
    except KeyboardInterrupt:
        common.emit(
            common.envelope("pending", "adp admin models", {"outcome": "unknown"}, "Inspect policy and retry only the same operation ID/request."),
            "--json" in argv,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
