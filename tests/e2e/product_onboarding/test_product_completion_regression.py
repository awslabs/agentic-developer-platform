"""Synthetic contract tests, not a deployed Product regression pass."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e.product_onboarding import product_completion_regression as regression
from tests.e2e.product_onboarding.product_completion_release import COMPONENTS


@pytest.fixture
def attempt(tmp_path):
    tmp_path.chmod(0o700)
    environment = {
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": "aws-e/adp/.github/workflows/product-profile-regression.yml@refs/heads/main",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "ADP_CONTRACT_DIGEST": "c" * 64,
        "ADP_CORRELATION": "d" * 64,
        "PRODUCT_ENVIRONMENT": "dev",
        "PRODUCT_SUITE": "product-profile",
        "PRODUCT_MAX_PARALLEL": "8",
    }
    authorization = tmp_path / "engine-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "run_id": 42,
                "run_attempt": 2,
                "source_revision": "a" * 40,
                "workflow_revision": "b" * 40,
                "contract_digest": "c" * 64,
                "correlation": "d" * 64,
                "authorized": True,
            }
        )
    )
    authorization.chmod(0o600)
    manifest = tmp_path / "release-manifest.json"
    record = {
        "schema_version": 1,
        "release_id": "product-40-1",
        "run_id": 40,
        "run_attempt": 1,
        "repository": "aws-e/adp",
        "repository_id": 123,
        "environment": "dev",
        "source_revision": "e" * 40,
        "components": {name: {"verified": True} for name in COMPONENTS},
    }

    def write():
        manifest.write_text(json.dumps(record))
        manifest.chmod(0o600)
        return hashlib.sha256(manifest.read_bytes()).hexdigest()

    return environment, authorization, manifest, record, write


def test_engine_guard_requires_this_run_authorization_and_exact_transport(attempt):
    environment, authorization, manifest, _, write = attempt
    regression.guard_engine(environment, authorization)
    assert (
        regression.select_manifest(
            manifest,
            release_id="product-40-1",
            manifest_sha256=write(),
            environment=environment,
        )["source_revision"]
        == "e" * 40
    )
    for name, value in (
        ("GITHUB_RUN_ATTEMPT", "1"),
        ("ADP_CORRELATION", "0" * 64),
        ("ADP_CONTRACT_DIGEST", "0" * 64),
        ("GITHUB_WORKFLOW_SHA", "0" * 40),
    ):
        changed = {**environment, name: value}
        with pytest.raises(ValueError):
            regression.guard_engine(changed, authorization)
    authorization.unlink()
    with pytest.raises(ValueError, match="missing"):
        regression.guard_engine(environment, authorization)


def test_standalone_never_claims_engine_transport_or_engine_workflow(attempt):
    environment, _, _, _, _ = attempt
    standalone = {
        key: value for key, value in environment.items() if not key.startswith("ADP_")
    }
    standalone["GITHUB_WORKFLOW_REF"] = (
        "aws-e/adp/.github/workflows/product-profile-regression-standalone.yml@refs/heads/main"
    )
    regression.guard_standalone(standalone)
    standalone["GITHUB_EVENT_NAME"] = "schedule"
    regression.guard_standalone(standalone)
    for key in ("ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_CONTRACT_DIGEST"):
        with pytest.raises(ValueError, match="cannot claim engine"):
            regression.guard_standalone({**standalone, key: "0"})
    with pytest.raises(ValueError, match="reviewed main workflow"):
        regression.guard_standalone(environment)


@pytest.mark.parametrize(
    "failure",
    [
        "changed_hash",
        "wrong_release",
        "missing_component",
        "unverified",
        "wrong_repository",
        "symlink",
    ],
)
def test_wrong_or_incomplete_named_release_cannot_be_selected(attempt, failure):
    environment, _, manifest, record, write = attempt
    if failure == "missing_component":
        del record["components"]["gateway-frontend"]
    elif failure == "unverified":
        record["components"]["installed-cli"]["verified"] = False
    elif failure == "wrong_repository":
        record["repository_id"] = 999
    digest = write()
    if failure == "changed_hash":
        digest = "0" * 64
    elif failure == "symlink":
        manifest.rename(manifest.with_name("retained.json"))
        manifest.symlink_to(manifest.with_name("retained.json"))
    with pytest.raises(ValueError):
        regression.select_manifest(
            manifest,
            release_id="product-0-1" if failure == "wrong_release" else "product-40-1",
            manifest_sha256=digest,
            environment=environment,
        )


def test_exact_original_issue_and_release_check_names():
    assert regression.RELEASE_CHECKS == (
        "release-manifest",
        "gateway-runtime",
        "frontend-runtime",
        "installed-cli",
        "release-health",
    )
    assert len(regression.required_checks()) == 31
    assert all(
        f"issue-8273-ac-{index:02}" in regression.required_checks()
        for index in range(1, 4)
    )
    assert regression.required_checks()[:3] == tuple(
        f"issue-8250-ac-{index:02}" for index in range(1, 4)
    )
    assert regression.required_checks()[-4:] == (
        "baseline-regression",
        "repeatability",
        "cleanup",
        "evidence-index",
    )
    assert not any(check.startswith("issue-8276-") for check in regression.required_checks())
    assert "AC-04" in regression.CASES[8276]


def test_both_workflow_modes_emit_only_a_required_check_index(attempt):
    environment, authorization, manifest, _, write = attempt
    digest = write()
    command = [
        sys.executable,
        "-m",
        "tests.e2e.product_onboarding.product_completion_regression",
    ]
    selection = [
        "--manifest",
        str(manifest),
        "--release-id",
        "product-40-1",
        "--manifest-sha256",
        digest,
    ]
    for mode in ("engine", "standalone"):
        variables = {**os.environ, **environment}
        if mode == "standalone":
            variables = {
                key: value
                for key, value in variables.items()
                if not key.startswith("ADP_")
            }
            variables["GITHUB_WORKFLOW_REF"] = (
                "aws-e/adp/.github/workflows/product-profile-regression-standalone.yml@refs/heads/main"
            )
        output = manifest.with_name(f"{mode}-index.json")
        options = ["--authorization", str(authorization)] if mode == "engine" else []
        result = subprocess.run(
            [*command, mode, *selection, *options, "--output", str(output)],
            env=variables,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        index = json.loads(output.read_text())
        assert index == {
            "release_id": "product-40-1",
            "manifest_sha256": digest,
            "mode": mode,
            "required_checks": list(regression.required_checks()),
        }
        assert (output.stat().st_mode & 0o777) == 0o600


def test_standalone_cli_refuses_engine_authorization_without_writing_results(attempt):
    environment, authorization, manifest, _, write = attempt
    standalone = {
        key: value
        for key, value in {**os.environ, **environment}.items()
        if not key.startswith("ADP_")
    }
    standalone["GITHUB_WORKFLOW_REF"] = (
        "aws-e/adp/.github/workflows/product-profile-regression-standalone.yml@refs/heads/main"
    )
    destination = manifest.with_name("standalone-index.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.e2e.product_onboarding.product_completion_regression",
            "standalone",
            "--authorization",
            str(authorization),
            "--manifest",
            str(manifest),
            "--release-id",
            "product-40-1",
            "--manifest-sha256",
            write(),
            "--output",
            str(destination),
        ],
        env=standalone,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not destination.exists()


def test_entrypoints_separate_engine_delivery_from_standalone_results():
    root = Path(__file__).resolve().parents[3] / ".github/workflows"
    engine = yaml.safe_load((root / "product-profile-regression.yml").read_text())
    standalone = yaml.safe_load(
        (root / "product-profile-regression-standalone.yml").read_text()
    )
    transport = {
        "adp_correlation",
        "adp_source_revision",
        "adp_definition_revision",
        "adp_contract_digest",
    }
    assert transport <= set(engine[True]["workflow_dispatch"]["inputs"])
    assert not transport.intersection(standalone[True]["workflow_dispatch"]["inputs"])
    assert "schedule" in standalone[True]
    for name, expected in (
        ("environment", "dev"),
        ("suite", "product-profile"),
        ("max_parallel", "8"),
    ):
        assert engine[True]["workflow_dispatch"]["inputs"][name]["options"] == [
            expected
        ]
    assert (
        "ADP deployment" in engine["run-name"]
        and "ADP deployment" not in standalone["run-name"]
    )
    assert "delivery delivery-evidence " in str(engine["jobs"]["regression"]["steps"])
    assert "delivery delivery-evidence " not in str(standalone["jobs"]["regression"]["steps"])
    assert "workflow-artifact product-regression " in str(standalone["jobs"]["regression"]["steps"])


@pytest.mark.parametrize(
    "release_id",
    [
        "",
        "product-0-1",
        "product-1-0",
        "product-1-1/../2",
        "product-1-1\nname=other",
        "product-" + "9" * 20 + "-1",
    ],
)
def test_release_artifact_rejects_untrusted_identifiers(release_id):
    with pytest.raises(ValueError, match="identifier"):
        regression.release_artifact(release_id)


def test_both_modes_download_exact_named_release_before_selection():
    root = Path(__file__).resolve().parents[3] / ".github/workflows"
    assert regression.release_artifact("product-40-2") == (
        "40",
        "product-profile-release-40-2",
    )
    for filename in (
        "product-profile-regression.yml",
        "product-profile-regression-standalone.yml",
    ):
        workflow = yaml.safe_load((root / filename).read_text())
        steps = workflow["jobs"]["regression"]["steps"]
        download = next(
            step for step in steps if step.get("name") == "Download the authenticated named release manifest"
        )
        assert "tests.e2e.product_onboarding.product_completion_manifest" in download["run"]
        names = [step.get("name") for step in steps]
        selection = (
            "Select the verified shared Product release"
            if filename == "product-profile-regression.yml"
            else "Select a named, verified shared release"
        )
        assert (
            names.index("Establish trusted deployment identity")
            < names.index("Download the authenticated named release manifest")
            < names.index(selection)
        )
        assert workflow["permissions"]["actions"] == "read"


def test_public_operator_handoff_indexes_every_required_check():
    guide = (Path(__file__).resolve().parents[3] /
             "docs/product-onboarding/delivery/product-regression-operations.md").read_text()
    recorded = re.findall(r"`(issue-\d+-ac-\d{2})`", guide)
    required = regression.required_checks()
    assert len(recorded) == len(set(recorded))
    deferred = {f"issue-8276-ac-{number:02}" for number in range(1, 5)}
    assert set(recorded) == set(required[:-4]) | deferred
    for check in (*regression.RELEASE_CHECKS, *required[-4:]):
        assert f"`{check}`" in guide
