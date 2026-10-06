"""Real Git histories and bounded storage doubles; never upload source to AWS."""

import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

import _release_path  # noqa: F401
from releases import operator_source as source


def command(*args, cwd=None):
    return subprocess.check_output(
        args, cwd=cwd, text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    command("git", "init", "-b", "main", str(root))
    commits = []
    for index in range(3):
        (root / "history.txt").write_text(str(index))
        command("git", "add", "history.txt", cwd=root)
        command(
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            f"commit {index}",
            cwd=root,
        )
        commits.append(command("git", "rev-parse", "HEAD", cwd=root))
    return root, commits


def manifest_fixture(tmp_path, repository):
    root, commits = repository
    args = SimpleNamespace(
        account="111122223333",
        region="us-east-1",
        environment="dev",
        source_sha=commits[-1],
        output=tmp_path / "operator-output",
        role_arn="arn:aws:iam::111122223333:role/selected-operator",
        role_id="AROA" + "A" * 17,
        manifest_version_id="manifest-version",
    )
    bucket, prefix = source.target(
        args.account, args.region, args.environment, args.source_sha
    )
    bundle = tmp_path / "source.bundle"
    source.make_bundle(root, args.source_sha, bundle)
    consumer = Path(source.__file__)
    manifest = {
        "version": 1,
        "repository": source.REPOSITORY,
        "repository_id": 123,
        "account": args.account,
        "region": args.region,
        "environment": args.environment,
        "source_sha": args.source_sha,
        "source_tree": source.git(root, "rev-parse", "HEAD^{tree}"),
        "main_observation": {
            "main_sha": args.source_sha,
            "merge_base_sha": args.source_sha,
            "status": "identical",
        },
        "producer": {
            "workflow": source.WORKFLOW,
            "role_arn": "arn:aws:iam::111122223333:role/adp-dev-trusted-build",
        },
    }
    objects = {}
    for field, path, directory, suffix in (
        ("bundle", bundle, "bundles", ".bundle"),
        ("consumer", consumer, "consumers", ".py"),
    ):
        digest = source.sha256(path)
        manifest[field] = {
            "key": prefix + "/" + directory + "/" + digest + suffix,
            "version_id": field + "-version",
            "sha256": digest,
            "bytes": path.stat().st_size,
        }
        objects[(manifest[field]["key"], field + "-version")] = path.read_bytes()
    raw = source.canonical(manifest)
    import hashlib

    args.manifest_sha256 = hashlib.sha256(raw).hexdigest()
    objects[
        (
            prefix + "/manifests/" + args.manifest_sha256 + ".json",
            args.manifest_version_id,
        )
    ] = raw
    return args, manifest, bucket, prefix, objects


class Storage:
    def __init__(self, args, objects):
        self.args, self.objects = args, objects
        self.calls = []

    def __call__(self, region, *parts):
        self.calls.append(parts)
        if parts[:2] == ("sts", "get-caller-identity"):
            return {
                "Account": self.args.account,
                "Arn": "arn:aws:sts::111122223333:assumed-role/selected-operator/session",
                "UserId": self.args.role_id + ":session",
            }
        if parts[:2] == ("iam", "get-role"):
            return {"Role": {"Arn": self.args.role_arn, "RoleId": self.args.role_id}}
        if parts[:2] == ("s3api", "get-bucket-versioning"):
            return {"Status": "Enabled"}
        if parts[:2] == ("s3api", "get-public-access-block"):
            return {
                "PublicAccessBlockConfiguration": {
                    key: True
                    for key in (
                        "BlockPublicAcls",
                        "IgnorePublicAcls",
                        "BlockPublicPolicy",
                        "RestrictPublicBuckets",
                    )
                }
            }
        if parts[:2] == ("s3api", "get-bucket-ownership-controls"):
            return {
                "OwnershipControls": {
                    "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
                }
            }
        if parts[:2] == ("s3api", "get-bucket-location"):
            return {"LocationConstraint": None}
        assert parts[0] == "s3api" and parts[1] in ("head-object", "get-object")
        assert parts[parts.index("--expected-bucket-owner") + 1] == self.args.account
        key = parts[parts.index("--key") + 1]
        version = parts[parts.index("--version-id") + 1]
        content = self.objects[(key, version)]
        if parts[1] == "get-object":
            Path(parts[-1]).write_bytes(content)
        return {
            "ContentLength": len(content),
            "VersionId": version,
            "ServerSideEncryption": "AES256",
        }


def test_bundle_contains_exact_real_history_without_credentials_or_unrelated_refs(
    tmp_path, repository
):
    root, commits = repository
    command(
        "git",
        "config",
        "remote.origin.url",
        "https://fixture-token@github.com/aws-e/adp",
        cwd=root,
    )
    command("git", "checkout", "-b", "unreviewed-private-branch", cwd=root)
    (root / "unreviewed.txt").write_text("must not be exported")
    command("git", "add", "unreviewed.txt", cwd=root)
    command(
        "git",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "unreviewed",
        cwd=root,
    )
    private_commit = command("git", "rev-parse", "HEAD", cwd=root)
    command("git", "checkout", "main", cwd=root)
    bundle, checkout = tmp_path / "source.bundle", tmp_path / "received"
    source.make_bundle(root, commits[-1], bundle)
    source.verify_bundle(bundle, commits[-1], checkout)
    assert source.git(checkout, "rev-list", "--count", "HEAD") == "3"
    assert source.git(checkout, "show", commits[0] + ":history.txt") == "0"
    assert "fixture-token" not in (checkout / ".git/config").read_text()
    with pytest.raises(source.SourceRefused):
        source.git(checkout, "cat-file", "-e", private_commit)


@pytest.mark.parametrize("problem", ["dirty", "shallow", "missing-object"])
def test_incomplete_or_dirty_source_cannot_be_packaged(tmp_path, repository, problem):
    root, commits = repository
    if problem == "dirty":
        (root / "history.txt").write_text("uncommitted")
    elif problem == "shallow":
        shallow = tmp_path / "shallow"
        command("git", "clone", "--depth=1", root.as_uri(), str(shallow))
        root = shallow
    else:
        blob = command("git", "rev-parse", commits[0] + ":history.txt", cwd=root)
        (root / ".git/objects" / blob[:2] / blob[2:]).unlink()
    with pytest.raises(source.SourceRefused):
        source.make_bundle(root, commits[-1], tmp_path / "refused.bundle")


def test_prerequisite_bundle_is_not_a_self_contained_release(tmp_path, repository):
    root, commits = repository
    command("git", "update-ref", source.REF, commits[-1], cwd=root)
    bundle = tmp_path / "thin.bundle"
    command(
        "git",
        "bundle",
        "create",
        "--version=2",
        str(bundle),
        source.REF,
        "--not",
        commits[0],
        cwd=root,
    )
    with pytest.raises(source.SourceRefused):
        source.verify_bundle(bundle, commits[-1], tmp_path / "refused")


def test_truncated_pack_is_refused_before_verified_checkout(tmp_path, repository):
    root, commits = repository
    bundle = tmp_path / "truncated.bundle"
    source.make_bundle(root, commits[-1], bundle)
    bundle.write_bytes(bundle.read_bytes()[:-100])
    with pytest.raises(source.SourceRefused):
        source.verify_bundle(bundle, commits[-1], tmp_path / "refused")


def test_selected_operator_retrieves_verified_source_without_github_token(
    tmp_path, repository, monkeypatch
):
    args, manifest, bucket, prefix, objects = manifest_fixture(tmp_path, repository)
    storage = Storage(args, objects)
    monkeypatch.setattr(source, "aws", storage)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    receipt = source.fetch(args)
    assert receipt["status"] == "operator-source-verified"
    assert source.git(Path(receipt["checkout"]), "rev-parse", "HEAD") == args.source_sha
    assert all(
        parts[1]
        in (
            "get-caller-identity",
            "get-role",
            "head-object",
            "get-object",
            "get-bucket-versioning",
            "get-public-access-block",
            "get-bucket-ownership-controls",
            "get-bucket-location",
        )
        for parts in storage.calls
    )


@pytest.mark.parametrize("field", ["manifest", "bundle"])
def test_storage_tampering_is_refused(tmp_path, repository, monkeypatch, field):
    args, manifest, bucket, prefix, objects = manifest_fixture(tmp_path, repository)
    for key in objects:
        if (field == "manifest" and "/manifests/" in key[0]) or (
            field == "bundle" and "/bundles/" in key[0]
        ):
            objects[key] += b"tamper"
    monkeypatch.setattr(source, "aws", Storage(args, objects))
    with pytest.raises(source.SourceRefused, match="integrity"):
        source.fetch(args)


@pytest.mark.parametrize(
    "problem",
    [
        "foreign-repo",
        "arbitrary-key",
        "unknown-publisher",
        "wrong-main",
        "oversize",
        "malformed",
    ],
)
def test_manifest_cannot_widen_source_target(tmp_path, repository, problem):
    args, manifest, bucket, prefix, objects = manifest_fixture(tmp_path, repository)
    manifest = copy.deepcopy(manifest)
    if problem == "foreign-repo":
        manifest["repository"] = "other/private"
    elif problem == "arbitrary-key":
        manifest["bundle"]["key"] = "some/other/private/object"
    elif problem == "unknown-publisher":
        manifest["producer"]["role_arn"] = "arn:aws:iam::111122223333:role/developer"
    elif problem == "wrong-main":
        manifest["main_observation"]["merge_base_sha"] = "0" * 40
    elif problem == "oversize":
        manifest["bundle"]["bytes"] = source.MAX_BUNDLE + 1
    else:
        manifest["bundle"] = []
    with pytest.raises(source.SourceRefused):
        source.validate_manifest(manifest, args, prefix)


def test_publish_cannot_run_from_untrusted_workflow_context(
    tmp_path, repository, monkeypatch
):
    root, commits = repository
    args = SimpleNamespace(
        account="111122223333",
        region="us-east-1",
        environment="dev",
        source_sha=commits[-1],
        root=root,
        output=tmp_path / "output",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    monkeypatch.setattr(
        source, "aws", lambda *args: pytest.fail("Must refuse before AWS")
    )
    with pytest.raises(source.SourceRefused, match="workflow"):
        source.publish(args)


def test_workflow_wrappers_keep_source_token_local_and_preserve_paid_cli():
    root = Path(__file__).resolve().parents[4]
    for name in ("superplane-operator-source.yml", "superplane-paid-worker-build.yml"):
        workflow = yaml.load(
            (root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
        )
        assert list(workflow["on"]) == ["workflow_dispatch"]
        job = next(iter(workflow["jobs"].values()))
        assert job["environment"] == "adp-build-dev"
        assert job["if"] == "github.ref == 'refs/heads/main'"
        assert job["permissions"] == {"contents": "read", "id-token": "write"}
        checkout = job["steps"][0]
        assert checkout["with"] == {"fetch-depth": "0", "persist-credentials": "false"}
        assert any(
            step.get("uses") == "./.github/actions/trusted-build"
            for step in job["steps"]
        )
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/upload-artifact"):
                assert (
                    "receipt" in step["with"]["name"]
                    and ".json" in step["with"]["path"]
                )
    paid = (root / ".github/workflows/superplane-paid-worker-build.yml").read_text()
    assert "releases/build_paid_worker.py" in paid
    assert '--source-sha "$GITHUB_SHA"' in paid
    assert "--python-image" in paid


def test_authorized_publisher_captures_main_proof_and_only_private_versioned_objects(
    tmp_path, repository, monkeypatch
):
    root, commits = repository
    consumer = root / "modules/domain-apps/superplane/releases/operator_source.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_bytes(Path(source.__file__).read_bytes())
    command("git", "add", ".", cwd=root)
    command(
        "git",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "source consumer",
        cwd=root,
    )
    sha = command("git", "rev-parse", "HEAD", cwd=root)
    args = SimpleNamespace(
        account="111122223333",
        region="us-east-1",
        environment="dev",
        source_sha=sha,
        root=root,
        output=tmp_path / "publication",
    )
    monkeypatch.setattr(source, "__file__", str(consumer))
    for name, value in {
        "GITHUB_REPOSITORY": source.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": sha,
        "GITHUB_WORKFLOW_REF": source.WORKFLOW,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GH_TOKEN": "fixture-token-never-export",
    }.items():
        monkeypatch.setenv(name, value)
    original = source.run
    github_calls = []

    def transport(args, **kwargs):
        if args[0] != "gh":
            return original(args, **kwargs)
        github_calls.append(args)
        if args[-1].endswith("/git/ref/heads/main"):
            return json.dumps({"object": {"sha": sha}})
        if "/compare/" in args[-1]:
            return json.dumps(
                {"status": "identical", "merge_base_commit": {"sha": sha}}
            )
        return json.dumps({"id": 123, "full_name": source.REPOSITORY})

    monkeypatch.setattr(source, "run", transport)
    uploaded = {}

    def aws(region, *parts):
        if parts[:2] == ("sts", "get-caller-identity"):
            return {
                "Account": args.account,
                "Arn": "arn:aws:sts::111122223333:assumed-role/adp-dev-trusted-build/run",
            }
        assert "--expected-bucket-owner" in parts
        operation = parts[1]
        if operation == "get-bucket-versioning":
            return {"Status": "Enabled"}
        if operation == "get-public-access-block":
            return {
                "PublicAccessBlockConfiguration": {
                    key: True
                    for key in (
                        "BlockPublicAcls",
                        "IgnorePublicAcls",
                        "BlockPublicPolicy",
                        "RestrictPublicBuckets",
                    )
                }
            }
        if operation == "get-bucket-ownership-controls":
            return {
                "OwnershipControls": {
                    "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
                }
            }
        if operation == "get-bucket-location":
            return {"LocationConstraint": None}
        assert operation == "put-object"
        assert parts[parts.index("--if-none-match") + 1] == "*"
        key = parts[parts.index("--key") + 1]
        uploaded[key] = Path(parts[parts.index("--body") + 1]).read_bytes()
        return {
            "VersionId": "version-" + str(len(uploaded)),
            "ServerSideEncryption": "AES256",
        }

    monkeypatch.setattr(source, "aws", aws)
    receipt = source.publish(args)
    assert receipt["status"] == "operator-source-published"
    assert len(uploaded) == 3
    assert all(
        key.startswith(f"superplane/releases/operator-source/dev/{sha}/")
        for key in uploaded
    )
    manifest = json.loads(uploaded[receipt["key"]])
    assert manifest["main_observation"] == {
        "status": "identical",
        "merge_base_sha": sha,
        "main_sha": sha,
    }
    assert any("/compare/" in args[-1] for args in github_calls)
    assert "fixture-token-never-export" not in json.dumps(receipt)
    assert "fixture-token-never-export" not in json.dumps(manifest)


def test_lost_conditional_upload_response_is_reconciled_by_exact_version_bytes(
    tmp_path, monkeypatch
):
    payload = tmp_path / "payload"
    payload.write_bytes(b"source fixture")
    calls = []

    def aws(region, *parts):
        calls.append(parts[1])
        if parts[1] == "put-object":
            raise source.SourceRefused("lost reply")
        if parts[1] == "get-object":
            Path(parts[-1]).write_bytes(payload.read_bytes())
        return {
            "VersionId": "original-version",
            "ServerSideEncryption": "AES256",
            "ContentLength": payload.stat().st_size,
        }

    monkeypatch.setattr(source, "aws", aws)
    result = source.put(
        "111122223333",
        "us-east-1",
        "adp-terraform-state-111122223333",
        "fixed-key",
        payload,
    )
    assert result["version_id"] == "original-version"
    assert calls == ["put-object", "head-object", "head-object", "get-object"]


def test_operator_role_replacement_refuses_before_object_reads(
    tmp_path, repository, monkeypatch
):
    args, manifest, bucket, prefix, objects = manifest_fixture(tmp_path, repository)
    storage = Storage(args, objects)

    def altered(region, *parts):
        result = storage(region, *parts)
        if parts[:2] == ("iam", "get-role"):
            result["Role"]["RoleId"] = "AROA" + "B" * 17
        return result

    monkeypatch.setattr(source, "aws", altered)
    with pytest.raises(source.SourceRefused, match="identity changed"):
        source.fetch(args)
    assert not any(parts[0] == "s3api" for parts in storage.calls)
