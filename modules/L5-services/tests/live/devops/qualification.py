"""Fail-closed offline oracle for DA19's private live readbacks."""

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from native_usage import verify_native_usage

ROOT = Path(__file__).resolve().parents[5]
MATRIX = ROOT / "docs/l5-services/devopsagnent/capability-matrix.json"
REVISION = "R3-secure-native-ui-ingress"
ARTIFACTS = ("api", "ui", "cli", "mcp", "worker")
JOURNEYS = ("product", "ui", "cli", "mcp")


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is not None:
            return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        pass
    raise ValueError("evidence needs an aware observation time")


def digest(value, size):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % size, value) is not None


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("requirements_revision") != REVISION:
        raise ValueError("accepted R3 requirements revision required")
    release = manifest.get("release", {})
    if (not isinstance(release, dict) or not digest(release.get("source_sha"), 40)
            or not release.get("deployment_id") or not isinstance(release.get("artifacts"), dict)
            or set(release["artifacts"]) != set(ARTIFACTS)
            or not all(digest(value, 64) for value in release["artifacts"].values())):
        raise ValueError("release must pin source, deployment and every artifact digest")
    timestamp(release.get("deployed_at"))
    if not manifest.get("fixture_owner") or manifest.get("authorized") is not True or not manifest.get("target_ref"):
        raise ValueError("qualification needs an authorized private owner and target")
    return manifest


def load_manifest(path):
    supplied = Path(path)
    if not supplied.is_absolute() or supplied.is_relative_to(ROOT):
        raise ValueError("manifest must be outside the checkout")
    descriptor = os.open(supplied, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077
                or supplied.resolve().is_relative_to(ROOT)):
            raise ValueError("manifest must be private regular file outside checkout")
        with os.fdopen(descriptor, "r", closefd=False) as source:
            return validate_manifest(json.load(source))
    finally:
        os.close(descriptor)


def verify_release_artifacts(manifest, reader, *, now=None):
    validate_manifest(manifest)
    release = manifest["release"]
    observed = reader.read_release(manifest["target_ref"])
    if (not isinstance(observed, dict)
            or any(observed.get(key) != release[key] for key in ("source_sha", "deployment_id", "artifacts"))
            or not timestamp(release["deployed_at"]) <= timestamp(observed.get("observed_at")) <= (now or datetime.now(UTC))):
        raise ValueError("independent deployed release differs from pinned artifacts or is stale")
    return observed


def verify_release(manifest, reader, *, now=None, qualification_run_id=None):
    """Reader must fetch target state independently; submitted claims never count."""
    observed = verify_release_artifacts(manifest, reader, now=now)
    release = manifest["release"]
    target = manifest["target_ref"]
    ingress = reader.read_ingress(target)
    if (not isinstance(ingress, dict) or ingress.get("browser_launch_route") != "gateway"
            or ingress.get("function_urls") != [] or ingress.get("public_launchers") != []
            or any(ingress.get(key) != release[key] for key in ("source_sha", "deployment_id"))):
        raise ValueError("deployed ingress does not prove gateway-only SSO")
    capabilities = {item["id"] for item in json.loads(MATRIX.read_text())["capabilities"]}
    results = reader.read_capabilities(target)
    if (not isinstance(results, list) or len(results) != len(capabilities)
            or any(not isinstance(item, dict) or item.get("capability") not in capabilities
                   or item.get("deployment_id") != release["deployment_id"]
                   or item.get("status") != "completed" or not item.get("native_id")
                   or not item.get("readback_ref")
                   or (qualification_run_id and item.get("qualification_run_id") != qualification_run_id)
                   for item in results)
            or {item["capability"] for item in results} != capabilities):
        raise ValueError("every native capability needs separate scoped readback")
    if qualification_run_id:
        for result in results:
            usage = result.get("usage", {})
            limit = manifest["limits"][result["capability"]]
            verify_native_usage(usage, limit)

    journeys = reader.read_journeys(target)
    if (not isinstance(journeys, list) or len(journeys) != len(JOURNEYS)
            or any(not isinstance(item, dict) for item in journeys)
            or {item.get("entry_point") for item in journeys} != set(JOURNEYS)
            or any(item.get("deployment_id") != release["deployment_id"]
                   or item.get("status") != "completed" or not item.get("readback_ref")
                   or (qualification_run_id and item.get("qualification_run_id") != qualification_run_id)
                   for item in journeys)):
        raise ValueError("Product, UI, CLI and MCP need separately observed journeys")
    return {"requirements_revision": REVISION, "release": observed, "ingress": ingress,
            "capabilities": results, "journeys": journeys,
            "live_checks": {"sso-gateway-only-ingress": ingress}}
