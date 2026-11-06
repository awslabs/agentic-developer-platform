"""Validate protected catalog records shared by API and served-browser checks."""

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from tests.e2e.product_onboarding.product_v02_record import private_json


def validate_membership_fixture(fixture, binding, source_revision):
    if not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True:
        raise ValueError("Catalog fixture is not run-owned")
    for key in ("run_id", "source_revision", "tenant_id", "actor_id"):
        if not isinstance(fixture.get(key), str) or fixture[key] != binding.get(key):
            raise ValueError("Catalog fixture does not match the protected browser run")
    if fixture["source_revision"] != source_revision:
        raise ValueError("Catalog fixture differs from the selected source")
    if not all(isinstance(fixture.get(key), str) and fixture[key] for key in ("product_id", "project_id", "product_name")):
        raise ValueError("Catalog fixture has no Product identity")
    portfolios = fixture.get("portfolios")
    if not isinstance(portfolios, list) or len(portfolios) != 2:
        raise ValueError("Exactly two owned Portfolios are required")
    for key in ("id", "name", "revision", "relationship_id", "relationship_revision", "valid_from"):
        if any(not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key]
               for row in portfolios):
            raise ValueError("Catalog fixture has an incomplete relationship receipt")
    if any(len({row[key] for row in portfolios}) != 2 for key in ("id", "name", "relationship_id")):
        raise ValueError("Catalog fixture has duplicate Portfolio or relationship identities")
    return fixture


def load_membership_fixture(path, binding, source_revision):
    selected = Path(path)
    if not selected.is_absolute() or selected.is_symlink():
        raise ValueError("Catalog fixture must be a protected absolute file")
    metadata = selected.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Catalog fixture must be an owned private file")
    return validate_membership_fixture(json.loads(selected.read_text()), binding, source_revision)


MEMBERSHIP_SELECTORS = {
    "api": "tests/e2e/product_onboarding/test_v12.py::test_overlapping_membership_matches_receipts_and_preserves_product",
    "browser": "tests/e2e/new_ui/test_product_catalog.py::test_catalog_membership",
}


