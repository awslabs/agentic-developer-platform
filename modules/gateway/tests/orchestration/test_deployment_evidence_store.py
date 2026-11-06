"""Real signatures protect S3 bytes even from an unrelated bucket writer."""

import base64
import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa

from deployment_evidence.store import ISSUER, MAX_ENVELOPE, EvidenceStore, audience, bucket_name, envelope, object_key, verify

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("evidence_publisher", ROOT / "modules/gateway/scripts/publish-deployment-evidence.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


@pytest.fixture
def signed():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Retained evidence is still valid after the original short-lived token expired.
    issued = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    claims = dict(
        iss=ISSUER,
        aud=audience(b'{"verified":true}'),
        iat=issued,
        nbf=issued,
        exp=issued + 300,
        repository="org/repo",
        repository_id="17",
        run_id="42",
        run_attempt="2",
        workflow_ref="org/repo/.github/workflows/gateway-deploy.yml@refs/heads/main",
        workflow_sha="a" * 40,
    )
    expected = dict(
        repository="org/repo",
        repository_id=17,
        run_id=42,
        attempt=2,
        workflow_path=".github/workflows/gateway-deploy.yml",
        workflow_revision="a" * 40,
        published_at=datetime.fromtimestamp(issued + 30, UTC),
    )
    return SimpleNamespace(key=key, claims=claims, expected=expected, payload=b'{"verified":true}')


def package(ctx):
    return envelope(ctx.payload, jwt.encode(ctx.claims, ctx.key, algorithm="RS256", headers={"kid": "test"}))


def test_retained_signature_is_valid_after_bearer_expiry(signed):
    assert verify(package(signed), signing_key=signed.key.public_key(), **signed.expected) == signed.payload


@pytest.mark.parametrize(
    "claim,value",
    [
        ("iss", "https://attacker.invalid"),
        ("aud", "sts.amazonaws.com"),
        ("repository", "other/repo"),
        ("repository_id", "99"),
        ("run_id", "99"),
        ("run_attempt", "1"),
        ("workflow_sha", "b" * 40),
        ("workflow_ref", "org/repo/.github/workflows/unreviewed.yml@refs/heads/main"),
        ("job_workflow_ref", "org/repo/.github/workflows/unreviewed.yml@refs/heads/main"),
    ],
)
def test_other_workflow_run_or_audience_cannot_sign_this_evidence(signed, claim, value):
    signed.claims[claim] = value
    with pytest.raises((ValueError, jwt.PyJWTError)):
        verify(package(signed), signing_key=signed.key.public_key(), **signed.expected)


def test_reusable_workflow_requires_exact_callee_revision(signed):
    signed.expected["workflow_path"] = ".github/workflows/run-gateway-migrations.yml"
    signed.claims.update(job_workflow_ref="org/repo/.github/workflows/run-gateway-migrations.yml@refs/heads/main", job_workflow_sha="a" * 40)
    assert verify(package(signed), signing_key=signed.key.public_key(), **signed.expected) == signed.payload
    signed.claims["job_workflow_sha"] = "b" * 40
    with pytest.raises(ValueError, match="workflow"):
        verify(package(signed), signing_key=signed.key.public_key(), **signed.expected)


@pytest.mark.parametrize("delta", [-60, 301])
def test_object_written_outside_token_validity_is_refused(signed, delta):
    signed.expected["published_at"] = datetime.fromtimestamp(signed.claims["iat"] + delta, UTC)
    with pytest.raises(ValueError, match="published"):
        verify(package(signed), signing_key=signed.key.public_key(), **signed.expected)


def test_modified_bytes_and_forged_signature_are_refused(signed):
    raw = json.loads(package(signed))
    raw["payload"] = base64.b64encode(b'{"verified":false}').decode()
    with pytest.raises(jwt.InvalidAudienceError):
        verify(json.dumps(raw).encode(), signing_key=signed.key.public_key(), **signed.expected)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        verify(package(signed), signing_key=wrong.public_key(), **signed.expected)


def reader(signed, **changes):
    raw = package(signed)
    response = dict(
        ContentLength=len(raw),
        Body=io.BytesIO(raw),
        VersionId="immutable/version",
        ServerSideEncryption="AES256",
        LastModified=signed.expected["published_at"],
    )
    response.update(changes)
    calls = []

    def get(**kw):
        calls.append(kw)
        return response

    store = EvidenceStore(
        s3=SimpleNamespace(get_object=get), keys=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signed.key.public_key()))
    )
    expected = {k: v for k, v in signed.expected.items() if k != "published_at"}
    expected.update(account="123456789012", environment="dev", region="us-east-1", kind="context", name="gateway-deploy.yml")
    return store, expected, calls, response


