"""Credential-free workspace from a host-authorized repository archive.

Invocation/provider adapters supply verified archive identity and repository
binding. This layer does not authorize a URL, fetch credentials or publish code.
"""

from __future__ import annotations

import hashlib
import base64
import fcntl
import io
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path

from lib.codex_source_limits import MAX_PROVIDER_ARCHIVE_BYTES, MAX_SOURCE_BYTES, MAX_SOURCE_ENTRIES


class WorkspaceError(ValueError):
    pass


class CodexWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        provider: str,
        repository: str,
        source_revision: str,
        repository_id: str | None = None,
    ):
        if provider not in {"github", "gitlab"} or not re.fullmatch(
            r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)+", repository
        ):
            raise WorkspaceError("Invalid authorized repository binding")
        if not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", source_revision):
            raise WorkspaceError("Invalid provider source revision")
        if repository_id is not None and (
            not isinstance(repository_id, str) or not re.fullmatch(r"[1-9][0-9]*", repository_id)
        ):
            raise WorkspaceError("Invalid provider repository identity")
        self.repository_id = repository_id
        self._base_head = None
        self.root = root.resolve()
        self.provider, self.repository, self.source_revision = provider, repository, source_revision

    @staticmethod
    def _parts(path):
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 4096
            or "\\" in path
            or "\x00" in path
            or ":" in path
        ):
            raise WorkspaceError("Invalid workspace path")
        parts = path.split("/")
        if len(parts) > 32 or any(
            part in {"", ".", ".."} or part.casefold() == ".git" for part in parts
        ):
            raise WorkspaceError("Workspace path escapes source scope")
        return parts

    def _git(self, *args):
        return self._git_bytes(*args).decode().rstrip("\n")

    def _git_bytes(self, *args):
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.root.parent),
            "BG_CONFIG_DIR": str(self.root.parent / "bg"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=ADP",
                "-c",
                "user.email=adp@localhost",
                *args,
            ],
            cwd=self.root,
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError("Workspace Git operation failed")
        return result.stdout

    def materialize(self, archive: bytes, *, archive_sha256: str):
        if (
            not isinstance(archive, bytes)
            or not 0 < len(archive) <= MAX_PROVIDER_ARCHIVE_BYTES
            or hashlib.sha256(archive).hexdigest() != archive_sha256
        ):
            raise WorkspaceError("Archive digest or size differs from host receipt")
        if self.root.exists():
            raise WorkspaceError("Workspace destination already exists")
        # Validate the entire archive before any extraction. Provider archives
        # must wrap one tree in one directory; metadata cannot become .git.
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
            members, total, prefix, seen = [], 0, None, set()
            file_paths = set()
            entry_count = 0
            for member in bundle:
                entry_count += 1
                if entry_count > MAX_SOURCE_ENTRIES or member.size < 0:
                    raise WorkspaceError("Archive expansion exceeds workspace bound")
                raw = member.name.rstrip("/")
                parts = self._parts(raw)
                if prefix is None:
                    prefix = parts[0]
                if parts[0] != prefix or (len(parts) == 1 and not member.isdir()):
                    raise WorkspaceError("Archive contains multiple source roots")
                if len(parts) == 1:
                    continue
                relative = "/".join(parts[1:])
                if relative in seen or not (member.isfile() or member.isdir()):
                    raise WorkspaceError(
                        "Archive contains duplicate paths or unsupported links/devices"
                    )
                seen.add(relative)
                if member.isfile():
                    file_paths.add(relative)
                total += member.size
                if len(seen) > MAX_SOURCE_ENTRIES or total > MAX_SOURCE_BYTES:
                    raise WorkspaceError("Archive expansion exceeds workspace bound")
                members.append((member, relative))
            if prefix is None:
                raise WorkspaceError("Archive contains no source root")
            for _, relative in members:
                parts = relative.split("/")
                if any("/".join(parts[:i]) in file_paths for i in range(1, len(parts))):
                    raise WorkspaceError("Archive file conflicts with a parent directory")
            self.root.mkdir(mode=0o700)
            for member, relative in members:
                target = self.root / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = bundle.extractfile(member)
                    if stream is None:
                        raise WorkspaceError("Archive file is unavailable")
                    with stream, target.open("xb") as writer:
                        writer.write(stream.read(member.size + 1))
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
        self._git("init", "--initial-branch=adp-work")
        self._git("add", "--all", "--force")
        self._git("commit", "--allow-empty", "-m", "Materialized authorized source")
        self._base_head = self._git("rev-parse", "HEAD")
        return self.state()

    def _parent(self, path, *, create=False):
        parts = self._parts(path)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                next_descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor, parts[-1]
        except BaseException:
            os.close(descriptor)
            raise

    def read_file(self, path):
        parent, name = self._parent(path)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as reader:
                if (
                    not stat.S_ISREG(os.fstat(reader.fileno()).st_mode)
                    or os.fstat(reader.fileno()).st_nlink != 1
                ):
                    raise WorkspaceError("Workspace entry is not a regular file")
                fcntl.flock(reader.fileno(), fcntl.LOCK_SH)
                content = reader.read(32769)
            if len(content) > 32768:
                raise WorkspaceError("Workspace file exceeds tool output bound")
            return {
                "path": path,
                "content": content.decode("utf-8"),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        finally:
            os.close(parent)

    def write_file(self, path, content, *, expected_sha256):
        if not isinstance(content, str) or len(content.encode()) > 32768:
            raise WorkspaceError("Workspace edit exceeds bound")
        parent, name = self._parent(path, create=True)
        descriptor = None
        try:
            if expected_sha256 is None:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent
                )
            else:
                if not isinstance(expected_sha256, str) or not re.fullmatch(
                    r"[a-f0-9]{64}", expected_sha256
                ):
                    raise WorkspaceError("Invalid expected file digest")
                descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                if (
                    not stat.S_ISREG(os.fstat(descriptor).st_mode)
                    or os.fstat(descriptor).st_nlink != 1
                ):
                    raise WorkspaceError("Workspace edit requires a regular file with one link")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                old = os.read(descriptor, 32769)
                if hashlib.sha256(old).hexdigest() != expected_sha256:
                    raise WorkspaceError("Workspace edit is stale")
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.ftruncate(descriptor, 0)
            with os.fdopen(descriptor, "wb") as writer:
                descriptor = None
                writer.write(content.encode())
                writer.flush()
                os.fsync(writer.fileno())
            return {"path": path, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def list_files(self, *, prefix="", offset=0, limit=100):
        if prefix:
            self._parts(prefix)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("Invalid workspace listing window")
        paths = sorted(
            set(
                path
                for path in self._git(
                    "ls-files", "-z", "--cached", "--others", "--exclude-standard"
                ).split("\0")
                if path
            )
        )
        if len(paths) > MAX_SOURCE_ENTRIES:
            raise WorkspaceError("Workspace listing exceeds bound")
        paths = [path for path in paths if path.startswith(prefix)]
        return {
            "paths": paths[offset : offset + limit],
            "nextOffset": offset + limit if offset + limit < len(paths) else None,
        }

    def commit(self, message):
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 2000
            or "\x00" in message
        ):
            raise WorkspaceError("Invalid local commit message")
        self._git("add", "--all")
        self._git("commit", "-m", message)
        return self.state()

    def export_changes(self, *, expected_head):
        """Host-only publication manifest from committed objects, never dirty files.

        Local and provider commit identities differ. The gateway must check base
        and resulting tree identities, validation receipts and current authority
        before changing the provider ref. This manifest is not that authority.
        """
        current = self.state()
        if (
            not self._base_head
            or not self.repository_id
            or not current["clean"]
            or current["localHead"] != expected_head
        ):
            raise WorkspaceError("Publication requires the expected clean bound commit")
        diff = self._git_bytes(
            "diff-tree",
            "-r",
            "-z",
            "--no-commit-id",
            "--no-renames",
            "--raw",
            self._base_head,
            expected_head,
        )
        parts = diff.split(b"\x00")
        if parts[-1] != b"" or len(parts) % 2 != 1:
            raise WorkspaceError("Publication diff is invalid")
        changes, total = [], 0
        for index in range(0, len(parts) - 1, 2):
            header = parts[index].decode("ascii").split()
            path = parts[index + 1].decode("utf-8")
            self._parts(path)
            if (
                len(header) != 5
                or header[4] not in {"A", "M", "D"}
                or not re.fullmatch(r"[a-f0-9]{40}", header[3])
            ):
                raise WorkspaceError("Publication change kind is unsupported")
            deleted = header[4] == "D"
            mode = header[0].removeprefix(":") if deleted else header[1]
            if mode not in {"100644", "100755"}:
                raise WorkspaceError("Publication file mode is unsupported")
            content = None if deleted else self._git_bytes("cat-file", "blob", header[3])
            total += len(content) if content is not None else 0
            if total > 192 * 1024 or len(changes) >= 100:
                raise WorkspaceError("Publication changes exceed transfer bound")
            changes.append(
                {
                    "path": path,
                    "mode": mode,
                    "deleted": deleted,
                    "content_base64": None
                    if content is None
                    else base64.b64encode(content).decode(),
                }
            )
        if not changes:
            raise WorkspaceError("Publication has no committed changes")
        if self.state() != current:
            raise WorkspaceError("Workspace changed while preparing publication")
        result = {
            "schema_version": "1.0",
            "provider": self.provider,
            "repository_id": self.repository_id,
            "repository": self.repository,
            "source_revision": self.source_revision,
            "local_head": expected_head,
            "base_tree": self._git("rev-parse", self._base_head + "^{tree}"),
            "tree": current["tree"],
            "changes": changes,
        }
        import rfc8785

        if len(rfc8785.dumps(result)) > 262144:
            raise WorkspaceError("Publication manifest exceeds transfer bound")
        return result

    def state(self):
        return {
            "provider": self.provider,
            "repository": self.repository,
            "sourceRevision": self.source_revision,
            "localHead": self._git("rev-parse", "HEAD"),
            "tree": self._git("rev-parse", "HEAD^{tree}"),
            "clean": not bool(self._git("status", "--porcelain=v1", "--untracked-files=all")),
        }
