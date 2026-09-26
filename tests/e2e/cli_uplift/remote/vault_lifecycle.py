"""Zero-inference, run-owned vault CRUD and unverified identity lifecycle."""

import json
import os
import tempfile
from pathlib import Path

import common
from capability_contrast import _write_session
from vault_lifecycle_plan import recovery_plan, plan_digest


def detail(value):
    return value.get("detail") or {} if isinstance(value, dict) else {}


def entries(cli, area):
    value = detail(cli.json([area, "list"]))
    common.require(
        value.get("complete") is True and isinstance(value.get("items"), list),
        "Vault metadata listing is incomplete",
    )
    rows = value["items"]
    common.require(
        all(
            isinstance(row, dict)
            and row.get("id")
            and not {
                "value",
                "secret_arn",
                "access_token",
                "refresh_token",
            }.intersection(row)
            for row in rows
        ),
        "Vault metadata exposed forbidden fields or omitted identity",
    )
    return rows


def target(cli, area, predicate):
    rows = [row for row in entries(cli, area) if predicate(row)]
    common.require(len(rows) <= 1, "Owned vault target is ambiguous")
    return rows[0] if rows else None


def execute(config, evidence):
    fixture = config.get("vault_lifecycle")
    common.require(
        isinstance(fixture, dict) and fixture.get("owned_mutations_authorized") is True,
        "Explicit run-owned vault fixture authorization required",
    )
    common.require(
        config.get("evaluation_id")
        and config.get("work_dir")
        and config.get("cli_path"),
        "Installed fixture and stable evaluation ID required",
    )
    common.require(
        config.get("test_user_id") == fixture.get("login_user_id")
        and fixture.get("canonical_user_id")
        and fixture.get("tenant_id"),
        "Vault diagnostic must use the installed fixture human",
    )
    plan = recovery_plan(config)
    common.require(
        config.get("recovery_plan") == plan,
        "Vault recovery plan must be durably recorded by the caller before dispatch",
    )
    digest = plan_digest(plan)
    operation = plan["operation_id"]
    service, label = plan["service"], plan["label"]
    provider, provider_id = plan["provider"], plan["provider_user_id"]
    state = {
        "operation_id": operation,
        "recovery_plan": plan,
        "provider": provider,
        "provider_user_id": provider_id,
        "gateway": config["gateway_url"],
        "tenant_id": fixture["tenant_id"],
        "canonical_user_id": fixture["canonical_user_id"],
        "entry_phase": "not_started",
        "identity_phase": "not_started",
        "checks": [],
        "qualification": "Run-owned user-scope metadata CRUD and unverified claim only; no secret reveal, provider consent, inference or role changes",
    }
    evidence["detail"] = state
    recovery_dir = Path(config["work_dir"]) / ("vault-" + operation)
    recovery_dir.mkdir(mode=0o700, exist_ok=True)
    path = recovery_dir / "recovery.json"
    if not path.exists():
        path.touch(mode=0o600, exist_ok=False)

    def save():
        with path.open("w") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())

    save()  # Predetermined entry UUID and claim tuple survive lost write receipts.
    with tempfile.TemporaryDirectory(prefix="adp-owned-vault-") as directory:
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
            principal.get("tenant_id") == fixture["tenant_id"]
            and principal.get("principal_id") == fixture["canonical_user_id"],
            "Vault fixture resolved a different canonical owner or tenant",
        )

        def by_id(row):
            return row.get("id") == operation

        def by_claim(row):
            return (
                row.get("provider") == provider
                and row.get("provider_user_id") == provider_id
            )

        baseline_entries = {
            row["id"]: row for row in entries(cli, "credential") if not by_id(row)
        }
        baseline_links = {
            row["id"]: row for row in entries(cli, "identity") if not by_claim(row)
        }
        common.require(
            target(cli, "credential", by_id) is None
            and target(cli, "identity", by_claim) is None,
            "A prior same-evaluation vault target exists; reconcile retained recovery before restarting",
        )
        add = [
            "credential",
            "add",
            "--operation-id",
            operation,
            "--service",
            service,
            "--label",
            label,
            "--type",
            "api_key",
            "--scope",
            "user",
            "--value-stdin",
        ]
        synthetic = "ADP_SYNTHETIC_NOT_A_PROVIDER_CREDENTIAL_" + digest
        checks_complete = False
        entry_confirmed = False
        try:
            common.require(
                cli.json([*add, "--dry-run"]).get("status") == "dry_run",
                "Vault add preview was not read-only",
            )
            state["entry_phase"] = "create_attempted"
            save()
            cli.run([*add, "--yes"], expected=None, stdin_text=synthetic)
            row = target(cli, "credential", by_id)
            common.require(
                row
                and row.get("scope") == "user"
                and row.get("service") == service
                and row.get("label") == label,
                "Owned vault create outcome is unconfirmed; retain the operation ID",
            )
            entry_confirmed = True
            initial_revision = row.get("revision")
            common.require(
                isinstance(initial_revision, str) and initial_revision,
                "Vault create omitted its compare-and-set revision",
            )
            state.update(entry_phase="created", initial_revision=initial_revision)
            save()
            cli.run([*add, "--yes"], expected=None, stdin_text=synthetic)
            common.require(
                target(cli, "credential", by_id).get("revision") == initial_revision,
                "Identical operation replay changed vault metadata",
            )
            state["checks"].append("same-operation-create-replay")
            update = [
                "credential",
                "update",
                operation,
                "--label",
                label + "-updated",
                "--expected-revision",
                initial_revision,
            ]
            common.require(
                cli.json([*update, "--dry-run"]).get("status") == "dry_run",
                "Vault metadata preview failed",
            )
            state["entry_phase"] = "update_attempted"
            save()
            cli.run([*update, "--yes"], expected=None)
            changed = target(cli, "credential", by_id)
            common.require(
                changed
                and changed.get("label") == label + "-updated"
                and changed.get("revision") != initial_revision,
                "Vault metadata update was not observed",
            )
            state["updated_revision"] = changed["revision"]
            stale_code, stale = cli.run(
                [
                    "credential",
                    "update",
                    operation,
                    "--label",
                    label + "-stale",
                    "--expected-revision",
                    initial_revision,
                    "--yes",
                ],
                expected=None,
            )
            common.require(
                stale_code != 0
                and (stale or {}).get("error", {}).get("http_status") == 409,
                "Stale vault update was not refused with conflict",
            )
            unchanged = target(cli, "credential", by_id)
            common.require(
                unchanged == changed, "Refused stale update changed vault metadata"
            )
            state["checks"].extend(["metadata-update-readback", "stale-update-refused"])
            # Rotation is deliberately unsupported; this must be rejected before any write.
            refused_code, refusal = cli.run(
                ["credential", "update", operation, "--value-stdin", "--yes"],
                expected=None,
                stdin_text=synthetic,
            )
            common.require(
                refused_code == 1
                and (refusal or {}).get("error", {}).get("code") == "usage_error"
                and target(cli, "credential", by_id) == changed,
                "Unsupported secret rotation was not refused without mutation",
            )
            state["checks"].append("unsupported-rotation-refused")
            link = [
                "identity",
                "link",
                "--provider",
                provider,
                "--provider-user-id",
                provider_id,
            ]
            common.require(
                cli.json([*link, "--dry-run"]).get("status") == "dry_run",
                "Identity preview failed",
            )
            state["identity_phase"] = "claim_attempted"
            save()
            cli.run([*link, "--yes"], expected=None)
            claim = target(cli, "identity", by_claim)
            common.require(
                claim
                and claim.get("verification_method") == "self_asserted"
                and claim.get("verified_at") is None,
                "Owned identity claim must remain unverified",
            )
            state.update(identity_id=claim["id"], identity_phase="unverified")
            save()
            resume_code, resumed = cli.run([*link, "--resume"], expected=None)
            common.require(
                resume_code == 4 and detail(resumed).get("id") == claim["id"],
                "Unverified identity resume must remain pending on the same claim",
            )
            state["checks"].append("unverified-claim-readback")
            checks_complete = True
        finally:
            if state["identity_phase"] != "not_started":
                try:
                    claim = target(cli, "identity", by_claim)
                    if claim:
                        common.require(
                            claim.get("verification_method") == "self_asserted"
                            and claim.get("verified_at") is None,
                            "Owned claim gained external verification; do not unlink it automatically",
                        )
                        state.update(
                            identity_id=claim["id"], identity_phase="unlink_attempted"
                        )
                        save()
                        cli.json(
                            [
                                "identity",
                                "unlink",
                                claim["id"],
                                "--provider",
                                provider,
                                "--dry-run",
                            ]
                        )
                        cli.run(
                            [
                                "identity",
                                "unlink",
                                claim["id"],
                                "--provider",
                                provider,
                                "--yes",
                            ],
                            expected=None,
                        )
                    common.require(
                        target(cli, "identity", by_claim) is None,
                        "Owned identity unlink lacks absence proof",
                    )
                    state["identity_phase"] = "absent"
                except Exception:
                    state["identity_phase"] = "cleanup_pending"
            if state["entry_phase"] != "not_started":
                try:
                    row = target(cli, "credential", by_id)
                    if row:
                        common.require(
                            row.get("scope") == "user"
                            and row.get("service") == service,
                            "Owned entry changed scope; do not delete it automatically",
                        )
                        state["entry_phase"] = "delete_attempted"
                        save()
                        cli.json(["credential", "delete", operation, "--dry-run"])
                        cli.run(
                            ["credential", "delete", operation, "--yes"], expected=None
                        )
                        entry_confirmed = True
                    common.require(
                        target(cli, "credential", by_id) is None and entry_confirmed,
                        "Create/delete outcome unknown despite absent metadata",
                    )
                    state["entry_phase"] = "metadata_absent"
                except Exception:
                    state["entry_phase"] = "cleanup_pending"
            save()
            common.require(
                state["entry_phase"] in {"not_started", "metadata_absent"}
                and state["identity_phase"] in {"not_started", "absent"},
                "Owned vault cleanup remains pending; operation ID and claim tuple retained in diagnostic detail",
            )
        common.require(checks_complete, "Vault lifecycle did not complete all checks")
        common.require(
            {row["id"]: row for row in entries(cli, "credential")} == baseline_entries
            and {row["id"]: row for row in entries(cli, "identity")} == baseline_links,
            "Unrelated vault metadata changed during the diagnostic",
        )
        state["checks"].extend(
            [
                "identity-unlink-absence",
                "entry-delete-metadata-absence",
                "unrelated-metadata-retained",
            ]
        )
        state["deletion_limit"] = (
            "Metadata is absent; Secrets Manager recovery window still applies. No physical secret erasure claim."
        )
        save()
        evidence.update(success=True, stage="complete")
