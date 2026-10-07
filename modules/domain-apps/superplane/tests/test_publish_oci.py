"""Offline publication contract tests: registry mutation is always a fake."""

import gzip
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "publish_oci", Path(__file__).parents[1] / "releases" / "publish_oci.py"
)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
ACCOUNT = "123456789012"
REGION = "us-east-1"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


@pytest.fixture
def artifact(tmp_path):
    layout = tmp_path / "oci"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def put(value, media):
        data = json.dumps(value).encode() if not isinstance(value, bytes) else value
        sha = publisher.digest(data)
        (blobs / sha[7:]).write_bytes(data)
        return {"mediaType": media, "digest": sha, "size": len(data)}

    config = put(
        {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {
                "type": "layers",
                "diff_ids": [publisher.digest(b"exact compressed layer")],
            },
            "config": {
                "User": "0",
                "Cmd": ["python3"],
                "Env": ["PYTHON_VERSION=3.12.15"],
            },
        },
        "application/vnd.oci.image.config.v1+json",
    )
    layer = put(
        gzip.compress(b"exact compressed layer"),
        "application/vnd.oci.image.layer.v1.tar+gzip",
    )
    manifest = put(
        {
            "schemaVersion": 2,
            "mediaType": publisher.MANIFEST,
            "config": config,
            "layers": [layer],
        },
        publisher.MANIFEST,
    )
    index = put(
        {"schemaVersion": 2, "mediaType": publisher.INDEX, "manifests": [manifest]},
        publisher.INDEX,
    )
    (layout / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [index]})
    )
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    review = tmp_path / "review.json"
    review.write_text('{"review": "external evidence, not automatic approval"}')
    args = SimpleNamespace(
        kind="python-base",
        account=ACCOUNT,
        region=REGION,
        repository="adp-superplane-api",
        layout=layout,
        platform_digest=manifest["digest"],
        config_digest=config["digest"],
        review_evidence=review,
        review_sha256=publisher.digest(review.read_bytes()),
        receipt=tmp_path / "receipt.json",
        publish=True,
    )
    return args, put, layer


class Registry:
    def __init__(self, args):
        self.args = args
        self.present = False
        self.auth_calls = 0
        self.policy = {"rules": [{"selection": {"tagStatus": "untagged"}}]}
        self.image_digest = args.platform_digest
        self.raw = (
            args.layout / "blobs" / "sha256" / self.image_digest[7:]
        ).read_text()
        self.repo = {
            "registryId": ACCOUNT,
            "repositoryName": args.repository,
            "repositoryArn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{args.repository}",
            "repositoryUri": f"{REGISTRY}/{args.repository}",
            "imageTagMutability": "IMMUTABLE",
        }

    def describe_repositories(self, **kwargs):
        return {"repositories": [self.repo]}

    def get_lifecycle_policy(self, **kwargs):
        return {"lifecyclePolicyText": json.dumps(self.policy)}

    def describe_images(self, **kwargs):
        if not self.present:
            raise AwsError("ImageNotFoundException")
        return {"imageDetails": [{"imageDigest": self.image_digest}]}

    def batch_get_image(self, **kwargs):
        return {"images": [{"imageManifest": self.raw}]}

    def get_authorization_token(self, **kwargs):
        self.auth_calls += 1
        return {
            "authorizationData": [
                {"proxyEndpoint": f"https://{REGISTRY}", "authorizationToken": "SECRET"}
            ]
        }


STS = SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT})


def no_run(*args, **kwargs):
    pytest.fail("Existing/prepared/refused publication must not invoke OCI transport")


def test_prepare_is_local_and_binds_evidence(artifact):
    args, _, _ = artifact
    args.publish = False
    result = publisher.publish(args, run=no_run)
    assert result["status"] == "prepared"
    assert result["review_evidence_sha256"] == args.review_sha256
    assert "registry_manifest_verified" not in result
    assert args.receipt.stat().st_mode & 0o777 == 0o600


def test_exact_existing_tag_is_reused_without_credentials(artifact):
    args, _, _ = artifact
    ecr = Registry(args)
    ecr.present = True
    result = publisher.publish(args, sts=STS, ecr=ecr, run=no_run)
    assert result["status"] == "reused"
    assert result["registry_manifest_verified"] is True
    assert ecr.auth_calls == 0


