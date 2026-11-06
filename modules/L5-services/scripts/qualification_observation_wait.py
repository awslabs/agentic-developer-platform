"""Wait for exact same-run evidence chosen from runtime facts, never fixture IDs."""

import os
import time

import qualification_human_observations as human
import qualification_request_effects as effects
from release_context import release_identity

CONTRACT = "l5-observation-discovery/v1"
PATH = "/orchestration/operations-release-job/l5-observations/discover"


def collect_by_scope(journal, selections, *, environment=None, opener=None, timeout_seconds=600,
                     poll_seconds=5, clock=time.monotonic, sleep=time.sleep):
    environment = os.environ if environment is None else environment
    identity = release_identity(environment)
    if journal.binding["context"] != identity:
        raise ValueError("Observation discovery and journal release identities differ")
    if (not isinstance(selections, list) or not 1 <= len(selections) <= 32
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1800
            or type(poll_seconds) is not int or not 1 <= poll_seconds <= 30):
        raise ValueError("Use a bounded same-run observation wait")
    collected = []
    deadline = clock() + timeout_seconds
    for selection in selections:
        required = {"selection", "preparer_id", "observer_id", "evidence"}
        if (not isinstance(selection, dict) or selection.get("evidence") not in {"human", "effect"}
                or set(selection) != required | ({"operation", "request_sha256"} if selection.get("evidence") == "effect" else set())
                or not isinstance(selection["selection"], dict) or set(selection["selection"]) != human.SELECTION_FIELDS
                or selection["selection"].get("service_id") != identity["service"]
                or any(not isinstance(selection[key], str) or not selection[key] for key in ("preparer_id", "observer_id"))):
            raise ValueError("Select stable owners plus exact runtime scope, without future challenge IDs or fixture verdicts")
        journal.append("observation-awaiting-human", {"selection": selection, "release_check_completed": False, "native_qualified": False})
        while True:
            result = human.request_export(environment, identity, selection, path=PATH, opener=opener)
            if (not isinstance(result, dict) or set(result) != {
                        "contract", "status", "selector", "pending_challenge_ids", "release_check_completed", "native_qualified", "next_action",
                    } or result["contract"] != CONTRACT or result["release_check_completed"] is not False
                    or result["native_qualified"] is not False or result["status"] not in {"ready", "pending"}
                    or not isinstance(result["pending_challenge_ids"], list) or len(result["pending_challenge_ids"]) > 128
                    or any(not isinstance(value, str) or not human.UUID.fullmatch(value) for value in result["pending_challenge_ids"])):
                raise ValueError("Gateway observation discovery response is invalid")
            selector = result["selector"]
            if result["status"] == "ready":
                if (not isinstance(selector, dict) or selector.get("selection") != selection["selection"]
                        or (selection["evidence"] == "effect" and any(selector.get(key) != selection[key]
                            for key in ("operation", "request_sha256")))):
                    raise ValueError("Discovered evidence differs from the selected runtime scope")
                reader = human if selection["evidence"] == "human" else effects
                values = reader.collect_into(journal, [selector], environment=environment, opener=opener)
                if selection["evidence"] == "human" and values[0].facts["observation"]["actor_id"] != selection["observer_id"]:
                    raise ValueError("Exported human differs from the intended observer")
                collected.extend(values)
                break
            if selector is not None:
                raise ValueError("Pending discovery cannot supply a collected selector")
            if clock() >= deadline:
                journal.append("observation-wait-expired", {"selection": selection, "pending_challenge_ids": result["pending_challenge_ids"],
                                                           "release_check_completed": False, "native_qualified": False})
                raise TimeoutError("Selected same-run human observation is still pending; complete the bounded human task, without repeating native work")
            sleep(min(poll_seconds, max(0, deadline - clock())))
    return tuple(collected)


def wait_for_preparation(journal, selection, *, environment=None, opener=None, timeout_seconds=600,
                         poll_seconds=5, clock=time.monotonic, sleep=time.sleep):
    """Return a unique owner-prepared effect challenge before the canonical call.

    This does not prepare on the human's behalf, operate a UI, or issue the
    effect. A completed/uncertain prior effect refuses further execution.
    """
    environment = os.environ if environment is None else environment
    identity = release_identity(environment)
    if (journal.binding["context"] != identity or not isinstance(selection, dict)
            or set(selection) != {"selection", "preparer_id", "observer_id", "evidence", "operation", "request_sha256"}
            or selection["evidence"] != "effect" or selection["operation"] not in {"save", "check", "replay"}
            or not isinstance(selection["selection"], dict) or set(selection["selection"]) != human.SELECTION_FIELDS
            or selection["selection"].get("service_id") != identity["service"]
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1800
            or type(poll_seconds) is not int or not 1 <= poll_seconds <= 30):
        raise ValueError("Use the exact selected effect and a bounded preparation wait")
    deadline = clock() + timeout_seconds
    journal.append("effect-awaiting-preparation", {"selection": selection, "release_check_completed": False, "native_qualified": False})
    while True:
        result = human.request_export(environment, identity, selection, path=PATH, opener=opener)
        if (not isinstance(result, dict) or result.get("contract") != CONTRACT
                or result.get("release_check_completed") is not False or result.get("native_qualified") is not False
                or result.get("status") not in {"pending", "ready"}):
            raise ValueError("Gateway effect preparation discovery is invalid")
        if result["status"] == "ready":
            raise ValueError("This exact effect was already attempted; collect its receipt instead of repeating work")
        candidates = result.get("pending_challenge_ids")
        if (result.get("selector") is not None or not isinstance(candidates, list)
                or any(not isinstance(value, str) or not human.UUID.fullmatch(value) for value in candidates)):
            raise ValueError("Gateway preparation selection is invalid")
        if len(candidates) > 1:
            raise ValueError("Prepared effect challenge is ambiguous; the owner must resolve the duplicate scope")
        if candidates:
            journal.append("effect-preparation-selected", {"selection": selection, "challenge_id": candidates[0],
                                                           "release_check_completed": False, "native_qualified": False})
            return candidates[0]
        if clock() >= deadline:
            raise TimeoutError("Owner preparation is still pending; no effect was attempted")
        sleep(min(poll_seconds, max(0, deadline - clock())))
