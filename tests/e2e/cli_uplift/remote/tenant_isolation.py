#!/usr/bin/env python3
"""E23 tenant smoke and E27 two-tenant read/refresh isolation; no inference."""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import uuid
from pathlib import Path

import common
from capability_contrast import _write_session, ordinary_session


def detail(value):
    common.require(
        isinstance(value, dict) and value.get("status") == "ok", "Tenant command failed"
    )
    data = value.get("detail")
    common.require(isinstance(data, dict), "Tenant command lacks detail")
    return data


def smoke(cli, evidence):
    rows = detail(cli.json(["tenant", "list"])).get("items")
    common.require(isinstance(rows, list) and rows, "No visible tenant fixture")
    tenant_id = rows[0].get("org_id")
    common.require(
        isinstance(tenant_id, str) and tenant_id, "Visible tenant ID missing"
    )
    # Selecting a visible fixture explicitly is different from a product default.
    chosen = detail(cli.json(["--tenant", tenant_id, "tenant", "current"]))
    common.require(
        chosen.get("tenant_id") == tenant_id
        and chosen.get("selection_source") == "flag",
        "Explicit tenant did not bind",
    )
    code, current = cli.run(["tenant", "current"], expected=None)
    if len(rows) == 1:
        common.require(
            code == 0 and detail(current).get("tenant_id") == tenant_id,
            "Single membership default failed",
        )
    else:
        common.require(
            code == 4
            and (current.get("error") or {}).get("code") == "tenant_selection_required",
            "Ambiguous memberships silently selected a tenant",
        )
    code, refused = cli.run(
        ["--tenant", "absent-" + str(uuid.uuid4()), "tenant", "current"], expected=None
    )
    common.require(
        code == 4
        and (refused.get("error") or {}).get("code") == "tenant_selection_required",
        "Foreign/unknown selector was not refused",
    )
    evidence.update(
        tenant_id=tenant_id,
        visible_memberships=len(rows),
        qualification="tenant selection/error smoke; no inference or cross-tenant acceptance claim",
    )


def scoped_capabilities(cli, tenant_id):
    data = detail(cli.json(["--tenant", tenant_id, "capabilities", "--refresh"]))
    common.require(
        (data.get("tenant") or {}).get("org_id") == tenant_id,
        "Request crossed its selected tenant",
    )
    return tenant_id


