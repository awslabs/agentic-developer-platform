#!/usr/local/bin/python3
"""PID 1 for the disposable exact-image isolation test VM; never shipped at runtime."""
import ctypes
import os
from pathlib import Path
import resource
import subprocess
import sys

libc = ctypes.CDLL(None, use_errno=True)


def mount(source, target, filesystem, flags=0, data=None):
    result = libc.mount(source.encode(), target.encode(), filesystem.encode(), flags,
                        data.encode() if data else None)
    if result != 0:
        raise OSError(ctypes.get_errno(), f"mount {target}")


def worker_identity():
    # The test supervisor has the production UID and no capabilities. Its child
    # installs the actual worker's Landlock/seccomp restrictions, without relying
    # on the CodeBuild host's LSM configuration or Docker syscall filter.
    for capability in range(41):
        if libc.prctl(24, capability, 0, 0, 0) != 0:  # PR_CAPBSET_DROP
            raise OSError(ctypes.get_errno(), "drop capability")
    os.setgroups([])
    os.setgid(61161)
    os.setuid(61161)
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "no_new_privs")


status = 125
try:
    # initramfs hands over /dev and /proc. The root disk is mounted read-only;
    # only scratch is writable, as in the production pod.
    mount("tmpfs", "/tmp", "tmpfs", 2 | 4, "size=256m,mode=1777")  # nosuid,nodev
    assert os.statvfs("/").f_flag & os.ST_RDONLY, "root must be read-only"
    assert Path("/proc/self/status").exists(), "proc must be mounted for negative probes"
    abi = libc.syscall(444, 0, 0, 1)
    print(f"Exact-image kernel: {os.uname().release}; Landlock ABI: {abi}", flush=True)
    assert abi >= 3, "test kernel must support the production Landlock requirement"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "/app/tests/test_isolation.py"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
             "PYTHONDONTWRITEBYTECODE": "1"},
        preexec_fn=worker_identity,
        timeout=480,
    )
    status = result.returncode
except BaseException as exc:
    print(f"Exact-image VM failed: {exc}", flush=True)
finally:
    print(f"ADP_CYBER_IMAGE_RESULT={status}", flush=True)
    os.sync()
    # PID 1 remains only as a test harness and powers off the disposable guest.
    libc.reboot(0x4321FEDC)  # LINUX_REBOOT_CMD_POWER_OFF
    os._exit(125)
