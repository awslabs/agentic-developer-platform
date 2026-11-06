"""Reconcile run-owned catalog writes after an unknown response."""

from copy import deepcopy

from tests.e2e.product_onboarding.product_catalog_recovery_scenario import pinned_recovery_scenario


def assert_membership_readback(page, portfolio, product, receipt, valid_from):
    if not isinstance(page, dict) or not isinstance(page.get("items"), list) or page.get("next_cursor"):
        raise AssertionError("Independent catalog readback is missing or unbounded")
    rows = [row for row in page["items"] if row["product_ref"]["record_id"] == product["product_id"]]
    if len(rows) != 1:
        raise AssertionError("Catalog retry created duplicate or missing current membership")
    row = rows[0]
    if (row["relationship_id"], row["relationship_revision"], row["valid_from"],
            row["portfolio_ref"]["record_id"], row["product_ref"]["project_id"]) != (
            receipt["id"], receipt["revision"], valid_from, portfolio["id"], product["project_id"]):
        raise AssertionError("Catalog readback differs from the original write receipt")
    return row


async def recover_membership_write(state, name, portfolio, payload, write, readback, release):
    intent = deepcopy(state.document["catalog"])
    plan = intent["plan"]
    if (not intent["cleanup_pending"] or state.binding["run_id"] != plan["run_id"]
            or state.binding["source_revision"] != plan["source_revision"]):
        raise ValueError("Catalog recovery differs from protected run ownership")
    if (release != state.document.get("catalog_release") or not isinstance(release, dict)
            or release.get("source_revision") != plan["source_revision"] or not release.get("schema_head")):
        raise ValueError("Catalog recovery needs the selected release and schema receipt")
    scenario = state.document.get("catalog_recovery")
    if (not isinstance(scenario, dict) or scenario.get("cleanup_pending") is not True
            or scenario.get("plan") != pinned_recovery_scenario(state)):
        raise ValueError("Catalog recovery needs original pinned scenario ownership")
    product = intent["product"]
    product_ref = payload.get("product_ref", {})
    if (state.binding["tenant_id"] != product_ref.get("tenant_id")
            or product["product_id"] != product_ref.get("record_id")
            or product["project_id"] != product_ref.get("project_id")
            or payload.get("portfolio_ref", {}).get("record_id") != portfolio.get("id")
            or payload.get("valid_from") != plan["clock"]):
        raise ValueError("Catalog recovery payload differs from owned intent")
    matching = [entry for entry in intent["portfolios"] if entry["name"] == name]
    if len(matching) != 1 or "receipts" in matching[0]:
        raise ValueError("Catalog membership recovery needs one unrecorded owned collection")
    if not all(isinstance(portfolio.get(key), str) and portfolio[key] for key in ("id", "revision", "change_id")):
        raise ValueError("Catalog recovery needs the owned Portfolio receipt")
    key = matching[0]["keys"]["membership"]
    expected = {"run_id": plan["run_id"], "source_revision": plan["source_revision"],
                "tenant_id": state.binding["tenant_id"], "actor_id": state.binding["actor_id"],
                "portfolio": deepcopy(portfolio), "payload": deepcopy(payload), "idempotency_key": key,
                "release": {field: value for field, value in release.items() if field != "observation_ref"}}
    pending = matching[0].get("recovery")
    if pending is None:
        pending = {**expected, "attempts": []}
        matching[0]["recovery"] = pending
        state.save({**state.document, "catalog": intent})
    elif {field: pending.get(field) for field in expected} != expected:
        raise ValueError("Catalog recovery differs from the durable write intent")
    if len(pending["attempts"]) >= scenario["plan"]["max_attempts"]:
        raise ValueError("Catalog retry exceeded the bounded attempt count")
    try:
        receipt = await write(key, deepcopy(payload))
    except Exception:
        pending["attempts"].append({"state": "response_unknown", "observation_ref": release["observation_ref"]})
        state.save({**state.document, "catalog": intent})
        raise
    if not isinstance(receipt, dict) or not all(isinstance(receipt.get(field), str) and receipt[field]
                                                 for field in ("id", "revision", "change_id")):
        pending["attempts"].append({"state": "receipt_invalid", "observation_ref": release["observation_ref"]})
        state.save({**state.document, "catalog": intent})
        raise ValueError("Catalog recovery needs the original write receipt")
    if pending.get("receipt") is not None and receipt != pending["receipt"]:
        pending["attempts"].append({"state": "receipt_mismatch", "observation_ref": release["observation_ref"]})
        state.save({**state.document, "catalog": intent})
        raise AssertionError("Catalog retry changed the original relationship receipt")
    try:
        assert_membership_readback(await readback(), portfolio, product, receipt, plan["clock"])
    except Exception:
        pending["attempts"].append({"state": "readback_failed", "observation_ref": release["observation_ref"]})
        state.save({**state.document, "catalog": intent})
        raise
    pending["attempts"].append({"state": "verified", "receipt": deepcopy(receipt),
                                "observation_ref": release["observation_ref"]})
    pending["receipt"] = deepcopy(receipt)
    state.save({**state.document, "catalog": intent})
    return deepcopy(receipt)
