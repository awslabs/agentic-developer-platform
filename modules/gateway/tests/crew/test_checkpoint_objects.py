"""Use immutable version IDs and full-content hashes instead of S3 ETags."""

import base64
import hashlib
import io
import json
import tarfile
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.crew.checkpoint_objects import InvalidCheckpointObjectsError, VersionedCheckpointObjects, checkpoint_s3_client, valid_path
from src.shared.models.crew_checkpoint import CrewCheckpoint


class S3Fixture:
    def __init__(self):
        self.versioning = "Enabled"
        self.objects = {}
        self.gets = []
        self.deletes = []
        self.fail_manifest_delete = False

    def get_bucket_versioning(self, **kwargs):
        return {"Status": self.versioning}

    def generate_presigned_url(self, operation, **kwargs):
        assert kwargs["ExpiresIn"] == 300
        if operation == "put_object":
            assert kwargs["HttpMethod"] == "PUT" and kwargs["Params"]["ChecksumSHA256"]
        else:
            assert operation == "get_object" and kwargs["HttpMethod"] == "GET"
            assert kwargs["Params"]["VersionId"]
        # Shaped like a real SigV4 grant; the adapter refuses anything else (#8223 cause 45).
        return "https://s3.fixture/" + kwargs["Params"]["Key"] + "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-SignedHeaders=host"

    def head_object(self, **kwargs):
        if (kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"]) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Missing version"}}, "HeadObject")
        return {"VersionId": kwargs["VersionId"]}

    def delete_object(self, **kwargs):
        if self.fail_manifest_delete and kwargs["Key"].endswith("/manifest"):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "Injected failure"}}, "DeleteObject")
        self.deletes.append((kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"]))
        self.objects.pop(self.deletes[-1], None)
        return {}

    def get_object(self, **kwargs):
        assert kwargs["ChecksumMode"] == "ENABLED"
        self.gets.append((kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"]))
        content = self.objects[(kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"])]
        return {"Body": io.BytesIO(content), "VersionId": kwargs["VersionId"], "ETag": '"untrusted-etag"'}


def fixture(archive_name="lessons.jsonl"):
    data = b'{"provenance":"owner-approved"}\n'
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as output:
        item = tarfile.TarInfo(archive_name)
        item.size = len(data)
        output.addfile(item, io.BytesIO(data))
    archive = tar.getvalue()
    manifest = json.dumps(
        {
            "format": 1,
            "crew_version": "0.11.2",
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "files": {archive_name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}},
        }
    ).encode()
    return archive, manifest


@pytest.mark.parametrize(
    "path",
    [
        "workspace/.config/gh/hosts.yml",
        "sessions/.codex/auth.json",
        "artifacts/.git-credentials",
        ".kiro/sessions/cli/.adp/token",
        ".claude/.credentials.json",
        ".codex/auth.json",
        ".claude/model.env",
        ".codex/model.env",
        ".claude/adp-dev-box-shell/model.env",
        ".codex/adp-dev-box-shell/key",
    ],
)
def test_checkpoint_rejects_nested_shell_credentials(path):
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe archive inventory"):
        valid_path(path)
    valid_path("workspace/src/config.json")
    # Claude Code and Codex state (transcripts, settings, sessions) is owner continuity and is accepted (#8558).
    for kept in (".claude/settings.json", ".claude/projects/-mnt-crew-workspace/turn.jsonl", ".codex/sessions/s1.jsonl", ".codex/config.toml"):
        valid_path(kept)


def prepared(archive_name="lessons.jsonl"):
    s3 = S3Fixture()
    adapter = VersionedCheckpointObjects(s3, "fixture-bucket")
    checkpoint = CrewCheckpoint(id="candidate", owner_key="a" * 52)
    archive, manifest = fixture(archive_name)
    objects = {}
    for label, content in (("archive", archive), ("manifest", manifest)):
        objects[label] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    grant = adapter.grant(checkpoint, objects)
    for label, content in (("archive", archive), ("manifest", manifest)):
        setattr(checkpoint, label + "_bucket", grant[label]["bucket"])
        setattr(checkpoint, label + "_key", grant[label]["key"])
        setattr(checkpoint, label + "_sha256", objects[label]["sha256"])
        setattr(checkpoint, label + "_size", len(content))
        objects[label].update(bucket=grant[label]["bucket"], key=grant[label]["key"], version_id="first")
        s3.objects[(grant[label]["bucket"], grant[label]["key"], "first")] = content
    return adapter, s3, checkpoint, objects, grant


def test_checks_both_exact_versions_and_full_content_not_etag():
    adapter, s3, checkpoint, objects, grant = prepared()
    s3.objects[(grant["archive"]["bucket"], grant["archive"]["key"], "latest")] = b"wrong content"
    adapter.verify(checkpoint, objects)
    assert s3.gets == [("fixture-bucket", grant[label]["key"], "first") for label in ("archive", "manifest")]
    assert grant["archive"]["headers"].get("x-amz-checksum-sha256")
    objects["archive"]["version_id"] = "latest"
    with pytest.raises(InvalidCheckpointObjectsError, match="hash"):
        adapter.verify(checkpoint, objects)


def test_partial_upload_corruption_and_wrong_reference_never_verify():
    adapter, s3, checkpoint, objects, grant = prepared()
    s3.objects[(grant["manifest"]["bucket"], grant["manifest"]["key"], "first")] = b"broken manifest"
    with pytest.raises(InvalidCheckpointObjectsError, match="hash"):
        adapter.verify(checkpoint, objects)
    objects["archive"]["key"] = "crew-checkpoints/another-owner/candidate/archive"
    with pytest.raises(InvalidCheckpointObjectsError, match="unrecorded"):
        adapter.verify(checkpoint, objects)


@pytest.mark.parametrize("member", [".claude/model.env", ".codex/model.env"])
def test_matching_shell_credential_archive_is_refused(member):
    adapter, _, checkpoint, objects, _ = prepared(member)
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe"):
        adapter.verify(checkpoint, objects)


@pytest.mark.parametrize(
    "member",
    [
        ".codex/sessions/2026/10/23/rollout-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.jsonl",
        ".codex/owner-shell/sessions/2026/10/23/rollout-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.jsonl",
        "workspace/archive/rollout-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.jsonl",
    ],
)
def test_named_codex_rollout_archive_is_refused_even_with_matching_hashes(member):
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe archive inventory"):
        valid_path(member)
    adapter, _, checkpoint, objects, _ = prepared(member)
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe archive inventory"):
        adapter.verify(checkpoint, objects)


@pytest.mark.parametrize(
    "member",
    [
        ".codex/owner-shell/history.jsonl",
        ".codex/owner-shell/session_index.jsonl",
        ".codex/owner-shell/config.toml",
        ".codex/owner-shell/sessions/2026/10/23/metadata-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.json",
        ".codex/sessions/s1.jsonl",
    ],
)
def test_owner_codex_metadata_archive_is_accepted(member):
    valid_path(member)
    adapter, _, checkpoint, objects, _ = prepared(member)
    adapter.verify(checkpoint, objects)


@pytest.mark.parametrize("member", [".claude/adp-dev-box-shell/key", ".codex/adp-dev-box-shell/config.json"])
def test_matching_copied_runtime_directory_archive_is_refused(member):
    adapter, _, checkpoint, objects, _ = prepared(member)
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe"):
        adapter.verify(checkpoint, objects)


def test_even_matching_malicious_archive_path_is_refused():
    adapter, _, checkpoint, objects, _ = prepared("../outside")
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe"):
        adapter.verify(checkpoint, objects)


@pytest.mark.parametrize("member", ["adp-crew-session-map/1234.json", ".kiro/sessions/cli/turn.jsonl", "workspace/memory/preferences.md"])
def test_owner_continuity_members_the_host_helper_archives_verify(member):
    # The host helper (crew_checkpoint.py) keeps exactly these trees; the ADP session map was archived by the
    # helper but refused here, so a home with one mapped session never verified (#8223 cause 37 follow-up).
    adapter, _, checkpoint, objects, _ = prepared(member)
    adapter.verify(checkpoint, objects)


@pytest.mark.parametrize("member", [".kiro/agents/kirocrew.json", ".kiro/settings/entry.json", "connections/github.json", "trust/sel_hmac.key"])
def test_box_rendered_and_unlisted_members_are_still_refused(member):
    adapter, _, checkpoint, objects, _ = prepared(member)
    with pytest.raises(InvalidCheckpointObjectsError, match="unsafe"):
        adapter.verify(checkpoint, objects)


def test_disabled_bucket_versioning_fails_before_grants():
    s3 = S3Fixture()
    s3.versioning = "Suspended"
    with pytest.raises(InvalidCheckpointObjectsError, match="versioned"):
        VersionedCheckpointObjects(s3, "fixture-bucket").grant(
            CrewCheckpoint(id="candidate", owner_key="a" * 52), {label: {"sha256": "a" * 64, "size": 12} for label in ("archive", "manifest")}
        )


def test_late_helper_put_cannot_erase_accepted_s3_version():
    import base64

    from moto import mock_aws

    archive, manifest = fixture()
    checkpoint = CrewCheckpoint(id="accepted", owner_key="a" * 52)
    with mock_aws():
        s3 = checkpoint_s3_client("us-east-1")
        s3.create_bucket(Bucket="fixture-bucket")
        s3.put_bucket_versioning(Bucket="fixture-bucket", VersioningConfiguration={"Status": "Enabled"})
        signed = []
        presign = s3.generate_presigned_url

        def signed_url(operation, **kwargs):
            signed.append((operation, kwargs["HttpMethod"]))
            return presign(operation, **kwargs)

        s3.generate_presigned_url = signed_url
        adapter = VersionedCheckpointObjects(s3, "fixture-bucket")
        uploads = {
            label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for label, content in (("archive", archive), ("manifest", manifest))
        }
        grants = adapter.grant(checkpoint, uploads)
        assert signed == [("put_object", "PUT"), ("put_object", "PUT")]
        receipts = {}
        for label, content in (("archive", archive), ("manifest", manifest)):
            item = grants[label]
            checksum = base64.b64encode(bytes.fromhex(uploads[label]["sha256"])).decode()
            accepted = s3.put_object(Bucket=item["bucket"], Key=item["key"], Body=content, ChecksumSHA256=checksum)
            for field in ("bucket", "key", "sha256", "size"):
                setattr(checkpoint, f"{label}_{field}", item[field] if field in item else uploads[label][field])
            receipts[label] = {**uploads[label], "bucket": item["bucket"], "key": item["key"], "version_id": accepted["VersionId"]}
        adapter.verify(checkpoint, receipts)
        late = s3.put_object(Bucket=grants["archive"]["bucket"], Key=grants["archive"]["key"], Body=b"late-put")
        assert late["VersionId"] != receipts["archive"]["version_id"]
        assert (
            s3.get_object(Bucket=grants["archive"]["bucket"], Key=grants["archive"]["key"], VersionId=receipts["archive"]["version_id"])[
                "Body"
            ].read()
            == archive
        )
        adapter.verify(checkpoint, receipts)
        assert all(grants[label]["url"].startswith("https://") for label in grants)
        assert all(grants[label]["headers"].get("x-amz-checksum-sha256") for label in grants)


def test_retention_removes_only_recorded_versions_and_retry_keeps_newer_versions():
    import base64

    from botocore.exceptions import ClientError
    from moto import mock_aws

    archive, manifest = fixture()
    checkpoint = CrewCheckpoint(id="retained", owner_key="a" * 52)
    with mock_aws():
        s3 = checkpoint_s3_client("us-east-1")
        s3.create_bucket(Bucket="fixture-bucket")
        s3.put_bucket_versioning(Bucket="fixture-bucket", VersioningConfiguration={"Status": "Enabled"})
        adapter = VersionedCheckpointObjects(s3, "fixture-bucket")
        uploads = {
            label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for label, content in (("archive", archive), ("manifest", manifest))
        }
        grants = adapter.grant(checkpoint, uploads)
        versions = {}
        for label, content in (("archive", archive), ("manifest", manifest)):
            item = grants[label]
            checksum = base64.b64encode(bytes.fromhex(uploads[label]["sha256"])).decode()
            versions[label] = s3.put_object(Bucket=item["bucket"], Key=item["key"], Body=content, ChecksumSHA256=checksum)["VersionId"]
            for field in ("bucket", "key"):
                setattr(checkpoint, f"{label}_{field}", item[field])
            setattr(checkpoint, f"{label}_version_id", versions[label])
        late = s3.put_object(Bucket="fixture-bucket", Key=grants["archive"]["key"], Body=b"newer-local")
        adapter.delete_recorded(checkpoint)
        adapter.delete_recorded(checkpoint)
        with pytest.raises(ClientError) as error:
            s3.get_object(Bucket="fixture-bucket", Key=grants["archive"]["key"], VersionId=versions["archive"])
        assert error.value.response["Error"]["Code"] in ("NoSuchVersion", "NoSuchKey", "404")
        assert s3.get_object(Bucket="fixture-bucket", Key=grants["archive"]["key"], VersionId=late["VersionId"])["Body"].read() == b"newer-local"


def test_checkpoint_client_presigns_sigv4_with_the_checksum_header_signed():
    """#8223 cause 45: ``boto3.client("s3")`` in us-east-1 presigns SigV2, which S3 refused with 403
    SignatureDoesNotMatch on the KMS-encrypted checkpoint bucket, so every host upload failed. The
    client the route uses must sign V4 and bind the checksum header into the signature."""
    with mock_aws():
        s3 = checkpoint_s3_client("us-east-1")
        s3.create_bucket(Bucket="fixture-bucket")
        s3.put_bucket_versioning(Bucket="fixture-bucket", VersioningConfiguration={"Status": "Enabled"})
        checkpoint = CrewCheckpoint(id="signed", owner_key="a" * 52)
        archive, manifest = fixture()
        grants = VersionedCheckpointObjects(s3, "fixture-bucket").grant(
            checkpoint,
            {
                label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                for label, content in (("archive", archive), ("manifest", manifest))
            },
        )
    contents = {"archive": archive, "manifest": manifest}
    for label, grant in grants.items():
        query = parse_qs(urlparse(grant["url"]).query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert "x-amz-checksum-sha256" in query["X-Amz-SignedHeaders"][0].split(";")
        assert grant["headers"] == {"x-amz-checksum-sha256": base64.b64encode(hashlib.sha256(contents[label]).digest()).decode()}


def test_a_sigv2_client_can_never_hand_out_a_checkpoint_grant():
    """The default boto3 S3 client in us-east-1 still signs V2; the adapter refuses its URLs instead
    of letting the host discover the 403 on upload."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="fixture-bucket")
        s3.put_bucket_versioning(Bucket="fixture-bucket", VersioningConfiguration={"Status": "Enabled"})
        archive, manifest = fixture()
        with pytest.raises(InvalidCheckpointObjectsError, match="SigV4"):
            VersionedCheckpointObjects(s3, "fixture-bucket").grant(
                CrewCheckpoint(id="legacy", owner_key="a" * 52),
                {
                    label: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                    for label, content in (("archive", archive), ("manifest", manifest))
                },
            )


def test_matching_codex_history_metadata_and_rollout_inventory_refuses_only_rollout():
    adapter, s3, checkpoint, objects, _ = prepared()
    metadata_path = ".codex/owner-shell/sessions/2026/10/23/metadata-2026-10-23T00-00-00-session-one.json"
    kept = {
        ".codex/owner-shell/history.jsonl": b'{"session_id":"session-one","ts":1792713600,"text":"hello"}\n',
        metadata_path: b'{"id":"session-one","timestamp":"2026-10-23T00:00:00Z"}',
    }
    rollout = ".codex/owner-shell/sessions/2026/10/23/rollout-2026-10-23T00-00-00-session-one.jsonl"
    for include_rollout in (True, False):
        members = kept | ({rollout: b'{"type":"response_item","payload":"fixture"}\n'} if include_rollout else {})
        tar = io.BytesIO()
        with tarfile.open(fileobj=tar, mode="w") as source:
            for name, content in members.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                source.addfile(member, io.BytesIO(content))
        archive = tar.getvalue()
        manifest = json.dumps(
            {
                "format": 1,
                "crew_version": "0.11.2",
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)} for name, content in members.items()},
            }
        ).encode()
        for label, content in (("archive", archive), ("manifest", manifest)):
            objects[label].update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
            setattr(checkpoint, label + "_sha256", objects[label]["sha256"])
            setattr(checkpoint, label + "_size", len(content))
            s3.objects[(objects[label]["bucket"], objects[label]["key"], "first")] = content
        if include_rollout:
            with pytest.raises(InvalidCheckpointObjectsError, match="unsafe archive inventory"):
                adapter.verify(checkpoint, objects)
        else:
            adapter.verify(checkpoint, objects)


def test_actual_capture_with_large_codex_rollout_verifies_and_indexes_metadata(tmp_path):
    import importlib.util
    from pathlib import Path

    helper_path = Path(__file__).resolve().parents[3] / "agent-factory" / "agent" / "scripts" / "crew_checkpoint.py"
    specification = importlib.util.spec_from_file_location("capture_contract_helper", helper_path)
    assert specification and specification.loader
    helper = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(helper)

    home = tmp_path / "crew"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "dashboard_chat-fixture.jsonl").write_text(
        '{"role":"user","ts":"2026-10-23T00:00:00Z","title":"Captured chat","text":"fixture body"}\n'
    )
    owner = tmp_path / "owner"
    codex = owner / ".codex"
    codex.mkdir(parents=True, mode=0o700)
    owner.chmod(0o700)
    codex.chmod(0o700)
    session_id = "11111111-2222-3333-4444-555555555555"
    (codex / "history.jsonl").write_text(json.dumps({"session_id": session_id, "ts": 1792713600, "text": "Captured prompt"}) + "\n")
    sessions = codex / "sessions" / "2026" / "10" / "23"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-2026-10-23T00-00-00-{session_id}.jsonl"
    with rollout.open("wb") as writer:
        writer.write(json.dumps({"type": "session_meta", "payload": {"id": session_id, "timestamp": "2026-10-23T00:00:00Z"}}).encode() + b"\n")
        writer.truncate(200 * 1024 * 1024)
    archive_file = tmp_path / "archive.tar"
    manifest_file = tmp_path / "manifest.json"
    manifest = helper.capture(home, archive_file, manifest_file, owner_home=owner)
    metadata_path = f".codex/owner-shell/sessions/2026/10/23/metadata-2026-10-23T00-00-00-{session_id}.json"
    assert metadata_path in manifest["files"]
    assert ".codex/owner-shell/history.jsonl" in manifest["files"]
    assert f".codex/owner-shell/sessions/2026/10/23/{rollout.name}" not in manifest["files"]
    assert archive_file.stat().st_size < 8 * 1024 * 1024

    adapter, s3, checkpoint, objects, _ = prepared()
    for label, content in (("archive", archive_file.read_bytes()), ("manifest", manifest_file.read_bytes())):
        objects[label].update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
        setattr(checkpoint, label + "_sha256", objects[label]["sha256"])
        setattr(checkpoint, label + "_size", len(content))
        s3.objects[(objects[label]["bucket"], objects[label]["key"], "first")] = content
    adapter.verify(checkpoint, objects)
    headers = adapter.conversation_headers(objects)
    assert [(row["source"], row["title"], row["unreadable"]) for row in headers] == [
        ("kiro_crew", "Captured chat", False),
        ("codex", "Captured prompt", False),
    ]
