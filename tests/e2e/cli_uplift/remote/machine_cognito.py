"""Opt-in D05 owned Cognito client; credentials stay in private temporary files."""

import json
import os
import stat

import common
from machine_lifecycle import detail, require_refusal


def execute(admin, ordinary, foreign, tenant, native, plan, root, state, save):
    private = root / "owned-cognito"
    private.mkdir(mode=0o700)
    spec = private / "spec.json"
    spec.write_text(json.dumps({"name": plan["name"], "scopes": plan["scopes"]}))
    os.chmod(spec, 0o600)
    first_file, replay_file = private / "first.json", private / "replay.json"
    value = {
        "plan": plan,
        "client_id": None,
        "phase": "prepared",
        "cleanup": "not_started",
        "checks": [],
    }
    state["cognito"] = value
    save()  # Caller plan was already retained externally before SSM.
    base = ["admin", "agent"]

    def argv(action, client=None, org=tenant):
        return [
            *base,
            action,
            *([client] if client else []),
            "--org",
            org,
            "--identity-type",
            "cognito-client",
        ]

    def register(path):
        return [
            *argv("register"),
            "--operation-id",
            plan["registration_id"],
            "--spec-file",
            str(spec),
            "--credential-file",
            str(path),
        ]

    def snapshot():
        row = detail(admin.json(argv("show", value["client_id"])))
        common.require(
            row.get("id") == value["client_id"]
            and row.get("client_id") == value["client_id"]
            and row.get("org_id") == tenant
            and row.get("identity_type") == "cognito-client"
            and row.get("name") == plan["name"]
            and row.get("scopes") == plan["scopes"]
            and not row.get("team_id")
            and not row.get("department_id")
            and isinstance(row.get("revision"), str)
            and row["revision"],
            "Owned Cognito identity changed; preserve for reconciliation",
        )
        return row

    def credential(path):
        metadata = path.lstat()
        common.require(
            stat.S_ISREG(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_uid == os.getuid(),
            "Credential file protection failed",
        )
        document = json.loads(path.read_text())
        common.require(
            document.get("client_id") == value["client_id"]
            and isinstance(document.get("client_secret"), str)
            and document["client_secret"],
            "Credential delivery incomplete",
        )
        return document["client_secret"]

    attempted = False
    try:
        common.require(
            admin.json([*register(first_file), "--dry-run"]).get("status") == "dry_run"
            and not first_file.exists(),
            "Cognito preview wrote credentials",
        )
        value["phase"] = "registration_attempted"
        save()
        attempted = True
        _, first = admin.run([*register(first_file), "--yes"], expected=None)
        first = first if isinstance(first, dict) else {}
        candidate = detail(first).get("id") or detail(first).get("target")
        if candidate:
            value["client_id"] = candidate
            save()
        # Reconcile only this registration, into a different private file.
        _, replay = admin.run([*register(replay_file), "--yes"], expected=None)
        replay = replay if isinstance(replay, dict) else {}
        recovered = detail(replay).get("id") or detail(replay).get("target")
        common.require(
            not candidate or not recovered or recovered == candidate,
            "Registration replay changed client identity",
        )
        if recovered:
            value["client_id"] = recovered
            save()
        common.require(
            replay.get("status") == "ok" and recovered,
            "Cognito registration uncertain; reconcile original name and operation",
        )
        original = snapshot()
        common.require(
            original.get("status") == "active", "Owned Cognito client not active"
        )
        replay_secret = credential(replay_file)
        if first_file.exists() and first_file.stat().st_size:
            common.require(
                credential(first_file) == replay_secret,
                "Same operation changed client secret",
            )
        # Never retain even a digest of the secret in public evidence.
        common.require(
            replay_secret not in json.dumps([first, replay, state]),
            "CLI metadata exposed credential",
        )
        del replay_secret
        value["checks"].append(
            "private_credential_delivery_same_operation_same_identity"
        )
        require_refusal(ordinary.run(argv("show", recovered), expected=None), http=403)
        require_refusal(
            foreign.run(argv("show", recovered, native), expected=None), http=404
        )
        common.require(snapshot() == original, "Denied reads changed owned identity")
        value["checks"].append("ordinary_and_foreign_tenant_reads_refused")
        # Existing output is refused before credential delivery or registration.
        require_refusal(admin.run([*register(replay_file), "--yes"], expected=None))
        common.require(snapshot() == original, "Unsafe-file retry changed client")
        value["checks"].append("existing_private_output_refused")
        value["phase"] = "checks_complete"
        save()
    finally:
        if value["client_id"]:
            try:
                before = snapshot()
                common.require(
                    before.get("status") in {"active", "retiring", "retired"},
                    "Unexpected client state; preserve for reconciliation",
                )
                value["retirement_expected_revision"] = before["revision"]
                value["cleanup"] = "retirement_pending"
                save()
                command = [
                    *argv("deregister", value["client_id"]),
                    "--operation-id",
                    plan["retirement_id"],
                    "--expected-revision",
                    before["revision"],
                    "--yes",
                ]
                admin.run(command, expected=None)
                final = snapshot()
                common.require(
                    final.get("status") == "retired",
                    "Cognito retirement unresolved; preserve original retirement operation",
                )
                value["final_identity"] = final
                value["cleanup"] = "retired_metadata_retained"
                save()
            except Exception:
                value["cleanup"] = "retirement_pending"
                save()
                raise
        elif attempted:
            value["cleanup"] = "reconcile_original_registration_operation"
            save()
        else:
            value["cleanup"] = "not_created"
            save()
    final = snapshot()
    retired_file = private / "retired.json"
    code, retired = admin.run([*register(retired_file), "--yes"], expected=None)
    common.require(
        code != 0
        and isinstance(retired, dict)
        and retired.get("status") in {"pending", "failed"},
        "Retired registration unexpectedly delivered credentials",
    )
    common.require(
        not retired_file.exists() or retired_file.stat().st_size == 0,
        "Retired registration wrote credentials",
    )
    common.require(snapshot() == final, "Retired registration reminted identity")
    value["checks"].append(
        "retired_readback_and_no_same_operation_credential_redelivery"
    )
    value["phase"] = "complete"
    save()
