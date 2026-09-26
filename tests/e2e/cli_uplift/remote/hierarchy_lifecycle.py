"""Owned hierarchy lifecycle and ordinary tenant revocation; no inference."""

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import common
from capability_contrast import _write_session
from hierarchy_plan import recovery_plan


def detail(value):
    return value.get("detail") or {}


def refused(result, *, http=None, codes=()):
    status, envelope = result
    error = (envelope or {}).get("error") or {}
    matched = error.get("code") in codes
    if http is not None:
        matched = (
            matched
            or error.get("http_status") == http
            or f"HTTP {http}" in error.get("message", "")
        )
    common.require(
        status != 0 and matched, "Expected hierarchy refusal was not observed"
    )
    return {
        "exit_code": status,
        "code": error.get("code"),
        "expected_http_status": http,
    }


def execute(config, evidence):
    fixture = config.get("hierarchy_lifecycle") or {}
    common.require(
        fixture.get("owned_mutations_authorized") is True,
        "Explicit hierarchy authorization required",
    )
    plan = recovery_plan(config)
    common.require(
        config.get("recovery_plan") == plan,
        "Exact externally retained hierarchy recovery plan required",
    )
    common.require(
        config.get("test_user_id") == fixture["login_user_id"],
        "Installed administrator fixture mismatch",
    )
    common.require(
        fixture["ordinary_login_user_id"] != fixture["login_user_id"],
        "Independent ordinary login required",
    )
    tenant, native = fixture["tenant_id"], fixture["ordinary_native_tenant"]
    common.require(tenant != native, "Two distinct ordinary memberships required")
    ordinary_user = fixture["ordinary_canonical_user_id"]
    state = {
        "plan": plan,
        "checks": [],
        "cleanup": {},
        "membership_restoration": "not_attempted",
        "qualification": "Role-change authority remains unqualified: adding an existing person to a disposable organization retains an org-local user after membership removal; no supported CLI identity cleanup is available.",
    }
    evidence["detail"] = state
    checks = state["checks"]
    with tempfile.TemporaryDirectory(prefix="adp-hierarchy-") as directory:
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
            env["ADP_HOME"] = str(home / ".adp")
            _write_session(home, config["gateway_url"], tokens)
            return common.Cli(
                config["cli_path"], env, evidence["transcript"], timeout=90
            ), env

        admin, env = session("admin", common.session_tokens(config), tenant)
        actor = detail(admin.json(["models", "mappings", "list"]))
        common.require(
            actor.get("principal_id") == fixture["canonical_user_id"]
            and actor.get("tenant_id") == tenant,
            "Administrator tenant/identity mismatch",
        )
        username = common.fixture_secret(config, env, "non_admin_username")
        password = common.fixture_secret(config, env, "non_admin_password")
        # Authentication fixture setup only. All lifecycle operations below use
        # the installed CLI; no credential is put in a command or evidence.
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
            tokens.get("access_token"),
            "Ordinary fixture requires completed authentication",
        )
        ordinary, _ = session("ordinary", tokens, tenant)
        ordinary_native, _ = session("ordinary-native", tokens, native)
        for cli, expected_tenant, expected_user in (
            (ordinary, tenant, ordinary_user),
            (ordinary_native, native, fixture["ordinary_login_user_id"]),
        ):
            observed = detail(cli.json(["models", "mappings", "list"]))
            common.require(
                observed.get("tenant_id") == expected_tenant
                and observed.get("principal_id") == expected_user,
                "Ordinary canonical identity mismatch",
            )
        # Retain one signed lease privately before revocation. Its later API
        # refusal corroborates the CLI's fresh-selection denial without calling
        # a newly issued lease an old process context.
        lease_request = urllib.request.Request(
            config["gateway_url"].rstrip("/") + "/workspaces/context",
            data=json.dumps({"org_id": tenant}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + tokens["access_token"],
            },
            method="POST",
        )
        with urllib.request.urlopen(lease_request, timeout=45) as response:
            retained_lease = json.load(response)
        common.require(
            retained_lease.get("canonical_user_id") == ordinary_user
            and retained_lease.get("tenant_id") == tenant,
            "Retained ordinary tenant lease has the wrong owner",
        )
        retained_bearer = (
            "adpctx1~" + retained_lease["context_token"] + "~" + tokens["access_token"]
        )
        code, _ = ordinary.run(
            ["admin", "login", "--credentials-stdin"],
            expected=None,
            stdin_text=json.dumps({"username": username, "password": password}),
        )
        common.require(code == 3, "Ordinary login was not refused administrator access")
        checks.append("independent-ordinary-admin-denial")

        def member():
            return detail(
                admin.json(
                    [
                        "admin",
                        "member",
                        "remove",
                        "--org",
                        tenant,
                        "--user",
                        ordinary_user,
                        "--dry-run",
                    ]
                )
            )["before"]

        def baseline(snapshot):
            row = snapshot["resource"]
            return {key: row.get(key) for key in plan["restore"]}

        common.require(
            baseline(member()) == plan["restore"],
            "Ordinary membership has unrelated baseline state; do not modify it",
        )

        def target(kind, identifier, org=tenant):
            args = ["admin", kind, "show", "--org", org]
            if kind != "org":
                args += ["--id", identifier]
            code, result = admin.run(args, expected=None)
            if code == 0:
                return detail(result)
            error = (result or {}).get("error") or {}
            common.require(
                error.get("http_status") == 404
                or "HTTP 404" in error.get("message", ""),
                "Exact hierarchy read failed without absence proof",
            )
            return None

        owned = [
            ("org", plan["org_id"], plan["org_id"]),
            ("department", plan["department_id"], tenant),
        ] + [("team", value, tenant) for value in plan["team_ids"]]
        default_children = [
            ("department", plan["default_department_id"], plan["org_id"]),
            ("team", plan["default_team_id"], plan["org_id"]),
        ]
        for kind, identifier, org in owned + default_children:
            common.require(
                target(kind, identifier, org) is None,
                "Preexisting owned target requires explicit recovery, not a fresh diagnostic",
            )

        attempted = []
        revoked = False
        complete = False

        def mutate(args):
            preview = admin.json(args + ["--dry-run"])
            common.require(
                preview.get("status") == "dry_run", "Mutation preview failed"
            )
            return admin.json(args + ["--yes"])

        def team_change(action, team):
            current = member()
            return mutate(
                [
                    "admin",
                    "team",
                    "members",
                    action,
                    "--org",
                    tenant,
                    "--team",
                    team,
                    "--user",
                    ordinary_user,
                    "--expected-revision",
                    current["revision"],
                ]
            )

        try:
            for kind, identifier, org in owned:
                args = [
                    "admin",
                    kind,
                    "create",
                    "--id",
                    identifier,
                    "--name",
                    identifier,
                ]
                if kind != "org":
                    args += ["--org", org]
                if kind == "team":
                    args += ["--department", plan["department_id"]]
                attempted.append((kind, identifier, org))
                if kind == "org":
                    # Canonical organization creation creates these children
                    # atomically. Retain them before transport, including a lost ACK.
                    attempted.extend(default_children)
                mutate(args)
                created = target(kind, identifier, org)
                common.require(created, "Created hierarchy target missing")
                resource = created["resource"]
                if kind != "org":
                    common.require(
                        resource.get("org_id") == org,
                        "Created hierarchy has wrong organization",
                    )
                if kind == "team":
                    common.require(
                        resource.get("department_id") == plan["department_id"],
                        "Created team has wrong department",
                    )
                state.setdefault("parentage", {})[identifier] = {
                    "org_id": org,
                    "department_id": resource.get("department_id")
                    if kind == "team"
                    else None,
                }
                if kind == "team" and identifier == plan["team_ids"][0]:
                    # Reuse the exact caller-selected ID/body; duplicates are
                    # explicit conflicts, not invented successful replay receipts.
                    state["create_retry"] = refused(
                        admin.run(args + ["--yes"], expected=None), http=409
                    )
                    common.require(
                        target(kind, identifier, org) == created,
                        "Same-ID create retry changed the original resource",
                    )
                    checks.append("same-id-create-conflict-original-unchanged")
            checks.append("explicit-department-team-parentage")
            first_team = plan["team_ids"][0]
            before_denials = target("team", first_team)
            state["ordinary_hierarchy_read"] = refused(
                ordinary.run(
                    ["admin", "team", "show", "--org", tenant, "--id", first_team],
                    expected=None,
                ),
                http=403,
            )
            state["ordinary_hierarchy_write"] = refused(
                ordinary.run(
                    [
                        "admin",
                        "team",
                        "update",
                        "--org",
                        tenant,
                        "--id",
                        first_team,
                        "--name",
                        first_team + "-denied",
                        "--expected-revision",
                        before_denials["revision"],
                        "--yes",
                    ],
                    expected=None,
                ),
                http=403,
                codes=("permission_denied",),
            )
            state["name_selector_refusal"] = refused(
                admin.run(
                    ["admin", "team", "show", "--org", tenant, "--name", "Default"],
                    expected=None,
                ),
                codes=("usage_error",),
            )
            common.require(
                target("team", first_team) == before_denials,
                "Denied or name-based operation changed the owned team",
            )
            checks.extend(
                [
                    "ordinary-hierarchy-read-write-refused",
                    "canonical-id-required-name-selector-refused",
                ]
            )
            org_id = plan["org_id"]
            original = target("org", org_id, org_id)
            mutate(
                [
                    "admin",
                    "org",
                    "update",
                    "--org",
                    org_id,
                    "--name",
                    org_id + "-renamed",
                    "--expected-revision",
                    original["revision"],
                ]
            )
            code, result = admin.run(
                [
                    "admin",
                    "org",
                    "update",
                    "--org",
                    org_id,
                    "--name",
                    "stale",
                    "--expected-revision",
                    original["revision"],
                    "--yes",
                ],
                expected=None,
            )
            common.require(
                code == 4
                and (result or {}).get("error", {}).get("code") == "stale_revision",
                "Stale hierarchy CLI update not refused",
            )
            checks.append("org-create-update-local-stale-refusal")
            dep = target("department", plan["department_id"])
            code, result = admin.run(
                [
                    "admin",
                    "department",
                    "delete",
                    "--org",
                    tenant,
                    "--id",
                    plan["department_id"],
                    "--expected-revision",
                    dep["revision"],
                    "--yes",
                ],
                expected=None,
            )
            common.require(
                code == 4
                and (result or {}).get("error", {}).get("code")
                == "hierarchy_has_dependencies",
                "Populated department deletion not refused",
            )
            checks.append("populated-delete-no-cascade")
            for team in plan["team_ids"]:
                team_change("add", team)
            first, second = plan["team_ids"]
            team_change("remove", first)
            common.require(
                {row["team_id"] for row in member()["resource"]["teams"]} == {second},
                "Removing one team changed the other membership",
            )
            common.require(
                target("team", first, org_id) is None,
                "Foreign-parent lookup exposed owned team",
            )
            team_change("remove", second)
            common.require(
                baseline(member()) == plan["restore"],
                "Team removal did not restore empty membership baseline",
            )
            checks.extend(["two-teams-remove-one", "foreign-parent-refused"])
            current = member()
            revoked = True  # Even a lost DELETE reply requires restoration.
            state["membership_restoration"] = "required"
            mutate(
                [
                    "admin",
                    "member",
                    "remove",
                    "--org",
                    tenant,
                    "--user",
                    ordinary_user,
                    "--expected-revision",
                    current["revision"],
                ]
            )
            common.require(
                member()["resource"]["membership_status"] == "revoked",
                "Membership revocation not observed",
            )
            code, _ = ordinary.run(["models", "mappings", "list"], expected=None)
            common.require(
                code in (3, 4), "Ordinary revoked tenant remained accessible"
            )
            code, _ = ordinary.run(
                ["--tenant", tenant, "tenant", "current"], expected=None
            )
            common.require(
                code in (3, 4), "New revoked tenant selection remained accessible"
            )
            common.require(
                detail(ordinary_native.json(["models", "mappings", "list"])).get(
                    "principal_id"
                )
                == fixture["ordinary_login_user_id"],
                "Revocation affected the other native tenant",
            )
            old_request = urllib.request.Request(
                config["gateway_url"].rstrip("/") + "/me/persona-models",
                headers={"Authorization": "Bearer " + retained_bearer},
            )
            old_status = None
            try:
                with urllib.request.urlopen(old_request, timeout=45) as response:
                    old_status = response.status
            except urllib.error.HTTPError as exc:
                old_status = exc.code
            common.require(
                old_status == 403,
                "Previously issued tenant lease remained usable during revocation",
            )
            checks.append("retained-signed-lease-api-denied")
            checks.append("revoked-tenant-denied-native-preserved")
            complete = True
        finally:
            if revoked:
                try:
                    restoring = baseline(member())
                    common.require(
                        restoring["role"] == "member"
                        and restoring["team_id"] == ""
                        and restoring["teams"] == [],
                        "Membership changed outside the diagnostic; refuse restoration overwrite",
                    )
                    mutate(
                        [
                            "admin",
                            "member",
                            "add",
                            "--org",
                            tenant,
                            "--existing-user",
                            fixture["ordinary_login_user_id"],
                            "--role",
                            "member",
                        ]
                    )
                    common.require(
                        baseline(member()) == plan["restore"],
                        "Membership baseline restoration failed",
                    )
                    state["membership_restoration"] = "verified"
                except Exception:
                    state["membership_restoration"] = "pending"
            # Clean only the two predeclared membership edges, never a person's
            # identity or any preexisting team. Continue resource cleanup on error.
            try:
                for row in member()["resource"]["teams"]:
                    if row["team_id"] in plan["team_ids"]:
                        team_change("remove", row["team_id"])
            except Exception:
                state["cleanup"]["team_memberships"] = "pending"
            for kind, identifier, org in reversed(attempted):
                try:
                    row = target(kind, identifier, org)
                    if row:
                        resource = row["resource"]
                        is_default = (kind, identifier, org) in default_children
                        common.require(
                            kind == "org" or resource.get("org_id") == org,
                            "Owned hierarchy parent changed; preserve it",
                        )
                        if kind == "team" and not is_default:
                            common.require(
                                resource.get("department_id") == plan["department_id"],
                                "Owned team department changed; preserve it",
                            )
                        if is_default:
                            common.require(
                                resource.get("org_id") == plan["org_id"]
                                and resource.get("name") == "Default"
                                and resource.get("description") == "Default " + kind
                                and (
                                    kind != "team"
                                    or resource.get("department_id")
                                    == plan["default_department_id"]
                                ),
                                "Default child ownership or content changed; retain it",
                            )
                        common.require(
                            is_default
                            or resource.get("name")
                            in {identifier, identifier + "-renamed"},
                            "Owned ID now names an unexpected resource; preserve it for manual recovery",
                        )
                        args = [
                            "admin",
                            kind,
                            "delete",
                            "--org",
                            org,
                            "--expected-revision",
                            row["revision"],
                        ]
                        if kind != "org":
                            args += ["--id", identifier]
                        mutate(args)
                        if kind == "team" and identifier == plan["team_ids"][0]:
                            state["delete_retry"] = refused(
                                admin.run(args + ["--yes"], expected=None), http=404
                            )
                            checks.append("same-id-delete-retry-reports-absence")
                    common.require(
                        target(kind, identifier, org) is None,
                        "Cleanup lacks exact absence proof",
                    )
                    state["cleanup"][identifier] = "absent"
                except Exception:
                    state["cleanup"][identifier] = "pending"
            common.require(
                "pending" not in state["cleanup"].values()
                and state["membership_restoration"] != "pending",
                "Owned hierarchy cleanup/restoration remains pending; recover exact manifest targets",
            )
        common.require(
            complete and baseline(member()) == plan["restore"],
            "Hierarchy checks or original member baseline incomplete",
        )
        common.require(
            detail(ordinary.json(["models", "mappings", "list"])).get("principal_id")
            == ordinary_user,
            "Restored ordinary tenant is unavailable",
        )
        checks.append("membership-restored-and-owned-resources-absent")
        evidence["success"] = True
