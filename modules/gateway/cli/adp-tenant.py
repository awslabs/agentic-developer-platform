#!/usr/bin/env python3
"""Resolve authorized ADP tenants without changing global workspace selection."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

PIN_KEYS = ("ADP_TENANT_ID", "ADP_TENANT_SUB", "ADP_TENANT_SOURCE", "ADP_TENANT_MODE", "ADP_TENANT_MEMBERSHIP")


def raw_token():
    env = dict(os.environ)
    for key in PIN_KEYS:
        env.pop(key, None)
    resolved = common.deployment()
    if resolved:
        env.update(resolved.environment())
    try:
        token = subprocess.run(
            ["bash", str(Path(__file__).with_name("bg-cognito-auth.sh")), "token"], env=env, text=True, capture_output=True, timeout=120, check=True
        ).stdout.strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError
        return token
    except (OSError, subprocess.SubprocessError, ValueError):
        raise common.CliError("Sign in with adp login first.", "authentication_required", 2) from None


def subject(token):
    value = common._jwt_claims(token).get("sub")
    if not isinstance(value, str) or not value:
        raise common.CliError("The login has no stable human subject.", "authentication_required", 2)
    return value


def default_path(token):
    claims = common._jwt_claims(token)
    identity = hashlib.sha256(json.dumps([common.gateway_url(), claims.get("iss"), subject(token)]).encode()).hexdigest()
    return common.config_path().parent / ("tenant-default-" + identity + ".json")


def workspaces(token, client):
    data = client.request("GET", "/workspaces", token=token)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise common.CliError("Malformed membership response.", "invalid_response", 5)
    for item in data["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("org_id"), str) or not item["org_id"] or not isinstance(item.get("name"), str):
            raise common.CliError("Malformed visible membership.", "invalid_response", 5)
    return data["items"]


def exchange(token, tenant_id, client, expected=None):
    body = {"org_id": tenant_id}
    if expected:
        body["expected_membership"] = expected
    result = client.request("POST", "/workspaces/context", body, token=token)
    if (
        not isinstance(result, dict)
        or result.get("tenant_id") != tenant_id
        or result.get("identity") != subject(token)
        or not isinstance(result.get("context_token"), str)
        or not isinstance(result.get("membership_id"), str)
    ):
        raise common.CliError("Invalid tenant lease response.", "invalid_response", 5)
    return result


def resolve(token, client, selection=None):
    selection = selection or os.environ.get("ADP_TENANT_FLAG")
    identity = subject(token)
    inherited = os.environ.get("ADP_TENANT_ID")
    if inherited and not selection:
        if os.environ.get("ADP_TENANT_SUB") != identity:
            raise common.CliError("Login changed; start a new command after selecting its tenant.", "tenant_identity_changed", 4)
        return {key: os.environ.get(key, "") for key in PIN_KEYS}
    selected = selection or os.environ.get("ADP_TENANT")
    source = "flag" if selection else "environment" if selected else ""
    if not selected and default_path(token).exists():
        saved = common.read_private_json(default_path(token))
        selected = saved.get("tenant_id")
        source = "saved_default"
        if not isinstance(selected, str) or not selected:
            raise common.CliError("Invalid tenant default. Use adp tenant use TENANT_ID.", "invalid_response", 5)
    try:
        items = workspaces(token, client)
    except common.CliError as exc:
        if exc.status_code != 404 or selected:
            raise
        # Old server: preserve only its signed single-tenant claim. Never honor
        # an explicit selector without a server-authorized exchange.
        claims = common._jwt_claims(token)
        tenant = claims.get("org_id") or claims.get("custom:org_id") or ""
        if not tenant:
            raise common.CliError(
                "Legacy gateway supplied no unambiguous tenant. Upgrade the gateway or sign in again.", "tenant_selection_required", 4
            )
        return dict(zip(PIN_KEYS, [tenant, identity, "legacy_claim", "legacy", ""]))
    if selected:
        matches = [item for item in items if item["org_id"] == selected]
        if not matches:
            matches = [item for item in items if item["name"] == selected]
        if len(matches) != 1:
            raise common.CliError(
                "Tenant is unavailable or its visible name is ambiguous. Use adp tenant list and an exact ID.", "tenant_selection_required", 4
            )
        selected = matches[0]["org_id"]
    elif len(items) == 1:
        selected, source = items[0]["org_id"], "single_membership"
    else:
        raise common.CliError("Select one visible tenant with --tenant, ADP_TENANT or adp tenant use TENANT_ID.", "tenant_selection_required", 4)
    lease = exchange(token, selected, client)
    return dict(zip(PIN_KEYS, [selected, identity, source, "scoped", lease["membership_id"]]))


def wrap(token, client):
    if subject(token) != os.environ.get("ADP_TENANT_SUB"):
        raise common.CliError("Login changed during this command; refusing to reuse its tenant.", "tenant_identity_changed", 4)
    tenant = os.environ["ADP_TENANT_ID"]
    if os.environ.get("ADP_TENANT_MODE") == "legacy":
        claims = common._jwt_claims(token)
        if (claims.get("org_id") or claims.get("custom:org_id") or "") != tenant:
            raise common.CliError("Legacy login tenant changed; restart the command.", "tenant_identity_changed", 4)
        return token
    lease = exchange(token, tenant, client, os.environ.get("ADP_TENANT_MEMBERSHIP"))
    return "adpctx1~" + lease["context_token"] + "~" + token


def parser():
    root = common.Parser(prog="adp tenant")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("list", "current", "use"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        if name == "use":
            p.add_argument("tenant", help="Authorized tenant ID or unique visible tenant name.")
            p.add_argument("--dry-run", action="store_true")
    return root


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "--wrap-token":
            print(wrap(sys.stdin.read(32768).strip(), common.Api()), end="")
            return 0
        if argv and argv[0] == "--resolve-env":
            pin = resolve(raw_token(), common.Api(), argv[1] if len(argv) > 1 else None)
            for key, value in pin.items():
                print("export " + key + "=" + shlex.quote(value))
            return 0
        args = parser().parse_args(argv)
        token, client = raw_token(), common.Api()
        if args.command == "list":
            return common.emit(common.envelope("ok", "adp tenant list", {"items": workspaces(token, client)}), args.json)
        pin = resolve(token, client, args.tenant if args.command == "use" else None)
        detail = {
            "tenant_id": pin["ADP_TENANT_ID"],
            "selection_source": pin["ADP_TENANT_SOURCE"],
            "identity": pin["ADP_TENANT_SUB"],
            "mode": pin["ADP_TENANT_MODE"],
        }
        if args.command == "use" and not args.dry_run:
            common.write_json(default_path(token), {"tenant_id": pin["ADP_TENANT_ID"]})
            detail["selection_source"] = "saved_default"
        return common.emit(common.envelope("dry_run" if getattr(args, "dry_run", False) else "ok", "adp tenant " + args.command, detail), args.json)
    except (common.CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, "adp tenant", "--json" in argv)


if __name__ == "__main__":
    sys.exit(main())
