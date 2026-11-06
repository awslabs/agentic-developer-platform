"""Pinned V12 history inputs; live actor and provider checks remain separate."""

from copy import deepcopy
from datetime import datetime, timezone
import re

from tests.e2e.new_ui.product_fixture_ports import checked_json


HISTORY_SELECTORS = {
    "history": "tests/e2e/product_onboarding/test_v12.py::test_dated_membership_and_cross_product_contribution_history",
    "access": "tests/e2e/product_onboarding/test_v12_access.py::test_cross_product_initiative_does_not_grant_other_scope_or_disclose_counts",
}
ACTOR_ROLES = {"editor", "authorized", "revoked"}


def utc_instant(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("History requires pinned UTC instants")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("History requires pinned UTC instants") from error
    if instant.tzinfo != timezone.utc:
        raise ValueError("History requires pinned UTC instants")
    return instant


def pinned_history_scenario(source_revision, requirements_revision, run_id, actors, products, timeline, selectors=None):
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
               for value in (source_revision, requirements_revision)):
        raise ValueError("History requires pinned source and requirements commits")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("History requires a bounded run ID")
    if not isinstance(actors, dict) or set(actors) != ACTOR_ROLES or any(
        not isinstance(actor, dict) or set(actor) != {"actor_id", "subject", "tenant_id"}
        or any(not isinstance(value, str) or not value for value in actor.values())
        for actor in actors.values()
    ) or any(len({actor[key] for actor in actors.values()}) != 3 for key in ("actor_id", "subject")):
        raise ValueError("History requires three distinct ordinary actors")
    if len({actor["tenant_id"] for actor in actors.values()}) != 1:
        raise ValueError("History actors must belong to the selected tenant")
    if not isinstance(products, list) or len(products) != 2 or any(
        not isinstance(product, dict) or set(product) != {"product_id", "project_id", "work_id"}
        or any(not isinstance(value, str) or not value for value in product.values())
        for product in products
    ) or any(len({product[key] for product in products}) != 2 for key in ("product_id", "project_id", "work_id")):
        raise ValueError("History requires two distinct Product and native-work scopes")
    ordered = ("valid_from", "switch", "before_correction", "after_correction", "clock")
    if not isinstance(timeline, dict) or set(timeline) != set(ordered):
        raise ValueError("History requires pinned dated links and observation times")
    instants = [utc_instant(timeline[key]) for key in ordered]
    if any(earlier >= later for earlier, later in zip(instants, instants[1:])):
        raise ValueError("History dates must order effective and decision times")
    if selectors is not None and selectors != HISTORY_SELECTORS:
        raise ValueError("History requires the reviewed API and access selectors")
    return {"scenario_id": "SCN-02", "run_id": run_id, "source_revision": source_revision,
            "requirements_revision": requirements_revision, "actors": deepcopy(actors),
            "products": deepcopy(products), "timeline": deepcopy(timeline),
            "selectors": dict(HISTORY_SELECTORS), "status": "not_run"}


def begin_history_fixture(state, plan):
    expected = pinned_history_scenario(
        plan["source_revision"], plan["requirements_revision"], plan["run_id"],
        plan["actors"], plan["products"], plan["timeline"], plan["selectors"],
    )
    binding = state.binding
    if plan != expected or any(binding[key] != value for key, value in (
        ("run_id", plan["run_id"]), ("source_revision", plan["source_revision"]),
        ("tenant_id", plan["actors"]["editor"]["tenant_id"]),
        ("actor_id", plan["actors"]["editor"]["actor_id"]),
    )):
        raise ValueError("History intent differs from protected fixture ownership")
    existing = state.document.get("catalog_history")
    if existing is not None:
        if existing["plan"] != plan:
            raise ValueError("History recovery differs from original owned intent")
        return deepcopy(existing)
    intent = {"plan": deepcopy(plan), "attempts": {}, "cleanup_pending": True}
    state.save({**state.document, "catalog_history": intent})
    return deepcopy(intent)


