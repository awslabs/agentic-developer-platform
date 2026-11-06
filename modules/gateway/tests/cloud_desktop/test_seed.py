"""The stage seeds admission rows through the gateway, idempotently (#7998 AC-02)."""

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.cloud_desktop import seed as seed_module
from src.cloud_desktop.seed import SeedError, normalize_spec, run, seed
from src.shared.models.dev_box import DevBoxPolicy, DevBoxTemplate
from src.shared.models.organization import Organization
from tests.cloud_desktop.test_routes import client, context, create

pytest_plugins = ("tests.cloud_desktop.test_routes",)


def spec(**overrides):
    base = {
        "org_id": "one",
        "deployment_id": "test-deployment",
        "enabled": True,
        "max_live_boxes": 3,
        "max_running_boxes": 2,
        "allowed_sizes": ["m6i.xlarge"],
        "max_disk_gb": 100,
        "max_storage_gb": 600,
        "max_running_hours": 400,
        "budget_usd": "250",
        "max_lease_minutes": 600,
        "idle_stop_minutes": 60,
        "instance_hour_rate_usd": "0.192",
        "disk_gb_hour_rate_usd": "0.00011",
        "snapshot_gb_hour_rate_usd": "0.00007",
        "template": {
            "name": "assistant",
            "revision": "r2",
            "profile": "crew",
            "instance_size": "m6i.xlarge",
            "disk_gb": 100,
            "image_ref": "ami-0123456789abcdef0",
            "tools_revision": "a" * 40,
            "approved": True,
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_seed_creates_then_leaves_rows_unchanged(database):
    async with database.begin() as db:
        organization = await db.get(Organization, "one")
        organization.settings = {}
    async with database.begin() as db:
        first = await seed(db, spec(deployment_id="seed-deployment"), allow_ec2_crew_target=True)
    assert first["policy"] == "created" and first["template"] == "created" and first["crew_target"] == "added"
    assert first["changed"] is True and first["policy_enabled"] is True and first["template_approved"] is True
    async with database.begin() as db:
        second = await seed(db, spec(deployment_id="seed-deployment"), allow_ec2_crew_target=True)
    assert second["changed"] is False
    assert (second["policy"], second["template"], second["crew_target"]) == ("unchanged", "unchanged", "unchanged")
    assert second["policy_id"] == first["policy_id"] and second["template_id"] == first["template_id"]
    async with database() as db:
        policy = await db.get(DevBoxPolicy, first["policy_id"])
        assert policy.allowed_sizes == ["m6i.xlarge"] and policy.budget_usd == Decimal("250") and policy.idle_stop_minutes == 60
        assert await db.scalar(select(func.count(DevBoxPolicy.id)).where(DevBoxPolicy.org_id == "one")) == 2  # p1 fixture + new deployment row
        template = await db.get(DevBoxTemplate, first["template_id"])
        assert (template.image_ref, template.tools_revision, template.approved) == ("ami-0123456789abcdef0", "a" * 40, True)
        settings = await db.scalar(select(Organization.settings).where(Organization.id == "one"))
        assert settings["crew"]["allowed_targets"] == ["eks-pod", "ec2-instance"]


@pytest.mark.asyncio
async def test_seed_updates_the_existing_policy_and_preserves_other_targets(database):
    async with database.begin() as db:
        organization = await db.get(Organization, "one")
        organization.settings = {"crew": {"allowed_targets": ["ec2-instance", "eks-pod"]}, "plan": "team"}
    async with database.begin() as db:
        updated = spec(deployment_id="test-deployment", max_live_boxes=5, template={**spec()["template"], "revision": "r1", "disk_gb": 30})
        result = await seed(db, updated, allow_ec2_crew_target=True)
    assert result["policy"] == "updated" and result["policy_id"] == "p1" and result["template"] == "updated" and result["template_id"] == "t1"
    assert result["crew_target"] == "unchanged"
    async with database() as db:
        assert (await db.get(DevBoxPolicy, "p1")).max_live_boxes == 5
        assert (await db.get(DevBoxTemplate, "t1")).image_ref == "ami-0123456789abcdef0"
        settings = await db.scalar(select(Organization.settings).where(Organization.id == "one"))
        assert settings == {"crew": {"allowed_targets": ["ec2-instance", "eks-pod"]}, "plan": "team"}


@pytest.mark.asyncio
async def test_seeded_policy_admits_a_setup_through_the_real_routes(database):
    async with database.begin() as db:
        await seed(db, spec(template={**spec()["template"], "revision": "r1", "disk_gb": 30}))
    assert create(client(database, context("alice-sub")), key="after-seed").status_code == 202


@pytest.mark.asyncio
async def test_seed_refuses_unknown_org_and_malformed_spec(database):
    async with database.begin() as db:
        with pytest.raises(SeedError, match="does not exist"):
            await seed(db, spec(org_id="nobody"))
    for broken, message in (
        (spec(allowed_sizes=[]), "allowed_sizes"),
        (spec(budget_usd="0"), "budget_usd"),
        (spec(max_lease_minutes=0), "max_lease_minutes"),
        (spec(template={**spec()["template"], "instance_size": "t3.small"}), "not in allowed_sizes"),
        (spec(template={**spec()["template"], "disk_gb": 101}), "exceeds max_disk_gb"),
        (spec(idle_stop_minutes=-1), "idle_stop_minutes"),
    ):
        with pytest.raises(SeedError, match=message):
            normalize_spec(broken)
    async with database() as db:
        assert await db.scalar(select(func.count(DevBoxPolicy.id))) == 2


@pytest.mark.asyncio
async def test_run_requires_the_configured_deployment_and_an_enabled_result(database, monkeypatch):
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "seed-deployment")
    monkeypatch.setattr(seed_module, "get_session_factory", lambda: database)
    with pytest.raises(SeedError, match="BG_DEV_BOX_DEPLOYMENT_ID"):
        await run(spec(deployment_id="test-deployment"))
    result = await run(spec(deployment_id="seed-deployment"))
    assert result["policy"] == "created" and result["policy_enabled"] is True
    with pytest.raises(SeedError, match="not enabled"):
        await run(spec(deployment_id="seed-deployment", enabled=False))


def test_cli_reports_malformed_json_and_accepts_terraform_output_shape(database, monkeypatch, capsys):
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "test-deployment")
    monkeypatch.setattr(seed_module, "get_session_factory", lambda: database)
    assert seed_module.main(["--spec-json", "{not json"]) == 2
    assert "dev-box seed" in capsys.readouterr().err
    assert seed_module.main(["--spec-json", json.dumps({"value": spec()}), "--allow-ec2-crew-target"]) == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["policy_enabled"] is True and printed["crew_target"] in ("added", "unchanged")
