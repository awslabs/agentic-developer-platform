"""Selected dispatch binds signed release bytes to the canonical runtime reader."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from tests.e2e.cli_uplift import (
    config,
    live,
    ports,
    preflight,
    product_release,
    release,
)
from tests.e2e.cli_uplift.ports import PortError
from tests.e2e.product_onboarding import (
    product_execution,
    product_inventory,
    product_native,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"GITHUB_RUN_ID": "43"},
        {"GITHUB_RUN_ATTEMPT": "2"},
        {"GITHUB_REPOSITORY": ""},
        {
            "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/eval-cli-uplift.yml@refs/heads/main"
        },
        {
            "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/gateway-deploy.yml@refs/heads/topic"
        },
    ],
)
def test_cli_release_requires_selected_parent_before_reading_evidence(
    monkeypatch, changes
):
    for key, value in {
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/gateway-deploy.yml@refs/heads/main",
        **changes,
    }.items():
        monkeypatch.setenv(key, value)
    inventory = Mock(side_effect=AssertionError("Must validate parent before I/O"))
    monkeypatch.setattr(product_inventory, "observe", inventory)
    with pytest.raises(PortError, match="selected parent Gateway run"):
        product_release.observe({"product_run_id": "product-42-1"}, object())
    inventory.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing",
        "source",
        "account",
        "run",
        "flags",
        "pod",
        "tick",
        "unavailable",
    ],
)
def test_native_producer_checks_selected_release_and_actual_runtime(
    monkeypatch, failure
):
    source, digest, account = "a" * 40, "sha256:" + "b" * 64, "000000000000"
    for key, value in {
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_REPOSITORY_ID": "17",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_SHA": source,
        "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/gateway-deploy.yml@refs/heads/main",
    }.items():
        monkeypatch.setenv(key, value)
    cfg = {
        "platform_account": account,
        "region": "us-east-1",
        "expected_revision": source,
        "product_run_id": "product-42-1",
        "internal_invoke_url": "https://example.execute-api.us-east-1.amazonaws.com/dev",
    }
    inventory = {
        "source_revision": source,
        "digest": digest,
        "pods": [{"uid": "pod-uid"}],
    }
    artifact = ReleaseArtifact(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        source_revision=source,
        workflow_revision=source,
        workflow_path=".github/workflows/gateway-deploy.yml",
        account_id=account,
        region="us-east-1",
        resource_id="cluster/adp-gateway",
        component="gateway-backend",
        image_digest=digest,
        product_profile_enabled=True,
        product_onboarding_enabled=False,
        product_profile_schema_version="1.0",
        migration_head="head",
        cli_manifest_sha256="c" * 64,
        produced_at=datetime.now(UTC),
    )
    changes = {
        "source": {"source_revision": "d" * 40},
        "account": {"account_id": "000000000001"},
        "run": {"run_id": 43},
        "flags": {"product_onboarding_enabled": True},
    }
    artifact = artifact.model_copy(update=changes.get(failure, {}))
    payload = artifact.model_dump_json().encode()
    stored = Mock(
        return_value=None
        if failure == "missing"
        else (
            payload,
            hashlib.sha256(payload).hexdigest(),
            "s3://private/release?versionId=1",
        )
    )
    monkeypatch.setattr(
        product_native, "EvidenceStore", lambda **_: SimpleNamespace(read=stored)
    )
    monkeypatch.setattr(
        product_native.deployment_provenance,
        "binding",
        lambda _: {"cluster": "cluster", "namespace": "adp-gateway"},
    )
    kube = SimpleNamespace(
        gateway=Mock(
            return_value=(["different" if failure == "pod" else "pod-uid"], "head")
        ),
        close=Mock(),
    )
    constructor = Mock(return_value=kube)
    monkeypatch.setattr(product_native, "KubernetesRuntime", constructor)
    function = {
        "Configuration": {"State": "Active", "LastUpdateStatus": "Successful"},
        "Code": {
            "ResolvedImageUri": f"{account}.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@{digest}"
        },
    }
    if failure == "tick":
        function["Code"]["ResolvedImageUri"] = "other-image"
    tick = Mock(return_value=function)
    if failure == "unavailable":
        tick.side_effect = RuntimeError("private provider details")
    scoped = SimpleNamespace(
        region_name="us-east-1",
        client=lambda *_, **__: SimpleNamespace(get_function=tick),
    )
    aws = SimpleNamespace(
        session=lambda: scoped,
        client=lambda _: object(),
        call=lambda *_args, **_kwargs: {
            "cluster": {
                "name": "cluster",
                "arn": f"arn:aws:eks:us-east-1:{account}:cluster/cluster",
            }
        },
    )
    monkeypatch.setattr(ports, "AwsPort", lambda *_args, **_kwargs: aws)
    monkeypatch.setattr(product_inventory, "observe", lambda *_: inventory)

    def cli_preflight():
        selected, receipt, observed = live.wire(cfg)["product_release_evidence"]()
        record = {"served_cli_hashes": {"command-manifest.json": "c" * 64}}
        preflight.check_product_release(cfg, selected, receipt, observed, record)
        return record

    if failure:
        with pytest.raises(PortError, match="unavailable or mismatched") as error:
            product_execution.native_observation(cfg, inventory, aws)
        assert "private provider" not in str(error.value)
        with pytest.raises(PortError, match="unavailable or mismatched"):
            cli_preflight()
    else:
        result = product_execution.native_observation(cfg, inventory, aws)
        assert result["database_heads"] == result["image_heads"] == ["head"]
        assert result["tick_verified"] and result["tick_digest"] == digest
        assert result["runtime_receipt"]["cli_manifest_sha256"] == "c" * 64
        assert result["release_ref"] == "s3://private/release?versionId=1"
        assert result["release_hash"] == hashlib.sha256(payload).hexdigest()
        assert result["target"]["account_id"] == account
        assert result["target"]["resource_id"] == "cluster/adp-gateway"
        assert constructor.call_args.kwargs == {
            "proof_invoke_url": cfg["internal_invoke_url"]
        }
        assert kube.gateway.call_args.kwargs["product"] == artifact
        assert (
            tick.call_args.kwargs["FunctionName"]
            == f"arn:aws:lambda:us-east-1:{account}:function:adp-dev-orchestration-tick"
        )
        recorded = cli_preflight()["product_release_check"]
        assert recorded["source_revision"] == source
        assert recorded["image_digest"] == digest
        assert recorded["run_attempt"] == 1

        monkeypatch.setattr(product_inventory, "observe", lambda *_: inventory)
        mutation = Mock(
            side_effect=AssertionError(
                "Recovery readiness must precede fixture provisioning"
            )
        )
        monkeypatch.setattr(product_execution.runner, "main", mutation)
        with pytest.raises(
            ValueError, match="cancellation recovery requires private durable state"
        ):
            product_execution.evaluate(cfg, inventory, "product-42-1", aws)
        assert constructor.call_count == 3
        mutation.assert_not_called()
    assert stored.call_args.kwargs["run_id"] == 42
    assert stored.call_args.kwargs["attempt"] == 1


# #8242: the native credential host's pinned pip input is a release artifact.
#
# `release.manifest` reads the installed-file list out of install.sh, so the set
# it must hash is whatever the installer installs. When
# `native-host-requirements.txt` joined CLI_FILES, the blanket "every non-JSON
# artifact starts with #!" rule made the manifest raise ReleaseError and the live
# run died in preflight with no case executed. These tests pin the three artifact
# contracts and the installer's own digest pin, so neither side of the
# served/committed comparison can regress to a shape rule the release violates.

REVISION = "a" * 40

SPA_FALLBACK = b"<!doctype html>\n<html><body>ADP</body></html>\n"


def requirement(name, version, *fills):
    """One pip requirement with its hash pins, in the real file's own layout."""
    pins = [f"    --hash=sha256:{fill * 64}" for fill in fills]
    return f"{name}=={version} \\\n" + " \\\n".join(pins) + "\n"


PINNED_REQUIREMENTS = (
    "# Pinned runtime subset from modules/gateway/uv.lock; wheels only.\n"
    + requirement("cryptography", "50.0.0", "1", "2")
    + requirement("pycparser", "3.0", "3")
).encode()


def native_release(*, requirements=PINNED_REQUIREMENTS, pin=None):
    """A minimal release whose installer list includes the requirements file."""
    files = {
        "adp": b'#!/usr/bin/env bash\nreadonly ADP_VERSION="2.0.0"\n',
        "command-manifest.json": b'{"schema_version": "1.0", "commands": ["login"]}',
        release.NATIVE_REQUIREMENTS: requirements,
    }
    digest = hashlib.sha256(requirements).hexdigest()
    files["install.sh"] = "\n".join(
        [
            "#!/bin/sh",
            f'CLI_FILES="adp command-manifest.json {release.NATIVE_REQUIREMENTS}"',
            f'if [ "${{name}}" = "{release.NATIVE_REQUIREMENTS}" ]; then',
            f'    python3 -I -c digest-check "${{tmp}}" "{pin or digest}"',
            "fi",
            "",
        ]
    ).encode()

    def read(revision, path, *, repo_root=None):
        assert revision == REVISION
        name = path.rsplit("/", 1)[-1]
        if name not in files:
            raise release.ReleaseError(f"{path} does not exist at {revision[:12]}")
        return files[name]

    return files, read