def test_reads_exact_owned_object_and_retains_version(signed):
    store, args, calls, response = reader(signed)
    payload, digest, reference = store.read(**args)
    assert payload == signed.payload and digest == hashlib.sha256(payload).hexdigest()
    assert calls == [
        dict(
            Bucket="adp-dev-deployment-evidence-123456789012",
            Key="deployment-evidence/v1/17/42/2/context/gateway-deploy.yml.json",
            ExpectedBucketOwner="123456789012",
        )
    ]
    assert reference.endswith("?versionId=immutable%2Fversion") and response["Body"].closed


@pytest.mark.parametrize("kind,name", [("release", "tool-adapters"), ("context", "tools-platform-deploy.yml")])
def test_adapter_producer_signed_store_and_core_schema_roundtrip(tmp_path, monkeypatch, signed, kind, name, request):
    """Real renderer/observer, producer, publisher, signature and core contracts compose."""
    adapter_root = ROOT / "modules/tools/adapters"
    for directory in (adapter_root.parent, adapter_root, adapter_root / "tests"):
        monkeypatch.syspath_prepend(str(directory))

    def load(label, path):
        spec = importlib.util.spec_from_file_location(label, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    from tool_adapters.config import AdapterConfiguration

    from src.orchestration.deployment_runtime_contract import ReleaseArtifact
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    producer = load("adapter_release_producer", adapter_root / "release_evidence.py")
    fixtures = load("adapter_release_fixtures", adapter_root / "tests/test_release_evidence.py")
    configuration = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "foundation",
            "release": {
                "contract_version": "1.0",
                "source_revision": "a" * 40,
                "image_digest": "sha256:" + "b" * 64,
                "group": "tool-connectors",
                "group_generation": 1,
                "backend_generation": 1,
            },
        }
    )
    cluster = fixtures.cluster.__wrapped__(configuration, request)
    state = fixtures.document_inputs.__wrapped__(configuration, cluster)
    release, context = fixtures.produce(producer, state)
    document = release if kind == "release" else context
    path = tmp_path / "document.json"
    path.write_bytes(producer.encoded(document))
    env = {
        **state.env,
        "ACCOUNT_ID": state.env["ADP_TOOLS_ACCOUNT_ID"],
        "ENVIRONMENT": "dev",
        "ADP_WORKFLOW_REVISION": "d" * 40,
    }
    signed.claims.update(run_attempt="1", workflow_ref=state.env["GITHUB_WORKFLOW_REF"], workflow_sha="d" * 40)
    stored = {}

    def aws(args):
        if args == ["sts", "get-caller-identity"]:
            return {"Account": "123456789012"}
        assert args[:2] == ["s3api", "put-object"]
        assert args[args.index("--if-none-match") + 1] == "*"
        key = args[args.index("--key") + 1]
        stored[key] = Path(args[args.index("--body") + 1]).read_bytes()
        return {"VersionId": "adapter-receipt-v1"}

    result = publisher.publish(
        env,
        path,
        kind,
        name,
        aws=aws,
        request_token=lambda aud: jwt.encode({**signed.claims, "aud": aud}, signed.key, algorithm="RS256", headers={"kid": "test"}),
    )
    calls = []

    def get(**kwargs):
        calls.append(kwargs)
        raw = stored[kwargs["Key"]]
        return {
            "ContentLength": len(raw),
            "Body": io.BytesIO(raw),
            "VersionId": "adapter-receipt-v1",
            "ServerSideEncryption": "AES256",
            "LastModified": signed.expected["published_at"],
        }

    store = EvidenceStore(
        s3=SimpleNamespace(get_object=get), keys=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signed.key.public_key()))
    )
    expected = {
        "account": "123456789012",
        "environment": "dev",
        "region": "us-east-1",
        "repository": "org/repo",
        "repository_id": 17,
        "run_id": 42,
        "attempt": 1,
        "kind": kind,
        "name": name,
        "workflow_path": ".github/workflows/tools-platform-deploy.yml",
        "workflow_revision": "d" * 40,
    }
    payload, digest, reference = store.read(**expected)
    schema = ReleaseArtifact if kind == "release" else WorkflowContext
    parsed = schema.model_validate_json(payload)
    if kind == "release":
        assert parsed.compatibility == document["compatibility"]
    assert parsed.resource_id == "example-cluster/adp-tools"
    if kind == "release":
        assert parsed.compatibility == release["compatibility"]
        legacy = {key: value for key, value in release.items() if key != "compatibility"}
        assert ReleaseArtifact.model_validate(legacy).compatibility is None
    assert digest == result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert reference.endswith("?versionId=adapter-receipt-v1")
    assert calls == [
        {
            "Bucket": "adp-dev-deployment-evidence-123456789012",
            "Key": f"deployment-evidence/v1/17/42/1/{kind}/{name}.json",
            "ExpectedBucketOwner": "123456789012",
        }
    ]
    # A bucket writer cannot change the expected documents while retaining the signature.
    envelope_doc = json.loads(stored[result["key"]])
    tampered = {**document, "resource_id": "different-cluster/adp-tools"}
    envelope_doc["payload"] = base64.b64encode(producer.encoded(tampered)).decode()
    stored[result["key"]] = json.dumps(envelope_doc).encode()
    with pytest.raises(jwt.InvalidAudienceError):
        store.read(**expected)


