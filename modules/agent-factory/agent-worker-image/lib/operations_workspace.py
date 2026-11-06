"""Bounded source windows and exact replacements for the operations worker."""

import fcntl
import hashlib
import os
import re
import stat
from contextlib import contextmanager

from lib.codex_workspace import WorkspaceError

MAX_FILE_BYTES = 256 * 1024
MAX_WINDOW_BYTES = 8192


@contextmanager
def source_file(workspace, path, *, write=False):
    parent, name = workspace._parent(path)
    try:
        descriptor = os.open(name, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "r+b" if write else "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspaceError("Repair source must be a regular file with one link")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise WorkspaceError("Repair source exceeds 256 KiB file bound")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceError("Repair source must be UTF-8 text") from None
            yield stream, text, hashlib.sha256(content).hexdigest()
    finally:
        os.close(parent)


def read_window(workspace, path, *, offset=0, limit=MAX_WINDOW_BYTES, expected_sha256=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_WINDOW_BYTES:
        raise WorkspaceError("Invalid repair read window")
    with source_file(workspace, path) as (_, text, digest):
        if expected_sha256 is not None and expected_sha256 != digest:
            raise WorkspaceError("Repair source changed; restart reading at offset 0")
        if offset > len(text):
            raise WorkspaceError("Repair read offset exceeds file length")
        page = text[offset:offset + limit].encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        if not page and offset < len(text):
            raise WorkspaceError("Repair read window is smaller than one UTF-8 character")
        end = offset + len(page)
        return {"path": path, "content": page, "sha256": digest, "offset": offset,
                "offset_unit": "characters", "next_offset": end if end < len(text) else None,
                "total_characters": len(text)}


def replace_text(workspace, path, *, old_text, new_text, expected_sha256, validate):
    if (
        not isinstance(old_text, str) or not old_text or not isinstance(new_text, str)
        or len(old_text.encode()) > MAX_WINDOW_BYTES or len(new_text.encode()) > MAX_WINDOW_BYTES
        or not isinstance(expected_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
    ):
        raise WorkspaceError("Repair replacement requires bounded text and the full file digest")
    with source_file(workspace, path, write=True) as (stream, text, digest):
        if digest != expected_sha256:
            raise WorkspaceError("Workspace edit is stale")
        if old_text not in text or text.find(old_text) != text.rfind(old_text):
            raise WorkspaceError("Repair replacement must match exactly once")
        result = text.replace(old_text, new_text, 1).encode("utf-8")
        if len(result) > MAX_FILE_BYTES:
            raise WorkspaceError("Repair result exceeds 256 KiB file bound")
        # Validate the complete prospective file against the current gateway
        # policy before any local write; the lock spans both validation and edit.
        validate(path, result)
        stream.seek(0)
        stream.write(result)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        return {"path": path, "sha256": hashlib.sha256(result).hexdigest()}
