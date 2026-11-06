"""A denied grant is cleaned through real journal and claim transactions."""

import copy
import json

import pytest

from superplane_bootstrap.denied_grant import KIND, digest, recover_denied_grant
from superplane_bootstrap.errors import BootstrapRefused

from .test_authority_journal_postgres import authority  # noqa: F401
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401


@pytest.fixture
def denied(authority):  # noqa: F811
    journal, registry, reservation = authority
    plan = {
        "version": 1,
        "grants": [
            {
                "key": key,
                "kind": kind,
                "generation": journal.generation,
                "lifetime": "temporary",
            }
            for key, kind in (
                ("registrar-entry", "eks-entry"),
                ("registrar-policy", "eks-policy"),
            )
        ],
    }
    journal.begin(plan)
    with journal.fenced():
        journal.write_locked(
            {
                "phase": "revoking",
                "registrar-entry": {"phase": "revoked", "identity": None},
            }
        )
    evidence = {
        "evidence_kind": KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "denied_request_verified": True,
        "operation_id": journal.binding.operation_id,
        "org_id": journal.target.org_id,
        "workspace_id": journal.target.workspace_id,
        "cluster_arn": journal.target.cluster_arn,
        "generation": journal.generation,
        "claim": journal.claim,
        "plan_sha256": digest(plan),
    }
    return journal, registry, plan, evidence


def rows(journal):
    return journal.store.execute(
        "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
        journal.key,
    )


def test_native_denied_cleanup_retains_exact_plan_and_releases_only_claim(denied):
    journal, registry, plan, evidence = denied
    calls = []
    result = recover_denied_grant(
        journal,
        registry,
        revalidate=lambda: evidence,
        verify_absence=lambda value: calls.append(value) is None,
    )
    assert result["revoked"] and result["claim_released"] and result["journal_retained"]
    assert calls == [plan, plan, plan]
    retained = rows(journal)
    assert len(retained) == 1 and retained[0]["revoked"]
    assert json.loads(retained[0]["plan_json"]) == plan
    assert json.loads(retained[0]["progress_json"]) == {
        "phase": "revoked",
        "complete": True,
        "registrar-entry": {"phase": "revoked", "identity": None},
    }
    assert not journal.store.execute(
        "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
        journal.key,
    )
    with pytest.raises(BootstrapRefused, match="stale"):
        recover_denied_grant(
            journal,
            registry,
            revalidate=lambda: evidence,
            verify_absence=lambda _: True,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "second-intent",
        "components",
        "active",
        "retained",
        "claim",
        "generation",
        "operation_id",
        "org_id",
        "workspace_id",
        "cluster_arn",
        "plan_sha256",
        "evidence_kind",
        "original_authority_expired",
        "issued_sessions_expired",
        "denied_request_verified",
        "absence",
        "resolver-changed",
    ],
)
def test_uncertain_or_changed_denial_keeps_journal_and_reservation(denied, fault):
    journal, registry, _, evidence = denied
    if fault in {"identity", "second-intent", "components", "active", "retained"}:
        with journal.fenced():
            _, progress = journal.read_locked()
            if fault == "identity":
                progress["registrar-entry"]["identity"] = {"arn": "observed"}
            elif fault == "second-intent":
                progress["registrar-policy"] = {"phase": "grant_intent"}
            elif fault == "components":
                progress["components"] = {}
            elif fault == "active":
                progress["phase"] = "active"
            else:
                progress["retain_workspace"] = True
            journal.write_locked(progress)
    elif fault in evidence:
        evidence[fault] = False if isinstance(evidence[fault], bool) else "wrong"
    before = rows(journal)
    count = 0

    def resolver():
        nonlocal count
        count += 1
        value = copy.deepcopy(evidence)
        if fault == "resolver-changed" and count > 1:
            value["claim"] = "changed"
        return value

    with pytest.raises(BootstrapRefused):
        recover_denied_grant(
            journal,
            registry,
            revalidate=resolver,
            verify_absence=lambda _: fault != "absence",
        )
    assert rows(journal) == before
    assert (
        len(
            journal.store.execute(
                "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
                journal.key,
            )
        )
        == 1
    )


def test_failure_during_final_absence_check_rolls_back_cleanup(denied):
    journal, registry, _, evidence = denied
    before = rows(journal)
    count = 0

    def absent(_):
        nonlocal count
        count += 1
        return count < 3

    with pytest.raises(BootstrapRefused):
        recover_denied_grant(
            journal, registry, revalidate=lambda: evidence, verify_absence=absent
        )
    assert rows(journal) == before
