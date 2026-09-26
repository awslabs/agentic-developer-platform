#!/usr/bin/env python3
"""Read own budgets and manage exact tenant/entity/ledger/period caps."""

from __future__ import annotations

import http.client
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

PERIODS = ("daily", "weekly", "monthly")


def parser():
    root = common.Parser(prog="adp budget")
    areas = root.add_subparsers(dest="area", required=True)
    own = areas.add_parser("budget").add_subparsers(dest="action", required=True)
    admin = areas.add_parser("admin").add_subparsers(dest="admin_area", required=True)
    managed = admin.add_parser("budget").add_subparsers(dest="action", required=True)
    for action in ("me", "list", "show", "set", "delete", "status"):
        p = (own if action == "me" else managed).add_parser(action)
        p.add_argument("--json", action="store_true")
        if action != "me":
            p.add_argument("--org", required=True)
        if action == "list":
            p.add_argument("--page", type=int, choices=range(1, 10001), default=1, metavar="1..10000")
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20, metavar="1..100")
            p.add_argument("--max-pages", type=int, choices=range(1, 101), default=1, metavar="1..100")
        else:
            p.add_argument("--period", choices=PERIODS, required=action != "me", default="monthly" if action == "me" else None)
        if action not in {"me", "list"}:
            target = p.add_mutually_exclusive_group(required=True)
            for name in ("user", "team", "department"):
                target.add_argument("--" + name)
            target.add_argument("--scope", choices=["org"])
            p.add_argument("--usage", choices=["personal", "cloud-agents"])
        if action in {"set", "delete"}:
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--expected-revision", help="Timestamp from show or dry-run; required for existing caps")
        if action == "set":
            p.add_argument("--expect-absent", action="store_true", help="Create only if the inspected exact cap is absent")
            p.add_argument("--amount-usd", required=True)
            p.add_argument("--mode", choices=["hard", "soft"], required=True)
    for namespace, families in ((own, ("person-cap",)), (managed, ("person-cap", "person-default"))):
        for family in families:
            commands = namespace.add_parser(family).add_subparsers(dest="person_action", required=True)
            for action in ("show", "set", "delete"):
                p = commands.add_parser(action)
                p.add_argument("--json", action="store_true")
                p.add_argument("--period", choices=PERIODS, default="monthly")
                if namespace is managed:
                    p.add_argument("--person" if family == "person-cap" else "--scope", required=True)
                if action in {"set", "delete"}:
                    p.add_argument("--dry-run", action="store_true")
                    p.add_argument("--yes", action="store_true")
                    p.add_argument("--expected-revision", help="Exact revision from show/preview, or absent")
                if action == "set":
                    p.add_argument("--amount-usd", required=True)
    p = managed.add_parser("member-report")
    p.add_argument("--org", required=True)
    p.add_argument("--period", choices=PERIODS, default="monthly")
    p.add_argument("--page", type=int, choices=range(1, 10001), default=1)
    p.add_argument("--page-size", type=int, choices=range(1, 51), default=20)
    p.add_argument("--max-pages", type=int, choices=range(1, 101), default=1)
    p.add_argument("--json", action="store_true")
    return root


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact nonempty target ID.", "usage_error", 1)
    return quote(value, safe="")


def amount(value):
    try:
        number = Decimal(value)
        if not number.is_finite() or number <= 0 or number > Decimal("99999999.99") or number != number.quantize(Decimal("0.01")):
            raise ValueError
        return format(number, ".2f")
    except (ValueError, InvalidOperation):
        raise common.CliError("Use positive finite USD, at most 99999999.99 and two decimal places.", "usage_error", 1) from None


def target(args):
    if args.user:
        if args.usage is None:
            raise common.CliError("User caps require --usage personal|cloud-agents.", "usage_error", 1)
        kind = "user" if args.usage == "personal" else "root_user"
        return kind, identifier(args.user)
    if args.usage:
        raise common.CliError("--usage applies only to a user target.", "usage_error", 1)
    if args.scope:
        return "org", identifier(args.org)
    if args.team:
        return "team", identifier(args.team)
    return "department", identifier(args.department)


class Client:
    def __init__(self):
        self.api = common.Api()
        self.token = common.access_token()

    def request(self, method, path, body=None):
        return self.api.request(method, path, body, token=self.token, timeout=30)


