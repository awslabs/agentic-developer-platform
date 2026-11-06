"""GitHub user-connection scenarios with an independent account oracle."""

import re
from uuid import uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable


def github_ac01(driver, observer):
    if (driver is None or observer is None or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True):
        raise FixtureUnavailable("isolated GitHub OAuth owner and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        expected = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(expected, dict) or expected.get("source") != "independent-oauth-provider" or
                not all(isinstance(expected.get(field), str) and expected[field]
                        for field in ("owner_id", "connection_id", "account_id")) or
                not re.fullmatch(r"[1-9][0-9]{0,18}", expected["account_id"]) or
                int(expected["account_id"]) >= 2**63 or expected.get("scope") != "read:user"):
            raise ValueError("independent GitHub user identity and scope required")
        before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        connection = driver.connect(fixture_id="owner-a", attempt_id=attempt_id)
        result = driver.call(fixture_id="owner-a", attempt_id=attempt_id)
        after = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        observed = observer.provider_identity(fixture_id="owner-a", attempt_id=attempt_id)
        receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
        if (connection != {"connection_id": expected["connection_id"], "owner_id": expected["owner_id"]} or
                not isinstance(result, dict) or type(result.get("id")) is not int or
                result["id"] != int(expected["account_id"]) or
                result.get("connection_id") != expected["connection_id"] or
                not isinstance(result.get("receipt_id"), str) or not result["receipt_id"] or
                receipt != {"receipt_id": result["receipt_id"], "status": "committed"} or
                observed != {"source": "independent-oauth-provider", "account_id": expected["account_id"],
                             "scope": "read:user"} or
                type(before) is not int or type(after) is not int or after != before + 1):
            raise ValueError("GitHub user ID, receipt, scope or provider call did not match independent evidence")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "provider_calls": 1,
                                    "receipt": "committed", "user_id_match": True}
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


def github_ac02(driver, observer):
    if (driver is None or observer is None or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True):
        raise FixtureUnavailable("isolated GitHub OAuth owners and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    faults = {
        "wrong_user": "GitHub connection unqualified",
        "missing_scope": "scope_not_granted",
        "withdrawn_oauth_consent": "GitHub connection unqualified",
        "withdrawn_tool_consent": "tool_not_consented",
        "disconnected_with_pat": "GitHub connection unqualified",
    }
    try:
        owners = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                  for fixture_id in ("owner-a", "owner-b")}
        if (any(not isinstance(item, dict) or item.get("source") != "independent-oauth-provider" or
                not all(isinstance(item.get(field), str) and item[field]
                        for field in ("owner_id", "connection_id", "account_id")) or
                not re.fullmatch(r"[1-9][0-9]{0,18}", item["account_id"]) or item.get("scope") != "read:user"
                for item in owners.values()) or
                len({item["owner_id"] for item in owners.values()}) != 2 or
                len({item["connection_id"] for item in owners.values()}) != 2 or
                len({item["account_id"] for item in owners.values()}) != 2):
            raise ValueError("distinct independently observed GitHub owners required")
        for fixture_id in owners:
            other_id = "owner-b" if fixture_id == "owner-a" else "owner-a"
            for fault, reason in faults.items():
                before = {owner_id: (observer.provider_calls(fixture_id=owner_id, attempt_id=attempt_id),
                                     observer.broker_calls(fixture_id=owner_id, attempt_id=attempt_id),
                                     observer.pat_calls(fixture_id=owner_id, attempt_id=attempt_id))
                          for owner_id in owners}
                refused = driver.probe(fixture_id=fixture_id, foreign_fixture_id=other_id,
                                       fault=fault, attempt_id=attempt_id)
                after = {owner_id: (observer.provider_calls(fixture_id=owner_id, attempt_id=attempt_id),
                                    observer.broker_calls(fixture_id=owner_id, attempt_id=attempt_id),
                                    observer.pat_calls(fixture_id=owner_id, attempt_id=attempt_id))
                         for owner_id in owners}
                if (refused != {"status": 403, "reason": reason, "content": []} or
                        any(type(count) is not int or count != 0 for counts in before.values() for count in counts) or
                        after != before):
                    raise ValueError("GitHub denial dispatched a provider, broker or legacy PAT action")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "owners": 2,
                                    "denials": len(owners) * len(faults), "provider_calls": 0,
                                    "broker_calls": 0, "pat_calls": 0}
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


