"""Live Product checks use their own identity before returning to publication."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_product_regression_switches_roles_and_preserves_failure_propagation():
    for filename in ("product-profile-regression.yml", "product-profile-regression-standalone.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        assert workflow["concurrency"]["group"] == "cli-live-eval-dev"
        job = workflow["jobs"]["regression"]
        assert job["environment"] == "adp-gateway-deploy-dev"
        steps = job["steps"]
        fixture_identity = next(i for i, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-checks")
        setup = next(i for i, step in enumerate(steps) if "product_completion_setup" in step.get("run", ""))
        execute = next(i for i, step in enumerate(steps) if any(
            module in step.get("run", "") for module in ("product_completion_attempt", "product_completion_delivery")))
        restore = next(i for i, step in enumerate(steps) if i > execute
                       and step.get("uses") == "./.github/actions/trusted-deployment")
        publish = next(i for i, step in enumerate(steps) if "Retain verified" in step.get("name", ""))
        assert fixture_identity < setup < execute < restore < publish
        assert steps[fixture_identity]["with"]["duration_seconds"] == "10800"
        assert steps[fixture_identity]["with"]["role_arn"] == "${{ vars.ADP_CHECKS_ROLE_ARN }}"
        assert all(not steps[i].get("continue-on-error") and not steps[i].get("if")
                   for i in (fixture_identity, setup, execute, restore, publish))
        assert any("playwright install chromium --with-deps" in step.get("run", "") for step in steps[:setup])
