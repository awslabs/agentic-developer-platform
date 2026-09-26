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
from capability_contrast import _write_session


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
                isolated_reads(cli, config.get("tenant_isolation") or {}, evidence)
            else:
                smoke(cli, evidence)
        finally:
            # The disposable fixture shares one rotating login. Preserve any
            # refresh for later journeys before deleting this temporary HOME.
            refreshed = json.loads((home / ".bedrock-gateway/tokens.json").read_text())
            common.save_session(
                {**session, **refreshed}, work_dir=Path(config["session_ref"]).parent
            )
