#!/usr/bin/env python3
"""Nightly served-CLI reads; no inference or platform mutations.

These cases qualify read/error regressions only. Capability contrasts, marked
usage reconciliation and active remote controls retain separate acceptance.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import common
from capability_contrast import _write_session


def detail(envelope):
    common.require(
        isinstance(envelope, dict) and envelope.get("status") == "ok",
        "CLI read did not return success",
    )
    result = envelope.get("detail")
    common.require(isinstance(result, dict), "CLI read lacks structured detail")
    return result


def capabilities(cli, evidence):
    document = detail(cli.json(["capabilities", "--refresh"]))
    rows = document.get("operations")
    common.require(isinstance(rows, list) and rows, "Capabilities has no operations")
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    common.require(
        len(ids) == len(rows) and all(ids) and len(set(ids)) == len(ids),
        "Capabilities operation IDs are missing or duplicated",
    )
    findings = detail(cli.json(["doctor", "--checks", "auth,api"]))
    checks = findings.get("checks") or {}
    common.require(set(checks) == {"auth", "api"}, "Doctor omitted requested checks")
    common.require(
        all(check.get("state") == "ok" for check in checks.values()),
        "Authenticated API readiness was not confirmed",
    )
    evidence.update(operation_count=len(ids), checks=list(checks))


def usage(cli, evidence):
    end = datetime.now(timezone.utc)
    flags = [
        "--start",
        (end - timedelta(hours=1)).isoformat(),
        "--end",
        end.isoformat(),
    ]
    forms = [
        ["usage", "summary"],
        ["usage", "timeline"],
        ["usage", "models"],
        ["usage", "requests"],
        ["logs", "list"],
    ]
    for command in forms:
        result = detail(cli.json([*command, *flags]))
        common.require(
            result.get("scope", {}).get("kind") == "own"
            and isinstance(result.get("items"), list),
            "Usage read lacks own scope or records array",
        )
    # A fresh evaluation identity may legitimately have no inference records.
    # Do not call that spend reconciliation; check the export's actual status.
    code, envelope = cli.run(
        ["logs", "export", *flags, "--format", "json", "--max-pages", "1"],
        expected=None,
    )
    common.require(isinstance(envelope, dict), "Export lacks JSON output")
    exported = envelope.get("detail") or {}
    complete = exported.get("complete")
    common.require(type(complete) is bool, "Export completeness is missing")
    common.require(
        code == (0 if complete else 4)
        and envelope.get("status") == ("ok" if complete else "pending")
        and exported.get("scope", {}).get("kind") == "own"
        and isinstance(exported.get("items"), list),
        "Export status, exit code or scope is inconsistent",
    )
    common.require(
        complete or exported.get("next_cursor"), "Partial export lacks cursor"
    )
    evidence.update(commands=[" ".join(c) for c in forms], export_complete=complete)


def activity(cli, evidence):
    result = detail(cli.json(["agent", "list", "--page-size", "1"]))
    common.require(
        isinstance(result.get("items"), list) and type(result.get("complete")) is bool,
        "Activity list lacks items or pagination completeness",
    )
    missing = str(uuid.uuid4())
    # A random absent ID exercises the API error path without touching real work.
    # No POST/control action is issued by this nightly read scenario.
    for action in ("status", "state", "detail"):
        code, envelope = cli.run(["agent", action, "--run", missing], expected=None)
        common.require(
            code != 0
            and isinstance(envelope, dict)
            and envelope.get("status") == "failed"
            and (envelope.get("error") or {}).get("http_status") == 404,
            "Missing Activity run did not return structured HTTP 404",
        )
    evidence.update(
        cases=[
            "paginated-own-list",
            "missing-status",
            "missing-state",
            "missing-detail",
        ]
    )


def vault(cli, evidence):
    for area in ("credential", "identity"):
        result = detail(cli.json([area, "list"]))
        common.require(
            isinstance(result.get("items"), list), "Vault list lacks metadata rows"
        )
        for row in result["items"]:
            common.require(
                isinstance(row, dict)
                and row.get("id")
                and not {
                    "value",
                    "secret_arn",
                    "access_token",
                    "refresh_token",
                }.intersection(row),
                "Vault metadata exposed secret fields or lacks an ID",
            )
    # Deliberately unreadable input: dry-run must not open it or issue a write.
    preview = cli.json(
        [
            "credential",
            "add",
            "--service",
            "cli-regression",
            "--label",
            "preview",
            "--type",
            "api_key",
            "--operation-id",
            str(uuid.uuid4()),
            "--value-file",
            "/nonexistent-cli-dry-run-secret",
            "--dry-run",
        ]
    )
    common.require(
        preview.get("status") == "dry_run",
        "Credential preview did not remain a dry-run",
    )
    claim = cli.json(
        [
            "identity",
            "link",
            "--provider",
            "github",
            "--provider-user-id",
            "cli-regression-preview",
            "--dry-run",
        ]
    )
    common.require(
        claim.get("status") == "dry_run", "Identity preview did not remain a dry-run"
    )
    evidence["cases"] = [
        "credential-metadata",
        "identity-metadata",
        "credential-dry-run-no-secret",
        "identity-dry-run",
    ]


def access(cli, evidence):
    selected = detail(cli.json(["tenant", "current"]))
    tenant_id = selected.get("tenant_id")
    common.require(isinstance(tenant_id, str) and tenant_id, "No current tenant")
    status = detail(cli.json(["access", "status", "--tenant", tenant_id]))
    common.require(status.get("tenant_id") == tenant_id, "Access status changed target")
    common.require(
        status.get("spend_eligibility") == "not_evaluated",
        "Membership must not assert spend eligibility",
    )
    page = detail(cli.json(["admin", "access-request", "list", "--limit", "5"]))
    common.require(isinstance(page.get("items"), list), "Access request page missing")
    evidence["cases"] = ["tenant-access-status", "bounded-admin-review"]


def hierarchy(cli, evidence):
    orgs = detail(cli.json(["admin", "org", "list", "--page-size", "1"]))
    common.require(
        isinstance(orgs.get("items"), list), "Organization list is malformed"
    )
    common.require(bool(orgs["items"]), "No authorized hierarchy fixture is visible")
    org = orgs["items"][0].get("id")
    common.require(isinstance(org, str) and org, "Organization ID is missing")
    for area in ("department", "team", "member"):
        result = detail(
            cli.json(["admin", area, "list", "--org", org, "--page-size", "1"])
        )
        common.require(
            result.get("org_id") == org
            and result.get("kind") == area
            and isinstance(result.get("items"), list),
            "Hierarchy list lost its selected scope",
        )
        common.require(
            all(
                isinstance(row, dict) and row.get("org_id") == org
                for row in result["items"]
            ),
            "Foreign hierarchy row was returned",
        )
    evidence.update(
        org_id=org,
        forms=["org", "department", "team", "member"],
        qualification="bounded administrator reads only; membership and delete lifecycle acceptance remains separate",
    )


def budget(cli, evidence):
    periods = ("daily", "weekly", "monthly")
    for period in periods:
        result = detail(cli.json(["budget", "me", "--period", period]))
        common.require(
            isinstance(result.get("period"), dict)
            and result["period"].get("period_type") == period
            and isinstance(result.get("lines"), list),
            "Own budget period or lines are malformed",
        )
        for line in result["lines"]:
            common.require(isinstance(line, dict), "Budget line is malformed")
            common.require(
                line.get("cap_status") in {"capped", "uncapped"},
                "Budget cap status missing",
            )
            if line["cap_status"] == "uncapped":
                common.require(
                    line.get("cap_usd") is None and line.get("remaining_usd") is None,
                    "Uncapped budget was represented as zero headroom",
                )
    evidence.update(
        periods=list(periods),
        qualification="own budget reads only; no spend-through or enforcement claim",
    )


SCENARIOS = {
    "capabilities": capabilities,
    "usage": usage,
    "activity": activity,
    "vault": vault,
    "access": access,
    "hierarchy": hierarchy,
    "budget": budget,
}


def execute(config, evidence):
    common.require(config.get("cli_path"), "install_auth did not retain served CLI")
    mode = config.get("mode")
    common.require(mode in SCENARIOS, "Unknown story read scenario")
    with tempfile.TemporaryDirectory(prefix="adp-story-reads-") as directory:
        home = Path(directory)
        os.chmod(home, 0o700)
        env = common.clean_env(config, HOME=home)
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            path = home / name
            path.mkdir(mode=0o700)
            env[name] = str(path)
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=30)
        SCENARIOS[mode](cli, evidence)
        evidence["qualification"] = (
            "served CLI read/error regression; not full story acceptance"
        )