@pytest.mark.parametrize(
    "changes", [{"VersionId": "null"}, {"VersionId": None}, {"ServerSideEncryption": None}, {"ContentLength": MAX_ENVELOPE + 1}, {"ContentLength": 1}]
)
def test_unversioned_unencrypted_or_unbounded_objects_refused(signed, changes):
    store, args, _, response = reader(signed, **changes)
    with pytest.raises(ValueError):
        store.read(**args)
    assert response["Body"].closed


def test_missing_object_is_pending_but_access_denial_is_not(signed):
    store, args, _, _ = reader(signed)
    for code in ("NoSuchKey", "AccessDenied", "NoSuchBucket"):

        def get(**_):
            raise ClientError({"Error": {"Code": code}}, "GetObject")

        store.s3 = SimpleNamespace(get_object=get, list_objects_v2=get)
        if code == "NoSuchKey":
            assert store.read(**args) is None
        else:
            with pytest.raises(ClientError):
                store.read(**args)


@pytest.mark.parametrize("exists", [False, True])
def test_prefix_listing_distinguishes_missing_object_from_denied_existing_object(signed, exists):
    store, args, _, _ = reader(signed)
    key = object_key(17, 42, 2, "context", "gateway-deploy.yml")

    def get(**_):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    def listing(**kw):
        assert kw == dict(Bucket=bucket_name(args["account"], args["environment"]), Prefix=key, MaxKeys=1, ExpectedBucketOwner=args["account"])
        return {"Contents": [{"Key": key}]} if exists else {}

    store.s3 = SimpleNamespace(get_object=get, list_objects_v2=listing)
    if exists:
        with pytest.raises(ClientError):
            store.read(**args)
    else:
        assert store.read(**args) is None


