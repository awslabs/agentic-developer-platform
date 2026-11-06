"""Private fixture contract for two independently observed OAuth owners."""

from uuid import uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable


def oauth_ac01(driver, observer):
    if (
        driver is None or observer is None
        or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
    ):
        raise FixtureUnavailable("two isolated consenting owners and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        owners = {
            fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
            for fixture_id in ("owner-a", "owner-b")
        }
        if (
            any(
                not isinstance(item, dict)
                or item.get("source") != "independent-oauth-provider"
                or any(not isinstance(item.get(field), str) or not item[field] for field in ("owner_id", "connection_id", "account_id", "scope"))
                for item in owners.values()
            )
            or len({item["owner_id"] for item in owners.values()}) != 2
            or len({item["connection_id"] for item in owners.values()}) != 2
            or len({item["account_id"] for item in owners.values()}) != 2
        ):
            raise ValueError("two distinct provider-observed owners required")
        for fixture_id, expected in owners.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            connection = driver.connect(fixture_id=fixture_id, attempt_id=attempt_id)
            result = driver.call(fixture_id=fixture_id, attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            observed = observer.provider_identity(fixture_id=fixture_id, attempt_id=attempt_id)
            if (
                connection != {"connection_id": expected["connection_id"], "owner_id": expected["owner_id"]}
                or result != {"account_id": expected["account_id"], "connection_id": expected["connection_id"]}
                or observed != {"source": "independent-oauth-provider", "account_id": expected["account_id"], "scope": expected["scope"]}
                or type(before) is not int or type(after) is not int or after != before + 1
                or owners["owner-b" if fixture_id == "owner-a" else "owner-a"]["account_id"] in str(result)
            ):
                raise ValueError("wrong owner, scope, provider identity or provider-call count")
        for fixture_id in owners:
            other_id = "owner-b" if fixture_id == "owner-a" else "owner-a"
            for fault in ("foreign_state", "foreign_session", "other_user_token", "replayed_receipt"):
                before = {
                    owner_id: observer.provider_calls(fixture_id=owner_id, attempt_id=attempt_id)
                    for owner_id in owners
                }
                refused = driver.probe(fixture_id=fixture_id, foreign_fixture_id=other_id, fault=fault, attempt_id=attempt_id)
                after = {
                    owner_id: observer.provider_calls(fixture_id=owner_id, attempt_id=attempt_id)
                    for owner_id in owners
                }
                if refused != {"status": 403, "content": []} or after != before:
                    raise ValueError("foreign, replayed or mismatched consent dispatched a provider action")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "owners": 2, "provider_calls": 2, "denial_controls": 8}
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


def oauth_ac02(driver, observer):
    if (
        driver is None or observer is None
        or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
    ):
        raise FixtureUnavailable("two isolated refreshing owners and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        now = observer.clock(attempt_id=attempt_id)
        owners = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                  for fixture_id in ("owner-a", "owner-b")}
        if (
            type(now) is not int
            or any(
                not isinstance(expected, dict)
                or expected.get("source") != "independent-oauth-provider"
                or expected.get("expires_at") is None
                or type(expected["expires_at"]) is not int
                or expected["expires_at"] >= now
                or any(not isinstance(expected.get(field), str) or not expected[field]
                       for field in ("owner_id", "connection_id", "account_id", "scope"))
                or expected["owner_id"] != fixture_id
                for fixture_id, expected in owners.items()
            )
            or len({expected["connection_id"] for expected in owners.values()}) != 2
            or len({expected["account_id"] for expected in owners.values()}) != 2
        ):
            raise ValueError("two distinct expired provider-owned tokens required")
        for fixture_id, expected in owners.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            refresh_before = observer.refresh_count(fixture_id=fixture_id, attempt_id=attempt_id)
            result = driver.call_expired(fixture_id=fixture_id, attempt_id=attempt_id, now=now)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            refresh_after = observer.refresh_count(fixture_id=fixture_id, attempt_id=attempt_id)
            observation = observer.provider_identity(fixture_id=fixture_id, attempt_id=attempt_id)
            if (
                result != {"connection_id": expected["connection_id"], "account_id": expected["account_id"]}
                or observation != {"source": "independent-oauth-provider", "account_id": expected["account_id"],
                                   "scope": expected["scope"]}
                or type(before) is not int or type(after) is not int or after != before + 1
                or type(refresh_before) is not int or type(refresh_after) is not int
                or refresh_after != refresh_before + 1
            ):
                raise ValueError("refresh changed owner or observed scope")
        for fixture_id in owners:
            before = {owner: observer.provider_calls(fixture_id=owner, attempt_id=attempt_id) for owner in owners}
            refresh_before = observer.refresh_count(fixture_id=fixture_id, attempt_id=attempt_id)
            result = driver.probe_scope_change(fixture_id=fixture_id, attempt_id=attempt_id, scope="repo")
            after = {owner: observer.provider_calls(fixture_id=owner, attempt_id=attempt_id) for owner in owners}
            if (
                result != {"status": 403, "content": []}
                or after != before
                or observer.refresh_count(fixture_id=fixture_id, attempt_id=attempt_id) != refresh_before
            ):
                raise ValueError("changed scopes reused stale consent or dispatched provider action")
        if observer.clock(attempt_id=attempt_id) != now:
            raise ValueError("fixed refresh clock changed")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "owners": 2,
                                    "refreshes": 2, "provider_calls": 2, "scope_change_denials": 2}
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


