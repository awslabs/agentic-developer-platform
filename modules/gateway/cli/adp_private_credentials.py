#!/usr/bin/env python3
"""Trusted private native credential consumer; never a generic CLI/MCP result.

This module runs only inside a trusted, single-threaded native host. Selection
and canonical human/session authority must come from the separately reviewed
native interaction and issuer, never tool arguments or a worker credential.
The installed flat CLI intentionally does not import or ship this module yet.
Linux x86_64, cryptography, pidfds, sealed executable memfds, seccomp and a private
tmpfs root are required. Missing prerequisites refuse before an offer is made.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import resource
import select
import signal
import socket
import stat
import time
from dataclasses import dataclass
from typing import Any

VERSION = "adp-native-private-delivery-v1"
MAX_WIRE = 196608
MAX_FILE = 65536
PREPARE_SECONDS = 90
TMPFS_MAGIC = 0x01021994
_IDS = ("execution_id", "request_id", "receipt_id")
_SELECTION_FIELDS = {
    "issuer",
    "audience",
    "key_id",
    "tenant_id",
    "user_id",
    "session_id",
    "execution_id",
    "request_id",
    "receipt_id",
    "policy_version",
    "argument_digest",
    "credential_id",
    "service",
    "label",
    "operation",
    "argv",
    "cwd",
    "use_seconds",
}


class HostUnavailableError(Exception):
    """Fixed nonsecret message only; never include exception/response payloads."""


def canonical(value: Any) -> bytes:
    """Wire encoding: UTF-8 sorted JSON, ASCII escapes, no insignificant spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_WIRE:
        raise ValueError
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if b64(decoded) != value:
        raise ValueError
    return decoded


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode(value: bytes) -> dict:
    if not value or len(value) > MAX_WIRE:
        raise ValueError
    result = json.loads(value, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(result, dict):
        raise ValueError
    return result


def _send(channel, value):
    wire = canonical(value)
    if len(wire) > MAX_WIRE or channel.send(wire) != len(wire):
        raise ValueError


def _recv(channel):
    wire, _, flags, _ = channel.recvmsg(MAX_WIRE)
    if flags & socket.MSG_TRUNC:
        raise ValueError
    return _decode(wire)


def _crypto():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa, x25519
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return hashes, serialization, ed25519, padding, rsa, x25519, AESGCM, HKDF


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _linux_fd(number, *arguments):
    # Some supported CPython builds omit Linux-specific os/fcntl wrappers.
    # These syscall numbers/constants are native x86_64 only (checked first).
    value = ctypes.CDLL(None, use_errno=True).syscall(number, *arguments)
    if value < 0:
        raise OSError
    return value


def _prctl(option, argument):
    if ctypes.CDLL(None, use_errno=True).prctl(option, argument, 0, 0, 0) != 0:
        raise OSError


def _harden(keep):
    # Fork before any credential/key exists. Do not retain unrelated sockets,
    # terminals, loggers or native session files in the private processes.
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _prctl(4, 0)  # PR_SET_DUMPABLE
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGCHLD):
        signal.signal(sig, signal.SIG_DFL)


def _seccomp_group_bound():
    """Inherited filter: descendants cannot leave the watchdog-owned group.

    Only native x86_64 is supported; reject compat/x32 syscall bypasses. clone3
    returns ENOSYS for libc's ordinary clone fallback. Namespace clones fail.
    This is a lifetime boundary, not a sandbox for an untrusted recipient.
    """

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    allow, deny, kill = 0x7FFF0000, 0x00050000 | errno.EPERM, 0x80000000
    rows = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E), (0x06, 0, 0, kill), (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, kill)]
    # setpgid, setsid, unshare, setns; prevent ptrace/process_vm_writev meddling.
    for syscall in (109, 112, 272, 308, 101, 311):
        rows.extend([(0x15, 0, 1, syscall), (0x06, 0, 0, deny)])
    rows.extend([(0x15, 0, 1, 435), (0x06, 0, 0, 0x00050000 | errno.ENOSYS)])
    # clone(flags) permits ordinary descendants, denies every CLONE_NEW* flag.
    rows.extend([(0x15, 0, 3, 56), (0x20, 0, 0, 16), (0x45, 0, 1, 0x7E020000), (0x06, 0, 0, deny), (0x06, 0, 0, allow)])
    array = (Filter * len(rows))(*(Filter(*row) for row in rows))
    program = Program(len(rows), array)
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS, inherited through exec/fork
    if ctypes.CDLL(None, use_errno=True).prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError


