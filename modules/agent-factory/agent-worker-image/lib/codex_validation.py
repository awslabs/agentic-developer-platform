"""Host-owned isolated validation executor for the shared Codex harness.

The host supplies a verified source archive and an admitted check specification.
The model cannot select Docker flags, mounts, credentials or a mutable image tag.
This local Docker backend is also used for end-to-end qualification; Kubernetes
workers need a separately provisioned trusted executor, never a child Docker socket.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import rfc8785


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    image: str
    argv: tuple[str, ...]
    timeout_seconds: int = 120
    memory_mb: int = 512
    cpus: int = 1
    max_output_bytes: int = 32768

    def document(self):
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image)
            or not isinstance(self.argv, tuple)
            or not 1 <= len(self.argv) <= 64
            or any(
                not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096
                for arg in self.argv
            )
            or any(
                type(value) is not int
                for value in (
                    self.timeout_seconds,
                    self.memory_mb,
                    self.cpus,
                    self.max_output_bytes,
                )
            )
            or not 1 <= self.timeout_seconds <= 3600
            or not 64 <= self.memory_mb <= 8192
            or not 1 <= self.cpus <= 8
            or not 1 <= self.max_output_bytes <= 32768
        ):
            raise ValueError("Invalid admitted validation check")
        return {
            "name": self.name,
            "image": self.image,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "max_output_bytes": self.max_output_bytes,
        }


class ValidationUnavailable(RuntimeError):
    pass


class DockerValidationExecutor:
    def __init__(self, docker="/usr/bin/docker"):
        self.docker = docker

    def run_repository(self, *, check: ValidationCheck, repository: Path, expected_head: str):
        """Validate an immutable commit from a trusted host-owned checkout.

        Local checkout paths never come from SDK tool arguments. No git config,
        hooks, ignored files or working-tree edits enter the validation container.
        """
        if not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", expected_head):
            raise ValueError("Invalid expected validation commit")
        with tempfile.TemporaryDirectory(prefix="adp-codex-source-") as directory:
            root = Path(directory)
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(root),
                "BG_CONFIG_DIR": str(root / "bg"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            }
            command = [
                "/usr/bin/git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.allow=never",
            ]

            def git(*args):
                result = subprocess.run(
                    [*command, *args],
                    cwd=repository,
                    env=env,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode:
                    raise ValidationUnavailable("Validation source could not be read")
                return result.stdout.strip()

            if git("rev-parse", "HEAD") != expected_head.encode() or git(
                "status", "--porcelain=v1", "--untracked-files=all"
            ):
                raise ValidationUnavailable("Validation requires the expected clean commit")
            # Reject an oversized tree before writing its archive. git's object
            # sizes are independent of compression and exclude untracked files.
            tree = git("ls-tree", "-r", "-l", expected_head)
            sizes = [line.split(None, 4)[3] for line in tree.splitlines()]
            if (
                len(sizes) > 10000
                or any(size == b"-" for size in sizes)
                or sum(int(size) for size in sizes) > 48 * 1024 * 1024
            ):
                raise ValidationUnavailable("Validation tree is oversized or contains submodules")
            archive = root / "source.tar"
            result = subprocess.run(
                [*command, "archive", "--format=tar", "--output=" + str(archive), expected_head],
                cwd=repository,
                env=env,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise ValidationUnavailable("Validation source archive unavailable")
            if archive.stat().st_size > 64 * 1024 * 1024:
                raise ValidationUnavailable("Validation archive exceeds bound")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            result = self.run(
                check=check, archive=archive, archive_sha256=digest, commit=expected_head
            )
            if git("rev-parse", "HEAD") != expected_head.encode() or git(
                "status", "--porcelain=v1", "--untracked-files=all"
            ):
                result.update(status="failed", reason="source_changed")
            return result

    def run(self, *, check: ValidationCheck, archive: Path, archive_sha256: str, commit: str):
        specification = check.document()
        if not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", commit) or not re.fullmatch(
            r"[a-f0-9]{64}", archive_sha256
        ):
            raise ValueError("Invalid validation source binding")
        # Copy and hash once; later source-file replacement cannot alter mounted bytes.
        with tempfile.TemporaryDirectory(prefix="adp-codex-validation-") as directory:
            root = Path(directory)
            source = root / "source.tar"
            digest = hashlib.sha256()
            total = 0
            with archive.open("rb") as reader, source.open("wb") as writer:
                while chunk := reader.read(65536):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ValueError("Validation source archive exceeds bound")
                    digest.update(chunk)
                    writer.write(chunk)
            if total == 0 or digest.hexdigest() != archive_sha256:
                raise ValueError("Validation source archive digest mismatch")
            source.chmod(0o444)
            # Docker gets no inherited ADP/AWS/GitHub configuration or endpoint overrides.
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(root),
                "DOCKER_CONFIG": str(root / "docker"),
                "BG_CONFIG_DIR": str(root / "bg"),
                "TMPDIR": str(root),
            }
            runtime = subprocess.run(
                [
                    self.docker,
                    "info",
                    "--format",
                    "{{.ServerVersion}} {{.KernelVersion}} {{.Architecture}} {{.OSType}}",
                ],
                env=env,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if runtime.returncode != 0 or not 1 <= len(runtime.stdout) <= 1024:
                raise ValidationUnavailable("Validation runtime identity unavailable")
            runtime_identity = runtime.stdout.decode().strip()
            container = "adp-codex-validation-" + uuid.uuid4().hex
            try:
                wrapper = 'tar -xf /input/source.tar -C /work && exec "$@"'
                command = [
                    self.docker,
                    "create",
                    "--name",
                    container,
                    "--label=adp.codex-validation=true",
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=128",
                    "--user=65534:65534",
                    "--log-driver=none",
                    f"--memory={check.memory_mb}m",
                    f"--memory-swap={check.memory_mb}m",
                    f"--cpus={check.cpus}",
                    "--tmpfs=/work:rw,nosuid,nodev,size=536870912,mode=1777",
                    "--tmpfs=/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
                    "--mount",
                    f"type=bind,source={source},target=/input/source.tar,readonly",
                    "--workdir=/work",
                    "--env=HOME=/tmp",
                    "--env=BG_CONFIG_DIR=/tmp/bg",
                    "--env=TMPDIR=/tmp",
                    "--entrypoint=/bin/sh",
                    check.image,
                    "-c",
                    wrapper,
                    "adp-validation",
                    *check.argv,
                ]
                created = subprocess.run(
                    command, env=env, capture_output=True, timeout=30, check=False
                )
                if created.returncode != 0 or not re.fullmatch(rb"[a-f0-9]{64}\n?", created.stdout):
                    raise ValidationUnavailable("Validation container could not be created")
                started = time.monotonic()
                process = subprocess.Popen(
                    [self.docker, "start", "--attach", container],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                output = bytearray()
                stop = None
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while selector.get_map():
                            if time.monotonic() - started >= check.timeout_seconds:
                                stop = "timeout"
                                break
                            for key, _ in selector.select(timeout=0.05):
                                chunk = os.read(key.fd, 4096)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                remaining = check.max_output_bytes - len(output)
                                output.extend(chunk[:remaining])
                                if len(chunk) > remaining:
                                    stop = "output_limit"
                                    break
                            if stop:
                                break
                    if stop:
                        # Detach the bounded output consumer first. Otherwise a
                        # full attach pipe can stall Docker's kill acknowledgement.
                        process.kill()
                        process.stdout.close()
                        process.wait(timeout=10)
                        subprocess.run(
                            [self.docker, "kill", container],
                            env=env,
                            capture_output=True,
                            timeout=10,
                            check=False,
                        )
                    process.wait(timeout=10)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    process.stdout.close()
                inspected = (
                    subprocess.run(
                        [
                            self.docker,
                            "inspect",
                            "--format",
                            "{{.State.ExitCode}} {{.State.OOMKilled}} {{.State.Running}}",
                            container,
                        ],
                        env=env,
                        capture_output=True,
                        timeout=10,
                        check=True,
                    )
                    .stdout.decode()
                    .strip()
                    .split()
                )
                if len(inspected) != 3 or inspected[2] != "false":
                    raise ValidationUnavailable("Validation process exit is unconfirmed")
                text = output.decode("utf-8", errors="replace")
                if len(text.encode()) > check.max_output_bytes:
                    stop = stop or "output_limit"
                    text = text.encode()[:check.max_output_bytes].decode("utf-8", errors="ignore")
                passed = (
                    stop is None
                    and process.returncode == 0
                    and inspected == ["0", "false", "false"]
                )
                return {
                    "check": check.name,
                    "commit": commit,
                    "archiveSha256": archive_sha256,
                    "specificationDigest": hashlib.sha256(rfc8785.dumps(specification)).hexdigest(),
                    "environmentDigest": hashlib.sha256(
                        rfc8785.dumps(
                            {
                                "backend": "docker-isolated-v1",
                                "runtime": runtime_identity,
                                "image": check.image,
                                "network": "none",
                                "uid": 65534,
                                "memory_mb": check.memory_mb,
                                "cpus": check.cpus,
                            }
                        )
                    ).hexdigest(),
                    "status": "passed" if passed else "failed",
                    "exitCode": int(inspected[0]),
                    "reason": stop or ("completed" if passed else "process_failed"),
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "output": text,
                }
            finally:
                if container:
                    removed = subprocess.run(
                        [self.docker, "rm", "--force", container],
                        env=env,
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    if removed.returncode != 0 and b"No such container" not in removed.stderr:
                        raise ValidationUnavailable("Validation container cleanup is unconfirmed")
