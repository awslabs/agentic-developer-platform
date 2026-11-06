"""Bounded DevOps fault driver with separately observed session and cleanup state."""

import re
import time


DENIALS = {"missing": 401, "invalid": 401, "foreign": 404, "throttled": 429,
           "logged_out": 401, "revoked": 403}
FAULTS = ("worker", "event", "reporting")


def validate_recovery(manifest):
    recovery = manifest.get("recovery", {})
    users = recovery.get("users", [])
    bounds = recovery.get("bounds", {})
    owned = recovery.get("owned_routes", [])
    retained = recovery.get("retained_routes", [])
    if (recovery.get("authorized_faults") is not True
            or recovery.get("exclusive_agent_space") is not True
            or recovery.get("exclusive_worker_pool") is not True
            or not isinstance(users, list) or len(users) != 2
            or any(not isinstance(user, dict) or not user.get("id") or not user.get("credential_ref")
                   for user in users)
            or len({user["id"] for user in users}) != 2
            or not isinstance(recovery.get("scope"), dict)
            or any(not recovery["scope"].get(key) for key in ("tenant_id", "product_id", "project_id"))
            or not recovery.get("binding_id")
            or not isinstance(recovery.get("foreign_scope"), dict)
            or recovery["foreign_scope"].get("product_id") == recovery["scope"]["product_id"]
            or recovery["foreign_scope"].get("project_id") == recovery["scope"]["project_id"]
            or not isinstance(owned, list) or not isinstance(retained, list)
            or not owned or not retained or len(set(owned + retained)) != len(owned + retained)
            or any(not isinstance(route, str) or not re.fullmatch(r"[a-zA-Z0-9_./:-]+", route)
                   for route in owned + retained)
            or not isinstance(bounds, dict) or type(bounds.get("deadline_seconds")) is not int
            or not 30 <= bounds["deadline_seconds"] <= 900
            or type(bounds.get("max_requests")) is not int or not 50 <= bounds["max_requests"] <= 100
            or type(bounds.get("cleanup_grace_seconds")) is not int
            or not 10 <= bounds["cleanup_grace_seconds"] <= 60):
        raise ValueError("recovery needs isolated, bounded two-user faults and exact owned routes")
    receipts = recovery.get("receipts", {})
    if (not isinstance(receipts, dict) or set(receipts) != set(FAULTS)
            or len(set(receipts.values())) != len(FAULTS)
            or any(not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt)
                   for receipt in receipts.values())):
        raise ValueError("recovery needs unique pinned idempotency receipts")
    return recovery


def verify_recovery(manifest, driver, reader, *, clock=time.monotonic):
    """Only driver performs actions; reader must fetch independent target state."""
    recovery = validate_recovery(manifest)
    deadline = clock() + recovery["bounds"]["deadline_seconds"]
    requests = 0

    def act(method, *args):
        nonlocal requests
        requests += 1
        if (requests > recovery["bounds"]["max_requests"]
                or (method != "cleanup_routes" and (clock() >= deadline
                    or requests == recovery["bounds"]["max_requests"]))):
            raise ValueError("recovery exceeded its authorized deadline or request bound")
        return getattr(driver, method)(*args)

    scope = recovery["scope"]
    users = recovery["users"]
    def route_snapshot(*, ownership=False):
        if hasattr(reader, "read_route_snapshot"):
            return reader.read_route_snapshot(scope, ownership=ownership)
        return {"retained": reader.read_routes(scope, recovery["retained_routes"]),
                "owned": reader.read_routes(scope, recovery["owned_routes"]),
                "owners": reader.read_route_owners(scope, recovery["owned_routes"]) if ownership else {},
                "inventory": reader.read_route_inventory(scope)}

    before = route_snapshot(ownership=True)
    retained_before = before["retained"]
    if set(retained_before) != set(recovery["retained_routes"]):
        raise ValueError("retained route baseline is incomplete")
    owned_before = before["owned"]
    owners = before["owners"]
    if (set(owned_before) != set(recovery["owned_routes"])
            or owners != {route: scope for route in recovery["owned_routes"]}):
        raise ValueError("POC route cleanup requires independent exact ownership readback")
    inventory_before = before["inventory"]
    spaces_before = reader.read_spaces(scope)
    if (not isinstance(inventory_before, dict) or not isinstance(spaces_before, dict)
            or any(inventory_before.get(route) != owned_before[route] for route in owned_before)):
        raise ValueError("cleanup requires complete route and retained-space baseline")
    results = {}
    try:
        for user in users:
            response = act("launch", user["credential_ref"], scope, "allowed", user["id"])
            if response.get("status") != 200 or response.get("scope") != scope:
                raise ValueError("two authorized users must obtain scoped native sessions")
        sessions = reader.read_sessions(scope)
        if (not isinstance(sessions, list) or len(sessions) != 2
                or any(not isinstance(session, dict) for session in sessions)
                or {session.get("user_id") for session in sessions} != {user["id"] for user in users}
                or any(session.get("scope") != scope or not session.get("provider_identity")
                       for session in sessions)
                or len({session["provider_identity"] for session in sessions}) != 2):
            raise ValueError("two-user provider session attribution is not isolated")
        def verify_denial(kind):
            attempt_ref = f"{manifest.get('authorization', {}).get('run_id', recovery['binding_id'])}:{kind}"
            if reader.read_provider_calls(attempt_ref) != []:
                raise ValueError("denied attempt already has provider calls")
            credential = (users[0] if kind == "logged_out" else users[1])["credential_ref"]
            response = act("launch", credential,
                           recovery["foreign_scope"] if kind == "foreign" else scope, kind, attempt_ref)
            if (not isinstance(response, dict) or response.get("status") != DENIALS[kind]
                    or set(response) - {"status", "error"}):
                raise ValueError("denied or throttled launch disclosed a session")
            if reader.read_provider_calls(attempt_ref) != []:
                raise ValueError("denied or throttled launch reached the provider")

        for kind in ("missing", "invalid", "foreign", "throttled"):
            verify_denial(kind)
        act("logout", users[0]["credential_ref"])
        act("revoke", users[1]["credential_ref"])
        for kind in ("logged_out", "revoked"):
            verify_denial(kind)
        for fault in FAULTS:
            receipt = recovery["receipts"][fault]
            act("inject_fault", fault, scope, receipt)
            try:
                act("submit", scope, fault, receipt)
            except TimeoutError:
                pass
            act("restart_worker", fault)
            result = reader.read_work(scope, receipt)
            if (not isinstance(result, dict) or result.get("scope") != scope
                    or result.get("receipt") != receipt or result.get("status") != "completed"
                    or not result.get("work_id") or not result.get("native_job_id")
                    or result.get("native_submit_count") != 1):
                raise ValueError("uncertain worker recovery must reconcile one native job")
            results[fault] = result
    finally:
        act("cleanup_routes", scope, recovery["owned_routes"])
        after = route_snapshot()
        if after["owned"] != {}:
            raise ValueError("owned POC routes remain after cleanup")
        if (after["inventory"] != {
                route: value for route, value in inventory_before.items()
                if route not in recovery["owned_routes"]}
                or after["retained"] != retained_before
                or reader.read_spaces(scope) != spaces_before):
            raise ValueError("cleanup changed unrelated routes or retained spaces")
    return {"sessions": sessions, "denials": list(DENIALS), "recovery": results,
            "cleanup": {"owned_absent": True, "retained_unchanged": True}}