def config(value, org, kind, period, key=None, canonical=None):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("org_id") != org
        or value.get("entity_type") != kind
        or value.get("period_type") != period
        or not isinstance(value.get("entity_id"), str)
        or not value["entity_id"]
        or not isinstance(value.get("updated_at"), str)
        or (key is not None and kind not in {"user", "root_user"} and value.get("entity_id") != unquote(key))
        or (canonical is not None and value.get("entity_id") != canonical)
    ):
        raise common.CliError("Budget response target or period mismatch.", "invalid_response", 5)
    if not isinstance(value.get("budget_amount_usd"), str) or value.get("enforcement_mode") not in {"hard", "soft"}:
        raise common.CliError("Budget amount or mode is malformed.", "invalid_response", 5)
    try:
        if Decimal(value["budget_amount_usd"]) < 0 or not Decimal(value["budget_amount_usd"]).is_finite():
            raise ValueError
        if datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise common.CliError("Malformed budget amount or revision.", "invalid_response", 5) from None
    return value


def person_scope(value):
    parts = value.split(":")
    if not (parts == ["platform"] or len(parts) == 2 and parts[0] == "org" or len(parts) == 3 and parts[0] == "team"):
        raise common.CliError("Use platform, org:ORG, or team:ORG:TEAM.", "usage_error", 1)
    for part in parts:
        identifier(part)
    return parts[0], parts[1] if len(parts) > 1 else None, parts[2] if len(parts) > 2 else None


def person_row(value, args):
    def malformed():
        raise common.CliError("Malformed person limit or mismatched target/period.", "invalid_response", 5)

    if not isinstance(value, dict) or value.get("period_type") != args.period or value.get("cap_status") not in {"capped", "uncapped"}:
        malformed()
    if args.action == "person-default":
        if tuple(value.get(k) for k in ("scope_type", "scope_id_org", "scope_id_team")) != person_scope(args.scope):
            malformed()
    else:
        anchor = value.get("person_anchor")
        if not isinstance(anchor, str) or not anchor or ":" not in anchor:
            malformed()
        if args.area == "admin" and anchor != args.person:
            malformed()
        if value.get("source") not in {None, "own", "admin", "team_default", "org_default", "platform_default"}:
            malformed()
    if value["cap_status"] == "uncapped":
        if any(value.get(k) is not None for k in ("cap_usd", "enforcement_mode", "updated_at")):
            malformed()
        if args.action == "person-cap" and value.get("source") is not None:
            malformed()
    else:
        try:
            if not isinstance(value.get("cap_usd"), str) or not Decimal(value["cap_usd"]).is_finite() or Decimal(value["cap_usd"]) < 0:
                malformed()
            if value.get("enforcement_mode") not in {"hard", "soft"}:
                malformed()
            if args.action == "person-cap" and value.get("source") is None:
                malformed()
            if args.area == "admin" and not isinstance(value.get("updated_at"), str):
                malformed()
            if value.get("updated_at") is not None:
                if not isinstance(value["updated_at"], str) or datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00")).tzinfo is None:
                    malformed()
        except (ValueError, InvalidOperation):
            malformed()
    return value


