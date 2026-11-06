"""Retained history does not replace fresh source, expiry or live journal reads."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from installation import bootstrap_retained as owner


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "live",
        "sessions",
        "wrong-call",
        "changed",
        "foreign",
        "old-fence",
        "source",
    ],
)
def test_owner_requires_fresh_bound_source_and_stable_retained_history(
    monkeypatch, fault
):
    descriptor = dict(
        operation_id="operation",
        org_id="org",
        workspace_id="workspace",
        source_principal="run#1",
        source_key="call",
        source_fence=4,
    )
    retained = dict(
        reauthorization_id="previous",
        source_fence=1,
        proof_sha256="a" * 64,
        journal={k: descriptor[k] for k in ("operation_id", "org_id", "workspace_id")},
    )
    expiry = {
        **descriptor,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "earliest_owner_evidence_at": (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(),
    }
    reads = []

    def read_retained():
        reads.append(True)
        value = copy.deepcopy(retained)
        if fault == "changed" and len(reads) > 1:
            value["proof_sha256"] = "b" * 64
        return value

    def source(*args, **kwargs):
        if fault == "source":
            raise ValueError("source not qualified")
        return {**descriptor, "record_id": "fresh-owner-record"}

    monkeypatch.setattr(owner, "collect", source)
    if fault == "live":
        expiry["earliest_owner_evidence_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=1)
        ).isoformat()
    elif fault == "sessions":
        expiry["issued_sessions_expired"] = False
    elif fault == "wrong-call":
        expiry["source_key"] = "other"
    elif fault == "foreign":
        retained["journal"]["workspace_id"] = "other"
    elif fault == "old-fence":
        retained["source_fence"] = descriptor["source_fence"]

    def collect():
        return owner.collect_retained(
            {},
            {},
            descriptor,
            None,
            read_retained=read_retained,
            read_expiry=lambda: expiry,
        )

    if fault:
        with pytest.raises(ValueError):
            collect()
    else:
        result = collect()
        assert result["record_id"] == "fresh-owner-record"
        assert result["retained_revocation"] == retained
        assert result["evidence_kind"] == "bootstrap-retained-revocation-barrier-v1"


@pytest.mark.parametrize(
    "fault",
    [None, "actor", "count", "limit", "cap", "bool", "blank", "unknown", "changed"],
)
def test_owner_extension_binds_exhausted_live_lease_and_selected_human(
    monkeypatch, fault
):
    descriptor = dict(
        operation_id="op",
        org_id="org",
        workspace_id="ws",
        source_principal="run#1",
        source_key="call",
        source_fence=10,
    )
    extension = dict(
        authorized_by="human",
        authorization="Five additional attempts authorized",
        previous_limit=5,
        recorded_attempts=5,
        additional_attempts=5,
    )
    descriptor["attempt_extension"] = extension
    retained = dict(
        source_fence=8,
        journal={k: descriptor[k] for k in ("operation_id", "org_id", "workspace_id")},
    )
    expiry = {
        **descriptor,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "earliest_owner_evidence_at": (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(),
        "attempts": 5,
        "max_attempts": 5,
    }
    monkeypatch.setattr(
        owner, "collect", lambda *args, **kwargs: {"record_id": "fresh-source"}
    )
    if fault == "actor":
        extension["authorized_by"] = "other"
    if fault == "count":
        expiry["attempts"] = 4
    if fault == "limit":
        expiry["max_attempts"] = 6
    if fault == "cap":
        extension["additional_attempts"] = 6
    if fault == "bool":
        extension["recorded_attempts"] = True
    if fault == "blank":
        extension["authorization"] = " "
    if fault == "unknown":
        extension["extra"] = True
    reads = []

    def read_expiry():
        reads.append(True)
        result = copy.deepcopy(expiry)
        if fault == "changed" and len(reads) > 1:
            result["attempts"] = 6
        return result

    def collect():
        return owner.collect_retained(
            {"authority": {"subject": "human"}},
            {},
            descriptor,
            None,
            read_retained=lambda: retained,
            read_expiry=read_expiry,
        )

    if fault:
        with pytest.raises(ValueError):
            collect()
    else:
        assert collect()["attempt_extension"] == extension