def test_release_manifest_pins_the_native_host_requirements_file():
    files, read = native_release()
    hashes = release.manifest(REVISION, read=read)
    assert set(hashes) == set(files)
    assert (
        hashes[release.NATIVE_REQUIREMENTS]
        == hashlib.sha256(PINNED_REQUIREMENTS).hexdigest()
    )
    assert release.requirement_pins(PINNED_REQUIREMENTS) == {
        "cryptography==50.0.0": [
            "--hash=sha256:" + "1" * 64,
            "--hash=sha256:" + "2" * 64,
        ],
        "pycparser==3.0": ["--hash=sha256:" + "3" * 64],
    }


@pytest.mark.parametrize(
    "requirements",
    [
        SPA_FALLBACK,
        b"",
        b"# nothing but a comment\n",
        b"cryptography==50.0.0\n",
        b"cryptography\n    --hash=sha256:" + b"1" * 64 + b"\n",
        b"cryptography==50.0.0 \\\n    --hash=md5:" + b"1" * 32 + b"\n",
        b"\xff\xfe not text at all\n",
    ],
)
def test_release_manifest_refuses_an_unpinned_native_runtime(requirements):
    _, read = native_release(requirements=requirements)
    with pytest.raises(release.ReleaseError, match="hash-pinned pip requirements"):
        release.manifest(REVISION, read=read)


def test_release_manifest_refuses_an_installer_pin_the_release_cannot_satisfy():
    _, read = native_release(pin="f" * 64)
    with pytest.raises(release.ReleaseError, match="would refuse the released runtime"):
        release.manifest(REVISION, read=read)


def test_release_manifest_still_refuses_a_non_script_helper():
    files, read = native_release()
    files["adp"] = SPA_FALLBACK
    with pytest.raises(release.ReleaseError, match="adp .* is not a script"):
        release.manifest(REVISION, read=read)


def test_served_requirements_are_hashed_and_a_misrouted_one_aborts():
    files, read = native_release()
    expected = release.manifest(REVISION, read=read)
    cfg = {"gateway_url": "https://gw.example.com/api"}
    record = {}

    served = preflight.check_served_cli_hashes(
        cfg,
        expected,
        record,
        fetch=lambda url: files[url.rsplit("/", 1)[-1]],
    )
    assert served == expected
    assert record["release_comparison"]["mismatched"] == []
    assert record["release_comparison"]["missing"] == []
    assert release.NATIVE_REQUIREMENTS in record["served_cli_hashes"]

    def spa(url):
        name = url.rsplit("/", 1)[-1]
        return SPA_FALLBACK if name == release.NATIVE_REQUIREMENTS else files[name]

    with pytest.raises(preflight.PreflightError) as error:
        preflight.check_served_cli_hashes(cfg, expected, {}, fetch=spa)
    assert release.NATIVE_REQUIREMENTS in str(error.value)
    assert "hash-pinned pip requirements" in str(error.value)


