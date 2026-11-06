"""Receiver publication/configuration checks with local Git and mocked provider/Docker calls."""

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = load("publish-emitter-image")
reader = load("read-receiver-config")
ACCOUNT = "000000000000"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/reviewed-release-role"
CONFIG_RAW = b'{"architecture":"amd64","os":"linux"}'
CONFIG_DIGEST = "sha256:" + hashlib.sha256(CONFIG_RAW).hexdigest()
PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    + base64.b64encode(bytes.fromhex("302a300506032b6570032100") + b"x" * 32).decode()
    + "\n-----END PUBLIC KEY-----\n"
)
URL = "http://bedrockgateway.adp-gateway.svc.cluster.local:80"


class FakeRuntime:
    def __init__(self, args):
        self.args = args
        self.calls = []
        self.invocations = []
        self.docker_configurations = []
        self.temporary_paths = []
        self.overrides = {}
        self.context = None
        self.image_present = False
        self.push_failure = False
        self.push_persists = True
        self.read_failure_after_push = False
        self.pushed = False
        self.manifest_config = CONFIG_DIGEST
        self.inspected_config = CONFIG_DIGEST
        self.manifest_digest_override = None
        self.archive_fault = None
        self.login_failure = False
        self.public_auth = False
        self.descriptor = None
        self.build_manifest = None
        self.build_layer = None

    def use_manifest_image_id(self):
        self.build_layer = b"reviewed layer bytes"
        layer_digest = "sha256:" + hashlib.sha256(self.build_layer).hexdigest()
        self.build_manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": publisher.MANIFEST_TYPES[0],
                "config": {"digest": CONFIG_DIGEST, "size": len(CONFIG_RAW)},
                "layers": [{"digest": layer_digest, "size": len(self.build_layer)}],
            }
        ).encode()
        self.inspected_config = (
            "sha256:" + hashlib.sha256(self.build_manifest).hexdigest()
        )
        self.descriptor = {
            "mediaType": publisher.MANIFEST_TYPES[0],
            "digest": self.inspected_config,
            "size": len(self.build_manifest),
            "annotations": {"config.digest": CONFIG_DIGEST},
        }

    def push(self, argv):
        pending = Path(str(self.args.output) + ".pending.json")
        assert pending.is_file() and pending.stat().st_mode & 0o077 == 0
        assert json.loads(pending.read_text())["config_digest"] == CONFIG_DIGEST
        self.image_present = self.push_persists
        self.pushed = True
        if self.push_failure:
            raise subprocess.CalledProcessError(1, argv)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.invocations.append((argv, kwargs))
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        value = None
        if argv[0] == "aws":
            assert argv[argv.index("--region") + 1] == REGION
            action = tuple(argv[1:3])
            if action in self.overrides:
                value = self.overrides[action]
            elif action == ("sts", "get-caller-identity"):
                value = {
                    "Account": ACCOUNT,
                    "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/reviewed-release-role/session",
                }
            elif action == ("ssm", "get-parameter"):
                name = "/adp/dev/platform/account-id"
                assert argv[argv.index("--name") + 1] == name
                value = {
                    "Parameter": {
                        "Name": name,
                        "Value": ACCOUNT,
                        "ARN": f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{name}",
                    }
                }
            elif action == ("ecr", "describe-repositories"):
                value = {
                    "repositories": [
                        {
                            "repositoryName": "adp-dev-insights-emitter",
                            "registryId": ACCOUNT,
                            "repositoryUri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-insights-emitter",
                            "repositoryArn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/adp-dev-insights-emitter",
                            "imageTagMutability": "IMMUTABLE",
                        }
                    ]
                }
            elif action == ("ecr", "batch-get-image"):
                assert argv[argv.index("--registry-id") + 1] == ACCOUNT
                assert (
                    argv[argv.index("--repository-name") + 1]
                    == "adp-dev-insights-emitter"
                )
                tag = "source-" + self.args.source_revision
                assert argv[argv.index("--image-ids") + 1] == "imageTag=" + tag
                if self.read_failure_after_push and self.pushed:
                    raise subprocess.CalledProcessError(1, argv)
                if not self.image_present:
                    value = {
                        "images": [],
                        "failures": [
                            {
                                "failureCode": "ImageNotFound",
                                "imageId": {"imageTag": tag},
                            }
                        ],
                    }
                else:
                    raw = json.dumps(
                        {
                            "schemaVersion": 2,
                            "mediaType": publisher.MANIFEST_TYPES[0],
                            "config": {"digest": self.manifest_config},
                        }
                    )
                    digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
                    value = {
                        "images": [
                            {
                                "registryId": ACCOUNT,
                                "repositoryName": "adp-dev-insights-emitter",
                                "imageId": {
                                    "imageTag": tag,
                                    "imageDigest": self.manifest_digest_override
                                    or digest,
                                },
                                "imageManifest": raw,
                                "imageManifestMediaType": publisher.MANIFEST_TYPES[0],
                            }
                        ],
                        "failures": [],
                    }
            elif action == ("ecr", "get-login-password"):
                return subprocess.CompletedProcess(argv, 0, "fixture-token-never-log\n")
            else:
                raise AssertionError(argv)
        elif argv[0] == "docker":
            assert argv[1] == "--config"
            assert Path(argv[2]).stat().st_mode & 0o077 == 0
            config = Path(argv[2]) / "config.json"
            if config.exists():
                assert config.stat().st_mode & 0o077 == 0
            self.docker_configurations.append(
                json.loads(config.read_text()) if config.exists() else {}
            )
            action = argv[3]
            if action == "build":
                assert argv[argv.index("--platform") + 1] == "linux/amd64"
                assert (
                    argv[argv.index("--file") + 1]
                    == "modules/insights/emitter/Dockerfile"
                )
                assert argv[-1] == "-"
                assert "--load" in argv and "--provenance=false" in argv
                self.context = kwargs["stdin"].read()
            elif action == "image":
                if argv[4] == "save":
                    assert argv[5] == self.inspected_config
                    if self.archive_fault == "save":
                        raise subprocess.CalledProcessError(1, argv)
                    output = kwargs["stdout"]
                    assert os.fstat(output.fileno()).st_mode & 0o077 == 0
                    with tarfile.open(fileobj=output, mode="w") as saved:
                        config = (
                            CONFIG_RAW if self.archive_fault != "config" else b"changed"
                        )
                        manifest = [
                            {"Config": "config.json", "RepoTags": None, "Layers": []}
                        ]
                        layer_name = None
                        if self.build_layer is not None:
                            layer_name = (
                                "blobs/sha256/"
                                + hashlib.sha256(self.build_layer).hexdigest()
                            )
                            manifest[0]["Layers"] = [layer_name]
                        if self.archive_fault == "multiple":
                            manifest *= 2
                        elif self.archive_fault == "layers":
                            manifest[0]["Layers"] = ["unattributed-layer"]
                        entries = [
                            ("manifest.json", json.dumps(manifest).encode()),
                            ("config.json", config),
                        ]
                        if layer_name:
                            entries.append(
                                (
                                    layer_name,
                                    b"x" * len(self.build_layer)
                                    if self.archive_fault == "layer-digest"
                                    else self.build_layer,
                                )
                            )
                        if self.build_manifest is not None:
                            entries.append(
                                (
                                    "blobs/sha256/"
                                    + self.inspected_config.removeprefix("sha256:"),
                                    self.build_manifest,
                                )
                            )
                        for name, data in entries:
                            entry = tarfile.TarInfo(name)
                            entry.size = len(data)
                            if name == "config.json" and self.archive_fault == "link":
                                entry.type = tarfile.SYMTYPE
                                entry.linkname = "/unreadable/credential"
                                entry.size = 0
                            saved.addfile(entry, io.BytesIO(data))
                            if (
                                name == "config.json"
                                and self.archive_fault == "duplicate"
                            ):
                                saved.addfile(entry, io.BytesIO(data))
                    return subprocess.CompletedProcess(argv, 0, "")
                assert argv[4] == "inspect"
                value = [
                    {
                        "Id": self.inspected_config,
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": self.args.source_revision,
                                "adp.source-context": "sha256:"
                                + hashlib.sha256(self.context).hexdigest(),
                            }
                        },
                    }
                ]
                if self.descriptor is not None:
                    value[0]["Descriptor"] = self.descriptor
            elif action == "login":
                assert "--password-stdin" in argv
                assert kwargs["input"] == "fixture-token-never-log\n"
            elif action == "push":
                self.push(argv)
            else:
                raise AssertionError(argv)
        elif argv[0] == "skopeo":
            if argv[1] == "login":
                auth = Path(argv[argv.index("--authfile") + 1])
                assert auth.stat().st_mode & 0o077 == 0
                assert json.loads(auth.read_text()) == {"auths": {}}
                assert kwargs["input"] == "fixture-token-never-log\n"
                assert "--password-stdin" in argv
                if self.login_failure:
                    raise subprocess.CalledProcessError(
                        1, argv, stderr="fixture-token-never-log"
                    )
                auth.write_text(
                    json.dumps(
                        {
                            "auths": {
                                argv[-1]: {
                                    "auth": base64.b64encode(
                                        b"AWS:fixture-token-never-log"
                                    ).decode()
                                }
                            }
                        }
                    )
                )
                if self.public_auth:
                    auth.chmod(0o644)
                self.temporary_paths.append(auth)
            elif argv[1] == "copy":
                auth = Path(argv[argv.index("--dest-authfile") + 1])
                assert auth.is_file() and auth.stat().st_mode & 0o077 == 0
                assert argv[-2].startswith("docker-archive:")
                archive = Path(argv[-2].removeprefix("docker-archive:"))
                assert archive.is_file() and archive.stat().st_mode & 0o077 == 0
                self.temporary_paths.append(archive)
                assert (
                    argv[-1]
                    == f"docker://{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-insights-emitter:source-{self.args.source_revision}"
                )
                assert argv[argv.index("--format") + 1] == "v2s2"
                assert "--dest-tls-verify=true" in argv
                self.push(argv)
            else:
                raise AssertionError(argv)
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(value) if value is not None else ""
        )


