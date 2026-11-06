"""Qualified, isolated cleanup checks; never report these as deployed acceptance."""

import hashlib
from uuid import uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable


def cleanup_ac01(driver, observer, tool):
    if (driver is None or observer is None or tool not in {"search", "interpreter", "browser"}
            or getattr(driver, "isolated", False) is not True):
        raise FixtureUnavailable("isolated cleanup driver and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "status": "blocked", "cleanup": {"status": "not-run"},
                "evidence_scope": "repository-integration"}
    try:
        receipt = driver.close(tool, attempt_id=attempt_id)
        execution_id = receipt["execution_id"]
        evidence["execution_id"] = execution_id
        observed = observer.readback(tool, execution_id, attempt_id=attempt_id)
        evidence["observations"] = observed
        expected_provider = "not-required" if tool == "search" else "TERMINATED"
        if (receipt.get("status_code") != 200 or receipt.get("cleanup_status") != "confirmed"
                or observed.get("source") != "isolated-provider-and-ledger-readback"
                or observed.get("execution_state") != "closed"
                or observed.get("cleanup_status") != "confirmed"
                or observed.get("provider_state") != expected_provider
                or observed.get("dispatch_count") != 1
                or (tool == "search" and (observed.get("accepted_operations") != 1 or
                                          observed.get("unresolved_operations") != 0))
                or (tool != "search" and observed.get("stop_count") != 1)):
            raise ValueError("independent cleanup evidence does not confirm closure")
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(
                tool, attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def cleanup_ac02(driver, observer, tool):
    if (driver is None or observer is None or tool not in {"search", "interpreter", "browser"}
            or getattr(driver, "isolated", False) is not True):
        raise FixtureUnavailable("isolated scheduled-cleanup driver and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "status": "blocked", "cleanup": {"status": "not-run"},
                "evidence_scope": "repository-integration"}
    try:
        execution_id = driver.prepare(tool, attempt_id=attempt_id)
        evidence["execution_id"] = execution_id
        before = observer.readback(tool, execution_id, attempt_id=attempt_id)
        driver.first_pass(tool, attempt_id=attempt_id)
        pending = observer.readback(tool, execution_id, attempt_id=attempt_id)
        driver.restart(tool, attempt_id=attempt_id)
        recovered = observer.readback(tool, execution_id, attempt_id=attempt_id)
        evidence["observations"] = {"before": before, "pending": pending, "recovered": recovered}
        expected_provider = "not-required" if tool == "search" else "TERMINATED"
        if (any(row.get("source") != "isolated-provider-and-ledger-readback" for row in
                (before, pending, recovered)) or before.get("dispatch_count") != 1 or
                before.get("execution_state") != ("active" if tool == "browser" else "closing")):
            raise ValueError("initial independent execution evidence unavailable")
        if (pending.get("cleanup_status") != "unknown" if tool != "search" else
                pending.get("cleanup_status") != "confirmed"):
            raise ValueError("scheduled cleanup did not preserve uncertainty or settle search")
        if (tool != "search" and (pending.get("execution_state") != "closing" or
                pending.get("provider_state") != "READY" or pending.get("stop_count") != 0 or
                pending.get("next_cleanup_at") is None or pending.get("cleanup_last_outcome") not in
                {None, "unknown", "provider_error"})):
            raise ValueError("interrupted cleanup was not durably pending")
        if (recovered.get("execution_state") != "closed" or
                recovered.get("cleanup_status") != "confirmed" or
                recovered.get("provider_state") != expected_provider or
                recovered.get("dispatch_count") != 1 or
                recovered.get("cleanup_evidence") != ("all_operations_settled" if tool == "search" else
                                                       "provider_terminated") or
                recovered.get("next_cleanup_at") is not None or
                recovered.get("cleanup_partition") is not None or
                recovered.get("cleanup_attempts") != (2 if tool == "interpreter" else 1) or
                (tool != "search" and recovered.get("stop_count") != 1) or
                (tool == "search" and (recovered.get("accepted_operations") != 1 or
                                       recovered.get("unresolved_operations") != 0))):
            raise ValueError("restarted cleanup lacks independent terminal proof")
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(
                tool, attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def cleanup_ac03(driver, observer, tool):
    if (driver is None or observer is None or tool != "search" or
            getattr(driver, "isolated", False) is not True):
        raise FixtureUnavailable("isolated owned-artifact fixture and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "status": "blocked", "cleanup": {"status": "not-run"},
                "evidence_scope": "repository-integration"}
    try:
        receipt = driver.prepare(attempt_id=attempt_id)
        execution_id = receipt["execution_id"]
        evidence["execution_id"] = execution_id
        before = observer.readback(execution_id, attempt_id=attempt_id)
        driver.revoke(attempt_id=attempt_id)
        refusals = driver.foreign_access(attempt_id=attempt_id)
        original, original_bytes = driver.read_owned(attempt_id=attempt_id)
        close = driver.close_owned(attempt_id=attempt_id)
        recovered, recovered_bytes = driver.read_owned(attempt_id=attempt_id)
        after = observer.readback(execution_id, attempt_id=attempt_id)
        evidence["observations"] = {"before": before, "after": after, "foreign": refusals,
                                    "artifact_sha256": hashlib.sha256(recovered_bytes).hexdigest(),
                                    "artifact_length": len(recovered_bytes)}
        if (before.get("source") != "independent-isolated-ledger-and-provider-readback" or
                after.get("source") != before["source"] or
                before.get("state") != "active" or after.get("state") != "closed" or
                after.get("cleanup_status") != "confirmed" or
                after.get("cleanup_evidence") != "all_operations_settled" or
                after.get("cleanup_partition") is not None or
                (before.get("tenant"), before.get("owner"), before.get("binding")) !=
                (after.get("tenant"), after.get("owner"), after.get("binding")) or
                after.get("binding") != "run:fixture-run:fixture-worker" or
                before.get("provider_calls") != 1 or after.get("provider_calls") != 1 or
                original != receipt or recovered != receipt or
                close.get("cleanup_status") != "confirmed" or
                original_bytes != recovered_bytes or len(recovered_bytes) <= 8192 or
                hashlib.sha256(recovered_bytes).hexdigest() != after.get("sha256") or
                len(recovered_bytes) != after.get("total_length") or
                set(refusals) != {"user", "tenant", "run"} or
                any(value != {"operation": 404, "artifact": 404, "close": 404}
                    for value in refusals.values())):
            raise ValueError("independent ownership and artifact evidence does not confirm recovery")
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(
                attempt_id=attempt_id) is True else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def cleanup_ac04(driver, observer, tool):
    if (driver is None or observer is None or tool != "search" or
            getattr(driver, "isolated", False) is not True):
        raise FixtureUnavailable("isolated five-helper driver and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "status": "blocked", "cleanup": {"status": "not-run"},
                "evidence_scope": "repository-integration"}
    try:
        reference, execution_id = driver.start_lost_reply(attempt_id=attempt_id)
        evidence["execution_id"] = execution_id
        before = observer.readback(execution_id, reference=reference)
        result = driver.recover_original(attempt_id=attempt_id)
        after = observer.readback(execution_id, reference=reference)
        evidence["observations"] = {"before": before, "after": after, "artifact_sha256": result["sha256"],
                                    "artifact_length": result["length"], "operation_ref": reference}
        if (before.get("source") != "independent-isolated-ledger-and-provider-readback" or
                before.get("dispatch_count") != 1 or before.get("execution_state") != "active" or
                result.get("operation_ref") != reference or result.get("listed") is not True or
                result.get("pending_status") != "unknown" or result.get("recovered_status") != "confirmed" or
                result.get("post_restart_status") != "confirmed" or result.get("close_status") != "confirmed" or
                not 8192 < result["length"] <= 262144 or result["sha256"] != after.get("sha256") or
                result["length"] != after.get("byte_length") or
                after.get("source") != before["source"] or after.get("dispatch_count") != 1 or
                after.get("execution_state") != "closed" or after.get("cleanup_status") != "confirmed" or
                after.get("cleanup_evidence") != "all_operations_settled" or
                after.get("accepted_operations") != 1 or after.get("unresolved_operations") != 0):
            raise ValueError("independent five-helper recovery evidence is incomplete")
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(
                execution_id=evidence.get("execution_id")) is True else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence
