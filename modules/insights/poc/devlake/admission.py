import hashlib
import json
import os
import re
from pathlib import Path

import finalize
import migration_receipt
import publish


SCHEMA = Path(__file__).with_name("schema-manifest.json")
RECEIPT_FIELDS = {"component", "revision", "image_digest", "repository_uri", "schema_sha256", "sbom_sha256"}


def image_preflight(api_receipt, worker_receipt=None):
    receipts = {"api": api_receipt}
    if worker_receipt is not None:
        receipts["worker"] = worker_receipt
    schema_digest = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    for component, receipt in receipts.items():
        if (set(receipt) != RECEIPT_FIELDS or receipt["component"] != component or
                receipt["schema_sha256"] != schema_digest or
                not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["image_digest"]) or
                not re.fullmatch(r"[0-9a-f]{64}", receipt["sbom_sha256"])):
            raise ValueError("checked publication receipt or schema digest differs")

    scope, _, record = finalize.get_registered("publisher")
    if record.get("finalization_requested") or os.environ.get("GITHUB_ACTOR") != record["operator"]:
        raise ValueError("only the active registered operator may install")
    _, _, account, region, _, _, label = scope
    for component, receipt in receipts.items():
        artifact = record["artifacts"].get(component)
        publish.verify_target(receipt["repository_uri"], account, region, "devlake-poc/" + component)
        if (not artifact or artifact["state"] not in ("registered", "pending", "accepted") or
                receipt["revision"] != record["finalizer_revision"] or
                receipt["repository_uri"] != artifact["repository_uri"] or
                receipt["image_digest"] != artifact["digest"]):
            raise ValueError("published image differs from protected resource intent")
    for component, receipt in receipts.items():
        artifact = record["artifacts"][component]
        finalize.preflight(receipt["repository_uri"], receipt["image_digest"], component)
        response = finalize.aws(label, "ecr", "batch-get-image", "--region", region,
                                "--repository-name", "devlake-poc/" + component,
                                "--image-ids", "imageTag=" + artifact["tag"])
        images = response.get("images", [])
        if (response.get("failures") or len(images) != 1 or
                images[0].get("registryId") != account or
                images[0].get("repositoryName") != "devlake-poc/" + component or
                images[0].get("imageId") != {"imageDigest": artifact["digest"], "imageTag": artifact["tag"]}):
            raise ValueError("independent ECR image readback differs from checked receipt")
    return {component: receipt["repository_uri"] + "@" + receipt["image_digest"]
            for component, receipt in receipts.items()}


def load_receipt(path):
    return json.loads(Path(path).read_text())


def installation_preflight(api_receipt, worker_receipt, target):
    schema_digest = migration_receipt.verify_migrated(migration_receipt.readback, **target)
    images = image_preflight(api_receipt, worker_receipt)
    if api_receipt["schema_sha256"] != schema_digest or worker_receipt["schema_sha256"] != schema_digest:
        raise ValueError("migration_required: deployed images differ from applied schema")
    return images
