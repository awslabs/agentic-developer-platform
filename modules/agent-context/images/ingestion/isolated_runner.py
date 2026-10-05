"""Isolated parser runner — orchestrates fetch/parse/publish stages.

This module is the integration layer between the existing ingestion pipeline
(ingest-repo.py) and the isolated parser backend.  It:

1. **Fetch** (trusted): clone is already done by ingest-repo.py; this stage
   prepares dependencies via ``dep_preparation`` and builds the input manifest.
2. **Parse** (isolated): launches a credential-free Docker container with
   no network. Kubernetes job dispatch is not yet integrated.
3. **Validate** (trusted): freezes validated SCIP bytes. External publication
   requires a separate canonical asset/graph grant and remains unavailable.

Backend selection:
- ``DockerBackend``: for local development/testing — runs the parser image
  with ``docker run --network=none --read-only --cap-drop=ALL``.
- ``SubprocessBackend``: for CI/testing without Docker — runs isolated_parser.py
  as a subprocess with credential-scrubbed environment.
- A backend must be selected explicitly; the subprocess seam is refused with
  ProductionAuthorizer even if it gains a production grant issuer in the future.
- Kubernetes job manifests are templates only; there is no production job backend.

When no backend is available, the runner returns a truthful
``structural_stage_unavailable`` result.  It NEVER falls back to in-process
credential-bearing parsing.
"""

from __future__ import annotations

import abc
import logging
import os
import re
import shutil
import subprocess
import tempfile
import tarfile
import time
import uuid
from dataclasses import dataclass, field, replace

from dep_preparation import PreparedDeps, prepare_dependencies
from parser_capability import (
    Authorizer,
    CapabilityDeniedError,
    ProductionAuthorizer,
)
from parser_manifest import (
    InvocationBinding,
    ParseInputManifest,
    ParseOutputManifest,
    ResourceLimits,
    compute_tree_digest,
)
from parser_publisher import OutputPublisher, PublicationError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runner result
# ---------------------------------------------------------------------------


@dataclass
class IsolatedParseResult:
    """Result from the isolated parser pipeline."""

    status: str  # complete, no_languages, indexing_failed, backend_unavailable, error, refused
    scip_files: dict[str, str] = field(default_factory=dict)  # lang -> abs path
    output_manifest: ParseOutputManifest | None = None
    error: str | None = None
    detail: str = ""
    duration_seconds: float = 0.0
    work_dir: str = ""
    publish_cap: object | None = None

    def cleanup(self):
        if self.work_dir:
            shutil.rmtree(self.work_dir)
            self.work_dir = ""


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class ParserBackend(abc.ABC):
    """Abstract interface for running the isolated parser."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is ready to run parsers."""

    @abc.abstractmethod
    def run(
        self,
        input_manifest: ParseInputManifest,
        source_dir: str,
        output_dir: str,
        *,
        prepared_deps: PreparedDeps | None = None,
    ) -> int:
        """Run the parser.  Returns exit code (0 = success)."""

    @abc.abstractmethod
    def cleanup(self) -> None:
        """Clean up backend resources."""


class SubprocessBackend(ParserBackend):
    """Run the isolated parser as a subprocess with credential-scrubbed env.

    For CI/testing without Docker.  Provides import discipline and environment
    scrubbing but not full container isolation.
    """

    def __init__(self, parser_script: str | None = None, timeout: int = 600):
        self._parser_script = parser_script or os.path.join(
            os.path.dirname(__file__), "isolated_parser.py"
        )
        self._timeout = timeout

    def is_available(self) -> bool:
        return os.path.isfile(self._parser_script)

    def run(
        self,
        input_manifest: ParseInputManifest,
        source_dir: str,
        output_dir: str,
        *,
        prepared_deps: PreparedDeps | None = None,
    ) -> int:
        env = _scrubbed_env(source_dir)

        # Write input manifest where the parser expects it
        manifest_path = os.path.join(output_dir, "input_manifest.json")
        with open(manifest_path, "w") as f:
            f.write(input_manifest.to_json())

        try:
            result = subprocess.run(
                [
                    "python3",
                    self._parser_script,
                ],
                env={
                    **env,
                    "PARSER_INPUT_MANIFEST": manifest_path,
                },
                cwd=source_dir,
                capture_output=True,
                timeout=min(self._timeout, input_manifest.resource_limits.deadline_seconds),
                check=False,
            )
            if result.returncode != 0:
                log.warning(
                    "Parser subprocess exited %d: %s",
                    result.returncode,
                    result.stderr[:500] if result.stderr else "",
                )
            return result.returncode
        except subprocess.TimeoutExpired:
            log.error("Parser subprocess exceeded its configured deadline")
            return 1
        except FileNotFoundError:
            log.error("Parser script not found: %s", self._parser_script)
            return 1

    def cleanup(self) -> None:
        pass


