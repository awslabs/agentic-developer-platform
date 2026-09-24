#!/usr/bin/env python3
"""E18: served Superplane CLI against a real mounted domain service."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import common


def _require_durable_recovery(evidence):
    """Defense for direct/stale dispatches that bypass orchestrator preflight.

    This is a missing implementation, not a configurable fixture. Before this
    can return, the CLI's original operation receipt must reach durable recovery
    storage before POST and cleanup must recover under the original principal.
    The instance currently has read-only access to the run's S3 bundle, and a
    TemporaryDirectory plus finally cannot survive termination or a lost reply.
    """
    evidence.update(stage="recovery_preflight", success=False)
    evidence.setdefault("resources", [])
    evidence.setdefault("removed", [])
    message = (
        "E18 durable recovery is not implemented: CLI operation receipts must be "
        "published before mutation and recovered by exact resource identity under "
        "the original ordinary or administrator principal. Superplane mutations "
        "remain disabled until the producer and scoped recovery deleters exist."
    )
    evidence["detail"] = {
        "success": False,
        "unimplemented": True,
        "blocker": "superplane_durable_recovery_unimplemented",
        "message": message,
    }
    raise common.RemoteError(message)


def _home(config, root, session):
    home = Path(root)
    gateway = home / ".bedrock-gateway"
    gateway.mkdir(mode=0o700)
    auth_config = {"gateway_url": config["gateway_url"]}
    for key in ("client_id", "user_pool_id", "region", "refresh_via"):
        if session.get(key):
            auth_config[key] = session[key]
    (gateway / "config.json").write_text(json.dumps(auth_config))
    tokens = gateway / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                "access_token": session["access_token"],
                "id_token": session.get("id_token", ""),
                "refresh_token": session.get("refresh_token", ""),
                "expires_at": session.get("expires_at", 0),
            }
        )
    )
    tokens.chmod(0o600)
    return home, common.clean_env(
        config,
        HOME=str(home),
        AWS_CONFIG_FILE=str(home / "aws-config"),
        AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
    )


def _detail(payload):
    common.require(
        isinstance(payload, dict), "Superplane command returned no JSON envelope"
    )
    return payload.get("detail") or {}


def _rows(payload, key):
    if isinstance(payload, list):
        return payload
    return (payload or {}).get(key) or (payload or {}).get("items") or []


def _claims(token):
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded))
    except (IndexError, ValueError, TypeError):
        claims = {}
    common.require(claims.get("sub"), "E18 identity token has no principal subject")
    return claims


def _ordinary_session(config):
    scoped = {
        **config,
        "credential_secret": config["superplane"]["ordinary_session_secret_name"],
    }
    env = common.clean_env(config)
    session = {
        key: common.fixture_secret(scoped, env, key)
        for key in (
            "access_token",
            "id_token",
            "refresh_token",
            "client_id",
            "user_pool_id",
            "region",
        )
    }
    session["refresh_via"] = "gateway"
    session["expires_at"] = int(common.fixture_secret(scoped, env, "expires_at"))
    common.require(
        session["expires_at"] > int(time.time()) + 600,
        "E18 ordinary session expires too soon for the bounded journey",
    )
    return session


def _identity(session):
    claims = _claims(session.get("id_token") or session["access_token"])
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return {
        "principal_id": claims["sub"],
        "tenant_id": claims.get("custom:org_id") or claims.get("org_id") or "",
        "role": claims.get("custom:role") or "member",
        "groups": sorted(str(group) for group in groups),
    }


def _update_rollback(config, evidence, root, env):
    """Exercise updater recovery on a private copy, never the shared install."""
    source = Path(config["cli_path"]).parent
    copied = Path(root) / "update-copy"
    shutil.copytree(source, copied)
    cli = common.Cli(copied / "adp", env, evidence["transcript"], timeout=300)
    cli.run(["update"], expected=0, json_output=False)
    common.require(
        any(copied.glob("*.prev")),
        "E18 update created no rollback generation in its isolated install",
    )
    cli.run(["update", "--rollback"], expected=0, json_output=False)
    common.require(
        not any(copied.glob("*.prev")),
        "E18 rollback left an unconsumed previous generation",
    )
    return cli


def _workspace_deletion_complete(config, fixture, token, workspace_id):
    response_status, payload = common.api(
        config,
        f"{fixture['base_path']}/workspaces/{workspace_id}",
        token,
        expect=(200, 404),
    )
    if response_status == 404:
        return True
    return isinstance(payload, dict) and payload.get("status") == "Deleted"


def execute(config, evidence):
    _require_durable_recovery(evidence)
    os.umask(0o077)
    common.assert_owned_instance(config)
    session = common.load_session(config)
    fixture = config.get("superplane") or {}
    for key in (
        "base_path",
        "ordinary_session_secret_name",
        "model_name",
        "aws_connection_id",
    ):
        common.require(fixture.get(key), f"E18 received no superplane.{key}")
    common.require(config.get("cli_path"), "E18 received no served CLI path")

    label = config["evaluation_id"].lower().replace("_", "-")[-40:]
    workspace_name = f"e18-{label}"[-63:]
    deployment_name = "e18-model"
    provider_name = f"e18-{label}"[-80:]
    workspace_id = None
    provider_record = None
    provider_ids = {}
    account_record = None
    account_name = f"e18-{label}"[-64:]
    deployment_id = None
    deployment_active = False
    ordinary_session = _ordinary_session(config)
    ordinary_identity = _identity(ordinary_session)
    admin_identity = _identity(session)
    common.require(
        ordinary_identity["principal_id"] != admin_identity["principal_id"],
        "E18 ordinary and admin sessions resolve to the same principal",
    )
    common.require(
        ordinary_identity["tenant_id"]
        and ordinary_identity["tenant_id"] == admin_identity["tenant_id"],
        "E18 ordinary and admin sessions are not authorized in the same tenant",
    )
    common.require(
        ordinary_identity["role"] != "platform_admin"
        and "admins" not in ordinary_identity["groups"],
        "E18 ordinary fixture has administrator authority",
    )
    common.require(
        admin_identity["role"] == "platform_admin"
        or "admins" in admin_identity["groups"],
        "E18 inherited session is not the verified platform administrator",
    )
    token = ordinary_session["access_token"]
    cleanup = []
    removed = set()

    with (
        tempfile.TemporaryDirectory(prefix="adp-superplane-") as ordinary_root,
        tempfile.TemporaryDirectory(prefix="adp-superplane-admin-") as admin_root,
    ):
        _ordinary_home, ordinary_env = _home(config, ordinary_root, ordinary_session)
        cli = common.Cli(
            Path(config["cli_path"]), ordinary_env, evidence["transcript"], timeout=600
        )
        _admin_home, admin_env = _home(config, admin_root, session)
        admin_cli = common.Cli(
            Path(config["cli_path"]), admin_env, evidence["transcript"], timeout=600
        )

        try:
            evidence["stage"] = "update_rollback"
            rollback_cli = _update_rollback(
                config, evidence, ordinary_root, ordinary_env
            )
            cli = rollback_cli
            cli.json(["superplane", "workspace", "list"])
            evidence["checks"].append("isolated_update_and_rollback_completed")
            evidence["checks"].append("rolled_back_cli_superplane_command_completed")

            evidence["stage"] = "workspace_create"
            preview = cli.json(
                [
                    "superplane",
                    "workspace",
                    "create",
                    "--name",
                    workspace_name,
                    "--budget-daily",
                    "5",
                    "--budget-gpus",
                    "1",
                    "--dry-run",
                ]
            )
            common.require(
                _detail(preview).get("performed") == "nothing",
                "workspace dry-run did not prove no-write behavior",
            )
            created = cli.json(
                [
                    "superplane",
                    "workspace",
                    "create",
                    "--name",
                    workspace_name,
                    "--budget-daily",
                    "5",
                    "--budget-gpus",
                    "1",
                    "--yes",
                ],
                expected=None,
            )
            common.require(
                created.get("status") in {"ok", "pending"},
                "workspace create returned neither accepted nor completed",
            )
            workspace_id = str(_detail(created).get("id") or "")
            if not workspace_id:
                listed = cli.json(["superplane", "workspace", "list"])
                matches = [
                    row
                    for row in _detail(listed).get("workspaces") or []
                    if row.get("name") == workspace_name
                ]
                common.require(
                    len(matches) == 1,
                    "created workspace was not uniquely visible through CLI list",
                )
                workspace_id = str(matches[0].get("id") or "")
            uuid.UUID(workspace_id)

            described = cli.json(
                ["superplane", "workspace", "describe", "--workspace", workspace_name]
            )
            common.require(
                str(_detail(described).get("id")) == workspace_id,
                "workspace name did not resolve to the created UUID",
            )
            workspace_ready = common.wait_for(
                lambda: str(
                    _detail(
                        cli.json(
                            [
                                "superplane",
                                "workspace",
                                "describe",
                                "--workspace",
                                workspace_id,
                            ]
                        )
                    ).get("status")
                    or ""
                ).lower()
                in {"active", "ready", "succeeded"},
                timeout=600,
                interval=10,
            )
            common.require(
                workspace_ready,
                "workspace did not become active in the bounded ten-minute window",
            )
            kubeconfig = cli.json(
                ["superplane", "workspace", "kubeconfig", "--workspace", workspace_name]
            )
            common.require(
                _detail(kubeconfig).get("kubeconfig")
                and _detail(kubeconfig).get("expires_at"),
                "kubeconfig omitted content or expiry",
            )
            cli.json(["superplane", "cost", "--workspace", workspace_name])
            events = cli.json(
                [
                    "superplane",
                    "events",
                    "--resource-type",
                    "workspace",
                    "--limit",
                    "10",
                ]
            )
            common.require(
                isinstance(_detail(events).get("events"), list),
                "events response was not pageable",
            )

            evidence["stage"] = "authorization"
            denied = cli.json(
                [
                    "superplane",
                    "quota",
                    "set",
                    "--workspace",
                    workspace_name,
                    "--max-gpus",
                    "1",
                    "--max-nodes",
                    "1",
                    "--max-cost-per-day",
                    "5",
                    "--yes",
                ],
                expected=3,
            )
            common.require(
                (denied.get("error") or {}).get("code"),
                "ordinary identity quota mutation was not explicitly denied",
            )
            admin_cli.json(
                [
                    "superplane",
                    "quota",
                    "set",
                    "--workspace",
                    workspace_id,
                    "--max-gpus",
                    "1",
                    "--max-nodes",
                    "1",
                    "--max-cost-per-day",
                    "5",
                    "--yes",
                ]
            )
            admin_cli.json(["superplane", "quota", "show", "--workspace", workspace_id])
            evidence["identities"] = {
                "ordinary": ordinary_identity,
                "admin": admin_identity,
                "effective_authorization": {
                    "ordinary_quota_set": "denied",
                    "admin_quota_set": "allowed",
                },
            }
            foreign = cli.json(
                [
                    "superplane",
                    "workspace",
                    "describe",
                    "--workspace",
                    "00000000-0000-4000-8000-000000000000",
                ],
                expected=5,
            )
            common.require(
                foreign.get("status") == "failed",
                "foreign/absent workspace was not rejected",
            )

            evidence["stage"] = "deployment"
            deployment = cli.json(
                [
                    "superplane",
                    "deploy",
                    "create",
                    "--workspace",
                    workspace_name,
                    "--name",
                    deployment_name,
                    "--model",
                    fixture["model_name"],
                    "--replicas",
                    "1",
                    "--gpu-per-replica",
                    "1",
                    "--yes",
                ],
                expected=None,
            )
            common.require(
                deployment.get("status") in {"ok", "pending"},
                "deployment create returned neither accepted nor completed",
            )
            deployment_active = True
            deployment_id = _detail(deployment).get("deployment_id")
            deadline = time.monotonic() + 300
            while True:
                deployments = cli.json(
                    ["superplane", "deploy", "list", "--workspace", workspace_name]
                )
                if any(
                    row.get("name") == deployment_name
                    for row in _detail(deployments).get("deployments") or []
                ):
                    break
                common.require(
                    time.monotonic() < deadline,
                    "deployment never appeared in the bounded five-minute window",
                )
                time.sleep(10)
            cli.json(
                [
                    "superplane",
                    "deploy",
                    "delete",
                    "--workspace",
                    workspace_name,
                    "--name",
                    deployment_name,
                    "--yes",
                ]
            )
            deployment_active = False
            deployment_absent = common.wait_for(
                lambda: not any(
                    row.get("name") == deployment_name
                    for row in _detail(
                        cli.json(
                            [
                                "superplane",
                                "deploy",
                                "list",
                                "--workspace",
                                workspace_name,
                            ]
                        )
                    ).get("deployments")
                    or []
                ),
                timeout=120,
                interval=5,
            )
            common.require(
                deployment_absent, "deployment remained visible after CLI delete"
            )
            cleanup.append("deployment_absence_verified")
            removed.add(("superplane_deployment", deployment_id or deployment_name))

            evidence["stage"] = "provider_handoff"
            before_status, before_payload = common.api(
                config, "/auth/credentials", token
            )
            before_ids = {
                str(row.get("id")) for row in _rows(before_payload, "credentials")
            }
            failed = cli.json(
                [
                    "superplane",
                    "provider",
                    "add",
                    "--name",
                    provider_name + "-rejected",
                    "--provider",
                    "x" * 51,
                    "--stdin",
                    "--yes",
                ],
                expected=5,
                stdin_text=secrets.token_urlsafe(32),
            )
            common.require(
                (failed.get("error") or {}).get("code") == "provider_add_rolled_back",
                "definitive second-stage rejection did not compensate",
            )
            _after_status, after_payload = common.api(
                config, "/auth/credentials", token
            )
            after_ids = {
                str(row.get("id")) for row in _rows(after_payload, "credentials")
            }
            common.require(
                after_ids == before_ids,
                "failed provider registration left a vault credential behind",
            )

            added = cli.json(
                [
                    "superplane",
                    "provider",
                    "add",
                    "--name",
                    provider_name,
                    "--provider",
                    "nebius",
                    "--stdin",
                    "--yes",
                ],
                stdin_text=secrets.token_urlsafe(32),
            )
            added_detail = _detail(added)
            provider_record = str(added_detail.get("domain_record") or "")
            vault_reference = str(added_detail.get("adp_credential_id") or "")
            provider_ids = {
                "domain_record": provider_record,
                "adp_credential_id": vault_reference,
            }
            common.require(
                provider_record
                and vault_reference
                and provider_record != vault_reference,
                "provider handoff did not return distinct domain and vault IDs",
            )
            providers = cli.json(["superplane", "provider", "list"])
            common.require(
                any(
                    str(row.get("id")) == provider_record
                    and str(row.get("adp_credential_id")) == vault_reference
                    for row in _detail(providers).get("providers") or []
                ),
                "provider list did not preserve both authoritative identifiers",
            )

            account_command = [
                "superplane",
                "aws-onboard",
                "register",
                "--account-id",
                str(config["destination_account"]),
                "--credential-id",
                fixture["aws_connection_id"],
                "--name",
                account_name,
            ]
            account_preview = admin_cli.json([*account_command, "--dry-run"])
            common.require(
                _detail(account_preview).get("performed") == "nothing",
                "account dry-run did not prove no-write behavior",
            )
            account = admin_cli.json([*account_command, "--yes"])
            account_record = str(
                (_detail(account).get("account") or {}).get("id") or ""
            )
            common.require(
                account_record, "account handoff returned no domain record id"
            )
            retried_account = admin_cli.json([*account_command, "--yes"])
            common.require(
                str((_detail(retried_account).get("account") or {}).get("id"))
                == account_record,
                "account retry created or returned a different domain record",
            )
            accounts = admin_cli.json(["superplane", "account", "list"])
            common.require(
                any(
                    str(row.get("id")) == account_record
                    and str(row.get("account_id")) == str(config["destination_account"])
                    and fixture["aws_connection_id"]
                    in (row.get("adp_credential_ids") or [])
                    for row in _detail(accounts).get("accounts") or []
                ),
                "account list did not confirm the credential/account handoff",
            )
            admin_cli.json(["superplane", "account", "delete", account_record, "--yes"])
            accounts_after = admin_cli.json(["superplane", "account", "list"])
            common.require(
                not any(
                    str(row.get("id")) == account_record
                    for row in _detail(accounts_after).get("accounts") or []
                ),
                "account domain record remained visible after CLI delete",
            )
            cleanup.append("account_domain_absence_verified")
            removed.add(("superplane_account", account_record))
            cli.json(["superplane", "provider", "delete", provider_record, "--yes"])
            providers_after = cli.json(["superplane", "provider", "list"])
            common.require(
                not any(
                    str(row.get("id")) == provider_record
                    for row in _detail(providers_after).get("providers") or []
                ),
                "provider domain record remained visible after CLI delete",
            )
            _vault_status, vault_after = common.api(config, "/auth/credentials", token)
            common.require(
                vault_reference
                not in {
                    str(row.get("id")) for row in _rows(vault_after, "credentials")
                },
                "provider vault metadata remained visible after CLI delete",
            )
            provider_record = None
            cleanup.append("provider_domain_and_vault_absence_verified")
            removed.add(("superplane_provider", provider_ids["domain_record"]))
            removed.add(("adp_vault_credential", provider_ids["adp_credential_id"]))

            evidence["checks"].extend(
                [
                    "served_cli_real_workspace_create_read_kubeconfig_cost_events",
                    "ordinary_and_admin_authorization_separated",
                    "bounded_deployment_created_listed_and_deleted",
                    "provider_two_id_handoff_and_compensation_verified",
                    "account_create_read_retry_delete_handoff_verified",
                ]
            )
            evidence["authoritative_state"] = {
                "workspace_id": workspace_id,
                "deployment_name": deployment_name,
                "deployment_id": deployment_id,
                "provider_ids": provider_ids,
                "provider_deleted": True,
                "account_record": account_record,
                "account_deleted": True,
                "evaluation_id": config["evaluation_id"],
            }
        finally:
            evidence["stage"] = "cleanup"
            if deployment_active and workspace_id:
                try:
                    cli.json(
                        [
                            "superplane",
                            "deploy",
                            "delete",
                            "--workspace",
                            workspace_id,
                            "--name",
                            deployment_name,
                            "--yes",
                        ]
                    )
                    deployment_absent = common.wait_for(
                        lambda: not any(
                            row.get("name") == deployment_name
                            for row in _detail(
                                cli.json(
                                    [
                                        "superplane",
                                        "deploy",
                                        "list",
                                        "--workspace",
                                        workspace_id,
                                    ]
                                )
                            ).get("deployments")
                            or []
                        ),
                        timeout=120,
                        interval=5,
                    )
                    common.require(
                        deployment_absent,
                        "emergency deployment cleanup was not observable",
                    )
                    cleanup.append("deployment_emergency_absence_verified")
                    removed.add(
                        ("superplane_deployment", deployment_id or deployment_name)
                    )
                except Exception as exc:  # noqa: BLE001
                    cleanup.append(f"deployment_cleanup_failed:{type(exc).__name__}")
            if account_record and ("superplane_account", account_record) not in removed:
                try:
                    admin_cli.json(
                        ["superplane", "account", "delete", account_record, "--yes"]
                    )
                    accounts_after = admin_cli.json(["superplane", "account", "list"])
                    common.require(
                        not any(
                            str(row.get("id")) == account_record
                            for row in _detail(accounts_after).get("accounts") or []
                        ),
                        "emergency account cleanup was not observable",
                    )
                    cleanup.append("account_emergency_absence_verified")
                    removed.add(("superplane_account", account_record))
                except Exception as exc:  # noqa: BLE001
                    cleanup.append(f"account_cleanup_failed:{type(exc).__name__}")
            if provider_record:
                try:
                    cli.json(
                        ["superplane", "provider", "delete", provider_record, "--yes"]
                    )
                    providers_after = cli.json(["superplane", "provider", "list"])
                    _status, vault_after = common.api(
                        config, "/auth/credentials", token
                    )
                    domain_absent = not any(
                        str(row.get("id")) == provider_record
                        for row in _detail(providers_after).get("providers") or []
                    )
                    vault_absent = provider_ids.get("adp_credential_id") not in {
                        str(row.get("id")) for row in _rows(vault_after, "credentials")
                    }
                    common.require(
                        domain_absent and vault_absent,
                        "emergency provider cleanup was not absent from both registries",
                    )
                    cleanup.append("provider_emergency_absence_verified")
                    removed.add(("superplane_provider", provider_ids["domain_record"]))
                    removed.add(
                        ("adp_vault_credential", provider_ids["adp_credential_id"])
                    )
                except Exception as exc:  # noqa: BLE001
                    cleanup.append(f"provider_cleanup_failed:{type(exc).__name__}")
            if workspace_id:
                try:
                    common.api(
                        config,
                        f"{fixture['base_path']}/workspaces/{workspace_id}",
                        token,
                        method="DELETE",
                        expect=(200, 204),
                    )
                    workspace_deleted = common.wait_for(
                        lambda: _workspace_deletion_complete(
                            config, fixture, token, workspace_id
                        ),
                        timeout=120,
                        interval=5,
                    )
                    common.require(
                        workspace_deleted,
                        "workspace did not reach the authoritative Deleted state",
                    )
                    cleanup.append("workspace_terminal_deletion_verified")
                    removed.add(("superplane_workspace", workspace_id))
                except Exception as exc:  # noqa: BLE001
                    cleanup.append(f"workspace_cleanup_failed:{type(exc).__name__}")
            evidence["cleanup"] = cleanup
            common.require(
                not any("failed:" in item for item in cleanup),
                "E18 cleanup was not verified",
            )

    evidence["resources"] = [
        ["superplane_workspace", workspace_id],
        ["superplane_deployment", deployment_id or deployment_name],
        ["superplane_provider", provider_ids.get("domain_record")],
        ["superplane_account", account_record],
        ["adp_vault_credential", provider_ids.get("adp_credential_id")],
    ]
    evidence["resources"] = [item for item in evidence["resources"] if item[1]]
    evidence["removed"] = [list(item) for item in sorted(removed)]
    common.require(
        set(map(tuple, evidence["resources"])) == removed,
        "E18 did not authoritatively verify every recorded resource as removed",
    )
    evidence["detail"] = {
        "success": True,
        "checks": evidence["checks"],
        "stage_reached": evidence["stage"],
        "authoritative_state": evidence["authoritative_state"],
        "cleanup": evidence["cleanup"],
    }
    evidence["success"] = True
