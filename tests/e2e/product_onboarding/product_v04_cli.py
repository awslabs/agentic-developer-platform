"""Run an installed Product CLI against the protected B03 browser fixture."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import quote
from types import SimpleNamespace

from tests.e2e.new_ui.test_product_resume import installed_cli
from tests.e2e.new_ui.test_product_scopes import (
    assert_scope_fixture_inputs,
    assert_scope_profile,
)
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v04_evidence import ISOLATION_SELECTORS

CLI_SOURCE = Path(__file__).resolve().parents[3] / "modules/gateway/cli"


def install_served_cli(provider, evidence, directory):
    source = CLI_SOURCE
    manifest = source / "command-manifest.json"
    expected = evidence.context["identities"]["cli_checksum"]
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != expected:
        raise ValueError("B03 checked-out CLI manifest differs from the signed backend")
    installer = (source / "install.sh").read_bytes()
    listing = re.search(rb'^CLI_FILES="([^"\n]+)"$', installer, re.MULTILINE)
    if listing is None:
        raise ValueError("B03 checked-out installer has no fixed client inventory")
    names = listing.group(1).decode("ascii").split()
    if len(names) != len(set(names)) or not all(
        re.fullmatch(r"[a-zA-Z0-9_.-]+", name) and (source / name).is_file()
        for name in names
    ):
        raise ValueError("B03 checked-out client inventory is not bounded")
    response = provider.request.get(
        provider.base_url + "/api/cli/install.sh", timeout=30_000
    )
    if (
        response.status != 200
        or hashlib.sha256(response.body()).digest()
        != hashlib.sha256(installer).digest()
    ):
        raise ValueError("B03 served installer differs from the selected source")
    prefix = directory / "bin"
    environment = {
        "HOME": str(directory),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BG_CONFIG_DIR": str(directory / ".bedrock-gateway"),
    }
    result = subprocess.run(
        [
            "sh",
            "-s",
            "--",
            "--gateway-url",
            provider.base_url,
            "--prefix",
            str(prefix),
            "--no-path-edit",
        ],
        input=installer,
        capture_output=True,
        env=environment,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("B03 served CLI installation failed")
    checksums = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names
    }
    if not all(
        (prefix / name).is_file()
        and not (prefix / name).is_symlink()
        and hashlib.sha256((prefix / name).read_bytes()).hexdigest() == checksums[name]
        for name in names
    ) or not os.access(prefix / "adp", os.X_OK):
        raise ValueError("B03 installed client differs from the selected source")
    return prefix / "adp", checksums


def run_isolation(provider, fixture, evidence):
    if (
        fixture["run_id"] != evidence.run_id
        or fixture["source_revision"] != evidence.source
    ):
        raise ValueError("B03 installed CLI cannot use a foreign owned scope fixture")
    first, second = assert_scope_fixture_inputs(fixture)
    if (
        evidence.context is None
        or evidence.context["identities"]["source_revision"] != evidence.source
    ):
        raise ValueError("B03 installed CLI has no correlated signed release")
    events = SimpleNamespace(cli_events=[])
    reads = []
    evidence.cli_attempts = getattr(evidence, "cli_attempts", {})
    status = "interrupted"
    checksums = {}
    try:
        with TemporaryDirectory(prefix="product-v04-cli-") as home:
            binary, checksums = install_served_cli(provider, evidence, Path(home))
            root = provider.base_url + "/api/v1/products"
            headers = {"Authorization": "Bearer " + provider.owner["access_token"]}
            listing = provider.request.get(root, headers=headers, timeout=30_000)
            if listing.status != 200:
                raise AssertionError(
                    "B03 independent Product binding list is unavailable"
                )
            rows = listing.json().get("items")
            bound = (
                [item for item in rows if item.get("id") == fixture["product_id"]]
                if isinstance(rows, list)
                else []
            )
            if len(bound) != 1 or set(bound[0].get("project_ids") or ()) != {
                first["project_id"],
                second["project_id"],
            }:
                raise AssertionError(
                    "B03 installed CLI fixture differs from authoritative bindings"
                )
            reads.append({"status": listing.status, "body": listing.json()})
            identity = None
            for scope in (first, second, first):
                shown = installed_cli(
                    binary,
                    checksums,
                    provider.owner,
                    provider.base_url,
                    [
                        "show",
                        "--product",
                        fixture["product_id"],
                        "--project",
                        scope["project_id"],
                    ],
                    command="product",
                    record=events,
                )
                route = (
                    root
                    + "/"
                    + quote(fixture["product_id"], safe="")
                    + "/projects/"
                    + quote(scope["project_id"], safe="")
                )
                response = provider.request.get(route, headers=headers, timeout=30_000)
                if response.status != 200:
                    raise AssertionError("B03 independent scoped API read failed")
                observed = response.json()
                reads.append({"status": response.status, "body": observed})
                product = assert_scope_profile(observed, fixture, scope)
                if shown != observed or (identity is not None and identity != product):
                    raise AssertionError(
                        "B03 installed CLI and API disagree on shared identity or isolated scope"
                    )
                identity = product
            status = "pass"
    finally:
        root = evidence.root
        artifact = private_json(
            root / "ac-01-cli-result.json",
            {"events": events.cli_events, "result": status},
        )
        readback = private_json(root / "ac-01-cli-api.json", reads)
        stdout = private_json(
            root / "ac-01-cli-stdout.json",
            [event["stdout"] for event in events.cli_events],
        )
        evidence.cli_attempts["AC-01"] = [
            {
                "attempt_id": evidence.run_id + "/AC-01/cli",
                "run_id": evidence.run_id,
                "surface": "cli",
                "test_ref": ISOLATION_SELECTORS["cli"],
                "result": status,
                "requirements_revision": evidence.context["identities"][
                    "requirements_revision"
                ],
                "source_revision": evidence.source,
                "workflow_run_id": evidence.run,
                "workflow_attempt": evidence.attempt,
                "expected": "Shared Product identity and isolated scoped data",
                "actual": "Installed CLI and independent scoped API "
                + ("matched" if status == "pass" else "did not complete"),
                "artifact_ref": artifact,
                "readback_ref": readback,
                "stdout_ref": stdout,
                "installed_cli_sha256": checksums.get("adp"),
                "exit_code": 0 if status == "pass" else 1,
            }
        ]


def write_cli_report(evidence):
    if not getattr(evidence, "cli_attempts", None):
        return
    private_json(
        evidence.root / "product-v04-cli-results.json",
        {
            "run_id": evidence.run_id,
            "source_revision": evidence.source,
            "workflow_revision": evidence.workflow,
            "workflow_run_id": evidence.run,
            "workflow_attempt": evidence.attempt,
            "release_manifest_sha256": evidence.context["identities"]["cli_checksum"],
            "installed_cli_sha256": evidence.cli_attempts["AC-01"][0][
                "installed_cli_sha256"
            ],
            "cases": {
                criterion: {
                    "attempts": attempts,
                    "api_attempts": getattr(evidence, "api_attempts", {}).get(
                        criterion, []
                    ),
                }
                for criterion, attempts in evidence.cli_attempts.items()
            },
            "cleanup": {
                "status": "verified"
                if getattr(evidence, "cleanup", None)
                and evidence.cleanup["verified"]
                and evidence.cleanup.get("original_projects_preserved")
                and evidence.cleanup.get("reused_assets_preserved")
                else "incomplete",
                "original_projects_preserved": bool(
                    getattr(evidence, "cleanup", None)
                    and evidence.cleanup.get("original_projects_preserved")
                ),
                "reused_assets_preserved": bool(
                    getattr(evidence, "cleanup", None)
                    and evidence.cleanup.get("reused_assets_preserved")
                ),
            },
        },
    )