def isolated_reads(cli, fixture, evidence):
    tenants = fixture.get("tenant_ids") or []
    common.require(
        isinstance(tenants, list)
        and len(tenants) == 2
        and all(isinstance(t, str) and t for t in tenants)
        and len(set(tenants)) == 2,
        "Supply two distinct existing tenant memberships",
    )
    visible = {row["org_id"] for row in detail(cli.json(["tenant", "list"]))["items"]}
    common.require(
        set(tenants) <= visible,
        "Both tenant fixtures must be visible to the existing session",
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for target in tenants:
            pending = [
                pool.submit(scoped_capabilities, cli, tenant_id)
                for tenant_id in tenants
            ]
            # This modifies only the disposable client's identity-specific default.
            common.require(
                detail(cli.json(["tenant", "use", target])).get("tenant_id") == target,
                "Default save failed",
            )
            common.require(
                [future.result() for future in pending] == tenants,
                "Concurrent reads lost tenant scope",
            )
    # Refresh the one original Cognito store. Subsequent explicit selectors must
    # remain independent of whatever shared Cognito default its token contains.
    cli.run(["refresh"], json_output=False)
    for tenant_id in tenants:
        scoped_capabilities(cli, tenant_id)
    evidence.update(
        tenant_ids=tenants,
        concurrent_rounds=2,
        refreshed=True,
        qualification="two-tenant CLI read/default/refresh isolation; long-lived model inference, membership revocation and lost mutation acknowledgement remain separate acceptance",
    )


def user_switch_reads(cli, config, fixture, home, evidence):
    """Swap legitimate fixture sessions only in this disposable client's stores."""
    before = detail(cli.json(["tenant", "current"]))
    common.require(
        before.get("selection_source") == "saved_default"
        and before.get("identity")
        and before["identity"] != fixture["ordinary_login_user_id"]
        and before.get("tenant_id") != fixture["ordinary_tenant_id"],
        "User switch requires distinct identities and observable tenant defaults",
    )
    tokens = ordinary_session(config, fixture)  # Fixture setup, not login acceptance.
    directory = home / ".bedrock-gateway"
    original = {
        directory / name: (directory / name).read_bytes()
        for name in ("config.json", "tokens.json")
    }
    try:
        _write_session(home, config["gateway_url"], tokens)
        rows = detail(cli.json(["tenant", "list"])).get("items")
        common.require(
            isinstance(rows, list)
            and rows
            and all(
                isinstance(row, dict) and isinstance(row.get("org_id"), str)
                for row in rows
            )
            and len({row["org_id"] for row in rows}) == len(rows)
            and fixture["ordinary_tenant_id"] in {row["org_id"] for row in rows},
            "User-switch fixture lacks its known native membership",
        )
        if len(rows) == 1:
            current = detail(cli.json(["tenant", "current"]))
            expected_source = "single_membership"
        else:
            code, current = cli.run(["tenant", "current"], expected=4)
            common.require(
                code == 4
                and (current.get("error") or {}).get("code")
                == "tenant_selection_required",
                "Original user's default leaked into an ambiguous ordinary session",
            )
            current = detail(
                cli.json(
                    ["--tenant", fixture["ordinary_tenant_id"], "tenant", "current"]
                )
            )
            expected_source = "flag"
        common.require(
            current.get("identity") == fixture["ordinary_login_user_id"]
            and current.get("tenant_id") == fixture["ordinary_tenant_id"]
            and current.get("selection_source") == expected_source,
            "Original user's saved default leaked into the ordinary session",
        )
        saved = detail(cli.json(["tenant", "use", fixture["ordinary_tenant_id"]]))
        common.require(
            saved.get("identity") == fixture["ordinary_login_user_id"]
            and saved.get("tenant_id") == fixture["ordinary_tenant_id"]
            and saved.get("selection_source") == "saved_default",
            "Ordinary user's identity-specific default was not saved",
        )
        readback = detail(cli.json(["tenant", "current"]))
        common.require(
            all(
                readback.get(key) == saved.get(key)
                for key in ("identity", "tenant_id", "selection_source")
            ),
            "Ordinary user's saved default did not survive the next command",
        )
        scoped_capabilities(cli, fixture["ordinary_tenant_id"])
    finally:
        # Restore even on a refused command or partial fixture write. The outer
        # session handoff must never replace the original login with this fixture.
        for path, content in original.items():
            path.write_bytes(content)
            os.chmod(path, 0o600)
    restored = detail(cli.json(["tenant", "current"]))
    common.require(
        all(
            restored.get(key) == before.get(key)
            for key in ("identity", "tenant_id", "selection_source")
        ),
        "Switching back did not restore the original user's saved default",
    )
    evidence["user_switch"] = {
        "original_login_user_id": before["identity"],
        "original_tenant_id": before["tenant_id"],
        "ordinary_login_user_id": fixture["ordinary_login_user_id"],
        "ordinary_tenant_id": fixture["ordinary_tenant_id"],
        "ordinary_identity_verified": True,
        "ordinary_default_isolated": True,
        "original_default_preserved": True,
        "original_session_restored": True,
        "qualification": "served CLI default isolation with legitimate session fixture setup; no login, inference or revocation acceptance claim",
    }


def execute(config, evidence):
    common.require(config.get("cli_path"), "install_auth did not retain the served CLI")
    with tempfile.TemporaryDirectory(prefix="adp-tenant-isolation-") as directory:
        home = Path(directory)
        os.chmod(home, 0o700)
        env = common.clean_env(config, HOME=home)
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            path = home / key
            path.mkdir(mode=0o700)
            env[key] = str(path)
        session = common.load_session(config)
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        settings = session.get("cli_config") or {}
        if config.get("mode") == "isolation":
            common.require(
                settings.get("refresh_via") == "gateway"
                or (settings.get("client_id") and settings.get("user_pool_id")),
                "Install fixture did not retain refresh configuration",
            )
        if settings:
            (home / ".bedrock-gateway/config.json").write_text(
                json.dumps({**settings, "gateway_url": config["gateway_url"]})
            )
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=60)
        try:
            if config.get("mode") == "isolation":
                fixture = config.get("tenant_isolation") or {}
                isolated_reads(cli, fixture, evidence)
                if fixture.get("user_switch"):
                    user_switch_reads(
                        cli, config, fixture["user_switch"], home, evidence
                    )
            else:
                smoke(cli, evidence)
        finally:
            # The disposable fixture shares one rotating login. Preserve any
            # refresh for later journeys before deleting this temporary HOME.
            refreshed = json.loads((home / ".bedrock-gateway/tokens.json").read_text())
            common.save_session(
                {**session, **refreshed}, work_dir=Path(config["session_ref"]).parent
            )
    evidence["detail"] = {
        key: value
        for key, value in evidence.items()
        if key not in {"success", "stage", "transcript", "detail"}
    }
    evidence.update(stage="complete", success=True)
