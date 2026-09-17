#!/usr/bin/env python3
"""Offline historical counterexamples for the 2026-09-17 rework audit.

Run from an ADP checkout containing the pinned Git objects. No network, AWS,
real credentials or user configuration are used. These reproduce old failures;
they are not current-product regression tests or evidence of live acceptance.
"""

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace


def historical_functions(revision, path, names, **bindings):
    source = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        check=True, capture_output=True, text=True,
    ).stdout
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == set(names)
    namespace = dict(globals(), **bindings)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), namespace)
    return SimpleNamespace(**namespace)


def main():
    observed = {}
    cli = "modules/gateway/cli/"
    with tempfile.TemporaryDirectory(prefix="adp-history-probe-") as temporary:
        config = Path(temporary) / "config.json"
        config.write_text(json.dumps({"identity_pool_id": "existing-pool",
                                      "gateway_url": "https://example.invalid"}))
        common = historical_functions(
            "3d22b4a039", cli + "adp_common.py",
            ["CliError", "private_directory", "write_json", "save_session"],
            config_path=lambda: config,
        )
        common.save_session(dict(client_id="fixture", user_pool_id="fixture",
                                 region="us-east-1", access_token="synthetic",
                                 id_token="synthetic", refresh_token="synthetic",
                                 expires_in=60))
        value = json.loads(config.read_text())["identity_pool_id"]
        assert value == "", "Pinned failure no longer reproduced"
        observed["existing_identity_pool_after_login"] = value

    admin = historical_functions(
        "18ff18db70", cli + "adp-github-admin.py", ["owner_choice"],
        CliError=common.CliError,
    )
    with contextlib.redirect_stderr(io.StringIO()):
        value = admin.owner_choice(SimpleNamespace(
            owner="user", github_org="fixture-org", org="fixture-tenant"), False)
    assert value == ("user", None)
    observed["contradictory_owner_and_org"] = value

    # Supply only API/state boundaries. Keep the actual connect, matching,
    # provenance, state-clearing and output code in the exercised path.
    saved = {"gateway_url": "https://example.invalid",
             "requested_repository": "fixture-org/repo", "awaiting": "approval"}
    writes = []
    boundary = SimpleNamespace(
        read_state=lambda name: saved,
        write_state=lambda name, value: writes.append(value),
        envelope=lambda status, command, detail, *rest: {"status": status},
    )
    user = historical_functions(
        "bf45250531", cli + "adp-github.py",
        ["platform_app_missing", "connect", "parse_repo", "connections",
         "grants_repo", "repositories_of", "repositories_proven", "detail_of",
         "repository_access", "pending_request", "clear_request"],
        common=boundary, CliError=common.CliError, NAME="fixture-state",
        CONNECTIONS="/fixture-connections", _REPO=re.compile(r"^([^/]+)/([^/]+)$"),
    )
    value = user.platform_app_missing(common.CliError(
        "ADP returned HTTP 503 (gateway_unavailable)."))
    assert value is True
    observed["infrastructure_503_classified_as_missing_app"] = value
    api = SimpleNamespace(base=saved["gateway_url"], request=lambda *args: {
        "connections": [{"repositories": ["fixture-org/repo"],
                         "verification": {"repositories_live": True}}]})
    user.connect(SimpleNamespace(repo="fixture-org/repo", org=None, dry_run=True), api)
    assert writes == [{}]
    observed["dry_run_reuse_state_writes"] = writes

    # One unrelated successful row is all the API returns; no Codex request
    # exists. Run the real selector and polling predicate, without waiting.
    row = {"status_code": 200, "timestamp": "2026-09-17T10:00:00Z",
           "request_id": "unrelated-claude-request", "model": "claude"}
    boundary = SimpleNamespace(
        api=lambda *args: (200, {"items": [row]}),
        load_session=lambda config: {"access_token": "synthetic"},
        wait_for=lambda predicate, **kwargs: predicate(),
    )
    inference = historical_functions(
        "3c88c8a2", "tests/e2e/cli_uplift/remote/personal_inference.py",
        ["_usage_record"], common=boundary,
    )
    value = inference._usage_record(
        {"org_id": "fixture-org", "test_user_id": "fixture-user"},
        "this-run-codex", after="2026-09-17T09:59:00Z",
    )
    assert value == row
    observed["codex_marker_selects_unrelated_claude_usage"] = value["request_id"]
    print(json.dumps({"historical_counterexamples_reproduced": 5,
                      "observed": observed}, indent=2))


if __name__ == "__main__":
    main()
