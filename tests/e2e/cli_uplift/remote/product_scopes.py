#!/usr/bin/env python3
"""Read two protected Product scopes through the installed CLI and real API."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import common
from capability_contrast import _write_session, ordinary_session
from product_profile import RecordingCli, detail, failure, verify_install


def scope_fixture_inputs(fixture):
    scopes = fixture.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != 2:
        return None
    values = []
    for scope in scopes:
        if not isinstance(scope, dict) or not isinstance(
            scope.get("configuration"), dict
        ):
            return None
        configuration = scope["configuration"]
        basic, financials = configuration.get("basic"), configuration.get("financials")
        if not isinstance(basic, dict) or not isinstance(financials, dict):
            return None
        repositories = basic.get("repositories")
        if (
            not isinstance(repositories, list)
            or len(repositories) != 1
            or not isinstance(repositories[0], dict)
        ):
            return None
        reference = repositories[0]
        if reference.get("tenant_id") != fixture.get("tenant_id") or reference.get(
            "project_id"
        ) != scope.get("project_id"):
            return None
        connection, revision, plan_date = (
            reference.get("source_connection_id"),
            reference.get("record_revision"),
            financials.get("release_target_date"),
        )
        if not all(
            isinstance(value, str) and value
            for value in (connection, revision, plan_date)
        ):
            return None
        if not isinstance(scope.get("progress"), dict):
            return None
        values.append((connection, plan_date))
    if (
        values[0][0] == values[1][0]
        or values[0][1] == values[1][1]
        or scopes[0]["progress"] == scopes[1]["progress"]
    ):
        return None
    return values


def verify_scope_reads(config, cli, readback, evidence):
    fixture = config.get("product_scopes") or {}
    scopes = fixture.get("scopes")
    common.require(
        fixture.get("owned_fixture") is True
        and fixture.get("run_id") == config.get("evaluation_id")
        and fixture.get("tenant_id") == config.get("org_id")
        and fixture.get("login_user_id") == config.get("test_user_id")
        and fixture.get("source_revision") == config.get("expected_revision")
        and fixture.get("product_id")
        and fixture.get("product_name")
        and fixture.get("actor_id")
        and isinstance(scopes, list)
        and len(scopes) == 2
        and all(isinstance(scope, dict) and scope.get("project_id") for scope in scopes)
        and scopes[0]["project_id"] != scopes[1]["project_id"]
        and scope_fixture_inputs(fixture) is not None,
        "Two distinct, pinned and run-owned Product scopes are required",
    )
    evidence["installed_hashes"] = verify_install(config)
    product_id = fixture["product_id"]
    status, page = readback("/api/v1/products")
    common.require(
        status == 200
        and isinstance(page, dict)
        and isinstance(page.get("items"), list)
        and len(page["items"]) <= 100,
        "Authoritative Product binding listing is unavailable",
    )
    bound = [item for item in page["items"] if item.get("id") == product_id]
    common.require(
        len(bound) == 1
        and set(bound[0].get("project_ids") or [])
        == {scope["project_id"] for scope in scopes},
        "Installed CLI fixture does not match authoritative Project bindings",
    )
    identity = None
    for scope in (scopes[0], scopes[1], scopes[0]):
        project_id = scope["project_id"]
        path = (
            "/api/v1/products/"
            + quote(product_id, safe="")
            + "/projects/"
            + quote(project_id, safe="")
        )
        shown = detail(cli, ["show", "--product", product_id, "--project", project_id])
        status, observed = readback(path)
        common.require(
            status == 200
            and shown == observed
            and isinstance(observed, dict)
            and observed.get("product_id") == product_id
            and observed.get("project_id") == project_id
            and observed.get("tenant_id") == fixture["tenant_id"]
            and isinstance(observed.get("product"), dict)
            and observed["product"].get("name") == fixture["product_name"]
            and observed["product"].get("owner_ref")
            == {"kind": "user", "id": fixture["actor_id"]}
            and observed.get("project", {}).get("id") == project_id
            and observed["project"].get("name") == scope.get("name")
            and observed.get("configuration") == scope.get("configuration")
            and observed.get("progress") == scope.get("progress"),
            "Installed CLI scope differs from independent API and protected fixture",
        )
        if identity is None:
            identity = observed["product"]
        common.require(
            identity == observed["product"],
            "Product identity changed when switching Projects",
        )
        evidence.setdefault("api_readback", []).append(
            {
                "product_id": product_id,
                "project_id": project_id,
                "status": status,
                "revision": observed.get("revision"),
            }
        )
    evidence["result"] = "passed"
    evidence["success"] = True


def verify_scope_denials(config, editor_cli, foreign_cli, readback, evidence):
    fixture = config.get("product_scopes") or {}
    scopes = fixture.get("scopes") or []
    common.require(
        fixture.get("owned_fixture") is True
        and fixture.get("run_id") == config.get("evaluation_id")
        and fixture.get("tenant_id") == config.get("org_id")
        and fixture.get("source_revision") == config.get("expected_revision")
        and scope_fixture_inputs(fixture) is not None
        and len(scopes) == 2
        and scopes[0]["project_id"] != scopes[1]["project_id"]
        and fixture.get("editor_user_id")
        and fixture["editor_user_id"] != fixture.get("actor_id")
        and fixture.get("foreign_user_id")
        and fixture["foreign_user_id"] != fixture.get("editor_user_id")
        and fixture.get("foreign_tenant_id")
        and fixture["foreign_tenant_id"] != fixture["tenant_id"]
        and fixture.get("foreign_project_id")
        not in (None, "", *[scope["project_id"] for scope in scopes]),
        "Protected Product editor and foreign Project fixtures are required",
    )
    evidence["installed_hashes"] = verify_install(config)
    product_id = fixture["product_id"]
    baselines = {}
    for scope in scopes:
        project_id = scope["project_id"]
        path = (
            "/api/v1/products/"
            + quote(product_id, safe="")
            + "/projects/"
            + quote(project_id, safe="")
        )
        status, baseline = readback(path)
        common.require(
            status == 200
            and isinstance(baseline, dict)
            and baseline.get("product_id") == product_id
            and baseline.get("project_id") == project_id
            and baseline.get("tenant_id") == fixture["tenant_id"]
            and baseline.get("product", {}).get("owner_ref")
            == {"kind": "user", "id": fixture["actor_id"]}
            and type(baseline.get("revision")) is int
            and baseline["revision"] > 0
            and baseline.get("configuration") == scope["configuration"],
            "Owner baseline does not match the protected Project fixture",
        )
        baselines[path] = baseline
        shown = detail(
            editor_cli, ["show", "--product", product_id, "--project", project_id]
        )
        common.require(
            shown == baseline, "Ordinary editor cannot read the shared-team Product"
        )
        for verb in ("show", "history"):
            denial = failure(
                foreign_cli,
                [verb, "--product", product_id, "--project", project_id],
                5,
                "not_found",
                evidence,
            )
            common.require(
                product_id not in json.dumps(denial)
                and project_id not in json.dumps(denial),
                "Foreign Product denial disclosed an owned identity",
            )
            common.require(
                denial.get("detail") in (None, {})
                and set(denial["error"]) <= {"code", "message"}
                and denial["error"].get("message")
                in (
                    None,
                    "ADP returned HTTP 404 (not_found). Check the target and your access; it may be unavailable.",
                ),
                "Foreign Product denial disclosed history metadata",
            )
        denial = failure(
            foreign_cli,
            ["project", "bindings", "--product", product_id, "--project", project_id],
            5,
            "not_found",
            evidence,
        )
        common.require(
            product_id not in json.dumps(denial)
            and project_id not in json.dumps(denial)
            and denial.get("detail") in (None, {})
            and set(denial["error"]) <= {"code", "message"}
            and denial["error"].get("message")
            in (
                None,
                "ADP returned HTTP 404 (not_found). Check the target and your access; it may be unavailable.",
            ),
            "Foreign binding-history denial disclosed scope metadata",
        )
        evidence.setdefault("api_readback", []).append(
            {
                "product_id": product_id,
                "project_id": project_id,
                "status": status,
                "revision": baseline["revision"],
            }
        )
    with tempfile.TemporaryDirectory(prefix="adp-product-scope-denials-") as directory:
        admin_patch = Path(directory) / "admin-patch.json"
        admin_patch.write_text(json.dumps({"team_id": "unauthorized-team"}))
        os.chmod(admin_patch, 0o600)
        selection = Path(directory) / "foreign-project.json"
        selection.write_text(
            json.dumps({"mode": "select", "project_id": fixture["foreign_project_id"]})
        )
        os.chmod(selection, 0o600)
        failure(
            editor_cli,
            [
                "update",
                "--product",
                product_id,
                "--project",
                scopes[0]["project_id"],
                "--input",
                str(admin_patch),
                "--revision",
                str(baselines[next(iter(baselines))]["revision"]),
                "--idempotency-key",
                "scope_admin_denied_" + config["evaluation_id"],
            ],
            3,
            "permission_denied",
            evidence,
        )
        failure(
            editor_cli,
            [
                "project",
                "bind",
                "--product",
                product_id,
                "--input",
                str(selection),
                "--revision",
                str(baselines[next(iter(baselines))]["revision"]),
                "--idempotency-key",
                "scope_bind_denied_" + config["evaluation_id"],
            ],
            5,
            "not_found",
            evidence,
        )
    for command in (["product", "list"], ["product", "project", "list"]):
        code, envelope = foreign_cli.run(command, expected=None)
        common.require(
            code == 0
            and isinstance(envelope, dict)
            and envelope.get("status") == "ok"
            and set(envelope)
            <= {"status", "command", "detail", "next_action", "capability_preflight"}
            and isinstance(envelope.get("detail"), dict)
            and set(envelope["detail"]) == {"items", "next_cursor"}
            and isinstance(envelope["detail"]["items"], list)
            and len(envelope["detail"]["items"]) <= 100
            and (
                envelope["detail"]["items"] or envelope["detail"]["next_cursor"] is None
            )
            and not any(
                identifier in json.dumps(envelope)
                for identifier in (
                    product_id,
                    *[scope["project_id"] for scope in scopes],
                )
            ),
            "Foreign Product or Project listing disclosed an owned scope",
        )
    for path, baseline in baselines.items():
        common.require(
            readback(path) == (200, baseline),
            "Denied Product commands changed durable state",
        )
        evidence["api_readback"].append(
            {
                "product_id": product_id,
                "project_id": baseline["project_id"],
                "status": 200,
                "revision": baseline["revision"],
            }
        )
    evidence["result"] = "passed"
    evidence["success"] = True


def verify_scope_history(config, cli, readback, evidence):
    fixture = config.get("product_scopes") or {}
    history = fixture.get("history") or {}
    scopes = fixture.get("scopes") or []
    timestamps = [
        history.get(key) for key in ("valid_from", "unbound_at", "rebound_at")
    ]
    try:
        times = [datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ") for value in timestamps]
    except (TypeError, ValueError):
        times = []
    common.require(
        fixture.get("owned_fixture") is True
        and fixture.get("run_id") == config.get("evaluation_id")
        and fixture.get("recovery_manifest_ref") == config.get("evaluation_id")
        and fixture.get("tenant_id") == config.get("org_id")
        and fixture.get("login_user_id") == config.get("test_user_id")
        and fixture.get("source_revision") == config.get("expected_revision")
        and scope_fixture_inputs(fixture) is not None
        and history.get("project_id") == scopes[1]["project_id"]
        and history.get("relationship_id")
        and type(history.get("linked_revision")) is int
        and len(times) == 3
        and times[0] < times[1] < times[2],
        "Pinned dated link and run-owned Product recovery are required",
    )
    evidence["installed_hashes"] = verify_install(config)
    product_id, project_id = fixture["product_id"], history["project_id"]
    profile_path = (
        "/api/v1/products/"
        + quote(product_id, safe="")
        + "/projects/"
        + quote(project_id, safe="")
    )
    binding_path = profile_path + "/project-bindings"
    first_path = (
        "/api/v1/products/"
        + quote(product_id, safe="")
        + "/projects/"
        + quote(scopes[0]["project_id"], safe="")
    )
    status, before = readback(profile_path)
    common.require(
        status == 200
        and isinstance(before, dict)
        and before.get("product_id") == product_id
        and before.get("project_id") == project_id
        and before.get("tenant_id") == fixture["tenant_id"]
        and before.get("configuration") == scopes[1]["configuration"]
        and before.get("product", {}).get("owner_ref")
        == {"kind": "user", "id": fixture["actor_id"]}
        and type(before.get("revision")) is int
        and before["revision"] >= history["linked_revision"],
        "Dated binding baseline does not match the protected Project",
    )
    status, original = readback(binding_path)
    initial = (
        original.get("items") if status == 200 and isinstance(original, dict) else None
    )
    common.require(
        isinstance(initial, list)
        and len(initial) == 1
        and initial[0].get("revision") == history["linked_revision"]
        and len(initial[0].get("relationship_versions") or []) == 1
        and initial[0]["relationship_versions"][0].get("relationship_id")
        == history["relationship_id"]
        and initial[0]["relationship_versions"][0].get("valid_from") == timestamps[0]
        and "valid_to" not in initial[0]["relationship_versions"][0]
        and detail(
            cli,
            ["project", "bindings", "--product", product_id, "--project", project_id],
        )
        == original,
        "Original binding revision differs from pinned API and installed CLI",
    )
    removed = detail(
        cli,
        [
            "project",
            "unbind",
            "--product",
            product_id,
            "--project",
            project_id,
            "--yes",
            "--revision",
            str(before["revision"]),
            "--idempotency-key",
            "scope_unbind_" + config["evaluation_id"],
        ],
    )
    unbound_status, _ = readback(profile_path)
    first_status, first = readback(first_path)
    project_status, projects = readback("/api/v1/projects")
    common.require(
        unbound_status == 404
        and first_status == 200
        and removed == first
        and first.get("revision") == before["revision"] + 1
        and first.get("product") == before["product"]
        and project_status == 200
        and isinstance(projects, dict)
        and any(
            item.get("id") == project_id and item.get("name") == scopes[1]["name"]
            for item in projects.get("items") or []
        ),
        "Unlink removed the original Project or changed another scope",
    )
    status, closed = readback(binding_path)
    rows = closed.get("items") if status == 200 and isinstance(closed, dict) else None
    common.require(
        isinstance(rows, list)
        and len(rows) == 2
        and rows[0] == initial[0]
        and rows[1].get("revision") == before["revision"] + 1
        and len(rows[1].get("relationship_versions") or []) == 1
        and rows[1]["relationship_versions"][0].get("relationship_id")
        == history["relationship_id"]
        and rows[1]["relationship_versions"][0].get("relationship_revision") == "2"
        and rows[1]["relationship_versions"][0].get("supersedes_revision") == "1"
        and rows[1]["relationship_versions"][0].get("valid_from") == timestamps[0]
        and rows[1]["relationship_versions"][0].get("valid_to") == timestamps[1]
        and detail(
            cli,
            ["project", "bindings", "--product", product_id, "--project", project_id],
        )
        == closed,
        "Unlink failed to preserve the exact historical link revision",
    )
    with tempfile.TemporaryDirectory(prefix="adp-product-scope-history-") as directory:
        selection = Path(directory) / "rebind.json"
        selection.write_text(json.dumps({"mode": "select", "project_id": project_id}))
        os.chmod(selection, 0o600)
        rebound = detail(
            cli,
            [
                "project",
                "bind",
                "--product",
                product_id,
                "--input",
                str(selection),
                "--revision",
                str(first["revision"]),
                "--idempotency-key",
                "scope_rebind_" + config["evaluation_id"],
            ],
        )
    rebound_status, observed = readback(profile_path)
    status, renewed = readback(binding_path)
    latest = (
        renewed.get("items") if status == 200 and isinstance(renewed, dict) else None
    )
    common.require(
        rebound_status == 200
        and rebound == observed
        and isinstance(observed, dict)
        and observed.get("project_id") == project_id
        and observed.get("configuration") == before["configuration"]
        and observed.get("product") == before["product"]
        and observed.get("revision") == before["revision"] + 2
        and isinstance(latest, list)
        and len(latest) == 3
        and latest[:2] == rows
        and latest[2].get("revision") == before["revision"] + 2
        and len(latest[2].get("relationship_versions") or []) == 1
        and latest[2]["relationship_versions"][0].get("relationship_id")
        != history["relationship_id"]
        and latest[2]["relationship_versions"][0].get("relationship_revision") == "1"
        and latest[2]["relationship_versions"][0].get("valid_from") == timestamps[2]
        and "valid_to" not in latest[2]["relationship_versions"][0]
        and detail(
            cli,
            ["project", "bindings", "--product", product_id, "--project", project_id],
        )
        == renewed,
        "Rebind changed the original Project asset or historical revisions",
    )
    evidence["api_readback"] = [
        {"project_id": project_id, "revision": before["revision"], "status": 200},
        {"project_id": project_id, "revision": first["revision"], "status": 404},
        {"project_id": project_id, "revision": observed["revision"], "status": 200},
    ]
    evidence["binding_history"] = renewed["items"]
    evidence["result"] = "passed"
    evidence["success"] = True


def execute(config, evidence):
    session = common.session_tokens(config)
    fixture = config.get("product_scopes") or {}
    with tempfile.TemporaryDirectory(prefix="adp-product-scopes-") as directory:
        home = Path(directory)
        if config.get("mode") == "denials":
            common.require(
                fixture.get("editor_fixture_name")
                and fixture.get("foreign_fixture_name"),
                "Protected editor and foreign login references are required",
            )
            editor_session = ordinary_session(
                config, {"ordinary_fixture_name": fixture["editor_fixture_name"]}
            )
            foreign_session = ordinary_session(
                config, {"ordinary_fixture_name": fixture["foreign_fixture_name"]}
            )
            for name, tokens, tenant_id, user_id in (
                (
                    "editor",
                    editor_session,
                    fixture["tenant_id"],
                    fixture["editor_user_id"],
                ),
                (
                    "foreign",
                    foreign_session,
                    fixture["foreign_tenant_id"],
                    fixture["foreign_user_id"],
                ),
            ):
                status, identity = common.api(
                    config, "/api/auth/me", tokens["access_token"]
                )
                common.require(
                    status == 200
                    and isinstance(identity, dict)
                    and identity.get("org_id") == tenant_id
                    and identity.get("user_id") == user_id
                    and identity.get("is_admin") is False,
                    "Product denial actor does not match the protected ordinary fixture",
                )
                actor_home = home / name
                actor_home.mkdir(mode=0o700)
                _write_session(actor_home, config["gateway_url"], tokens)
            editor_cli = RecordingCli(
                config["cli_path"],
                common.clean_env(
                    config, HOME=home / "editor", ADP_TENANT=fixture["tenant_id"]
                ),
                evidence["transcript"],
                evidence,
                "editor",
            )
            foreign_cli = RecordingCli(
                config["cli_path"],
                common.clean_env(
                    config,
                    HOME=home / "foreign",
                    ADP_TENANT=fixture["foreign_tenant_id"],
                ),
                evidence["transcript"],
                evidence,
                "foreign",
            )
            verify_scope_denials(
                config,
                editor_cli,
                foreign_cli,
                lambda path: common.api(config, path, session["access_token"]),
                evidence,
            )
            return
        common.require(
            config.get("mode") in ("isolation", "history"),
            "Unknown Product scope journey mode",
        )
        _write_session(home, config["gateway_url"], session)
        cli = RecordingCli(
            config["cli_path"],
            common.clean_env(config, HOME=home, ADP_TENANT=config["org_id"]),
            evidence["transcript"],
            evidence,
            "owner",
        )
        if config["mode"] == "history":
            verify_scope_history(
                config,
                cli,
                lambda path: common.api(
                    config, path, session["access_token"], expect=(200, 404)
                ),
                evidence,
            )
        else:
            verify_scope_reads(
                config,
                cli,
                lambda path: common.api(config, path, session["access_token"]),
                evidence,
            )