def oauth_ac03(driver, observer):
    if (
        driver is None or observer is None
        or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
    ):
        raise FixtureUnavailable("isolated consenting owners and independent token-store observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        owners = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                  for fixture_id in ("owner-a", "owner-b")}
        if (
            any(
                not isinstance(expected, dict)
                or expected.get("source") != "independent-oauth-provider"
                or expected.get("owner_id") != fixture_id
                or not isinstance(expected.get("connection_id"), str)
                or not expected["connection_id"]
                for fixture_id, expected in owners.items()
            )
            or owners["owner-a"]["connection_id"] == owners["owner-b"]["connection_id"]
        ):
            raise ValueError("two distinct owner-bound connections required")
        for fixture_id, expected in owners.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            active_before = observer.token_active(fixture_id=fixture_id, attempt_id=attempt_id)
            disconnected = driver.disconnect(fixture_id=fixture_id, attempt_id=attempt_id)
            denied = driver.call_after_disconnect(fixture_id=fixture_id, attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            active_after = observer.token_active(fixture_id=fixture_id, attempt_id=attempt_id)
            if (
                disconnected != {"status": "disconnected", "identity_token_revoked": False,
                                 "provider_token_revocation": "not_attempted", "revocation_complete": False,
                                 "provider_access_token_expires_at": None, "private_assertion_max_seconds": 30}
                or denied != {"status": 403, "content": []}
                or type(before) is not int or type(after) is not int or after != before
                or active_before is not True or active_after is not True
            ):
                raise ValueError("disconnect misreported vendor revocation or dispatched another provider call")
            if observer.connection_removed(fixture_id=fixture_id, connection_id=expected["connection_id"],
                                           attempt_id=attempt_id) is not True:
                raise ValueError("owned connection remained available after disconnect")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "owners": 2,
                                    "new_adp_calls_denied": 2, "new_provider_calls": 0,
                                    "provider_token_active": True, "revocation_complete": False}
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "blocked"
        evidence["reason"] = ("provider token remains active; Identity user-token deletion unavailable"
                              if evidence["cleanup"]["status"] == "confirmed" else "independent cleanup not confirmed")
    return evidence


def oauth_ac04(driver, observer, client_name, revisions):
    from decimal import Decimal, InvalidOperation

    from tests.e2e.tools.pricing_envelope import reserve_all_in

    if (
        driver is None or observer is None or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
        or getattr(driver, "client_name", None) != client_name
        or client_name not in ("cli", "claude-code", "codex")
        or any(not callable(getattr(observer, method, None)) for method in
               ("pricing", "deployment", "client_version", "expected", "clock", "provider_calls",
                "provider_identity", "refresh_count", "token_active", "session", "cleaned"))
    ):
        raise FixtureUnavailable("protected OAuth deployment, consenting owners and independent observer required")
    attempt_id = str(uuid4())
    pricing = observer.pricing(client_name=client_name, attempt_id=attempt_id)
    if (
        not isinstance(pricing, dict) or pricing.get("source") != "independent-test-pricing"
        or type(pricing.get("max_sessions")) is not int or pricing["max_sessions"] != 1
        or type(pricing.get("ttl_seconds")) is not int or pricing["ttl_seconds"] != 300
        or type(pricing.get("cleanup_seconds")) is not int or pricing["cleanup_seconds"] != 600
        or type(pricing.get("per_call_usd")) is not str or type(pricing.get("max_cell_usd")) is not str
    ):
        raise FixtureUnavailable("fresh protected pricing and bounded lifecycle session required")
    try:
        per_call = Decimal(pricing["per_call_usd"])
        max_cell = Decimal(pricing["max_cell_usd"])
        if (
            not per_call.is_finite() or not max_cell.is_finite() or per_call <= 0
            or 7 * per_call > max_cell or 3 * max_cell > Decimal(20)
        ):
            raise ValueError("OAuth test-run price ceiling exceeded")
        quote = pricing.get("all_in_quote")
        if client_name != "cli" and (not isinstance(quote, dict) or
                                     type(quote.get("max_model_requests")) is not int or
                                     quote["max_model_requests"] < 9):
            raise ValueError("native client calls are not fully priced")
        estimate = reserve_all_in({"all_in_quote": quote, "unit_cost_usd": per_call,
                                   "expected_provider_calls": 7, "max_cost_usd": max_cell},
                                  execution_kind="stdio" if client_name == "cli" else "model")
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise FixtureUnavailable("fresh all-in OAuth pricing below the $20 run ceiling required") from None
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    try:
        release = observer.deployment(client_name=client_name, attempt_id=attempt_id)
        version = observer.client_version(client_name=client_name, attempt_id=attempt_id)
        if (
            not isinstance(release, dict) or release.get("source") != "independent-release-observer"
            or any(release.get(key) != expected for key, expected in revisions.items())
            or not isinstance(release.get("app_revisions"), dict)
            or set(release["app_revisions"]) != {"github", "atlassian", "slack"}
            or any(not isinstance(value, str) or not value for value in release["app_revisions"].values())
            or not isinstance(version, dict) or version.get("source") != "independent-client-install"
            or version.get("name") != client_name or not version.get("version")
            or version["version"] != getattr(driver, "version", None)
        ):
            raise ValueError("protected app, client and deployment revisions are not independently pinned")
        now = observer.clock(attempt_id=attempt_id)
        owners = {fixture_id: observer.expected(fixture_id=fixture_id, attempt_id=attempt_id)
                  for fixture_id in ("owner-a", "owner-b")}
        if (
            type(now) is not int or any(
                not isinstance(expected, dict) or expected.get("source") != "independent-oauth-provider"
                or expected.get("owner_id") != fixture_id or type(expected.get("expires_at")) is not int
                or expected["expires_at"] >= now
                or any(not isinstance(expected.get(field), str) or not expected[field]
                       for field in ("connection_id", "account_id", "scope"))
                for fixture_id, expected in owners.items()
            ) or len({expected["connection_id"] for expected in owners.values()}) != 2
            or len({expected["account_id"] for expected in owners.values()}) != 2
        ):
            raise ValueError("two isolated consenting owners and an expired token required")
        for fixture_id, expected in owners.items():
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            connected = driver.connect(fixture_id=fixture_id, attempt_id=attempt_id)
            result = driver.call(fixture_id=fixture_id, attempt_id=attempt_id)
            after = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            if (
                connected != {"connection_id": expected["connection_id"], "owner_id": expected["owner_id"]}
                or result != {"connection_id": expected["connection_id"], "account_id": expected["account_id"]}
                or observer.provider_identity(fixture_id=fixture_id, attempt_id=attempt_id) !=
                {"source": "independent-oauth-provider", "account_id": expected["account_id"], "scope": expected["scope"]}
                or type(before) is not int or type(after) is not int or after != before + 2
            ):
                raise ValueError("connection or call used the wrong provider identity or scope")
        refreshed_before = observer.refresh_count(fixture_id="owner-a", attempt_id=attempt_id)
        before = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        refreshed = driver.call_expired(fixture_id="owner-a", attempt_id=attempt_id, now=now)
        if (
            refreshed != {"connection_id": owners["owner-a"]["connection_id"],
                          "account_id": owners["owner-a"]["account_id"]}
            or observer.refresh_count(fixture_id="owner-a", attempt_id=attempt_id) != refreshed_before + 1
            or observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before + 1
            or observer.provider_identity(fixture_id="owner-a", attempt_id=attempt_id) !=
            {"source": "independent-oauth-provider", "account_id": owners["owner-a"]["account_id"],
             "scope": owners["owner-a"]["scope"]}
        ):
            raise ValueError("refresh changed provider owner or scope")
        for fixture_id in owners:
            disconnected = driver.disconnect(fixture_id=fixture_id, attempt_id=attempt_id)
            before = observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id)
            denied = driver.call_after_disconnect(fixture_id=fixture_id, attempt_id=attempt_id)
            if (
                disconnected != {"status": "disconnected", "identity_token_revoked": False,
                                 "provider_token_revocation": "not_attempted", "revocation_complete": False,
                                 "provider_access_token_expires_at": None, "private_assertion_max_seconds": 30}
                or denied != {"status": 403, "content": []}
                or observer.provider_calls(fixture_id=fixture_id, attempt_id=attempt_id) != before
                or observer.token_active(fixture_id=fixture_id, attempt_id=attempt_id) is not True
            ):
                raise ValueError("disconnect dispatched provider action or misreported token retention")
        session = observer.session(attempt_id=attempt_id)
        if (
            observer.clock(attempt_id=attempt_id) != now or not isinstance(session, dict)
            or session.get("source") != "independent-oauth-provider"
            or type(session.get("count")) is not int or session["count"] != 1
            or type(session.get("ttl_seconds")) is not int or not 0 < session["ttl_seconds"] <= 300
            or type(session.get("cleanup_seconds")) is not int or not 0 <= session["cleanup_seconds"] <= 600
        ):
            raise ValueError("session lifetime or cleanup exceeded protected bounds")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "client": client_name,
                                    "provider_app_revisions_recorded": sorted(release["app_revisions"]),
                                    "pricing_source_revision": estimate["pricing_source_revision"],
                                    "estimated_all_in_usd": str(estimate["reserved_usd"]),
                                    "provider_calls": 5, "max_sessions": 1, "ttl_seconds": session["ttl_seconds"],
                                    "cleanup_seconds": session["cleanup_seconds"],
                                    "new_provider_calls_after_disconnect": 0, "revocation_complete": False}
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "blocked"
        evidence["reason"] = ("provider token remains active after ADP disconnect"
                              if evidence["cleanup"]["status"] == "confirmed" else "independent cleanup unconfirmed")
    return evidence