def test_key_and_bucket_inputs_cannot_escape_selected_scope():
    for environment in ("../prod", "dev/other", "", "DEV"):
        with pytest.raises(ValueError):
            bucket_name("123456789012", environment)
    for name in ("../gateway-deploy.yml", "..yml", "gateway-deploy.yml/extra"):
        with pytest.raises(ValueError):
            object_key(17, 42, 2, "context", name)
    for name in ("tools-interceptor-deploy.yml", "tools-facade-deploy.yml", "tools-platform-deploy.yml", "ship-any-product.yaml"):
        assert object_key(17, 42, 2, "context", name).endswith(f"/context/{name}.json")
    assert object_key(17, 42, 2, "context", "chat-agent-deploy.yml").endswith("/context/chat-agent-deploy.yml.json")
    for component in ("tools-interceptor", "tools-facade", "tools-platform", "agent-worker", "chat-worker"):
        assert object_key(17, 42, 2, "release", component).endswith(f"/release/{component}.json")
    with pytest.raises(ValueError):
        object_key(17, 42, 2, "release", "tools-platform-deploy.yml")


def test_publisher_creates_only_and_never_logs_or_passes_token_in_argv(tmp_path):
    doc = dict(
        repository_id=17,
        run_id=42,
        run_attempt=2,
        account_id="123456789012",
        workflow_revision="a" * 40,
        workflow_path=".github/workflows/gateway-deploy.yml",
    )
    path = tmp_path / "context.json"
    path.write_text(json.dumps(doc))
    env = dict(
        GITHUB_REPOSITORY_ID="17",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="2",
        ACCOUNT_ID="123456789012",
        ENVIRONMENT="dev",
        ADP_WORKFLOW_REVISION="a" * 40,
    )
    calls = []

    def aws(parts):
        calls.append(parts)
        if parts[0] == "sts":
            return {"Account": env["ACCOUNT_ID"]}
        stored = json.loads(Path(parts[parts.index("--body") + 1]).read_text())
        assert stored["signature"] == "private-signed-token"
        assert parts[parts.index("--if-none-match") + 1] == "*"
        assert parts[parts.index("--expected-bucket-owner") + 1] == env["ACCOUNT_ID"]
        return {"VersionId": "one-version"}

    result = publisher.publish(env, path, "context", "gateway-deploy.yml", aws=aws, request_token=lambda aud: "private-signed-token")
    assert "private-signed-token" not in json.dumps([calls, result])
    doc["run_attempt"] = 1
    path.write_text(json.dumps(doc))
    calls.clear()
    with pytest.raises(ValueError, match="this deployment"):
        publisher.publish(env, path, "context", "gateway-deploy.yml", aws=aws, request_token=lambda _: pytest.fail("must not sign"))
    assert calls == []


