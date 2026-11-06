"""Isolated local grant-and-consent scenario using supported gateway interfaces."""

from uuid import uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable, canonical_uuid

CORE_TOOLS = ("browser.start", "code_interpreter.start", "websearch.search")
STEPS = ("grant_tenant", "grant_team", "allow", "consent", "discover", "call")


def access_ac01(driver, observer):
    if driver is None or observer is None or not all(
        getattr(driver, name, False) is True for name in ("isolated", "authorized", "fresh_user", "ready_targets")
    ):
        raise FixtureUnavailable("isolated fresh user, READY targets and independent observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "audit_operation_ids": [], "discovered_tools": [],
                "provider_call_count": 0, "cleanup": {"status": "not-run"}, "status": "blocked"}
    try:
        initial = driver.read_policy()
        if initial.get("allowed_tools") != [] or initial.get("consented_tools") != []:
            raise ValueError("fresh user already has tool authority")
        for action in STEPS:
            result = driver.step(action, attempt_id=attempt_id)
            if result.get("status") != 200:
                raise ValueError("supported provisioning or invocation failed")
            if action in {"grant_tenant", "grant_team", "allow", "consent"}:
                evidence["audit_operation_ids"].extend(canonical_uuid(receipt) for receipt in result["audit_operation_ids"])
            if action == "allow" and result.get("consented_tools") != []:
                raise ValueError("administrator grant implied user consent")
            if action == "discover":
                evidence["discovered_tools"] = sorted(result["tools"])
                if evidence["discovered_tools"] != sorted(CORE_TOOLS):
                    raise ValueError("core targets were not discoverable after consent")
            if action == "call":
                evidence["provider_call_count"] = result.get("provider_call_count")
                if evidence["provider_call_count"] != len(CORE_TOOLS):
                    raise ValueError("core targets were not authorized")
        observed = observer.observe(attempt_id=attempt_id)
        if (observed.get("source") != "independent-gateway-readback"
                or observed.get("allowed_tools") != list(CORE_TOOLS)
                or observed.get("consented_tools") != list(CORE_TOOLS)
                or observed.get("tenant_grants") != list(CORE_TOOLS)
                or observed.get("team_grants") != list(CORE_TOOLS)
                or observed.get("provider_call_count") != len(CORE_TOOLS)
                or set(observed.get("audit_operation_ids", [])) != set(evidence["audit_operation_ids"])):
            raise ValueError("independent access or audit assertions failed")
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(attempt_id=attempt_id) is True
                                   else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


DENIALS = {
    "not_consented": ("not_consented", "tool_not_consented", (), CORE_TOOLS, CORE_TOOLS, ()),
    "team_disabled": ("team_disabled", "tool_not_entitled", CORE_TOOLS[1:], CORE_TOOLS,
                      CORE_TOOLS[1:], CORE_TOOLS),
    "tenant_disabled": ("tenant_disabled", "tool_not_entitled", (CORE_TOOLS[2],),
                        (CORE_TOOLS[0], CORE_TOOLS[2]), CORE_TOOLS[1:], CORE_TOOLS),
    "other_tenant": ("tenant_boundary", "unknown_run", (),
                     (CORE_TOOLS[0], CORE_TOOLS[2]), CORE_TOOLS[1:], CORE_TOOLS),
}