def pinned_membership_scenario(source_revision, requirements_revision, run_id, clock, selectors=None):
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
               for value in (source_revision, requirements_revision)):
        raise ValueError("Catalog scenario requires pinned source and requirements commits")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("Catalog scenario requires a bounded run ID")
    try:
        selected = datetime.fromisoformat(clock.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("Catalog scenario requires an explicit UTC clock") from error
    if selected.tzinfo != timezone.utc:
        raise ValueError("Catalog scenario requires an explicit UTC clock")
    if selectors is not None and selectors != MEMBERSHIP_SELECTORS:
        raise ValueError("Catalog scenario requires the reviewed API and browser selectors")
    return {"scenario_id": "SCN-01", "run_id": run_id, "source_revision": source_revision,
            "requirements_revision": requirements_revision, "clock": clock,
            "selectors": dict(MEMBERSHIP_SELECTORS), "status": "not_run"}


def begin_membership_fixture(state, plan, product, names=("Commerce", "Shared services")):
    binding = state.binding
    if (plan["run_id"], plan["source_revision"]) != (binding["run_id"], binding["source_revision"]):
        raise ValueError("Catalog intent differs from protected browser run")
    if plan != pinned_membership_scenario(plan["source_revision"], plan["requirements_revision"], plan["run_id"], plan["clock"], plan["selectors"]):
        raise ValueError("Catalog intent differs from reviewed scenario")
    if set(product) != {"product_id", "project_id", "product_name"} or not all(isinstance(product.get(key), str) and product[key]
               for key in ("product_id", "project_id", "product_name")):
        raise ValueError("Catalog intent requires an owned Product receipt")
    if tuple(names) != ("Commerce", "Shared services"):
        raise ValueError("Catalog intent requires the two selected collections")
    intent = {"plan": plan, "product": dict(product), "cleanup_pending": True, "portfolios": []}
    for name in names:
        seed = f"{plan['run_id']}:{name}:SCN-01"
        intent["portfolios"].append({"name": name, "keys": {
            operation: hashlib.sha256(f"{seed}:{operation}".encode()).hexdigest()
            for operation in ("create", "membership", "close", "retire")
        }})
    existing = state.document.get("catalog")
    if existing is not None:
        if existing["plan"] != plan or existing["product"] != product:
            raise ValueError("Catalog recovery differs from the original owned intent")
        return deepcopy(existing)
    state.save({**state.document, "catalog": intent})
    return deepcopy(intent)


def record_membership_receipts(state, name, portfolio, relationship):
    intent = deepcopy(state.document["catalog"])
    if not intent["cleanup_pending"]:
        raise ValueError("Cannot add receipts after catalog cleanup")
    matches = [item for item in intent["portfolios"] if item["name"] == name]
    if len(matches) != 1:
        raise ValueError("Catalog receipt does not belong to the selected collection")
    for receipt in (portfolio, relationship):
        if not isinstance(receipt, dict) or not all(isinstance(receipt.get(key), str) and receipt[key]
                                                    for key in ("id", "revision", "change_id")):
            raise ValueError("Catalog receipt is incomplete")
    candidate = {"portfolio": dict(portfolio), "relationship": dict(relationship)}
    previous = matches[0].get("receipts")
    if previous is not None:
        if previous != candidate:
            raise ValueError("Catalog retry differs from the recorded write receipts")
        return deepcopy(previous)
    if any(item.get("receipts", {}).get("portfolio", {}).get("id") == portfolio["id"]
           or item.get("receipts", {}).get("relationship", {}).get("id") == relationship["id"]
           for item in intent["portfolios"]):
        raise ValueError("Catalog receipts reused another owned record")
    matches[0]["receipts"] = candidate
    state.save({**state.document, "catalog": intent})
    return deepcopy(candidate)


def record_membership_baseline(state, profile):
    intent = deepcopy(state.document["catalog"])
    product = intent["product"]
    expected = {"product_id", "project_id", "name", "purpose", "intended_users"}
    if (not intent["cleanup_pending"] or not isinstance(profile, dict) or set(profile) != expected
            or (profile["product_id"], profile["project_id"], profile["name"]) != (
                product["product_id"], product["project_id"], product["product_name"])
            or not isinstance(profile["purpose"], str)
            or not isinstance(profile["intended_users"], list)
            or any(not isinstance(value, str) for value in profile["intended_users"])):
        raise ValueError("Catalog baseline requires the original owned Product profile")
    previous = intent.get("profile_before")
    if previous is not None:
        if previous != profile:
            raise ValueError("Catalog Product baseline differs from protected intent")
        return deepcopy(previous)
    if any("receipts" in item for item in intent["portfolios"]):
        raise ValueError("Catalog Product baseline must precede relationship writes")
    intent["profile_before"] = deepcopy(profile)
    state.save({**state.document, "catalog": intent})
    return deepcopy(profile)


def publish_membership_fixture(state, path):
    selected = Path(path)
    if (not selected.is_absolute() or selected.is_symlink() or selected.parent.stat().st_uid != os.getuid()
            or stat.S_IMODE(selected.parent.stat().st_mode) != 0o700):
        raise ValueError("Catalog fixture output requires an owned private directory")
    intent = state.document["catalog"]
    if not intent["cleanup_pending"] or any("receipts" not in item for item in intent["portfolios"]):
        raise ValueError("Catalog fixture requires both owned write receipts before browser readback")
    plan = intent["plan"]
    fixture = {key: state.binding[key] for key in ("run_id", "source_revision", "tenant_id", "actor_id")}
    fixture.update(owned_fixture=True, **intent["product"], portfolios=[{
        "id": item["receipts"]["portfolio"]["id"], "revision": item["receipts"]["portfolio"]["revision"],
        "name": item["name"], "relationship_id": item["receipts"]["relationship"]["id"],
        "relationship_revision": item["receipts"]["relationship"]["revision"], "valid_from": plan["clock"],
    } for item in intent["portfolios"]])
    validate_membership_fixture(fixture, state.binding, plan["source_revision"])
    return private_json(selected, fixture)


def record_membership_cleanup(state, observations):
    intent = deepcopy(state.document["catalog"])
    if not intent["cleanup_pending"]:
        if observations != intent.get("cleanup_observations"):
            raise ValueError("Catalog recovery differs from recorded cleanup")
        return deepcopy(observations)
    if any("receipts" not in item for item in intent["portfolios"]):
        raise ValueError("Catalog cleanup requires receipts for both owned records")
    if set(observations) != {item["name"] for item in intent["portfolios"]} or any(
        not isinstance(entry, dict) or not entry.get("closed_revision") or not entry.get("retired_revision")
        or entry.get("current_memberships") != [] or entry.get("portfolio_status") != "retired"
        for entry in observations.values()
    ):
        raise ValueError("Catalog cleanup requires independent absence and retirement receipts for owned records")
    intent["cleanup_pending"] = False
    intent["cleanup_observations"] = deepcopy(observations)
    state.save({**state.document, "catalog": intent})
    return deepcopy(intent["cleanup_observations"])
