"""Authenticate a selected Product release before browser mutations."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from modules.gateway.deployment_evidence.store import EvidenceStore


def verified_frontend_release(*, selection, base_url, sts, ssm, cloudfront, store=None):
    match = re.fullmatch(
        r"arn:aws:iam::([0-9]{12}):role/(adp-[a-z0-9-]+-trusted-checks)",
        selection["checks_role"],
    )
    if not match:
        raise ValueError("Product observation requires the protected checks role")
    account, role_name = match.groups()
    identity = sts.get_caller_identity()
    if identity.get("Account") != account or not re.fullmatch(
        rf"arn:aws:sts::{account}:assumed-role/{re.escape(role_name)}/[^/]+",
        identity.get("Arn", ""),
    ):
        raise ValueError("Product observation is not using its selected checks role")
    source = selection["source_revision"]
    workflow = selection["workflow_revision"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (source, workflow)):
        raise ValueError("Product frontend source and workflow must be pinned commits")
    if selection["repository"] != "aws-e/adp" or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,23}", selection["environment"]
    ):
        raise ValueError("Product release target is not the reviewed repository/environment")
    if selection["region"] != "us-east-1" or any(
        type(selection[key]) is not int or selection[key] <= 0
        for key in ("repository_id", "run_id", "attempt")
    ):
        raise ValueError("Product release selection is incomplete")
    origin = urlsplit(base_url)
    if (
        origin.scheme != "https" or not origin.hostname or origin.netloc != origin.hostname or origin.path not in ("", "/")
        or origin.query or origin.fragment
    ):
        raise ValueError("Product browser target must be an HTTPS origin")

    signed = (store or EvidenceStore()).read(
        account=account, environment=selection["environment"], region=selection["region"],
        repository=selection["repository"], repository_id=selection["repository_id"],
        run_id=selection["run_id"], attempt=selection["attempt"], kind="release",
        name="gateway-frontend", workflow_path=".github/workflows/gateway-frontend-deploy.yml",
        workflow_revision=workflow,
    )
    if signed is None:
        raise ValueError("Selected signed frontend release is missing")
    payload, digest, reference = signed
    release = json.loads(payload)
    expected = {
        "schema_version": 1, "repository_id": selection["repository_id"],
        "run_id": selection["run_id"], "run_attempt": selection["attempt"],
        "workflow_revision": workflow,
        "workflow_path": ".github/workflows/gateway-frontend-deploy.yml",
        "account_id": account, "region": selection["region"], "component": "gateway-frontend",
    }
    if any(type(release.get(key)) is not type(value) or release[key] != value for key, value in expected.items()):
        raise ValueError("Authenticated frontend release differs from the selected deployment")
    from tests.e2e.product_onboarding.product_completion_observe import (
        compatible_source,
    )

    try:
        compatible_source(release.get("source_revision"), source, "gateway-frontend")
    except ValueError as error:
        raise ValueError("Authenticated frontend release differs from the selected deployment: " + str(error)) from error

    distribution_id = ssm.get_parameter(
        Name=f"/adp/{selection['environment']}/gateway/cloudfront-id"
    )["Parameter"]["Value"]
    distribution = cloudfront.get_distribution(Id=distribution_id)["Distribution"]
    if (
        distribution.get("ARN") != f"arn:aws:cloudfront::{account}:distribution/{distribution_id}"
        or distribution.get("Status") != "Deployed"
        or distribution.get("DistributionConfig", {}).get("Enabled") is not True
        or distribution.get("DomainName") != origin.hostname
    ):
        raise ValueError("Product browser target is not the enabled release distribution")
    return {"verified": True, "release": release, "domain": origin.hostname,
            "digest": digest, "reference": reference}


def selection_from_environment(environment):
    required = (
        "ADP_CHECKS_ROLE_ARN", "PRODUCT_EXPECTED_REVISION", "PRODUCT_RELEASE_WORKFLOW_REVISION",
        "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "PRODUCT_RELEASE_RUN_ID", "PRODUCT_RELEASE_ATTEMPT",
        "AWS_REGION", "ENVIRONMENT",
    )
    missing = [name for name in required if not isinstance(environment.get(name), str) or not environment[name].strip()]
    if missing:
        raise ValueError("Product release selection requires non-empty inputs: " + ", ".join(missing))
    numbers = {}
    for name in ("GITHUB_REPOSITORY_ID", "PRODUCT_RELEASE_RUN_ID", "PRODUCT_RELEASE_ATTEMPT"):
        try:
            numbers[name] = int(environment[name])
            if numbers[name] <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Product release selection requires a positive integer: " + name) from None
    return {
        "checks_role": environment["ADP_CHECKS_ROLE_ARN"],
        "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
        "workflow_revision": environment["PRODUCT_RELEASE_WORKFLOW_REVISION"],
        "repository": environment["GITHUB_REPOSITORY"],
        "repository_id": numbers["GITHUB_REPOSITORY_ID"],
        "run_id": numbers["PRODUCT_RELEASE_RUN_ID"],
        "attempt": numbers["PRODUCT_RELEASE_ATTEMPT"],
        "region": environment["AWS_REGION"],
        "environment": environment["ENVIRONMENT"],
    }
