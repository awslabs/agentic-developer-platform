"""Idempotent Dev Box admission seed: one policy row and one approved template.

The operator never writes `dev_box_policies` or `dev_box_template_versions` by
hand (#7998 AC-02). The self-managed stage (modules/user-services/dev-box/
scripts/deploy-dev-box.sh) runs this command inside a gateway pod::

    kubectl exec -n adp-gateway deploy/bedrockgateway -- \\
        env BG_DEV_BOX_DEPLOYMENT_ID=<deployment> \\
        python -m src.cloud_desktop.seed --spec-json '<terraform output policy_spec>' \\
        --allow-ec2-crew-target

with the exact values Terraform applied (foundation output ``policy_spec``). It
upserts the organization's policy for that deployment (enabled, sizes, disk,
limits, rates, lease), upserts the template version (name + revision, pinned to
the deployed image) as approved, and, when asked, adds ``ec2-instance`` to the
organization's ``crew.allowed_targets`` setting, which is what makes the
"Kiro Crew on my EC2 box" option appear (src/crew/proxy.py ``/crew/runtime``,
frontend ``useCrewRuntime``). Repeating the command with the same spec changes
nothing and reports ``changed: false``. The deployment in the spec must match
``BG_DEV_BOX_DEPLOYMENT_ID``, like ``rollback``.

A wrong result that must fail: the stage reporting success while
``dev_box_policies`` is empty — the command exits non-zero unless the policy row
exists and is enabled when it returns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_session_factory
from src.shared.models.base import new_uuid
from src.shared.models.dev_box import DevBoxPolicy, DevBoxTemplate
from src.shared.models.organization import Organization

POLICY_INTEGER_FIELDS = ("max_live_boxes", "max_running_boxes", "max_disk_gb", "max_storage_gb", "max_running_hours", "max_lease_minutes")
POLICY_DECIMAL_FIELDS = ("budget_usd", "instance_hour_rate_usd", "disk_gb_hour_rate_usd", "snapshot_gb_hour_rate_usd")
TEMPLATE_FIELDS = ("name", "revision", "profile", "instance_size", "disk_gb", "image_ref")
EC2_TARGET = "ec2-instance"


class SeedError(ValueError):
    pass


def _positive_int(spec: dict, field: str) -> int:
    value = spec.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value or int(value) <= 0:
        raise SeedError(f"{field} must be a positive integer")
    return int(value)


def _positive_decimal(spec: dict, field: str) -> Decimal:
    try:
        value = Decimal(str(spec.get(field)))
    except (InvalidOperation, TypeError):
        raise SeedError(f"{field} must be a positive decimal") from None
    if not value.is_finite() or value <= 0:
        raise SeedError(f"{field} must be a positive decimal")
    return value


def normalize_spec(spec: dict) -> dict:
    """Validate the Terraform ``policy_spec`` shape before any row is touched."""
    if not isinstance(spec, dict):
        raise SeedError("spec must be a JSON object")
    org_id = spec.get("org_id")
    deployment_id = spec.get("deployment_id")
    if not isinstance(org_id, str) or not org_id.strip():
        raise SeedError("org_id is required (policy_org_id in the dev-box tfvars)")
    if not isinstance(deployment_id, str) or not 0 < len(deployment_id) <= 128:
        raise SeedError("deployment_id is required")
    sizes = spec.get("allowed_sizes")
    if not isinstance(sizes, list) or not sizes or any(not isinstance(item, str) or not item for item in sizes):
        raise SeedError("allowed_sizes must be a non-empty list of instance sizes")
    normalized = {
        "org_id": org_id,
        "deployment_id": deployment_id,
        "enabled": spec.get("enabled", True) is True,
        "allowed_sizes": sorted(set(sizes)),
    }
    for field in POLICY_INTEGER_FIELDS:
        normalized[field] = _positive_int(spec, field)
    for field in POLICY_DECIMAL_FIELDS:
        normalized[field] = _positive_decimal(spec, field)
    idle = spec.get("idle_stop_minutes")
    if idle is not None:
        normalized["idle_stop_minutes"] = _positive_int(spec, "idle_stop_minutes")
    else:
        normalized["idle_stop_minutes"] = None
    template = spec.get("template")
    if not isinstance(template, dict):
        raise SeedError("template is required")
    for field in TEMPLATE_FIELDS:
        if not isinstance(template.get(field), str if field != "disk_gb" else int) or not template.get(field):
            raise SeedError(f"template.{field} is required")
    if template["instance_size"] not in normalized["allowed_sizes"]:
        raise SeedError("template.instance_size is not in allowed_sizes")
    if not 0 < template["disk_gb"] <= normalized["max_disk_gb"]:
        raise SeedError("template.disk_gb exceeds max_disk_gb")
    normalized["template"] = {
        **{field: template[field] for field in TEMPLATE_FIELDS},
        "tools_revision": template.get("tools_revision") or None,
        "approved": template.get("approved", True) is True,
    }
    return normalized


def _assign(row, values: dict) -> bool:
    changed = False
    for field, value in values.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


async def seed(db: AsyncSession, spec: dict, *, allow_ec2_crew_target: bool = False) -> dict:
    """Upsert the policy, the template and (optionally) the org target setting in one transaction."""
    spec = normalize_spec(spec)
    organization = await db.scalar(select(Organization).where(Organization.id == spec["org_id"]).with_for_update())
    if organization is None:
        raise SeedError(f"organization {spec['org_id']!r} does not exist; create the tenant before seeding Dev Box")
    result = {"org_id": spec["org_id"], "deployment_id": spec["deployment_id"], "changed": False}

    policy_fields = ("enabled", "allowed_sizes", *POLICY_INTEGER_FIELDS, *POLICY_DECIMAL_FIELDS, "idle_stop_minutes")
    policy_values = {field: spec[field] for field in policy_fields}
    policy = await db.scalar(
        select(DevBoxPolicy).where(DevBoxPolicy.org_id == spec["org_id"], DevBoxPolicy.deployment_id == spec["deployment_id"]).with_for_update()
    )
    if policy is None:
        policy = DevBoxPolicy(id=new_uuid(), org_id=spec["org_id"], deployment_id=spec["deployment_id"], **policy_values)
        db.add(policy)
        result["policy"] = "created"
        result["changed"] = True
    else:
        result["policy"] = "updated" if _assign(policy, policy_values) else "unchanged"
        result["changed"] = result["changed"] or result["policy"] == "updated"

    template_spec = spec["template"]
    template_values = {field: template_spec[field] for field in ("profile", "instance_size", "disk_gb", "image_ref", "tools_revision", "approved")}
    template = await db.scalar(
        select(DevBoxTemplate)
        .where(DevBoxTemplate.name == template_spec["name"], DevBoxTemplate.revision == template_spec["revision"])
        .with_for_update()
    )
    if template is None:
        template = DevBoxTemplate(id=new_uuid(), name=template_spec["name"], revision=template_spec["revision"], **template_values)
        db.add(template)
        result["template"] = "created"
        result["changed"] = True
    else:
        result["template"] = "updated" if _assign(template, template_values) else "unchanged"
        result["changed"] = result["changed"] or result["template"] == "updated"

    if allow_ec2_crew_target:
        settings = dict(organization.settings or {})
        crew = dict(settings.get("crew") or {})
        targets = list(crew.get("allowed_targets") or ["eks-pod"])
        if EC2_TARGET in targets:
            result["crew_target"] = "unchanged"
        else:
            crew["allowed_targets"] = [*targets, EC2_TARGET]
            settings["crew"] = crew
            organization.settings = settings
            result["crew_target"] = "added"
            result["changed"] = True
    await db.flush()
    result["policy_id"] = policy.id
    result["template_id"] = template.id
    result["policy_enabled"] = policy.enabled
    result["template_approved"] = template.approved
    return result


async def run(spec: dict, *, allow_ec2_crew_target: bool = False, deployment_id_env: str | None = None) -> dict:
    expected = deployment_id_env if deployment_id_env is not None else os.getenv("BG_DEV_BOX_DEPLOYMENT_ID", "")
    if not isinstance(spec, dict) or not spec.get("deployment_id") or spec.get("deployment_id") != expected:
        raise SeedError("spec deployment_id must equal BG_DEV_BOX_DEPLOYMENT_ID for this gateway")
    async with get_session_factory().begin() as db:
        result = await seed(db, spec, allow_ec2_crew_target=allow_ec2_crew_target)
    if not result.get("policy_enabled"):
        raise SeedError("the policy row is not enabled after seeding")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Upsert the Dev Box admission policy and approved template through the gateway")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec-json", help="policy spec as a JSON string (terraform output -json policy_spec)")
    source.add_argument("--spec-file", help="path to the policy spec JSON")
    parser.add_argument("--allow-ec2-crew-target", action="store_true", help="add ec2-instance to the organization's crew.allowed_targets")
    args = parser.parse_args(argv)
    try:
        raw = args.spec_json if args.spec_json is not None else open(args.spec_file).read()
        spec = json.loads(raw)
        if isinstance(spec, dict) and set(spec) == {"value"}:
            spec = spec["value"]
        result = asyncio.run(run(spec, allow_ec2_crew_target=args.allow_ec2_crew_target))
    except (SeedError, OSError, json.JSONDecodeError) as error:
        print(f"dev-box seed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
