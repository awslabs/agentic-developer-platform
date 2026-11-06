"""The composed read proof remains fresh before any bootstrap grant effect."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from superplane_bootstrap.authority_backend import (
    BootstrapClients,
    BootstrapGrantBackend,
)
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.target import verify_target

from workspace_provisioning.credentials import (
    _bootstrap_actor_identity_reads,
    assume_session,
)
from workspace_provisioning.runtime_config import LifecycleRefused


ACTORS = ("registrar", "installer", "supervisor")
ACTOR_READS = [(service, actor) for service in ("sts", "iam") for actor in ACTORS]


@pytest.fixture
def proof(binding, provider_identity, observed_cluster, expected_target):
    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    roles = {a: f"arn:aws:iam::{target.account_id}:role/{a}" for a in ACTORS}
    state = SimpleNamespace(
        events=[], revoked=False, fault=None, on_read=lambda event: None
    )

    def read(event):
        state.events.append(event)
        state.on_read(event)

    def identity(actor):
        read(("sts", actor))
        return {
            "Account": "999999999999"
            if state.fault == "account" and actor == "installer"
            else target.account_id,
            "UserId": "immutable-" + actor + ":session",
        }

    def role(*, RoleName):
        read(("iam", RoleName))
        return {
            "Role": {
                "Arn": roles[RoleName] + "-other"
                if state.fault == "role" and RoleName == "installer"
                else roles[RoleName],
                "RoleId": "replacement"
                if state.fault == "role-id" and RoleName == "installer"
                else "immutable-" + RoleName,
            }
        }

    def cluster(*, name):
        assert name == target.cluster_name
        read(("eks", "cluster"))
        value = {
            "name": name,
            "arn": target.cluster_arn,
            "endpoint": target.endpoint,
            "certificateAuthority": {"data": target.certificate_authority_data},
            "status": "ACTIVE",
            "accessConfig": {"authenticationMode": "API"},
        }
        if state.fault == "ca":
            value["certificateAuthority"]["data"] = "different-ca"
        elif state.fault == "auth-mode":
            value["accessConfig"]["authenticationMode"] = "API_AND_CONFIG_MAP"
        elif state.fault in {"name", "arn", "endpoint", "status"}:
            value[state.fault] = "different-value"
        return {"cluster": value}

    def resolve(operation_id, *, recovery=False):
        assert operation_id == binding.operation_id
        state.events.append(("authority", recovery))
        if state.revoked:
            raise LifecycleRefused("synthetic authority withdrawal")
        return binding

    sessions = {
        actor: SimpleNamespace(
            client=lambda service, actor=actor: SimpleNamespace(
                get_caller_identity=lambda: identity(actor)
            )
        )
        for actor in ACTORS
    }
    source = SimpleNamespace(client=lambda service: SimpleNamespace(get_role=role))
    eks = SimpleNamespace(
        meta=SimpleNamespace(region_name=target.region), describe_cluster=cluster
    )

    def clients():
        return BootstrapClients(
            binding=binding,
            target=target,
            principals=roles,
            binding_resolver=resolve,
            identity_readers={actor: None for actor in ACTORS},
            identity_snapshot=_bootstrap_actor_identity_reads(sessions, source, roles),
            eks=eks,
            entry_client=None,
            registrar_kubernetes=None,
            supervisor_kubernetes=None,
            installer_access=None,
            supervisor_access=None,
        )

    def create():
        adapter = SimpleNamespace(
            create=lambda spec: state.events.append(("effect", spec["key"]))
        )
        # Exercise the real effect-entry guard without constructing unrelated
        # provider adapters or a persistent authority journal.
        backend = SimpleNamespace(clients=clients(), _adapter=lambda spec: adapter)
        BootstrapGrantBackend.create(backend, {"key": "installer-role"})

    return SimpleNamespace(
        state=state,
        clients=clients,
        create=create,
        sessions=sessions,
        roles=roles,
        target=target,
        identity=identity,
        eks=eks,
    )


def test_each_effect_gets_a_new_complete_proof_with_one_authority_bracket(proof):
    barrier = Barrier(6)

    def overlap(event):
        if event in ACTOR_READS:
            barrier.wait(timeout=10)

    proof.state.on_read = overlap
    for _ in range(2):
        before = len(proof.state.events)
        proof.create()
        events = proof.state.events[before:]
        assert events[0] == ("authority", False)
        assert sorted(events[1:7]) == sorted(ACTOR_READS)
        assert events[7:] == [
            ("eks", "cluster"),
            ("authority", False),
            ("effect", "installer-role"),
        ]


def test_rejected_initial_authority_starts_no_read_or_effect(proof):
    proof.state.revoked = True
    with pytest.raises(LifecycleRefused, match="withdrawal"):
        proof.create()
    assert proof.state.events == [("authority", False)]


@pytest.mark.parametrize("withdraw_at", ACTOR_READS + [("eks", "cluster")])
def test_withdrawal_during_each_read_refuses_the_effect(proof, withdraw_at):
    def withdraw(event):
        if event == withdraw_at:
            proof.state.revoked = True

    proof.state.on_read = withdraw
    with pytest.raises(LifecycleRefused, match="withdrawal"):
        proof.create()
    assert proof.state.events[-1] == ("authority", False)
    assert not any(event[0] == "effect" for event in proof.state.events)


@pytest.mark.parametrize(
    "fault",
    [
        "account",
        "role",
        "role-id",
        "region",
        "name",
        "arn",
        "endpoint",
        "ca",
        "status",
        "auth-mode",
    ],
)
def test_actor_or_cluster_drift_is_not_hidden_by_an_earlier_proof(proof, fault):
    proof.create()
    proof.state.events.clear()
    proof.state.fault = fault
    if fault == "region":
        proof.eks.meta.region_name = "different-region"
    with pytest.raises((LifecycleRefused, BootstrapRefused)):
        proof.create()
    assert not any(event[0] == "effect" for event in proof.state.events)


@pytest.mark.parametrize("failure", [PermissionError, TimeoutError])
def test_failed_batch_joins_all_started_reads_before_returning(proof, failure):
    barrier = Barrier(6)
    failed, waiting, release, finished = Event(), Event(), Event(), []

    def block_and_fail(event):
        if event not in ACTOR_READS:
            return
        try:
            barrier.wait(timeout=10)
            if event == ("sts", "registrar"):
                failed.set()
                raise failure("synthetic SDK failure")
            if event == ("iam", "supervisor"):
                waiting.set()
                assert release.wait(timeout=10)
        finally:
            finished.append(event)

    proof.state.on_read = block_and_fail
    with ThreadPoolExecutor(max_workers=1) as executor:
        attempt = executor.submit(proof.create)
        try:
            assert failed.wait(timeout=10) and waiting.wait(timeout=10)
            with pytest.raises(TimeoutError):
                attempt.result(timeout=0.1)
            assert not attempt.done()
        finally:
            release.set()
        with pytest.raises(failure, match="synthetic SDK failure"):
            attempt.result(timeout=10)
    assert sorted(finished) == sorted(ACTOR_READS)
    assert not any(event[0] in {"eks", "effect"} for event in proof.state.events)


@pytest.mark.parametrize("withdraw_at", [None, "before-issue", "after-issue"])
@pytest.mark.parametrize(
    "session_name",
    ["superplane-lifecycle", "sp-acquisition-cleanup-0123456789abcdef01234567"],
)
def test_read_triggered_actor_refresh_keeps_its_independent_gates(
    proof, monkeypatch, withdraw_at, session_name
):
    """Force real RefreshableCredentials to call production assume_session.refresh."""
    import boto3

    events, sessions = [], []
    refreshing = False

    def verify():
        events.append("credential-authority")
        if refreshing and (
            withdraw_at == "before-issue"
            or (withdraw_at == "after-issue" and "issue" in events)
        ):
            raise LifecycleRefused("synthetic credential authority withdrawal")

    def deadline():
        events.append("deadline")
        return datetime.now(UTC) + timedelta(hours=1)

    def issue(**arguments):
        assert arguments["RoleArn"] == proof.roles["registrar"]
        assert arguments["DurationSeconds"] == 900
        assert arguments["RoleSessionName"] == session_name
        events.append("issue")
        return {
            "Credentials": {
                "AccessKeyId": "synthetic-access-key",
                "SecretAccessKey": "synthetic-secret-key",
                "SessionToken": "synthetic-session-token",
                "Expiration": datetime.now(UTC) + timedelta(seconds=900),
            }
        }

    def session_factory(*, botocore_session):
        def identity():
            botocore_session.get_credentials().get_frozen_credentials()
            return proof.identity("registrar")

        session = SimpleNamespace(
            client=lambda *args, **kwargs: SimpleNamespace(
                get_caller_identity=identity
            ),
            credentials=botocore_session.get_credentials(),
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(boto3, "Session", session_factory)
    source = SimpleNamespace(
        client=lambda *args, **kwargs: SimpleNamespace(assume_role=issue),
        _superplane_authority_deadline=deadline,
    )
    proof.sessions["registrar"] = assume_session(
        source,
        role_arn=proof.roles["registrar"],
        region=proof.target.region,
        verify=verify,
        session_name=session_name,
    )
    sessions[0].credentials._expiry_time = datetime.now(UTC) - timedelta(seconds=1)
    events.clear()
    proof.state.events.clear()
    refreshing = True
    if withdraw_at:
        with pytest.raises(LifecycleRefused, match="credential authority withdrawal"):
            proof.create()
        assert not any(event[0] == "effect" for event in proof.state.events)
    else:
        proof.create()
        assert proof.state.events[-1] == ("effect", "installer-role")
    expected = ["credential-authority"]
    if withdraw_at != "before-issue":
        expected += ["deadline", "issue", "credential-authority"]
    if withdraw_at is None:
        expected.append("deadline")
    assert events == expected


def test_cleanup_entry_session_keeps_exact_arn_and_named_trust(monkeypatch):
    import json
    from workspace_provisioning import cluster_clients

    issued = []
    eks = object()

    def assume(source, **arguments):
        issued.append((source, arguments))
        return SimpleNamespace(client=lambda service, **kwargs: eks)

    monkeypatch.setattr(cluster_clients, "assume_session", assume)
    owner, verify = object(), lambda: None
    role = (
        "arn:aws:iam::123456789012:role/sp-acquisition-cleanup-0123456789abcdef01234567"
    )
    resolver = cluster_clients.scoped_entry_client(
        owner,
        role_arn=role,
        region="us-east-1",
        verify=verify,
        session_name=role.rsplit("/", 1)[1],
    )
    entry = "arn:aws:eks:us-east-1:123456789012:access-entry/workspace/role/123456789012/original/immutable"
    assert resolver(entry) is eks
    assert issued[0][0] is owner
    args = issued[0][1]
    assert args["session_name"] == role.rsplit("/", 1)[1]
    assert args["role_arn"] == role and args["verify"] is verify
    policy = json.loads(args["policy"])
    assert len(policy["Statement"]) == 1
    assert policy["Statement"][0]["Resource"] == entry
    assert set(policy["Statement"][0]["Action"]) == {
        "eks:DeleteAccessEntry",
        "eks:DisassociateAccessPolicy",
        "eks:DescribeAccessEntry",
        "eks:ListAssociatedAccessPolicies",
    }
    with pytest.raises(LifecycleRefused):
        resolver(entry.replace("123456789012", "999999999999"))
    assert len(issued) == 1


@pytest.mark.parametrize("name", ["", "*", "nested/session", "x" * 65, None])
def test_invalid_role_session_name_refused_before_aws(name):
    with pytest.raises(LifecycleRefused, match="session name"):
        assume_session(
            object(),
            role_arn="arn:aws:iam::123456789012:role/cleanup",
            region="us-east-1",
            verify=lambda: None,
            session_name=name,
        )
