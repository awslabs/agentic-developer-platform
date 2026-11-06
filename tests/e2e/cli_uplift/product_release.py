"""Bind the installed CLI checkpoint to its parent Gateway deployment."""

import os
import re

from .ports import PortError
from .preflight import PRODUCT_RELEASE_CASES


def observe(cfg, aws):
    run_id = cfg.get("product_run_id", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if (
        not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
        or run_id
        != f"product-{os.environ.get('GITHUB_RUN_ID')}-{os.environ.get('GITHUB_RUN_ATTEMPT')}"
        or not repository
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{repository}/.github/workflows/gateway-deploy.yml@refs/heads/main"
    ):
        raise PortError("Product release requires its selected parent Gateway run")

    from tests.e2e.product_onboarding import product_inventory, product_native

    inventory = product_inventory.observe(aws, cfg, cfg["expected_revision"])
    native = product_native.observe(cfg, inventory, aws)
    receipt = native["release_artifact"]
    runtime = native["runtime_receipt"]
    # The canonical reader returns only after the authenticated runtime-proof API
    # verifies the flags/schema on every serving pod and checks the tick digest.
    # This is release preflight evidence; it does not execute the D01 scenarios.
    observed = {
        "source_revision": native["source_revision"],
        "image_digest": runtime["image_digest"],
        "migration_head": runtime["migration_head"],
        "api": {
            "authenticated": True,
            "product_profile_schema_version": "1.0",
            "product_profile_enabled": native["product_profile_enabled"],
            "product_onboarding_enabled": native["product_onboarding_enabled"],
        },
    }
    return tuple(sorted(PRODUCT_RELEASE_CASES)), receipt, observed