@pytest.mark.parametrize("mutation", [None, "bytes", "version", "source", "attempt", "key", "size"])
@pytest.mark.parametrize("artifact_name", ["scan", "identity-proof", "atlassian-foundation", "arbitrary-product-observations"])
def test_signed_scan_archive_roundtrip_and_tampering(tmp_path, monkeypatch, signed, mutation, artifact_name):
    import zipfile

    env = dict(
        GITHUB_REPOSITORY_ID="17",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="2",
        GITHUB_REPOSITORY="org/repo",
        GITHUB_WORKFLOW_REF="org/repo/.github/workflows/security-scan.yml@refs/heads/main",
        ACCOUNT_ID="123456789012",
        ENVIRONMENT="dev",
        ADP_WORKFLOW_REVISION="a" * 40,
        EXPECTED_SOURCE_REVISION="b" * 40,
    )
    if artifact_name == "identity-proof":
        env.update(
            GITHUB_WORKFLOW_REF="org/repo/.github/workflows/gateway-deploy.yml@refs/heads/main",
            GITHUB_EVENT_NAME="workflow_dispatch",
            PROOF_PHASE="stage.identity-proof",
            GATEWAY_REVISION="b" * 40,
        )
        monkeypatch.setattr(publisher.subprocess, "check_output", lambda *a, **kw: "a" * 40)
        ancestry = []
        monkeypatch.setattr(publisher.subprocess, "check_call", lambda args: ancestry.append(args))
        path = tmp_path / "atlassian-stage-summary.json"
        path.write_text('{"generated_cleanup":true}')
        expected_files = {path.name: path.read_bytes()}
    elif artifact_name == "atlassian-foundation":
        env.update(
            GITHUB_WORKFLOW_REF="org/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/main",
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_RUN_ATTEMPT="1",
            ADP_FOUNDATION_EVIDENCE_STORE="s3-oidc-v1",
        )
        monkeypatch.setattr(publisher.subprocess, "check_output", lambda *args, **kwargs: "b" * 40)
        ancestry = []
        monkeypatch.setattr(publisher.subprocess, "check_call", lambda args: ancestry.append(args))
        path = tmp_path / "atlassian-stage-summary.json"
        path.write_text(json.dumps(dict(stage_id="foundation", source_revision="b" * 40, workflow_revision="a" * 40, run_id="42", run_attempt="1")))
        expected_files = {path.name: path.read_bytes()}
        signed.claims["run_attempt"] = "1"
        signed.expected["attempt"] = 1
    else:
        monkeypatch.setattr(publisher.subprocess, "check_output", lambda *a, **kw: "b" * 40)
        path = tmp_path / "evidence"
        path.mkdir()
        (path / "scan-receipt.json").write_text('{"coverage_complete":true}')
        (path / "provenance").mkdir()
        (path / "provenance" / "image.json").write_text('{"digest":"actual"}')
        expected_files = {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file()}
    stored = {}

    def aws(parts):
        if parts[0] == "sts":
            return {"Account": env["ACCOUNT_ID"]}
        key = parts[parts.index("--key") + 1]
        assert parts[parts.index("--if-none-match") + 1] == "*"
        assert key not in stored
        stored[key] = Path(parts[parts.index("--body") + 1]).read_bytes()
        return {"VersionId": "v1"}

    signed.claims["workflow_ref"] = env["GITHUB_WORKFLOW_REF"]
    signed.expected["workflow_path"] = env["GITHUB_WORKFLOW_REF"].removeprefix("org/repo/").split("@")[0]

    def sign(aud):
        return jwt.encode({**signed.claims, "aud": aud}, signed.key, algorithm="RS256", headers={"kid": "test"})

    kind = "workflow-artifact" if artifact_name == "arbitrary-product-observations" else "repository-artifact"
    publisher.publish_archive(env, path, artifact_name, aws=aws, request_token=sign, kind=kind)
    if artifact_name in {"identity-proof", "atlassian-foundation"}:
        assert ancestry == [["git", "merge-base", "--is-ancestor", "b" * 40, "a" * 40]]
    descriptor_key = object_key(17, 42, int(env["GITHUB_RUN_ATTEMPT"]), kind, artifact_name)
    descriptor = json.loads(base64.b64decode(json.loads(stored[descriptor_key])["payload"]))
    if mutation in {"source", "key", "size"}:
        descriptor[{"source": "source_revision", "key": "key", "size": "size"}[mutation]] = {
            "source": "c" * 40,
            "key": "other/key",
            "size": 999999999,
        }[mutation]
        data = json.dumps(descriptor).encode()
        stored[descriptor_key] = envelope(data, sign(audience(data)))
    calls = []

    def get(**kw):
        calls.append(kw)
        key = kw["Key"]
        data = stored[key]
        if key.endswith(".zip") and mutation == "bytes":
            data = b"x" * len(data)
        return dict(
            ContentLength=len(data),
            Body=io.BytesIO(data),
            VersionId="wrong" if mutation == "version" else "v1",
            ServerSideEncryption="AES256",
            LastModified=signed.expected["published_at"],
        )

    store = EvidenceStore(
        s3=SimpleNamespace(get_object=get), keys=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signed.key.public_key()))
    )
    expected = {k: v for k, v in signed.expected.items() if k != "published_at"}
    expected.update(account=env["ACCOUNT_ID"], environment="dev", region="us-east-1", kind=kind, name=artifact_name, source_revision="b" * 40)
    if mutation == "attempt":
        # Copying an old signed descriptor to this attempt still cannot authenticate it.
        stored[object_key(17, 42, 3, kind, artifact_name)] = stored[descriptor_key]
        expected["attempt"] = 3
    if mutation:
        with pytest.raises((ValueError, jwt.PyJWTError)):
            store.read_archive(**expected)
    else:
        content, digest, ref = store.read_archive(**expected)
        assert hashlib.sha256(content).hexdigest() == digest
        assert ref.endswith("?versionId=v1")
        assert calls[-1]["VersionId"] == "v1"
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert set(archive.namelist()) == set(expected_files)
            assert {name: archive.read(name) for name in archive.namelist()} == expected_files


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_WORKFLOW_REF", "org/repo/.github/workflows/security-scan.yml@refs/heads/main"),
        ("GITHUB_WORKFLOW_REF", "org/repo/.github/workflows/gateway-deploy.yml@refs/heads/other"),
        ("PROOF_PHASE", "none"),
        ("ENVIRONMENT", "prod"),
        ("GATEWAY_REVISION", "c" * 40),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("ADP_WORKFLOW_REVISION", "d" * 40),
    ],
)
def test_identity_proof_publisher_refuses_wrong_context_before_upload(tmp_path, monkeypatch, field, value):
    env = dict(
        GITHUB_WORKFLOW_REF="org/repo/.github/workflows/gateway-deploy.yml@refs/heads/main",
        GITHUB_REPOSITORY="org/repo",
        GITHUB_EVENT_NAME="workflow_dispatch",
        PROOF_PHASE="stage.identity-proof",
        ENVIRONMENT="dev",
        GATEWAY_REVISION="b" * 40,
        EXPECTED_SOURCE_REVISION="b" * 40,
        ADP_WORKFLOW_REVISION="a" * 40,
    )
    env[field] = value
    monkeypatch.setattr(publisher.subprocess, "check_output", lambda *a, **kw: "a" * 40)
    path = tmp_path / "atlassian-stage-summary.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        publisher.publish_archive(
            env, path, "identity-proof", aws=lambda _: pytest.fail("must not upload"), request_token=lambda _: pytest.fail("must not sign")
        )