def fixture(test):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    tmp_path = Path(directory.name)
    root = tmp_path / "checkout"
    root.mkdir()
    for path in publisher.CONTEXT_PATHS:
        target = root / path
        if path == "libs/python/adp-common":
            target = target / "pyproject.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("reviewed source\n")
    (root / ".gitignore").write_text("ignored-credential\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "reviewed fixture",
        ],
        cwd=root,
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    args = argparse.Namespace(
        environment="dev",
        region=REGION,
        account=ACCOUNT,
        role_arn=ROLE,
        source_revision=revision,
        output=str(private / "image.json"),
    )
    runtime = FakeRuntime(args)
    return root, args, runtime


def cases(names, values):
    def decorate(function):
        def run_cases(test):
            for value in values:
                arguments = value if "," in names else (value,)
                with test.subTest(**dict(zip(names.split(","), arguments))):
                    function(test, *arguments)

        return run_cases

    return decorate


class ReceiverInstallationTest(unittest.TestCase):
    def test_publish_uses_only_exact_root_layout_archive_and_private_verified_receipt(
        self,
    ):
        root, args, runtime = fixture(self)
        (root / "ignored-credential").write_text("must never be in the image context")
        receipt = publisher.Publisher(args, root=root, run=runtime).publish()
        with tarfile.open(fileobj=io.BytesIO(runtime.context)) as archive:
            names = archive.getnames()
            assert "modules/insights/emitter/Dockerfile" in names
            assert "libs/python/adp-common/pyproject.toml" in names
            assert not any(
                ".git" in name or "ignored-credential" in name for name in names
            )
            assert (
                archive.extractfile("modules/insights/emitter/server.py").read()
                == b"reviewed source\n"
            )
        output = Path(args.output)
        assert receipt == json.loads(output.read_text())
        assert output.stat().st_mode & 0o077 == 0
        assert receipt["qualified"] is False
        assert receipt["config_digest"] == CONFIG_DIGEST
        assert receipt["source_revision"] == args.source_revision
        assert receipt["image"].startswith(
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-insights-emitter@sha256:"
        )
        assert "fixture-token" not in output.read_text()
        assert "fixture-token" not in Path(str(output) + ".build.log").read_text()
        assert not Path(
            next(call[2] for call in runtime.calls if call[0] == "docker")
        ).exists()
        assert not any(call[0] in {"terraform", "kubectl"} for call in runtime.calls)

    @cases(
        "condition",
        [
            "dirty",
            "untracked",
            "wrong-revision",
            "wrong-role",
            "wrong-account",
            "wrong-parameter",
            "wrong-repository",
            "mutable-repository",
            "public-output",
        ],
    )
    def test_target_and_source_mismatches_stop_before_build_or_push(self, condition):
        root, args, runtime = fixture(self)
        if condition == "dirty":
            (root / "modules/insights/emitter/server.py").write_text("unreviewed")
        elif condition == "untracked":
            (root / "extra").write_text("unreviewed")
        elif condition == "wrong-revision":
            args.source_revision = "f" * 40
        elif condition in {"wrong-role", "wrong-account"}:
            runtime.overrides[("sts", "get-caller-identity")] = {
                "Account": ACCOUNT if condition == "wrong-role" else "111111111111",
                "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/other-role/session",
            }
        elif condition == "wrong-parameter":
            runtime.overrides[("ssm", "get-parameter")] = {
                "Parameter": {"Value": "111111111111"}
            }
        elif condition == "wrong-repository":
            runtime.overrides[("ecr", "describe-repositories")] = {"repositories": []}
        elif condition == "mutable-repository":
            result = runtime(
                ["aws", "ecr", "describe-repositories", "--region", REGION]
            )
            value = json.loads(result.stdout)
            value["repositories"][0]["imageTagMutability"] = "MUTABLE"
            runtime.overrides[("ecr", "describe-repositories")] = value
        else:
            Path(args.output).parent.chmod(0o755)
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(args, root=root, run=runtime).publish()
        assert not any(call[0] == "docker" for call in runtime.calls)
        assert not Path(args.output).exists()

    def test_existing_unattributed_source_tag_is_never_claimed_or_rebuilt(self):
        root, args, runtime = fixture(self)
        runtime.image_present = True
        with self.assertRaisesRegex(publisher.PublishError, "original receipt"):
            publisher.Publisher(args, root=root, run=runtime).publish()
        assert not any(call[0] == "docker" for call in runtime.calls)

    @cases("failure", ["wrong-config", "wrong-manifest-digest", "absent-after-push"])
    def test_no_receipt_when_remote_image_cannot_be_verified(self, failure):
        root, args, runtime = fixture(self)
        if failure == "wrong-config":
            runtime.manifest_config = "sha256:" + "d" * 64
        elif failure == "wrong-manifest-digest":
            runtime.manifest_digest_override = "sha256:" + "e" * 64
        else:
            runtime.push_persists = False
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(args, root=root, run=runtime).publish()
        assert runtime.pushed
        assert not Path(args.output).exists()
        assert Path(str(args.output) + ".pending.json").exists()

    def test_lost_push_response_is_reconciled_without_republishing(self):
        root, args, runtime = fixture(self)
        runtime.push_failure = True
        receipt = publisher.Publisher(args, root=root, run=runtime).publish()
        runtime.calls.clear()
        assert publisher.Publisher(args, root=root, run=runtime).publish() == receipt
        assert not any(call[0] == "docker" for call in runtime.calls)

    def test_pending_publication_is_reconciled_after_read_outage(self):
        root, args, runtime = fixture(self)
        runtime.read_failure_after_push = True
        with self.assertRaisesRegex(publisher.PublishError, "ecr:batch-get-image"):
            publisher.Publisher(args, root=root, run=runtime).publish()
        assert not Path(args.output).exists()
        runtime.read_failure_after_push = False
        runtime.calls.clear()
        receipt = publisher.Publisher(args, root=root, run=runtime).publish()
        assert receipt["config_digest"] == CONFIG_DIGEST
        assert not any(call[0] == "docker" for call in runtime.calls)

    def test_pending_receipt_with_different_binding_cannot_be_reused(self):
        root, args, runtime = fixture(self)
        runtime.read_failure_after_push = True
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(args, root=root, run=runtime).publish()
        path = Path(str(args.output) + ".pending.json")
        pending = json.loads(path.read_text())
        pending["context_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(pending))
        runtime.calls.clear()
        with self.assertRaisesRegex(publisher.PublishError, "different source/target"):
            publisher.Publisher(args, root=root, run=runtime).publish()
        assert not any(call[0] == "docker" for call in runtime.calls)

    @cases(
        "keys,url",
        [
            ({}, URL),
            ({"key": "-----BEGIN PRIVATE KEY-----"}, URL),
            ({"key": PEM}, "http://unreviewed.example"),
            ({"key": PEM}, URL + "/"),
            ({"key": PEM}, URL.replace(":80", ":8080")),
            ({"key": PEM}, "\n" + URL),
            ({"key": PEM}, "https://user:password@example.test"),
            ({"key": PEM}, "https://gateway.example:bad"),
        ],
    )
    def test_invalid_receiver_config_is_not_defaulted(self, keys, url):
        def run(argv, **kwargs):
            assert argv[:3] == ["aws", "ssm", "get-parameter"]
            name = argv[argv.index("--name") + 1]
            value = json.dumps(keys) if name.endswith("verification-keys") else url
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "Parameter": {
                            "Name": name,
                            "ARN": f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{name}",
                            "Type": "String",
                            "Version": 1,
                            "Value": value,
                        }
                    }
                ),
            )

        with self.assertRaises(ValueError):
            reader.configuration("dev", REGION, ACCOUNT, run=run)

    def test_config_mismatched_parameter_identity_is_rejected(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "Parameter": {
                            "Name": "/adp/prod/insights/l5-consumer-verification-keys",
                            "Type": "String",
                            "Version": 1,
                            "ARN": "arn:aws:ssm:us-west-2:111111111111:parameter/other",
                            "Value": json.dumps({"key": PEM}),
                        }
                    }
                ),
            )

        with self.assertRaises(ValueError):
            reader.configuration("dev", REGION, ACCOUNT, run=run)