def member_report(args, client):
    path = "/admin/organizations/" + identifier(args.org) + "/member-budgets?"
    items, seen, page = [], set(), args.page
    for _ in range(args.max_pages):
        value = client.request("GET", path + urlencode(dict(period_type=args.period, page=page, page_size=args.page_size)))
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("items"), list)
            or type(value.get("has_more")) is not bool
            or value.get("page") != page
            or value.get("page_size") != args.page_size
            or value.get("period_type") != args.period
            or type(value.get("total")) is not int
            or value["total"] < 0
            or len(value["items"]) > args.page_size
            or not isinstance(value.get("period_start"), str)
        ):
            raise common.CliError("Malformed member budget page.", "invalid_response", 5)
        try:
            datetime.fromisoformat(value["period_start"])
            for row in value["items"]:
                if not isinstance(row, dict) or not isinstance(row.get("user_id"), str) or not row["user_id"] or row["user_id"] in seen:
                    raise ValueError
                seen.add(row["user_id"])
                if not isinstance(row.get("spend_usd"), str) or not Decimal(row["spend_usd"]).is_finite() or Decimal(row["spend_usd"]) < 0:
                    raise ValueError
                if row.get("limit_status") not in {"capped", "uncapped"}:
                    raise ValueError
                if row["limit_status"] == "capped":
                    if not isinstance(row.get("limit_usd"), str) or not Decimal(row["limit_usd"]).is_finite() or Decimal(row["limit_usd"]) < 0:
                        raise ValueError
                    if row.get("source") not in {"own", "admin", "team_default", "org_default", "platform_default"}:
                        raise ValueError
                elif row.get("limit_usd") is not None or row.get("source") is not None:
                    raise ValueError
                items.append(row)
        except (ValueError, InvalidOperation):
            raise common.CliError("Malformed or repeated member budget row.", "invalid_response", 5) from None
        if not value["has_more"]:
            break
        if not value["items"]:
            raise common.CliError("Incomplete empty member budget page.", "invalid_response", 5)
        page += 1
    value.update(
        items=items,
        org_id=args.org,
        complete=not value["has_more"],
        next_page=page if value["has_more"] else None,
        snapshot=False,
        usage_kind="settled_selected_org",
        cross_org_headroom="unknown",
    )
    return common.envelope(
        "ok",
        "adp admin budget member-report",
        value,
        "Person caps govern cross-tenant totals; this report contains only selected-org settled spend. It cannot establish global headroom.",
    )


def person_execute(args, client):
    command = "adp " + ("admin " if args.area == "admin" else "") + "budget " + args.action + " " + args.person_action
    if args.area != "admin" and args.person_action != "show":
        raise common.CliError(
            "Person limits are admin-governed. Self set/delete is unavailable; ask a platform admin. No request was sent.", "permission_denied", 3
        )
    if args.action == "person-default":
        person_scope(args.scope)
        path = "/budget/person-default/" + identifier(args.scope)
    elif args.area == "admin":
        # Require the canonical anchor, not tenant user IDs or ledger root_user IDs.
        if not args.person.startswith("github:") or not args.person[7:].isdigit():
            raise common.CliError("Use the canonical github:NUMERIC_ID person anchor.", "usage_error", 1)
        path = "/budget/person-cap/" + identifier(args.person)
    else:
        path = "/me/budget/person-cap"
    path += "?" + urlencode({"period_type": args.period})
    desired = amount(args.amount_usd) if args.person_action == "set" else None
    before = person_row(client.request("GET", path), args)
    if args.person_action == "show":
        return common.envelope(
            "ok",
            command,
            {"configuration": before, "authority": "platform_admin", "usage": "unknown", "headroom": "unknown"},
            "Use budget me for your settled person envelope and blockers. Admin show reads the explicit row; absence may inherit a default.",
        )
    revision = before["updated_at"] if before["cap_status"] == "capped" else "absent"
    effect = (
        "Individual rows override defaults. Deleting an individual row restores applicable team/org/platform defaults, or uncapped if none applies. "
        "Default changes govern current and future members without a more specific rule; deleting a default restores broader rules. "
        "Usage is preserved. Other budgets still apply; person-limit cache refresh can take 60 seconds."
    )
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            dict(
                before=before,
                amount_usd=desired,
                enforcement_mode="hard" if desired else None,
                expected_revision=revision,
                effect=effect,
                resolved_after="unknown_until_person_readback",
            ),
        )
    if not args.yes or not args.expected_revision:
        raise common.CliError("Inspect --dry-run, then pass --yes --expected-revision REV (or absent).", "confirmation_required", 1)
    if args.expected_revision != revision:
        raise common.CliError("Person limit changed since review; inspect again.", "conflict", 4)
    common.ensure_can_mutate("budget.person.write", request=client.api.request, token=client.token)
    route = path + "&" + urlencode({"expected_revision": args.expected_revision})
    method = "PUT" if args.person_action == "set" else "DELETE"
    unknown = False
    try:
        ack = client.request(method, route, {"budget_amount_usd": desired} if method == "PUT" else None)
        if method == "PUT":
            person_row(ack, args)
            if ack["cap_status"] != "capped" or Decimal(ack["cap_usd"]) != Decimal(desired) or ack["enforcement_mode"] != "hard":
                raise common.CliError("Mismatched person limit acknowledgement.", "invalid_response", 5)
        elif ack != {}:
            raise common.CliError("Malformed person limit delete acknowledgement.", "invalid_response", 5)
    except common.CliError as exc:
        if exc.code not in {"unknown_mutation_outcome", "invalid_response"} and not (exc.status_code and exc.status_code >= 500):
            raise
        unknown = True
    except (http.client.HTTPException, OSError, ValueError):
        unknown = True
    try:
        observed = person_row(client.request("GET", path), args)
    except (common.CliError, http.client.HTTPException, OSError, ValueError):
        observed = None
        unknown = True
    matches = not unknown and observed is not None and (observed["cap_status"] == "uncapped" if method == "DELETE" else observed == ack)
    return common.envelope(
        "ok" if matches else "pending",
        command,
        dict(configuration=observed, readback_matches=bool(matches), outcome="unknown" if unknown else "acknowledged"),
        "No mutation replayed. " + effect + " Saved configuration is not live enforcement evidence.",
    )


