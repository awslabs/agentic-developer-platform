"""Independent synthetic-fixture assertions for consented broker delivery."""

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from tests.e2e.tools.pricing_envelope import reserve_all_in
from tests.e2e.tools.scenarios import FixtureUnavailable

OPERATIONS = ("list", "proxy_request", "assume_role", "materialize")


def broker_ac01(driver, observer):
    if driver is None or observer is None or getattr(driver, "isolated", False) is not True:
        raise FixtureUnavailable("isolated broker owners and independent credential observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        fixtures = [observer.expected(fixture_id=owner, attempt_id=attempt_id) for owner in ("owner-a", "owner-b")]
        if (any(not isinstance(item, dict) or item.get("source") != "independent-broker-fixture" or
                not isinstance(item.get("credential_id"), str) or not isinstance(item.get("sentinel"), str)
                for item in fixtures) or fixtures[0]["credential_id"] == fixtures[1]["credential_id"]):
            raise ValueError("independent isolated broker fixtures unavailable")
        discovered = driver.discover(attempt_id=attempt_id)
        advertised = [tool for tool in discovered if isinstance(tool, dict) and str(tool.get("name", "")).startswith("credentials___")]
        if ({tool["name"] for tool in advertised} != {"credentials___" + operation for operation in OPERATIONS} or
                any(not isinstance(tool.get("inputSchema"), dict) or
                    {"user_id", "agent_id", "task_id"} & set(tool["inputSchema"].get("properties", {})) for tool in advertised)):
            raise ValueError("broker inventory requires a caller-selected identity")
        for owner, expected, foreign in zip(("owner-a", "owner-b"), fixtures, reversed(fixtures)):
            before = observer.provider_calls(fixture_id=owner, attempt_id=attempt_id)
            listing = driver.invoke(fixture_id=owner, operation="list", arguments={}, attempt_id=attempt_id)
            after_list = observer.provider_calls(fixture_id=owner, attempt_id=attempt_id)
            proxy = driver.invoke(fixture_id=owner, operation="proxy_request", arguments={"service": "fixture", "method": "GET", "url": expected["url"]}, attempt_id=attempt_id)
            after_proxy = observer.provider_calls(fixture_id=owner, attempt_id=attempt_id)
            if (type(before) is not int or after_list != before or after_proxy != before + 1 or
                    listing != [{"id": expected["credential_id"], "service": "fixture", "label": "owned", "credential_type": "bearer"}] or
                    proxy != {"status": 200, "body": expected["result"]} or
                    foreign["credential_id"] in str(listing) or
                    any(item["sentinel"] in str((listing, proxy, observer.generic_evidence(attempt_id=attempt_id))) for item in fixtures)):
                raise ValueError("broker result, ownership, provider count or private evidence assertion failed")
        evidence.update(status="pass", observations={"source": "independent-broker-fixture", "owners": 2, "operations": ["credentials___list", "credentials___proxy_request"]})
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, TypeError, AttributeError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["cleanup"]["status"] != "confirmed":
        evidence["status"] = "blocked"
    return evidence


def broker_ac02(driver, observer):
    if driver is None or observer is None or getattr(driver, "isolated", False) is not True:
        raise FixtureUnavailable("isolated role/file owners and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        now = observer.clock(attempt_id=attempt_id)
        if type(now) is not int:
            raise ValueError("independent fixed clock required")
        fixtures = {}
        for owner in ("owner-a", "owner-b"):
            for operation in ("assume_role", "materialize"):
                fixture_id = f"{owner}-{operation}"
                expected = observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                if (not isinstance(expected, dict) or expected.get("source") != "independent-broker-fixture" or
                        expected.get("owner_id") != owner or
                        any(not isinstance(expected.get(field), str) or not expected[field]
                            for field in ("credential_id", "receipt_id", "service", "label", "sentinel"))):
                    raise ValueError("independent owned role/file fixture unavailable")
                fixtures[fixture_id] = expected
        if len({fixture["credential_id"] for fixture in fixtures.values()}) != len(fixtures):
            raise ValueError("credential fixture ownership is not isolated")
        for fixture_id, expected in fixtures.items():
            operation = fixture_id.split("-", 2)[-1]
            maximum = 900 if operation == "assume_role" else 300
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            result = driver.invoke(
                fixture_id=fixture_id, operation=operation,
                arguments={"service": expected["service"], "label": expected["label"], "use_seconds": maximum},
                attempt_id=attempt_id,
            )
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            issued = observer.issued(fixture_id=fixture_id, attempt_id=attempt_id)
            if (type(before) is not int or after != before + 1 or
                    result != {"receipt_id": expected["receipt_id"], "status": "delivered"} or
                    not isinstance(issued, dict) or issued.get("source") != "independent-broker-fixture" or
                    issued.get("owner_id") != expected["owner_id"] or
                    issued.get("credential_id") != expected["credential_id"] or
                    issued.get("receipt_id") != expected["receipt_id"] or
                    issued.get("recipient_bound") is not True or
                    issued.get("volatile_sink") is not True or
                    type(issued.get("issued_at")) is not int or
                    type(issued.get("expires_at")) is not int or
                    not now <= issued["issued_at"] <= now + 10 or
                    not 0 < issued["expires_at"] - issued["issued_at"] <= maximum or
                    (operation == "assume_role" and issued["expires_at"] - issued["issued_at"] != 900)):
                raise ValueError("private role/file delivery, ownership or lifetime assertion failed")
            generic = observer.generic_evidence(attempt_id=attempt_id)
            if (not isinstance(generic, dict) or
                    any(fixture["sentinel"] in str((result, generic)) for fixture in fixtures.values()) or
                    any(key in str((result, generic)).lower() for key in ("secret_access_key", "session_token", "file_base64"))):
                raise ValueError("private credential escaped through generic evidence")
        evidence.update(status="pass", observations={
            "source": "independent-broker-fixture", "owners": 2,
            "operations": ["credentials___assume_role", "credentials___materialize"],
            "provider_calls": 4, "maximum_role_seconds": 900, "maximum_file_seconds": 300,
        })
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, TypeError, AttributeError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["cleanup"]["status"] != "confirmed":
        evidence["status"] = "blocked"
    return evidence


def broker_ac03(driver, observer):
    if driver is None or observer is None or getattr(driver, "isolated", False) is not True:
        raise FixtureUnavailable("isolated broker owners and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        fixed_time = observer.clock(attempt_id=attempt_id)
        if type(fixed_time) is not int:
            raise ValueError("independent fixed clock required")
        fixtures = {owner: observer.expected(fixture_id=owner, attempt_id=attempt_id) for owner in ("owner-a", "owner-b")}
        if (any(not isinstance(item, dict) or item.get("source") != "independent-broker-fixture" or
                item.get("owner_id") != owner or
                any(not isinstance(item.get(field), str) or not item[field]
                    for field in ("credential_id", "service", "label", "sentinel"))
                for owner, item in fixtures.items()) or
                len({item["credential_id"] for item in fixtures.values()}) != 2 or
                len({(item["service"], item["label"]) for item in fixtures.values()}) != 2):
            raise ValueError("independent isolated broker fixtures unavailable")
        attempts = 0
        for owner, foreign_owner in (("owner-a", "owner-b"), ("owner-b", "owner-a")):
            owned, foreign = fixtures[owner], fixtures[foreign_owner]
            for operation in OPERATIONS:
                for selector, forged in (("user_id", foreign_owner), ("agent_id", "forged-agent"), ("task_id", "forged-task")):
                    before = {fixture_id: observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) for fixture_id in fixtures}
                    arguments = {"service": owned["service"], "label": owned["label"], selector: forged}
                    result = driver.invoke(fixture_id=owner, operation=operation, arguments=arguments, attempt_id=attempt_id)
                    after = {fixture_id: observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) for fixture_id in fixtures}
                    if (any(type(count) is not int or count != 0 for count in before.values()) or
                            any(type(count) is not int for count in after.values()) or after != before or
                            result != {"status": "denied", "error": "identity_binding_failed"}):
                        raise ValueError("forged identity reached a broker or provider")
                    attempts += 1
                before = {fixture_id: observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) for fixture_id in fixtures}
                result = driver.invoke(
                    fixture_id=owner, operation=operation,
                    arguments={"service": foreign["service"], "label": foreign["label"]}, attempt_id=attempt_id,
                )
                after = {fixture_id: observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) for fixture_id in fixtures}
                expected = [] if operation == "list" else {"status": "denied", "error": "credential_not_found"}
                if (any(type(count) is not int or count != 0 for count in before.values()) or
                        any(type(count) is not int for count in after.values()) or after != before or
                        result != expected or foreign["credential_id"] in str(result)):
                    raise ValueError("foreign credential was visible or reached a provider")
                attempts += 1
        generic = observer.generic_evidence(attempt_id=attempt_id)
        if (observer.clock(attempt_id=attempt_id) != fixed_time or not isinstance(generic, dict) or
                any(item["sentinel"] in str(generic) for item in fixtures.values())):
            raise ValueError("denial clock or generic evidence changed")
        evidence.update(status="pass", observations={"source": "independent-broker-fixture", "owners": 2,
                                                     "operations": ["credentials___" + operation for operation in OPERATIONS],
                                                     "denied_attempts": attempts, "provider_calls": 0})
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, TypeError, AttributeError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["cleanup"]["status"] != "confirmed":
        evidence["status"] = "blocked"
    return evidence