class ManifestImageIdentityTest(unittest.TestCase):
    @cases("transport", ["docker", "archive"])
    def test_containerd_manifest_id_binds_actual_config_before_auth(self, transport):
        root, args, runtime = fixture(self)
        args.push_transport = transport
        runtime.use_manifest_image_id()
        receipt = publisher.Publisher(args, root=root, run=runtime).publish()
        assert receipt["build_image_id"] == runtime.inspected_config
        assert receipt["config_digest"] == CONFIG_DIGEST
        assert receipt["build_image_id"] != receipt["config_digest"]
        save_at = next(
            i
            for i, argv in enumerate(runtime.calls)
            if argv[0] == "docker" and argv[3:5] == ["image", "save"]
        )
        auth_at = next(
            i
            for i, argv in enumerate(runtime.calls)
            if argv[:3] == ["aws", "ecr", "get-login-password"]
        )
        assert save_at < auth_at

    @cases(
        "fault",
        [
            "index",
            "wrong-id",
            "size",
            "annotation",
            "manifest-hash",
            "config-chain",
            "missing-manifest",
            "config",
            "layers",
            "layer-digest",
        ],
    )
    def test_ambiguous_or_inconsistent_manifest_identity_stops_before_auth(self, fault):
        root, args, runtime = fixture(self)
        args.push_transport = "archive"
        runtime.use_manifest_image_id()
        if fault == "index":
            runtime.descriptor["mediaType"] = "application/vnd.oci.image.index.v1+json"
        elif fault == "wrong-id":
            runtime.descriptor["digest"] = CONFIG_DIGEST
        elif fault == "size":
            runtime.descriptor["size"] += 1
        elif fault == "annotation":
            runtime.descriptor["annotations"]["config.digest"] = "sha256:" + "a" * 64
        elif fault == "manifest-hash":
            runtime.build_manifest += b" "
            runtime.descriptor["size"] += 1
        elif fault == "config-chain":
            manifest = json.loads(runtime.build_manifest)
            manifest["config"]["digest"] = "sha256:" + "b" * 64
            runtime.build_manifest = json.dumps(manifest).encode()
            runtime.inspected_config = (
                "sha256:" + hashlib.sha256(runtime.build_manifest).hexdigest()
            )
            runtime.descriptor.update(
                digest=runtime.inspected_config, size=len(runtime.build_manifest)
            )
        elif fault == "missing-manifest":
            runtime.build_manifest = None
        else:
            runtime.archive_fault = fault
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(args, root=root, run=runtime).publish()
        assert not runtime.pushed
        assert not any(
            argv[0] == "skopeo" or argv[:3] == ["aws", "ecr", "get-login-password"]
            for argv in runtime.calls
        )
        assert not Path(str(args.output) + ".pending.json").exists()


