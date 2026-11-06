"""Slack read-only qualification against a separately observed isolated fixture."""

import re
from time import monotonic, sleep
from uuid import UUID, uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable


def slack_ac01(driver, observer):
    if (
        driver is None
        or observer is None
        or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
    ):
        raise FixtureUnavailable(
            "isolated Slack owner and independent provider observer required"
        )
    attempt_id = str(uuid4())
    evidence = {
        "status": "fail",
        "attempt_id": attempt_id,
        "cleanup": {"status": "not-run"},
    }
    try:
        expected = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
        if (
            not isinstance(expected, dict)
            or expected.get("source") != "independent-oauth-provider"
            or not all(
                isinstance(expected.get(field), str) and expected[field]
                for field in ("owner_id", "connection_id", "team_id", "user_id")
            )
            or not re.fullmatch(r"T[A-Z0-9]{8,63}", expected["team_id"])
            or not re.fullmatch(r"[UW][A-Z0-9]{8,63}", expected["user_id"])
            or expected.get("scope") != "users:read"
        ):
            raise ValueError("independent Slack user/workspace and read scope required")
        before_reads = observer.provider_calls(
            fixture_id="owner-a", attempt_id=attempt_id
        )
        before_posts = observer.provider_posts(
            fixture_id="owner-a", attempt_id=attempt_id
        )
        connection = driver.connect(fixture_id="owner-a", attempt_id=attempt_id)
        discovered = driver.discover(fixture_id="owner-a", attempt_id=attempt_id)
        result = driver.call(
            fixture_id="owner-a", attempt_id=attempt_id, user_id=expected["user_id"]
        )
        denied = driver.post_denied(fixture_id="owner-a", attempt_id=attempt_id)
        after_reads = observer.provider_calls(
            fixture_id="owner-a", attempt_id=attempt_id
        )
        after_posts = observer.provider_posts(
            fixture_id="owner-a", attempt_id=attempt_id
        )
        observed = observer.provider_identity(
            fixture_id="owner-a", attempt_id=attempt_id
        )
        receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
        if (
            connection
            != {
                "connection_id": expected["connection_id"],
                "owner_id": expected["owner_id"],
            }
            or discovered != ["slack___get_user"]
            or not isinstance(result, dict)
            or result.get("id") != expected["user_id"]
            or result.get("connection_id") != expected["connection_id"]
            or not isinstance(result.get("receipt_id"), str)
            or not result["receipt_id"]
            or receipt != {"receipt_id": result["receipt_id"], "status": "committed"}
            or observed
            != {
                "source": "independent-oauth-provider",
                "team_id": expected["team_id"],
                "user_id": expected["user_id"],
                "scope": "users:read",
            }
            or denied != {"error": "Slack connection unqualified"}
            or type(before_reads) is not int
            or type(after_reads) is not int
            or after_reads != before_reads + 2
            or type(before_posts) is not int
            or after_posts != before_posts
        ):
            raise ValueError(
                "Slack read, denied post, receipt, identity or independent call count mismatched"
            )
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {
            "source": "independent-oauth-provider",
            "provider_reads": 2,
            "provider_posts": 0,
            "receipt": "committed",
            "user_id_match": True,
        }
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        TimeoutError,
        OSError,
    ) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            evidence["cleanup"] = {
                "status": "confirmed"
                if observer.cleaned(attempt_id=attempt_id) is True
                else "unknown"
            }
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = (
            "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
        )
    return evidence


