"""Exec a task child after installing a no-network seccomp filter."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_RET_K = 0x06
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000


class _Filter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


def _deny_network() -> None:
    numbers = {
        "x86_64": (41, 42, 53),
        "amd64": (41, 42, 53),
        "aarch64": (198, 199, 203),
        "arm64": (198, 199, 203),
    }.get(platform.machine().lower())
    if numbers is None:
        raise RuntimeError("unsupported architecture for task network isolation")
    instructions = [_Filter(_BPF_LD_W_ABS, 0, 0, 0)]
    for number in numbers:
        instructions.extend(
            [
                _Filter(_BPF_JMP_JEQ_K, 0, 1, number),
                _Filter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM),
            ]
        )
    instructions.append(_Filter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    filters = (_Filter * len(instructions))(*instructions)
    program = _Program(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not enable no_new_privs")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program)) != 0:
        raise OSError(ctypes.get_errno(), "could not install task network filter")


def main() -> int:
    if len(sys.argv) < 2:
        return 64
    try:
        _deny_network()
        os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
    except (OSError, RuntimeError) as error:
        print(f"task network isolation failed: {type(error).__name__}", file=sys.stderr)
        return 70
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