def test_the_shipped_installer_and_requirements_file_agree():
    """The real artifacts, not a double: the pin and the file must match on main."""
    cli = Path(__file__).resolve().parents[3] / release.CLI_DIR
    installer = (cli / "install.sh").read_bytes().decode()
    requirements = (cli / release.NATIVE_REQUIREMENTS).read_bytes()

    assert release.NATIVE_REQUIREMENTS in installer
    assert release.artifact_mismatch(release.NATIVE_REQUIREMENTS, requirements) is None
    found = release.INSTALLER_NATIVE_PIN.search(installer)
    assert found, "install.sh no longer pins the native requirements digest"
    assert found.group(1) == hashlib.sha256(requirements).hexdigest()


# Both of preflight's injected `fetch` seams are driven in production by the
# run's own HTTP transport, which raises `ports.PortError` — not
# `PreflightError`. Neither call site accounted for that, so every offline test
# above (which drives them with doubles raising PreflightError) passed while the
# live preflight behaved differently: one refused gateway read surfaced as a bare
# `stage_failure: "PortError"` with no indication of which read was refused. The
# three cases below drive the same seams with the production refusal type, and
# pin the offline type's behaviour so the two cannot be collapsed into one.


def test_a_refused_release_download_names_the_artifact_it_could_not_read():
    """A release the gateway will not serve must still abort — but by name."""
    files, read = native_release()
    expected = release.manifest(REVISION, read=read)
    cfg = {"gateway_url": "https://gw.example.com/api"}

    def refused(url):
        name = url.rsplit("/", 1)[-1]
        if name == release.NATIVE_REQUIREMENTS:
            raise PortError(f"{url} returned HTTP 404")
        return files[name]

    with pytest.raises(preflight.PreflightError) as error:
        preflight.check_served_cli_hashes(cfg, expected, {}, fetch=refused)
    assert release.NATIVE_REQUIREMENTS in str(error.value)
    assert "PortError" in str(error.value)
    assert "HTTP 404" in str(error.value)


def test_a_preflight_refusal_is_not_rewrapped_as_a_transport_refusal():
    """This module's own sentences must reach the report unaltered.

    `check_served_cli_hashes` re-raises `PreflightError` before the catch-all, so
    the redirect refusal keeps its own wording instead of being reported as a
    transport failure. Reordering those two handlers would hide it.
    """
    _, read = native_release()
    expected = release.manifest(REVISION, read=read)
    cfg = {"gateway_url": "https://gw.example.com/api"}

    def refused(url):
        raise preflight.PreflightError(f"{url} redirected; refusing to follow")

    with pytest.raises(preflight.PreflightError) as error:
        preflight.check_served_cli_hashes(cfg, expected, {}, fetch=refused)
    assert str(error.value).endswith("redirected; refusing to follow")
    assert "own transport" not in str(error.value)
    assert "PreflightError" not in str(error.value)


def test_an_unreachable_deployment_binding_blocks_instead_of_aborting():
    """A missing FIXTURE blocks its own cases; only a wrong TARGET aborts."""
    names = [f"dep{index}" for index in range(config.REQUIRED_DEPLOYMENTS)]
    cfg = {
        "deployments": [
            {"name": name, "gateway_url": f"https://{name}.example.com"}
            for name in names
        ]
    }
    discovery = {
        "user_pool_id": "us-east-1_pool",
        "client_id": "client",
        "cli_client_id": "cli-client",
        "identity_pool_id": "",
        "region": "us-east-1",
    }
    record = {}

    def refused(url):
        if url.startswith(f"https://{names[0]}."):
            return discovery
        raise PortError(f"{url} returned HTTP 503")

    assert preflight.check_deployment_bindings(cfg, record, fetch=refused) is False
    observed = record["deployments"]
    # Every binding was attempted: the first refusal did not end the preflight.
    assert [entry["name"] for entry in observed["reachable"]] == [names[0]]
    assert sorted(observed["problems"]) == sorted(names[1:])
    assert "HTTP 503" in observed["problems"][names[1]]
    assert "PortError" in observed["problems"][names[1]]
