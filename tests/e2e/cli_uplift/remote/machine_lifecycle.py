"""D05: canonical metadata lifecycle, ordinary denial, and session review only."""

import json
import os
import tempfile
import urllib.request
from pathlib import Path

import common
from capability_contrast import _write_session
from machine_lifecycle_plan import recovery_plan


def detail(result):
    return result.get("detail") or {} if isinstance(result, dict) else {}


def require_refusal(result, *, http=None, code=None):
    status, envelope = result
    error = (envelope or {}).get("error") or {}
    common.require(
        status != 0 and (envelope or {}).get("status") in {"failed", "unavailable"},
        "Expected refusal was not observed",
    )
    if http is not None:
        common.require(
            error.get("http_status") == http
            or error.get("status_code") == http
            or f"HTTP {http}" in error.get("message", ""),
            "Refusal did not prove expected HTTP status",
        )
    if code is not None:
        common.require(error.get("code") == code, "Refusal had another error code")


def ordinary_tokens(config, env):
    username = common.fixture_secret(config, env, "non_admin_username")
    password = common.fixture_secret(config, env, "non_admin_password")
    request = urllib.request.Request(
        config["gateway_url"].rstrip("/") + "/auth/cli/password",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            tokens = json.load(response)
    except Exception:
        raise common.RemoteError("Ordinary fixture authentication failed") from None
    common.require(
        tokens.get("access_token"), "Ordinary fixture authentication is incomplete"
    )
    return tokens


def execute(config, evidence):
    fixture = config.get("machine_lifecycle") or {}
    common.require(
        fixture.get("owned_mutations_authorized") is True,
        "Explicit machine lifecycle authorization required",
    )
    common.require(
        config.get("test_user_id") == fixture.get("login_user_id"),
        "Installed administrator login subject mismatch",
    )
    plan = recovery_plan(config)
    common.require(
        config.get("recovery_plan") == plan,
        "Externally retained machine recovery intent required before dispatch",
    )
    tenant = fixture["tenant_id"]
    native = fixture["ordinary_native_tenant"]
    common.require(
        native != tenant and native == config.get("org_id"),
        "Verified separate native tenant required",
    )
    state = {
        "recovery_plan": plan,
        "phase": "not_started",
        "principal_id": None,
        "checks": [],
        "cleanup": "not_started",
        "qualification": "Canonical metadata only; no provider identity, credential, inference, session revocation or membership mutation. Access/session coverage is read/denial only.",
    }
    evidence["detail"] = state
    recovery = Path(config["work_dir"]) / ("machine-" + plan["registration_id"])
    recovery.mkdir(mode=0o700, exist_ok=False)
    path = recovery / "recovery.json"

    def save():
        with path.open("w") as stream:
            os.chmod(path, 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())

    save()
    with tempfile.TemporaryDirectory(prefix="adp-owned-machine-") as directory:
        root = Path(directory)

        def session(name, tokens, selected):
            home = root / name
            home.mkdir(mode=0o700)
            env = common.clean_env(config, HOME=home, ADP_TENANT=selected)
            for key in (
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "XDG_STATE_HOME",
                "XDG_RUNTIME_DIR",
                "CODEX_HOME",
            ):
                folder = home / key
                folder.mkdir(mode=0o700)
                env[key] = str(folder)
            env["BG_CONFIG_DIR"] = str(home / ".bedrock-gateway")
            _write_session(home, config["gateway_url"], tokens)
            return common.Cli(
                config["cli_path"], env, evidence["transcript"], timeout=60
            ), env

        admin_tokens = common.session_tokens(config)
        admin, env = session("admin", admin_tokens, tenant)
        foreign, _ = session("admin-native", admin_tokens, native)
        actor = detail(admin.json(["models", "mappings", "list"]))
        common.require(
            actor.get("principal_id") == fixture["canonical_user_id"]
            and actor.get("tenant_id") == tenant,
            "Administrator canonical owner mismatch",
        )
        tokens = ordinary_tokens(config, env)
        ordinary, _ = session("ordinary", tokens, tenant)
        observed = detail(ordinary.json(["models", "mappings", "list"]))
        common.require(
            observed.get("principal_id") == fixture["ordinary_canonical_user_id"]
            and observed.get("tenant_id") == tenant,
            "Independent ordinary owner mismatch",
        )
        common.require(
            fixture["ordinary_canonical_user_id"] != fixture["canonical_user_id"],
            "Ordinary fixture cannot be the administrator",
        )

        def access_baseline():
            results = {}
            for selected in (tenant, native):
                value = detail(
                    ordinary.json(["access", "status", "--tenant", selected])
                )
                common.require(
                    value.get("tenant_id") == selected
                    and value.get("spend_eligibility") == "not_evaluated",
                    "Access status changed target or asserted spend",
                )
                results[selected] = value
            return results

        access_before = access_baseline()
        review = [
            "admin",
            "session",
            "revoke-user",
            "--user",
            fixture["ordinary_canonical_user_id"],
            "--org",
            tenant,
            "--reason",
            "Owned diagnostic review only",
            "--dry-run",
        ]
        before_family = admin.json(review)
        family = detail(before_family)
        common.require(
            before_family.get("status") == "preview"
            and family.get("user_id") == fixture["ordinary_canonical_user_id"]
            and family.get("org") == tenant
            and family.get("credential_families") == ["gateway_jwt"]
            and family.get("cognito_sessions_revoked") is False,
            "Session review lacks scoped credential-family evidence",
        )
        require_refusal(ordinary.run(review, expected=None), http=403)
        state["session_review"] = family
        state["checks"].append("ordinary_access_and_admin_session_review_boundary")
        base = ["admin", "service-principal"]
        primary, secondary = plan["aliases"]

        def registration(operation):
            return [
                *base,
                "register",
                "--org",
                tenant,
                "--name",
                plan["display_name"],
                "--alias-source",
                primary["alias_source"],
                "--alias-id",
                primary["alias_id"],
                "--operation-id",
                operation,
            ]

        register = registration(plan["registration_id"])
        common.require(
            admin.json([*register, "--dry-run"]).get("status") == "dry_run",
            "Canonical registration preview failed",
        )
        require_refusal(ordinary.run([*register, "--yes"], expected=None), http=403)
        principal = None

        def snapshot():
            value = detail(admin.json([*base, "show", principal, "--org", tenant]))
            common.require(
                value.get("canonical_service_principal_id") == principal
                and value.get("tenant_id") == tenant
                and value.get("display_name") == plan["display_name"],
                "Owned canonical principal changed identity or name",
            )
            expected = {
                (row["alias_source"], row["alias_id"]) for row in plan["aliases"]
            }
            aliases = value.get("aliases")
            common.require(
                isinstance(aliases, list)
                and aliases
                and all(
                    (row.get("alias_source"), row.get("alias_id")) in expected
                    for row in aliases
                ),
                "Principal has unrelated aliases; preserve it for review",
            )
            common.require(
                len({(row["alias_source"], row["alias_id"]) for row in aliases})
                == len(aliases),
                "Owned alias identity is ambiguous",
            )
            return value

        def change(action, extra):
            current = snapshot()
            return admin.json(
                [
                    *base,
                    action,
                    principal,
                    "--org",
                    tenant,
                    "--expected-revision",
                    current["revision"],
                    *extra,
                    "--yes",
                ],
                expected=None,
            )

        try:
            state["phase"] = "registration_attempted"
            save()
            first = admin.json([*register, "--yes"], expected=None)
            recovered = admin.json([*register, "--yes"], expected=None)
            candidate = detail(recovered).get("canonical_service_principal_id")
            common.require(
                recovered.get("status") == "ok" and candidate,
                "Registration unresolved; reconcile original operation ID",
            )
            principal = candidate
            state["principal_id"] = principal
            save()
            common.require(
                not detail(first).get("canonical_service_principal_id")
                or detail(first)["canonical_service_principal_id"] == principal,
                "Registration replay changed canonical identity",
            )
            original = snapshot()
            common.require(
                original["status"] == "active",
                "Prior canonical principal is not active; do not restart lifecycle",
            )
            mapped = detail(
                admin.json(
                    ["models", "mappings", "list", "--service-principal", principal]
                )
            )
            common.require(
                (
                    mapped.get("canonical_service_principal_id")
                    or mapped.get("principal_id")
                )
                == principal
                and mapped.get("tenant_id") == tenant,
                "Mapping metadata names another canonical principal",
            )
            state["checks"].append("same_operation_and_mapping_identity")
            require_refusal(
                admin.run(
                    [*registration(plan["duplicate_id"]), "--yes"], expected=None
                ),
                http=422,
            )
            common.require(
                snapshot() == original, "Duplicate alias attempt changed the principal"
            )
            require_refusal(
                foreign.run([*base, "show", principal, "--org", native], expected=None),
                http=404,
            )
            state["checks"].append("duplicate_alias_and_foreign_tenant_refused")
            added = change(
                "alias-add",
                [
                    "--alias-source",
                    secondary["alias_source"],
                    "--alias-id",
                    secondary["alias_id"],
                ],
            )
            common.require(
                added.get("status") == "ok", "Alias addition outcome unknown"
            )
            after_alias = snapshot()
            common.require(
                len(after_alias["aliases"]) == 2, "Second owned alias not present"
            )
            stale = [
                *base,
                "status",
                principal,
                "--org",
                tenant,
                "--status",
                "suspended",
                "--expected-revision",
                original["revision"],
                "--yes",
            ]
            require_refusal(admin.run(stale, expected=None), code="revision_conflict")
            common.require(
                snapshot() == after_alias, "Stale command modified canonical metadata"
            )
            state["checks"].append("owned_alias_and_stale_revision_refusal")
            suspended = change("status", ["--status", "suspended"])
            common.require(
                suspended.get("status") == "ok" and snapshot()["status"] == "suspended",
                "Canonical suspension unconfirmed",
            )
            state["checks"].append("suspended_readback")
            state["phase"] = "checks_complete"
        finally:
            if principal:
                current = snapshot()
                for alias in current["aliases"]:
                    if alias["is_active"]:
                        result = change("alias-remove", ["--alias-row-id", alias["id"]])
                        common.require(
                            result.get("status") == "ok",
                            "Owned alias retirement uncertain",
                        )
                current = snapshot()
                if current["status"] != "retired":
                    common.require(
                        change("status", ["--status", "retired"]).get("status") == "ok",
                        "Principal retirement uncertain",
                    )
                final = snapshot()
                common.require(
                    final["status"] == "retired"
                    and all(row["is_active"] is False for row in final["aliases"]),
                    "Owned canonical cleanup incomplete",
                )
                state["final_identity"] = final
                state["cleanup"] = "retired_aliases_revoked_history_retained"
            else:
                state["cleanup"] = "reconcile_original_registration_operation"
            save()
        current = snapshot()
        require_refusal(
            admin.run(
                [
                    *base,
                    "status",
                    principal,
                    "--org",
                    tenant,
                    "--status",
                    "active",
                    "--expected-revision",
                    current["revision"],
                    "--yes",
                ],
                expected=None,
            ),
            http=422,
        )
        common.require(snapshot() == current, "Retirement was not terminal")
        common.require(
            access_baseline() == access_before, "Ordinary membership/access changed"
        )
        common.require(
            admin.json(review) == before_family,
            "Reviewed ordinary token family changed",
        )
        state["checks"].append("terminal_retirement_and_ordinary_access_preserved")
        save()
