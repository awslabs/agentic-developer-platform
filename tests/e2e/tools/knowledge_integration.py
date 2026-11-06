"""Local knowledge discovery and sentinel contract; live fixtures are supplied privately."""

from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable

KNOWLEDGE_OPERATIONS = ("search", "understand", "impact", "browse", "remember", "experience", "secure")
READ_ACTIONS = {"browse": ("ls", "tree", "read", "info"), "secure": ("identify", "plan", "verify")}
GATED_ACTIONS = {"remember": (None,), "experience": ("save", "recall", "list_syntheses")}
BACKEND_PREREQUISITES = {
    "search": "code index", "understand": "structural index", "impact": "structural index",
    "browse": "indexed content", "secure": "vulnerability index",
}


def knowledge_ac01(driver, observer, operation="search"):
    if (driver is None or observer is None or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True):
        raise FixtureUnavailable("two isolated indexed knowledge fixtures and independent index observer required")
    if operation not in KNOWLEDGE_OPERATIONS:
        raise ValueError("unknown knowledge operation")
    attempt_id = str(uuid4())
    evidence = {"status": "blocked", "attempt_id": attempt_id,
                "cleanup": {"status": "not-run"}}
    try:
        fixtures = [observer.expected(fixture_id=fixture_id, attempt_id=attempt_id) for fixture_id in ("owner-a", "owner-b")]
        if any(not isinstance(fixture, dict) or fixture.get("source") != "independent-knowledge-index" or
               fixture.get("indexed") is not True or
               any(not isinstance(fixture.get(field), str) or not fixture[field] for field in
                   ("owner", "tenant", "fact", "citation", "source_ref")) for fixture in fixtures):
            raise ValueError("independent indexed fact and citation unavailable")
        if fixtures[0]["owner"] == fixtures[1]["owner"] or fixtures[0]["tenant"] == fixtures[1]["tenant"] or fixtures[0]["fact"] == fixtures[1]["fact"]:
            raise ValueError("knowledge fixtures are not isolated")
        tools = driver.discover(attempt_id=attempt_id)
        advertised = [tool for tool in tools if isinstance(tool, dict) and str(tool.get("name", "")).startswith("knowledge___")] if isinstance(tools, list) else []
        if (len(advertised) != len(KNOWLEDGE_OPERATIONS) or
                {tool["name"] for tool in advertised} != {"knowledge___" + name for name in KNOWLEDGE_OPERATIONS} or
                any(not isinstance(tool.get("inputSchema"), dict) or tool["inputSchema"].get("type") != "object" for tool in advertised)):
            raise ValueError("released knowledge inventory not discovered")
        if operation in BACKEND_PREREQUISITES and observer.backend_ready(operation=operation, attempt_id=attempt_id) is not True:
            evidence["reason"] = BACKEND_PREREQUISITES[operation] + " unavailable"
        else:
            actions = READ_ACTIONS.get(operation, GATED_ACTIONS.get(operation, (None,)))
            for fixture_id, expected, foreign in zip(("owner-a", "owner-b"), fixtures, reversed(fixtures)):
                for action in actions:
                    before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
                    response = (driver.query(fixture_id=fixture_id, query=expected["fact"], attempt_id=attempt_id)
                                if operation == "search" else driver.invoke(
                                    fixture_id=fixture_id, operation=operation, action=action, attempt_id=attempt_id))
                    after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
                    if operation in GATED_ACTIONS:
                        if (response != {"isError": True, "code": "knowledge_admission_unavailable"} or
                                type(before) is not int or type(after) is not int or after != before):
                            raise ValueError("unadmitted knowledge action reached a backend")
                    elif (not isinstance(response, dict) or response.get("fact") != expected["fact"] or
                          response.get("citation") != expected["citation"] or
                          response.get("source_ref") != expected["source_ref"] or
                          foreign["fact"] in str(response) or
                          type(before) is not int or type(after) is not int or after != before + 1):
                        raise ValueError("knowledge sentinel, citation or provider-call assertion failed")
            evidence["status"] = "pending-admission" if operation in GATED_ACTIONS else "pending-cleanup"
            if operation in GATED_ACTIONS:
                evidence["reason"] = "knowledge_admission_unavailable"
            evidence["observations"] = {"source": "independent-knowledge-index", "fixtures": 2,
                                        "operations": ["knowledge___" + operation],
                                        "actions": [action for action in actions if action is not None]}
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
        evidence["status"] = "fail"
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    elif evidence["status"] == "pending-admission":
        evidence["status"] = "blocked"
        if evidence["cleanup"]["status"] != "confirmed":
            evidence["reason"] = "knowledge fixture cleanup unconfirmed"
    elif evidence["status"] == "blocked" and evidence["cleanup"]["status"] != "confirmed":
        evidence["reason"] = "knowledge fixture cleanup unconfirmed"
    return evidence