class ArchivePublicationTest(unittest.TestCase):
    def test_remote_builder_never_receives_registry_or_operator_credentials(self):
        root, args, runtime = fixture(self)
        args.push_transport = "archive"
        plugins = root.parent / "cli-plugins"
        plugins.mkdir()
        args.docker_cli_plugins_dir = str(plugins)
        environ = {
            "PATH": os.environ["PATH"],
            "HOME": str(root.parent),
            "DOCKER_HOST": "ssh://reviewed-builder",
            "SSH_AUTH_SOCK": "/fixture/ssh-agent",
            "AWS_ACCESS_KEY_ID": "operator-key-never-forward",
            "AWS_SECRET_ACCESS_KEY": "operator-secret-never-forward",
            "AWS_SESSION_TOKEN": "operator-session-never-forward",
            "AWS_PROFILE": "operator-profile",
            "AWS_WEB_IDENTITY_TOKEN_FILE": "/fixture/private-token",
            "GITHUB_TOKEN": "github-never-forward",
            "DOCKER_AUTH_CONFIG": "registry-auth-never-forward",
            "REGISTRY_AUTH_FILE": "/fixture/other-auth",
            "BUILDX_CONFIG": "/fixture/user-builder",
            "UNRELATED_SECRET": "secret-never-forward",
        }
        receipt = publisher.Publisher(
            args, root=root, run=runtime, environ=environ
        ).publish()
        assert receipt["config_digest"] == CONFIG_DIGEST and not receipt["qualified"]
        docker = [
            (argv, kwargs)
            for argv, kwargs in runtime.invocations
            if argv[0] == "docker"
        ]
        assert [argv[3:5] for argv, _ in docker] == [
            ["build", "--platform"],
            ["image", "inspect"],
            ["image", "save"],
        ]
        for argv, kwargs in docker:
            assert kwargs["env"] == {
                key: environ[key]
                for key in ("PATH", "HOME", "DOCKER_HOST", "SSH_AUTH_SOCK")
            }
            assert "input" not in kwargs
            assert not any(
                "secret-never-forward" in arg or "fixture-token" in arg for arg in argv
            )
        assert all(
            config == {"cliPluginsExtraDirs": [str(plugins.resolve())]}
            for config in runtime.docker_configurations
        )
        local = [
            (argv, kwargs)
            for argv, kwargs in runtime.invocations
            if argv[0] == "skopeo"
        ]
        assert [argv[1] for argv, _ in local] == ["login", "copy"]
        assert all(
            kwargs["env"] == {key: environ[key] for key in ("PATH", "HOME")}
            for _, kwargs in local
        )
        save_at = next(
            i
            for i, argv in enumerate(runtime.calls)
            if argv[:1] == ["docker"] and argv[3:5] == ["image", "save"]
        )
        token_at = next(
            i
            for i, argv in enumerate(runtime.calls)
            if argv[:3] == ["aws", "ecr", "get-login-password"]
        )
        assert save_at < token_at
        assert all(not path.exists() for path in runtime.temporary_paths)
        assert not Path(docker[0][0][2]).exists()
        for path in Path(args.output).parent.iterdir():
            if path.is_file():
                assert "fixture-token" not in path.read_text()
                assert "never-forward" not in path.read_text()

    @cases("fault", ["save", "config", "multiple", "link", "duplicate"])
    def test_archive_mismatch_stops_before_obtaining_credentials(self, fault):
        root, args, runtime = fixture(self)
        args.push_transport = "archive"
        runtime.archive_fault = fault
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(
                args, root=root, run=runtime, environ={"DOCKER_HOST": "ssh://builder"}
            ).publish()
        assert not runtime.pushed
        assert not any(
            argv[0] == "skopeo" or argv[:3] == ["aws", "ecr", "get-login-password"]
            for argv in runtime.calls
        )
        assert not Path(str(args.output) + ".pending.json").exists()

    @cases("fault", ["login", "public-auth"])
    def test_local_auth_failure_is_private_and_prevents_push(self, fault):
        root, args, runtime = fixture(self)
        args.push_transport = "archive"
        runtime.login_failure = fault == "login"
        runtime.public_auth = fault == "public-auth"
        with self.assertRaises(publisher.PublishError) as raised:
            publisher.Publisher(
                args, root=root, run=runtime, environ={"DOCKER_HOST": "ssh://builder"}
            ).publish()
        assert "fixture-token" not in str(raised.exception)
        assert not runtime.pushed
        assert not Path(str(args.output) + ".pending.json").exists()
        assert all(not path.exists() for path in runtime.temporary_paths)

    @cases("fault", ["lost-response", "read-outage"])
    def test_archive_publication_reconciles_journal_without_rebuild_or_login(
        self, fault
    ):
        root, args, runtime = fixture(self)
        args.push_transport = "archive"
        runtime.push_failure = fault == "lost-response"
        runtime.read_failure_after_push = fault == "read-outage"

        def run():
            return publisher.Publisher(
                args, root=root, run=runtime, environ={"DOCKER_HOST": "ssh://builder"}
            ).publish()

        if fault == "read-outage":
            with self.assertRaises(publisher.PublishError):
                run()
        else:
            run()
        runtime.read_failure_after_push = False
        runtime.calls.clear()
        receipt = run()
        assert receipt["config_digest"] == CONFIG_DIGEST
        assert not any(argv[0] in {"docker", "skopeo"} for argv in runtime.calls)

    @cases("fault", ["wrong-config", "wrong-manifest", "absent"])
    def test_archive_push_requires_independent_ecr_identity_and_digest(self, fault):
        root, args, runtime = fixture(self)
        args.push_transport = "archive"
        if fault == "wrong-config":
            runtime.manifest_config = "sha256:" + "d" * 64
        elif fault == "wrong-manifest":
            runtime.manifest_digest_override = "sha256:" + "e" * 64
        else:
            runtime.push_persists = False
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(
                args, root=root, run=runtime, environ={"DOCKER_HOST": "ssh://builder"}
            ).publish()
        assert runtime.pushed and not Path(args.output).exists()
        assert Path(str(args.output) + ".pending.json").exists()

    @cases(
        "transport,environ",
        [
            ("docker", {"DOCKER_HOST": "ssh://builder"}),
            ("archive", {"DOCKER_HOST": "tcp://builder:2375"}),
            ("archive", {"DOCKER_HOST": "ssh://user:password@builder"}),
            ("archive", {"DOCKER_HOST": "ssh://builder/path"}),
            (
                "archive",
                {"DOCKER_HOST": "ssh://builder", "DOCKER_CONTEXT": "unreviewed"},
            ),
        ],
    )
    def test_unsafe_engine_selection_stops_before_any_provider_or_docker_call(
        self, transport, environ
    ):
        root, args, runtime = fixture(self)
        args.push_transport = transport
        with self.assertRaises(publisher.PublishError):
            publisher.Publisher(args, root=root, run=runtime, environ=environ).publish()
        assert not runtime.calls


if __name__ == "__main__":
    unittest.main()
