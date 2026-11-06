"""Read back immutable shared artifacts for downstream runtime qualification."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from release_context import release_identity


def aws(*arguments):
    return json.loads(subprocess.check_output(
        ["aws", *arguments, "--output", "json"], timeout=60,
    ))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def version(value):
    if not isinstance(value, str) or not value or value in {"None", "null"}:
        raise ValueError("Publication needs immutable storage versions")
    return value


def read_artifacts(wheel, archive, context, environment, *, read=aws):
    if context != release_identity(environment):
        raise ValueError("Publication context belongs to another attempt")
    bucket = environment["RELEASE_BUCKET"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Publication requires its private release bucket")
    wheel, archive = Path(wheel), Path(archive)
    hashes = {"wheel": digest(wheel), "archive": digest(archive)}
    # The existing prefix is a storage compatibility name. Both services use
    # these same package bytes; changing it would break pinned rollback refs.
    prefix = f"l5/security/{context['source_revision']}/{hashes['wheel']}"
    with zipfile.ZipFile(wheel) as packaged:
        required = {"l5_services/workers/execution.py", "l5_services/workers/recovery.py"}
        if context["service"] == "transform":
            required |= {
                "l5_services/adapters/transform/execution.py",
                "l5_services/adapters/transform/retention.py",
                "l5_services/migrations/versions/l5_transform_cleanup.py",
                "l5_services/run_input.py",
            }
        if context["service"] == "finops":
            required |= {"l5_services/adapters/finops/billing.py",
                         "l5_services/finops_reconciliation.py",
                         "l5_services/publication/contributions.py"}
        if context["service"] == "well_architected":
            required |= {
                "l5_services/adapters/well_architected/binding.py",
                "l5_services/adapters/well_architected/generation.py",
                "l5_services/adapters/well_architected/results.py",
                "l5_services/migrations/versions/l5_wa_usage.py",
                "l5_services/migrations/versions/l5_wa_reuse_authorization.py",
                "l5_services/models/wa_reuse_authorization.py",
                "l5_services/migrations/retained_downgrade.py",
                "l5_services/workers/relay.py",
                "l5_services/run_input.py",
            }
        if not required <= set(packaged.namelist()):
            raise ValueError("Release wheel lacks selected service entry points")
        runtime_modules = {name: packaged.read(name) for name in required}
    with zipfile.ZipFile(archive) as workers:
        if (workers.read(f"release/{wheel.name}") != wheel.read_bytes()
                or json.loads(workers.read("release/wheel.json")) != {
                    "wheel_filename": wheel.name, "wheel_sha256": hashes["wheel"]}):
            raise ValueError("Workers do not contain the selected shared wheel")
        if context["service"] in {"finops", "well_architected"}:
            if not required <= set(workers.namelist()):
                raise ValueError("Release archive lacks selected worker modules")
            if any(workers.read(name) != content for name, content in runtime_modules.items()):
                raise ValueError("Release worker modules differ from the selected shared wheel")
    manifest_key = f"{prefix}/manifest.json"
    manifest_version = version(read("s3api", "head-object", "--bucket", bucket,
                                 "--key", manifest_key).get("VersionId"))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest_path = root / "manifest.json"
        observed = read("s3api", "get-object", "--bucket", bucket, "--key", manifest_key,
                        "--version-id", manifest_version, str(manifest_path))
        if observed.get("VersionId") != manifest_version:
            raise ValueError("Manifest storage version changed")
        manifest = json.loads(manifest_path.read_bytes())
        if (manifest.get("source_sha") != context["source_revision"]
                or any(manifest.get(f"{name}_sha256") != value for name, value in hashes.items())):
            raise ValueError("Published manifest differs from the shared build")
        artifacts = {}
        for label, filename in (("wheel", "wheel.whl"), ("archive", "workers.zip")):
            selected_version = version(manifest.get(f"{label}_version"))
            key = f"{prefix}/{filename}"
            target = root / filename
            observed = read("s3api", "get-object", "--bucket", bucket, "--key", key,
                            "--version-id", selected_version, str(target))
            if observed.get("VersionId") != selected_version or digest(target) != hashes[label]:
                raise ValueError("Published artifact differs from its pinned version or build")
            artifacts[label] = {"key": key, "version_id": selected_version, "sha256": hashes[label]}
    return {
        **context,
        "bucket": bucket,
        "manifest": {"key": manifest_key, "version_id": manifest_version},
        "artifacts": artifacts,
    }


def image_evidence(result):
    """State the executor-image disposition of this shared publication.

    Only the Transform release publishes executor images, and it writes its own
    descriptor. The shared workflow retains one evidence set for every service,
    so another service must record that it published no image instead of
    omitting the file and failing the retention of its real release evidence.
    """
    if result["service"] == "transform":
        return None
    return {
        "schema_version": 1,
        "phase": "artifacts",
        "service": result["service"],
        "source_revision": result["source_revision"],
        "run_id": result["run_id"],
        "wheel_sha256": result["artifacts"]["wheel"]["sha256"],
        "archive_sha256": result["artifacts"]["archive"]["sha256"],
        "images_published": False,
        "images": {},
    }


def write_private(destination, payload):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    try:
        wheel, archive, context_path, destination = sys.argv[1:]
        result = read_artifacts(wheel, archive, json.loads(Path(context_path).read_bytes()), os.environ)
        write_private(destination, result)
        images = image_evidence(result)
        if images is not None:
            write_private(str(Path(destination).with_name("validation-images.json")), images)
    except Exception:
        raise SystemExit("Shared release artifact readback failed; no qualification was recorded") from None
