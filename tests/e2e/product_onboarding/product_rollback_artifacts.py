"""Qualify the installed rollback baseline before creating a held Product."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

import jwt

from deployment_evidence.store import (
    EvidenceStore,
    MAX_ENVELOPE,
    bucket_name,
    object_key,
)
from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from tests.e2e.cli_uplift import deployment_provenance

ROOT = Path(__file__).resolve().parents[3]
# Verified installed schema-131 release, including the operations retry fixes.
# The former schema-123 baseline cannot qualify against the current candidate:
# compatible() deliberately requires equal schema heads before any mutation.
BASELINE_SOURCE = "0af3d490af4f85ced80042367ee7071d864961ea"
BASELINE_RUN = 39969181872
BASELINE_ATTEMPT = 1
spec = importlib.util.spec_from_file_location(
    "product_release_qualification",
    ROOT / "modules/gateway/scripts/product-release-qualification.py",
)
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def read_baseline(aws, *, account, region, repository, repository_id):
    """Use an untrusted signature SHA only to locate its verified, same-version receipt."""
    store = EvidenceStore(s3=aws.client("s3"))
    bucket = bucket_name(account, "dev")
    key = object_key(
        repository_id, BASELINE_RUN, BASELINE_ATTEMPT, "release", "gateway-backend"
    )
    response = store.s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    try:
        if not 0 < response.get("ContentLength", 0) <= MAX_ENVELOPE:
            raise ValueError("Product baseline envelope exceeds evidence bound")
        raw = response["Body"].read(MAX_ENVELOPE + 1)
    finally:
        response["Body"].close()
    version = response.get("VersionId")
    if (
        len(raw) != response["ContentLength"]
        or not version
        or version == "null"
        or response.get("ServerSideEncryption") != "AES256"
    ):
        raise ValueError("Product baseline lacks immutable encrypted evidence")
    claims = jwt.decode(
        json.loads(raw)["signature"], options={"verify_signature": False}
    )
    revision = (
        claims.get("job_workflow_sha")
        if "job_workflow_ref" in claims
        else claims.get("workflow_sha")
    )
    if not re.fullmatch(r"[a-f0-9]{40}", str(revision)):
        raise ValueError("Product baseline has no pinned workflow revision")
    record = store.read(
        account=account,
        environment="dev",
        region=region,
        repository=repository,
        repository_id=repository_id,
        run_id=BASELINE_RUN,
        attempt=BASELINE_ATTEMPT,
        kind="release",
        name="gateway-backend",
        workflow_path=".github/workflows/gateway-deploy.yml",
        workflow_revision=revision,
    )
    reference = f"s3://{bucket}/{key}?versionId={quote(version, safe='')}"
    if record is None or record[2] != reference:
        raise ValueError("Product baseline evidence changed between signed reads")
    release = ReleaseArtifact.model_validate_json(record[0]).model_dump(mode="json")
    if (
        release["source_revision"] != BASELINE_SOURCE
        or release["workflow_revision"] != revision
        or release["run_id"] != BASELINE_RUN
        or release["run_attempt"] != BASELINE_ATTEMPT
        or release["repository_id"] != repository_id
    ):
        raise ValueError("Product baseline is not the reviewed installed release")
    return release, reference


def is_ancestor(older, newer):
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", older, newer],
            cwd=ROOT,
            timeout=20,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def qualify_baseline(
    cfg,
    native,
    aws,
    *,
    baseline_reader=read_baseline,
    ancestor=is_ancestor,
    immutable_source=deployment_provenance.immutable_source,
):
    candidate = native["release_artifact"]
    target = native["target"]
    baseline, reference = baseline_reader(
        aws,
        account=target["account_id"],
        region=target["region"],
        repository=os.environ["GITHUB_REPOSITORY"],
        repository_id=target["repository_id"],
    )
    selected = deployment_provenance.binding(cfg)
    if (
        target["account_id"] != str(cfg["platform_account"])
        or target["region"] != cfg["region"]
        or target["resource_id"] != selected["cluster"] + "/" + selected["namespace"]
        or not isinstance(native.get("release_ref"), str)
        or "versionId=" not in native["release_ref"]
    ):
        raise ValueError(
            "Product baseline target differs from independently observed candidate"
        )
    runtime_receipt = native["runtime_receipt"]
    runtime = {
        "source_revision": runtime_receipt["actual_revision"],
        "workflow_revision": candidate["workflow_revision"],
        "image_digest": runtime_receipt["image_digest"],
        "migration_head": runtime_receipt["migration_head"],
        "product_profile_enabled": runtime_receipt["product_profile_enabled"],
        "product_onboarding_enabled": runtime_receipt["product_onboarding_enabled"],
        **{field: target[field] for field in ("account_id", "region", "resource_id")},
    }
    images = {
        candidate["source_revision"]: candidate["image_digest"],
        baseline["source_revision"]: baseline["image_digest"],
    }

    def resolve(source):
        digest = images[source]
        return (
            digest
            if immutable_source(aws, selected, digest)["source_sha"] == source
            else None
        )

    qualification.compatible(
        candidate, baseline, runtime, is_ancestor=ancestor, resolve_image=resolve
    )
    return candidate, baseline, reference