def execute(args, client):
    if args.action in {"person-cap", "person-default"}:
        return person_execute(args, client)
    if args.action == "member-report":
        return member_report(args, client)
    command = "adp " + ("budget me" if args.action == "me" else "admin budget " + args.action)
    if args.action == "me":
        value = client.request("GET", "/me/budget?" + urlencode({"period_type": args.period}))
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("period"), dict)
            or value["period"].get("period_type") != args.period
            or not isinstance(value.get("lines"), list)
        ):
            raise common.CliError("Malformed own budget response.", "invalid_response", 5)
        return common.envelope("ok", command, value, "Direct and cloud-agent lines are separate; combined informational spend is not a shared cap.")
    base = "/admin/organizations/" + identifier(args.org)
    if args.action == "list":
        seen, items = set(), []
        page = args.page
        for _ in range(args.max_pages):
            value = client.request("GET", base + "/budgets?" + urlencode({"page": page, "limit": args.page_size}))
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("items"), list)
                or type(value.get("has_more")) is not bool
                or value.get("page") != page
                or value.get("page_size") != args.page_size
                or type(value.get("total")) is not int
                or value["total"] < 0
                or len(value["items"]) > args.page_size
            ):
                raise common.CliError("Malformed budget page.", "invalid_response", 5)
            for row in value["items"]:
                if not isinstance(row, dict):
                    raise common.CliError("Malformed budget row.", "invalid_response", 5)
                key = tuple(row.get(n) for n in ("entity_type", "entity_id", "period_type"))
                if not all(isinstance(i, str) and i for i in key) or key in seen:
                    raise common.CliError("Missing or repeated budget identity.", "invalid_response", 5)
                seen.add(key)
                items.append(row)
            if not value["has_more"]:
                break
            if not value["items"]:
                raise common.CliError("Incomplete empty budget page.", "invalid_response", 5)
            page += 1
        value.update(items=items, org_id=args.org, complete=not value["has_more"], next_page=page if value["has_more"] else None, snapshot=False)
        return common.envelope("ok", command, value)
    kind, key = target(args)
    path = base + "/budget/" + kind + "/" + key + "/" + args.period
    selected = {
        "org_id": args.org,
        "entity_type": kind,
        "requested_entity_id": getattr(args, "user", None) or getattr(args, "team", None) or getattr(args, "department", None) or args.org,
        "period_type": args.period,
        "usage": args.usage,
    }
    if args.action == "status":
        canonical = config(client.request("GET", path), args.org, kind, args.period, key)
        if canonical is None:
            return common.envelope("unavailable", command, {"selection": selected, "configuration": None})
        selected["canonical_entity_id"] = canonical["entity_id"]
        value = client.request("GET", base + "/budgets/" + kind + "/" + key + "/" + args.period + "/status")
        if (
            not isinstance(value, dict)
            or value.get("period_type") != args.period
            or not all(
                n in value
                for n in ("budget_amount_usd", "current_spend_usd", "remaining_budget_usd", "period_start", "period_end", "enforcement_mode")
            )
        ):
            raise common.CliError("Malformed exact-period budget status.", "invalid_response", 5)
        try:
            for field in ("budget_amount_usd", "current_spend_usd", "remaining_budget_usd"):
                if not isinstance(value[field], str) or not Decimal(value[field]).is_finite():
                    raise ValueError
            if value["enforcement_mode"] not in {"hard", "soft"}:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise common.CliError("Malformed exact-period budget money or mode.", "invalid_response", 5) from None
        return common.envelope(
            "ok",
            command,
            {"selection": selected, **value},
            "This cap alone does not establish ancestor headroom. Soft caps do not deny inference; in-flight estimates may overshoot.",
        )
    desired = amount(args.amount_usd) if args.action == "set" else None
    before = config(client.request("GET", path), args.org, kind, args.period, key)
    if args.action == "show":
        return common.envelope(
            "ok" if before else "unavailable",
            command,
            {"selection": selected, "configuration": before},
            "No selected cap does not mean zero remaining; ancestor caps may still apply." if before is None else None,
        )
    if args.action == "delete" and before is None:
        return common.envelope("unavailable", command, {"selection": selected, "configuration": None})
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            {
                "selection": selected,
                "before": before,
                "amount_usd": desired,
                "mode": getattr(args, "mode", None),
                "expected_revision": before["updated_at"] if before else None,
                "expect_absent": before is None,
                "effect": "Only the selected cap changes. Usage is preserved and ancestor limits still apply.",
            },
        )
    if not args.yes:
        raise common.CliError("Inspect --dry-run and pass --yes.", "confirmation_required", 1)
    if before is not None and (not args.expected_revision or getattr(args, "expect_absent", False)):
        raise common.CliError("Existing cap requires --expected-revision from show; --expect-absent cannot update it.", "usage_error", 1)
    if before is None and (not args.expect_absent or args.expected_revision):
        raise common.CliError("New cap requires --expect-absent and no revision.", "usage_error", 1)
    common.ensure_can_mutate("budget.managed.write", request=client.api.request, token=client.token)
    method = "PUT" if args.action == "set" else "DELETE"
    body = (
        {
            "budget_amount_usd": desired,
            "enforcement_mode": args.mode,
            "expected_revision": args.expected_revision,
            "expect_absent": args.expect_absent,
        }
        if method == "PUT"
        else None
    )
    route = path if method == "PUT" else path + "/revision?" + urlencode({"expected_revision": args.expected_revision})
    unknown = False
    canonical_id = before["entity_id"] if before is not None else None
    if canonical_id is not None:
        selected["canonical_entity_id"] = canonical_id
    try:
        response = client.request(method, route, body)
        if method == "PUT":
            if config(response, args.org, kind, args.period, key, canonical=canonical_id) is None:
                raise common.CliError("Missing write acknowledgement.", "invalid_response", 5)
            canonical_id = response["entity_id"]
            selected["canonical_entity_id"] = canonical_id
        elif response != {}:
            raise common.CliError("Malformed delete acknowledgement.", "invalid_response", 5)
    except common.CliError as exc:
        if exc.code not in {"unknown_mutation_outcome", "invalid_response"} and not (exc.status_code and exc.status_code >= 500):
            raise
        unknown = True
    except (http.client.HTTPException, OSError, ValueError):
        unknown = True
    try:
        observed = config(client.request("GET", path), args.org, kind, args.period, key, canonical=canonical_id)
    except (common.CliError, http.client.HTTPException, OSError, ValueError):
        observed = None
        unknown = True
    if unknown:
        return common.envelope(
            "pending",
            command,
            {"selection": selected, "outcome": "unknown", "observed_configuration": observed},
            "No mutation was replayed. Inspect this exact cap before retrying; observed state does not prove which operation wrote it.",
        )
    matches = (
        observed is None
        if method == "DELETE"
        else observed is not None
        and Decimal(observed["budget_amount_usd"]) == Decimal(desired)
        and observed["enforcement_mode"] == args.mode
        and observed["updated_at"] == response["updated_at"]
    )
    return common.envelope(
        "ok" if matches else "pending",
        command,
        {"selection": selected, "configuration": observed, "readback_matches": matches},
        "Saved configuration is not live enforcement evidence. Usage was not reset.",
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        if args.action == "set":
            amount(args.amount_usd)
        if args.action not in {"me", "list", "person-cap", "person-default", "member-report"}:
            target(args)
        return common.emit(execute(args, Client()), args.json)
    except (common.CliError, OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        return common.report_error(exc, "adp budget", "--json" in argv)
    except KeyboardInterrupt:
        common.emit(
            common.envelope(
                "pending",
                "adp budget",
                {"outcome": "unknown"},
                "Client interrupted. No cancellation was sent; inspect the selected cap before retrying.",
            ),
            "--json" in argv,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