DENIAL_CONTROLS = ("foreign_project", "foreign_tenant", "forged_assertion",
                   "expired_assertion", "replayed_assertion", "wrong_request", "unauthenticated_direct")


def knowledge_ac02(driver, observer):
    if (driver is None or observer is None or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True):
        raise FixtureUnavailable("isolated knowledge owners and independent denial observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "blocked", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        fixtures = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                    for fixture_id in ("owner-a", "owner-b")}
        if (any(not isinstance(item, dict) or item.get("source") != "independent-knowledge-index" or
                item.get("indexed") is not True or
                any(not isinstance(item.get(field), str) or not item[field] for field in
                    ("owner", "tenant", "fact", "citation", "source_ref"))
                for item in fixtures.values()) or
                fixtures["owner-a"]["owner"] == fixtures["owner-b"]["owner"] or
                fixtures["owner-a"]["tenant"] == fixtures["owner-b"]["tenant"]):
            raise ValueError("two independent isolated knowledge fixtures required")
        for fixture_id in fixtures:
            other_id = "owner-b" if fixture_id == "owner-a" else "owner-a"
            expected, foreign = fixtures[fixture_id], fixtures[other_id]
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            answer = driver.query(fixture_id=fixture_id, query=expected["fact"], attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            if (answer != {"fact": expected["fact"], "citation": expected["citation"],
                           "source_ref": expected["source_ref"]} or
                    foreign["fact"] in str(answer) or
                    type(before) is not int or type(after) is not int or after != before + 1):
                raise ValueError("permitted knowledge control failed")
            for fault in DENIAL_CONTROLS:
                before = {owner: observer.provider_calls(fixture_id=owner, attempt_id=attempt_id)
                          for owner in fixtures}
                result = driver.probe(fixture_id=fixture_id, foreign_fixture_id=other_id,
                                      fault=fault, attempt_id=attempt_id)
                after = {owner: observer.provider_calls(fixture_id=owner, attempt_id=attempt_id)
                         for owner in fixtures}
                expected_status = 401 if fault == "unauthenticated_direct" else 403
                if (result != {"status": expected_status, "content": []} or
                        any(type(count) is not int for count in (*before.values(), *after.values())) or
                        after != before):
                    raise ValueError("private knowledge denial or provider isolation failed")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-knowledge-index", "fixtures": 2,
                                    "controls": list(DENIAL_CONTROLS)}
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["status"] = "fail"
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def knowledge_ac03(driver, observer):
    if (driver is None or observer is None or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True):
        raise FixtureUnavailable("private knowledge fault fixture and independent core observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "blocked", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        expected = observer.expected_core(attempt_id=attempt_id)
        if (not isinstance(expected, dict) or expected.get("source") != "independent-engine-fixture" or
                any(not isinstance(expected.get(field), str) or not expected[field]
                    for field in ("name", "fact", "citation"))):
            raise ValueError("independent engine oracle unavailable")
        knowledge_before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        core_before = observer.core_calls(attempt_id=attempt_id)
        fault = driver.unavailable(attempt_id=attempt_id)
        if (not isinstance(fault, dict) or
                fault.get("connector_status") != "unavailable" or
                fault.get("query") != {"status": 503, "content": []} or
                fault.get("tools") != [expected["name"]] or
                not expected["name"].startswith("websearch___")):
            raise ValueError("knowledge fault hid engine or returned empty success")
        answer = driver.core_query(query=expected["fact"], attempt_id=attempt_id)
        knowledge_after = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        core_after = observer.core_calls(attempt_id=attempt_id)
        if (answer != {"fact": expected["fact"], "citation": expected["citation"]} or
                any(type(count) is not int for count in
                    (knowledge_before, knowledge_after, core_before, core_after)) or
                knowledge_after != knowledge_before or core_after != core_before + 1):
            raise ValueError("engine regression or private backend dispatched")
        pending = {fixture_id: observer.pending_expected(fixture_id=fixture_id, attempt_id=attempt_id)
                   for fixture_id in ("owner-a", "owner-b")}
        if (any(not isinstance(fixture, dict) or fixture.get("source") != "independent-knowledge-index" or
                fixture.get("indexed") is not False or fixture.get("stage") != "pending" or
                any(not isinstance(fixture.get(field), str) or not fixture[field]
                    for field in ("owner", "tenant", "fact"))
                for fixture in pending.values()) or
                pending["owner-a"]["owner"] == pending["owner-b"]["owner"] or
                pending["owner-a"]["tenant"] == pending["owner-b"]["tenant"] or
                pending["owner-a"]["fact"] == pending["owner-b"]["fact"]):
            raise ValueError("two independent pending index fixtures required")
        for fixture_id, fixture in pending.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            response = driver.pending(fixture_id=fixture_id, query=fixture["fact"], attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            if (response != {"isError": True, "code": "knowledge_index_not_ready"} or
                    type(before) is not int or type(after) is not int or before != after):
                raise ValueError("pending index returned empty success or reached provider")
        evidence["status"] = "pending-cleanup"
        indexed = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                   for fixture_id in ("owner-a", "owner-b")}
        if (any(not isinstance(fixture, dict) or fixture.get("source") != "independent-knowledge-index" or
                fixture.get("indexed") is not True or
                any(not isinstance(fixture.get(field), str) or not fixture[field]
                    for field in ("owner", "tenant", "fact", "citation"))
                for fixture in indexed.values()) or
                indexed["owner-a"]["owner"] == indexed["owner-b"]["owner"] or
                indexed["owner-a"]["tenant"] == indexed["owner-b"]["tenant"] or
                indexed["owner-a"]["fact"] == indexed["owner-b"]["fact"]):
            raise ValueError("two independent indexed backend failure fixtures required")
        for fixture_id, fixture in indexed.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            response = driver.backend_failure(fixture_id=fixture_id, query=fixture["fact"], attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            if (response != {"isError": True, "code": "knowledge_backend_unavailable"} or
                    type(before) is not int or type(after) is not int or after != before + 1):
                raise ValueError("failed knowledge backend returned empty success or was not dispatched")
        before_target = observer.target_state(attempt_id=attempt_id)
        if before_target != {"source": "independent-gateway-control", "knowledge": True, "engine": True}:
            raise ValueError("target removal precondition unavailable")
        provider_before = {fixture_id: observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
                           for fixture_id in indexed}
        core_before_removal = observer.core_calls(attempt_id=attempt_id)
        removed = driver.removed(attempt_id=attempt_id)
        after_target = observer.target_state(attempt_id=attempt_id)
        core_result = driver.core_query(query=expected["fact"], attempt_id=attempt_id)
        provider_after = {fixture_id: observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
                          for fixture_id in indexed}
        core_after_removal = observer.core_calls(attempt_id=attempt_id)
        if (after_target != {"source": "independent-gateway-control", "knowledge": False, "engine": True} or
                removed != {"tools": [expected["name"]], "query": {"status": 403, "code": "tool_undeployed"}} or
                core_result != {"fact": expected["fact"], "citation": expected["citation"]} or
                any(type(count) is not int for count in (*provider_before.values(), *provider_after.values(),
                                                       core_before_removal, core_after_removal)) or
                provider_before != provider_after or core_after_removal != core_before_removal + 1):
            raise ValueError("removed target remained callable or engine regressed")
        evidence["observations"] = {"source": "independent-engine-fixture", "core_operation": expected["name"],
                                    "fault": "knowledge_backend_unavailable", "pending_fixtures": 2,
                                    "indexing": "knowledge_index_not_ready", "failed_fixtures": 2,
                                    "backend": "knowledge_backend_unavailable", "target_removed": True,
                                    "engine_calls": 2}
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["status"] = "fail"
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def knowledge_ac04(driver, observer, client_name, operation):
    if (driver is None or observer is None or getattr(driver, "client_name", None) != client_name or
            getattr(driver, "isolated", False) is not True or getattr(driver, "authorized", False) is not True or
            client_name not in ("cli", "claude-code", "codex", "engine-worker") or
            operation not in (*KNOWLEDGE_OPERATIONS, "acl") or
            (client_name == "engine-worker") != (operation == "acl") or
            any(not callable(getattr(observer, name, None)) for name in
                ("pricing", "expected", "provider_calls", "cleanup_status", "client_version", "session_status"))):
        raise FixtureUnavailable("isolated client/worker fixture and independent observer required")
    attempt_id = str(uuid4())
    price = observer.pricing(attempt_id=attempt_id)
    if (not isinstance(price, dict) or price.get("source") != "independent-test-pricing" or
            type(price.get("max_sessions")) is not int or price["max_sessions"] != 1 or
            type(price.get("ttl_seconds")) is not int or price["ttl_seconds"] != 300 or
            type(price.get("cleanup_seconds")) is not int or price["cleanup_seconds"] != 600):
        raise FixtureUnavailable("bounded independent price and session authority required")
    try:
        if (type(price.get("per_call_usd")) is not str or type(price.get("max_cell_usd")) is not str):
            raise FixtureUnavailable("independent per-operation pricing unavailable")
        per_call = Decimal(price["per_call_usd"])
        max_cell = Decimal(price["max_cell_usd"])
    except (InvalidOperation, TypeError, KeyError):
        raise FixtureUnavailable("independent per-operation pricing unavailable") from None
    if (not per_call.is_finite() or not max_cell.is_finite() or per_call <= 0 or max_cell <= 0 or
            22 * max_cell > Decimal(20)):
        raise FixtureUnavailable("run-wide $20 cost ceiling cannot be reserved")
    evidence = {"status": "blocked", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    start = monotonic()
    try:
        expected = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                    for fixture_id in ("owner-a", "owner-b")}
        if (any(not isinstance(fixture, dict) or fixture.get("source") != "independent-knowledge-index" or
                fixture.get("indexed") is not True or any(not isinstance(fixture.get(field), str) or not fixture[field]
                for field in ("owner", "tenant", "fact", "citation", "source_ref"))
                for fixture in expected.values()) or
                expected["owner-a"]["owner"] == expected["owner-b"]["owner"] or
                expected["owner-a"]["tenant"] == expected["owner-b"]["tenant"] or
                expected["owner-a"]["fact"] == expected["owner-b"]["fact"]):
            raise ValueError("two independently indexed isolated owners required")
        client = observer.client_version(client_name=client_name, attempt_id=attempt_id)
        if (not isinstance(client, dict) or client.get("source") != "independent-client-install" or
                client.get("name") != client_name or not isinstance(client.get("version"), str) or
                not client["version"] or client["version"] != getattr(driver, "version", None)):
            raise ValueError("client is not bound to its independent version oracle")
        evidence["client_version"] = client["version"]
        tools = driver.discover(attempt_id=attempt_id)
        discovered = [tool for tool in tools if isinstance(tool, dict) and
                      str(tool.get("name", "")).startswith("knowledge___")] if isinstance(tools, list) else []
        if (not isinstance(tools, list) or
                {tool.get("name") for tool in discovered} !=
                {"knowledge___" + name for name in KNOWLEDGE_OPERATIONS} or
                len(discovered) != len(KNOWLEDGE_OPERATIONS) or
                any(not isinstance(tool.get("inputSchema"), dict) for tool in discovered)):
            raise ValueError("released operation inventory differs from native discovery")
        actions = (None,) if operation == "acl" else READ_ACTIONS.get(operation, GATED_ACTIONS.get(operation, (None,)))
        expected_calls = 1 if operation == "acl" else 0 if operation in GATED_ACTIONS else 2 * len(actions)
        if expected_calls * per_call > max_cell:
            raise FixtureUnavailable("operation actions exceed priced cell ceiling")
        for fixture_id, fixture in expected.items():
            foreign = expected["owner-b" if fixture_id == "owner-a" else "owner-a"]
            for action in (actions if operation != "acl" or fixture_id == "owner-a" else ()):
                if monotonic() - start >= 300:
                    raise TimeoutError("knowledge fixture exceeded session TTL")
                before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
                answer = (driver.worker(fixture_id=fixture_id, attempt_id=attempt_id) if operation == "acl"
                          else driver.query(fixture_id=fixture_id, query=fixture["fact"], attempt_id=attempt_id)
                          if operation == "search" else driver.invoke(
                              fixture_id=fixture_id, operation=operation, action=action, attempt_id=attempt_id))
                after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
                if operation in GATED_ACTIONS:
                    if answer != {"isError": True, "code": "knowledge_admission_unavailable"} or after != before:
                        raise ValueError("unadmitted write attempted provider work")
                elif (answer != {key: fixture[key] for key in ("fact", "citation", "source_ref")} or
                      foreign["fact"] in str(answer) or type(before) is not int or type(after) is not int or
                      after != before + 1):
                    raise ValueError("scoped knowledge result or independent citation differs")
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            denied = driver.foreign(fixture_id=fixture_id, foreign_fixture_id=("owner-b" if fixture_id == "owner-a" else "owner-a"),
                                    attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            if denied != {"status": 403, "content": []} or type(before) is not int or after != before:
                raise ValueError("foreign fixture reached knowledge provider")
        if monotonic() - start > 300:
            raise TimeoutError("knowledge fixture exceeded session TTL")
        evidence["status"] = "pending-admission" if operation in GATED_ACTIONS else "pending-cleanup"
        sessions = observer.session_status(attempt_id=attempt_id)
        if (not isinstance(sessions, dict) or sessions.get("source") != "independent-session-observer" or
                type(sessions.get("count")) is not int or not 0 <= sessions["count"] <= 1 or
                type(sessions.get("ttl_seconds")) is not int or
                not 0 < sessions["ttl_seconds"] <= 300):
            raise ValueError("run-owned session bound unconfirmed")
        evidence["sessions"] = sessions["count"]
        if operation in GATED_ACTIONS:
            evidence["reason"] = "knowledge_admission_unavailable"
        evidence["observations"] = {"source": "independent-knowledge-index", "fixtures": 2,
                                    "actions": [action for action in actions if action is not None],
                                    "provider_calls": expected_calls, "reserved_usd": str(max_cell),
                                    "pricing_source": price["source"]}
    except (FixtureUnavailable, ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["status"] = "blocked" if isinstance(exc, FixtureUnavailable) else "fail"
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            cleaned = observer.cleanup_status(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if (
                isinstance(cleaned, dict) and cleaned.get("source") == "independent-cleanup" and
                cleaned.get("attempt_id") == attempt_id and cleaned.get("status") == "confirmed" and
                cleaned.get("owned_only") is True and type(cleaned.get("elapsed_seconds")) in (int, float) and
                0 <= cleaned["elapsed_seconds"] <= 600) else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    elif evidence["status"] == "pending-admission":
        evidence["status"] = "blocked"
    if evidence["cleanup"]["status"] != "confirmed" and evidence["status"] != "fail":
        evidence["status"] = "blocked"
        evidence["reason"] = "independent cleanup unconfirmed"
    return evidence
