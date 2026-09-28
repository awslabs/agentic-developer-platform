"""Opt-in D03 department role transition after the scoped-authority rollout."""

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

import common


def require_release(config, admin):
    fixture = config["hierarchy_lifecycle"]
    common.require(
        fixture.get("exclusive_ordinary_fixture") is True,
        "Role transition requires exclusive ordinary fixture",
    )
    release = fixture.get("role_scope_release")
    common.require(
        isinstance(release, str)
        and re.fullmatch(r"[0-9a-f]{40}", release)
        and config.get("expected_revision") == release,
        "Role transition requires an exact reviewed scoped-authority release",
    )
    gateway = (admin.json(["capabilities", "--refresh"]).get("detail") or {}).get(
        "gateway"
    ) or {}
    common.require(
        gateway.get("state") == "yes" and gateway.get("release") == release,
        "Scoped-authority release is not deployed",
    )


def exercise(config, state, admin, ordinary, ordinary_native, member, retained_bearer):
    plan = config["recovery_plan"]
    intent = plan["role_transition"]
    tenant = plan["fixture"]["tenant_id"]
    user = plan["fixture"]["ordinary_canonical_user_id"]
    team = intent["team_id"]
    own, other = intent["department_id"], intent["other_department_id"]
    baseline = plan["restore"]
    journal = {
        "intent": intent,
        "baseline": baseline,
        "phase": "prepared",
        "attempts": [],
    }
    path = Path(config["work_dir"]) / ("hierarchy-role-" + own + ".json")
    common.require(
        not path.exists(), "Prior role recovery journal requires reconciliation"
    )
    state["role_transition"] = journal
    state["role_restoration"] = "required"

    def save():
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(journal, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def shape(snapshot):
        row = snapshot["resource"]
        return {key: row.get(key) for key in baseline}

    attached = {
        **baseline,
        "team_id": team,
        "teams": [{"team_id": team, "role": "member", "is_primary": True}],
    }
    promoted = {**attached, "role": "dept_admin"}

    def change(args, before, expected):
        common.require(
            shape(member()) == shape(before), "Member changed before reviewed mutation"
        )
        args = [*args, "--expected-revision", before["revision"]]
        preview = admin.json(args + ["--dry-run"])
        common.require(
            preview.get("status") == "dry_run", "Role mutation preview failed"
        )
        # This intent is durable before delivery, including an uncertain ACK.
        journal["attempts"].append(
            {"command": args, "before": before, "expected": expected}
        )
        save()
        admin.json(args + ["--yes"])
        after = member()
        common.require(
            shape(after) == expected,
            "Role mutation readback changed unrelated membership state",
        )
        journal["last_readback"] = after
        save()

    def role_args(role):
        return [
            "admin",
            "member",
            "update",
            "--org",
            tenant,
            "--user",
            user,
            "--role",
            role,
        ]

    def team_args(action):
        return [
            "admin",
            "team",
            "members",
            action,
            "--org",
            tenant,
            "--team",
            team,
            "--user",
            user,
        ]

    def read(cli, department, allowed):
        code, response = cli.run(
            [
                "admin",
                "budget",
                "show",
                "--org",
                tenant,
                "--department",
                department,
                "--period",
                "daily",
            ],
            expected=None,
        )
        if allowed:
            common.require(
                code == 0 and response.get("status") == "ok",
                "Owned department read was not authorized",
            )
        else:
            error = response.get("error") or {}
            common.require(
                code != 0
                and (
                    error.get("http_status") == 403
                    or "HTTP 403" in error.get("message", "")
                ),
                "Department read was not refused",
            )

    def retained(department, expected):
        request = urllib.request.Request(
            config["gateway_url"].rstrip("/")
            + f"/admin/organizations/{tenant}/budget/department/{department}/daily",
            headers={"Authorization": "Bearer " + retained_bearer},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        journal.setdefault("retained_lease_checks", []).append(
            {
                "department_id": department,
                "observed_http_status": status,
                "expected_http_status": expected,
            }
        )
        common.require(
            status == expected,
            "Retained tenant lease did not follow current department authority",
        )

    def native_view():
        identity = (
            ordinary_native.json(["models", "mappings", "list"]).get("detail") or {}
        )
        common.require(
            identity.get("tenant_id") == plan["fixture"]["ordinary_native_tenant"]
            and identity.get("principal_id")
            == plan["fixture"]["ordinary_login_user_id"],
            "Native tenant identity changed",
        )
        snapshot = (
            admin.json(
                [
                    "admin",
                    "member",
                    "remove",
                    "--org",
                    plan["fixture"]["ordinary_native_tenant"],
                    "--user",
                    plan["fixture"]["ordinary_login_user_id"],
                    "--dry-run",
                ]
            ).get("detail")
            or {}
        )["before"]
        return {
            "tenant_id": identity["tenant_id"],
            "principal_id": identity["principal_id"],
            "membership": shape(snapshot),
        }

    native_before = native_view()
    journal["native_baseline"] = native_before
    initial = member()
    common.require(
        shape(initial) == baseline,
        "Role transition requires unchanged empty member baseline",
    )
    journal["initial_readback"] = initial
    save()
    try:
        read(ordinary, own, False)
        retained(own, 403)
        change(team_args("add") + ["--primary"], initial, attached)
        change(role_args("dept_admin"), member(), promoted)
        journal["phase"] = "promoted"
        save()
        read(ordinary, own, True)
        read(ordinary, other, False)
        retained(own, 200)
        retained(other, 403)
        common.require(
            native_view() == native_before,
            "Native tenant changed during owned department promotion",
        )
        journal["phase"] = "authority_verified"
        save()
    finally:
        try:
            current = member()
            current_shape = shape(current)
            common.require(
                current_shape in (baseline, attached, promoted),
                "Unexpected member role or team drift; retain owned resources for recovery",
            )
            if current_shape == promoted:
                change(role_args("member"), current, attached)
            if shape(member()) == attached:
                change(team_args("remove"), member(), baseline)
            common.require(
                shape(member()) == baseline, "Original member baseline not restored"
            )
            read(ordinary, own, False)
            retained(own, 403)
            common.require(
                native_view() == native_before,
                "Native tenant changed during role restoration",
            )
            journal["phase"] = "restored"
            save()
            state["role_restoration"] = "verified"
        except Exception:
            state["role_restoration"] = "pending"
            raise
    state["checks"].append(
        "owned-department-role-authority-retained-lease-and-restoration"
    )