def test_new_upload_uses_private_exact_platform_and_no_secret_in_receipt(artifact):
    args, _, _ = artifact
    ecr = Registry(args)
    source_index = (args.layout / "index.json").read_bytes()
    auth_paths = []

    def run(command, **kwargs):
        assert "SECRET" not in str(command)
        if command == ["skopeo", "--version"]:
            return SimpleNamespace(stdout="skopeo version test")
        assert command[:3] == ["skopeo", "copy", "--preserve-digests"]
        authfile = Path(command[4])
        auth_paths.append(authfile)
        assert authfile.stat().st_mode & 0o777 == 0o600
        assert authfile.parent.stat().st_mode & 0o777 == 0o700
        assert json.loads(authfile.read_text())["auths"][REGISTRY]["auth"] == "SECRET"
        selected = Path(command[5][4:].removesuffix(":reviewed"))
        _, _, raw = publisher.inspect(
            selected, args.platform_digest, args.config_digest, args.kind
        )
        assert raw.decode() == ecr.raw
        assert json.loads(args.receipt.read_text())["status"] == "publication_attempted"
        assert command[-1].endswith(f":python-base-{args.platform_digest[7:]}")
        ecr.present = True
        return SimpleNamespace(returncode=0)

    assert publisher.publish(args, sts=STS, ecr=ecr, run=run)["status"] == "published"
    assert (args.layout / "index.json").read_bytes() == source_index
    assert not auth_paths[0].exists()
    assert "SECRET" not in args.receipt.read_text()


@pytest.mark.parametrize(
    "failure",
    [
        "collision",
        "manifest",
        "retention",
        "mutable",
        "account",
        "denied",
        "repository",
    ],
)
def test_closed_registry_failures(artifact, failure):
    args, _, _ = artifact
    ecr = Registry(args)
    sts = STS
    ecr.present = True
    if failure == "collision":
        ecr.image_digest = "sha256:" + "0" * 64
    elif failure == "manifest":
        ecr.raw += " "
    elif failure == "retention":
        ecr.policy["rules"][0]["selection"]["tagStatus"] = "any"
    elif failure == "mutable":
        ecr.repo["imageTagMutability"] = "MUTABLE"
    elif failure == "account":
        sts = SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})
    elif failure == "repository":
        ecr.repo["repositoryArn"] += "-other"
    else:

        def denied(**kwargs):
            raise AwsError("AccessDeniedException")

        ecr.describe_images = denied
    with pytest.raises((ValueError, AwsError)):
        publisher.publish(args, sts=sts, ecr=ecr, run=no_run)
    assert json.loads(args.receipt.read_text())["status"] == "blocked"
    assert ecr.auth_calls == 0


@pytest.mark.parametrize(
    "failure", ["layer", "review", "unreachable", "destination", "config", "diagnostic"]
)
def test_invalid_local_inputs_refused(artifact, failure):
    args, put, layer = artifact
    if failure == "layer":
        (args.layout / "blobs" / "sha256" / layer["digest"][7:]).write_bytes(
            b"tampered"
        )
    elif failure == "review":
        args.review_evidence.write_text("tampered")
    elif failure == "destination":
        args.repository = "adp-gateway"
    elif failure == "unreachable":
        (args.layout / "index.json").write_text('{"manifests":[]}')
    elif failure == "config":
        args.config_digest = "sha256:" + "0" * 64
    else:
        config = put(
            {
                "os": "linux",
                "architecture": "amd64",
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [publisher.digest(b"exact compressed layer")],
                },
                "config": {"Labels": {"com.adp.local-diagnostic": "false"}},
            },
            "application/vnd.oci.image.config.v1+json",
        )
        manifest = put(
            {
                "schemaVersion": 2,
                "mediaType": publisher.MANIFEST,
                "config": config,
                "layers": [layer],
            },
            publisher.MANIFEST,
        )
        (args.layout / "index.json").write_text(json.dumps({"manifests": [manifest]}))
        args.platform_digest, args.config_digest = manifest["digest"], config["digest"]
    with pytest.raises(ValueError):
        publisher.publish(args, sts=STS, ecr=Registry(args), run=no_run)
    assert not args.receipt.exists()


def test_transport_failure_is_uncertain_and_retry_reconciles(artifact):
    args, _, _ = artifact
    ecr = Registry(args)

    def run(command, **kwargs):
        if command == ["skopeo", "--version"]:
            return SimpleNamespace(stdout="skopeo version test")
        ecr.present = True  # Upload completed before connection was lost.
        return SimpleNamespace(returncode=1, stderr="SECRET")

    with pytest.raises(RuntimeError, match="OCI copy failed"):
        publisher.publish(args, sts=STS, ecr=ecr, run=run)
    assert json.loads(args.receipt.read_text())["status"] == "outcome_uncertain"
    assert "SECRET" not in args.receipt.read_text()
    with pytest.raises(ValueError, match="new receipt"):
        publisher.publish(args, sts=STS, ecr=ecr, run=no_run)
    args.receipt = args.receipt.with_name("retry.json")
    assert publisher.publish(args, sts=STS, ecr=ecr, run=no_run)["status"] == "reused"


