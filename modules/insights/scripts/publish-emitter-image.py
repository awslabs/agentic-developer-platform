#!/usr/bin/env python3
"""Publish the reviewed Insights receiver source to its existing immutable ECR repository."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[3]
CONTEXT_PATHS = (
    "modules/insights/emitter/Dockerfile",
    "modules/insights/emitter/requirements.txt",
    "modules/insights/emitter/server.py",
    "libs/python/adp-common",
)
DIGEST = r"sha256:[a-f0-9]{64}"
MANIFEST_TYPES = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
)


class PublishError(Exception):
    """An actionable error safe to display without credentials or provider output."""


def require(condition, message):
    if not condition:
        raise PublishError(message)


def read_private(path):
    require(
        not path.is_symlink() and path.is_file() and not path.stat().st_mode & 0o077,
        "Receipt and pending files must be private regular files.",
    )
    require(path.stat().st_size <= 32768, "Receipt exceeds its size bound.")
    return json.loads(path.read_text())


def write_new(path, value):
    """Publish a complete fsynced record without replacing an existing record."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=".insights-receipt-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


@contextmanager
def output_lock(output):
    descriptor = os.open(
        str(output) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        require(
            not os.fstat(descriptor).st_mode & 0o077, "Receipt lock must be private."
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError(
                "Another publisher owns this receipt; wait for that operation."
            ) from None
        yield
    finally:
        os.close(descriptor)


class Publisher:
    def __init__(self, args, *, root=ROOT, run=subprocess.run, environ=None):
        self.args, self.root, self.run = args, root, run
        self.environ = dict(os.environ if environ is None else environ)
        self.transport = getattr(args, "push_transport", "docker")
        self.plugins_dir = getattr(args, "docker_cli_plugins_dir", None)
        self.repository = f"adp-{args.environment}-insights-emitter"
        self.registry = f"{args.account}.dkr.ecr.{args.region}.amazonaws.com"
        self.uri = f"{self.registry}/{self.repository}"
        self.tag = "source-" + args.source_revision
        self.output = Path(args.output)
        self.pending = Path(str(self.output) + ".pending.json")

    def client_environment(self, *, docker=False):
        # SSH may use SendEnv. Never give Docker/SSH AWS, registry, GitHub or
        # arbitrary operator variables to inherit or forward to the builder.
        selected = {
            key: value
            for key, value in self.environ.items()
            if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"}
        }
        if docker:
            selected.update(
                {
                    key: self.environ[key]
                    for key in ("DOCKER_HOST", "SSH_AUTH_SOCK", "SSH_AGENT_PID")
                    if key in self.environ
                }
            )
        return selected

    def docker_configuration(self, workspace):
        configuration = workspace / "docker"
        configuration.mkdir(mode=0o700)
        if self.plugins_dir:
            # Select only the explicitly supplied plugin directory; never copy
            # the operator's config.json, credential helpers or registry auth.
            write_new(
                configuration / "config.json",
                {"cliPluginsExtraDirs": [str(Path(self.plugins_dir).resolve())]},
            )
        return configuration

    def export_image(self, workspace, docker_config, image_id, descriptor, log):
        archive = workspace / "image.tar"
        archive_fd = os.open(
            archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(archive_fd, "wb") as image:
                # stdout is a LOCAL file descriptor, including when DOCKER_HOST
                # selects an SSH daemon. Export the inspected ID, not a mutable tag.
                self.run(
                    [
                        "docker",
                        "--config",
                        str(docker_config),
                        "image",
                        "save",
                        image_id,
                    ],
                    stdout=image,
                    stderr=log,
                    timeout=1800,
                    check=True,
                    env=self.client_environment(docker=True),
                )
                image.flush()
                os.fsync(image.fileno())
            with tarfile.open(archive, "r:") as saved:
                members = saved.getmembers()
                names = [member.name for member in members]
                require(
                    len(names) == len(set(names)),
                    "Exported image archive contains duplicate entries; no image was pushed.",
                )

                def read_entry(name, limit):
                    require(
                        isinstance(name, str)
                        and not name.startswith("/")
                        and not {".", ".."}.intersection(name.split("/")),
                        "Exported image archive contains an invalid entry; no image was pushed.",
                    )
                    entry = saved.getmember(name)
                    require(
                        entry.isfile() and entry.size <= limit,
                        "Exported image metadata is not a bounded regular file; no image was pushed.",
                    )
                    return saved.extractfile(entry).read()

                manifest = json.loads(read_entry("manifest.json", 65536))
                require(
                    isinstance(manifest, list) and len(manifest) == 1,
                    "Export must contain exactly the inspected image; no image was pushed.",
                )
                raw = read_entry(manifest[0]["Config"], 1048576)
                config_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                if descriptor is None:
                    require(
                        config_digest == image_id,
                        "Exported image config differs from the inspected image; no image was pushed.",
                    )
                else:
                    # Containerd-backed Docker reports the single manifest as
                    # Id. Bind the exported config to that exact manifest; the
                    # legacy Docker archive entry alone is not this proof.
                    manifest_raw = read_entry(
                        "blobs/sha256/" + image_id.removeprefix("sha256:"), 65536
                    )
                    selected = json.loads(manifest_raw)
                    require(
                        len(manifest_raw) == descriptor["size"]
                        and "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
                        == image_id
                        and isinstance(selected, dict)
                        and selected.get("schemaVersion") == 2
                        and selected.get("mediaType") == descriptor["mediaType"]
                        and isinstance(selected.get("config"), dict)
                        and selected.get("config", {}).get("digest") == config_digest
                        and selected["config"].get("size") == len(raw),
                        "Exported manifest/config chain differs from the inspected image; no image was pushed.",
                    )
                    layers = selected.get("layers")
                    require(
                        isinstance(layers, list)
                        and all(
                            isinstance(layer, dict)
                            and re.fullmatch(DIGEST, layer.get("digest", ""))
                            and type(layer.get("size")) is int
                            and layer["size"] > 0
                            for layer in layers
                        )
                        and manifest[0].get("Layers")
                        == [
                            "blobs/sha256/" + layer["digest"].removeprefix("sha256:")
                            for layer in layers
                        ],
                        "Exported archive layers differ from the inspected manifest; no image was pushed.",
                    )
                    for layer, name in zip(layers, manifest[0]["Layers"]):
                        entry = saved.getmember(name)
                        require(
                            entry.isfile()
                            and entry.size == layer["size"]
                            and "sha256:"
                            + hashlib.file_digest(
                                saved.extractfile(entry), "sha256"
                            ).hexdigest()
                            == layer["digest"],
                            "Exported layer digest differs from the inspected manifest; no image was pushed.",
                        )
                    annotation = descriptor.get("annotations", {}).get("config.digest")
                    require(
                        annotation is None or annotation == config_digest,
                        "Docker config annotation differs from the verified archive; no image was pushed.",
                    )
        except (
            OSError,
            subprocess.SubprocessError,
            tarfile.TarError,
            ValueError,
            KeyError,
            TypeError,
        ):
            raise PublishError(
                "Image export failed; inspect the private .build.log and local archive transport prerequisites. No image was pushed; use a new receipt for retry."
            ) from None
        return archive, config_digest

    def push_command(self, workspace, docker_config, archive):
        password = self.command(
            [
                "aws",
                "ecr",
                "get-login-password",
                "--region",
                self.args.region,
                "--cli-connect-timeout",
                "3",
                "--cli-read-timeout",
                "10",
            ],
            "ECR login authorization failed; verify the selected role's existing push permission, then use a new receipt for retry.",
        )
        if archive is not None:
            auth = workspace / "registry-auth.json"
            write_new(auth, {"auths": {}})
            self.command(
                [
                    "skopeo",
                    "login",
                    "--authfile",
                    str(auth),
                    "--username",
                    "AWS",
                    "--password-stdin",
                    self.registry,
                ],
                "Local Skopeo login failed; no image was pushed. Use a new receipt for retry.",
                input=password,
                env=self.client_environment(),
            )
            require(
                not auth.is_symlink()
                and auth.is_file()
                and not auth.stat().st_mode & 0o077,
                "Skopeo auth file must remain private; no image was pushed.",
            )
            return [
                "skopeo",
                "copy",
                "--dest-authfile",
                str(auth),
                "--dest-tls-verify=true",
                "--format",
                "v2s2",
                "docker-archive:" + str(archive),
                "docker://" + self.uri + ":" + self.tag,
            ], self.client_environment()
        self.command(
            [
                "docker",
                "--config",
                str(docker_config),
                "login",
                "--username",
                "AWS",
                "--password-stdin",
                self.registry,
            ],
            "ECR login failed; no image was pushed. Use a new receipt for retry.",
            input=password,
            env=self.client_environment(docker=True),
        )
        return [
            "docker",
            "--config",
            str(docker_config),
            "push",
            self.uri + ":" + self.tag,
        ], self.client_environment(docker=True)

    def command(self, argv, message, **kwargs):
        try:
            return self.run(
                argv, check=True, timeout=30, capture_output=True, text=True, **kwargs
            ).stdout
        except (OSError, subprocess.SubprocessError):
            raise PublishError(message) from None

    def aws(self, argv):
        raw = self.command(
            [
                "aws",
                *argv,
                "--region",
                self.args.region,
                "--output",
                "json",
                "--cli-connect-timeout",
                "3",
                "--cli-read-timeout",
                "10",
            ],
            "Target read failed; verify the selected role's permission for "
            + ":".join(argv[:2])
            + ".",
        )
        require(len(raw) <= 1048576, "Target response exceeds its size bound.")
        return json.loads(raw)

    def validate(self):
        args = self.args
        host = self.environ.get("DOCKER_HOST", "")
        require(
            self.transport in {"docker", "archive"},
            "Select docker or archive publication transport.",
        )
        require(
            not self.environ.get("DOCKER_CONTEXT"),
            "Unset DOCKER_CONTEXT and select the reviewed engine explicitly with DOCKER_HOST.",
        )
        local = not host or host.startswith(("unix:///", "npipe://"))
        require(
            not any(ord(character) < 32 for character in host)
            and (
                local
                or (
                    self.transport == "archive"
                    and re.fullmatch(
                        r"ssh://(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*(?::[0-9]{1,5})?",
                        host,
                    )
                )
            ),
            "Remote builders require --push-transport archive and an SSH DOCKER_HOST; Docker registry login/push is restricted to a local engine.",
        )
        if self.plugins_dir:
            require(
                Path(self.plugins_dir).is_absolute()
                and Path(self.plugins_dir).is_dir()
                and not any(ord(character) < 32 for character in self.plugins_dir),
                "Docker CLI plugin directory must be an existing absolute directory.",
            )
        require(
            args.environment in {"dev", "prod"}
            and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", args.region)
            and re.fullmatch(r"[0-9]{12}", args.account)
            and re.fullmatch(r"[a-f0-9]{40}", args.source_revision),
            "Supply the exact reviewed source and target.",
        )
        require(
            re.fullmatch(
                rf"arn:aws:iam::{args.account}:role/[A-Za-z0-9+=,.@_/-]+", args.role_arn
            ),
            "Supply the selected account's exact existing role ARN.",
        )
        require(
            self.output.is_absolute()
            and self.output.parent.is_dir()
            and not self.output.parent.is_symlink()
            and not self.output.parent.stat().st_mode & 0o077
            and not self.output.resolve().is_relative_to(self.root.resolve()),
            "Receipt must be outside the checkout in an existing private directory (mode 0700).",
        )
        head = self.command(
            ["git", "rev-parse", "HEAD"], "Cannot read source revision.", cwd=self.root
        ).strip()
        dirty = self.command(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            "Cannot verify the source checkout.",
            cwd=self.root,
        )
        require(
            head == args.source_revision and not dirty,
            "Use a clean checkout at the exact reviewed source revision.",
        )
        identity = self.aws(["sts", "get-caller-identity"])
        role_name = args.role_arn.rsplit("/", 1)[1]
        require(
            identity.get("Account") == args.account
            and re.fullmatch(
                rf"arn:aws:sts::{args.account}:assumed-role/{re.escape(role_name)}/[^/]+",
                identity.get("Arn", ""),
            ),
            "Active AWS identity differs from the selected account or role; select the reviewed role before publishing.",
        )
        name = f"/adp/{args.environment}/platform/account-id"
        parameter = self.aws(["ssm", "get-parameter", "--name", name]).get(
            "Parameter", {}
        )
        require(
            parameter.get("Name") == name
            and parameter.get("Value") == args.account
            and parameter.get("ARN")
            == f"arn:aws:ssm:{args.region}:{args.account}:parameter{name}",
            "Protected platform account parameter differs from the selected target.",
        )
        repositories = self.aws(
            [
                "ecr",
                "describe-repositories",
                "--registry-id",
                args.account,
                "--repository-names",
                self.repository,
            ]
        ).get("repositories", [])
        require(
            len(repositories) == 1
            and repositories[0].get("repositoryUri") == self.uri
            and repositories[0].get("repositoryArn")
            == f"arn:aws:ecr:{args.region}:{args.account}:repository/{self.repository}"
            and repositories[0].get("registryId") == args.account
            and repositories[0].get("repositoryName") == self.repository
            and repositories[0].get("imageTagMutability") == "IMMUTABLE",
            "Existing emitter repository must match the target and use immutable tags; run the reviewed infra stage first.",
        )

    def image(self, config_digest=None):
        value = self.aws(
            [
                "ecr",
                "batch-get-image",
                "--registry-id",
                self.args.account,
                "--repository-name",
                self.repository,
                "--image-ids",
                "imageTag=" + self.tag,
                "--accepted-media-types",
                *MANIFEST_TYPES,
            ]
        )
        images, failures = value.get("images", []), value.get("failures", [])
        if not images:
            require(
                len(failures) == 1
                and failures[0].get("failureCode") == "ImageNotFound"
                and failures[0].get("imageId") == {"imageTag": self.tag},
                "Could not determine whether the exact immutable source tag exists; preserve records and reconcile before retrying.",
            )
            return None
        require(
            len(images) == 1 and not failures,
            "ECR did not return exactly one selected image.",
        )
        image = images[0]
        manifest_raw = image.get("imageManifest", "")
        digest = "sha256:" + hashlib.sha256(manifest_raw.encode()).hexdigest()
        manifest = json.loads(manifest_raw)
        require(
            image.get("registryId") == self.args.account
            and image.get("repositoryName") == self.repository
            and image.get("imageId") == {"imageTag": self.tag, "imageDigest": digest}
            and manifest.get("schemaVersion") == 2
            and manifest.get("mediaType") in MANIFEST_TYPES
            and image.get("imageManifestMediaType") in MANIFEST_TYPES,
            "ECR image identity or manifest digest does not match the selected repository/tag.",
        )
        if config_digest is not None:
            require(
                manifest.get("config", {}).get("digest") == config_digest,
                "ECR image config differs from the locally built image; do not install this digest.",
            )
        return digest

    def publish(self):
        self.validate()
        with (
            output_lock(self.output),
            tempfile.TemporaryDirectory(prefix="insights-emitter-") as directory,
        ):
            workspace = Path(directory)
            archive = workspace / "context.tar"
            self.command(
                [
                    "git",
                    "archive",
                    "--format=tar",
                    "--output=" + str(archive),
                    self.args.source_revision,
                    "--",
                    *CONTEXT_PATHS,
                ],
                "Cannot archive the reviewed source build context.",
                cwd=self.root,
            )
            context_digest = (
                "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            )
            binding = {
                "schema_version": 1,
                "account": self.args.account,
                "region": self.args.region,
                "environment": self.args.environment,
                "role_arn": self.args.role_arn,
                "source_revision": self.args.source_revision,
                "context_digest": context_digest,
                "context_paths": list(CONTEXT_PATHS),
                "repository": self.uri,
                "source_tag": self.tag,
                "platform": "linux/amd64",
                "qualified": False,
            }
            if self.output.exists() or self.output.is_symlink():
                receipt = read_private(self.output)
                require(
                    all(receipt.get(key) == val for key, val in binding.items())
                    and re.fullmatch(DIGEST, receipt.get("config_digest", "")),
                    "Existing receipt belongs to different source/target; choose the original matching receipt.",
                )
                digest = self.image(receipt["config_digest"])
                require(
                    digest and receipt.get("image") == self.uri + "@" + digest,
                    "Existing receipt no longer matches the independently read ECR image.",
                )
                return receipt
            pending = (
                read_private(self.pending)
                if self.pending.exists() or self.pending.is_symlink()
                else None
            )
            if pending is not None:
                require(
                    all(pending.get(key) == val for key, val in binding.items())
                    and re.fullmatch(DIGEST, pending.get("config_digest", "")),
                    "Pending publication belongs to different source/target; preserve it and use a distinct receipt.",
                )
                digest = self.image(pending["config_digest"])
                require(
                    digest,
                    "Pending image is absent in ECR. Preserve this journal; use a new receipt to rebuild before retrying.",
                )
                receipt = pending | {"image": self.uri + "@" + digest}
                write_new(self.output, receipt)
                return receipt
            require(
                self.image() is None,
                "Source tag already exists without this publication journal; use its original receipt, not a new attribution.",
            )
            docker_config = self.docker_configuration(workspace)
            log_path = Path(str(self.output) + ".build.log")
            log_fd = os.open(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(log_fd, "wb") as log, archive.open("rb") as context:
                try:
                    self.run(
                        [
                            "docker",
                            "--config",
                            str(docker_config),
                            "build",
                            "--platform",
                            "linux/amd64",
                            "--load",
                            "--provenance=false",
                            "--file",
                            CONTEXT_PATHS[0],
                            "--tag",
                            self.uri + ":" + self.tag,
                            "--label",
                            "org.opencontainers.image.revision="
                            + self.args.source_revision,
                            "--label",
                            "adp.source-context=" + context_digest,
                            "-",
                        ],
                        stdin=context,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=1800,
                        check=True,
                        env=self.client_environment(docker=True),
                    )
                except (OSError, subprocess.SubprocessError):
                    raise PublishError(
                        "Docker build failed; inspect the private receipt .build.log. No image was pushed; use a new receipt for retry."
                    ) from None
                built = json.loads(
                    self.command(
                        [
                            "docker",
                            "--config",
                            str(docker_config),
                            "image",
                            "inspect",
                            self.uri + ":" + self.tag,
                        ],
                        "Cannot inspect the locally built image; no image was pushed.",
                        env=self.client_environment(docker=True),
                    )
                )
                require(
                    isinstance(built, list) and len(built) == 1,
                    "Docker did not identify exactly one built image.",
                )
                image_id = built[0].get("Id", "")
                labels = built[0].get("Config", {}).get("Labels", {})
                require(
                    re.fullmatch(DIGEST, image_id)
                    and built[0].get("Os") == "linux"
                    and built[0].get("Architecture") == "amd64"
                    and labels.get("org.opencontainers.image.revision")
                    == self.args.source_revision
                    and labels.get("adp.source-context") == context_digest,
                    "Built image config, platform or source labels differ from the reviewed source; no image was pushed.",
                )
                descriptor = built[0].get("Descriptor")
                require(
                    descriptor is None
                    or isinstance(descriptor, dict)
                    and descriptor.get("mediaType") in MANIFEST_TYPES
                    and descriptor.get("digest") == image_id
                    and type(descriptor.get("size")) is int
                    and 0 < descriptor["size"] <= 65536
                    and isinstance(descriptor.get("annotations", {}), dict),
                    "Docker image descriptor must identify one exact supported manifest, without an index; no image was pushed.",
                )
                config_digest = image_id
                archive = None
                if self.transport == "archive" or descriptor is not None:
                    archive, config_digest = self.export_image(
                        workspace, docker_config, image_id, descriptor, log
                    )
                push, push_environment = self.push_command(
                    workspace,
                    docker_config,
                    archive if self.transport == "archive" else None,
                )
                pending = binding | {
                    "config_digest": config_digest,
                    "build_image_id": image_id,
                }
                write_new(self.pending, pending)
                # A failed/lost push response is reconciled by the independent ECR read.
                try:
                    self.run(
                        push,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=1800,
                        check=True,
                        env=push_environment,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            digest = self.image(config_digest)
            require(
                digest,
                "Push is not visible in ECR. Preserve the pending journal and private build log; rerun to reconcile before rebuilding.",
            )
            receipt = pending | {"image": self.uri + "@" + digest}
            write_new(self.output, receipt)
            return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "environment",
        "region",
        "account",
        "role-arn",
        "source-revision",
        "output",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument(
        "--push-transport",
        choices=("docker", "archive"),
        default="docker",
        help="Use archive to export from an SSH Docker engine and publish with local Skopeo.",
    )
    parser.add_argument(
        "--docker-cli-plugins-dir",
        help="Explicit local Docker CLI plugin directory, without importing user Docker credentials.",
    )
    args = parser.parse_args(argv)
    try:
        receipt = Publisher(args).publish()
        print(receipt["image"])
        print(
            "Private image receipt saved. Installation and live qualification remain separate.",
            file=sys.stderr,
        )
        return 0
    except PublishError as error:
        print("Emitter publication stopped: " + str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            "Emitter publication stopped: malformed response or unreadable private receipt. Preserve local records and inspect the selected target; no default was applied.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
