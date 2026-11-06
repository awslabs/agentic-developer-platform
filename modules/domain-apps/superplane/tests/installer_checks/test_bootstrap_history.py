"""Archived statements never become fabricated full installation receipts."""

import copy
import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from installation.bootstrap_history import archived_member, qualify
from installation.config import LABEL, identity


def archive(document, member):
    raw = json.dumps(document, indent=2).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        entry = tarfile.TarInfo(member)
        entry.size = len(raw)
        stream.addfile(entry, io.BytesIO(raw))
    body = output.getvalue()
    sha = hashlib.sha256(body).hexdigest()
    return body, {
        "key": f"domain-evidence/dev/superplane/history/{sha}.tar.gz",
        "version_id": "immutable-version",
        "archive_sha256": sha,
        "member": member,
        "member_sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.fixture
def history():
    env = dict(
        account_id="879318057152",
        region="us-east-1",
        environment="dev",
        cluster="management",
        namespace="superplane",
        org_id="original-org",
        adp_org_id="original-adp-org",
    )
    owner = identity(env)
    statement = dict(
        version=1,
        run_id="historical-install",
        installation_id=owner,
        status="installed-and-verified",
        original_receipt_sha256="a" * 64,
    )
    preparation = dict(
        version=1,
        run_id="historical-preparation",
        installation_id=owner,
        status="recovery-credentials-prepared",
        environment=copy.deepcopy(env),
    )
    enrollment = dict(
        environment="dev",
        management_region="us-east-1",
        management_cluster="management",
        authority={
            **{k: env[k] for k in ("account_id", "region", "org_id", "adp_org_id")},
            "installation_id": owner,
        },
    )
    lock = {
        "bucket": "adp-terraform-state-879318057152",
        "key": "dev/modules/superplane/installation.lock",
        "etag": "original-lock",
    }
    rows = {}
    for kind, name in (
        ("Namespace", "superplane"),
        ("Deployment", "superplane-api"),
        ("ScaledJob", "superplane-paid-worker"),
    ):
        rows[kind] = {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": "superplane",
                "uid": "original-" + kind,
                "resourceVersion": "1",
                "labels": {LABEL: owner},
            },
            "spec": {"preserved": True},
        }
    state = SimpleNamespace(
        lock=lock,
        lock_reads=0,
        lose_lock_at=None,
        identity_valid=True,
        requests=[],
        rows=rows,
        blobs={},
        refs={},
    )
    installer = SimpleNamespace(
        env=env,
        owner=owner,
        bucket=lock["bucket"],
        receipt={"remote_lock": lock},
        json=lambda value: value,
    )

    def verify():
        if not state.identity_valid:
            raise ValueError("changed operator")

    def locked():
        state.lock_reads += 1
        return (
            None
            if state.lock_reads == state.lose_lock_at
            else copy.deepcopy(state.lock)
        )

    installer.verify_deployment_identity = verify
    installer.reconcile_lock_attempt = locked
    state.cluster = {
        "arn": "arn:aws:eks:us-east-1:879318057152:cluster/management",
        "status": "ACTIVE",
        "endpoint": "https://management.example",
        "certificateAuthority": {"data": "original-ca"},
    }
    state.kube_cluster = [
        {
            "cluster": {
                "server": "https://management.example",
                "certificate-authority-data": "original-ca",
            }
        }
    ]
    installer.kube = lambda *args: copy.deepcopy(
        state.kube_cluster if args[0] == "config" else state.rows[args[1]]
    )

    def rebuild():
        for kind, document in (
            ("verified_statement", statement),
            ("recovery_preparation", preparation),
        ):
            raw, reference = archive(document, kind + ".json")
            state.refs[kind] = reference
            state.blobs[reference["key"]] = raw

    def get_object(**args):
        assert args["Bucket"] == installer.bucket
        state.requests.append(args)
        raw = state.blobs[args["Key"]]
        return {
            "VersionId": "immutable-version",
            "ContentLength": len(raw),
            "Body": io.BytesIO(raw),
        }

    state.reader_identity = {
        "Account": env["account_id"],
        "Arn": "arn:aws:sts::879318057152:assumed-role/installer/session",
        "UserId": "original-installer-role:session",
    }

    def client(name, **kwargs):
        if name == "eks":
            return SimpleNamespace(
                describe_cluster=lambda **kwargs: {"cluster": state.cluster}
            )
        if name == "sts":
            return SimpleNamespace(
                get_caller_identity=lambda: copy.deepcopy(state.reader_identity)
            )
        assert name == "s3"
        return SimpleNamespace(get_object=get_object)

    session = SimpleNamespace(client=client)
    rebuild()
    return SimpleNamespace(
        installer=installer,
        enrollment=enrollment,
        statement=statement,
        preparation=preparation,
        session=session,
        state=state,
        rebuild=rebuild,
        run=lambda: qualify(installer, enrollment, statement, state.refs, session),
    )


