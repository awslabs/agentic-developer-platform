"""Protected Jira read matrix with independently observed identity and issue content."""

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from tests.e2e.tools.pricing_envelope import reserve_all_in
from tests.e2e.tools.scenarios import CLIENTS, FixtureUnavailable

DENIALS = {
    "other_site": (403, "jira_site_denied", 0),
    "denied_issue": (404, "jira_not_found", 3),
    # Missing required scope invalidates the owner/account binding before dispatch.
    "missing_scope": (403, "jira_connection_unqualified", 0),
    "missing_consent": (403, "tool_not_consented", 0),
    "disconnected": (403, "jira_connection_unqualified", 0),
}


def _fixture(observer, attempt_id):
    value = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
    if (not isinstance(value, dict) or value.get("source") != "independent-jira-fixture" or
            any(not isinstance(value.get(field), str) or not value[field] for field in (
                "owner_id", "vendor_user_id", "connection_id", "cloud_id", "issue_id", "issue_key", "sentinel")) or
            value.get("scope") != "read:jira-work"):
        raise ValueError("independent Jira account, site and issue oracle required")
    return value


def _readback(expected):
    return {"source": "independent-jira-provider", "id": expected["issue_id"],
            "key": expected["issue_key"], "summary": expected["sentinel"]}


def _assert_read(result, expected, observer, attempt_id):
    receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
    identity = observer.provider_identity(fixture_id="owner-a", attempt_id=attempt_id)
    if (result != {"id": expected["issue_id"], "key": expected["issue_key"], "fields": {"summary": expected["sentinel"]}} or
            not isinstance(receipt, dict) or not isinstance(receipt.get("receipt_id"), str) or not receipt["receipt_id"] or
            receipt.get("status") != "committed" or
            any(receipt.get(field) != expected[field] for field in ("owner_id", "vendor_user_id", "connection_id")) or
            identity != {"source": "independent-jira-identity", "account_id": expected["vendor_user_id"], "scope": "read:me"}):
        raise ValueError("Jira read or attributed account differs from independent evidence")


def _repository_case(driver, observer, exercise, *, driver_methods=(), observer_methods=()):
    if (driver is None or observer is None or
            any(getattr(driver, flag, False) is not True for flag in ("isolated", "authorized", "read_only", "identity_qualified")) or
            any(not callable(getattr(driver, method, None)) for method in ("cleanup", *driver_methods)) or
            any(not callable(getattr(observer, method, None)) for method in (
                "expected", "provider_readback", "provider_calls", "mutations", "shared_token_calls", "cleaned", *observer_methods))):
        raise FixtureUnavailable("isolated consented Jira fixture and independent account/site/provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        expected = _fixture(observer, attempt_id)
        if (observer.provider_readback(fixture_id="owner-a", attempt_id=attempt_id) != _readback(expected) or
                type(observer.mutations(attempt_id=attempt_id)) is not int or observer.mutations(attempt_id=attempt_id) != 0 or
                type(observer.shared_token_calls(attempt_id=attempt_id)) is not int or observer.shared_token_calls(attempt_id=attempt_id) != 0):
            raise ValueError("fixture readback, mutation or shared-token baseline unavailable")
        observations = exercise(expected, attempt_id)
        if (observer.provider_readback(fixture_id="owner-a", attempt_id=attempt_id) != _readback(expected) or
                observer.mutations(attempt_id=attempt_id) != 0 or observer.shared_token_calls(attempt_id=attempt_id) != 0):
            raise ValueError("Jira scenario mutated user content or used a shared token")
        evidence.update(status="pending-cleanup", observations={"source": "independent-jira-provider", **observations})
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
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


def jira_ac01(driver, observer):
    def exercise(expected, attempt_id):
        before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        result = driver.read(fixture_id="owner-a", cloud_id=expected["cloud_id"], issue_id_or_key=expected["issue_id"], attempt_id=attempt_id)
        _assert_read(result, expected, observer, attempt_id)
        if type(before) is not int or observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before + 3:
            raise ValueError("Jira read requires independent account, site and issue observations")
        return {"provider_calls": 3, "user_and_issue_match": True, "receipt": "committed"}
    return _repository_case(driver, observer, exercise, driver_methods=("read",), observer_methods=("receipt", "provider_identity"))


def jira_ac02(driver, observer):
    def exercise(expected, attempt_id):
        for fault, (status, reason, dispatches) in DENIALS.items():
            before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
            denied = driver.deny(fixture_id="owner-a", fault=fault, attempt_id=attempt_id)
            if (denied != {"status": status, "reason": reason, "content": []} or type(before) is not int or
                    observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before + dispatches):
                raise ValueError("Jira denial changed content, vendor error or dispatch boundary")
        return {"denials": len(DENIALS), "provider_calls": 3, "shared_token_calls": 0}
    return _repository_case(driver, observer, exercise, driver_methods=("deny",))


def jira_ac03(driver, observer):
    def exercise(expected, attempt_id):
        now = observer.clock(attempt_id=attempt_id)
        expiry = observer.expired_access(fixture_id="owner-a", attempt_id=attempt_id)
        if (type(now) is not int or not isinstance(expiry, dict) or expiry.get("source") != "independent-token-lifecycle" or
                type(expiry.get("expires_at")) is not int or expiry["expires_at"] >= now):
            raise ValueError("independently observed expired access is required")
        before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        refreshes = observer.refresh_count(fixture_id="owner-a", attempt_id=attempt_id)
        result = driver.refresh_read(fixture_id="owner-a", cloud_id=expected["cloud_id"], issue_id_or_key=expected["issue_id"],
                                     now=now, attempt_id=attempt_id)
        _assert_read(result, expected, observer, attempt_id)
        if (type(before) is not int or type(refreshes) is not int or
                observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before + 3 or
                observer.refresh_count(fixture_id="owner-a", attempt_id=attempt_id) != refreshes + 1):
            raise ValueError("refresh must retain the same owner, account, site and receipt")
        faults = {"unauthorized": (401, "jira_unauthorized", {}), "forbidden": (403, "jira_forbidden", {}),
                  "not_found": (404, "jira_not_found", {}), "throttled": (429, "jira_throttled", {"Retry-After": "9"}),
                  "api_error": (502, "jira_api_error", {})}
        for fault, (status, reason, headers) in faults.items():
            before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
            refused = driver.provider_fault(fixture_id="owner-a", fault=fault, retry_after="9", attempt_id=attempt_id)
            if (refused != {"status": status, "reason": reason, "content": [], "headers": headers} or type(before) is not int or
                    observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before + 3):
                raise ValueError("provider fault must remain distinct without an automatic retry")
        before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        disconnected = driver.disconnect(fixture_id="owner-a", attempt_id=attempt_id)
        denied = driver.deny(fixture_id="owner-a", fault="disconnected", attempt_id=attempt_id)
        if (not isinstance(disconnected, dict) or disconnected.get("status") != "disconnected" or
                disconnected.get("revocation_complete") is not False or disconnected.get("provider_token_revocation") != "not_attempted" or
                denied != {"status": 403, "reason": "jira_connection_unqualified", "content": []} or
                observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before or
                observer.connection_removed(fixture_id="owner-a", connection_id=expected["connection_id"], attempt_id=attempt_id) is not True):
            raise ValueError("disconnect must deny new calls without overstating vendor revocation")
        return {"refreshed_reads": 1, "distinct_provider_faults": len(faults), "calls_after_disconnect": 0,
                "provider_token_revocation": "not_attempted"}
    return _repository_case(driver, observer, exercise,
                            driver_methods=("refresh_read", "provider_fault", "disconnect", "deny"),
                            observer_methods=("receipt", "provider_identity", "clock", "expired_access", "refresh_count", "connection_removed"))


def jira_ac04(driver, observer, client_name, revisions):
    if (driver is None or observer is None or client_name not in CLIENTS or
            getattr(driver, "client_name", None) != client_name or
            any(getattr(driver, flag, False) is not True for flag in ("isolated", "authorized", "read_only", "identity_qualified")) or
            any(not callable(getattr(observer, method, None)) for method in (
                "pricing", "deployment", "client_version", "expected", "provider_calls", "provider_readback",
                "receipt", "mutations", "client_invocation", "session", "cleaned"))):
        raise FixtureUnavailable("qualified Jira connection, protected read-only client and independent provider observer required")
    attempt_id = str(uuid4())
    pricing = observer.pricing(client_name=client_name, attempt_id=attempt_id)
    if (not isinstance(pricing, dict) or pricing.get("source") != "independent-test-pricing" or
            type(pricing.get("max_sessions")) is not int or pricing["max_sessions"] != 1 or
            type(pricing.get("ttl_seconds")) is not int or pricing["ttl_seconds"] != 300 or
            type(pricing.get("cleanup_seconds")) is not int or pricing["cleanup_seconds"] != 600 or
            type(pricing.get("per_call_usd")) is not str or type(pricing.get("max_cell_usd")) is not str):
        raise FixtureUnavailable("fresh pricing and bounded Jira session required")
    try:
        per_call = Decimal(pricing["per_call_usd"])
        max_cell = Decimal(pricing["max_cell_usd"])
        if (not per_call.is_finite() or not max_cell.is_finite() or per_call <= 0 or
                6 * per_call > max_cell or 3 * max_cell > Decimal(20)):
            raise ValueError("Jira test-run price ceiling exceeded")
        all_in = pricing.get("all_in_quote")
        if client_name != "cli" and (not isinstance(all_in, dict) or type(all_in.get("max_model_requests")) is not int or
                                     all_in["max_model_requests"] < 1 + len(DENIALS)):
            raise ValueError("native Jira invocation matrix is underpriced")
        quote = reserve_all_in(
            {"all_in_quote": pricing.get("all_in_quote"), "unit_cost_usd": per_call,
             "expected_provider_calls": 6, "max_cost_usd": max_cell},
            execution_kind="stdio" if client_name == "cli" else "model",
        )
    except (ValueError, KeyError, TypeError, InvalidOperation):
        raise FixtureUnavailable("fresh all-in Jira pricing below the $20 run ceiling required") from None
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        release = observer.deployment(client_name=client_name, attempt_id=attempt_id)
        version = observer.client_version(client_name=client_name, attempt_id=attempt_id)
        if (not isinstance(release, dict) or release.get("source") != "independent-release-observer" or
                any(release.get(field) != value for field, value in revisions.items()) or
                not isinstance(release.get("connector_revision"), str) or not release["connector_revision"] or
                not isinstance(release.get("oauth_provider_revision"), str) or not release["oauth_provider_revision"] or
                not isinstance(version, dict) or version.get("source") != "independent-client-install" or
                version.get("name") != client_name or not version.get("version") or
                version["version"] != getattr(driver, "version", None)):
            raise ValueError("Jira connector, OAuth provider and client revisions not independently pinned")
        fixture = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(fixture, dict) or fixture.get("source") != "independent-jira-fixture" or
                any(not isinstance(fixture.get(field), str) or not fixture[field] for field in (
                    "owner_id", "vendor_user_id", "connection_id", "cloud_id", "issue_id", "issue_key", "sentinel")) or
                fixture.get("scope") != "read:jira-work"):
            raise ValueError("independent Jira owner, scope and immutable issue fixture required")
        before = observer.provider_readback(fixture_id="owner-a", attempt_id=attempt_id)
        if before != {"source": "independent-jira-provider", "id": fixture["issue_id"],
                      "key": fixture["issue_key"], "summary": fixture["sentinel"]}:
            raise ValueError("independent Jira issue oracle unavailable")
        calls = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        mutations = observer.mutations(attempt_id=attempt_id)
        result = driver.read(fixture_id="owner-a", cloud_id=fixture["cloud_id"],
                             issue_id_or_key=fixture["issue_id"], attempt_id=attempt_id)
        receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
        if (result != {"id": fixture["issue_id"], "key": fixture["issue_key"],
                       "fields": {"summary": fixture["sentinel"]}} or
                not isinstance(receipt, dict) or not isinstance(receipt.get("receipt_id"), str) or
                not receipt["receipt_id"] or receipt.get("status") != "committed" or
                receipt.get("owner_id") != fixture["owner_id"] or
                receipt.get("vendor_user_id") != fixture["vendor_user_id"] or
                receipt.get("connection_id") != fixture["connection_id"] or
                type(calls) is not int or observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != calls + 3):
            raise ValueError("Jira issue or owner-bound receipt differs from independent provider evidence")
        calls += 3
        for fault, (status, reason, dispatches) in DENIALS.items():
            denial = driver.deny(fixture_id="owner-a", fault=fault, attempt_id=attempt_id)
            observed = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
            if (denial != {"status": status, "reason": reason, "content": []} or
                    type(observed) is not int or observed != calls + dispatches):
                raise ValueError("Jira denial or provider dispatch count changed")
            calls = observed
        session = observer.session(attempt_id=attempt_id)
        if (observer.client_invocation(client_name=client_name, attempt_id=attempt_id) != {
                "source": "independent-client-transcript", "name": client_name,
                "operation": "jira___get_issue", "calls": 1 + len(DENIALS)} or
                observer.provider_readback(fixture_id="owner-a", attempt_id=attempt_id) != before or
                type(mutations) is not int or mutations != 0 or observer.mutations(attempt_id=attempt_id) != 0 or
                not isinstance(session, dict) or session.get("source") != "independent-jira-provider" or
                type(session.get("count")) is not int or session["count"] != 1 or
                type(session.get("ttl_seconds")) is not int or not 0 < session["ttl_seconds"] <= 300 or
                type(session.get("cleanup_seconds")) is not int or not 0 <= session["cleanup_seconds"] <= 600):
            raise ValueError("Jira client, fixture integrity or session bounds failed")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {
            "source": "independent-jira-provider", "client": client_name,
            "connector_revision": release["connector_revision"],
            "oauth_provider_revision": release["oauth_provider_revision"],
            "pricing_source_revision": quote["pricing_source_revision"],
            "estimated_all_in_usd": str(quote["reserved_usd"]),
            "provider_calls": calls, "user_and_issue_match": True,
            "denials": len(DENIALS), "user_content_mutations": 0,
            "max_sessions": 1, "ttl_seconds": session["ttl_seconds"],
            "cleanup_seconds": session["cleanup_seconds"],
        }
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
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