def access_ac02(driver, observer):
    if driver is None or observer is None or not all(
        getattr(driver, name, False) is True for name in ("isolated", "authorized", "fresh_user", "ready_targets")
    ):
        raise FixtureUnavailable("isolated scoped user and independent provider observer required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "audit_operation_ids": [], "denials": {},
                "provider_call_count": 0, "cleanup": {"status": "not-run"}, "status": "blocked"}
    try:
        if driver.read_policy() != {"allowed_tools": [], "consented_tools": []}:
            raise ValueError("fixture is not fresh")
        for action in ("grant_tenant", "grant_team", "allow"):
            result = driver.step(action, attempt_id=attempt_id)
            if result.get("status") != 200 or action == "allow" and result.get("consented_tools") != []:
                raise ValueError("administrator grant failed or implied consent")
            evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in result["audit_operation_ids"])
        for case, (reason, call_reason, listed, tenant_grants, team_grants, consented) in DENIALS.items():
            if case == "team_disabled":
                consent = driver.step("consent", attempt_id=attempt_id)
                if consent.get("status") != 200:
                    raise ValueError("separate owner consent failed")
                evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in consent["audit_operation_ids"])
            result = driver.deny_case(case, attempt_id=attempt_id)
            evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in result.get("audit_operation_ids", []))
            observed = observer.observe_denial(attempt_id=attempt_id, foreign_tenant=result.get("foreign_tenant"))
            if (result.get("status") != 200 or result.get("reason") != reason
                    or result.get("diagnostic_status") != (403 if case == "other_tenant" else 200)
                    or result.get("list_status") != (404 if case == "other_tenant" else 200)
                    or result.get("listed_tools") != list(listed)
                    or result.get("call_status") != (404 if case == "other_tenant" else 403)
                    or result.get("call_reason") != call_reason
                    or result.get("provider_call_count") != 0
                    or observed.get("source") != "independent-gateway-readback"
                    or observed.get("provider_call_count") != 0 or observed.get("activity_call_count") != 0
                    or observed.get("allowed_tools") != list(CORE_TOOLS)
                    or observed.get("consented_tools") != list(consented)
                    or observed.get("tenant_grants") != list(tenant_grants)
                    or observed.get("team_grants") != list(team_grants)
                    or set(observed.get("audit_operation_ids", [])) != set(evidence["audit_operation_ids"])
                    or observed.get("foreign_owner_exists", False) or observed.get("foreign_policy_exists", False)):
                raise ValueError("denial or independent readback differs: " + case)
            evidence["denials"][case] = {"reason": reason, "call_status": result["call_status"],
                                         "discovered_tools": list(listed)}
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(attempt_id=attempt_id) is True
                                   else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence


def access_ac03(driver, observer):
    if driver is None or observer is None or not all(
        getattr(driver, name, False) is True for name in ("isolated", "authorized", "fresh_user", "ready_targets")
    ):
        raise FixtureUnavailable("isolated revisioned policy and independent oracle required")
    attempt_id = str(uuid4())
    evidence = {"attempt_id": attempt_id, "audit_operation_ids": [], "conflicts": [],
                "provider_call_count": 0, "cleanup": {"status": "not-run"}, "status": "blocked"}
    try:
        if driver.read_policy() != {"allowed_tools": [], "consented_tools": []}:
            raise ValueError("fixture is not fresh")
        for action in ("grant_tenant", "grant_team", "allow"):
            result = driver.step(action, attempt_id=attempt_id)
            if result.get("status") != 200 or action == "allow" and result.get("consented_tools") != []:
                raise ValueError("initial grant failed or implied consent")
            evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in result["audit_operation_ids"])
        for action in ("stale_policy", "stale_entitlement"):
            if driver.reconcile_action(action, attempt_id=attempt_id).get("status") != 409:
                raise ValueError("stale administrator write was accepted")
            evidence["conflicts"].append(action)
        observed = observer.observe_reconciliation(attempt_id=attempt_id)
        if (observed.get("policy_revision") != 1 or observed.get("allowed_tools") != list(CORE_TOOLS)
                or observed.get("consented_tools") != []
                or observed.get("tenant_grants") != list(CORE_TOOLS)
                or observed.get("team_grants") != list(CORE_TOOLS)):
            raise ValueError("stale administrator write lost grants or created consent")
        advanced = driver.reconcile_action("advance_policy", attempt_id=attempt_id)
        if advanced.get("status") != 200 or advanced.get("revision") != 2 or advanced.get("consented_tools") != []:
            raise ValueError("revisioned administrator update failed")
        evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in advanced["audit_operation_ids"])
        if driver.reconcile_action("stale_consent", attempt_id=attempt_id).get("status") != 409:
            raise ValueError("stale user consent was accepted")
        evidence["conflicts"].append("stale_consent")
        if observer.observe_reconciliation(attempt_id=attempt_id).get("consented_tools") != []:
            raise ValueError("conflicted consent changed policy")
        consent = driver.step("consent", attempt_id=attempt_id)
        if consent.get("status") != 200:
            raise ValueError("current owner consent failed")
        evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in consent["audit_operation_ids"])
        called = driver.reconcile_action("call_before_removal", attempt_id=attempt_id)
        if (called.get("status") != 200 or called.get("provider_call_count") != 1
                or not all(canonical_uuid(called.get("execution", {}).get(name))
                           for name in ("request_id", "execution_id"))):
            raise ValueError("target was not callable before removal")
        evidence["execution_id"] = called["execution"]["execution_id"]
        removed = driver.reconcile_action("remove_target", attempt_id=attempt_id, execution=called["execution"])
        if (removed.get("status") != 403 or removed.get("call_reason") != "tool_undeployed"
                or removed.get("diagnostic_reason") != "undeployed"
                or removed.get("discovered_tools") != list(CORE_TOOLS[1:])
                or removed.get("provider_call_count") != 1):
            raise ValueError("removed target remained callable")
        previous = observer.observe_reconciliation(attempt_id=attempt_id)
        if (previous.get("allowed_tools") != list(CORE_TOOLS)
                or previous.get("consented_tools") != list(CORE_TOOLS)
                or previous.get("catalog_tools") != list(CORE_TOOLS)
                or previous.get("deployed_tools") != list(CORE_TOOLS[1:])):
            raise ValueError("removed target lacked stale-policy fixture")
        reconciled = driver.reconcile_action("reconcile", attempt_id=attempt_id)
        if (reconciled.get("status") != 200 or reconciled.get("revision") != 4
                or reconciled.get("consented_tools") != list(CORE_TOOLS[1:])):
            raise ValueError("target reconciliation retained stale consent")
        evidence["audit_operation_ids"].extend(canonical_uuid(item) for item in reconciled["audit_operation_ids"])
        if driver.reconcile_action("stale_reconcile", attempt_id=attempt_id).get("status") != 409:
            raise ValueError("stale reconciliation overwrote current policy")
        evidence["conflicts"].append("stale_reconcile")
        again = driver.reconcile_action("check_removed", attempt_id=attempt_id, execution=called["execution"])
        observed = observer.observe_reconciliation(attempt_id=attempt_id)
        if (again.get("status") != 403 or again.get("call_reason") != "tool_undeployed"
                or again.get("provider_call_count") != 1
                or observed.get("source") != "independent-gateway-readback"
                or observed.get("policy_revision") != 4
                or observed.get("allowed_tools") != list(CORE_TOOLS[1:])
                or observed.get("consented_tools") != list(CORE_TOOLS[1:])
                or observed.get("catalog_tools") != list(CORE_TOOLS[1:])
                or observed.get("tenant_grants") != list(CORE_TOOLS)
                or observed.get("team_grants") != list(CORE_TOOLS)
                or observed.get("provider_call_count") != 1 or observed.get("activity_call_count") != 1
                or set(observed.get("audit_operation_ids", [])) != set(evidence["audit_operation_ids"])):
            raise ValueError("reconciled target or independent readback differs")
        evidence["provider_call_count"] = 1
        evidence["removed_target"] = CORE_TOOLS[0]
        evidence["status"] = "pending-cleanup"
    except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError) as exc:
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            driver.cleanup_owned(attempt_id=attempt_id)
            evidence["cleanup"] = {"status": "confirmed" if observer.verify_cleanup(attempt_id=attempt_id) is True
                                   else "unknown"}
        except (ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError):
            evidence["cleanup"] = {"status": "unknown"}
    if evidence["status"] == "pending-cleanup":
        evidence["status"] = "pass" if evidence["cleanup"]["status"] == "confirmed" else "blocked"
    return evidence