def record_history_expectations(state, expected):
    intent = deepcopy(state.document["catalog_history"])
    plan = intent["plan"]
    projects = {product["project_id"] for product in plan["products"]}
    contributions = expected.get("contributions") if isinstance(expected, dict) else None
    membership = expected.get("membership") if isinstance(expected, dict) else None
    if (not intent["cleanup_pending"] or not isinstance(expected, dict)
            or set(expected) != {"initiative_id", "contributions", "membership"}
            or not isinstance(expected["initiative_id"], str) or not expected["initiative_id"]
            or not isinstance(contributions, dict) or set(contributions) != projects
            or not isinstance(membership, dict)
            or set(membership) != {"portfolio_id", "relationship_id", "revision", "valid_from"}
            or any(not isinstance(value, str) or not value for value in membership.values())
            or membership["valid_from"] != plan["timeline"]["valid_from"]):
        raise ValueError("History expectations must bind both owned scopes and dated membership")
    for product in plan["products"]:
        item = contributions[product["project_id"]]
        if (not isinstance(item, dict) or set(item) != {"id", "product_id", "before", "after"}
                or not isinstance(item["id"], str) or not item["id"]
                or item["product_id"] != product["product_id"]):
            raise ValueError("History contribution expectations differ from owned Product scopes")
        for time in ("before", "after"):
            value = item[time]
            if (not isinstance(value, dict) or set(value) != {"revision", "objective", "work_ids"}
                    or any(not isinstance(value.get(key), str) or not value[key] for key in ("revision", "objective"))
                    or not isinstance(value.get("work_ids"), list) or not value["work_ids"]
                    or any(not isinstance(work_id, str) or not work_id for work_id in value["work_ids"])
                    or len(value["work_ids"]) != len(set(value["work_ids"]))
                    or (time == "before" and value["work_ids"] != [product["work_id"]])):
                raise ValueError("History needs independently specified dated Contribution receipts")
    previous = intent.get("expected")
    if previous is not None:
        if previous != expected:
            raise ValueError("History expectations cannot change after an attempt")
        return deepcopy(previous)
    if intent["attempts"]:
        raise ValueError("History expectations must precede verification attempts")
    intent["expected"] = deepcopy(expected)
    state.save({**state.document, "catalog_history": intent})
    return deepcopy(expected)


def verify_history_sessions(request, base_url, plan, sessions):
    if not isinstance(sessions, dict) or set(sessions) != ACTOR_ROLES:
        raise ValueError("History requires separately authenticated actors")
    tokens = [session.get("access_token") if isinstance(session, dict) else None for session in sessions.values()]
    if any(not isinstance(token, str) or not token for token in tokens) or len(set(tokens)) != 3:
        raise ValueError("History requires separately authenticated actors")
    observed = {}
    for role in ("editor", "authorized", "revoked"):
        identity = checked_json(request.get(base_url + "/api/auth/me",
                            headers={"Authorization": "Bearer " + sessions[role]["access_token"]}, timeout=30_000))
        expected = plan["actors"][role]
        if (identity.get("account_type") != "human" or identity.get("is_admin") is not False
                or identity.get("org_id") != expected["tenant_id"]
                or identity.get("user_id") not in {expected["actor_id"], expected["subject"]}):
            raise ValueError("Authenticated history actor differs from protected fixture")
        observed[role] = {"actor_id": expected["actor_id"], "tenant_id": expected["tenant_id"],
                          "authenticated_user_id": identity["user_id"]}
    if len({value["authenticated_user_id"] for value in observed.values()}) != 3:
        raise ValueError("History requires separately authenticated actors")
    return observed


def record_history_attempt(state, attempt_id, role, product, observation):
    intent = deepcopy(state.document["catalog_history"])
    if not intent["cleanup_pending"] or role not in ACTOR_ROLES:
        raise ValueError("History attempt is not active or has an unknown actor")
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt_id):
        raise ValueError("History attempt needs a bounded ID")
    if product not in intent["plan"]["products"]:
        raise ValueError("History attempt refers to an unowned Product scope")
    if not isinstance(observation, dict) or set(observation) != {"result", "evidence_ref"} or (
        observation["result"] not in ("pass", "fail", "interrupted")
        or not isinstance(observation["evidence_ref"], str) or not observation["evidence_ref"]
    ):
        raise ValueError("History attempt needs a durable observation")
    entry = {"run_id": intent["plan"]["run_id"], "source_revision": intent["plan"]["source_revision"],
             "requirements_revision": intent["plan"]["requirements_revision"],
             "actor": deepcopy(intent["plan"]["actors"][role]), "product": deepcopy(product),
             "timeline": deepcopy(intent["plan"]["timeline"]), "observation": deepcopy(observation)}
    previous = intent["attempts"].get(attempt_id)
    if previous is not None:
        if previous != entry:
            raise ValueError("History attempt differs from its durable record")
        return deepcopy(previous)
    intent["attempts"][attempt_id] = entry
    state.save({**state.document, "catalog_history": intent})
    return deepcopy(entry)