def test_scan_archive_refuses_symlinks_before_aws(tmp_path):
    path = tmp_path / "linked"
    path.symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="regular archive"):
        publisher.publish_archive({}, path, "scan", aws=lambda _: pytest.fail("no AWS call"), request_token=lambda _: pytest.fail("no signature"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_WORKFLOW_REF", "org/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/other"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("ENVIRONMENT", "prod"),
        ("ADP_FOUNDATION_EVIDENCE_STORE", "github-artifact"),
        ("EXPECTED_SOURCE_REVISION", "c" * 40),
        ("stage_id", "identity-proof"),
        ("source_revision", "c" * 40),
        ("workflow_revision", "c" * 40),
        ("run_id", "99"),
        ("run_attempt", "2"),
    ],
)
def test_foundation_publisher_rejects_unbound_context_before_signing(tmp_path, monkeypatch, field, value):
    env = dict(
        GITHUB_WORKFLOW_REF="org/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/main",
        GITHUB_REPOSITORY="org/repo",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_RUN_ID="42",
        ENVIRONMENT="dev",
        ADP_FOUNDATION_EVIDENCE_STORE="s3-oidc-v1",
        EXPECTED_SOURCE_REVISION="b" * 40,
        ADP_WORKFLOW_REVISION="a" * 40,
    )
    report = dict(stage_id="foundation", source_revision="b" * 40, workflow_revision="a" * 40, run_id="42", run_attempt="1")
    (env if field in env else report)[field] = value
    monkeypatch.setattr(publisher.subprocess, "check_output", lambda *args, **kwargs: "b" * 40)
    monkeypatch.setattr(publisher.subprocess, "check_call", lambda *args, **kwargs: None)
    path = tmp_path / "atlassian-stage-summary.json"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        publisher.publish_archive(
            env, path, "atlassian-foundation", aws=lambda _: pytest.fail("must not upload"), request_token=lambda _: pytest.fail("must not sign")
        )


