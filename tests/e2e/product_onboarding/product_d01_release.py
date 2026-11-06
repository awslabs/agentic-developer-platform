"""Materialize the signed Gateway release and independent runtime observation for D01."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .product_publish import read_input


REQUIREMENTS_REVISION = "6c875c6b5a29f54bbd94a98dda9a37bc8e411db5"


def materialize(dispatch_path, api_path, output_path):
    issues = []
    dispatch = read_input(dispatch_path, "selected API dispatch", issues)
    api = read_input(api_path, "selected API evidence", issues)
    native = api.get("native") if isinstance(api.get("native"), dict) else {}
    release = native.get("release_artifact") if isinstance(native.get("release_artifact"), dict) else {}
    runtime = native.get("runtime_receipt") if isinstance(native.get("runtime_receipt"), dict) else {}
    inventory = api.get("inventory") if isinstance(api.get("inventory"), dict) else {}
    target = native.get("target") if isinstance(native.get("target"), dict) else {}
    pods = inventory.get("pods") if isinstance(inventory.get("pods"), list) else []
    run = re.fullmatch(r"product-([1-9][0-9]*)-([1-9][0-9]*)", str(dispatch.get("run_id")))
    fields = {
        "source_revision": dispatch.get("expected_revision"),
        "workflow_revision": dispatch.get("workflow_revision"),
        "image_digest": inventory.get("digest"),
    }
    if (
        issues or not run
        or dispatch.get("status") != "pass"
        or dispatch.get("cleanup_verified") is not True
        or dispatch.get("observed_revision") != fields["source_revision"]
        or api.get("run_id") != dispatch.get("run_id")
        or api.get("source_revision") != fields["source_revision"]
        or api.get("workflow_revision") != fields["workflow_revision"]
        or api.get("observation_ref") != dispatch.get("observation_ref")
        or not re.fullmatch(r"[0-9a-f]{40}", str(fields["source_revision"]))
        or not re.fullmatch(r"[0-9a-f]{40}", str(fields["workflow_revision"]))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(fields["image_digest"]))
        or release.get("run_id") != (int(run[1]) if run else None)
        or release.get("run_attempt") != (int(run[2]) if run else None)
        or release.get("component") != "gateway-backend"
        or release.get("workflow_path") != ".github/workflows/gateway-deploy.yml"
        or any(release.get(key) != target.get(key) or not target.get(key)
               for key in ("account_id", "region", "resource_id", "repository_id"))
        or any(release.get(key) != value for key, value in fields.items())
        or not isinstance(release.get("migration_head"), str)
        or not release["migration_head"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(release.get("cli_manifest_sha256")))
        or release.get("product_profile_enabled") is not True
        or release.get("product_onboarding_enabled") is not False
        or runtime.get("component") != "gateway-backend"
        or runtime.get("actual_revision") != fields["source_revision"]
        or runtime.get("image_digest") != fields["image_digest"]
        or runtime.get("migration_head") != release.get("migration_head")
        or runtime.get("cli_manifest_sha256") != release.get("cli_manifest_sha256")
        or runtime.get("product_profile_enabled") is not True
        or runtime.get("product_onboarding_enabled") is not False
        or runtime.get("artifact_hash") != native.get("release_hash")
        or not isinstance(native.get("release_ref"), str)
        or not native["release_ref"].startswith("s3://")
        or "versionId=" not in native["release_ref"]
        or not isinstance(native.get("observation_ref"), str)
        or not native["observation_ref"].startswith("runtime:sha256:")
        or native["observation_ref"] != dispatch.get("observation_ref")
        or native["observation_ref"] == native.get("release_ref")
        or native.get("database_heads") != [release.get("migration_head")] * len(pods)
        or native.get("image_heads") != native.get("database_heads")
        or native.get("pod_uids") != [pod.get("uid") for pod in pods if isinstance(pod, dict)]
        or runtime.get("pod_uids") != native.get("pod_uids")
        or not pods
        or any(not isinstance(pod, dict) or pod.get("digest") != fields["image_digest"] or not pod.get("uid") for pod in pods)
        or native.get("tick_verified") is not True
        or native.get("tick_digest") != fields["image_digest"]
        or runtime.get("tick_digest") != fields["image_digest"]
    ):
        raise ValueError("Selected Product release lacks matched signed and independent runtime evidence")
    report = {
        "plan_revision": f"reviewed-product-design:{REQUIREMENTS_REVISION}",
        "identities": {
            "requirements_revision": REQUIREMENTS_REVISION,
            "source_revision": fields["source_revision"],
            "workflow_revision": fields["workflow_revision"],
            "schema_head": runtime["migration_head"],
            "api_image_digest": fields["image_digest"],
            "cli_checksum": runtime["cli_manifest_sha256"],
        },
        "deployment": {
            "receipt_ref": native["release_ref"],
            "observation_ref": native["observation_ref"],
            "observed": {
                "source_revision": runtime["actual_revision"],
                "workflow_revision": release["workflow_revision"],
                "schema_head": runtime["migration_head"],
                "api_image_digest": runtime["image_digest"],
                "cli_checksum": runtime["cli_manifest_sha256"],
                "serving_pods": [
                    {
                        "pod_ref": pod["uid"],
                        "source_revision": runtime["actual_revision"],
                        "api_image_digest": pod["digest"],
                        "product_profile_enabled": runtime["product_profile_enabled"],
                        "product_onboarding_enabled": runtime["product_onboarding_enabled"],
                    }
                    for pod in pods
                ],
            },
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch-report", required=True)
    parser.add_argument("--api-evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    materialize(args.dispatch_report, args.api_evidence, args.output)


if __name__ == "__main__":
    main()