class DockerBackend(ParserBackend):
    """Run the isolated parser in a Docker container with full isolation.

    Container runs with:
    - --network=none (no network access)
    - --read-only (read-only root filesystem)
    - --user 1001:1001 (non-root)
    - --cap-drop=ALL (all capabilities dropped)
    - --security-opt=no-new-privileges (no privilege escalation)
    - --security-opt seccomp=unconfined is NOT used; default seccomp profile
    - Source mounted read-only, output on a bounded tmpfs
    """

    def __init__(
        self,
        image: str = "adp-scip-parser:latest",
        timeout: int = 600,
    ):
        self._image = image
        self._timeout = timeout

    def is_available(self) -> bool:
        """Require an immutable local image before considering Docker ready."""
        if not re.fullmatch(r"(?:[A-Za-z0-9./:_-]+@)?sha256:[a-f0-9]{64}", self._image):
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self._image],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def run(
        self,
        input_manifest: ParseInputManifest,
        source_dir: str,
        output_dir: str,
        *,
        prepared_deps: PreparedDeps | None = None,
    ) -> int:
        if not re.fullmatch(r"(?:[A-Za-z0-9./:_-]+@)?sha256:[a-f0-9]{64}", self._image):
            raise CapabilityDeniedError("Parser image must be pinned by digest")
        limits = input_manifest.resource_limits
        if not (0 < limits.output_bytes_max <= 512 * 1024 * 1024):
            raise CapabilityDeniedError("Unsupported output budget")
        manifest_dir = tempfile.mkdtemp(prefix="parser-manifest-")
        manifest_path = os.path.join(manifest_dir, "input_manifest.json")
        with open(manifest_path, "w") as output:
            output.write(input_manifest.to_json())
        name = "adp-parser-" + uuid.uuid4().hex
        created = False
        command = [
            "docker",
            "create",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--user=1001:1001",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "-v",
            f"{os.path.abspath(source_dir)}:/source:ro",
            "-v",
            f"{manifest_path}:/input/input_manifest.json:ro",
            "--tmpfs",
            f"/output:size={limits.output_bytes_max},uid=1001,gid=1001,mode=0700",
            "--tmpfs",
            "/tmp:size=2g,mode=1777",
            "--tmpfs",
            "/home/appuser:size=512m,uid=1001,gid=1001,mode=0700",
            f"--memory={limits.memory_mib}m",
            f"--cpus={limits.cpu_millicores / 1000:.1f}",
        ]
        if prepared_deps and prepared_deps.scratch_dir:
            command += ["-v", f"{os.path.abspath(prepared_deps.scratch_dir)}:/deps:ro"]
        # Keep tmpfs alive until a frozen snapshot is copied. Untrusted parser
        # output is validated only after all container processes are removed.
        command += [
            "--entrypoint",
            "sh",
            self._image,
            "-c",
            "python3 /app/isolated_parser.py; code=$?; echo $code > /output/.parser-exit; sleep 86400",
        ]
        deadline = time.monotonic() + min(self._timeout, limits.deadline_seconds)

        def remaining_timeout(maximum: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Isolated parser deadline exceeded")
            return min(maximum, remaining)

        try:
            created = True  # cleanup also covers an uncertain create response
            subprocess.run(command, check=True, capture_output=True, timeout=remaining_timeout(30))
            subprocess.run(
                ["docker", "start", name],
                check=True,
                capture_output=True,
                timeout=remaining_timeout(30),
            )
            while time.monotonic() < deadline:
                result = subprocess.run(
                    ["docker", "exec", name, "cat", "/output/.parser-exit"],
                    capture_output=True,
                    timeout=remaining_timeout(10),
                )
                if result.returncode == 0:
                    code = int(result.stdout.strip())
                    if code != 0:
                        return code
                    self._export_output(
                        name,
                        output_dir,
                        limits.output_bytes_max,
                        timeout=remaining_timeout(60),
                    )
                    return 0
                time.sleep(remaining_timeout(0.2))
            raise TimeoutError("Isolated parser deadline exceeded")
        finally:
            try:
                if created:
                    subprocess.run(
                        ["docker", "rm", "--force", name],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
            finally:
                shutil.rmtree(manifest_dir)

    def _export_output(
        self,
        name: str,
        output_dir: str,
        budget: int,
        *,
        timeout: float = 60,
    ) -> None:
        # Docker's archive API does not reliably include tmpfs contents. Export
        # a bounded regular-file archive from the live mount instead. No output
        # is consumed until the container has been removed and digests checked.
        exporter = """
import os, stat, sys, tarfile
budget = int(sys.argv[1])
total = 0
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for count, entry in enumerate(os.scandir('/output')):
        if count >= 256:
            raise RuntimeError('Too many parser output entries')
        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('Parser output must be regular files')
            total += info.st_size
            if total > budget:
                raise RuntimeError('Parser output exceeds budget')
            member = tarfile.TarInfo(entry.name)
            member.size = info.st_size
            member.mode = 0o400
            archive.addfile(member, source)
"""
        with tempfile.TemporaryFile() as stream:
            subprocess.run(
                ["docker", "exec", name, "python3", "-I", "-c", exporter, str(budget)],
                stdout=stream,
                stderr=subprocess.PIPE,
                check=True,
                timeout=timeout,
            )
            if stream.tell() > budget + 256 * 1024 + 10240:
                raise CapabilityDeniedError("Parser output archive exceeds budget")
            stream.seek(0)
            total = 0
            seen = set()
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    if (
                        not member.isfile()
                        or member.name in seen
                        or os.path.basename(member.name) != member.name
                        or member.name in (".", "..")
                        or len(seen) >= 256
                    ):
                        raise CapabilityDeniedError("Invalid parser output archive")
                    seen.add(member.name)
                    total += member.size
                    if total > budget:
                        raise CapabilityDeniedError("Parser output exceeds budget")
                    with archive.extractfile(member) as source:
                        with open(os.path.join(output_dir, member.name), "xb") as output:
                            shutil.copyfileobj(source, output)

    def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


class IsolatedParserRunner:
    """Orchestrates the fetch → parse → publish pipeline.

    Usage::

        runner = IsolatedParserRunner(backend=SubprocessBackend())
        result = runner.run(clone_path, "org/repo")
        if result.status == "complete":
            # result.scip_files contains validated .scip paths
            ...
    """

    def __init__(
        self,
        backend: ParserBackend | None = None,
        authorizer: Authorizer | None = None,
    ):
        self._backend = backend
        self._authorizer = authorizer or ProductionAuthorizer()

    def run(
        self,
        clone_path: str,
        org_repo: str,
        *,
        allowed_languages: list[str] | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> IsolatedParseResult:
        """Execute the full isolated parse pipeline.

        Returns an IsolatedParseResult.  Never raises — all errors are
        captured in the result.
        """
        start_time = time.time()

        # Check backend availability
        if (
            self._backend is None
            or (
                isinstance(self._backend, SubprocessBackend)
                and isinstance(self._authorizer, ProductionAuthorizer)
            )
            or not self._backend.is_available()
        ):
            return IsolatedParseResult(
                status="backend_unavailable",
                error="Isolated parser backend not available",
                detail="structural_stage_unavailable",
                duration_seconds=time.time() - start_time,
            )

        # Generate invocation identity
        invocation_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        asset_id = org_repo

        # Resolve server authority BEFORE dependency preparation or parser launch.
        # Request fields are selectors only; ProductionAuthorizer denies until
        # the canonical server-owned grant issuer is integrated.
        try:
            fetch_cap = self._authorizer.issue_fetch(asset_id, attempt_id)
            if (
                fetch_cap.asset_id != asset_id
                or fetch_cap.attempt_id != attempt_id
                or fetch_cap.is_expired
            ):
                raise CapabilityDeniedError("Invalid fetch grant binding or expiry")
            invocation_id = fetch_cap.invocation_id
        except CapabilityDeniedError as exc:
            return IsolatedParseResult(
                status="authority_unavailable",
                error=str(exc),
                detail="structural_stage_unavailable",
            )

        # Create working directories
        work_dir = tempfile.mkdtemp(prefix="isolated-parse-")
        output_dir = os.path.join(work_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        try:
            result = self._execute(
                clone_path=clone_path,
                org_repo=org_repo,
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                asset_id=asset_id,
                output_dir=output_dir,
                work_dir=work_dir,
                allowed_languages=allowed_languages,
                resource_limits=resource_limits or ResourceLimits(),
                start_time=start_time,
                fetch_cap=fetch_cap,
            )
            if result.status == "complete":
                result.work_dir = work_dir
            else:
                shutil.rmtree(work_dir)
            return result
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            ValueError,
            TypeError,
            tarfile.TarError,
        ) as e:
            log.error("Isolated parse pipeline failed: %s", e)
            shutil.rmtree(work_dir)
            return IsolatedParseResult(
                status="error",
                error=str(e),
                duration_seconds=time.time() - start_time,
            )
        finally:
            try:
                self._backend.cleanup()
            except OSError:
                log.debug("Backend cleanup failed", exc_info=True)

    def _execute(
        self,
        *,
        clone_path: str,
        org_repo: str,
        invocation_id: str,
        attempt_id: str,
        asset_id: str,
        output_dir: str,
        work_dir: str,
        allowed_languages: list[str] | None,
        resource_limits: ResourceLimits,
        start_time: float,
        fetch_cap,
    ) -> IsolatedParseResult:
        # Copy an immutable input snapshot, excluding all Git metadata. Symlinks
        # cannot smuggle host files into either preparation or the parser.
        for root, dirs, files in os.walk(clone_path):
            dirs[:] = [name for name in dirs if name != ".git"]
            if any(os.path.islink(os.path.join(root, name)) for name in dirs + files):
                raise CapabilityDeniedError(
                    "Source symlinks require a separate admitted snapshot format"
                )
        private_source = os.path.join(work_dir, "source")
        shutil.copytree(clone_path, private_source, ignore=shutil.ignore_patterns(".git"))
        # The enclosing work directory stays 0700 on the host. Inside the
        # read-only bind mount UID 1001 must be able to traverse/read source
        # even when the caller's clone root was 0700.
        for root, _dirs, files in os.walk(private_source):
            os.chmod(root, 0o755)
            for filename in files:
                path = os.path.join(root, filename)
                os.chmod(path, 0o755 if os.stat(path).st_mode & 0o111 else 0o644)
        clone_path = private_source

        # --- Stage 1: Fetch (dependency preparation) ---

        # Detect languages from the clone
        from scip_indexer import detect_languages

        detected = detect_languages(clone_path)
        if not detected:
            return IsolatedParseResult(
                status="no_languages",
                detail="no SCIP-supported languages detected",
                duration_seconds=time.time() - start_time,
            )

        lang_list = list(detected.keys())
        if allowed_languages:
            lang_list = [language for language in lang_list if language in set(allowed_languages)]
            if not lang_list:
                return IsolatedParseResult(
                    status="no_languages",
                    detail="no allowed languages detected",
                    duration_seconds=time.time() - start_time,
                )

        parse_cap = self._authorizer.issue_parse(
            asset_id,
            attempt_id,
            source_dir=clone_path,
            output_dir=output_dir,
            allowed_languages=lang_list,
        )
        publish_cap = self._authorizer.issue_publish(asset_id, attempt_id)
        for grant in (parse_cap, publish_cap):
            if (grant.invocation_id, grant.asset_id, grant.attempt_id) != (
                invocation_id,
                asset_id,
                attempt_id,
            ):
                raise CapabilityDeniedError("Cross-bound parser grant")
        if not publish_cap.is_valid:
            raise CapabilityDeniedError("Invalid publish grant")

        # The issuer may narrow the request. Paths must bind this exact owned
        # snapshot/output pair; never redirect work to grant-selected host paths.
        if parse_cap.source_dir != clone_path or parse_cap.output_dir != output_dir:
            raise CapabilityDeniedError("Parse grant paths do not match owned work directories")
        if (
            not isinstance(parse_cap.allowed_languages, list)
            or not parse_cap.allowed_languages
            or any(
                not isinstance(language, str) or not language
                for language in parse_cap.allowed_languages
            )
        ):
            raise CapabilityDeniedError("Invalid or empty parse grant languages")
        lang_list = [language for language in lang_list if language in parse_cap.allowed_languages]
        if not lang_list:
            raise CapabilityDeniedError("No detected/requested languages authorized by parse grant")
        for value in (
            parse_cap.output_bytes_max,
            parse_cap.deadline_seconds,
            resource_limits.output_bytes_max,
            resource_limits.deadline_seconds,
        ):
            if type(value) is not int or value <= 0:
                raise CapabilityDeniedError("Parse grant/request bounds must be positive integers")
        resource_limits = replace(
            resource_limits,
            output_bytes_max=min(resource_limits.output_bytes_max, parse_cap.output_bytes_max),
            deadline_seconds=min(resource_limits.deadline_seconds, parse_cap.deadline_seconds),
        )

        # Prepare only authorized languages under validated grant bounds.
        prepared = prepare_dependencies(
            clone_path,
            lang_list,
            scratch_base=os.path.join(work_dir, "deps"),
            fetch_cap=fetch_cap,
        )

        if any(not result.success or result.refused for result in prepared.results):
            raise CapabilityDeniedError("Dependency preparation refused or failed")
        if sum(result.total_bytes for result in prepared.results) > fetch_cap.max_fetch_bytes:
            raise CapabilityDeniedError("Prepared dependency bytes exceed fetch grant")
        if fetch_cap.is_expired or not publish_cap.is_valid:
            raise CapabilityDeniedError("Grant expired before parser launch")

        # Compute source digest for the invocation binding
        source_digest = compute_tree_digest(clone_path)

        # Build input manifest
        input_manifest = ParseInputManifest(
            invocation_id=invocation_id,
            asset_id=asset_id,
            attempt_id=attempt_id,
            source_dir="/source" if isinstance(self._backend, DockerBackend) else clone_path,
            source_digest=source_digest,
            allowed_languages=lang_list,
            output_dir="/output" if isinstance(self._backend, DockerBackend) else output_dir,
            resource_limits=resource_limits,
        )

        # Build invocation binding for the publisher
        binding = InvocationBinding(
            invocation_id=invocation_id,
            asset_id=asset_id,
            attempt_id=attempt_id,
            source_digest=source_digest,
            output_bytes_max=resource_limits.output_bytes_max,
        )

        # --- Stage 2: Parse (isolated) ---
        parser_started = time.monotonic()
        exit_code = self._backend.run(
            input_manifest,
            clone_path,
            output_dir,
            prepared_deps=prepared,
        )

        if time.monotonic() - parser_started > resource_limits.deadline_seconds:
            raise CapabilityDeniedError("Parser exceeded granted deadline")
        if exit_code != 0:
            return IsolatedParseResult(
                status="indexing_failed",
                error=f"parser exited with code {exit_code}",
                duration_seconds=time.time() - start_time,
            )

        # --- Stage 3: Publish (validate output) ---
        publisher = OutputPublisher(binding, publish_cap=publish_cap)

        try:
            validated = publisher.validate_and_collect(output_dir)
        except PublicationError as e:
            return IsolatedParseResult(
                status="error",
                error=f"output validation failed: {e}",
                duration_seconds=time.time() - start_time,
            )

        if any(result.language not in lang_list for result in validated.languages):
            raise CapabilityDeniedError("Parser output contains a language outside its grant")

        # Collect validated .scip files
        scip_files = publisher.collect_scip_files(output_dir, validated)

        status = "complete" if validated.any_success else "indexing_failed"
        return IsolatedParseResult(
            status=status,
            scip_files=scip_files,
            output_manifest=validated,
            publish_cap=publish_cap,
            duration_seconds=time.time() - start_time,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scrubbed_env(clone_path: str) -> dict[str, str]:
    """Build a subprocess environment with credentials removed.

    Removes AWS, GitHub, database, and other sensitive environment variables
    that the parser must never see.
    """
    env = dict(os.environ)
    abs_clone = os.path.abspath(clone_path)

    # Remove credential-bearing variables
    credential_vars = [
        # AWS
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        # GitHub
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_ID",
        # Database
        "DATABASE_URL",
        "PGPASSWORD",
        "PGHOST",
        "DB_HOST",
        "DB_PASSWORD",
        # Neptune
        "NEPTUNE_ENDPOINT",
        "NEPTUNE_PORT",
        # General secrets
        "SECRET_KEY",
        "API_KEY",
        "INTERNAL_API_KEY",
        # Kubernetes service account
        "KUBERNETES_SERVICE_HOST",
        "KUBERNETES_SERVICE_PORT",
        # S3 / storage
        "S3_BUCKET_NAME",
        "S3_VECTORS_BUCKET",
        # SQS
        "SQS_QUEUE_URL",
        # Status callback
        "STATUS_CALLBACK_URL",
    ]
    for var in credential_vars:
        env.pop(var, None)

    # Also remove any var ending in _SECRET, _TOKEN, _KEY, _PASSWORD
    for key in list(env.keys()):
        suffix = key.rsplit("_", 1)[-1] if "_" in key else ""
        if suffix in ("SECRET", "TOKEN", "KEY", "PASSWORD", "CREDENTIALS"):
            env.pop(key, None)

    # Remove clone-resident PATH entries
    if "PATH" in env:
        entries = env["PATH"].split(os.pathsep)
        safe_entries = [p for p in entries if not os.path.abspath(p).startswith(abs_clone)]
        env["PATH"] = os.pathsep.join(safe_entries) if safe_entries else "/usr/bin:/bin"

    # Clear dangerous loader variables
    for var in (
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "VIRTUAL_ENV",
        "NODE_PATH",
        "RUBYOPT",
        "GEM_HOME",
        "BUNDLE_GEMFILE",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    ):
        env.pop(var, None)

    return env
