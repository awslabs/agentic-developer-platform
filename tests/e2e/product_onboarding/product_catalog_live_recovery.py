"""Recover owned cleanup writes against the selected serving catalog release."""

from copy import deepcopy
from urllib.parse import quote, urlencode

from tests.e2e.new_ui.product_fixture_ports import checked_json
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.product_onboarding.product_catalog_fixture import record_membership_cleanup
from tests.e2e.product_onboarding.product_catalog_recovery_scenario import (
    pin_recovery_cleanup, pinned_recovery_scenario, record_recovery_cleanup,
)


class _InjectedLostResponse(ConnectionError):
    pass


def run_live_recovery(provider, release_reader, cleanup_at, peer, source_revision):
    state = provider.state
    if not callable(release_reader):
        raise ValueError("Live recovery needs an independent selected release observer")
    plan = pinned_recovery_scenario(state)
    release = state.document["catalog_release"]
    if (plan["source_revision"] != source_revision or plan["run_id"] != provider.run_id
            or state.document["catalog"].get("cleanup_pending") is not True):
        raise ValueError("Live recovery differs from protected run and selected source")
    selected = release_reader(deepcopy(release))
    if (not isinstance(selected, dict) or any(selected.get(key) != release[key] for key in (
        "source_revision", "image_digest", "schema_head", "artifact_hash", "release_ref",
    )) or not isinstance(selected.get("observation_ref"), str) or not selected["observation_ref"]
            or selected["observation_ref"] in (release["release_ref"], release["observation_ref"])):
        raise ValueError("Independently observed catalog release or schema differs from protected receipt")
    check_served_assets(provider.request, provider.base_url, provider.frontend, source_revision)
    if not isinstance(peer, dict):
        raise ValueError("Live recovery needs a protected unowned peer fixture")
    headers = {"Authorization": "Bearer " + provider.owner["access_token"]}
    identity = checked_json(provider.request.get(provider.base_url + "/api/auth/me", headers=headers, timeout=30_000))
    if (identity.get("org_id") != state.binding["tenant_id"]
            or identity.get("user_id") not in (state.binding["actor_id"], state.binding.get("owner_subject"))
            or identity.get("account_type") != "human" or identity.get("is_admin") is not False):
        raise ValueError("Catalog recovery actor differs from protected owner")
    pin_recovery_cleanup(state, cleanup_at, peer)
    peer_path = "/api/v1/catalog/portfolios/" + quote(peer["portfolio_id"], safe="")
    peer_profile = ("/api/v1/products/" + quote(peer["product_id"], safe="") + "/projects/"
                    + quote(peer["project_id"], safe=""))

    def read(path, **params):
        suffix = "?" + urlencode(params) if params else ""
        return checked_json(provider.request.get(provider.base_url + path + suffix, headers=headers, timeout=30_000))

    def peer_snapshot():
        portfolio = read(peer_path)
        profile = read(peer_profile)
        memberships = read(peer_path + "/memberships", effective_at=cleanup_at)
        if (portfolio["id"] != peer["portfolio_id"]
                or (profile["product_id"], profile["project_id"]) != (peer["product_id"], peer["project_id"])
                or not isinstance(memberships.get("items"), list) or memberships.get("next_cursor")):
            raise ValueError("Unowned peer observation differs from pinned fixture")
        return {"portfolio": portfolio, "profile": profile, "memberships": memberships}

    before = peer_snapshot()
    catalog = state.document["catalog"]
    if any("receipts" not in entry for entry in catalog["portfolios"]):
        raise ValueError("Recovery cleanup requires both original owned write receipts")

    def recover_step(name, operation, path, payload, key, revision, receipt_id, verify):
        intent = deepcopy(state.document["catalog_recovery"])
        selected_step = {"release": plan["release"], "key": key, "payload": payload,
                         "revision": revision, "receipt_id": receipt_id}
        steps = intent.setdefault("cleanup_attempts", {}).setdefault(name, {})
        step = steps.setdefault(operation, {**selected_step, "attempts": []})
        if any(step.get(field) != value for field, value in selected_step.items()):
            raise ValueError("Catalog cleanup retry changed the selected release, receipt or idempotency key")
        if not step["attempts"]:
            state.save({**state.document, "catalog_recovery": intent})
        if step["attempts"] and step["attempts"][-1]["state"] == "verified":
            verify(step["receipt"])
            return deepcopy(step["receipt"])
        if len(step["attempts"]) >= plan["max_attempts"]:
            raise ValueError("Catalog cleanup exceeded the bounded retry attempt count")
        for attempt in range(2 if operation == "close" and name == plan["owner"]["portfolios"][0]
                             and not step["attempts"] else 1):
            try:
                response = provider.request.patch(provider.base_url + path, data=payload,
                                                  headers={**headers, "Idempotency-Key": key, "If-Match": revision},
                                                  timeout=30_000)
                receipt = checked_json(response)
                if attempt == 0 and operation == "close" and name == plan["owner"]["portfolios"][0] and not step["attempts"]:
                    raise _InjectedLostResponse("Owned cleanup response deliberately discarded")
            except Exception as error:
                step["attempts"].append({"state": "response_unknown", "observation_ref": selected["observation_ref"]})
                state.save({**state.document, "catalog_recovery": intent})
                if not isinstance(error, _InjectedLostResponse):
                    raise
                continue
            if (not isinstance(receipt, dict) or receipt.get("id") != receipt_id
                    or not isinstance(receipt.get("revision"), str) or receipt["revision"] == revision
                    or not isinstance(receipt.get("change_id"), str) or not receipt["change_id"]
                    or (step.get("receipt") is not None and step["receipt"] != receipt)):
                step["attempts"].append({"state": "receipt_mismatch", "observation_ref": selected["observation_ref"]})
                state.save({**state.document, "catalog_recovery": intent})
                raise AssertionError("Catalog cleanup retry changed the original owned receipt")
            try:
                verify(receipt)
            except Exception:
                step["attempts"].append({"state": "readback_failed", "observation_ref": selected["observation_ref"]})
                state.save({**state.document, "catalog_recovery": intent})
                raise
            step["receipt"] = deepcopy(receipt)
            step["attempts"].append({"state": "verified", "receipt": deepcopy(receipt),
                                     "observation_ref": selected["observation_ref"]})
            state.save({**state.document, "catalog_recovery": intent})
            return deepcopy(receipt)
        raise RuntimeError("Catalog cleanup has no verified response after bounded retry")

    observations = {}
    for entry in catalog["portfolios"]:
        portfolio = entry["receipts"]["portfolio"]
        relationship = entry["receipts"]["relationship"]
        portfolio_path = "/api/v1/catalog/portfolios/" + quote(portfolio["id"], safe="")
        membership_path = portfolio_path + "/memberships"

        def verify_close(receipt):
            current = read(membership_path, effective_at=cleanup_at)
            historical = read(membership_path, effective_at=catalog["plan"]["clock"])
            items = historical.get("items", [])
            if (current.get("items") != [] or current.get("next_cursor") or len(items) != 1
                    or historical.get("next_cursor") or items[0].get("relationship_id") != relationship["id"]
                    or items[0].get("relationship_revision") != receipt["revision"]
                    or items[0].get("valid_from") != catalog["plan"]["clock"]
                    or items[0].get("valid_to") != cleanup_at
                    or items[0].get("product_ref", {}).get("record_id") != plan["owner"]["product_id"]
                    or items[0].get("product_ref", {}).get("project_id") != plan["owner"]["project_id"]
                    or items[0].get("product_ref", {}).get("tenant_id") != state.binding["tenant_id"]):
                raise AssertionError("Independent catalog history differs from owned close receipt")

        def verify_retire(receipt):
            observed = read(portfolio_path)
            if (observed.get("id") != portfolio["id"] or observed.get("revision") != receipt["revision"]
                    or observed.get("data", {}).get("lifecycle_status") != "retired"):
                raise AssertionError("Independent owned Portfolio retirement differs from receipt")

        close = recover_step(entry["name"], "close", portfolio_path + "/memberships/" + quote(relationship["id"], safe=""),
                             {"valid_to": cleanup_at}, entry["keys"]["close"], relationship["revision"],
                             relationship["id"], verify_close)
        retire = recover_step(entry["name"], "retire", portfolio_path,
                              {"name": entry["name"], "lifecycle_status": "retired",
                               "owner_ref": {"kind": "user", "id": state.binding["actor_id"]}},
                              entry["keys"]["retire"], portfolio["revision"], portfolio["id"], verify_retire)
        observations[entry["name"]] = {"closed_revision": close["revision"],
                                       "retired_revision": retire["revision"], "current_memberships": [],
                                       "portfolio_status": "retired"}
    if peer_snapshot() != before:
        raise AssertionError("Catalog recovery changed an unowned peer")
    record_membership_cleanup(state, observations)
    archived = record_recovery_cleanup(state)
    if (archived["cleanup_pending"] or state.document["catalog_release"] != release
            or len(archived["attempts"]) < 5):
        raise AssertionError("Catalog cleanup lost receipt, release or interrupted attempt evidence")
    return {"run_id": provider.run_id, "source_revision": source_revision,
            "cleanup_verified": True, "peer_unchanged": True, "attempts": len(archived["attempts"])}
