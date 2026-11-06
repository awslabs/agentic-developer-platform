"""Installed-client Product denials using the same protected browser attempt."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import quote

from tests.e2e.new_ui.test_product_resume import installed_cli
from tests.e2e.new_ui.test_product_profile import (
    assert_denied_read,
    assert_revoked_read,
)
from tests.e2e.new_ui.test_product_scopes import (
    assert_scope_denial_actors,
    assert_scope_fixture_inputs,
    assert_scope_profile,
)
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v04_cli import install_served_cli
from tests.e2e.product_onboarding.product_v04_evidence import DENIAL_SELECTORS


def read(provider, actor, route, status):
    response = provider.request.get(
        provider.base_url + "/api/v1" + route,
        headers={"Authorization": "Bearer " + actor["access_token"]},
        timeout=30_000,
    )
    if response.status != status:
        raise AssertionError(
            "Protected Product route returned the wrong authorization status"
        )
    return response.json() if status == 200 else None


def foreign_project(provider, fixture, observations):
    outsider = provider.nonmember
    listing = read(provider, outsider, "/projects", 200)
    if (
        not isinstance(listing, dict)
        or not isinstance(listing.get("items"), list)
        or len(listing["items"]) > 100
        or listing.get("next_cursor") is not None
    ):
        raise ValueError("Protected foreign Project discovery is not bounded")
    own_ids = {scope["project_id"] for scope in fixture["scopes"]}
    for item in listing["items"]:
        if (
            not isinstance(item, dict)
            or not item.get("id")
            or not item.get("product_id")
            or item["id"] in own_ids
        ):
            continue
        route = (
            "/products/"
            + quote(item["product_id"], safe="")
            + "/projects/"
            + quote(item["id"], safe="")
        )
        profile = read(provider, outsider, route, 200)
        if (
            profile.get("tenant_id") == outsider["tenant_id"]
            and profile.get("product_id") == item["product_id"]
            and profile.get("project_id") == item["id"]
            and profile.get("product", {}).get("owner_ref")
            == {"kind": "user", "id": outsider["actor_id"]}
        ):
            observations.append({"status": 200, "body": profile})
            return item["id"]
    raise ValueError("Protected foreign actor has no independently readable Project")


def foreign_listing(provider, fixture, route, limit, observations):
    response = provider.request.get(
        provider.base_url + "/api/v1" + route + "?limit=" + str(limit),
        headers={"Authorization": "Bearer " + provider.nonmember["access_token"]},
        timeout=30_000,
    )
    if response.status != 200:
        raise AssertionError("Foreign Product listing was not independently readable")
    page = response.json()
    if (
        not isinstance(page, dict)
        or set(page) != {"items", "next_cursor"}
        or not isinstance(page["items"], list)
        or len(page["items"]) > limit
        or page["next_cursor"] is not None
        or any(
            identifier in json.dumps(page)
            for identifier in (
                fixture["product_id"],
                *[scope["project_id"] for scope in fixture["scopes"]],
            )
        )
    ):
        raise AssertionError("Foreign Product listing disclosed owned data or counts")
    observations.append({"status": 200, "body": page})
    return page


def journal_denial(provider, fixture, label, revision, foreign_id=None):
    scope = provider.state.document.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("cleanup_pending") is not True
        or type(revision) is not int
        or revision <= 0
    ):
        raise ValueError("CLI denied-write intent requires the durable owned scope")
    intended = {
        "key": "scope_cli_" + label + "_" + fixture["run_id"],
        "product_id": fixture["product_id"],
        "project_id": fixture["scopes"][0]["project_id"],
        "foreign_project_id": foreign_id,
        "revision": revision,
    }
    current = scope.get("cli_denial_intents", {}).get(label)
    if current is not None and current != intended:
        raise ValueError("CLI denied-write replay differs from the original intent")
    if current is None:
        changed = deepcopy(scope)
        changed.setdefault("cli_denial_intents", {})[label] = intended
        provider.state.save({**provider.state.document, "scope": changed})
    return intended


def denied(
    binary, checksums, actor, provider, arguments, record, code, error_code, fixture
):
    before = len(record.cli_events)
    try:
        installed_cli(
            binary,
            checksums,
            actor,
            provider.base_url,
            arguments,
            record=record,
            command="product",
        )
    except AssertionError:
        pass
    if len(record.cli_events) != before + 1:
        raise AssertionError("Installed CLI produced no denial event")
    event = record.cli_events[-1]
    try:
        envelope = json.loads(event["stdout"].strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise AssertionError("Installed CLI denial returned no JSON") from None
    if (
        event["exit_code"] != code
        or envelope.get("status") != "failed"
        or not isinstance(envelope.get("error"), dict)
        or envelope["error"].get("code") != error_code
        or envelope.get("detail") not in (None, {})
        or any(
            value in json.dumps(envelope)
            for value in (
                fixture["product_id"],
                *[scope["project_id"] for scope in fixture["scopes"]],
            )
        )
    ):
        raise AssertionError(
            "Installed CLI denial leaked identity or returned the wrong status"
        )
    return envelope


def run_denials(provider, fixture, evidence):
    first, second = assert_scope_fixture_inputs(fixture)
    assert_scope_denial_actors(fixture, provider.owner, provider.nonmember)
    editor = provider.editor
    if (
        editor is None
        or provider.editor_membership is None
        or editor["actor_id"] != provider.binding["editor_id"]
        or editor["tenant_id"] != fixture["tenant_id"]
        or provider.state.document.get("scope", {}).get("team_share", {}).get("team_id")
        != provider.binding["editor_team_id"]
        or fixture["run_id"] != evidence.run_id
        or fixture["source_revision"] != evidence.source
        or not evidence.cli_attempts["AC-01"][0]["result"] == "pass"
    ):
        raise ValueError(
            "Installed denial actors differ from the protected shared-team fixture"
        )
    events = SimpleNamespace(cli_events=[])
    observations = []
    result = "interrupted"
    checksums = {}
    try:
        foreign_id = foreign_project(provider, fixture, observations)
        with TemporaryDirectory(prefix="product-v04-cli-denials-") as home:
            binary, checksums = install_served_cli(provider, evidence, Path(home))
            routes = [
                "/products/"
                + quote(fixture["product_id"], safe="")
                + "/projects/"
                + quote(scope["project_id"], safe="")
                for scope in (first, second)
            ]
            baselines = [read(provider, provider.owner, route, 200) for route in routes]
            for scope, route, baseline in zip((first, second), routes, baselines):
                assert_scope_profile(baseline, fixture, scope)
                if (
                    baseline["product"].get("purpose")
                    != "Verify ordinary editor scope access"
                ):
                    raise AssertionError(
                        "Ordinary editor update was not observed by the installed client"
                    )
                observations.append({"status": 200, "body": baseline})
                shown = installed_cli(
                    binary,
                    checksums,
                    editor,
                    provider.base_url,
                    [
                        "show",
                        "--product",
                        fixture["product_id"],
                        "--project",
                        scope["project_id"],
                    ],
                    record=events,
                    command="product",
                )
                if shown != baseline:
                    raise AssertionError(
                        "Ordinary editor installed CLI differs from independent API"
                    )
                for action in ("show", "history"):
                    denied(
                        binary,
                        checksums,
                        provider.nonmember,
                        provider,
                        [
                            action,
                            "--product",
                            fixture["product_id"],
                            "--project",
                            scope["project_id"],
                        ],
                        events,
                        5,
                        "not_found",
                        fixture,
                    )
                denied(
                    binary,
                    checksums,
                    provider.nonmember,
                    provider,
                    [
                        "project",
                        "bindings",
                        "--product",
                        fixture["product_id"],
                        "--project",
                        scope["project_id"],
                    ],
                    events,
                    5,
                    "not_found",
                    fixture,
                )
                foreign = provider.request.get(
                    provider.base_url + "/api/v1" + route,
                    headers={
                        "Authorization": "Bearer " + provider.nonmember["access_token"]
                    },
                    timeout=30_000,
                )
                assert_denied_read(foreign, (fixture["product_name"], scope["name"]))
                observations.append({"status": 404, "body": {}})
            for arguments, route, limit in (
                (["list"], "/products", 50),
                (["project", "list"], "/projects", 100),
            ):
                expected = foreign_listing(
                    provider, fixture, route, limit, observations
                )
                page = installed_cli(
                    binary,
                    checksums,
                    provider.nonmember,
                    provider.base_url,
                    arguments,
                    record=events,
                    command="product",
                )
                if page != expected:
                    raise AssertionError(
                        "Foreign CLI listing differs from independent authorized API"
                    )
            admin = journal_denial(provider, fixture, "admin", baselines[0]["revision"])
            binding = journal_denial(
                provider, fixture, "bind", baselines[0]["revision"], foreign_id
            )
            admin_file = Path(home) / "admin.json"
            private_json(admin_file, {"team_id": "unauthorized-team"})
            foreign_file = Path(home) / "foreign.json"
            private_json(foreign_file, {"mode": "select", "project_id": foreign_id})
            denied(
                binary,
                checksums,
                editor,
                provider,
                [
                    "update",
                    "--product",
                    fixture["product_id"],
                    "--project",
                    first["project_id"],
                    "--input",
                    str(admin_file),
                    "--revision",
                    str(admin["revision"]),
                    "--idempotency-key",
                    admin["key"],
                ],
                events,
                3,
                "permission_denied",
                fixture,
            )
            denied(
                binary,
                checksums,
                editor,
                provider,
                [
                    "project",
                    "bind",
                    "--product",
                    fixture["product_id"],
                    "--input",
                    str(foreign_file),
                    "--revision",
                    str(binding["revision"]),
                    "--idempotency-key",
                    binding["key"],
                ],
                events,
                5,
                "not_found",
                fixture,
            )
            try:
                provider.editor_membership.revoke(provider.state)
                for scope, route in zip((first, second), routes):
                    for action in ("show", "history"):
                        denied(
                            binary,
                            checksums,
                            editor,
                            provider,
                            [
                                action,
                                "--product",
                                fixture["product_id"],
                                "--project",
                                scope["project_id"],
                            ],
                            events,
                            3,
                            "tenant_membership_revoked",
                            fixture,
                        )
                    denied(
                        binary,
                        checksums,
                        editor,
                        provider,
                        [
                            "project",
                            "bindings",
                            "--product",
                            fixture["product_id"],
                            "--project",
                            scope["project_id"],
                        ],
                        events,
                        3,
                        "tenant_membership_revoked",
                        fixture,
                    )
                    revoked = provider.request.get(
                        provider.base_url + "/api/v1" + route,
                        headers={"Authorization": "Bearer " + editor["access_token"]},
                        timeout=30_000,
                    )
                    assert_revoked_read(
                        revoked, (fixture["product_name"], scope["name"])
                    )
                    observations.append({"status": 403, "body": {}})
                for arguments, route, limit in (
                    (["list"], "/products", 50),
                    (["project", "list"], "/projects", 100),
                ):
                    response = provider.request.get(
                        provider.base_url + "/api/v1" + route + "?limit=" + str(limit),
                        headers={"Authorization": "Bearer " + editor["access_token"]},
                        timeout=30_000,
                    )
                    assert_revoked_read(
                        response, (fixture["product_name"], fixture["product_id"])
                    )
                    observations.append({"status": 403, "body": {}})
                    denied(
                        binary,
                        checksums,
                        editor,
                        provider,
                        arguments,
                        events,
                        3,
                        "tenant_membership_revoked",
                        fixture,
                    )
            finally:
                provider.editor_membership.restore(provider.state)
            for route, baseline in zip(routes, baselines):
                observed = read(provider, provider.owner, route, 200)
                if (
                    observed != baseline
                    or read(provider, editor, route, 200) != baseline
                ):
                    raise AssertionError(
                        "Denied CLI operations or membership restoration changed owned data"
                    )
                observations.append({"status": 200, "body": observed})
            result = "pass"
    finally:
        artifact = private_json(
            evidence.root / "ac-02-cli-result.json",
            {"events": events.cli_events, "result": result},
        )
        readback = private_json(evidence.root / "ac-02-cli-api.json", observations)
        stdout = private_json(
            evidence.root / "ac-02-cli-stdout.json",
            [event["stdout"] for event in events.cli_events],
        )
        evidence.cli_attempts["AC-02"] = [
            {
                "attempt_id": evidence.run_id + "/AC-02/cli",
                "run_id": evidence.run_id,
                "surface": "cli",
                "test_ref": DENIAL_SELECTORS["cli"],
                "result": result,
                "requirements_revision": evidence.context["identities"][
                    "requirements_revision"
                ],
                "source_revision": evidence.source,
                "workflow_run_id": evidence.run,
                "workflow_attempt": evidence.attempt,
                "expected": "Ordinary editor succeeds; foreign and revoked members receive no owned data",
                "actual": "Installed CLI denials and independent API "
                + ("matched" if result == "pass" else "did not complete"),
                "artifact_ref": artifact,
                "readback_ref": readback,
                "stdout_ref": stdout,
                "installed_cli_sha256": checksums.get("adp"),
                "exit_code": 0 if result == "pass" else 1,
            }
        ]
