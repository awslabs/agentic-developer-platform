"""Real-kernel network confinement tests; also run as the built image's agent user."""

import ctypes
import errno
import importlib.util
import json
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WRAPPER = Path(__file__).resolve().parents[1] / "lib/task_network_exec.py"
spec = importlib.util.spec_from_file_location("task_network_exec", WRAPPER)
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class NetworkFilterTests(unittest.TestCase):
    def run_child(self, source):
        return subprocess.run(
            [sys.executable, str(WRAPPER), sys.executable, "-c", source],
            capture_output=True, text=True, timeout=10,
        )

    def test_native_socket_and_socketpair_denied(self):
        result = self.run_child('''import errno, json, socket
errors = []
for operation in (socket.socket, socket.socketpair):
    try:
        operation()
    except OSError as error:
        errors.append(error.errno)
    else:
        errors.append(0)
print(json.dumps(errors))
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [errno.EPERM, errno.EPERM])

    def test_io_uring_entrypoints_denied(self):
        # Null arguments would give EFAULT/EINVAL/ENOSYS without our filter.
        # Deny even enter/register so an inherited ring cannot enable networking.
        result = self.run_child('''import ctypes, json
libc = ctypes.CDLL(None, use_errno=True)
errors = []
for number in (425, 426, 427):
    ctypes.set_errno(0)
    result = libc.syscall(number, 0, 0, 0, 0, 0, 0)
    errors.append([result, ctypes.get_errno()])
print(json.dumps(errors))
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [[-1, errno.EPERM]] * 3)

    def test_legitimate_exec_and_descendant_inheritance(self):
        result = self.run_child('''import json, subprocess, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
    file = Path(directory) / "fixture"
    file.write_text("ordinary task output")
    child = subprocess.run([sys.executable, "-c",
        "import socket; socket.socket()"], capture_output=True, text=True)
    print(json.dumps({"data": file.read_text(), "child": child.returncode,
                      "denied": "PermissionError" in child.stderr}))
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "data": "ordinary task output", "child": 1, "denied": True,
        })

    def test_missing_command(self):
        with patch.object(sys, "argv", [str(WRAPPER)]):
            self.assertEqual(network.main(), 64)

    def test_unsupported_architecture_never_executes(self):
        with patch.object(network.platform, "machine", return_value="unsupported"), \
             patch.object(network.os, "execvpe") as execute, \
             patch.object(sys, "argv", [str(WRAPPER), "unused"]):
            self.assertEqual(network.main(), 70)
            execute.assert_not_called()

    def test_filter_install_failure_never_executes(self):
        for results in ([-1], [0, -1]):
            with self.subTest(prctl_results=results), \
                 patch.object(network.ctypes, "CDLL") as library, \
                 patch.object(network.os, "execvpe") as execute, \
                 patch.object(sys, "argv", [str(WRAPPER), "unused"]):
                library.return_value.prctl.side_effect = results
                self.assertEqual(network.main(), 70)
                execute.assert_not_called()

    def test_both_supported_architectures_reject_other_abis(self):
        # Evaluate the actual generated classic BPF, including the ARM contract
        # on x86 CI. Real-kernel tests above additionally check this host's ABI.
        for machine, audit_arch, socket_number in (
            ("x86_64", 0xC000003E, 41), ("amd64", 0xC000003E, 41),
            ("aarch64", 0xC00000B7, 198), ("arm64", 0xC00000B7, 198),
        ):
            instructions = []

            def prctl(option, *args):
                if option == 22:
                    program = ctypes.cast(args[1], ctypes.POINTER(network._Program)).contents
                    instructions.extend((f.code, f.jt, f.jf, f.k)
                                        for f in program.filter[:program.len])
                return 0

            with self.subTest(machine=machine), \
                 patch.object(network.platform, "machine", return_value=machine), \
                 patch.object(network.ctypes, "CDLL") as library:
                library.return_value.prctl.side_effect = prctl
                network._deny_network()

                def evaluate(arch, number):
                    accumulator, pc = 0, 0
                    while pc < len(instructions):
                        code, jt, jf, value = instructions[pc]
                        if code == 0x20:
                            accumulator = {0: number, 4: arch}[value]
                        elif code in (0x15, 0x45):
                            match = accumulator == value if code == 0x15 else bool(accumulator & value)
                            pc += jt if match else jf
                        elif code == 0x06:
                            return value
                        else:
                            self.fail(f"unexpected BPF instruction {code}")
                        pc += 1
                    self.fail("filter fell through without a verdict")

                self.assertEqual(evaluate(audit_arch, socket_number), 0x00050001)
                self.assertEqual(evaluate(audit_arch, 425), 0x00050001)
                self.assertEqual(evaluate(audit_arch, 1), 0x7FFF0000)
                for other in (0x40000003, 0x40000028, 0, 0xC000003E, 0xC00000B7):
                    if other != audit_arch:
                        self.assertEqual(evaluate(other, 359), 0x80000000)
                if machine in ("x86_64", "amd64"):
                    self.assertEqual(evaluate(audit_arch, 0x40000029), 0x80000000)


@unittest.skipUnless(platform.machine().lower() in ("x86_64", "amd64"), "x86 syscall ABIs")
class CompatibilityKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="task-network-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        source = root / "abi.c"
        source.write_text(r'''
#include <stdint.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
static unsigned int socket_args[] = {2, 1, 0};
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    struct rlimit no_core = {0, 0};
    if (setrlimit(RLIMIT_CORE, &no_core)) return 2;
    int result;
    if (!strcmp(argv[1], "compat-socket")) {
        __asm__ volatile("int $0x80" : "=a"(result)
            : "0"(359), "b"(2), "c"(1), "d"(0) : "memory");
    } else if (!strcmp(argv[1], "compat-socketcall")) {
        __asm__ volatile("int $0x80" : "=a"(result)
            : "0"(102), "b"(1), "c"((unsigned int)(uintptr_t)socket_args) : "memory");
    } else {
        result = syscall(0x40000000L | 41, 2, 1, 0);
    }
    if (result >= 0) close(result);
    // Returning at all means the filter did not reject the unsupported ABI.
    // This must fail even on kernels which return ENOSYS for the syscall.
    return 0;
}
''')
        cls.probe = root / "abi"
        subprocess.run(["gcc", "-Wall", "-Wextra", "-Werror", "-no-pie",
                        str(source), "-o", str(cls.probe)], check=True, timeout=30,
                       capture_output=True, text=True)

    def test_compatibility_network_syscalls_kill_child(self):
        for mode in ("compat-socket", "compat-socketcall", "x32-socket"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, str(WRAPPER), str(self.probe), mode],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, -signal.SIGSYS, result.stderr)


if __name__ == "__main__":
    unittest.main()
