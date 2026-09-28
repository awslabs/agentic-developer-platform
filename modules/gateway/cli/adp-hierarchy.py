#!/usr/bin/env python3
"""Scoped hierarchy and membership administration over protected ADP APIs."""

from __future__ import annotations

import http.client
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

ROLES = ("member", "dept_admin", "org_admin", "platform_admin")


def parser():
    root = common.Parser(prog="adp admin")
    areas = root.add_subparsers(dest="area", required=True)
    for area in ("org", "department", "team", "member", "tenant"):
        parent = areas.add_parser(area)
        actions = parent.add_subparsers(dest="action", required=True)
        if area == "tenant":
            parent = actions.add_parser("org-links")
            actions = parent.add_subparsers(dest="action", required=True)
            forms = ("list", "add", "remove")
        else:
            forms = ("list", "add", "update", "remove") if area == "member" else ("list", "show", "create", "update", "delete")
        for action in forms:
            p = actions.add_parser(action)
            p.add_argument("--json", action="store_true")
            if area == "tenant":
                p.add_argument("--tenant", required=True)
                if action != "list":
                    p.add_argument("--github-org-id", required=True)
            elif area != "org" or action not in {"list", "create"}:
                p.add_argument("--org", required=True)
            if action == "list":
                pages(p)
                if area in {"team", "member"}:
                    p.add_argument("--department")
            if area in {"org", "department", "team"}:
                if action == "create" or (action in {"show", "update", "delete"} and area != "org"):
                    p.add_argument("--id", required=True, help="Canonical ID; create IDs must be stable across reconciliation")
                if action in {"create", "update"}:
                    p.add_argument("--name", required=action == "create")
                    if area != "org":
                        p.add_argument("--description")
                if area == "team" and action == "create":
                    p.add_argument("--department", required=True)
            if area == "member":
                if action == "add":
                    source = p.add_mutually_exclusive_group(required=True)
                    source.add_argument("--existing-user")
                    source.add_argument("--new-user", action="store_true")
                    p.add_argument("--email")
                    p.add_argument("--name")
                    p.add_argument("--team")
                    p.add_argument("--role", choices=ROLES, default="member")
                elif action in {"update", "remove"}:
                    p.add_argument("--user", required=True)
                    if action == "update":
                        p.add_argument("--role", choices=ROLES)
                        p.add_argument("--name")
            if action not in {"list", "show"}:
                mutation_flags(p, revision=action in {"update", "delete", "remove"} or area == "tenant")
        if area == "team":
            members = actions.add_parser("members").add_subparsers(dest="member_action", required=True)
            for action in ("list", "add", "remove"):
                p = members.add_parser(action)
                p.add_argument("--org", required=True)
                p.add_argument("--team", required=True)
                p.add_argument("--json", action="store_true")
                if action == "list":
                    pages(p)
                else:
                    p.add_argument("--user", required=True)
                    if action == "add":
                        p.add_argument("--role", choices=("member", "lead"), default="member")
                        p.add_argument("--primary", action="store_true")
                    mutation_flags(p, revision=True)
    return root


def pages(p):
    p.add_argument("--page", type=int, choices=range(1, 10001), default=1, metavar="1..10000")
    p.add_argument("--page-size", type=int, choices=range(1, 101), default=20, metavar="1..100")
    p.add_argument("--max-pages", type=int, choices=range(1, 101), default=1, metavar="1..100")


def mutation_flags(p, revision):
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    if revision:
        p.add_argument("--expected-revision")


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact nonempty canonical ID.", "usage_error", 1)
    return quote(value, safe="")


class Client:
    def __init__(self):
        self.api = common.Api()
        self.token = common.access_token()

    def request(self, method, path, body=None):
        return self.api.request(method, path, body, token=self.token, timeout=30)


def checked(value, org, kind, key):
    if (
        not isinstance(value, dict)
        or value.get("org_id") != org
        or value.get("kind") != kind
        or value.get("id") != key
        or not isinstance(value.get("resource"), dict)
        or not re.fullmatch(r"[a-f0-9]{64}", value.get("revision", ""))
        or not isinstance(value.get("dependent_tables"), list)
        or value.get("cascade_supported") is not False
    ):
        raise common.CliError("Hierarchy readback scope or revision is malformed.", "invalid_response", 5)
    return value


