"""Local browser denial checks with independent provider-effect readback."""

import base64
import binascii
import hashlib
import json
from uuid import UUID, uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable

DENIALS = {
    "stale_view": "Browser session or view unavailable",
    "foreign_candidate": "Browser candidate unavailable",
    "prohibited_destination": "Analysis destination refused",
    "revoked_access": "grant revoked",
}
COUNTERS = ("network_requests", "browser_actions", "mutations")


def browser_ac02(driver, observer):
    if (driver is None or observer is None or
            not all(getattr(driver, name, False) is True for name in
                    ("isolated", "authorized", "inert_https"))):
        raise FixtureUnavailable("isolated inert HTTPS browser fixture and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "denials": {}, "status": "blocked",
                "cleanup": {"status": "not-run", "provider_termination": None}}
    try:
        opened = driver.open_owned(attempt_id=attempt_id)
        if (not isinstance(opened, dict) or not isinstance(opened.get("session_id"), str) or
                not opened["session_id"] or not isinstance(opened.get("view_id"), str) or
                not opened["view_id"] or not isinstance(opened.get("candidate_id"), str) or
                not opened["candidate_id"]):
            raise ValueError("browser fixture did not return owned session, view and candidate")
        baseline = observer.snapshot(attempt_id=attempt_id)
        if (baseline.get("source") != "independent-browser-provider" or
                any(type(baseline.get(name)) is not int or baseline[name] < 0 for name in COUNTERS)):
            raise ValueError("independent browser effect counters unavailable")
        for case, error in DENIALS.items():
            if case == "revoked_access":
                driver.revoke_owned(attempt_id=attempt_id)
            result = driver.step_denied(case, opened, attempt_id=attempt_id)
            observed = observer.snapshot(attempt_id=attempt_id)
            if (result.get("status") != "rejected" or result.get("error") != error or
                    observed.get("source") != baseline["source"] or
                    any(observed.get(name) != baseline[name] for name in COUNTERS)):
                raise ValueError("browser denial or independent effect assertion failed")
            evidence["denials"][case] = error
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.close_owned(attempt_id=attempt_id)
            terminated = observer.terminated(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if terminated is True else "unknown",
                                   "provider_termination": terminated}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown", "provider_termination": None}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def browser_ac03(driver, observer):
    if (driver is None or observer is None or
            not all(getattr(driver, name, False) is True for name in
                    ("isolated", "authorized", "inert_https"))):
        raise FixtureUnavailable("isolated browser recovery fixture and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "status": "blocked",
                "cleanup": {"status": "not-run", "provider_termination": None}}
    opened = None
    try:
        opened = driver.start_owned(attempt_id=attempt_id)
        if (not isinstance(opened, dict) or
                not all(isinstance(opened.get(field), str) and opened[field] for field in
                        ("session_id", "view_id", "provider_id", "artifact_sha256")) or
                type(opened.get("artifact_size")) is not int or not 0 < opened["artifact_size"] <= 128000 or
                type(opened.get("ttl_seconds")) is not int or not 0 < opened["ttl_seconds"] <= 300):
            raise ValueError("bounded owned browser start and artifact receipt required")
        original = observer.snapshot(attempt_id=attempt_id)
        if (original.get("source") != "independent-browser-provider" or
                original.get("provider_id") != opened["provider_id"] or
                any(type(original.get(name)) is not int or original[name] < 0 for name in COUNTERS)):
            raise ValueError("independent browser provider identity or effect counters unavailable")
        chunks = []
        for offset in range(0, opened["artifact_size"], 8192):
            chunk = driver.read_artifact(opened, offset=offset, length=8192, attempt_id=attempt_id)
            data = base64.b64decode(chunk["data_base64"], validate=True)
            if (chunk.get("sha256") != opened["artifact_sha256"] or
                    chunk.get("total_length") != opened["artifact_size"] or
                    chunk.get("offset") != offset or chunk.get("length") != len(data) or
                    len(data) != min(8192, opened["artifact_size"] - offset)):
                raise ValueError("browser artifact page differs from owned receipt")
            chunks.append(data)
        content = b"".join(chunks)
        if hashlib.sha256(content).hexdigest() != opened["artifact_sha256"]:
            raise ValueError("browser artifact bytes differ from owned hash")
        packet = json.loads(content)
        if (packet.get("view_id") != opened["view_id"] or
                packet.get("manifest", {}).get("session_id") != opened["provider_id"] or
                not isinstance(packet.get("observations"), list) or not packet["observations"]):
            raise ValueError("browser artifact does not describe the observed session")
        evidence["artifact_sha256"] = opened["artifact_sha256"]
        lost = driver.lose_reply(opened, attempt_id=attempt_id)
        if (lost.get("status") != "unknown" or not isinstance(lost.get("operation_ref"), str) or
                str(UUID(lost["operation_ref"])) != lost["operation_ref"]):
            raise ValueError("browser reply loss was not recorded as uncertain")
        after_loss = observer.snapshot(attempt_id=attempt_id)
        if (after_loss.get("source") != original["source"] or
                after_loss.get("provider_id") != opened["provider_id"] or
                any(type(after_loss.get(name)) is not int or
                    after_loss[name] < original[name] or
                    after_loss[name] > original[name] + 1 for name in COUNTERS)):
            raise ValueError("browser action effects cannot be reconciled")
        recovered = driver.recover_original(lost["operation_ref"], attempt_id=attempt_id)
        if (recovered.get("operation_ref") != lost["operation_ref"] or
                recovered.get("status") not in {"confirmed", "unknown"} or
                observer.snapshot(attempt_id=attempt_id) != after_loss):
            raise ValueError("browser recovery replayed or changed the original action")
        evidence["operation_ref"] = lost["operation_ref"]
        evidence["provider_actions"] = after_loss["browser_actions"] - original["browser_actions"]
        evidence["status"] = "pending-cleanup"
    except (ValueError, binascii.Error, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            closed = driver.close_owned(attempt_id=attempt_id)
            terminal = observer.termination(attempt_id=attempt_id)
            confirmed = (opened is not None and closed.get("status") == "confirmed" and
                         closed.get("termination_confirmed") is True and
                         terminal.get("source") == "independent-browser-provider" and
                         terminal.get("provider_id") == opened["provider_id"] and
                         terminal.get("provider_status") == "TERMINATED" and
                         type(terminal.get("elapsed_seconds")) is int and
                         0 <= terminal["elapsed_seconds"] <= 600)
            evidence["cleanup"] = {"status": "confirmed" if confirmed else "unknown",
                                   "provider_termination": terminal.get("provider_status")}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown", "provider_termination": None}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def browser_ac04(driver, observer, client_name, revision):
    if (driver is None or observer is None or getattr(driver, "client_name", None) != client_name or
            not all(getattr(driver, name, False) is True for name in
                    ("isolated", "authorized", "inert_https"))):
        raise FixtureUnavailable("isolated installed-client browser fixture and independent observer required")
    fixture = getattr(driver, "fixture", None)
    if (not isinstance(fixture, dict) or
            not all(isinstance(fixture.get(field), str) and fixture[field] for field in
                    ("url", "title", "link_text", "destination_url", "destination_title", "marker")) or
            not fixture["url"].startswith("https://") or
            not fixture["destination_url"].startswith("https://") or
            fixture.get("form_inert") is not True):
        raise FixtureUnavailable("run-owned inert HTTPS page, link and form required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "status": "blocked",
                "cleanup": {"status": "not-run", "provider_termination": None}}
    opened = None
    try:
        opened = driver.start_owned(attempt_id=attempt_id)
        if (opened.get("status") != "confirmed" or opened.get("client_name") != client_name or
                opened.get("source_revision") != revision or
                not isinstance(opened.get("client_version"), str) or not opened["client_version"] or
                not all(isinstance(opened.get(field), str) and opened[field] for field in
                        ("session_id", "view_id", "provider_id")) or
                type(opened.get("ttl_seconds")) is not int or not 0 < opened["ttl_seconds"] <= 300):
            raise ValueError("installed-client start or bounded provider identity unavailable")
        first = observer.snapshot(attempt_id=attempt_id)
        inspected = driver.inspect_owned(opened, attempt_id=attempt_id)
        before = observer.snapshot(attempt_id=attempt_id)
        choices = inspected.get("choices", [])
        links = [choice for choice in choices if isinstance(choice, dict) and
                 choice.get("text") == fixture["link_text"] and
                 choice.get("url") == fixture["destination_url"] and
                 choice.get("action") == "follow" and
                 isinstance(choice.get("id"), str) and choice["id"]]
        if (inspected.get("status") != "confirmed" or
                inspected.get("session_id") != opened["session_id"] or
                inspected.get("view_id") != opened["view_id"] or len(links) != 1 or
                any(first.get(field) != before.get(field) for field in
                    ("source", "provider_id", "url", "page_title", "visible_text", *COUNTERS)) or
                before.get("source") != "independent-browser-provider" or
                before.get("provider_id") != opened["provider_id"] or
                before.get("url") != fixture["url"] or before.get("page_title") != fixture["title"] or
                any(type(before.get(name)) is not int or before[name] < 0 for name in COUNTERS) or
                before["mutations"] != 0):
            raise ValueError("read-only inspection did not independently observe the inert page")
        followed = driver.follow_owned(inspected, candidate_id=links[0]["id"],
                                       attempt_id=attempt_id)
        navigated = observer.snapshot(attempt_id=attempt_id)
        final = driver.inspect_owned(followed, attempt_id=attempt_id)
        after = observer.snapshot(attempt_id=attempt_id)
        if (followed.get("status") != "confirmed" or
                followed.get("session_id") != opened["session_id"] or
                not isinstance(followed.get("view_id"), str) or
                followed["view_id"] == opened["view_id"] or
                final.get("status") != "confirmed" or final.get("view_id") != followed["view_id"] or
                final.get("session_id") != opened["session_id"] or
                any(navigated.get(field) != after.get(field) for field in
                    ("source", "provider_id", "url", "page_title", "visible_text", *COUNTERS)) or
                after.get("source") != before["source"] or
                after.get("provider_id") != opened["provider_id"] or
                after.get("url") != fixture["destination_url"] or
                after.get("page_title") != fixture["destination_title"] or
                fixture["marker"] not in after.get("visible_text", "") or
                after.get("browser_actions") != before["browser_actions"] + 1 or
                after.get("network_requests", 0) <= before["network_requests"] or
                after.get("mutations") != 0):
            raise ValueError("client navigation or independently observed changed view unavailable")
        artifact = final.get("artifact", {})
        size = artifact.get("byte_length")
        digest = artifact.get("content_sha256")
        if (type(size) is not int or not 0 < size <= 128000 or
                not isinstance(digest, str) or len(digest) != 64):
            raise ValueError("bounded inspection artifact unavailable")
        content = bytearray()
        for offset in range(0, size, 8192):
            chunk = driver.read_artifact(final, offset=offset, length=8192, attempt_id=attempt_id)
            data = base64.b64decode(chunk["data_base64"], validate=True)
            if (chunk.get("sha256") != digest or chunk.get("offset") != offset or
                    chunk.get("total_length") != size or chunk.get("length") != len(data) or
                    len(data) != min(8192, size - offset)):
                raise ValueError("inspection artifact page differs from receipt")
            content.extend(data)
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("inspection artifact bytes differ from receipt")
        evidence.update({"client_version": opened["client_version"], "provider_id": opened["provider_id"],
                         "artifact_sha256": digest, "view_ids": [opened["view_id"], followed["view_id"]],
                         "status": "pending-cleanup"})
    except (ValueError, binascii.Error, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            closed = driver.close_owned(attempt_id=attempt_id)
            terminal = observer.termination(attempt_id=attempt_id)
            confirmed = (opened is not None and closed.get("status") == "confirmed" and
                         closed.get("termination_confirmed") is True and
                         terminal.get("source") == "independent-browser-provider" and
                         terminal.get("provider_id") == opened.get("provider_id") and
                         terminal.get("provider_status") == "TERMINATED" and
                         type(terminal.get("elapsed_seconds")) is int and
                         0 <= terminal["elapsed_seconds"] <= 600)
            evidence["cleanup"] = {"status": "confirmed" if confirmed else "unknown",
                                   "provider_termination": terminal.get("provider_status")}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown", "provider_termination": None}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def browser_ac01(driver, observer, revision):
    return browser_ac04(driver, observer, "cli", revision)