def github_ac03(driver, observer):
    if (driver is None or observer is None or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True):
        raise FixtureUnavailable("isolated GitHub refresh, PAT-grace audit and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        owners = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                  for fixture_id in ("owner-a", "owner-b")}
        now = observer.clock(attempt_id=attempt_id)
        if (type(now) is not int or
                any(not isinstance(item, dict) or item.get("source") != "independent-oauth-provider" or
                    item.get("owner_id") != fixture_id or item.get("scope") != "read:user" or
                    not isinstance(item.get("connection_id"), str) or not item["connection_id"] or
                    not isinstance(item.get("account_id"), str) or
                    not re.fullmatch(r"[1-9][0-9]{0,18}", item["account_id"]) or
                    type(item.get("expired_at")) is not int or item["expired_at"] >= now
                    for fixture_id, item in owners.items()) or
                len({item["connection_id"] for item in owners.values()}) != 2 or
                len({item["account_id"] for item in owners.values()}) != 2):
            raise ValueError("two independently observed, expired owner-bound connections required")
        for fixture_id, expected in owners.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            refreshed = driver.refresh_read(fixture_id=fixture_id, now=now, attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            receipt = observer.receipt(fixture_id=fixture_id, attempt_id=attempt_id)
            observed = observer.provider_identity(fixture_id=fixture_id, attempt_id=attempt_id)
            if (not isinstance(refreshed, dict) or type(refreshed.get("id")) is not int or
                    refreshed["id"] != int(expected["account_id"]) or
                    refreshed.get("connection_id") != expected["connection_id"] or
                    not isinstance(refreshed.get("receipt_id"), str) or not refreshed["receipt_id"] or
                    receipt != {"receipt_id": refreshed["receipt_id"], "status": "committed"} or
                    observed != {"source": "independent-oauth-provider", "account_id": expected["account_id"],
                                 "scope": "read:user"} or
                    observer.refresh_count(fixture_id=fixture_id, attempt_id=attempt_id) != 1 or
                    type(before) is not int or type(after) is not int or after != before + 1):
                raise ValueError("refresh did not preserve independently observed GitHub user identity")
            disconnected = driver.disconnect(fixture_id=fixture_id, attempt_id=attempt_id)
            denied = driver.read_after_disconnect(fixture_id=fixture_id, attempt_id=attempt_id)
            if (disconnected != {"status": "disconnected", "revocation_complete": False,
                                 "provider_token_revocation": "not_attempted"} or
                    denied != {"status": 403, "reason": "GitHub connection unqualified", "content": []} or
                    observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) != after or
                    observer.connection_removed(fixture_id=fixture_id,
                                                connection_id=expected["connection_id"], attempt_id=attempt_id) is not True):
                raise ValueError("new ADP call after disconnect was accepted or provider token revocation overstated")
            grace = observer.pat_grace(fixture_id=fixture_id, attempt_id=attempt_id)
            if (not isinstance(grace, dict) or grace.get("source") != "independent-adp-audit" or
                    type(grace.get("started_at")) is not int or
                    type(grace.get("grace_seconds")) is not int or
                    not 0 < grace["grace_seconds"] <= 365 * 86400):
                raise ValueError("independently audited legacy PAT grace boundary required")
            deadline = grace["started_at"] + grace["grace_seconds"]
            pat_before = observer.pat_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            permitted = driver.explicit_legacy_pat_read(fixture_id=fixture_id, at=deadline - 1, attempt_id=attempt_id)
            pat_after_read = observer.pat_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            denied_pat = driver.explicit_legacy_pat_read(fixture_id=fixture_id, at=deadline, attempt_id=attempt_id)
            if (permitted != {"status": 200, "channel": "explicit-legacy-pat"} or
                    denied_pat != {"status": 403, "reason": "github_pat_retired"} or
                    type(pat_before) is not int or type(pat_after_read) is not int or
                    pat_after_read != pat_before + 1 or
                    observer.pat_calls(fixture_id=fixture_id, attempt_id=attempt_id) != pat_after_read or
                    observer.pat_denial_audited(fixture_id=fixture_id, at=deadline, attempt_id=attempt_id) is not True or
                    observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) != after):
                raise ValueError("explicit PAT grace expiry did not deny and audit at the deadline")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "owners": 2,
                                    "refreshed_reads": 2, "new_adp_calls_after_disconnect": 0,
                                    "pat_grace": "explicit-only", "pat_expiry": "denied-and-audited",
                                    "provider_token_revocation": "not_attempted", "revocation_complete": False}
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