def snapshot(client, org, kind, key):
    path = "/admin/organizations/" + identifier(org) + "/hierarchy/" + kind + "/" + identifier(key)
    return path, checked(client.request("GET", path), org, kind, key)


def list_rows(args, client, kind, team_members=False):
    if args.area == "org":
        path = "/admin/organizations"
    else:
        path = "/admin/organizations/" + identifier(args.org) + "/hierarchy/" + kind
    items, seen = [], set()
    page = args.page
    for _ in range(args.max_pages):
        params = {"page": page, "page_size": args.page_size}
        if getattr(args, "department", None):
            params["department_id"] = args.department
        if team_members:
            params["team_id"] = args.team
        value = client.request("GET", path + "?" + urlencode(params))
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("items"), list)
            or value.get("page") != page
            or value.get("page_size") != args.page_size
            or type(value.get("has_more")) is not bool
            or type(value.get("total")) is not int
            or value["total"] < 0
            or len(value["items"]) > args.page_size
            or (args.area != "org" and (value.get("org_id") != args.org or value.get("kind") != kind))
        ):
            raise common.CliError("Malformed or foreign hierarchy page.", "invalid_response", 5)
        for row in value["items"]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
                raise common.CliError("Missing or repeated hierarchy identity.", "invalid_response", 5)
            if args.area != "org" and row.get("org_id") != args.org:
                raise common.CliError("Foreign hierarchy row.", "invalid_response", 5)
            seen.add(row["id"])
            items.append(row)
        if not value["has_more"]:
            break
        if not value["items"]:
            raise common.CliError("Empty incomplete page.", "invalid_response", 5)
        page += 1
    value.update(items=items, complete=not value["has_more"], next_page=page if value["has_more"] else None, snapshot=False)
    return value


def write(client, method, path, body, reconcile, command):
    unknown = False
    try:
        acknowledgement = client.request(method, path, body)
        if not isinstance(acknowledgement, dict):
            unknown = True
    except common.CliError as exc:
        if exc.code not in {"unknown_mutation_outcome", "invalid_response"} and not (exc.status_code and exc.status_code >= 500):
            raise
        unknown = True
    except (OSError, ValueError, http.client.HTTPException):
        unknown = True
    try:
        observed = reconcile(acknowledgement if not unknown else None)
    except (common.CliError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        observed = None
        unknown = True
    return common.envelope(
        "pending" if unknown else "ok",
        command,
        {"outcome": "unknown" if unknown else "observed", "readback": observed, "request": {"method": method, "path": path, "body": body}},
        "No mutation was replayed. Inspect the exact target before retrying." if unknown else None,
    )


def confirm(args, before, desired, command):
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            {"before": before, "desired": desired, "cascade_supported": False, "expected_revision": before.get("revision") if before else None},
        )
    if not args.yes:
        raise common.CliError("Inspect --dry-run and pass --yes.", "confirmation_required", 1)
    if hasattr(args, "expected_revision") and (not args.expected_revision or not before or args.expected_revision != before.get("revision")):
        raise common.CliError("Use the exact revision from a fresh show or dry-run.", "stale_revision", 4)
    return None


