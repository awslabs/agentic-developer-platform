#!/usr/bin/env python3
"""Owned usage CLI diagnostic. No inference, control mutations or budget writes.

This is deliberately outside full CLI-15 acceptance: a supplied existing record
is not a new marked inference and an own session is not an admin contrast.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

import common
from capability_contrast import _write_session


def fixture_values(fixture):
    for name in ("run_id", "request_id", "start", "end"):
        value = fixture.get(name)
        common.require(
            isinstance(value, str)
            and 0 < len(value) <= 255
            and not any(c in value for c in "\r\n"),
            f"Supply explicit owned fixture {name}",
        )
    start = datetime.fromisoformat(fixture["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(fixture["end"].replace("Z", "+00:00"))
    common.require(
        start.tzinfo is not None
        and end.tzinfo is not None
        and 0 < (end - start).total_seconds() <= 90 * 86400,
        "Supply bounded timezone-aware fixture dates",
    )
    return fixture


def detail(envelope):
    common.require(
        isinstance(envelope, dict) and envelope.get("status") == "ok",
        "Usage read did not succeed",
    )
    value = envelope.get("detail") or {}
    common.require(
        value.get("scope", {}).get("kind") == "own",
        "Usage diagnostic requires own scope",
    )
    common.require(
        value.get("scope", {}).get("coverage") == "selected_run",
        "Usage was not restricted to the owned run",
    )
    common.require(isinstance(value.get("items"), list), "Usage response lacks records")
    return value


def exercise(cli, fixture, evidence):
    fixture = fixture_values(fixture)
    flags = [
        "--run",
        fixture["run_id"],
        "--start",
        fixture["start"],
        "--end",
        fixture["end"],
    ]
    selector = ["--request-id", fixture["request_id"]]
    summary = detail(cli.json(["usage", "summary", *flags, *selector]))
    requests = detail(
        cli.json(["usage", "requests", *flags, *selector, "--page-size", "1"])
    )
    lookup = detail(
        cli.json(
            ["usage", "request", fixture["request_id"], *flags, "--page-size", "1"]
        )
    )
    common.require(
        requests["items"],
        "Fixture has no visible usage; delayed/missing records do not pass",
    )
    expected_ids = [row.get("id") for row in requests["items"]]
    common.require(
        all(isinstance(i, str) and i for i in expected_ids), "Usage record IDs missing"
    )
    common.require(
        expected_ids == [row.get("id") for row in lookup["items"]],
        "Request lookup and paged request IDs disagree",
    )
    for row in requests["items"]:
        common.require(
            row.get("request_id") == fixture["request_id"]
            and row.get("invocation_id") == fixture["run_id"],
            "Fixture linkage mismatch",
        )
    code, exported = cli.run(
        [
            "logs",
            "export",
            *flags,
            *selector,
            "--page-size",
            "1",
            "--max-pages",
            "1",
            "--format",
            "json",
        ],
        expected=None,
    )
    common.require(isinstance(exported, dict), "Export did not return its envelope")
    export = exported.get("detail") or {}
    common.require(
        code == (0 if export.get("complete") is True else 4),
        "Export exit code hides incomplete pagination",
    )
    common.require(
        exported.get("status")
        == ("ok" if export.get("complete") is True else "pending"),
        "Export completeness status mismatch",
    )
    common.require(
        export.get("scope") == requests.get("scope"),
        "Export scope differs from request read",
    )
    common.require(
        [row.get("id") for row in export.get("items", [])] == expected_ids,
        "Export and request IDs disagree",
    )
    cursor = export.get("next_cursor")
    if export.get("complete") is not True:
        common.require(
            isinstance(cursor, str) and cursor,
            "Bounded export lacks continuation cursor",
        )
        next_code, next_export = cli.run(
            [
                "logs",
                "export",
                *flags,
                *selector,
                "--page-size",
                "1",
                "--max-pages",
                "1",
                "--format",
                "json",
                "--cursor",
                cursor,
            ],
            expected=None,
        )
        common.require(
            isinstance(next_export, dict), "Continuation lacks a JSON envelope"
        )
        more = next_export.get("detail") or {}
        common.require(
            type(more.get("complete")) is bool,
            "Continuation lacks explicit completeness",
        )
        common.require(
            next_export.get("status") == ("ok" if more["complete"] else "pending"),
            "Continuation status mismatch",
        )
        common.require(
            isinstance(more.get("items"), list) and more["items"],
            "Continuation did not return the promised next record",
        )
        for row in more["items"]:
            common.require(
                row.get("request_id") == fixture["request_id"]
                and row.get("invocation_id") == fixture["run_id"],
                "Continuation linkage mismatch",
            )
        if not more["complete"]:
            common.require(
                isinstance(more.get("next_cursor"), str)
                and more["next_cursor"] != cursor,
                "Continuation did not advance its cursor",
            )
        common.require(
            next_code == (0 if more.get("complete") is True else 4),
            "Continuation completeness/exit mismatch",
        )
        common.require(
            more.get("scope") == export.get("scope"), "Continuation scope changed"
        )
        common.require(
            not set(expected_ids) & {row.get("id") for row in more.get("items", [])},
            "Continuation duplicated a record",
        )
    common.require(
        sum(group.get("requests", 0) for group in summary["items"])
        >= len(expected_ids),
        "Summary omitted the selected visible request",
    )
    evidence.update(
        run_id=fixture["run_id"],
        request_id=fixture["request_id"],
        start=fixture["start"],
        end=fixture["end"],
        records_observed=len(expected_ids),
        continuation_exercised=bool(cursor),
        cases=[
            "own-summary",
            "own-request-page",
            "request-lookup",
            "bounded-json-export",
        ],
        qualification="existing owned-record diagnostic only; marked inference/admin/CSV/NDJSON and complete accounting acceptance remain open",
    )


def execute(config, evidence):
    fixture = fixture_values(config.get("usage_readback") or {})
    common.require(config.get("cli_path"), "install_auth did not retain the served CLI")
    with tempfile.TemporaryDirectory(prefix="adp-usage-readback-") as directory:
        home = Path(directory)
        os.chmod(home, 0o700)
        env = common.clean_env(config, HOME=home)
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            path = home / name
            path.mkdir(mode=0o700)
            env[name] = str(path)
        _write_session(home, config["gateway_url"], common.session_tokens(config))
        cli = common.Cli(config["cli_path"], env, evidence["transcript"], timeout=30)
        exercise(cli, fixture, evidence)