def test_exact_archived_bytes_and_current_context_remain_separate_without_writes(
    history,
):
    before = copy.deepcopy(history.statement)
    context = history.run()
    assert history.statement == before and "environment" not in history.statement
    assert context["historical"]["full_original_receipt_recovered"] is False
    assert context["current"]["identity"]["namespace"] == "superplane"
    assert len(history.state.requests) == 2 and history.state.lock_reads == 2
    assert context["historical"]["archives"] == history.state.refs
    history.state.refs["verified_statement"]["member"] = "different"
    assert (
        context["historical"]["archives"]["verified_statement"]["member"]
        == "verified_statement.json"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "archive-bytes",
        "member-hash",
        "member-name",
        "prefix",
        "bucket",
        "statement",
        "spliced-environment",
        "preparation-owner",
        "preparation-status",
        "cluster",
        "account",
        "namespace",
        "org",
        "adp-org",
        "operator",
        "owner",
        "deleting",
        "missing-lock",
        "lost-lock",
        "kube-cluster",
        "kube-ca",
        "eks-account",
    ],
)
def test_archive_provenance_identity_live_ownership_and_lock_fail_closed(
    history, fault
):
    ref = history.state.refs["verified_statement"]
    if fault == "version":
        ref["version_id"] = "another-version"
    elif fault == "archive-bytes":
        history.state.blobs[ref["key"]] += b"changed"
    elif fault == "member-hash":
        ref["member_sha256"] = "0" * 64
    elif fault == "member-name":
        ref["member"] = "missing.json"
    elif fault == "prefix":
        ref["key"] = ref["key"].replace("/dev/", "/other/")
    elif fault == "bucket":
        history.installer.bucket = "another-bucket"
    elif fault == "statement":
        history.statement["run_id"] = "invented-history"
    elif fault == "spliced-environment":
        history.statement["environment"] = history.preparation["environment"]
    elif fault == "preparation-owner":
        history.preparation["installation_id"] = "foreign"
        history.rebuild()
    elif fault == "preparation-status":
        history.preparation["status"] = "planned"
        history.rebuild()
    elif fault in {"cluster", "account", "namespace", "org", "adp-org"}:
        field = {"account": "account_id", "org": "org_id", "adp-org": "adp_org_id"}.get(
            fault, fault
        )
        history.installer.env[field] = "another"
    elif fault == "operator":
        history.state.identity_valid = False
    elif fault == "owner":
        history.state.rows["Deployment"]["metadata"]["labels"][LABEL] = "foreign"
    elif fault == "deleting":
        history.state.rows["Namespace"]["metadata"]["deletionTimestamp"] = "now"
    elif fault == "kube-cluster":
        history.state.kube_cluster[0]["cluster"]["server"] = "https://another.example"
    elif fault == "kube-ca":
        history.state.kube_cluster[0]["cluster"]["certificate-authority-data"] = (
            "another-ca"
        )
    elif fault == "eks-account":
        history.state.cluster["arn"] = (
            "arn:aws:eks:us-east-1:123456789012:cluster/management"
        )
    else:
        history.state.lose_lock_at = 1 if fault == "missing-lock" else 2
    with pytest.raises(ValueError):
        history.run()


@pytest.mark.parametrize("change", ["uid", "configuration", "lock"])
def test_independent_current_context_snapshots_detect_mid_collection_changes(
    history, change
):
    before = history.run()
    if change == "uid":
        history.state.rows["ScaledJob"]["metadata"]["uid"] = "replacement"
    elif change == "configuration":
        history.state.rows["Deployment"]["spec"]["preserved"] = False
    else:
        history.state.lock["etag"] = "new-lock"
    assert history.run() != before


@pytest.mark.parametrize("kind", ["symlink", "duplicate"])
def test_duplicate_or_nonregular_archive_member_is_not_a_statement(history, kind):
    ref = history.state.refs["verified_statement"]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        member = tarfile.TarInfo(ref["member"])
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "another.json"
            stream.addfile(member)
        else:
            raw = json.dumps(history.statement).encode()
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
            stream.addfile(member, io.BytesIO(raw))
            ref["member_sha256"] = hashlib.sha256(raw).hexdigest()
    raw = output.getvalue()
    ref["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    ref["key"] = (
        "domain-evidence/dev/superplane/history/" + ref["archive_sha256"] + ".tar.gz"
    )
    history.state.blobs[ref["key"]] = raw
    with pytest.raises(ValueError):
        archived_member(history.installer, ref, history.session)


def test_archive_size_refusal_closes_stream_without_decompressing(history):
    from installation.bootstrap_history import MAX_ARCHIVE

    body = io.BytesIO(b"unread")
    s3 = SimpleNamespace(
        get_object=lambda **kwargs: {
            "VersionId": "immutable-version",
            "ContentLength": MAX_ARCHIVE + 1,
            "Body": body,
        }
    )
    session = SimpleNamespace(client=lambda *args, **kwargs: s3)
    with pytest.raises(ValueError):
        archived_member(
            history.installer, history.state.refs["verified_statement"], session
        )
    assert body.closed


@pytest.mark.parametrize("different_account", [False, True])
def test_separate_archive_reader_cannot_supply_installer_authority(
    history, different_account
):
    services = []
    original_client = history.session.client

    def archive_client(name, **kwargs):
        services.append(name)
        assert name in {"sts", "s3"}
        if name == "sts":
            return SimpleNamespace(
                get_caller_identity=lambda: {
                    "Account": "123456789012" if different_account else "879318057152",
                    "Arn": "arn:aws:sts::879318057152:assumed-role/archive-reader/session",
                    "UserId": "archive-role:session",
                }
            )
        return original_client(name, **kwargs)

    archive_session = SimpleNamespace(client=archive_client)

    def installer_client(name, **kwargs):
        assert name != "s3"
        return original_client(name, **kwargs)

    installer_session = SimpleNamespace(client=installer_client)

    def call():
        return qualify(
            history.installer,
            history.enrollment,
            history.statement,
            history.state.refs,
            installer_session,
            archive_read_session=archive_session,
        )

    if different_account:
        with pytest.raises(ValueError):
            call()
        assert services == ["sts"]
    else:
        result = call()
        assert services == ["sts", "s3", "s3"]
        assert (
            result["historical"]["archive_reader"]["UserId"] == "archive-role:session"
        )
        assert history.state.lock_reads == 2