def execute(args, client):
    team_members = args.area == "team" and args.action == "members"
    action = args.member_action if team_members else args.action
    command = "adp admin " + args.area + (" members" if team_members else " org-links" if args.area == "tenant" else "") + " " + action
    kind = "member" if team_members else args.area
    if args.area == "tenant":
        return links(args, client, command)
    if action == "list":
        return common.envelope("ok", command, list_rows(args, client, kind, team_members))
    if action == "create" or (args.area == "member" and action == "add"):
        return create(args, client, command)
    key = args.user if kind == "member" else args.org if kind == "org" else args.id
    path, before = snapshot(client, args.org, kind, key)
    if action == "show":
        return common.envelope("ok", command, before)
    if team_members:
        patch = (
            {"team_add": {"team_id": args.team, "role": args.role, "is_primary": args.primary}}
            if action == "add"
            else {"team_remove": {"team_id": args.team}}
        )
    else:
        patch = {name: getattr(args, name) for name in ("name", "description", "role") if getattr(args, name, None) is not None}
    deleting = action in {"delete", "remove"} and not team_members
    if not deleting and not patch:
        raise common.CliError("Supply at least one field to update.", "usage_error", 1)
    preview = confirm(args, before, {"delete": True} if deleting else patch, command)
    if preview:
        return preview
    if deleting and before["dependent_tables"]:
        raise common.CliError("Dependent records exist; remove them explicitly. Cascades are not supported.", "hierarchy_has_dependencies", 4)
    capability = "hierarchy.member.write" if kind == "member" and not deleting else "hierarchy.write"
    common.ensure_can_mutate(capability, request=client.api.request, token=client.token)

    def reconcile(ack):
        if not isinstance(ack, dict):
            return snapshot(client, args.org, kind, key)[1]
        if deleting and kind != "member":
            if ack != {"org_id": args.org, "kind": kind, "id": key, "deleted": True}:
                raise common.CliError("Malformed deletion acknowledgement.", "invalid_response", 5)
            try:
                client.request("GET", path)
            except common.CliError as exc:
                if exc.status_code == 404:
                    return {"deleted": True}
                raise
            raise common.CliError("Deleted hierarchy still exists.", "invalid_response", 5)
        checked(ack, args.org, kind, key)
        observed = snapshot(client, args.org, kind, key)[1]
        if observed["revision"] != ack["revision"]:
            raise common.CliError("Concurrent state changed after acknowledgement.", "invalid_response", 5)
        if deleting and observed["resource"].get("membership_status") != "revoked":
            raise common.CliError("Membership is still active.", "invalid_response", 5)
        if not deleting:
            resource = observed["resource"]
            if team_members:
                teams = resource.get("teams")
                if not isinstance(teams, list):
                    raise common.CliError("Missing team membership readback.", "invalid_response", 5)
                selected = [row for row in teams if isinstance(row, dict) and row.get("team_id") == args.team]
                matches = (
                    not selected
                    if action == "remove"
                    else len(selected) == 1 and selected[0].get("role") == args.role and (not args.primary or selected[0].get("is_primary") is True)
                )
            else:
                matches = all(resource.get(field) == value for field, value in patch.items())
            if not matches:
                raise common.CliError("Requested hierarchy changes were not observed.", "invalid_response", 5)
        return observed

    return write(
        client,
        "DELETE" if deleting else "PATCH",
        path + ("?" + urlencode({"expected_revision": args.expected_revision}) if deleting else ""),
        None if deleting else {"expected_revision": args.expected_revision, "patch": patch},
        reconcile,
        command,
    )


def create(args, client, command):
    area = args.area
    if area == "org":
        org = args.id
        body = {"id": args.id, "name": args.name}
        path = "/api/admin/identity/organizations"
    else:
        org = args.org
        base = "/admin/organizations/" + identifier(org)
        if area == "member":
            if args.existing_user:
                if args.email or args.name or args.team:
                    raise common.CliError("Existing-user placement accepts ID and role; use team members add separately.", "usage_error", 1)
                body = {"user_id": args.existing_user, "role": args.role}
                path = base + "/members"
            else:
                if not args.email:
                    raise common.CliError("--new-user requires --email.", "usage_error", 1)
                body = {"email": args.email, "role": args.role, "send_invite": False}
                if args.name:
                    body["name"] = args.name
                if args.team:
                    body["team_id"] = args.team
                path = "/api/admin/identity/organizations/" + identifier(org) + "/users"
        else:
            body = {"id": args.id, "name": args.name}
            if args.description is not None:
                body["description"] = args.description
            path = base + ("/departments" if area == "department" else "/departments/" + identifier(args.department) + "/teams")
    preview = confirm(args, None, {"org_id": org, "path": path, "body": body}, command)
    if preview:
        return preview
    common.ensure_can_mutate(
        "hierarchy.platform.write" if area in {"org", "member"} else "hierarchy.write", request=client.api.request, token=client.token
    )

    def reconcile(ack):
        if not isinstance(ack, dict) or not isinstance(ack.get("id"), str) or not ack["id"] or (area != "org" and ack.get("org_id") != org):
            raise common.CliError("Missing created hierarchy identity.", "invalid_response", 5)
        if area != "member" and ack["id"] != args.id:
            raise common.CliError("Created hierarchy ID mismatch.", "invalid_response", 5)
        return snapshot(client, org, area, ack["id"])[1]

    return write(client, "POST", path, body, reconcile, command)


