"""D04: explicit bounded owned-document lifecycle; input cleanup stays with caller."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import common
from capability_contrast import _write_session
from knowledge_lifecycle_plan import (
    recovery_plan,
    validate_dispatch_fixture,
    watch_events,
)


def detail(value):
    return value.get("detail") or {} if isinstance(value, dict) else {}


def source_digest(source):
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def find_source(cli, source):
    matches = []
    for page in range(1, 11):
        result = cli.json(
            [
                "knowledge",
                "list",
                "--scope",
                "personal",
                "--type",
                "doc",
                "--page",
                str(page),
                "--page-size",
                "100",
            ]
        )
        value = detail(result)
        rows = value.get("items")
        common.require(
            result.get("status") == "ok" and isinstance(rows, list),
            "Incomplete personal document listing",
        )
        matches.extend(
            row for row in rows if row.get("source_ref_sha256") == source_digest(source)
        )
        if value.get("has_more") is False:
            common.require(len(matches) <= 1, "Exact owned source is ambiguous")
            return matches[0] if matches else None
    raise common.RemoteError("Personal document listing exceeded bounded pagination")


def watch(cli, asset):
    argv = [
        cli.binary,
        "knowledge",
        "watch",
        asset,
        "--timeout",
        "300",
        "--interval",
        "5",
        "--json",
    ]
    cli.transcript.append(common.sanitize(argv))
    code, stdout, _ = common.bounded(argv, env=cli.env, timeout=330)
    common.require(code in {0, 4, 5}, "Knowledge watch process failed")
    return watch_events(stdout, asset)


def execute(config, evidence):
    fixture = config.get("knowledge_lifecycle") or {}
    validate_dispatch_fixture(fixture)
    common.require(
        config.get("test_user_id") == fixture.get("login_user_id"),
        "Knowledge fixture must use installed login identity",
    )
    plan = recovery_plan(config)
    common.require(
        config.get("recovery_plan") == plan,
        "Durable caller knowledge plan required before dispatch",
    )
    state = {
        "recovery_plan": plan,
        "asset_id": None,
        "phase": "not_started",
        "checks": [],
        "input_cleanup_ready": False,
        "source_receipt": plan["source_receipt"],
        "qualification": "Served CLI owned document lifecycle; caller deletes exact input only after terminal proof. Index/graph outputs retained. Spend bounds are externally verified, not enforced by this script.",
    }
    evidence["detail"] = state
    recovery = Path(config["work_dir"]) / ("knowledge-" + plan["registration_key"])
    recovery.mkdir(mode=0o700, exist_ok=False)
    record = recovery / "recovery.json"

    def save():
        with record.open("w") as stream:
            os.chmod(record, 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())

    save()
    with tempfile.TemporaryDirectory(prefix="adp-owned-knowledge-") as directory:
        home = Path(directory)
        env = common.clean_env(config, HOME=home, ADP_TENANT=fixture["tenant_id"])
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "CODEX_HOME",
        ):
            folder = home / name
            folder.mkdir(mode=0o700)
            env[name] = str(folder)
        env["BG_CONFIG_DIR"] = str(home / ".bedrock-gateway")
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=60)
        principal = detail(cli.json(["models", "mappings", "list"]))
        common.require(
            principal.get("principal_id") == fixture["canonical_user_id"]
            and principal.get("tenant_id") == fixture["tenant_id"],
            "Knowledge fixture selected another owner or tenant",
        )
        common.require(
            find_source(cli, plan["source_ref"]) is None,
            "Prior owned source exists; reconcile instead of restarting",
        )
        body = home / "document.json"
        body.write_text(
            json.dumps(
                {
                    "asset_type": "doc",
                    "scope": "personal",
                    "source_ref": plan["source_ref"],
                    "display_name": "ADP owned CLI evaluation",
                }
            )
        )
        add = [
            "knowledge",
            "add",
            "--file",
            str(body),
            "--key",
            plan["registration_key"],
        ]
        common.require(
            cli.json([*add, "--dry-run"]).get("status") == "preview",
            "Knowledge preview failed",
        )
        common.require(
            find_source(cli, plan["source_ref"]) is None, "Preview created a source"
        )
        state["checks"].append("read_only_preview")
        asset = None
        try:
            state["phase"] = "registration_attempted"
            save()
            cli.run([*add, "--yes"], expected=None)
            row = find_source(cli, plan["source_ref"])
            common.require(
                row and row.get("id"),
                "Registration outcome unresolved; retain exact source intent",
            )
            asset = row["id"]
            state["asset_id"] = asset
            save()
            observed = detail(cli.json(["knowledge", "show", asset]))
            common.require(
                observed.get("id") == asset
                and observed.get("source_ref_sha256")
                == source_digest(plan["source_ref"]),
                "Registered source readback mismatch",
            )
            first = watch(cli, asset)
            state["initial_watch"] = first
            save()
            common.require(
                first[-1]["status"] == "ok", "Initial indexing did not prove usability"
            )
            initial_run = first[-1]["detail"]["run_id"]
            state["initial_run_id"] = initial_run
            state["checks"].append("registered_and_usable")
            reindex = ["knowledge", "reindex", asset, "--key", plan["reindex_key"]]
            common.require(
                cli.json([*reindex, "--dry-run"]).get("status") == "preview",
                "Reindex preview failed",
            )
            state["phase"] = "reindex_attempted"
            state["reindex_started"] = True
            save()
            ack = cli.json([*reindex, "--yes"], expected=None)
            replay = cli.json([*reindex, "--yes"], expected=None)
            common.require(
                ack.get("status") == replay.get("status") == "pending"
                and all(
                    detail(ack).get(key) == detail(replay).get(key)
                    for key in ("id", "asset_type", "source_ref_sha256", "created_at")
                )
                and detail(ack).get("id") == asset
                and detail(ack).get("source_ref_sha256")
                == source_digest(plan["source_ref"]),
                "Same-key reindex receipt mismatch",
            )
            state["phase"] = "reindex_acknowledged"
            save()
            state["checks"].append("same_key_reindex_receipt")
            second = watch(cli, asset)
            state["reindex_watch"] = second
            save()
            common.require(
                second[-1]["status"] == "ok"
                and second[-1]["detail"]["run_id"] != initial_run,
                "Reindex did not prove a new successful run",
            )
            state["checks"].append("new_run_usable")
            state["phase"] = "checks_complete"
        finally:
            # A failed/unknown dispatch can still be in flight. Never remove its input.
            if asset:
                current = cli.json(["knowledge", "status", asset], expected=None)
                state["final_status"] = current
                terminal = detail(current).get("status") in {
                    "complete",
                    "indexed",
                    "failed",
                    "error",
                }
                if state.get("reindex_started"):
                    terminal = (
                        terminal
                        and bool(detail(current).get("run_id"))
                        and detail(current).get("run_id") != state["initial_run_id"]
                        and detail(current).get("run_status")
                        in {
                            "complete",
                            "completed",
                            "succeeded",
                            "success",
                            "verified",
                            "failed",
                            "error",
                        }
                    )
                if terminal and state["phase"] != "reindex_attempted":
                    result = cli.json(["knowledge", "delete", asset, "--yes"])
                    common.require(
                        result.get("status") == "ok"
                        and detail(result).get("soft_deleted") is True,
                        "Owned asset removal unconfirmed",
                    )
                    common.require(
                        find_source(cli, plan["source_ref"]) is None,
                        "Removed owned asset remains visible",
                    )
                    state["input_cleanup_ready"] = True
                    state["registry_cleanup"] = "soft_deleted_verified"
                else:
                    state["registry_cleanup"] = "retained_nonterminal"
            save()
        common.require(
            state["input_cleanup_ready"], "Caller input cleanup is not yet safe"
        )
        state["checks"].append("owned_registry_cleanup")
        save()
