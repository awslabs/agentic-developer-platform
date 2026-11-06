"""Publish the running adapter's key to the owner on tmpfs (never in the home) and seed Claude Code's onboarding state."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path


RUN_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
MODEL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
LOOPBACK = re.compile(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}\Z")
SECRET = re.compile(r"[0-9a-f]{64}\Z")


def trusted_receipt(path: Path, uid: int) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022 or info.st_size > 32768:
            raise ValueError("untrusted host receipt")
        with os.fdopen(descriptor, "r") as source:
            descriptor = -1
            return json.load(source)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def owner_directory(directory: Path, uid: int, group: int, create: bool = False) -> bool:
    parent = directory.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 and parent.st_uid != uid or parent.st_mode & 0o022:
        raise ValueError("untrusted runtime parent")
    if not directory.exists() and not directory.is_symlink():
        if not create:
            return False
        directory.mkdir(mode=0o700)
        os.chown(directory, uid, group)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("untrusted owner runtime directory")
    return True


def seed_claude_onboarding(home: Path, secret: str, *, owner_uid: int, owner_gid: int) -> None:
    """Let interactive `claude` open at the prompt: onboarding complete, this boot's adapter key approved.

    Claude Code runs its first-run wizard (theme, login method) unless ``~/.claude.json`` says
    ``hasCompletedOnboarding``; it asks whether to trust a custom key unless the key's last 20
    characters are in ``customApiKeyResponses.approved`` (its own rule). The key rotates per boot,
    so this is re-seeded on every publish and a stale rejection of the same key is dropped (#8719).
    Existing content (MCP servers, migrations) is merged, never replaced. The file is the owner's,
    mode 0600, written atomically; a symlink or a foreign owner is refused.
    """
    path = home / ".claude.json"
    current: dict = {}
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid:
            raise ValueError("untrusted Claude state file")
        try:
            loaded = json.loads(path.read_text())
            current = loaded if isinstance(loaded, dict) else {}
        except (ValueError, OSError):
            current = {}
    digest = secret.strip()[-20:]
    responses = current.get("customApiKeyResponses") if isinstance(current.get("customApiKeyResponses"), dict) else {}
    strings = lambda value: [item for item in value if isinstance(item, str)] if isinstance(value, list) else []  # noqa: E731
    approved = strings(responses.get("approved"))
    seeded = {
        **current,
        "hasCompletedOnboarding": True,
        "theme": current.get("theme") if isinstance(current.get("theme"), str) else "dark",
        "customApiKeyResponses": {
            "approved": approved + ([digest] if digest not in approved else []),
            "rejected": [item for item in strings(responses.get("rejected")) if item != digest],
        },
    }
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file_descriptor = os.open(".claude.json.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        try:
            os.fchown(file_descriptor, owner_uid, owner_gid)
            with os.fdopen(file_descriptor, "w") as destination:
                file_descriptor = -1
                json.dump(seeded, destination, indent=2)
                destination.flush()
                os.fsync(destination.fileno())
            os.rename(".claude.json.tmp", ".claude.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            try:
                os.unlink(".claude.json.tmp", dir_fd=descriptor)
            except FileNotFoundError:
                pass
    finally:
        os.close(descriptor)


def publish(payload: dict, *, root: Path = Path("/run/adp"), directory: Path = Path("/run/adp-dev-box-shell"),
            owner_uid: int, owner_gid: int, trusted_uid: int = 0, home: Path | None = Path("/home/ec2-user")) -> None:
    parent = root.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != trusted_uid or parent.st_mode & 0o022:
        raise ValueError("untrusted host runtime directory")
    ready = trusted_receipt(root / "crew-ready", trusted_uid)
    model = trusted_receipt(root / "crew-model.json", trusted_uid)
    # model_aliases (optional): what Claude Code's opus/sonnet/haiku resolve to on this box; each a model id.
    aliases = payload.get("model_aliases", {})
    if (
        set(payload) - {"model_aliases"} != {"run_id", "model_id", "base_url", "secret"}
        or any(not isinstance(value, str) for key, value in payload.items() if key != "model_aliases")
        or not isinstance(aliases, dict)
        or set(aliases) - {"opus", "sonnet", "haiku"}
        or any(not isinstance(value, str) or not MODEL_ID.fullmatch(value) for value in aliases.values())
        or not (RUN_ID.fullmatch(payload["run_id"]) and MODEL_ID.fullmatch(payload["model_id"])
                and LOOPBACK.fullmatch(payload["base_url"]) and SECRET.fullmatch(payload["secret"]))
    ):
        raise ValueError("invalid shell model data")
    if ready.get("run_id") != payload["run_id"] or model.get("run_id") != payload["run_id"] or model.get("model_id") != payload["model_id"] or model.get("lease_generation") != ready.get("lease_generation"):
        raise ValueError("shell model authority changed")
    owner_directory(directory, owner_uid, owner_gid, create=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        data = (f"ANTHROPIC_BASE_URL={payload['base_url']}\nANTHROPIC_API_KEY={payload['secret']}\n"
                f"ANTHROPIC_MODEL={payload['model_id']}\nADP_CREW_RUN_ID={payload['run_id']}\n")
        data += "".join(f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL={aliases[family]}\n" for family in ("opus", "sonnet", "haiku") if family in aliases)
        file_descriptor = os.open(".model.env.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=descriptor)
        try:
            os.fchown(file_descriptor, owner_uid, owner_gid)
            with os.fdopen(file_descriptor, "w") as destination:
                file_descriptor = -1
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            os.rename(".model.env.tmp", "model.env", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            try:
                os.unlink(".model.env.tmp", dir_fd=descriptor)
            except FileNotFoundError:
                pass
    finally:
        os.close(descriptor)
    if home is not None and home.is_dir():
        seed_claude_onboarding(home, payload["secret"], owner_uid=owner_uid, owner_gid=owner_gid)


def remove(*, directory: Path = Path("/run/adp-dev-box-shell"), owner_uid: int, owner_gid: int) -> None:
    if not owner_directory(directory, owner_uid, owner_gid):
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink("model.env", dir_fd=descriptor)
        except FileNotFoundError:
            pass
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        if os.getuid() != 0 or os.environ.get("SUDO_USER", "agent") != "agent" or sys.argv[1:] not in ([], ["--remove"]):
            raise ValueError("unauthorized shell publisher")
        owner = pwd.getpwnam("ec2-user")
        if sys.argv[1:]:
            remove(owner_uid=owner.pw_uid, owner_gid=owner.pw_gid)
        else:
            raw = sys.stdin.read(4097)
            if len(raw) > 4096:
                raise ValueError("shell model data too large")
            publish(json.loads(raw), owner_uid=owner.pw_uid, owner_gid=owner.pw_gid)
    except (OSError, ValueError, KeyError, TypeError):
        print("Dev Box shell model publication refused", file=sys.stderr)
        sys.exit(1)
