"""Select live fixtures from the named release's authenticated observations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from tests.e2e.product_onboarding.product_completion_regression import select_manifest
from tests.e2e.product_onboarding.product_shared_release import verify_shared_source_lineage
from tests.e2e.new_ui.product_fixture_bootstrap import validate_configuration


def fixture_environment(manifest, observations, configuration, environment, *,
                        lineage_verified=False):
    """Return only non-secret setup values; never change fixture configuration."""
    from src.orchestration.deployment_runtime_contract import ReleaseArtifact

    origin = configuration.get("base_url", "")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != parsed.hostname
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Product regression fixture origin is invalid")
    validate_configuration(configuration, base_url=origin)
    source = manifest["source_revision"]
    if not lineage_verified and environment.get("ADP_SOURCE_REVISION") not in (None, "", source):
        raise ValueError("Selected Product release differs from the engine source")
    selected = {}
    for name, workflow in (("gateway-backend", "gateway-deploy.yml"),
                           ("gateway-frontend", "gateway-frontend-deploy.yml")):
        receipt = manifest["components"][name]
        observed = observations[name]
        release = ReleaseArtifact.model_validate(observed["release"])
        if release.component != name:
            raise ValueError("Product fixture observation has the wrong component")
        digest = (release.image_digest.removeprefix("sha256:") if name == "gateway-backend"
                  else hashlib.sha256(json.dumps(release.assets, sort_keys=True).encode()).hexdigest())
        if (release.component != name or release.repository_id != manifest["repository_id"]
                or release.account_id != environment["ACCOUNT_ID"]
                or release.region != environment["PRODUCT_TARGET_REGION"]
                or release.workflow_path != ".github/workflows/" + workflow
                or release.source_revision != receipt["observed_source_revision"]
                or digest != receipt["observed_sha256"] or digest != receipt["desired_sha256"]
                or receipt.get("verified") is not True or not observed.get("release_ref")):
            raise ValueError("Product fixture release differs from the authenticated manifest")
        selected[name] = release
    backend, frontend = selected["gateway-backend"], selected["gateway-frontend"]
    if backend.product_profile_enabled is not True or backend.product_onboarding_enabled is not True:
        raise ValueError("The named Product release has not enabled onboarding")
    pairing = {"run_id": backend.run_id, "attempt": backend.run_attempt,
               "workflow_revision": backend.workflow_revision}
    if ({key: str(value) for key, value in pairing.items()}
            != {key: str(value) for key, value in configuration["backend_release"].items()}):
        raise ValueError("Maintained Product fixtures must be paired with the named release backend")
    values = {
        "PRODUCT_EXPECTED_REVISION": source,
        "PRODUCT_RELEASE_RUN_ID": str(frontend.run_id),
        "PRODUCT_RELEASE_ATTEMPT": str(frontend.run_attempt),
        "PRODUCT_RELEASE_WORKFLOW_REVISION": frontend.workflow_revision,
        "PRODUCT_BACKEND_RELEASE_RUN_ID": str(backend.run_id),
        "PRODUCT_BACKEND_RELEASE_ATTEMPT": str(backend.run_attempt),
        "PRODUCT_BACKEND_RELEASE_WORKFLOW_REVISION": backend.workflow_revision,
        "PRODUCT_BROWSER_WORKFLOW_REVISION": environment["GITHUB_WORKFLOW_SHA"],
        "E2E_CLOUDFRONT_URL": origin.rstrip("/"),
        "E2E_NEW_UI_ENABLED": "1", "E2E_NEW_UI_EXPECTED_FLAG": "on",
        "PRODUCT_B03_MODE": "false", "PRODUCT_V11_MODE": "false",
    }
    for version in ("06", "07", "08", "09", "10"):
        values.update({f"PRODUCT_V{version}_LIVE_ENABLED": "1",
                       f"PRODUCT_V{version}_SOURCE_REVISION": source,
                       f"PRODUCT_V{version}_REQUIREMENTS_REVISION": configuration["requirements_revision"]})
    if any(not isinstance(value, str) or "\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Product fixture setup cannot contain multiline values")
    return values


def load_observations(manifest, environment, store):
    result = store.read_archive(
        account=environment["ACCOUNT_ID"], environment=environment["ENVIRONMENT"],
        region=environment["PRODUCT_TARGET_REGION"], repository=manifest["repository"],
        repository_id=manifest["repository_id"], run_id=manifest["run_id"],
        attempt=manifest["run_attempt"], workflow_path=".github/workflows/product-profile-release.yml",
        workflow_revision=manifest["workflow_revision"], source_revision=manifest["source_revision"],
        kind="workflow-artifact", name="product-profile-observations",
    )
    if result is None:
        raise ValueError("The named Product release has no authenticated component observations")
    with zipfile.ZipFile(io.BytesIO(result[0])) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len(entries) > 64 or len(set(names)) != len(names)
                or any(entry.is_dir() or entry.file_size > 1024 * 1024 for entry in entries)):
            raise ValueError("Product observations archive is malformed or exceeds its bound")
        result = {}
        for component in ("gateway-backend", "gateway-frontend"):
            if manifest["components"][component].get("evidence_ref") != f"evidence/{component}.json":
                raise ValueError("Product component reference differs from its observation")
            if component + ".json" not in names:
                raise ValueError("Product observations are missing a component")
            result[component] = json.loads(archive.read(component + ".json"))
        return result


def main():
    import boto3
    from deployment_evidence.store import EvidenceStore
    from tests.e2e.new_ui.product_fixture_bootstrap import read_configuration

    environment = os.environ
    root = Path(environment["RUNNER_TEMP"]) / "product-profile-regression"
    manifest = select_manifest(root / "release-manifest.json",
        release_id=environment["PRODUCT_RELEASE_ID"],
        manifest_sha256=environment["PRODUCT_RELEASE_MANIFEST_SHA256"], environment=environment)
    observations = load_observations(manifest, environment, EvidenceStore())
    config = read_configuration(boto3.client("secretsmanager"), environment["ENVIRONMENT"],
                                scope_editor_enabled=True)
    values = fixture_environment(
        manifest, observations, config, environment,
        lineage_verified=verify_shared_source_lineage(manifest, environment))
    with Path(environment["GITHUB_ENV"]).open("a") as output:
        for key, value in values.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]+", key):
                raise ValueError("Invalid Product setup key")
            output.write(f"{key}={value}\n")
    print("Named Product release and protected fixture pairing verified; no fixtures allocated.")


if __name__ == "__main__":
    main()
