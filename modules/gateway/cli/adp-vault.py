#!/usr/bin/env python3
"""Credential metadata and external identity claims through the existing vault API."""

from __future__ import annotations

import getpass
import http.client
import os
import stat
import sys
import uuid
import warnings
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

TYPES = ("api_key", "oauth_token", "basic_auth", "bearer", "ssh_key", "certificate", "config_file", "aws_role")
FIELDS = ("id", "service", "label", "credential_type", "scope", "expires_at", "last_used_at", "strict", "created_at", "updated_at")
IDENTITY_FIELDS = ("id", "provider", "provider_user_id", "provider_username", "verification_method", "verified_at", "created_at")


def parser():
    root = common.Parser(prog="adp credential|identity", description=__doc__)
    areas = root.add_subparsers(dest="area", required=True)
    for area, actions in (("credential", ("list", "show", "add", "update", "delete")), ("identity", ("list", "link", "unlink"))):
        group = areas.add_parser(area).add_subparsers(dest="action", required=True)
        for action in actions:
            p = group.add_parser(action)
            p.add_argument("--json", action="store_true")
            if action in {"show", "update", "delete", "unlink"}:
                p.add_argument("id")
            if action in {"add", "update", "delete", "link", "unlink"}:
                p.add_argument("--yes", action="store_true")
                p.add_argument("--dry-run", action="store_true")
            if area == "identity":
                p.add_argument("--provider", choices=("slack", "github", "whatsapp", "discord"), required=action == "link")
                if action == "link":
                    p.add_argument("--provider-user-id", required=True)
                    p.add_argument("--resume", action="store_true", help="Read verification of this claim without issuing another link request")
            if action == "list" and area == "credential":
                p.add_argument("--scope", choices=("user", "team", "org", "domain_app"))
            if action in {"add", "update"}:
                p.add_argument("--label", required=action == "add")
                p.add_argument("--expires-at")
                p.add_argument("--strict", choices=("true", "false"))
            if action == "add":
                p.add_argument("--operation-id", required=True, help="Stable UUID; retain for same-operation retry")
                p.add_argument("--service", required=True)
                p.add_argument("--type", choices=TYPES, required=True)
                p.add_argument("--scope", choices=("user", "team", "org", "domain_app"), default="user")
                p.add_argument("--domain-app-id")
                value = p.add_mutually_exclusive_group()
                value.add_argument("--value-stdin", action="store_true")
                value.add_argument("--value-file")
            if action == "update":
                p.add_argument("--expected-revision", help="updated_at (or created_at) from credential show")
    return root


def identifier(value):
    if not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact credential or identity ID.", "usage_error", 1)
    return quote(value, safe="")


def metadata(row, *, identity=False):
    fields = IDENTITY_FIELDS if identity else FIELDS
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise common.CliError("Vault returned malformed metadata.", "invalid_response", 5)
    # Never emit secret values, secret ARNs, arbitrary server fields or scopes blobs.
    result = {key: row[key] for key in fields if key in row}
    if not identity:
        if row.get("credential_type") not in TYPES or row.get("scope") not in {"user", "team", "org", "domain_app"}:
            raise common.CliError("Vault returned unsupported metadata.", "invalid_response", 5)
        result["revision"] = row.get("updated_at") or row.get("created_at")
        result["rotation"] = "unsupported; update changes metadata only"
    return result


def mutation_metadata(row, target):
    try:
        result = metadata(row)
        if result["id"] != target:
            raise ValueError
        return result
    except (common.CliError, ValueError):
        raise common.CliError(
            "Vault acknowledgement is incomplete; inspect the same operation ID before retrying.", "unknown_mutation_outcome", 4
        ) from None


def rows(client, area):
    value = client.request("GET", "/auth/" + ("credentials" if area == "credential" else "identities"))
    if not isinstance(value, list):
        raise common.CliError("Vault returned malformed list.", "invalid_response", 5)
    return [metadata(row, identity=area == "identity") for row in value]