def _directory(path, *, volatile=False):
    # Walk from / using directory-relative O_NOFOLLOW at *every* component.
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.split("/"):
            if component in ("", "."):
                continue
            if component == "..":
                raise ValueError
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if volatile:
            info = os.fstat(fd)
            buffer = ctypes.create_string_buffer(256)
            if (
                info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or ctypes.CDLL(None).fstatfs(fd, buffer) != 0
                or ctypes.c_long.from_buffer(buffer).value != TMPFS_MAGIC
            ):
                raise ValueError
        return fd
    except BaseException:
        os.close(fd)
        raise


def _sealed_executable(path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError
    parent, name = os.path.split(path)
    directory = _directory(parent)
    source = target = None
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111 or info.st_size > 128 * 1024 * 1024:
            raise ValueError
        # Scripts would reintroduce an unbound interpreter and writable source.
        if os.read(source, 4) != b"\x7fELF":
            raise ValueError
        os.lseek(source, 0, os.SEEK_SET)
        target = _linux_fd(319, b"adp-selected-executable", 3)  # memfd_create(CLOEXEC | ALLOW_SEALING)
        digest = hashlib.sha256()
        while data := os.read(source, 65536):
            digest.update(data)
            offset = 0
            while offset < len(data):
                offset += os.write(target, data[offset:])
        os.fchmod(target, 0o500)
        fcntl.fcntl(target, 1033, 15)
        os.lseek(target, 0, os.SEEK_SET)
        result, target = target, None
        return result, digest.hexdigest()
    finally:
        for fd in (directory, source, target):
            if fd is not None:
                os.close(fd)


def _validate_selection(selection):
    if not isinstance(selection, dict) or set(selection) != _SELECTION_FIELDS:
        raise ValueError
    for key in _SELECTION_FIELDS - {"argv", "use_seconds"}:
        if not isinstance(selection[key], str) or not selection[key] or len(selection[key]) > 2048 or "\0" in selection[key]:
            raise ValueError
    argv = selection["argv"]
    if not isinstance(argv, list) or not argv or len(argv) > 256 or any(not isinstance(v, str) or "\0" in v or len(v) > 16384 for v in argv):
        raise ValueError
    if selection["operation"] not in {"assume_role", "materialize"}:
        raise ValueError
    duration = selection["use_seconds"]
    if type(duration) is not int or not 1 <= duration <= 900:
        raise ValueError
    if selection["operation"] == "assume_role" and duration != 900:
        raise ValueError
    if len(selection["argument_digest"]) != 64 or any(c not in "0123456789abcdef" for c in selection["argument_digest"]):
        raise ValueError
    if selection["audience"] != "adp-native-private-host":
        raise ValueError


def _outcome(selection, state, code, **extra):
    return {"type": "result", "delivery_state": state, "code": code, **{key: selection[key] for key in _IDS}, **extra}


def _recipient(channel, selection, command_fd, cwd_fd, sink_fd, identities, issuer_key):
    """Only this process creates recipient keys or sees decrypted bytes."""
    hashes, serialization, ed25519, padding, rsa, x25519, aesgcm, hkdf = _crypto()
    private = x25519.X25519PrivateKey.generate()
    signing = ed25519.Ed25519PrivateKey.generate()
    offer = {
        "version": VERSION,
        "selection": selection,
        **identities,
        "nonce": b64(os.urandom(32)),
        "encryption_public_key": b64(private.public_key().public_bytes_raw()),
        "proof_public_key": b64(signing.public_key().public_bytes_raw()),
    }
    offer["recipient_fingerprint"] = _digest({key: offer[key] for key in ("encryption_public_key", "proof_public_key")})
    _send(channel, {"type": "offer", "offer": offer})
    proof_made = False
    while True:
        message = _recv(channel)
        if message.get("type") == "prove" and set(message) == {"type", "challenge"} and not proof_made:
            challenge = unb64(message["challenge"])
            if len(challenge) != 32:
                raise ValueError
            proof_made = True
            proof = {"version": VERSION, "offer": offer, "challenge": message["challenge"]}
            _send(channel, {"type": "proof", "proof": proof, "signature": b64(signing.sign(canonical(proof)))})
        elif message.get("type") == "deliver" and set(message) == {"type", "envelope"} and proof_made:
            break
        else:
            raise ValueError
    envelope = message["envelope"]
    if not isinstance(envelope, dict) or set(envelope) != {"body", "signature"}:
        raise ValueError
    body = envelope["body"]
    if not isinstance(body, dict) or set(body) != {
        "version",
        "issuer",
        "audience",
        "key_id",
        "offer",
        "challenge",
        "issued_at",
        "delivery_expires_at",
        "credential_expires_at",
        "ephemeral_public_key",
        "iv",
        "ciphertext",
    }:
        raise ValueError
    issuer = serialization.load_pem_public_key(issuer_key)
    if not isinstance(issuer, rsa.RSAPublicKey) or issuer.key_size < 2048:
        raise ValueError
    issuer.verify(unb64(envelope["signature"]), canonical(body), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    if (
        body["version"] != VERSION
        or canonical(body["offer"]) != canonical(offer)
        or body["challenge"] != b64(challenge)
        or any(body[key] != selection[key] for key in ("issuer", "audience", "key_id"))
    ):
        raise ValueError
    issued, expiry, credential_expiry = (body[key] for key in ("issued_at", "delivery_expires_at", "credential_expires_at"))
    now = time.time()
    if (
        any(type(v) is not int for v in (issued, expiry, credential_expiry))
        or not issued <= now < expiry <= issued + PREPARE_SECONDS
        or not now < credential_expiry <= issued + selection["use_seconds"]
    ):
        raise ValueError
    if selection["operation"] == "assume_role" and credential_expiry - issued != 900:
        raise ValueError
    header = {key: value for key, value in body.items() if key != "ciphertext"}
    aad = canonical(header)
    shared = private.exchange(x25519.X25519PublicKey.from_public_bytes(unb64(body["ephemeral_public_key"])))
    key = hkdf(algorithm=hashes.SHA256(), length=32, salt=unb64(offer["nonce"]), info=VERSION.encode() + hashlib.sha256(aad).digest()).derive(shared)
    iv = unb64(body["iv"])
    if len(iv) != 12:
        raise ValueError
    plaintext = aesgcm(key).decrypt(iv, unb64(body["ciphertext"]), aad)
    payload = _decode(plaintext)
    del plaintext, key, shared, private, signing, envelope, message, body
    # Deliberately construct a minimal environment. No ambient AWS/profile,
    # container/web-identity, loader injection, proxy or Python variables survive.
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_CONFIG_FILE": "/dev/null",
    }
    secret_fd = None
    if selection["operation"] == "assume_role":
        if set(payload) != {"access_key_id", "secret_access_key", "session_token"} or any(
            not isinstance(v, str) or not v or len(v) > 16384 or "\0" in v for v in payload.values()
        ):
            raise ValueError
        environment.update(
            zip(
                ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"),
                (payload[k] for k in ("access_key_id", "secret_access_key", "session_token")),
                strict=True,
            )
        )
    else:
        if set(payload) != {"file_base64"}:
            raise ValueError
        material = unb64(payload["file_base64"])
        if not material or len(material) > MAX_FILE:
            raise ValueError
        secret_fd = os.open("credential", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=sink_fd)
        os.fchmod(secret_fd, 0o600)
        # Unlink BEFORE writing a byte. Even a double supervisor/watchdog crash
        # cannot leave a named secret file for a later host to discover.
        os.unlink("credential", dir_fd=sink_fd)
        offset = 0
        while offset < len(material):
            offset += os.write(secret_fd, material[offset:])
        os.lseek(secret_fd, 0, os.SEEK_SET)
        readonly = os.open(f"/proc/self/fd/{secret_fd}", os.O_RDONLY | os.O_CLOEXEC)
        os.close(secret_fd)
        secret_fd = readonly
        environment["ADP_CREDENTIAL_FILE"] = f"/proc/self/fd/{secret_fd}"
        del material
    del payload
    if time.time() >= min(expiry, credential_expiry) or select.select([channel], [], [], 0)[0]:
        raise ValueError
    launch_read, launch_write = os.pipe()  # CLOEXEC by default: EOF proves exec.
    child = os.fork()
    if child == 0:
        try:
            os.close(launch_read)
            keep = {command_fd, cwd_fd, launch_write}
            if secret_fd is not None:
                keep.add(secret_fd)
                os.set_inheritable(secret_fd, True)
            _harden(keep)
            os.fchdir(cwd_fd)
            os.close(cwd_fd)
            if time.time() >= min(expiry, credential_expiry):
                raise ValueError
            os.execve(command_fd, selection["argv"], environment)
        except BaseException:
            try:
                os.write(launch_write, b"failed")
            except OSError:
                pass
            os._exit(126)
    os.close(launch_write)
    environment.clear()
    if secret_fd is not None:
        os.close(secret_fd)
    launch_failed = os.read(launch_read, 16)
    os.close(launch_read)
    if launch_failed:
        os.waitpid(child, 0)
        _send(channel, _outcome(selection, "failed", "child_start_failed", exit_code=126))
        return
    _send(channel, {"type": "started", "credential_expires_at": credential_expiry})
    while True:
        pid, status = os.waitpid(child, os.WNOHANG)
        if pid:
            _send(channel, _outcome(selection, "completed", "child_exited", exit_code=os.waitstatus_to_exitcode(status)))
            return
        if time.time() >= credential_expiry:
            _send(channel, _outcome(selection, "expired", "local_lifetime_ended"))
            os.killpg(os.getpgrp(), signal.SIGKILL)
        if select.select([channel], [], [], 0.05)[0]:
            # The watchdog never sends another message after delivery. EOF means
            # it died: terminate this entire group, including this supervisor.
            os.killpg(os.getpgrp(), signal.SIGKILL)


def _kill_and_reap(group):
    # Keep the supervisor unreaped until the final group signal, preventing a
    # reused PID from becoming an unrelated cleanup target. We are a subreaper.
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            time.sleep(0.01)
    return False


def _watchdog(control, parent_fd, selection, issuer_key, command_fd, cwd_fd, root_fd, command_digest):
    sink_fd = supervisor = None
    sink_name = "adp-private-" + os.urandom(16).hex()
    result = _outcome(selection, "unknown", "host_failed")
    cleaned = False
    try:
        _harden({control.fileno(), parent_fd, command_fd, cwd_fd, root_fd})
        _prctl(36, 1)  # PR_SET_CHILD_SUBREAPER: reap every selected descendant.
        os.mkdir(sink_name, mode=0o700, dir_fd=root_fd)
        sink_fd = os.open(sink_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        os.fchmod(sink_fd, 0o700)
        info = os.fstat(sink_fd)
        sink_digest = _digest(
            {
                "kind": "unlinked-tmpfs-fd" if selection["operation"] == "materialize" else "child-environment",
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": 0o700,
            }
        )
        upstream, downstream = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        supervisor = os.fork()
        if supervisor == 0:
            grouped = False
            try:
                upstream.close()
                os.setpgid(0, 0)
                grouped = True
                _harden({downstream.fileno(), command_fd, cwd_fd, sink_fd})
                _seccomp_group_bound()  # Prerequisite tested BEFORE offer/issuance.
                _recipient(
                    downstream, selection, command_fd, cwd_fd, sink_fd, {"command_digest": command_digest, "sink_digest": sink_digest}, issuer_key
                )
            except BaseException:
                try:
                    _send(downstream, _outcome(selection, "unknown", "delivery_rejected"))
                except BaseException:
                    pass
            finally:
                # Covers watchdog loss during launch/ACK as well as ordinary
                # completion. Never leave descendants solely reliant on a
                # watchdog that may have died concurrently.
                if grouped:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(1)
        downstream.close()
        # Both sides establish the group before any possible termination.
        os.setpgid(supervisor, supervisor)
        for fd in (command_fd, cwd_fd):
            os.close(fd)
        deadline = time.monotonic() + PREPARE_SECONDS
        while True:
            ready, _, _ = select.select([control, upstream, parent_fd], [], [], min(0.1, max(0, deadline - time.monotonic())))
            if parent_fd in ready:
                result = _outcome(selection, "unknown", "host_disconnected")
                break
            if time.monotonic() >= deadline:
                result = _outcome(selection, "expired", "local_lifetime_ended")
                break
            if control in ready:
                try:
                    message = _recv(control)
                except (ValueError, OSError):
                    result = _outcome(selection, "unknown", "host_disconnected")
                    break
                if message == {"type": "cancel"}:
                    result = _outcome(selection, "cancelled", "host_cancelled")
                    break
                _send(upstream, message)
            if upstream in ready:
                try:
                    message = _recv(upstream)
                except (ValueError, OSError):
                    break
                if message["type"] == "result":
                    result = message
                    break
                if message["type"] == "started":
                    deadline = time.monotonic() + max(0, min(selection["use_seconds"], message["credential_expires_at"] - time.time()))
                _send(control, message)
            # Observe without reaping: reserve its PID/group identity for cleanup.
            if os.waitid(os.P_PID, supervisor, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                # Drain a final result sent immediately before supervisor exit.
                if select.select([upstream], [], [], 0)[0]:
                    try:
                        message = _recv(upstream)
                        if message["type"] == "result":
                            result = message
                    except (ValueError, OSError):
                        pass
                break
    except BaseException:
        pass
    finally:
        if supervisor:
            cleaned = _kill_and_reap(supervisor)
        if sink_fd is not None:
            try:
                os.unlink("credential", dir_fd=sink_fd)
            except FileNotFoundError:
                pass
            except OSError:
                cleaned = False
            try:
                actual = os.stat(sink_name, dir_fd=root_fd, follow_symlinks=False)
                expected = os.fstat(sink_fd)
                if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                    raise OSError
                os.rmdir(sink_name, dir_fd=root_fd)
            except OSError:
                cleaned = False
        try:
            _send(control, {**result, "cleanup": "complete" if cleaned else "unknown"})
        except BaseException:
            pass
        os._exit(0)


@dataclass
class PrivateHost:
    """Private native integration API; all returned values are nonsecret.

    Call prepare only after trusted command selection. Pin issuer_key_pem from
    native deployment configuration, not the envelope. No identity assertion in
    this object authenticates a human: the future issuer must do that itself.
    close/cancel terminates the owned group; loss of this process is independent
    watchdog cancellation. No retries, receipt replacement or secret recovery.
    """

    _control: socket.socket
    _watchdog_pid: int
    offer: dict
    _original_ids: dict
    _phase: str = "prepared"
    _result: dict | None = None

    @classmethod
    def prepare(cls, selection: dict, *, issuer_key_pem: bytes, volatile_root: str):
        fds = []
        control = child_control = None
        watchdog = None
        try:
            if platform.system() != "Linux" or platform.machine() != "x86_64" or len(os.listdir("/proc/self/task")) != 1:
                raise ValueError
            _crypto()
            _validate_selection(selection)
            selection = _decode(canonical(selection))
            _, serialization, _, _, rsa, *_ = _crypto()
            key = serialization.load_pem_public_key(issuer_key_pem)
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
                raise ValueError
            root_fd = _directory(volatile_root, volatile=True)
            fds.append(root_fd)
            cwd_fd = _directory(selection["cwd"])
            fds.append(cwd_fd)
            command_fd, executable_digest = _sealed_executable(selection["argv"][0])
            fds.append(command_fd)
            cwd = os.fstat(cwd_fd)
            command_digest = _digest(
                {"executable_sha256": executable_digest, "argv": selection["argv"], "cwd_device": cwd.st_dev, "cwd_inode": cwd.st_ino}
            )
            parent_fd = _linux_fd(434, os.getpid(), 0)  # pidfd_open
            fds.append(parent_fd)
            control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            watchdog = os.fork()
            if watchdog == 0:
                try:
                    _watchdog(child_control, parent_fd, selection, issuer_key_pem, command_fd, cwd_fd, root_fd, command_digest)
                finally:
                    os._exit(1)  # A private child must never resume native code.
            child_control.close()
            control.settimeout(5)
            response = _recv(control)
            if response.get("type") != "offer":
                raise ValueError
            control.settimeout(None)
            return cls(control, watchdog, response["offer"], {key: selection[key] for key in _IDS})
        except BaseException:
            if control is not None:
                control.close()
            if child_control is not None:
                child_control.close()
            if watchdog is not None:
                os.waitpid(watchdog, 0)
            raise HostUnavailableError("native_private_host_unavailable") from None
        finally:
            for fd in fds:
                os.close(fd)

    def prove(self, challenge: str):
        if self._phase != "prepared":
            raise HostUnavailableError("native_private_delivery_already_consumed")
        self._phase = "proved"
        try:
            _send(self._control, {"type": "prove", "challenge": challenge})
            response = _recv(self._control)
        except (ValueError, OSError, TypeError):
            self.cancel()
            raise HostUnavailableError("native_private_delivery_rejected") from None
        if response.get("type") != "proof":
            self._finish(response)
            raise HostUnavailableError("native_private_delivery_rejected")
        return response

    def start(self, envelope: dict):
        if self._phase != "proved":
            raise HostUnavailableError("native_private_delivery_already_consumed")
        self._phase = "consumed"  # Burn before writing, including lost ACK.
        try:
            _send(self._control, {"type": "deliver", "envelope": envelope})
            response = _recv(self._control)
        except (ValueError, OSError, TypeError):
            return self._lost_ack()
        if response.get("type") == "result":
            try:
                self._finish(response)
            except ValueError:
                return self._lost_ack()
        elif (
            set(response) != {"type", "credential_expires_at"} or response["type"] != "started" or type(response["credential_expires_at"]) is not int
        ):
            return self._lost_ack()
        return response

    def _lost_ack(self):
        terminal = self.cancel()
        self._result = _outcome(self._original_ids, "unknown", "delivery_acknowledgement_lost", cleanup=terminal["cleanup"])
        return self._result

    def _finish(self, response):
        allowed = {"type", "delivery_state", "code", "cleanup", "exit_code", *_IDS}
        required = allowed - {"exit_code"}
        if (
            not isinstance(response, dict)
            or not required <= response.keys() <= allowed
            or response.get("type") != "result"
            or response["delivery_state"] not in {"completed", "unknown", "failed", "cancelled", "expired"}
            or response["code"]
            not in {
                "child_exited",
                "child_start_failed",
                "host_cancelled",
                "host_failed",
                "host_disconnected",
                "delivery_rejected",
                "local_lifetime_ended",
            }
            or response["cleanup"] not in {"complete", "unknown"}
            or any(response[key] != self._original_ids[key] for key in _IDS)
            or ("exit_code" in response and type(response["exit_code"]) is not int)
        ):
            raise ValueError
        self._result = response
        self._phase = "closed"
        self._control.close()
        os.waitpid(self._watchdog_pid, 0)
        return response

    def _read_terminal(self):
        # A lost read can leave a nonterminal ACK queued ahead of cancellation.
        # Drain only the bounded public protocol messages; never store an ACK as
        # a final outcome or return unvalidated peer data to generic callers.
        for _ in range(4):
            response = _recv(self._control)
            if response.get("type") == "result":
                return self._finish(response)
            if response.get("type") not in {"started", "proof", "offer"}:
                raise ValueError
        raise ValueError

    def wait(self):
        if self._result is not None:
            return self._result
        if self._phase != "consumed":
            raise HostUnavailableError("native_private_delivery_not_started")
        try:
            return self._read_terminal()
        except (ValueError, OSError):
            return self._lost_ack()

    def cancel(self):
        if self._result is not None:
            return self._result
        try:
            _send(self._control, {"type": "cancel"})
            return self._read_terminal()
        except (ValueError, OSError):
            self._control.close()
            os.waitpid(self._watchdog_pid, 0)
            self._phase = "closed"
            self._result = _outcome(self._original_ids, "unknown", "host_disconnected", cleanup="unknown")
            return self._result

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cancel()
