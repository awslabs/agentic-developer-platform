"""Bind V12 recovery to protected release, fixture and attempt ownership."""

import re
from copy import deepcopy
from datetime import datetime, timezone

from tests.e2e.product_onboarding.product_catalog_fixture import pinned_membership_scenario

RECOVERY_SELECTORS = {
    "api": "tests/e2e/product_onboarding/test_v12_recovery.py::test_lost_membership_response_retries_original_key_without_duplicate_or_lost_history",
    "cleanup": "tests/e2e/product_onboarding/test_v12_recovery.py::test_cleanup_only_retires_owned_catalog_links_and_keeps_peer_evidence",
}
MAX_ATTEMPTS = 3


def pinned_recovery_scenario(state, selectors=None):
    catalog = state.document["catalog"]
    membership = catalog["plan"]
    binding = state.binding
    if (membership != pinned_membership_scenario(
        membership["source_revision"], membership["requirements_revision"],
        membership["run_id"], membership["clock"], membership["selectors"],
    ) or any(binding[key] != membership[key] for key in ("run_id", "source_revision"))
            or not all(isinstance(binding.get(key), str) and binding[key] for key in ("tenant_id", "actor_id"))
            or not all(isinstance(catalog["product"].get(key), str) and catalog["product"][key]
                       for key in ("product_id", "project_id"))):
        raise ValueError("Recovery differs from owned membership fixture")
    release = state.document.get("catalog_release")
    if (not isinstance(release, dict) or release.get("source_revision") != membership["source_revision"]
            or not isinstance(release.get("schema_head"), str) or not release["schema_head"]
            or not isinstance(release.get("workflow_revision"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", release["workflow_revision"])
            or type(release.get("run_id")) is not int or release["run_id"] <= 0
            or type(release.get("attempt")) is not int or release["attempt"] <= 0
            or not isinstance(release.get("image_digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", release["image_digest"])
            or not isinstance(release.get("artifact_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", release["artifact_hash"])
            or not isinstance(release.get("release_ref"), str) or not release["release_ref"]
            or not isinstance(release.get("observation_ref"), str) or not release["observation_ref"]):
        raise ValueError("Recovery requires pinned release and observed schema receipts")
    if selectors is not None and selectors != RECOVERY_SELECTORS:
        raise ValueError("Recovery requires reviewed API and cleanup selectors")
    return {"scenario_id": "SCN-03", "status": "not_run", "run_id": membership["run_id"],
            "source_revision": membership["source_revision"],
            "requirements_revision": membership["requirements_revision"], "clock": membership["clock"],
            "release": {key: value for key, value in release.items() if key != "observation_ref"},
            "owner": {"tenant_id": binding["tenant_id"], "actor_id": binding["actor_id"],
                      "product_id": catalog["product"]["product_id"],
                      "project_id": catalog["product"]["project_id"],
                      "portfolios": [entry["name"] for entry in catalog["portfolios"]]},
            "max_attempts": MAX_ATTEMPTS, "selectors": dict(RECOVERY_SELECTORS)}


def begin_recovery_fixture(state, plan):
    if (state.document.get("binding", state.binding) != state.binding
            or not state.document["catalog"]["cleanup_pending"]
            or plan != pinned_recovery_scenario(state, plan["selectors"])):
        raise ValueError("Recovery intent differs from original protected ownership")
    existing = state.document.get("catalog_recovery")
    if existing is not None:
        if existing["plan"] != plan:
            raise ValueError("Recovery cannot change original selected release or ownership")
        return deepcopy(existing)
    intent = {"plan": deepcopy(plan), "cleanup_pending": True}
    state.save({**state.document, "catalog_recovery": intent})
    return deepcopy(intent)


def pin_recovery_cleanup(state, cleanup_at, peer):
    intent = deepcopy(state.document["catalog_recovery"])
    if intent["plan"] != pinned_recovery_scenario(state) or not intent["cleanup_pending"]:
        raise ValueError("Cleanup requires original selected release and run ownership")
    try:
        cleanup_time = datetime.fromisoformat(cleanup_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("Cleanup requires a pinned UTC instant") from error
    if (not isinstance(cleanup_at, str) or not cleanup_at.endswith("Z")
            or cleanup_time.tzinfo != timezone.utc
            or cleanup_time <= datetime.fromisoformat(intent["plan"]["clock"].replace("Z", "+00:00"))):
        raise ValueError("Cleanup must occur after the owned relationship's UTC start")
    owned = state.document["catalog"]["product"]
    if (not isinstance(peer, dict) or set(peer) != {"portfolio_id", "product_id", "project_id", "tenant_id"}
            or any(not isinstance(value, str) or not value for value in peer.values())
            or peer["tenant_id"] != state.binding["tenant_id"]
            or peer["product_id"] == owned["product_id"] or peer["project_id"] == owned["project_id"]
            or any(entry.get("receipts", {}).get("portfolio", {}).get("id") == peer["portfolio_id"]
                   for entry in state.document["catalog"]["portfolios"])):
        raise ValueError("Cleanup requires a distinct same-tenant unowned peer")
    pinned = {"cleanup_at": cleanup_at, "peer": deepcopy(peer)}
    previous = {key: intent[key] for key in pinned if key in intent}
    if previous:
        if previous != pinned:
            raise ValueError("Cleanup cannot change pinned clock or peer on retry")
        return deepcopy(intent)
    intent.update(pinned)
    state.save({**state.document, "catalog_recovery": intent})
    return deepcopy(intent)


def record_recovery_cleanup(state):
    intent = deepcopy(state.document["catalog_recovery"])
    if intent["plan"] != pinned_recovery_scenario(state):
        raise ValueError("Recovery cannot change original selected release or ownership")
    if not intent["cleanup_pending"]:
        return deepcopy(intent)
    if (state.document["catalog"]["cleanup_pending"]
            or not state.document["catalog"].get("cleanup_observations")):
        raise ValueError("Recovery needs independent owned cleanup observations")
    attempts = []
    for entry in state.document["catalog"]["portfolios"]:
        recovery = entry.get("recovery")
        if recovery is not None:
            if (recovery["release"] != intent["plan"]["release"]
                    or not recovery.get("attempts") or recovery["attempts"][-1]["state"] != "verified"):
                raise ValueError("Recovery has an unfinished or differently pinned write")
            attempts.extend(deepcopy(recovery["attempts"]))
    cleanup_attempts = intent.get("cleanup_attempts", {})
    if cleanup_attempts:
        if set(cleanup_attempts) != {entry["name"] for entry in state.document["catalog"]["portfolios"]}:
            raise ValueError("Recovery has incomplete owned cleanup attempts")
        for steps in cleanup_attempts.values():
            for operation in ("close", "retire"):
                step = steps.get(operation)
                if (not isinstance(step, dict) or step.get("release") != intent["plan"]["release"]
                        or not isinstance(step.get("attempts"), list) or not step["attempts"]
                        or step["attempts"][-1]["state"] != "verified"):
                    raise ValueError("Recovery has an unfinished or differently pinned cleanup write")
                attempts.extend(deepcopy(step["attempts"]))
    if not attempts:
        raise ValueError("Recovery has no independently verified write attempt")
    intent["cleanup_pending"] = False
    intent["attempts"] = attempts
    intent["cleanup_observations"] = deepcopy(state.document["catalog"]["cleanup_observations"])
    state.save({**state.document, "catalog_recovery": intent})
    return deepcopy(intent)