def broker_ac04(driver, observer, client_name):
    if (driver is None or observer is None or getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True or getattr(driver, "client_name", None) != client_name or
            client_name not in ("cli", "claude-code", "codex") or
            any(not callable(getattr(observer, name, None)) for name in
                ("pricing", "expected", "client_version", "provider_calls", "generic_evidence", "session", "expired", "cleaned"))):
        raise FixtureUnavailable("protected client, isolated broker fixtures and independent provider observer required")
    attempt_id = str(uuid4())
    pricing = observer.pricing(client_name=client_name, attempt_id=attempt_id)
    if (not isinstance(pricing, dict) or pricing.get("source") != "independent-test-pricing" or
            type(pricing.get("max_sessions")) is not int or pricing["max_sessions"] != 1 or
            type(pricing.get("ttl_seconds")) is not int or pricing["ttl_seconds"] != 300 or
            type(pricing.get("cleanup_seconds")) is not int or pricing["cleanup_seconds"] != 600 or
            type(pricing.get("per_call_usd")) is not str or type(pricing.get("max_cell_usd")) is not str):
        raise FixtureUnavailable("independent pricing and one bounded session required")
    try:
        per_call = Decimal(pricing["per_call_usd"])
        max_cell = Decimal(pricing["max_cell_usd"])
    except (InvalidOperation, TypeError, ValueError):
        raise FixtureUnavailable("independent broker pricing unavailable") from None
    if (not per_call.is_finite() or not max_cell.is_finite() or per_call <= 0 or
            3 * per_call > max_cell or 3 * max_cell > Decimal(20)):
        raise FixtureUnavailable("broker cells exceed the $20 test-run ceiling")
    try:
        # Client selection, never fixture data, determines whether model work
        # exists. Include all eight positive/denial invocations for native clients.
        quote = pricing.get("all_in_quote")
        if client_name != "cli" and (not isinstance(quote, dict) or
                                     type(quote.get("max_model_requests")) is not int or
                                     quote["max_model_requests"] < 8):
            raise ValueError("native broker invocation bounds are incomplete")
        estimate = reserve_all_in({
            "all_in_quote": quote, "unit_cost_usd": per_call,
            "expected_provider_calls": 3, "max_cost_usd": max_cell,
        }, execution_kind="stdio" if client_name == "cli" else "model")
    except (ValueError, TypeError, KeyError):
        raise FixtureUnavailable("fresh all-in broker pricing within the three-cell ceiling required") from None
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        version = observer.client_version(client_name=client_name, attempt_id=attempt_id)
        if (not isinstance(version, dict) or version.get("source") != "independent-client-install" or
                version.get("name") != client_name or not version.get("version") or
                version["version"] != getattr(driver, "version", None)):
            raise ValueError("client version is not independently pinned")
        expected = {(client, operation): observer.expected(fixture_id=client + "-" + operation, attempt_id=attempt_id)
                    for client in ("cli", "claude-code", "codex") for operation in OPERATIONS}
        if (any(not isinstance(item, dict) or item.get("source") != "independent-broker-fixture" or
                item.get("client") != client or item.get("operation") != operation or
                any(not isinstance(item.get(field), str) or not item[field]
                    for field in ("credential_id", "service", "label", "sentinel", "receipt_id"))
                for (client, operation), item in expected.items()) or
                len({item["credential_id"] for item in expected.values()}) != len(expected)):
            raise ValueError("isolated client credentials unavailable")
        fixtures = {operation: expected[client_name, operation] for operation in OPERATIONS}
        discovered = driver.discover(attempt_id=attempt_id)
        if (not isinstance(discovered, list) or
                len([item for item in discovered if isinstance(item, dict) and
                     str(item.get("name", "")).startswith("credentials___")]) != len(OPERATIONS) or
                {item.get("name") for item in discovered if isinstance(item, dict) and
                 str(item.get("name", "")).startswith("credentials___")} !=
                {"credentials___" + operation for operation in OPERATIONS} or
                any(not isinstance(item, dict) or not isinstance(item.get("inputSchema"), dict) or
                    {"user_id", "agent_id", "task_id"} & set(item["inputSchema"].get("properties", {}))
                    for item in discovered if isinstance(item, dict) and str(item.get("name", "")).startswith("credentials___"))):
            raise ValueError("credential tools or their identity-free schema unavailable")
        for operation, item in fixtures.items():
            fixture_id = client_name + "-" + operation
            arguments = {"credential_id": item["credential_id"], "service": item["service"], "label": item["label"]}
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            result = driver.invoke(operation=operation, arguments=arguments, attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            if type(before) is not int or after != before + (operation != "list"):
                raise ValueError("broker provider call count differs from the independent observer")
            if operation == "list":
                if result != [{"id": item["credential_id"], "service": item["service"], "label": item["label"]}]:
                    raise ValueError("metadata-only listing changed")
            elif operation == "proxy_request":
                if result != {"status": 200, "body": item["result"]}:
                    raise ValueError("allowlisted proxy result changed")
            elif (result != {"status": "completed", "delivery": "private", "receipt_id": item["receipt_id"],
                             "expires_at": item["expires_at"]} or
                  type(item.get("issued_at")) is not int or type(item.get("expires_at")) is not int or
                  item["expires_at"] - item["issued_at"] != (900 if operation == "assume_role" else item.get("use_seconds")) or
                  operation == "materialize" and (type(item.get("use_seconds")) is not int or not 1 <= item["use_seconds"] <= 300)):
                raise ValueError("private role/file lifetime or recipient result changed")
            generic = str((result, observer.generic_evidence(attempt_id=attempt_id)))
            if any(other["sentinel"] in generic for other in expected.values()):
                raise ValueError("credential material entered generic evidence")
            revoked = driver.revoke(operation=operation, arguments=arguments, attempt_id=attempt_id)
            before_denied = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            denied = driver.invoke(operation=operation, arguments=arguments, attempt_id=attempt_id)
            if (revoked != {"status": "revoked"} or
                    denied != {"status": "denied", "error": "consent_revoked"} or
                    before_denied != after or observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) != after):
                raise ValueError("revocation allowed a new provider call")
        session = observer.session(attempt_id=attempt_id)
        if (any(observer.expired(fixture_id=client_name + "-" + operation,
                                 receipt_id=fixtures[operation]["receipt_id"], attempt_id=attempt_id) is not True
                for operation in ("assume_role", "materialize")) or
                not isinstance(session, dict) or session.get("source") != "independent-broker-fixture" or
                type(session.get("count")) is not int or session["count"] != 1 or
                type(session.get("ttl_seconds")) is not int or not 0 < session["ttl_seconds"] <= 300 or
                type(session.get("cleanup_seconds")) is not int or not 0 <= session["cleanup_seconds"] <= 600 or
                observer.expired(attempt_id=attempt_id) is not True):
            raise ValueError("session, expiry or cleanup exceeded independent bounds")
        evidence.update(status="pass", observations={"source": "independent-broker-fixture", "client": client_name,
                                                     "client_version": version["version"], "operations": list(OPERATIONS),
                                                     "estimated_all_in_usd": str(estimate["reserved_usd"]),
                                                     "pricing_source_revision": estimate["pricing_source_revision"],
                                                     "post_revocation_provider_calls": 0, "max_sessions": 1,
                                                     "ttl_seconds": 300, "cleanup_seconds": 600})
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["cleanup"]["status"] != "confirmed":
        evidence["status"] = "blocked"
    return evidence
