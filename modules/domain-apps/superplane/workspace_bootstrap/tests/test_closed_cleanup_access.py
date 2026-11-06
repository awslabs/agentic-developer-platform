"""Closed cleanup never replays an uncertain create or adopts a foreign reader."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from superplane_bootstrap.closed_cleanup_access import VerifierAccess
from superplane_bootstrap.closed_cleanup_journal import CleanupJournal
from superplane_bootstrap.closed_cleanup_permit import (
    KIND,
    PREFIX,
    VIEW_POLICY,
    binding_digest,
    canonical,
    digest,
    validate,
)
from superplane_bootstrap.errors import BootstrapRefused


def permit():
    now = datetime.now(UTC)
    plan = {
        "grants": [
            {
                "key": "registrar-entry",
                "principal_arn": "arn:aws:iam::000000000000:role/registrar",
            }
        ]
    }
    value = {
        "kind": KIND,
        "attempt_id": "44444444-4444-4444-8444-444444444444",
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "owner_role_arn": "arn:aws:iam::000000000000:role/owner",
        "owner_role_id": "AROA" + "O" * 17,
        "authority": {
            "org_id": "org",
            "workspace_id": "workspace",
            "subject": "human",
            "status": "active",
            "role_arn": "arn:aws:iam::000000000000:role/provider",
        },
        "qualification": {
            "evidence_kind": "bootstrap-reader-components-revoked-v1",
            "org_id": "org",
            "workspace_id": "workspace",
            "operation_id": "op",
            "generation": "a" * 64,
            "cluster_arn": "arn:aws:eks:us-east-1:000000000000:cluster/test",
            "original_authority_expired": True,
            "issued_sessions_expired": True,
            "settled_bootstrap": {},
            "retained_plan": plan,
            "plan_sha256": digest(plan),
        },
        "descriptor": {"settled_bootstrap": {}},
        "operation": {
            "org_id": "org",
            "workspace_id": "workspace",
            "operation_id": "op",
            "requester": "human",
            "state": "unknown",
            "budget_state": "retained",
        },
        "lease": {
            "holder": None,
            "closed_at": now.isoformat(),
            "expires_at": None,
            "runtime_deadline": None,
            "attempts": 2,
            "max_attempts": 5,
        },
        "artifact": {},
        "service_source_sha256": "b" * 64,
        "verifier_policy_arn": VIEW_POLICY,
        "registrar": {
            "role_arn": plan["grants"][0]["principal_arn"],
            "role_id": "AROA" + "R" * 17,
        },
    }
    value["permit_id"] = PREFIX + binding_digest(value)
    return value


class Journal:
    def __init__(self):
        self.state = {"sequence": 0, "phase": "new"}

    def read(self):
        return copy.deepcopy(self.state)

    def append(self, before, **changes):
        assert before == self.state
        self.state = {**before, **changes, "sequence": before["sequence"] + 1}
        return self.read()


class EKS:
    def __init__(self):
        self.entry, self.policies, self.writes = None, [], []
        self.lose = None
        self.authorized_arn = None

    def describe_access_entry(self, **kw):
        if self.entry is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeAccessEntry"
            )
        return {"accessEntry": copy.deepcopy(self.entry)}

    def list_associated_access_policies(self, **kw):
        return {"associatedAccessPolicies": copy.deepcopy(self.policies)}

    def create_access_entry(self, **kw):
        self.writes.append("create")
        assert self.entry is None
        self.entry = {k: v for k, v in kw.items() if k != "clientRequestToken"}
        self.entry.update(
            accessEntryArn="arn:immutable-verifier-entry", kubernetesGroups=[]
        )
        if self.lose == "create":
            raise TimeoutError("unknown create outcome")
        return {"accessEntry": copy.deepcopy(self.entry)}

    def scoped(self, arn):
        self.authorized_arn = arn
        return self

    def check(self):
        assert self.entry and self.entry["accessEntryArn"] == self.authorized_arn

    def associate_access_policy(self, **kw):
        self.check()
        self.writes.append("associate")
        self.policies = [
            {
                "policyArn": kw["policyArn"],
                "accessScope": {"type": "cluster", "namespaces": []},
            }
        ]
        if self.lose == "associate":
            raise TimeoutError("unknown association outcome")

    def disassociate_access_policy(self, **kw):
        self.check()
        self.writes.append("disassociate")
        self.policies = []
        if self.lose == "disassociate":
            raise TimeoutError("unknown disassociation outcome")

    def delete_access_entry(self, **kw):
        self.check()
        self.writes.append("delete")
        self.entry = None
        if self.lose == "delete":
            raise TimeoutError("unknown delete outcome")


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "revoked"),
        ("verifier_policy_arn", "cluster-admin"),
        ("service_source_sha256", "bad"),
    ],
)
def test_permit_is_bounded_and_scope_bound(field, value):
    p = permit()
    assert validate(p) == p
    p[field] = value
    p["permit_id"] = PREFIX + binding_digest(p)
    with pytest.raises((BootstrapRefused, ValueError)):
        validate(p)


def test_permit_expiry_and_binding_cannot_extend_paid_authority():
    p = permit()
    with pytest.raises(BootstrapRefused):
        validate(p, now=datetime.fromisoformat(p["expires_at"]))
    p["lease"]["holder"] = "expired-worker"
    p["permit_id"] = PREFIX + binding_digest(p)
    with pytest.raises(BootstrapRefused):
        validate(p)


@pytest.mark.parametrize(
    "lost", [None, "create", "associate", "disassociate", "delete"]
)
def test_lost_aws_replies_reconcile_without_recreating(lost):
    p, journal, client = permit(), Journal(), EKS()
    access = VerifierAccess(p, journal, client, client, lambda: None, client.scoped)
    client.lose = lost
    if lost in {"create", "associate"}:
        with pytest.raises(TimeoutError):
            access.acquire()
        client.lose = None
    access.acquire()
    access.record_verified(
        {
            "generation": p["qualification"]["generation"],
            "revoked": True,
            "claim_released": True,
        }
    )
    if lost in {"disassociate", "delete"}:
        with pytest.raises(TimeoutError):
            access.teardown()
        client.lose = None
    result = access.teardown()
    assert (
        result["phase"] == "complete" and client.entry is None and not client.policies
    )
    assert client.writes.count("create") == client.writes.count("associate") == 1
    assert client.writes.count("delete") == client.writes.count("disassociate") == 1
    assert access.teardown() == result


def test_unknown_create_cannot_be_replayed_or_declared_absent():
    journal, client = Journal(), EKS()
    access = VerifierAccess(
        permit(), journal, client, client, lambda: None, client.scoped
    )
    journal.append(journal.read(), phase="verifier-create-intent")
    for callback in (access.acquire, lambda: access.teardown(abandon=True)):
        with pytest.raises(BootstrapRefused):
            callback()
    assert client.writes == []


@pytest.mark.parametrize(
    "drift", ["tags", "username", "kubernetesGroups", "accessEntryArn"]
)
def test_replaced_or_retaged_entry_is_never_deleted(drift):
    p, journal, client = permit(), Journal(), EKS()
    access = VerifierAccess(p, journal, client, client, lambda: None, client.scoped)
    access.acquire()
    client.entry[drift] = (
        {}
        if drift == "tags"
        else ["foreign"]
        if drift == "kubernetesGroups"
        else "foreign"
    )
    with pytest.raises(BootstrapRefused):
        access.teardown(abandon=True)
    assert "delete" not in client.writes


class DDB:
    def __init__(self, p):
        self.permit, self.rows, self.lose = p, [], False

    def query(self, **kw):
        assert kw["ConsistentRead"] and not kw["ScanIndexForward"] and kw["Limit"] == 1
        return {"Items": self.rows[-1:]}

    def transact_write_items(self, TransactItems):
        condition, put = TransactItems
        assert condition["ConditionCheck"]["ExpressionAttributeValues"][":revision"][
            "S"
        ] == digest(self.permit)
        row = put["Put"]["Item"]
        assert not any(x["observed_at"] == row["observed_at"] for x in self.rows)
        self.rows.append(copy.deepcopy(row))
        if self.lose:
            raise TimeoutError("lost write reply")


def test_progress_is_append_only_and_lost_ack_is_observed():
    p = permit()
    ddb = DDB(p)
    journal = CleanupJournal(ddb, "authority", "evidence", p, lambda: None)
    initial = journal.read()
    ddb.lose = True
    with pytest.raises(TimeoutError):
        journal.append(initial, phase="verifier-create-intent")
    observed = journal.read()
    assert observed == {"sequence": 1, "phase": "verifier-create-intent"}
    with pytest.raises(BootstrapRefused):
        journal.append(initial, phase="verifier-create-intent")
    assert len(ddb.rows) == 1
    ddb.permit = {**p, "status": "revoked"}
    with pytest.raises(AssertionError):
        journal.append(observed, phase="verifier-created")
    assert len(ddb.rows) == 1


def test_progress_rejects_tampered_evidence():
    p = permit()
    ddb = DDB(p)
    journal = CleanupJournal(ddb, "authority", "evidence", p, lambda: None)
    journal.append(journal.read(), phase="verifier-create-intent")
    ddb.rows[0]["document"]["S"] = canonical({"sequence": 1, "phase": "complete"})
    with pytest.raises(BootstrapRefused):
        journal.read()


def test_large_original_provenance_is_losslessly_bounded_in_one_authority_record():
    from superplane_bootstrap.closed_cleanup_permit import (
        MAX_DOCUMENT_BYTES,
        decode_document,
        encode_document,
    )

    p = permit()
    p["original-provenance-example"] = {
        "untouched": "original approval; original immutable journal; " * 35000
    }
    assert len(canonical(p).encode()) > 1024 * 1024
    encoded = encode_document(p)
    assert len(encoded.encode()) < 380000 and decode_document(encoded) == p
    assert digest(decode_document(encoded)) == digest(p)
    with pytest.raises(BootstrapRefused):
        encode_document({"too_large": "x" * (MAX_DOCUMENT_BYTES + 1)})


def test_compressed_document_expansion_is_bounded():
    import base64
    import gzip
    from superplane_bootstrap.closed_cleanup_permit import (
        MAX_DOCUMENT_BYTES,
        decode_document,
    )

    oversized = canonical(
        {
            "encoding": "gzip-base64-v1",
            "value": base64.b64encode(
                gzip.compress(b"x" * (MAX_DOCUMENT_BYTES + 2))
            ).decode(),
        }
    )
    with pytest.raises(BootstrapRefused):
        decode_document(oversized)


def test_eks_creation_propagation_retries_reads_only(monkeypatch):
    from superplane_bootstrap import closed_cleanup_access

    monkeypatch.setattr(closed_cleanup_access.time, "sleep", lambda _: None)
    p, journal, client = permit(), Journal(), EKS()
    describe = client.describe_access_entry
    delayed = [2]

    def observe(**kw):
        if client.entry is not None and delayed[0]:
            delayed[0] -= 1
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeAccessEntry"
            )
        return describe(**kw)

    client.describe_access_entry = observe
    access = VerifierAccess(p, journal, client, client, lambda: None, client.scoped)
    assert access.acquire()["phase"] == "observer-ready"
    assert client.writes == ["create", "associate"] and delayed[0] == 0