def slack_ac02(driver, observer):
    if (
        driver is None or observer is None
        or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
        or getattr(driver, "approved", False) is not True
        or getattr(driver, "cleanup_authorized", False) is not True
    ):
        raise FixtureUnavailable("owner-approved isolated Slack channel, deletion and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    before = None
    try:
        expected = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
        if (
            not isinstance(expected, dict)
            or expected.get("source") != "independent-oauth-provider"
            or not all(isinstance(expected.get(key), str) and expected[key] for key in (
                "owner_id", "connection_id", "team_id", "user_id", "channel_id", "message", "run_marker",
            ))
            or not re.fullmatch(r"T[A-Z0-9]{8,63}", expected["team_id"])
            or not re.fullmatch(r"[UW][A-Z0-9]{8,63}", expected["user_id"])
            or not re.fullmatch(r"[CG][A-Z0-9]{8,63}", expected["channel_id"])
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", expected["run_marker"])
            or UUID(expected["run_marker"]).version != 4
            or expected.get("scopes") != ["users:read", "chat:write"]
        ):
            raise ValueError("independent approved Slack fixture required")
        before = observer.messages(fixture_id="owner-a", attempt_id=attempt_id)
        before_posts = observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id)
        marker = expected["run_marker"]
        text = f"{expected['message']} [run:{marker}]"
        connected = driver.connect(fixture_id="owner-a", attempt_id=attempt_id)
        discovered = driver.discover(fixture_id="owner-a", attempt_id=attempt_id)
        result = driver.post(
            fixture_id="owner-a", attempt_id=attempt_id, channel_id=expected["channel_id"],
            text=text, run_marker=marker,
        )
        after = observer.messages(fixture_id="owner-a", attempt_id=attempt_id)
        after_posts = observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id)
        if (
            type(before_posts) is not int or type(after_posts) is not int
            or after_posts != before_posts + 1
            or not isinstance(before, list) or not isinstance(after, list)
            or len(after) != len(before) + 1 or after[:len(before)] != before
            or connected != {"owner_id": expected["owner_id"], "connection_id": expected["connection_id"]}
            or discovered != ["slack___get_user", "slack___post_message"]
            or not isinstance(result, dict)
            or not isinstance(result.get("receipt_id"), str) or not result["receipt_id"]
            or result.get("connection_id") != expected["connection_id"]
            or result.get("channel_id") != expected["channel_id"]
            or result.get("text") != text or result.get("user_id") != expected["user_id"]
            or result.get("run_marker") != marker
            or after[-1] != {"channel_id": expected["channel_id"], "user_id": expected["user_id"],
                             "text": text, "run_marker": marker, "ts": result.get("ts")}
            or not isinstance(result.get("ts"), str)
            or not re.fullmatch(r"[0-9]{10,16}\.[0-9]{1,6}", result["ts"])
        ):
            raise ValueError("Slack post receipt, channel readback, or exact provider count mismatched")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {"source": "independent-oauth-provider", "provider_posts": 1,
                                    "exact_message_count": 1, "channel_match": True, "user_match": True, "marker_match": True}
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup(attempt_id=attempt_id)
            confirmed = before is not None and observer.messages(fixture_id="owner-a", attempt_id=attempt_id) == before
            evidence["cleanup"] = {"status": "confirmed" if confirmed and observer.cleaned(attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def slack_ac03(driver, observer):
    if (
        driver is None or observer is None
        or getattr(driver, "isolated", False) is not True
        or getattr(driver, "authorized", False) is not True
        or getattr(driver, "approved", False) is not True
        or getattr(driver, "cleanup_authorized", False) is not True
    ):
        raise FixtureUnavailable("isolated Slack posting, receipts, cleanup and independent provider observer required")
    faults = {
        "ok_false": (403, "slack_api_error", 1, 0),
        "reply_loss": (503, "slack_post_uncertain", 1, 1),
        "revoked_scope": (403, "slack_write_scope_missing", 0, 0),
    }
    attempts = {}
    evidence = {"status": "fail", "observations": [], "cleanup": {"status": "not-run"}}
    try:
        for fault, (status, error, expected_calls, expected_posts) in faults.items():
            attempt_id = str(uuid4())
            attempts[attempt_id] = None
            expected = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
            if (
                not isinstance(expected, dict) or expected.get("source") != "independent-oauth-provider"
                or not all(isinstance(expected.get(field), str) and expected[field] for field in (
                    "owner_id", "connection_id", "team_id", "user_id", "channel_id", "message", "run_marker",
                ))
                or expected["owner_id"] != "owner-a"
                or expected.get("scopes") != ["users:read", "chat:write"]
                or not re.fullmatch(r"T[A-Z0-9]{8,63}", expected["team_id"])
                or not re.fullmatch(r"[UW][A-Z0-9]{8,63}", expected["user_id"])
                or not re.fullmatch(r"[CG][A-Z0-9]{8,63}", expected["channel_id"])
                or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", expected["run_marker"])
                or UUID(expected["run_marker"]).version != 4
            ):
                raise ValueError("independent Slack fault fixture required")
            marker = expected["run_marker"]
            text = f"{expected['message']} [run:{marker}]"
            before = observer.messages(fixture_id="owner-a", attempt_id=attempt_id)
            attempts[attempt_id] = before
            before_calls = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
            before_posts = observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id)
            connection = driver.connect(fixture_id="owner-a", attempt_id=attempt_id)
            discovered = driver.discover(fixture_id="owner-a", attempt_id=attempt_id, fault=fault)
            result = driver.fault(
                fixture_id="owner-a", attempt_id=attempt_id, fault=fault,
                channel_id=expected["channel_id"], text=text, run_marker=marker,
            )
            receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
            recovery = driver.retry(fixture_id="owner-a", attempt_id=attempt_id, run_marker=marker) if fault != "revoked_scope" else None
            after = observer.messages(fixture_id="owner-a", attempt_id=attempt_id)
            after_calls = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
            after_posts = observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id)
            posted = after[len(before):] if isinstance(before, list) and isinstance(after, list) and after[:len(before)] == before else None
            message = posted[0] if posted is not None and len(posted) == 1 else None
            if (
                connection != {"owner_id": expected["owner_id"], "connection_id": expected["connection_id"]}
                or discovered != (["slack___get_user"] if fault == "revoked_scope" else ["slack___get_user", "slack___post_message"])
                or not isinstance(result, dict) or result.get("status") != status or result.get("error") != error
                or (fault == "ok_false" and (result.get("provider_status") != 200 or result.get("provider_error") != "missing_scope"))
                or (fault != "ok_false" and ("provider_error" in result or "provider_status" in result))
                or (fault == "revoked_scope" and (result.get("receipt_id") is not None or receipt is not None or posted != []))
                or (fault != "revoked_scope" and (
                    not isinstance(result.get("receipt_id"), str) or not result["receipt_id"]
                    or receipt != {"receipt_id": result["receipt_id"], "status": "committed"}
                    or recovery != {"status": 409, "error": "Slack receipt already used"}
                ))
                or (fault != "reply_loss" and posted != [])
                or (fault == "reply_loss" and (
                    not isinstance(message, dict) or set(message) != {"channel_id", "user_id", "text", "run_marker", "ts"}
                    or {key: message[key] for key in ("channel_id", "user_id", "text", "run_marker")} != {
                        "channel_id": expected["channel_id"], "user_id": expected["user_id"], "text": text, "run_marker": marker,
                    }
                    or not isinstance(message["ts"], str)
                    or not re.fullmatch(r"[0-9]{10,16}\.[0-9]{1,6}", message["ts"])
                ))
                or type(before_calls) is not int or type(after_calls) is not int
                or after_calls - before_calls != expected_calls
                or type(before_posts) is not int or type(after_posts) is not int
                or after_posts - before_posts != expected_posts
            ):
                raise ValueError("Slack error, committed receipt, provider readback or retry count mismatched")
            evidence["observations"].append({
                "source": "independent-oauth-provider", "attempt_id": attempt_id, "fault": fault,
                "provider_calls": expected_calls, "provider_posts": expected_posts,
                "receipt": "absent" if fault == "revoked_scope" else "committed",
                "outcome": "observed-posted" if fault == "reply_loss" else "observed-no-post",
            })
            driver.cleanup(attempt_id=attempt_id)
            if observer.cleaned(attempt_id=attempt_id) is not True or observer.messages(
                fixture_id="owner-a", attempt_id=attempt_id,
            ) != before:
                raise ValueError("Slack fixture cleanup not independently confirmed")
        evidence["status"] = "pending-cleanup"
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        confirmed = bool(attempts)
        for attempt_id in attempts:
            try:
                driver.cleanup(attempt_id=attempt_id)
            except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
                confirmed = False
        for attempt_id, before in attempts.items():
            try:
                if before is None or observer.cleaned(attempt_id=attempt_id) is not True or observer.messages(
                    fixture_id="owner-a", attempt_id=attempt_id,
                ) != before:
                    confirmed = False
            except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
                confirmed = False
        evidence["cleanup"] = {"status": "confirmed" if confirmed else "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def slack_ac04(driver, observer, client_name, revisions):
    from decimal import Decimal, InvalidOperation

    from tests.e2e.tools.pricing_envelope import reserve_all_in
    from tests.e2e.tools.scenarios import CLIENTS

    required = ("pricing", "deployment", "client_version", "client_invocation", "expected", "provider_identity",
                "cleanup_authority", "provider_calls", "provider_posts", "messages", "receipt",
                "session", "cleaned")
    if (driver is None or observer is None or client_name not in CLIENTS or
            getattr(driver, "client_name", None) != client_name or
            getattr(driver, "isolated", False) is not True or
            getattr(driver, "authorized", False) is not True or
            getattr(driver, "cleanup_authorized", False) is not True or
            any(not callable(getattr(observer, method, None)) for method in required)):
        raise FixtureUnavailable("protected Slack client, deletion authority and independent observer required")
    attempt_id = str(uuid4())
    pricing = observer.pricing(client_name=client_name, attempt_id=attempt_id)
    if (not isinstance(pricing, dict) or pricing.get("source") != "independent-test-pricing" or
            type(pricing.get("max_sessions")) is not int or pricing["max_sessions"] != 1 or
            type(pricing.get("ttl_seconds")) is not int or pricing["ttl_seconds"] != 300 or
            type(pricing.get("cleanup_seconds")) is not int or pricing["cleanup_seconds"] != 600 or
            type(pricing.get("per_call_usd")) is not str or
            type(pricing.get("max_cell_usd")) is not str):
        raise FixtureUnavailable("fresh all-in Slack pricing and bounded session required")
    try:
        per_call = Decimal(pricing["per_call_usd"])
        max_cell = Decimal(pricing["max_cell_usd"])
        if (not per_call.is_finite() or not max_cell.is_finite() or per_call <= 0 or
                max_cell <= 0 or 3 * max_cell > Decimal("20") or 2 * per_call > max_cell):
            raise ValueError("Slack run price ceiling exceeded")
        if (client_name != "cli" and
                (not isinstance(pricing.get("all_in_quote"), dict) or
                 type(pricing["all_in_quote"].get("max_model_requests")) is not int or
                 pricing["all_in_quote"]["max_model_requests"] < 9)):
            raise ValueError("native client requests underpriced")
        estimate = reserve_all_in(
            {"all_in_quote": pricing.get("all_in_quote"), "unit_cost_usd": per_call,
             "expected_provider_calls": 2, "max_cost_usd": max_cell},
            execution_kind="stdio" if client_name == "cli" else "model",
        )
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise FixtureUnavailable("fresh all-in Slack pricing below the $20 run ceiling required") from None

    evidence = {"status": "fail", "attempt_id": attempt_id, "cleanup": {"status": "not-run"}}
    expected = None
    authority = None
    deletion_approved = False
    deadline = monotonic() + 600
    try:
        release = observer.deployment(client_name=client_name, attempt_id=attempt_id)
        version = observer.client_version(client_name=client_name, attempt_id=attempt_id)
        if (not isinstance(release, dict) or release.get("source") != "independent-release-observer" or
                any(release.get(key) != value for key, value in revisions.items()) or
                not release.get("connector_revision") or not release.get("oauth_provider_revision") or
                not isinstance(version, dict) or version.get("source") != "independent-client-install" or
                version.get("name") != client_name or not version.get("version") or
                version["version"] != getattr(driver, "version", None)):
            raise ValueError("Slack release and installed client not independently pinned")
        expected = observer.expected(fixture_id="owner-a", attempt_id=attempt_id)
        authority = observer.cleanup_authority(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(expected, dict) or expected.get("source") != "independent-oauth-provider" or
                expected.get("owner_id") != "owner-a" or
                expected.get("scopes") != ["users:read", "chat:write"] or
                any(not isinstance(expected.get(key), str) or not expected[key] for key in
                    ("connection_id", "team_id", "user_id", "channel_id", "message", "run_marker")) or
                not re.fullmatch(r"T[A-Z0-9]{8,63}", expected["team_id"]) or
                not re.fullmatch(r"[UW][A-Z0-9]{8,63}", expected["user_id"]) or
                not re.fullmatch(r"[CG][A-Z0-9]{8,63}", expected["channel_id"]) or
                not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", expected["run_marker"]) or
                UUID(expected["run_marker"]).version != 4 or
                authority != {"source": "independent-owner-approval", "owner_id": "owner-a",
                              "team_id": expected["team_id"], "user_id": expected["user_id"],
                              "channel_id": expected["channel_id"], "method": "chat.delete",
                              "isolated": True, "authorized": True}):
            raise ValueError("independent consent, isolated channel and executable deletion approval required")
        deletion_approved = True
        text = f"{expected['message']} [run:{expected['run_marker']}]"
        before = observer.messages(fixture_id="owner-a", attempt_id=attempt_id, timeout_seconds=5)
        before_calls = observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id)
        before_posts = observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(before, list) or type(before_calls) is not int or
                type(before_posts) is not int or any(
                    isinstance(message, dict) and message.get("run_marker") == expected["run_marker"]
                    for message in before)):
            raise ValueError("Slack baseline or unique run marker not independently established")
        connected = driver.connect(fixture_id="owner-a", attempt_id=attempt_id)
        discovered = driver.discover(fixture_id="owner-a", attempt_id=attempt_id)
        if (connected != {"owner_id": "owner-a", "connection_id": expected["connection_id"]} or
                discovered != ["slack___get_user", "slack___post_message"]):
            raise ValueError("Slack connection or write consent not qualified")
        user = driver.call(fixture_id="owner-a", operation="slack___get_user",
                           attempt_id=attempt_id, user_id=expected["user_id"])
        identity = observer.provider_identity(fixture_id="owner-a", attempt_id=attempt_id)
        if (not isinstance(user, dict) or user.get("id") != expected["user_id"] or
                identity != {"source": "independent-oauth-provider", "team_id": expected["team_id"],
                             "user_id": expected["user_id"], "scopes": expected["scopes"]} or
                observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before_calls + 1 or
                observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id) != before_posts):
            raise ValueError("Slack user, workspace or write scope changed before post")
        result = driver.post(fixture_id="owner-a", attempt_id=attempt_id,
                             channel_id=expected["channel_id"], text=text,
                             run_marker=expected["run_marker"])
        after = observer.messages(fixture_id="owner-a", attempt_id=attempt_id, timeout_seconds=5)
        posted = after[len(before):] if isinstance(after, list) and after[:len(before)] == before else None
        receipt = observer.receipt(fixture_id="owner-a", attempt_id=attempt_id)
        invocation = observer.client_invocation(client_name=client_name, attempt_id=attempt_id)
        session = observer.session(attempt_id=attempt_id)
        if (not isinstance(result, dict) or
                any(result.get(key) != value for key, value in
                    {"connection_id": expected["connection_id"], "channel_id": expected["channel_id"],
                     "user_id": expected["user_id"], "text": text,
                     "run_marker": expected["run_marker"]}.items()) or
                not isinstance(result.get("receipt_id"), str) or not result["receipt_id"] or
                receipt != {"receipt_id": result["receipt_id"], "status": "committed"} or
                invocation != {"source": "independent-client-transcript", "name": client_name,
                               "operations": ["slack___get_user", "slack___post_message"], "calls": 2} or
                not isinstance(result.get("ts"), str) or
                not re.fullmatch(r"[0-9]{10,16}\.[0-9]{1,6}", result["ts"]) or
                posted != [{"channel_id": expected["channel_id"], "user_id": expected["user_id"],
                            "text": text, "run_marker": expected["run_marker"], "ts": result["ts"]}] or
                observer.provider_calls(fixture_id="owner-a", attempt_id=attempt_id) != before_calls + 2 or
                observer.provider_posts(fixture_id="owner-a", attempt_id=attempt_id) != before_posts + 1 or
                not isinstance(session, dict) or session.get("source") != "independent-oauth-provider" or
                type(session.get("count")) is not int or session["count"] != 1 or
                type(session.get("ttl_seconds")) is not int or not 0 < session["ttl_seconds"] <= 300 or
                type(session.get("cleanup_seconds")) is not int or not 0 <= session["cleanup_seconds"] <= 600):
            raise ValueError("Slack exact read/post, receipt, call count or session bounds mismatched")
        evidence["status"] = "pending-cleanup"
        evidence["observations"] = {
            "source": "independent-oauth-provider", "client": client_name,
            "connector_revision": release["connector_revision"],
            "oauth_provider_revision": release["oauth_provider_revision"],
            "estimated_all_in_usd": str(estimate["reserved_usd"]),
            "provider_calls": 2, "provider_posts": 1, "exact_message_count": 1,
            "max_sessions": 1, "ttl_seconds": session["ttl_seconds"],
            "cleanup_seconds": session["cleanup_seconds"],
        }
    except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        if deletion_approved:
            try:
                current = observer.messages(fixture_id="owner-a", attempt_id=attempt_id, timeout_seconds=5)
                owned = [message for message in current if isinstance(message, dict) and
                         message.get("channel_id") == expected["channel_id"] and
                         message.get("user_id") == expected["user_id"] and
                         message.get("run_marker") == expected["run_marker"] and
                         message.get("text") == f"{expected['message']} [run:{expected['run_marker']}]" and
                         isinstance(message.get("ts"), str)]
                if len(owned) > 1 or (not owned and observer.cleaned(attempt_id=attempt_id) is not True) or monotonic() > deadline:
                    raise ValueError("ambiguous Slack cleanup or deadline exceeded")
                for message in owned:
                    driver.delete(attempt_id=attempt_id, channel_id=expected["channel_id"],
                                  ts=message["ts"], method="chat.delete")
                while monotonic() <= deadline:
                    remaining = observer.messages(fixture_id="owner-a", attempt_id=attempt_id, timeout_seconds=5)
                    if (isinstance(remaining, list) and not any(isinstance(message, dict) and
                            message.get("run_marker") == expected["run_marker"] for message in remaining) and
                            observer.cleaned(attempt_id=attempt_id) is True and monotonic() <= deadline):
                        evidence["cleanup"] = {"status": "confirmed"}
                        break
                    sleep(min(1, max(0, deadline - monotonic())))
            except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError):
                evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence
