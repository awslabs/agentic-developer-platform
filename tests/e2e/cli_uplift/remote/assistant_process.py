"""Ordinary-user transport in a fail-closed Linux syscall sandbox.

Only an already connected target socket and RPC pipes cross the boundary. After
trusted imports and TLS setup, the child cannot open files, create/connect sockets,
execute programs, inspect another process or change its confinement. Tokens are
sent over stdin only after the child confirms that confinement is active.
"""

import ctypes
import errno
import http.client
import ipaddress
import json
import os
import resource
import selectors
import socket
import ssl
import subprocess
import sys
import sysconfig
import tempfile
import termios
import time
import urllib.parse
from pathlib import Path

if __package__:
    from . import assistant_client
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import assistant_client


LIMIT = 1024 * 1024 + 8192


def restrict():
    """Allow only computation and IO on existing descriptors; never fall back."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]

    class Comparison(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_uint),
            ("datum_a", ctypes.c_uint64),
            ("datum_b", ctypes.c_uint64),
        ]

    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(Comparison),
    ]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(4, 0, 0, 0, 0) != 0:
        raise RuntimeError(
            "Assistant isolation requires no-new-privileges and no dumps"
        )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    context = library.seccomp_init(0x00050000 | errno.EPERM)
    if not context:
        raise RuntimeError("Assistant syscall isolation unavailable")
    try:
        for name in (
            "read",
            "write",
            "close",
            "fstat",
            "lseek",
            "mmap",
            "mprotect",
            "munmap",
            "mremap",
            "madvise",
            "brk",
            "futex",
            "clock_gettime",
            "gettimeofday",
            "time",
            "getrandom",
            "getpid",
            "gettid",
            "rt_sigaction",
            "rt_sigprocmask",
            "rt_sigreturn",
            "sigaltstack",
            "exit",
            "exit_group",
            "poll",
            "ppoll",
            "select",
            "pselect6",
            "recvfrom",
            "sendto",
            "recvmsg",
            "sendmsg",
            "shutdown",
            "getsockopt",
            "setsockopt",
            "getpeername",
            "getsockname",
            "fcntl",
            "restart_syscall",
            "sched_yield",
        ):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if (
                number >= 0
                and library.seccomp_rule_add(context, 0x7FFF0000, number, 0) != 0
            ):
                raise RuntimeError("Assistant syscall isolation rule failed")
        comparison = Comparison(1, 4, termios.FIONBIO, 0)
        if (
            library.seccomp_rule_add_array(
                context,
                0x7FFF0000,
                library.seccomp_syscall_resolve_name(b"ioctl"),
                1,
                ctypes.byref(comparison),
            )
            != 0
        ):
            raise RuntimeError("Assistant nonblocking IO rule failed")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("Assistant syscall isolation could not be enforced")
    finally:
        library.seccomp_release(context)


def python_env(home):
    """Keep the interpreter's own shared library, never inherited loader paths.

    setup-python binaries can otherwise resolve a different system libpython
    after environment scrubbing, making native stdlib imports fail before seccomp.
    """
    env = assistant_client.ordinary_env(home)
    if sysconfig.get_config_var("Py_ENABLE_SHARED"):
        # setup-python relocates its installation; LIBDIR can retain the build
        # machine's prefix. Prefer the active base installation (also for venvs).
        candidates = [Path(sys.base_prefix) / "lib"]
        configured = sysconfig.get_config_var("LIBDIR")
        if configured:
            candidates.append(Path(configured))
        library = sysconfig.get_config_var("LDLIBRARY")
        directory = next(
            (
                path
                for path in candidates
                if path.is_absolute() and library and (path / library).is_file()
            ),
            None,
        )
        if directory is None:
            raise assistant_client.ClientError(
                "Python runtime library directory unavailable"
            )
        env["LD_LIBRARY_PATH"] = str(directory)
    return env


class ProcessClient:
    def __init__(self, connection):
        self.home = tempfile.TemporaryDirectory(prefix="assistant-user-")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    str(connection.fileno()),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                pass_fds=(connection.fileno(),),
                close_fds=True,
                cwd=self.home.name,
                env=python_env(self.home.name),
                bufsize=0,
            )
        except BaseException:
            self.home.cleanup()
            raise
        self.pending = bytearray()
        os.set_blocking(self.process.stdin.fileno(), False)
        try:
            if self._read() != {"isolated": True}:
                raise assistant_client.ClientError("Assistant process isolation failed")
        except BaseException:
            self.close()
            raise

    def _read(self, timeout=20):
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise assistant_client.ClientError(
                        "Assistant isolated client timed out"
                    )
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    raise assistant_client.ClientError(
                        "Assistant isolated client stopped; Linux libseccomp is required"
                    )
                self.pending.extend(chunk)
                if len(self.pending) > LIMIT:
                    raise assistant_client.ClientError(
                        "Assistant isolated evidence exceeds limit"
                    )
        line, _, rest = self.pending.partition(b"\n")
        self.pending = bytearray(rest)
        return json.loads(line)

    def call(self, action, **payload):
        encoded = json.dumps({"action": action, **payload}).encode() + b"\n"
        if len(encoded) > 65535:
            raise assistant_client.ClientError(
                "Assistant isolated request exceeds limit"
            )
        try:
            deadline = time.monotonic() + 20
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE)
                while encoded:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise assistant_client.ClientError(
                            "Assistant isolated request timed out"
                        )
                    written = os.write(self.process.stdin.fileno(), encoded[:4096])
                    encoded = encoded[written:]
            response = self._read(130 if action == "receive" else 20)
            if "error" in response:
                raise assistant_client.ClientError(response["error"])
            return response.get("result")
        except BaseException:
            self.close()
            raise

    def send(self, document):
        return self.call("send", document=document)

    def receive(self):
        return self.call("receive")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.home.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def start(url, session, mode):
    parsed = urllib.parse.urlsplit(url)
    if mode == "websocket":
        assistant_client.websocket_url(url, session)
    if (
        parsed.scheme not in {"https", "wss"}
        or not parsed.hostname
        or parsed.username
        or parsed.fragment
    ):
        raise assistant_client.ClientError("Assistant requires a secure target")
    token_kind = "id_token" if mode == "websocket" else "access_token"
    tokens = {
        token_kind: session.token(token_kind),
        "expires_at": session.tokens["expires_at"],
    }
    candidates = socket.getaddrinfo(
        parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
    )
    for family, kind, protocol, _name, address in candidates:
        target = ipaddress.ip_address(address[0])
        target = getattr(target, "ipv4_mapped", None) or target
        if (
            target.is_link_local
            or target.is_loopback
            or target.is_unspecified
            or target.is_multicast
            or str(target) == "fd00:ec2::254"
        ):
            raise assistant_client.ClientError(
                "Assistant target cannot be metadata or a local service"
            )
    if not candidates:
        raise assistant_client.ClientError("Assistant target has no address")
    family, kind, protocol, _name, address = candidates[0]
    with socket.socket(family, kind, protocol) as connection:
        connection.settimeout(15)
        connection.connect(address)
        client = ProcessClient(connection)
    try:
        client.call("initialize", url=url, mode=mode, tokens=tokens)
        return client
    except BaseException:
        client.close()
        raise


def worker():
    connection = socket.socket(fileno=int(sys.argv[1]))
    connection.settimeout(15)
    tls = ssl.create_default_context()
    "idna".encode("idna")
    restrict()
    print(json.dumps({"isolated": True}), flush=True)
    websocket = None
    http_socket = None
    parsed = None
    session = None
    for _operation in range(1024):
        raw = sys.stdin.buffer.readline(65536)
        if not raw:
            break
        try:
            if len(raw) > 65535:
                raise assistant_client.ClientError(
                    "Assistant isolated request exceeds limit"
                )
            request = json.loads(raw)
            action = request["action"]
            result = None
            if action == "initialize" and session is None:
                session = assistant_client.UserSession(request["tokens"])
                parsed = urllib.parse.urlsplit(request["url"])
                if request["mode"] == "websocket":
                    websocket = assistant_client.connect(
                        request["url"],
                        session,
                        dial=lambda *_args, **_kwargs: connection,
                        tls=lambda: tls,
                    )
                else:
                    http_socket = tls.wrap_socket(
                        connection, server_hostname=parsed.hostname
                    )
            elif action == "http" and http_socket is not None:
                client = http.client.HTTPConnection(parsed.hostname, parsed.port or 443)
                client.sock = http_socket
                path = urllib.parse.urlunsplit(
                    ("", "", parsed.path or "/", parsed.query, "")
                )
                client.request(
                    "GET",
                    path,
                    headers={
                        "Authorization": "Bearer " + session.token(),
                        "Accept": "application/json",
                    },
                )
                response = client.getresponse()
                if response.status != 200:
                    raise assistant_client.ClientError(
                        f"Assistant HTTP request returned {response.status}"
                    )
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise assistant_client.ClientError(
                        "Assistant HTTP response exceeds limit"
                    )
                result = json.loads(data)
                client.close()
                http_socket = None
            elif action == "send" and websocket is not None:
                websocket.send(request["document"])
            elif action == "receive" and websocket is not None:
                result = websocket.receive(timeout=120)
            else:
                raise assistant_client.ClientError(
                    "Invalid assistant isolated operation"
                )
            encoded = json.dumps({"result": result})
            if len(encoded) > LIMIT:
                raise assistant_client.ClientError(
                    "Assistant isolated evidence exceeds limit"
                )
            print(encoded, flush=True)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            http.client.HTTPException,
        ) as exc:
            message = (
                str(exc)
                if isinstance(exc, assistant_client.ClientError)
                else f"Assistant isolated transport failed ({type(exc).__name__})"
            )
            print(json.dumps({"error": message}), flush=True)
            break


if __name__ == "__main__":
    worker()