@pytest.mark.parametrize(
    "change",
    [
        "workflow",
        "attempt",
        "mode",
        "correlation",
        "failed",
        "missing",
        "duplicate",
        "cleanup",
        "source",
    ],
)
def test_identity_publisher_rejects_unbound_or_incomplete_summary_before_signing(tmp_path, monkeypatch, change):
    env = dict(
        GITHUB_WORKFLOW_REF="org/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/main",
        GITHUB_REPOSITORY="org/repo",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_RUN_ID="42",
        ENVIRONMENT="dev",
        ADP_FOUNDATION_EVIDENCE_STORE="s3-oidc-v1",
        EXPECTED_SOURCE_REVISION="b" * 40,
        ADP_WORKFLOW_REVISION="a" * 40,
        ADP_CORRELATION="orch:00000000-0000-4000-8000-000000000001",
        ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON='{"mode":"identity"}',
    )
    report = dict(
        stage_id="identity",
        source_revision="b" * 40,
        workflow_revision="a" * 40,
        run_id="42",
        run_attempt="1",
        correlation=env["ADP_CORRELATION"],
        criteria=[{"criterion_id": f"8448.AC-{index:02}", "phase_status": "pass", "evidence_verified": True} for index in range(1, 5)],
        cleanup_status="confirmed",
        runtime_verified=True,
        independent_verification="confirmed",
        unresolved_operations=[],
    )
    if change == "workflow":
        env["GITHUB_WORKFLOW_REF"] = "org/repo/.github/workflows/gateway-deploy.yml@refs/heads/main"
    elif change == "attempt":
        env["GITHUB_RUN_ATTEMPT"] = "2"
    elif change == "mode":
        env["ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON"] = '{"mode":"release"}'
    elif change == "correlation":
        report["correlation"] = "wrong"
    elif change == "failed":
        report["criteria"][0]["phase_status"] = "failed"
    elif change == "missing":
        report["criteria"].pop()
    elif change == "duplicate":
        report["criteria"].append(report["criteria"][0])
    elif change == "cleanup":
        report["cleanup_status"] = "unconfirmed"
    else:
        report["source_revision"] = "c" * 40
    monkeypatch.setattr(publisher.subprocess, "check_output", lambda *args, **kwargs: "b" * 40)
    monkeypatch.setattr(publisher.subprocess, "check_call", lambda *args, **kwargs: None)
    path = tmp_path / "atlassian-stage-summary.json"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        publisher.publish_archive(
            env,
            path,
            "atlassian-identity",
            aws=lambda _: pytest.fail("must not upload"),
            request_token=lambda _: pytest.fail("must not sign"),
        )


def test_knowledge_door_context_and_release_keys_are_supported():
    """#7829: the first S3-backed door run (2026-10-15 22:28Z, run 38817312505) failed at
    "Publish knowledge-door deployment context to S3" with `unsupported context workflow`
    because the allowlists only knew the gateway, tools and chat names."""
    assert object_key(17, 42, 1, "context", "agent-context-deploy.yml") == ("deployment-evidence/v1/17/42/1/context/agent-context-deploy.yml.json")
    assert object_key(17, 42, 1, "release", "knowledge-door") == "deployment-evidence/v1/17/42/1/release/knowledge-door.json"