def links(args, client, command):
    base = "/admin/tenants/" + identifier(args.tenant) + "/orgs"
    if args.action == "list":
        rows, seen = [], set()
        page = args.page
        for _ in range(args.max_pages):
            result = client.request("GET", base + "/page?" + urlencode({"page": page, "page_size": args.page_size}))
            if (
                not isinstance(result, dict)
                or result.get("tenant_id") != args.tenant
                or not isinstance(result.get("linked_orgs"), list)
                or result.get("page") != page
                or result.get("page_size") != args.page_size
                or type(result.get("has_more")) is not bool
                or type(result.get("total")) is not int
                or result["total"] < 0
                or len(result["linked_orgs"]) > args.page_size
            ):
                raise common.CliError("Malformed tenant organization links.", "invalid_response", 5)
            for row in result["linked_orgs"]:
                if not isinstance(row, dict) or not isinstance(row.get("org_id"), str) or not row["org_id"] or row["org_id"] in seen:
                    raise common.CliError("Repeated or malformed organization link.", "invalid_response", 5)
                rows.append(row)
                seen.add(row["org_id"])
            if not result["has_more"]:
                break
            if not result["linked_orgs"]:
                raise common.CliError("Empty incomplete link page.", "invalid_response", 5)
            page += 1
        result.update(linked_orgs=rows, complete=not result["has_more"], next_page=page if result["has_more"] else None)
        return common.envelope("ok", command, result)
    path = base + "/" + identifier(args.github_org_id)
    before = client.request("GET", path + "/preview")
    if (
        not isinstance(before, dict)
        or before.get("tenant_id") != args.tenant
        or before.get("github_org_id") != args.github_org_id
        or not re.fullmatch(r"[a-f0-9]{64}", before.get("revision", ""))
    ):
        raise common.CliError("Malformed organization link preview.", "invalid_response", 5)
    preview = confirm(args, before, {"action": args.action, "tenant_id": args.tenant, "github_org_id": args.github_org_id}, command)
    if preview:
        return preview
    common.ensure_can_mutate("hierarchy.platform.write", request=client.api.request, token=client.token)

    def reconcile(ack):
        if not isinstance(ack, dict) or ack.get("tenant_id") != args.tenant or ack.get("github_org_id") != args.github_org_id:
            raise common.CliError("Malformed link acknowledgement.", "invalid_response", 5)
        result = client.request("GET", path + "/preview")
        if not isinstance(result, dict) or result.get("parent_tenant_id") != (args.tenant if args.action == "add" else None):
            raise common.CliError("Organization link state mismatch.", "invalid_response", 5)
        return result

    return write(
        client,
        "POST" if args.action == "add" else "DELETE",
        base if args.action == "add" else path + "?" + urlencode({"expected_revision": args.expected_revision}),
        {"github_org_id": args.github_org_id, "expected_revision": args.expected_revision} if args.action == "add" else None,
        reconcile,
        command,
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, Client()), args.json)
    except (common.CliError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
        return common.report_error(exc, "adp admin hierarchy", "--json" in argv)
    except KeyboardInterrupt:
        common.emit(
            common.envelope("pending", "adp admin hierarchy", {"outcome": "unknown"}, "Interrupted; inspect the exact target before retrying."),
            "--json" in argv,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
