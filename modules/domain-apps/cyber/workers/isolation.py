"""Kernel confinement for *all* code that inspects an untrusted sample.

The queue supervisor never imports a native sample parser. A fresh interpreter
executes only after Landlock and seccomp have been installed irreversibly. The
restrictions are inherited by subprocess tools and do not depend on Python AST
validation, an empty environment, or cooperative script behavior.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import sys
import tempfile

MAX_OUTPUT = 1024 * 1024
TIMEOUT = 300


class IsolationError(Exception):
    """No analysis result is available; the stage must fail explicitly."""


def _confine(read_paths: list[str], scratch: str) -> None:
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise IsolationError("isolation_kernel_unsupported")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise IsolationError("isolation_no_new_privs_failed")

    # Landlock v3 includes REFER and TRUNCATE. Handle every filesystem right
    # available through v3; the denied IOCTL_DEV right (v5) is also handled when
    # supported. No device nodes other than /dev/null are exposed.
    abi = libc.syscall(444, 0, 0, 1)  # landlock_create_ruleset(VERSION)
    if abi < 3:
        raise IsolationError("isolation_landlock_unavailable")
    handled = (1 << 15) - 1
    if abi >= 5:
        handled |= 1 << 15

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    ruleset = Ruleset(handled)
    rules_fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if rules_fd < 0:
        raise IsolationError("isolation_landlock_create_failed")
    read = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read directory
    # No device/socket/FIFO creation, hard links, or executable scratch files.
    write = (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 14)
    try:
        for path, rights in [(p, read) for p in read_paths] + [(scratch, (read & ~1) | write)]:
            path = os.path.realpath(path)
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if not os.path.isdir(path):
                    rights &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
                rule = PathRule(rights, fd)
                if libc.syscall(445, rules_fd, 1, ctypes.byref(rule), 0) != 0:
                    raise IsolationError("isolation_landlock_rule_failed")
            finally:
                os.close(fd)
        if libc.syscall(446, rules_fd, 0) != 0:
            raise IsolationError("isolation_landlock_restrict_failed")
    finally:
        os.close(rules_fd)

    # Resolve by syscall name on both supported architectures. libseccomp also
    # rejects unexpected syscall architectures (including x32 on x86_64).
    sec = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    sec.seccomp_init.argtypes = [ctypes.c_uint32]
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    sec.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = sec.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise IsolationError("isolation_seccomp_init_failed")
    deny = """
        socket socketpair connect bind listen accept accept4 sendto sendmsg
        sendmmsg recvfrom recvmsg recvmmsg shutdown
        io_uring_setup io_uring_enter io_uring_register
        ptrace process_vm_readv process_vm_writev pidfd_open pidfd_getfd
        pidfd_send_signal kill tkill tgkill setsid setpgid
        mount umount2 pivot_root chroot move_mount open_tree fsopen fsconfig
        fsmount mount_setattr setns unshare
        bpf perf_event_open userfaultfd keyctl add_key request_key
        kexec_load kexec_file_load init_module finit_module delete_module
        reboot swapon swapoff open_by_handle_at name_to_handle_at
    """.split()
    try:
        for name in deny:
            number = sec.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and sec.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, number, 0) != 0:
                raise IsolationError("isolation_seccomp_rule_failed")
        if sec.seccomp_load(ctx) != 0:
            raise IsolationError("isolation_seccomp_load_failed")
    finally:
        sec.seccomp_release(ctx)


def _launch(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text())
    # Resource limits cover descendants as well. The pod's memory/PID limits
    # remain the aggregate ceiling; these bounds constrain each process.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    os.chdir(config["scratch"])
    _confine(config["read_paths"], config["scratch"])
    os.execve(config["command"][0], config["command"], config["env"])


def run_isolated(command: list[str], inputs: list[Path], *, timeout: float = TIMEOUT) -> dict:
    """Run with exact staged inputs, immutable tools, no cloud identity/network.

    stdout/stderr use capped regular files rather than unbounded pipe buffers.
    Kill the entire process group on *every* exit, including success, so an
    intentionally orphaned descendant cannot survive into the next job.
    """
    code_root = Path(__file__).resolve().parent
    interpreter_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LD_LIBRARY_PATH": str(Path(sys.base_prefix).resolve() / "lib")}
    with tempfile.TemporaryDirectory(prefix="cyber-isolation-") as td:
        root = Path(td)
        scratch = root / "scratch"
        scratch.mkdir(mode=0o700)
        reads = [str(code_root), str(Path(sys.base_prefix).resolve()), str(Path(sys.prefix).resolve()), *map(str, inputs)]
        reads += [p for p in ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache", "/dev/null") if Path(p).exists()]
        for p in ("/rules", "/opt/yara-rules"):
            if Path(p).is_dir():
                reads.append(p)
        config = root / "config.json"
        config.write_text(json.dumps({
            "command": command, "scratch": str(scratch), "read_paths": reads,
            "env": {**interpreter_env, "HOME": str(scratch),
                    "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1",
                    "YARA_RULES_DIR": "/rules" if Path("/rules").is_dir() else "/opt/yara-rules"},
        }))
        with (root / "stdout").open("w+b") as out, (root / "stderr").open("w+b") as err:
            child = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), str(config)],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True,
                start_new_session=True, env=interpreter_env,
            )
            try:
                status = child.wait(timeout=timeout)
                if status != 0:
                    err.seek(0)
                    setup_code = err.read(128).decode("ascii", errors="ignore").strip()
                    if status == 125 and setup_code in {
                        "isolation_kernel_unsupported", "isolation_no_new_privs_failed", "isolation_landlock_unavailable",
                        "isolation_landlock_create_failed", "isolation_landlock_rule_failed", "isolation_landlock_restrict_failed",
                        "isolation_seccomp_init_failed", "isolation_seccomp_rule_failed", "isolation_seccomp_load_failed",
                    }:
                        raise IsolationError(setup_code)
                    raise IsolationError(f"isolated_analysis_failed_exit_{status}")
            except subprocess.TimeoutExpired as exc:
                raise IsolationError("isolated_analysis_timeout") from exc
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            out.seek(0)
            raw = out.read(MAX_OUTPUT + 1)
            if len(raw) >= MAX_OUTPUT:
                raise IsolationError("isolated_analysis_output_limit")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise IsolationError("isolated_analysis_invalid_result") from exc
            if not isinstance(result, dict):
                raise IsolationError("isolated_analysis_invalid_result")
            return result


if __name__ == "__main__":
    try:
        _launch(sys.argv[1])
    except IsolationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(125)
    except Exception:
        # Never echo child paths, environment, output, or credential material.
        sys.exit(125)
