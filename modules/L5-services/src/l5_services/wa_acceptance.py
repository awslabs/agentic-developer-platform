"""Reconcile private WA evidence without copying operational values into public output."""

import re
from decimal import Decimal


STATUSES = frozenset({"pass", "fail", "blocked", "not_run"})
PHASES = frozenset({"before_review", "installed", "handoff"})
COST_BUCKETS = frozenset({"adp_accounting", "provider_charges", "ancillary_charges", "unattributed"})
OWNER_ALIASES = frozenset({*(f"WA{index:02}" for index in range(1, 8)),
                           "acceptance-owner", "platform-credential-owner", "client-owners"})


def expected_criteria(story_specs):
    if (not isinstance(story_specs, dict) or story_specs.get("epic") != 9078
            or not isinstance(story_specs.get("stories"), list)):
        raise ValueError("WA story contract unavailable")
    expected = {f"EC-{index:02}": set() for index in range(1, 9)}
    if {story.get("alias") for story in story_specs["stories"]} != {
            f"WA{index:02}" for index in range(1, 8)} or len(story_specs["stories"]) != 7:
        raise ValueError("WA acceptance mapping incomplete")
    for story in story_specs["stories"]:
        alias = story["alias"]
        for index, check in enumerate(story["checks"], 1):
            if (check["id"] != f"AC-{index:02}" or not check["ec"]
                    or not set(check["ec"]) <= expected.keys()):
                raise ValueError("WA acceptance mapping invalid")
            for criterion in (f"{alias}/{check['id']}", f"{alias}/SCN-{index:02}"):
                for epic in check["ec"]:
                    expected[epic].add(criterion)
    if sum(len(story["checks"]) for story in story_specs["stories"]) != 26:
        raise ValueError("WA acceptance mapping incomplete")
    return set(expected) | {criterion for children in expected.values() for criterion in children}, expected


