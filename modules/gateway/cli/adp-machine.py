#!/usr/bin/env python3
"""Manage explicitly typed machine identities and canonical principal metadata."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

SOURCES = ("sa_registration", "agent_registry", "cognito_m2m", "eventbridge", "github_actions")
BASE = "/admin/service-principals"


def parser():
    root = common.Parser(prog="adp admin", description=__doc__)
    areas = root.add_subparsers(dest="area", required=True)
    actions = areas.add_parser("service-principal").add_subparsers(dest="action", required=True)
    for action in ("register", "show", "status", "alias-add", "alias-remove"):
        p = actions.add_parser(action)
        p.add_argument("--json", action="store_true")
        p.add_argument("--org", required=True, help="Exact organization ID; must match the selected human tenant")
        if action != "register":
            p.add_argument("id", help="Exact canonical principal ID")
        if action != "show":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--yes", action="store_true")
        if action in {"alias-add", "alias-remove", "status"}:
            p.add_argument("--expected-revision", help="Revision from show or dry-run")
        if action in {"register", "alias-add"}:
            p.add_argument("--alias-source", choices=SOURCES, required=True)
            p.add_argument("--alias-id", required=True)
        if action == "register":
            p.add_argument("--name", required=True)
            p.add_argument("--operation-id", required=True, help="Stable UUID; retries reconcile the original registration")
        if action == "status":
            p.add_argument("--status", choices=("active", "suspended", "retired"), required=True)
        if action == "alias-remove":
            p.add_argument("--alias-row-id", required=True, help="Exact alias row ID from show")
    accounts = areas.add_parser("service-account").add_subparsers(dest="action", required=True)
    for action in ("list", "show", "create", "update", "delete"):
        p = accounts.add_parser(action)
        p.add_argument("--org", required=True)
        p.add_argument("--identity-type", choices=("sql-iam",), required=True)
        p.add_argument("--json", action="store_true")
        if action in {"show", "update", "delete"}:
            p.add_argument("id")
        if action == "list":
            p.add_argument("--page", type=int, default=1)
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20, metavar="1..100")
        if action in {"create", "update", "delete"}:
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
        if action in {"update", "delete"}:
            p.add_argument("--expected-revision")
        if action == "create":
            p.add_argument("--operation-id", required=True)
        if action in {"create", "update"}:
            for field in ("name", "department", "team", "role-arn"):
                p.add_argument("--" + field, required=action == "create")
    agents = areas.add_parser("agent").add_subparsers(dest="action", required=True)
    for action in ("list", "show", "register", "update", "deregister"):
        p = agents.add_parser(action)
        p.add_argument("--org", required=True)
        p.add_argument("--identity-type", choices=("iam-registry", "cognito-client"), required=True)
        p.add_argument("--json", action="store_true")
        if action in {"show", "update", "deregister"}:
            p.add_argument("id")
        if action == "list":
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20, metavar="1..100")
            p.add_argument("--cursor")
        if action in {"register", "update", "deregister"}:
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
        if action in {"update", "deregister"}:
            p.add_argument("--expected-revision")
        if action in {"register", "deregister"}:
            p.add_argument("--operation-id", required=True)
        if action in {"register", "update"}:
            p.add_argument("--spec-file", required=True, help="JSON metadata matching the documented identity type")
        if action == "register":
            p.add_argument("--credential-file", help="Required private new output file for Cognito credentials; never printed")
    return root


def segment(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact identity ID.", "usage_error", 1)
    return quote(value, safe="")


def snapshot(client, org, target):
    value = client.request("GET", BASE + "/" + segment(target) + "/identity")
    if (
        not isinstance(value, dict)
        or value.get("tenant_id") != org
        or value.get("canonical_service_principal_id") != target
        or value.get("status") not in {"active", "suspended", "retired"}
        or not isinstance(value.get("revision"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["revision"])
        or not isinstance(value.get("aliases"), list)
        or len(value["aliases"]) > 1000
    ):
        raise common.CliError("Principal snapshot is malformed or belongs to another tenant.", "invalid_response", 5)
    aliases = []
    for row in value["aliases"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row.get("alias_source") not in SOURCES
            or not isinstance(row.get("alias_id"), str)
            or not isinstance(row.get("is_active"), bool)
        ):
            raise common.CliError("Principal aliases are malformed.", "invalid_response", 5)
        aliases.append({k: row[k] for k in ("id", "alias_source", "alias_id", "is_active")})
    if len({row["id"] for row in aliases}) != len(aliases):
        raise common.CliError("Principal returned duplicate alias IDs.", "invalid_response", 5)
    return {
        **{k: value[k] for k in ("tenant_id", "canonical_service_principal_id", "status", "revision")},
        "display_name": value.get("display_name"),
        "aliases": aliases,
        "effect": "Canonical resolution refuses inactive principals and revoked aliases; existing runs are not terminated.",
    }


def execute(args, client):
    segment(args.org)
    auth = client.request("GET", "/auth/cli/admin-session")
    if not isinstance(auth, dict) or auth.get("org_id") != args.org:
        raise common.CliError("Select the requested tenant before administering machine identities.", "tenant_mismatch", 3)
    contract = client.request("GET", BASE + "/registration-contract")
    if not isinstance(contract, dict) or contract.get("version") != "1.0":
        raise common.CliError("Gateway lacks guarded machine identity operations.", "unsupported_operation", 4)
    if args.area == "agent":
        return agent_command(args, client)
    if args.area == "service-account":
        return account_command(args, client)
    command = f"admin {args.area} {args.action}"
    if args.action == "show":
        return common.envelope("ok", command, snapshot(client, args.org, args.id))
    if not args.yes and not args.dry_run:
        raise common.CliError("Inspect --dry-run, then pass --yes with the reviewed revision.", "confirmation_required", 1)
    body = {}
    path = BASE
    method = "POST"
    if args.action == "register":
        try:
            operation = str(uuid.UUID(args.operation_id))
        except ValueError:
            raise common.CliError("--operation-id must be a UUID.", "usage_error", 1) from None
        if not 1 <= len(args.name) <= 255 or not 1 <= len(args.alias_id) <= 255:
            raise common.CliError("Name and alias must contain 1 to 255 characters.", "usage_error", 1)
        body = {"display_name": args.name, "alias_source": args.alias_source, "alias_id": args.alias_id, "operation_id": operation}
        path += "/register"
        before = None
    else:
        before = snapshot(client, args.org, args.id)
        path += "/" + segment(args.id)
        if args.action == "status":
            method = "PATCH"
            path += "/status"
            body = {"status": args.status}
        elif args.action == "alias-add":
            path += "/aliases"
            body = {"alias_source": args.alias_source, "alias_id": args.alias_id}
        else:
            method = "DELETE"
            if not any(row["id"] == args.alias_row_id and row["is_active"] for row in before["aliases"]):
                raise common.CliError("Active alias row is absent from this principal.", "not_found", 5)
            path += "/aliases/" + segment(args.alias_row_id)
        if not args.dry_run:
            if args.expected_revision != before["revision"]:
                raise common.CliError("Pass the current --expected-revision after reviewing show/dry-run.", "revision_conflict", 4)
            if method == "DELETE":
                path += "?" + urlencode({"expected_revision": args.expected_revision})
            else:
                body["expected_revision"] = args.expected_revision
    if args.dry_run:
        return common.envelope(
            "dry_run", command, {"before": before, "changes": body, "credential_delivery": "none; canonical registration mints no credential"}
        )
    common.ensure_can_mutate("machine.principal.manage", request=client.request)
    acknowledged = False
    try:
        response = client.request(method, path, body if method != "DELETE" else None)
        acknowledged = True
        if not isinstance(response, dict) or not isinstance(response.get("canonical_service_principal_id"), str):
            raise ValueError("Malformed acknowledgement")
        target = response["canonical_service_principal_id"]
        if args.action != "register" and target != args.id:
            raise ValueError("Wrong principal acknowledgement")
        after = snapshot(client, args.org, target)
        if args.action == "status" and after["status"] != args.status:
            raise ValueError("Status differs")
        if args.action in {"register", "alias-add"} and not any(
            row["alias_source"] == args.alias_source and row["alias_id"] == args.alias_id and row["is_active"] for row in after["aliases"]
        ):
            raise ValueError("Alias not active")
        if args.action == "alias-remove" and not any(row["id"] == args.alias_row_id and not row["is_active"] for row in after["aliases"]):
            raise ValueError("Alias not revoked")
    except common.CliError as exc:
        if not acknowledged and exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code not in {408, 429}:
            raise
        return common.envelope(
            "pending",
            command,
            {
                "operation_id": getattr(args, "operation_id", None),
                "target": getattr(args, "id", None),
                "message": (
                    "Outcome uncertain. Inspect the original principal or repeat registration with the same operation ID "
                    "and inputs; do not invent a new identity."
                ),
            },
        )
    except (OSError, ValueError, TypeError, KeyError):
        return common.envelope(
            "pending",
            command,
            {
                "operation_id": getattr(args, "operation_id", None),
                "target": getattr(args, "id", None),
                "message": "Acknowledgement or readback is incomplete; reconcile the same identity and operation.",
            },
        )
    return common.envelope("ok", command, after)


def account_snapshot(client, base, org, target):
    value = client.request("GET", base + "/" + segment(target) + "/identity")
    if (
        not isinstance(value, dict)
        or value.get("org_id") != org
        or value.get("id") != target
        or value.get("identity_type") != "sql-iam"
        or not isinstance(value.get("revision"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["revision"])
    ):
        raise common.CliError("Invalid service-account snapshot.", "invalid_response", 5)
    return {
        key: value.get(key)
        for key in ("id", "org_id", "name", "department_id", "team_id", "iam_role_arn", "description", "revision", "identity_type")
    }


def account_command(args, client):
    base = "/admin/organizations/" + segment(args.org) + "/service-accounts"
    command = "admin service-account " + args.action
    if args.action == "list":
        if args.page < 1:
            raise common.CliError("--page must be positive.", "usage_error", 1)
        value = client.request("GET", base + "?" + urlencode({"page": args.page, "page_size": args.page_size}))
        if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > args.page_size:
            raise common.CliError("Invalid service-account page.", "invalid_response", 5)
        items = []
        for row in value["items"]:
            if not isinstance(row, dict) or row.get("org_id") != args.org or not isinstance(row.get("id"), str):
                raise common.CliError("Service-account page lost tenant scope.", "invalid_response", 5)
            items.append({key: row.get(key) for key in ("id", "org_id", "name", "department_id", "team_id", "iam_role_arn", "description")})
        return common.envelope(
            "ok",
            command,
            {
                "identity_type": "sql-iam",
                "items": items,
                "page": args.page,
                "has_more": value.get("has_more"),
                "next_page": args.page + 1 if value.get("has_more") else None,
            },
        )
    if args.action == "show":
        return common.envelope("ok", command, account_snapshot(client, base, args.org, args.id))
    if not args.dry_run and not args.yes:
        raise common.CliError("Inspect --dry-run then confirm with --yes.", "confirmation_required", 1)
    changes = {}
    if args.action in {"create", "update"}:
        for field, wire in (("name", "name"), ("department", "department_id"), ("team", "team_id"), ("role_arn", "iam_role_arn")):
            value = getattr(args, field)
            if value is not None:
                changes[wire] = value
        if not changes:
            raise common.CliError("Choose a metadata field to update.", "usage_error", 1)
    before = account_snapshot(client, base, args.org, args.id) if args.action != "create" else None
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            {
                "before": before,
                "changes": changes,
                "effect": "SQL IAM registration only; no credential is created. Deletion prevents future resolution, without stopping existing runs.",
            },
        )
    if before and args.expected_revision != before["revision"]:
        raise common.CliError("Review and supply the current --expected-revision.", "revision_conflict", 4)
    if args.action == "create":
        try:
            operation = str(uuid.UUID(args.operation_id))
        except ValueError:
            raise common.CliError("--operation-id must be a UUID.", "usage_error", 1) from None
        method, path, body = "POST", base + "/register", {"operation_id": operation, "account": changes}
    elif args.action == "update":
        method, path, body = "PATCH", base + "/" + segment(args.id) + "/identity", {"expected_revision": args.expected_revision, "account": changes}
    else:
        method, path, body = "DELETE", base + "/" + segment(args.id) + "/identity?" + urlencode({"expected_revision": args.expected_revision}), None
    common.ensure_can_mutate("machine.account.manage", request=client.request)
    acknowledged = False
    try:
        response = client.request(method, path, body)
        acknowledged = True
        if not isinstance(response, dict) or response.get("org_id") != args.org or not isinstance(response.get("id"), str):
            raise ValueError("Invalid acknowledgement")
        target = response["id"]
        if before and target != args.id:
            raise ValueError("Wrong target")
        if args.action == "delete":
            if response.get("deleted") is not True:
                raise ValueError("Incomplete deletion")
            try:
                account_snapshot(client, base, args.org, target)
            except common.CliError as exc:
                if exc.status_code != 404:
                    raise
            else:
                raise ValueError("Target still visible")
            return common.envelope("ok", command, {"id": target, "org_id": args.org, "deleted": True})
        after = account_snapshot(client, base, args.org, target)
        if any(after.get(key) != value for key, value in changes.items()):
            raise ValueError("Readback differs")
        return common.envelope("ok", command, after)
    except common.CliError as exc:
        if (
            not acknowledged
            and exc.status_code
            and 400 <= exc.status_code < 500
            and exc.status_code not in {408, 429}
            and exc.code != "registration_pending"
        ):
            raise
    except (OSError, ValueError, TypeError):
        pass
    return common.envelope(
        "pending",
        command,
        {
            "operation_id": getattr(args, "operation_id", None),
            "target": getattr(args, "id", None),
            "message": "Outcome uncertain; reconcile this operation and exact identity before retrying.",
        },
    )


AGENT_FIELDS = (
    "id",
    "agent_id",
    "client_id",
    "identity_type",
    "revision",
    "org_id",
    "name",
    "agent_name",
    "role_arn",
    "team_id",
    "department_id",
    "owner",
    "scope",
    "description",
    "status",
    "scopes",
    "allowed_models",
    "budget_config_id",
    "image_uri",
    "code_repo",
    "workflow_name",
    "created_at",
    "updated_at",
)


def agent_snapshot(client, kind, org, target):
    value = client.request("GET", "/admin/machine-agents/" + kind + "/" + segment(target) + "/identity?" + urlencode({"org_id": org}))
    if (
        not isinstance(value, dict)
        or value.get("id") != target
        or value.get("org_id") != org
        or value.get("identity_type") != kind
        or not isinstance(value.get("revision"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["revision"])
    ):
        raise common.CliError("Invalid agent identity snapshot.", "invalid_response", 5)
    return {key: value[key] for key in AGENT_FIELDS if key in value}


def credential_output(path):
    if not path:
        raise common.CliError("Cognito registration requires --credential-file in a private directory.", "unsafe_file", 1)
    target = Path(path).absolute()
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise common.CliError("Credential output directory must be owned by you and private (0700).", "unsafe_file", 1)
        # Hold the checked directory inode across creation; never follow a
        # swapped parent or leaf symlink, and never clobber an existing file.
        return os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    finally:
        os.close(directory)


def agent_command(args, client):
    kind = args.identity_type
    command = "admin agent " + args.action
    if args.action == "show":
        return common.envelope("ok", command, agent_snapshot(client, kind, args.org, args.id))
    if args.action == "list":
        params = {"org_id": args.org, "page_size": args.page_size}
        path = "/admin/registry/agents" if kind == "iam-registry" else "/admin/machine-agents/cognito-client/page"
        if args.cursor:
            params["last_key" if kind == "iam-registry" else "cursor"] = args.cursor
        value = client.request("GET", path + "?" + urlencode(params))
        if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > args.page_size:
            raise common.CliError("Invalid agent page.", "invalid_response", 5)
        items = []
        for row in value["items"]:
            if not isinstance(row, dict) or row.get("org_id") != args.org:
                raise common.CliError("Agent page lost tenant scope.", "invalid_response", 5)
            items.append({key: row[key] for key in AGENT_FIELDS if key in row})
        return common.envelope(
            "ok", command, {"identity_type": kind, "items": items, "next_cursor": value.get("last_key" if kind == "iam-registry" else "next_cursor")}
        )
    if not args.yes and not args.dry_run:
        raise common.CliError("Inspect --dry-run then confirm with --yes.", "confirmation_required", 1)
    spec = {}
    if args.action in {"register", "update"}:
        with open(args.spec_file, encoding="utf-8") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise common.CliError("Metadata specification is too large.", "usage_error", 1)
        spec = json.loads(raw)
        allowed = (
            {"name", "team_id", "department_id", "description", "scopes"}
            if kind == "cognito-client"
            else {
                "agent_name",
                "role_arn",
                "team_id",
                "owner",
                "scope",
                "description",
                "budget_config_id",
                "allowed_models",
                "image_uri",
                "code_repo",
                "workflow_name",
            }
        )
        if args.action == "update":
            allowed.add("status")
        if not isinstance(spec, dict) or not spec or set(spec) - allowed:
            raise common.CliError("Use documented metadata fields only; never place credentials in --spec-file.", "usage_error", 1)
    operation = None
    if args.action in {"register", "deregister"}:
        try:
            operation = str(uuid.UUID(args.operation_id))
        except ValueError:
            raise common.CliError("--operation-id must be a UUID.", "usage_error", 1) from None
    before = agent_snapshot(client, kind, args.org, args.id) if args.action != "register" else None
    effect = (
        "IAM disabled status prevents future authorizer checks; existing/cached authorization and runs are not terminated."
        if kind == "iam-registry"
        else "Cognito retirement deletes the client and prevents new token minting; existing JWTs expire normally and runs are not stopped."
    )
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            {
                "before": before,
                "changes": spec,
                "operation_id": operation,
                "effect": effect,
                "credential_delivery": "private output file" if kind == "cognito-client" and args.action == "register" else "none",
            },
        )
    resuming = bool(before and kind == "cognito-client" and args.action == "deregister" and before.get("status") in {"retiring", "retired"})
    if before and not resuming and args.expected_revision != before["revision"]:
        raise common.CliError("Review and supply the current --expected-revision.", "revision_conflict", 4)
    body = {"org_id": args.org, "agent": spec}
    path = "/admin/machine-agents/" + kind
    if args.action == "register":
        method, path = "POST", path + "/register"
        body["operation_id"] = operation
    else:
        method, path = "PATCH", path + "/" + segment(args.id) + "/identity"
        body.update(expected_revision=before["revision"], deregister=args.action == "deregister", operation_id=operation)
    common.ensure_can_mutate("machine.agent.registry.manage" if kind == "iam-registry" else "machine.agent.cognito.manage", request=client.request)
    fd = credential_output(args.credential_file) if kind == "cognito-client" and args.action == "register" else None
    target = getattr(args, "id", None)
    acknowledged = False
    try:
        result = client.request(method, path, body)
        acknowledged = True
        if not isinstance(result, dict) or result.get("org_id") != args.org or not isinstance(result.get("id"), str):
            raise ValueError("Invalid acknowledgement")
        if target and result["id"] != target:
            raise ValueError("Wrong identity")
        target = result["id"]
        after = agent_snapshot(client, kind, args.org, target)
        if any(after.get(key) != value for key, value in spec.items()):
            raise ValueError("Readback differs")
        if args.action == "deregister" and after.get("status") != ("disabled" if kind == "iam-registry" else "retired"):
            raise ValueError("Retirement incomplete")
        if fd is not None:
            if after.get("status") != "active":
                raise ValueError("Registered client is no longer active")
            secret = client.request("GET", "/admin/agents/" + segment(target) + "/credentials")
            if (
                not isinstance(secret, dict)
                or secret.get("client_id") != target
                or not isinstance(secret.get("client_secret"), str)
                or not secret["client_secret"]
            ):
                raise ValueError("Credential delivery incomplete")
            delivered = {key: secret[key] for key in ("client_id", "client_secret", "token_endpoint", "scopes") if key in secret}
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                fd = None
                json.dump(delivered, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            after["credential_delivery"] = "written to requested private file"
        after["effect"] = effect
        return common.envelope("ok", command, after)
    except common.CliError as exc:
        if (
            not acknowledged
            and exc.status_code
            and 400 <= exc.status_code < 500
            and exc.status_code not in {408, 429}
            and exc.code != "registration_pending"
        ):
            raise
    except (OSError, ValueError, TypeError, KeyError):
        pass
    finally:
        if fd is not None:
            os.close(fd)
    return common.envelope(
        "pending",
        command,
        {
            "operation_id": operation,
            "target": target,
            "message": (
                "Outcome uncertain. Reconcile the same operation and identity; credential recovery uses the same registration "
                "and a new private output file, never a new operation."
            ),
        },
    )


class Client:
    def __init__(self):
        self.api = common.Api()
        self.token = common.access_token()

    def request(self, method, path, body=None, **kwargs):
        return self.api.request(method, path, body, token=self.token, timeout=30)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    try:
        result = execute(parser().parse_args(argv), Client())
        common.emit(result, as_json)
        return 4 if result["status"] == "pending" else 0
    except KeyboardInterrupt:
        common.emit(common.envelope("pending", "admin machine", {"message": "Interrupted; inspect the original identity before retrying."}), as_json)
        return 130
    except (common.CliError, OSError, ValueError, TypeError) as exc:
        return common.report_error(exc, "admin machine", as_json)


if __name__ == "__main__":
    sys.exit(main())
