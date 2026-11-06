"""Read a served catalog independently against protected pre-write and write receipts."""

from urllib.parse import quote

from tests.e2e.new_ui.product_fixture_ports import checked_json
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.new_ui.test_product_catalog import matching_rows, membership_rows
from tests.e2e.product_onboarding.product_catalog_fixture import load_membership_fixture, pinned_membership_scenario


def verify_live_membership(provider, base_url, fixture_path, source_revision):
    state = provider.state
    fixture = load_membership_fixture(fixture_path, provider.binding, source_revision)
    intent = state.document["catalog"]
    plan = intent["plan"]
    if (not intent["cleanup_pending"] or plan != pinned_membership_scenario(
        source_revision, plan["requirements_revision"], provider.run_id, plan["clock"], plan["selectors"]
    ) or any(intent["product"][key] != fixture[key] for key in ("product_id", "project_id", "product_name"))
            or len(intent["portfolios"]) != len(fixture["portfolios"])):
        raise ValueError("Live membership differs from protected run-owned intent")
    baseline = intent.get("profile_before")
    if not isinstance(baseline, dict):
        raise ValueError("Live membership needs the Product baseline recorded before relationship writes")
    for owned, item in zip(intent["portfolios"], fixture["portfolios"]):
        receipts = owned.get("receipts")
        if (not isinstance(receipts, dict) or owned["name"] != item["name"]
                or receipts["portfolio"]["id"] != item["id"]
                or receipts["portfolio"]["revision"] != item["revision"]
                or receipts["relationship"]["id"] != item["relationship_id"]
                or receipts["relationship"]["revision"] != item["relationship_revision"]):
            raise ValueError("Live membership differs from original protected write receipts")
    check_served_assets(provider.request, base_url, provider.frontend, source_revision)
    headers = {"Authorization": "Bearer " + provider.owner["access_token"]}
    identity = checked_json(provider.request.get(base_url + "/api/auth/me", headers=headers, timeout=30_000))
    if (identity.get("org_id") != fixture["tenant_id"]
            or identity.get("user_id") not in (fixture["actor_id"], provider.binding.get("owner_subject"))
            or identity.get("account_type") != "human" or identity.get("is_admin") is not False):
        raise ValueError("Live membership actor differs from run-owned Product scope")
    profile = checked_json(provider.request.get(
        base_url + f"/api/v1/products/{quote(fixture['product_id'], safe='')}/projects/{quote(fixture['project_id'], safe='')}",
        headers=headers, timeout=30_000))
    observed_profile = {"product_id": profile["product_id"], "project_id": profile["project_id"],
                        **{key: profile["product"][key] for key in ("name", "purpose", "intended_users")}}
    if observed_profile != baseline:
        raise AssertionError("Served Product first-save fields or native identity changed")
    observed = {}
    for item in fixture["portfolios"]:
        portfolio = checked_json(provider.request.get(
            base_url + f"/api/v1/catalog/portfolios/{quote(item['id'], safe='')}", headers=headers, timeout=30_000))
        if (portfolio["id"], portfolio["revision"], portfolio["data"]["name"]) != (
            item["id"], item["revision"], item["name"]
        ):
            raise AssertionError("Served Portfolio differs from protected write receipt")
        rows = matching_rows(membership_rows(provider.request, base_url, headers, item["id"]), fixture, item)
        if len(rows) != 1 or (rows[0]["relationship_id"], rows[0]["relationship_revision"],
                              rows[0]["valid_from"], rows[0]["product_ref"]["tenant_id"]) != (
            item["relationship_id"], item["relationship_revision"], item["valid_from"], fixture["tenant_id"]
        ):
            raise AssertionError("Served membership differs from owned receipt or has duplicate current links")
        observed[item["name"]] = rows[0]["relationship_id"]
    return {"run_id": fixture["run_id"], "source_revision": source_revision, "memberships": observed,
            "product_unchanged": True}