def validate_ledger(ledger, story_specs):
    criteria, mapped = expected_criteria(story_specs)
    if (not isinstance(ledger, dict) or ledger.get("schema_version") != 1
            or not isinstance(ledger.get("candidate_revision"), str)
            or re.fullmatch(r"[0-9a-f]{40}", ledger["candidate_revision"]) is None
            or ledger.get("installed_revision") is not None and (
                not isinstance(ledger["installed_revision"], str)
                or re.fullmatch(r"[0-9a-f]{40}", ledger["installed_revision"]) is None
                or ledger["installed_revision"] != ledger["candidate_revision"]
                or not isinstance(ledger.get("installed_readback_ref"), str)
                or not ledger["installed_readback_ref"])):
        raise ValueError("candidate and installed code revisions required")
    rows = ledger.get("rows")
    if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
            or len(rows) != len(criteria)
            or {row.get("criterion") for row in rows} != criteria):
        raise ValueError("every epic, story and scenario row must be present once")
    by_criterion = {row["criterion"]: row for row in rows}
    for row in rows:
        status = row.get("status")
        revision = row.get("tested_revision")
        evidence = row.get("evidence_ref")
        owner = row.get("owner_alias")
        if (status not in STATUSES or row.get("phase") not in PHASES
                or owner not in OWNER_ALIASES
                or (revision is not None and (not isinstance(revision, str)
                    or re.fullmatch(r"[0-9a-f]{40}", revision) is None))
                or (status in {"pass", "fail"} and (revision is None
                    or not isinstance(evidence, str) or not evidence))
                or (status == "pass" and revision != ledger["candidate_revision"])
                or (status == "not_run" and (revision is not None or evidence is not None))
                or (status == "pass" and row.get("blocker_code") is not None)
                or (status == "not_run" and row.get("blocker_code") != "not_executed")
                or (status == "blocked" and row.get("blocker_code") == "not_executed")
                or (status != "pass" and row.get("blocker_code") not in {
                    "not_executed", "selected_role_unverified", "target_unverified",
                    "fixture_unavailable", "dependency_unverified", "test_failed",
                    "consumer_unverified", "cleanup_unverified", "other_verified_blocker"})):
            raise ValueError("acceptance row has incomplete phase, revision or evidence")
        criterion = row["criterion"]
        if status == "pass" and criterion.startswith("WA07/"):
            required_phase = "handoff" if criterion in {"WA07/AC-05", "WA07/SCN-05"} else "installed"
            if row["phase"] != required_phase or revision != ledger["installed_revision"]:
                raise ValueError("WA07 live pass requires matching installed revision and phase")
    for epic, children in mapped.items():
        if by_criterion[epic]["status"] == "pass" and any(
                by_criterion[criterion]["status"] != "pass" for criterion in children):
            raise ValueError("epic cannot pass while mapped story evidence is incomplete")
    if by_criterion["WA07/AC-05"]["status"] == "pass" and any(
            row["status"] != "pass" for row in rows if row["criterion"] != "WA07/AC-05"):
        raise ValueError("WA07 handoff cannot pass incomplete epic or scenario evidence")
    costs = ledger.get("costs")
    if (not isinstance(costs, dict) or set(costs) != COST_BUCKETS
            or ledger.get("allowance_renewed") is not False):
        raise ValueError("cost buckets and unchanged allowance required")
    for cost in costs.values():
        if not isinstance(cost, dict) or cost.get("state") not in {"known", "unknown"}:
            raise ValueError("cost state required")
        if cost["state"] == "unknown":
            if (cost.get("amount_usd") is not None or not isinstance(cost.get("reason"), str)
                    or not cost["reason"]):
                raise ValueError("unknown cost cannot be recorded as zero")
        elif (not isinstance(cost.get("amount_usd"), str)
              or re.fullmatch(r"(?:0|[1-9][0-9]{0,9})\.[0-9]{2}", cost["amount_usd"]) is None
              or Decimal(cost["amount_usd"]) < 0 or not cost.get("evidence_ref")):
            raise ValueError("known cost requires a priced receipt")
    resources = ledger.get("resources")
    if not isinstance(resources, list) or not all(isinstance(resource, dict) for resource in resources):
        raise ValueError("resource disposition ledger required")
    if len({resource.get("private_ref") for resource in resources}) != len(resources):
        raise ValueError("resource disposition duplicated")
    for resource in resources:
        if (not isinstance(resource.get("private_ref"), str) or not resource["private_ref"]
                or resource.get("ownership") not in {"owned", "adopted", "unknown"}
                or resource.get("state") not in {"retained", "removed", "unknown"}
                or resource.get("owner_alias") not in OWNER_ALIASES
                or not isinstance(resource.get("evidence_ref"), str) or not resource["evidence_ref"]
                or (resource["state"] == "removed" and (
                    resource["ownership"] != "owned" or not resource.get("terminal_receipt_ref")
                    or not resource.get("absence_readback_ref") or not resource.get("result_retained_ref")))):
            raise ValueError("owned removal needs terminal, absence and retained-result proof")
    if by_criterion["WA07/AC-05"]["status"] == "pass" and any(
            resource["state"] == "unknown" for resource in resources):
        raise ValueError("unknown resource disposition blocks live handoff")
    return by_criterion


def sanitized_summary(ledger, story_specs):
    rows = validate_ledger(ledger, story_specs)
    incomplete = [row for row in rows.values() if row["status"] != "pass"]
    return {"issue": 9105, "acceptance": "incomplete" if incomplete else "ready_for_owner_acceptance",
            "installed_revision_recorded": ledger["installed_revision"] is not None,
            "cost_states": {name: ledger["costs"][name]["state"] for name in sorted(COST_BUCKETS)},
            "resource_states": {state: sum(resource["state"] == state for resource in ledger["resources"])
                                for state in ("retained", "removed", "unknown")},
            "remaining": [{key: row[key] for key in ("criterion", "status", "phase", "owner_alias", "blocker_code")}
                          for row in sorted(incomplete, key=lambda item: item["criterion"])]}