def github_ac04(driver, observer, client_name, revisions):
    from decimal import Decimal, InvalidOperation

    from tests.e2e.tools.pricing_envelope import reserve_all_in
    from tests.e2e.tools.scenarios import CLIENTS

    if (driver is None or observer is None or getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True or
            getattr(driver, "read_only", False) is not True or
            getattr(driver, "client_name", None) != client_name or client_name not in CLIENTS or
            any(not callable(getattr(observer, method, None)) for method in
                ("pricing", "deployment", "client_version", "expected", "provider_calls", "provider_identity",
                 "receipt", "refresh_count", "repo_mutations", "client_invocation", "session", "connection_removed", "cleaned"))):
        raise FixtureUnavailable("protected read-only GitHub client and independent provider observer required")
    attempt_id = str(uuid4())
    pricing = observer.pricing(client_name=client_name, attempt_id=attempt_id)
    if (not isinstance(pricing, dict) or pricing.get("source") != "independent-test-pricing" or
            type(pricing.get("max_sessions")) is not int or pricing["max_sessions"] != 1 or
            type(pricing.get("ttl_seconds")) is not int or pricing["ttl_seconds"] != 300 or
            type(pricing.get("cleanup_seconds")) is not int or pricing["cleanup_seconds"] != 600 or
            type(pricing.get("per_call_usd")) is not str or type(pricing.get("max_cell_usd")) is not str):
        raise FixtureUnavailable("fresh pricing and bounded GitHub session required")
    try:
        per_call = Decimal(pricing["per_call_usd"])
        max_cell = Decimal(pricing["max_cell_usd"])
        if (not per_call.is_finite() or not max_cell.is_finite() or per_call <= 0 or
                2 * per_call > max_cell or 3 * max_cell > Decimal(20)):
            raise ValueError("GitHub test-run price ceiling exceeded")
        quote = pricing.get("all_in_quote")
        if client_name != "cli" and (not isinstance(quote, dict) or type(quote.get("max_model_requests")) is not int or quote["max_model_requests"] < 9):
            raise ValueError("native model requests underpriced")
        estimate = reserve_all_in({"all_in_quote": quote, "unit_cost_usd": per_call,
                                   "expected_provider_calls": 2, "max_cost_usd": max_cell},
                                  execution_kind="stdio" if client_name == "cli" else "model")
    except (ValueError, KeyError, TypeError, InvalidOperation):
        raise FixtureUnavailable("fresh all-in GitHub pricing below the $20 run ceiling required") from None
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        release = observer.deployment(client_name=client_name, attempt_id=attempt_id)
        version = observer.client_version(client_name=client_name, attempt_id=attempt_id)
        if (not isinstance(release, dict) or release.get("source") != "independent-release-observer" or
                any(release.get(key) != expected for key, expected in revisions.items()) or
                not isinstance(release.get("connector_revision"), str) or not release["connector_revision"] or
                not isinstance(release.get("oauth_provider_revision"), str) or not release["oauth_provider_revision"] or
                not isinstance(version, dict) or version.get("source") != "independent-client-install" or
                version.get("name") != client_name or not version.get("version") or
                version["version"] != getattr(driver, "version", None)):
            raise ValueError("GitHub connector, OAuth provider and client revisions not independently pinned")
        expected = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(expected, dict) or expected.get("source") != "independent-oauth-provider" or
                expected.get("owner_id") != "owner-a" or expected.get("scope") != "read:user" or
                not isinstance(expected.get("connection_id"), str) or not expected["connection_id"] or
                not isinstance(expected.get("account_id"), str) or
                not re.fullmatch(r"[1-9][0-9]{0,18}", expected["account_id"]) or
                int(expected["account_id"]) >= 2**63):
            raise ValueError("independent numeric GitHub user ID and granted scope required")
        before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        mutations = observer.repo_mutations(attempt_id=attempt_id)
        result = driver.call(fixture_id="owner-a", operation="github___get_user", attempt_id=attempt_id)
        observation = observer.provider_identity(fixture_id="owner-a", attempt_id=attempt_id)
        receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(result, dict) or type(result.get("id")) is not int or
                result["id"] != int(expected["account_id"]) or
                result.get("connection_id") != expected["connection_id"] or
                not isinstance(result.get("receipt_id"), str) or not result["receipt_id"] or
                receipt != {"receipt_id": result["receipt_id"], "status": "committed"} or
                observation != {"source": "independent-oauth-provider", "account_id": expected["account_id"],
                                "scope": "read:user"} or
                type(before) is not int or observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before + 1):
            raise ValueError("read did not return independently observed GitHub user and committed receipt")
        refreshed = driver.refresh_read(fixture_id="owner-a", operation="github___get_user", attempt_id=attempt_id)
        after = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(refreshed, dict) or type(refreshed.get("id")) is not int or
                refreshed["id"] != int(expected["account_id"]) or
                observer.refresh_count(fixture_id="owner-a", attempt_id=attempt_id) != 1 or after != before + 2 or
                observer.provider_identity(fixture_id="owner-a", attempt_id=attempt_id) != observation):
            raise ValueError("refresh changed GitHub user or repeated a provider action")
        denied = driver.probe_wrong_user(fixture_id="owner-a", attempt_id=attempt_id)
        disconnected = driver.disconnect(fixture_id="owner-a", attempt_id=attempt_id)
        revoked = driver.read_after_disconnect(fixture_id="owner-a", attempt_id=attempt_id)
        session = observer.session(attempt_id=attempt_id)
        invocation = observer.client_invocation(client_name=client_name, attempt_id=attempt_id)
        if (denied != {"status": 403, "reason": "GitHub connection unqualified", "content": []} or
                disconnected != {"status": "disconnected", "revocation_complete": False,
                                 "provider_token_revocation": "not_attempted"} or
                revoked != {"status": 403, "reason": "GitHub connection unqualified", "content": []} or
                invocation != {"source": "independent-client-transcript", "name": client_name,
                               "operation": "github___get_user", "calls": 4} or
                observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != after or
                observer.connection_removed(fixture_id="owner-a", connection_id=expected["connection_id"],
                                            attempt_id=attempt_id) is not True or
                type(mutations) is not int or mutations != 0 or
                observer.repo_mutations(attempt_id=attempt_id) != 0 or
                not isinstance(session, dict) or session.get("source") != "independent-oauth-provider" or
                type(session.get("count")) is not int or session["count"] != 1 or
                type(session.get("ttl_seconds")) is not int or not 0 < session["ttl_seconds"] <= 300 or
                type(session.get("cleanup_seconds")) is not int or not 0 <= session["cleanup_seconds"] <= 600):
            raise ValueError("GitHub denial, cleanup bounds or read-only repository evidence failed")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "client": client_name,
                                    "connector_revision": release["connector_revision"],
                                    "oauth_provider_revision": release["oauth_provider_revision"],
                                    "pricing_source_revision": estimate["pricing_source_revision"],
                                    "estimated_all_in_usd": str(estimate["reserved_usd"]),
                                    "provider_calls": 2, "repo_mutations": 0, "max_sessions": 1,
                                    "ttl_seconds": session["ttl_seconds"], "cleanup_seconds": session["cleanup_seconds"],
                                    "provider_token_revocation": "not_attempted", "revocation_complete": False}
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