def test_skypilot_uses_same_byte_preserving_path(artifact):
    args, _, _ = artifact
    args.kind, args.repository = "skypilot", "adp-superplane-skypilot"
    ecr = Registry(args)
    ecr.present = True
    result = publisher.publish(args, sts=STS, ecr=ecr, run=no_run)
    assert result["tag"] == f"skypilot-{args.platform_digest[7:]}"


def test_uncompressed_skypilot_layer_is_preserved(artifact):
    args, put, _ = artifact
    args.kind, args.repository = "skypilot", "adp-superplane-skypilot"
    layer = put(b"exact compressed layer", publisher.TAR_LAYER)
    config_path = args.layout / "blobs" / "sha256" / args.config_digest[7:]
    config = {
        "mediaType": publisher.CONFIG,
        "digest": args.config_digest,
        "size": config_path.stat().st_size,
    }
    manifest = put(
        {
            "schemaVersion": 2,
            "mediaType": publisher.MANIFEST,
            "config": config,
            "layers": [layer],
        },
        publisher.MANIFEST,
    )
    (args.layout / "index.json").write_text(json.dumps({"manifests": [manifest]}))
    args.platform_digest = manifest["digest"]
    ecr = Registry(args)
    ecr.present = True
    assert publisher.publish(args, sts=STS, ecr=ecr, run=no_run)["status"] == "reused"


def test_final_receipt_failure_preserves_uncertain_outcome(artifact, monkeypatch):
    args, _, _ = artifact
    ecr = Registry(args)
    original = publisher.receipt_write

    def write(path, receipt, **kwargs):
        if receipt["status"] == "published":
            raise OSError("disk temporarily unavailable")
        return original(path, receipt, **kwargs)

    def run(command, **kwargs):
        if command == ["skopeo", "--version"]:
            return SimpleNamespace(stdout="skopeo version test")
        ecr.present = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(publisher, "receipt_write", write)
    with pytest.raises(OSError):
        publisher.publish(args, sts=STS, ecr=ecr, run=run)
    assert json.loads(args.receipt.read_text())["status"] == "outcome_uncertain"


def test_receipt_reservation_is_exclusive(tmp_path):
    path = tmp_path / "receipt.json"
    publisher.receipt_write(path, {"status": "original"}, create=True)
    with pytest.raises(FileExistsError):
        publisher.receipt_write(path, {"status": "replacement"}, create=True)
    assert json.loads(path.read_text())["status"] == "original"


@pytest.mark.parametrize(
    "failure", ["diffid", "external", "config-media", "layer-media"]
)
def test_invalid_oci_structure_refused(artifact, failure):
    args, put, layer = artifact
    config = json.loads(
        (args.layout / "blobs" / "sha256" / args.config_digest[7:]).read_bytes()
    )
    if failure == "diffid":
        config["rootfs"]["diff_ids"] = ["sha256:" + "0" * 64]
    descriptor = put(config, "other" if failure == "config-media" else publisher.CONFIG)
    if failure == "external":
        layer["urls"] = ["https://example.com/blob"]
    if failure == "layer-media":
        layer["mediaType"] = "other"
    manifest = put(
        {
            "schemaVersion": 2,
            "mediaType": publisher.MANIFEST,
            "config": descriptor,
            "layers": [layer],
        },
        publisher.MANIFEST,
    )
    (args.layout / "index.json").write_text(json.dumps({"manifests": [manifest]}))
    args.platform_digest, args.config_digest = manifest["digest"], descriptor["digest"]
    with pytest.raises(ValueError):
        publisher.publish(args, sts=STS, ecr=Registry(args), run=no_run)


def test_real_skopeo_preserves_selected_platform_offline(artifact):
    import shutil
    import subprocess

    if not shutil.which("skopeo"):
        pytest.skip("Install Skopeo to exercise offline OCI transport")
    args, _, _ = artifact
    ecr = Registry(args)
    destination = args.layout.parent / "transported"

    def run(command, **kwargs):
        if command == ["skopeo", "--version"]:
            return subprocess.run(command, **kwargs)
        local = [*command[:-1], f"oci:{destination}:copied"]
        result = subprocess.run(local, **kwargs)
        if result.returncode == 0:
            _, _, raw = publisher.inspect(
                destination, args.platform_digest, args.config_digest, args.kind
            )
            ecr.raw, ecr.present = raw.decode(), True
        return result

    assert publisher.publish(args, sts=STS, ecr=ecr, run=run)["status"] == "published"