def selected(client, area, target):
    identifier(target)
    matches = [row for row in rows(client, area) if row["id"] == target]
    if len(matches) != 1:
        raise common.CliError("Target is absent or not visible to this login.", "not_found", 5, status_code=404)
    return matches[0]


def read_value(args):
    limit = 1024 * 1024
    if args.value_file:
        fd = os.open(args.value_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise common.CliError("Secret file must be owned by you, regular, and private (0600).", "unsafe_file", 1)
            value = source.read(limit + 1).decode("utf-8")
    elif args.value_stdin:
        value = sys.stdin.read(limit + 1)
    else:
        if not sys.stdin.isatty():
            raise common.CliError("Use --value-stdin or a private --value-file.", "usage_error", 1)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass("Credential value (hidden): ")
            except getpass.GetPassWarning:
                raise common.CliError("Cannot hide input; use protected stdin or file.", "unsafe_input", 1) from None
    if not value or len(value.encode("utf-8")) > limit:
        raise common.CliError("Secret must contain 1 to 1048576 UTF-8 bytes.", "usage_error", 1)
    return value


def confirm(args):
    if not args.yes and not args.dry_run:
        raise common.CliError("Inspect --dry-run, then pass --yes to apply this exact change.", "confirmation_required", 1)


def mutate(client, method, path, body=None):
    operations = {
        ("credential", "PUT"): "vault.credentials.register",
        ("credential", "PATCH"): "vault.credentials.metadata",
        ("credential", "DELETE"): "vault.credentials.delete",
        ("identity", "POST"): "vault.identities.claim",
        ("identity", "DELETE"): "vault.identities.unlink",
    }
    area = "credential" if path.startswith("/auth/credentials/") else "identity"
    common.ensure_can_mutate(operations[(area, method)], request=client.request)
    try:
        return client.request(method, path, body, timeout=30)
    except common.CliError:
        raise
    except (http.client.HTTPException, OSError, ValueError):
        raise common.CliError(
            "Vault did not acknowledge this operation; inspect metadata before retrying the same ID.", "unknown_mutation_outcome", 4
        ) from None


def execute(args, client):
    command = f"adp {args.area} {args.action}"
    if args.action == "list":
        items = rows(client, args.area)
        key = "scope" if args.area == "credential" else "provider"
        choice = getattr(args, key, None)
        return common.envelope(
            "ok",
            command,
            {
                "items": [r for r in items if not choice or r.get(key) == choice],
                "complete": True,
                "pagination": "server returns an unpaginated visible list",
            },
        )
    if args.action == "show":
        return common.envelope("ok", command, selected(client, args.area, args.id))
    if args.action == "link" and args.resume:
        matches = [r for r in rows(client, "identity") if r.get("provider") == args.provider and r.get("provider_user_id") == args.provider_user_id]
        if len(matches) != 1:
            raise common.CliError("Identity claim is absent or not visible.", "not_found", 5)
        row = matches[0]
        verified = row.get("verification_method") in {"oauth", "org_placement", "admin_attested", "magic_link_confirmed"} and bool(
            row.get("verified_at")
        )
        return common.envelope("ok" if verified else "pending", command, row)
    confirm(args)
    if args.action == "add":
        try:
            operation = str(uuid.UUID(args.operation_id))
        except ValueError:
            raise common.CliError("--operation-id must be a UUID.", "usage_error", 1) from None
        body = {"service": args.service, "label": args.label, "credential_type": args.type, "scope_hint": args.scope, "strict": args.strict == "true"}
        if args.domain_app_id:
            body["domain_app_id"] = args.domain_app_id
        if args.scope == "domain_app" and not args.domain_app_id:
            raise common.CliError("Domain scope requires --domain-app-id.", "usage_error", 1)
        if args.expires_at:
            body["expires_at"] = args.expires_at
        if args.dry_run:
            return common.envelope("dry_run", command, {"operation_id": operation, "metadata": body, "secret": "read only when confirmed"})
        body["value"] = read_value(args)
        result = mutate(client, "PUT", "/auth/credentials/" + operation, body)
        return common.envelope("ok", command, mutation_metadata(result, operation))
    if args.action == "update":
        before = selected(client, "credential", args.id)
        body = {key: getattr(args, key) for key in ("label", "expires_at") if getattr(args, key) is not None}
        if args.strict is not None:
            body["strict"] = args.strict == "true"
        if not body:
            raise common.CliError("Choose --label, --expires-at or --strict. Secret rotation is unsupported.", "unsupported_operation", 1)
        if args.dry_run:
            return common.envelope("dry_run", command, {"before": before, "changes": body, "expected_revision": before["revision"]})
        if not args.expected_revision:
            raise common.CliError("Pass --expected-revision from credential show/dry-run.", "usage_error", 1)
        body["expected_revision"] = args.expected_revision
        return common.envelope(
            "ok", command, mutation_metadata(mutate(client, "PATCH", "/auth/credentials/" + identifier(args.id) + "/metadata", body), args.id)
        )
    if args.action in {"delete", "unlink"}:
        before = selected(client, args.area, args.id)
        if args.area == "identity" and args.provider and before.get("provider") != args.provider:
            raise common.CliError("Identity does not match the requested provider.", "not_found", 5)
        effect = (
            "Credential removed; running jobs are not stopped. Dependents are not enumerated by this API; AWS secret recovery window applies."
            if args.area == "credential"
            else "This identity link is removed; unrelated identities and login sessions are unchanged."
        )
        if args.dry_run:
            return common.envelope("dry_run", command, {"target": before, "effect": effect})
        path = "/auth/" + ("credentials" if args.area == "credential" else "identities") + "/" + identifier(args.id)
        mutate(client, "DELETE", path)
        return common.envelope("ok", command, {"id": args.id, "effect": effect})
    if args.action == "link":
        if args.dry_run:
            return common.envelope(
                "dry_run",
                command,
                {
                    "provider": args.provider,
                    "provider_user_id": args.provider_user_id,
                    "effect": "Record an unverified claim; this does not establish provider consent or access.",
                },
            )
        row = mutate(client, "POST", "/auth/identities/" + args.provider + "/link", {"provider_user_id": args.provider_user_id})
        if not isinstance(row, dict) or row.get("provider") != args.provider or row.get("provider_user_id") != args.provider_user_id:
            raise common.CliError(
                "Identity acknowledgement is incomplete; use link --resume to inspect this claim before retrying.", "unknown_mutation_outcome", 4
            )
        result = {
            key: row.get(key) for key in ("status", "provider", "provider_user_id", "verification_method", "verified_at", "identity_id", "next_step")
        }
        # A claim acknowledgement is never provider-consent evidence.
        return common.envelope("pending", command, result)
    raise common.CliError("Unsupported vault command.", "usage_error", 1)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    try:
        try:
            args = parser().parse_args(argv)
        except common.CliError:
            # An unknown argv value could contain a mistakenly supplied secret.
            raise common.CliError("Invalid vault arguments; use --help. Secret values must use stdin or a private file.", "usage_error", 1) from None
        result = execute(args, common.Api())
        common.emit(result, as_json)
        return 4 if result["status"] == "pending" else 0
    except KeyboardInterrupt:
        common.emit(
            common.envelope(
                "pending",
                "adp vault",
                {
                    "outcome": "unknown",
                    "message": (
                        "Interrupted. Inspect the same credential operation or identity claim before retrying; interruption does not undo a request."
                    ),
                },
            ),
            as_json,
        )
        return 130
    except common.CliError as exc:
        result = common.envelope("failed", "adp vault")
        # Do not echo server text or input paths into logs of secret operations.
        result["error"] = {
            "code": exc.code,
            "http_status": exc.status_code,
            "message": str(exc) if exc.status_code is None else "Vault request refused; inspect status and permissions.",
        }
        common.emit(result, as_json)
        return exc.exit_code
    except (OSError, UnicodeError, ValueError, TypeError, http.client.HTTPException):
        common.emit(
            common.envelope(
                "failed", "adp vault", {"error": "Vault operation failed; inspect metadata before retrying. Secret input is not echoed."}
            ),
            as_json,
        )
        return 5


if __name__ == "__main__":
    sys.exit(main())
