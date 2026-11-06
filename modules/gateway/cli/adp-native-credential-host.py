#!/usr/bin/env python3
"""Human-launched private credential coordinator. No credential bytes cross IPC."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import importlib.util
import json
import os
import platform
import re
import signal
import socket
import stat
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common
import adp_native_credential_handoff as handoff
import adp_private_credentials as private

_spec = importlib.util.spec_from_file_location("adp_native_client", Path(__file__).with_name("adp-native-credentials.py"))
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)
MAX_JOBS = 4


def profile_path(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
        raise ValueError
    return common.private_directory(common.state_dir() / "native-profiles") / (name + ".json")


def config_digest(config):
    keys = {"version", "issuer", "audience", "key_id", "issuer_key_pem", "approval_origin"}
    if (
        not isinstance(config, dict)
        or set(config) != keys
        or config["version"] != private.VERSION
        or any(not isinstance(config[key], str) or not config[key] for key in keys)
    ):
        raise ValueError
    client.https_origin(config["approval_origin"])
    return handoff.digest(config)


def command_digest(body):
    cwd = private._directory(body["cwd"])
    executable = None
    try:
        executable, digest = private._sealed_executable(body["argv"][0])
        info = os.fstat(cwd)
        return private._digest({"executable_sha256": digest, "argv": body["argv"], "cwd_device": info.st_dev, "cwd_inode": info.st_ino})
    finally:
        os.close(cwd)
        if executable is not None:
            os.close(executable)


def register(args, transport):
    binding = handoff.binding_for(transport.base, transport.token)
    config = transport.request("GET", client.PREFIX + "/config")
    body = client.request_body(args, str(uuid.uuid4()))
    del body["request_id"]
    profile = {
        "version": handoff.VERSION,
        "binding": binding,
        "selection": body,
        "config_digest": config_digest(config),
        "command_digest": command_digest(body),
    }
    profile["profile_digest"] = handoff.digest(profile)
    path = profile_path(args.profile)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(profile, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    return {"profile": args.profile, "profile_digest": profile["profile_digest"], "state": "registered"}


def load_profiles(names, binding):
    profiles = []
    for name in names:
        value = common.read_private_json(profile_path(name))
        fields = {"version", "binding", "selection", "config_digest", "command_digest", "profile_digest"}
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["version"] != handoff.VERSION
            or value["binding"] != binding
            or value["profile_digest"] != handoff.digest({k: v for k, v in value.items() if k != "profile_digest"})
            or not handoff.hexadecimal(value["config_digest"])
            or not handoff.hexadecimal(value["command_digest"])
        ):
            raise handoff.UnavailableError("native_profile_unavailable")
        selection = value["selection"]
        if (
            not isinstance(selection, dict)
            or set(selection) != {"credential_id", "service", "label", "operation", "argv", "cwd", "use_seconds"}
            or selection["operation"] not in {"assume_role", "materialize"}
            or not isinstance(selection["argv"], list)
            or not selection["argv"]
            or any(not isinstance(arg, str) or "\0" in arg for arg in selection["argv"])
            or not selection["argv"][0].startswith("/")
            or not isinstance(selection["cwd"], str)
            or type(selection["use_seconds"]) is not int
            or not 1 <= selection["use_seconds"] <= 900
            or selection["operation"] == "assume_role"
            and selection["use_seconds"] != 900
            or any(not isinstance(selection[key], str) or not selection[key] for key in ("credential_id", "service", "label"))
        ):
            raise handoff.UnavailableError("native_profile_unavailable")
        if any(
            p["selection"]["operation"] == selection["operation"]
            and p["selection"]["service"] == selection["service"]
            and p["selection"]["label"] == selection["label"]
            for p in profiles
        ):
            raise handoff.UnavailableError("native_profile_unavailable")
        profiles.append(value)
    return profiles


class PinnedTransport:
    def __init__(self, transport, profile):
        self.transport = transport
        self.profile = profile

    def request(self, method, path, body=None):
        value = self.transport.request(method, path, body)
        if path == client.PREFIX + "/config" and config_digest(value) != self.profile["config_digest"]:
            raise handoff.UnavailableError("native_profile_unavailable")
        return value


def run_job(profile, request_id, arguments, transport, *, no_browser=False):
    body = profile["selection"]
    args = SimpleNamespace(
        **{k: v for k, v in body.items() if k != "argv"},
        command=body["argv"],
        request_id=request_id,
        purpose=arguments.get("purpose"),
        no_browser=no_browser,
    )

    def prepare(selection, **options):
        host = private.PrivateHost.prepare(selection, **options)
        # Compare the actual sealed snapshot and bound cwd, closing the race
        # between readiness and execution without changing the reviewed consumer.
        if host.offer["command_digest"] != profile["command_digest"]:
            host.cancel()
            raise private.HostUnavailableError("native_profile_unavailable")
        return host

    try:
        return client.use_credential(args, PinnedTransport(transport, profile), host_factory=prepare)
    except (handoff.UnavailableError, common.CliError, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
        # No private exception details, argv, proofs or responses are persisted.
        return {
            "request_id": request_id,
            "status": "unknown",
            "delivery_state": "unknown",
            "cleanup": "unknown",
            "code": "native_delivery_not_confirmed",
        }


class Coordinator:
    def __init__(self, profiles, binding, *, transport_factory=client.NativeTransport, no_browser=False):
        self.profiles = profiles
        self.binding = binding
        self.instance = str(uuid.uuid4())
        self.transport_factory = transport_factory
        self.no_browser = no_browser
        self.jobs = {}
        self.listener = None
        self.lock_fd = None
        self.path = None
        self.socket_identity = None
        self.channel = None

    def transport(self):
        result = self.transport_factory()
        if handoff.binding_for(result.base, result.token) != self.binding:
            raise handoff.UnavailableError("authentication_required")
        return result

    def configuration(self, transport):
        config = transport.request("GET", client.PREFIX + "/config")
        config_digest(config)
        return config

    def ready(self, transport):
        config = self.configuration(transport)
        available = []
        for profile in self.profiles:
            try:
                if profile["config_digest"] == config_digest(config) and command_digest(profile["selection"]) == profile["command_digest"]:
                    available.append("credentials___" + profile["selection"]["operation"])
            except (OSError, ValueError):
                continue
        if not available:
            raise handoff.UnavailableError("native_profile_unavailable")
        return {"state": "ready", "tools": sorted(set(available))}

    def select(self, tool, arguments, transport):
        config = self.configuration(transport)
        service = arguments.get("service", "aws")
        profiles = [
            p
            for p in self.profiles
            if "credentials___" + p["selection"]["operation"] == tool
            and p["selection"]["service"] == service
            and ("label" not in arguments or p["selection"]["label"] == arguments["label"])
        ]
        if len(profiles) != 1 or profiles[0]["config_digest"] != config_digest(config):
            raise handoff.UnavailableError("native_profile_unavailable")
        if command_digest(profiles[0]["selection"]) != profiles[0]["command_digest"]:
            raise handoff.UnavailableError("native_profile_unavailable")
        return profiles[0]

    def record_name(self, request_id):
        return "native-mcp-" + handoff.identifier(request_id)

    def record(self, request_id):
        value = common.read_state(self.record_name(request_id))
        if value and (value.get("request_id") != request_id or value.get("binding") != self.binding):
            raise handoff.UnavailableError("native_request_conflict")
        return value

    def reap(self):
        for request_id, (pid, pidfd) in list(self.jobs.items()):
            ended, _ = os.waitpid(pid, os.WNOHANG)
            if ended:
                os.close(pidfd)
                del self.jobs[request_id]

    def status(self, request_id, transport):
        record = self.record(request_id)
        if not record:
            raise handoff.UnavailableError("native_request_conflict")
        try:
            # Renewed native identity may read metadata only. Never fall back to
            # locally saved IDs/outcomes when the issuer denies current ownership.
            value = client.metadata(transport.request("GET", client.PREFIX + "/requests/by-request/" + request_id), request_id)
            local = handoff.project_result(common.read_state("native-credential-" + request_id), request_id)
            if any(key in local and key in value and local[key] != value[key] for key in handoff.IDS):
                raise ValueError
            # Only observe the worker's atomic record. A polling reader must not
            # overwrite a newer terminal worker write with an older HTTP state.
            value = {**local, **value}
            if value.get("status") == "unknown":
                value["delivery_state"] = "unknown"
        except common.CliError as error:
            if error.status_code == 404 and request_id in self.jobs:
                return {"request_id": request_id, "status": "pending", "code": "native_request_pending"}
            raise handoff.UnavailableError("native_delivery_unavailable") from None
        if value.get("status") == "awaiting_approval":
            config = self.configuration(transport)
            value["approval_url"] = client.https_origin(config["approval_origin"]) + "/credentials/native/" + value["receipt_id"]
        if request_id not in self.jobs and value.get("status") in {"created", "awaiting_approval", "approved", "challenged", "delivered"}:
            if value.get("delivery_state") not in {"completed", "cancelled", "failed", "expired"}:
                value.update(delivery_state="unknown", code="native_delivery_not_confirmed")
                value.setdefault("cleanup", "unknown")
        return handoff.project_result(value, request_id)

    def submit(self, request, transport):
        request_id, tool, arguments = request["request_id"], request["tool"], request["arguments"]
        handoff.validate_arguments(tool, arguments)
        previous = self.record(request_id)
        intent_digest = handoff.digest({"tool": tool, "arguments": arguments})
        if previous:
            if previous.get("intent_digest") != intent_digest:
                raise handoff.UnavailableError("native_request_conflict")
            return self.status(request_id, transport)
        profile = self.select(tool, arguments, transport)
        if len(self.jobs) >= MAX_JOBS:
            raise handoff.UnavailableError("native_host_busy")
        # Write-ahead one-shot lease. A crash before fork is deliberately not a
        # retry permission; that original request can only be read/cancelled.
        path = common.state_path(self.record_name(request_id))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(
                {"request_id": request_id, "binding": self.binding, "intent_digest": intent_digest, "profile_digest": profile["profile_digest"]},
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        parent = os.getpid()
        pid = os.fork()
        if pid == 0:
            try:
                if self.listener is not None:
                    self.listener.close()
                if self.channel is not None:
                    self.channel.close()
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                for _, child_fd in self.jobs.values():
                    os.close(child_fd)
                signal.signal(signal.SIGINT, signal.default_int_handler)
                signal.signal(signal.SIGTERM, signal.default_int_handler)
                # Coordinator death interrupts this job; its independent private
                # watchdog still owns recipient teardown if this job also dies.
                if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGINT, 0, 0, 0) != 0 or os.getppid() != parent:
                    os._exit(1)
                run_job(profile, request_id, arguments, transport, no_browser=self.no_browser)
            except BaseException:
                pass
            finally:
                os._exit(0)
        try:
            pidfd = private._linux_fd(434, pid, 0)
        except OSError:
            # This unreaped child cannot have had its PID recycled. Never leave
            # a dispatched job running without a coordinator cancellation handle.
            os.kill(pid, signal.SIGINT)
            os.waitpid(pid, 0)
            raise
        self.jobs[request_id] = (pid, pidfd)
        return {"request_id": request_id, "status": "pending", "code": "native_request_pending"}

    def dispatch(self, request):
        self.reap()
        required = {"version", "binding", "action"}
        action = request.get("action")
        if action not in {"ready", "connect", "submit", "status", "cancel"}:
            raise ValueError
        if action not in {"ready", "connect"}:
            required |= {"instance", "request_id"}
        if action == "submit":
            required |= {"tool", "arguments"}
        if (
            set(request) != required
            or type(request["version"]) is not int
            or request["version"] != handoff.VERSION
            or request["binding"] != self.binding
        ):
            raise ValueError
        if action not in {"ready", "connect"}:
            handoff.identifier(request["request_id"])
            if request["instance"] != self.instance:
                raise handoff.UnavailableError("native_host_changed")
        transport = self.transport()
        if action == "ready":
            return self.ready(transport)
        if action == "connect":
            self.configuration(transport)
            return {"state": "connected"}
        if action == "submit":
            return self.submit(request, transport)
        request_id = request["request_id"]
        value = self.status(request_id, transport)
        if action == "cancel" and request_id in self.jobs:
            # status already verified original server ownership (or a current
            # native /config recheck below if metadata creation is still pending).
            if value.get("status") == "pending":
                self.configuration(transport)
            private._linux_fd(424, self.jobs[request_id][1], signal.SIGINT, 0, 0)
            value["code"] = "native_cancel_requested"
        return value

    def bind(self):
        self.path = handoff.socket_path(self.binding)
        lock = self.path.with_name("host.lock")
        self.lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(self.lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise handoff.UnavailableError()
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.path.exists() or self.path.is_symlink():
            # Only exact owned stale sockets under this held per-binding lease.
            handoff.check_socket(self.path)
            self.path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.socket_identity = handoff.check_socket(self.path)
        self.listener.listen(8)
        self.listener.settimeout(0.5)

    def serve(self):
        try:
            self.bind()
            while True:
                self.reap()
                try:
                    channel, _ = self.listener.accept()
                except TimeoutError:
                    continue
                with channel:
                    self.channel = channel
                    channel.settimeout(2)
                    try:
                        handoff.check_peer(channel)
                        result = self.dispatch(handoff.receive(channel))
                        handoff.project_result(result, result.get("request_id"))
                    except handoff.UnavailableError as error:
                        result = {"state": "unavailable", "code": error.code}
                    except (common.CliError, OSError, ValueError, KeyError, TypeError, AttributeError):
                        result = {"state": "unavailable", "code": "native_host_unavailable"}
                    try:
                        handoff.send(channel, {"version": handoff.VERSION, "binding": self.binding, "instance": self.instance, "result": result})
                    except (OSError, ValueError):
                        pass  # Lost reply never permits a second dispatch.
                    finally:
                        self.channel = None
        finally:
            self.close()

    def close(self):
        for _, pidfd in self.jobs.values():
            try:
                private._linux_fd(424, pidfd, signal.SIGINT, 0, 0)
            except OSError:
                pass
        deadline = time.monotonic() + 3
        while self.jobs and time.monotonic() < deadline:
            self.reap()
            time.sleep(0.02)
        for pid, pidfd in self.jobs.values():
            try:
                private._linux_fd(424, pidfd, signal.SIGKILL, 0, 0)
            except OSError:
                pass
            os.waitpid(pid, 0)
            os.close(pidfd)
        self.jobs.clear()
        if self.listener is not None:
            self.listener.close()
        if self.path is not None and self.socket_identity is not None:
            try:
                if handoff.check_socket(self.path) == self.socket_identity:
                    self.path.unlink()
            except (OSError, handoff.UnavailableError):
                pass
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def parser():
    root = common.Parser(prog="adp credential", description=__doc__)
    actions = root.add_subparsers(dest="action", required=True)
    register = actions.add_parser("host-register", help="Select one immutable command profile independently of MCP")
    register.add_argument("--profile", required=True)
    for name in ("credential-id", "service", "label"):
        register.add_argument("--" + name, required=True)
    register.add_argument("--operation", required=True, choices=("assume_role", "materialize"))
    register.add_argument("--cwd", default=os.getcwd())
    register.add_argument("--use-seconds", type=int, default=900)
    register.add_argument("--json", action="store_true")
    register.add_argument("command", nargs=argparse.REMAINDER)
    register.set_defaults(purpose=None)
    serve = actions.add_parser("host-serve", help="Run the private human MCP coordinator in the foreground")
    serve.add_argument("--profile", action="append", default=[], help="Select a registered profile; omit for metadata recovery only")
    serve.add_argument("--no-browser", action="store_true")
    return root


def ensure_runtime():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise handoff.UnavailableError("native_host_unavailable")
    try:
        import cryptography

        if cryptography.__version__ == "50.0.0":
            return
    except ImportError:
        pass
    target = client.runtime_dir() / "bin/python"
    if target.is_file() and Path(sys.prefix) != target.parent.parent:
        os.execv(str(target), [str(target), "-I", str(Path(__file__).resolve()), *sys.argv[1:]])
    raise handoff.UnavailableError("native_host_unavailable")


def main():
    try:
        args = parser().parse_args()
        handoff.native_only()
        ensure_runtime()
        transport = client.NativeTransport()
        if args.action == "host-register":
            return common.emit(common.envelope("ok", "adp credential host-register", register(args, transport)), args.json)
        binding = handoff.binding_for(transport.base, transport.token)
        coordinator = Coordinator(load_profiles(args.profile, binding), binding, no_browser=args.no_browser)
        coordinator.configuration(transport)
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        print("Native credential coordinator started. New delivery requires a ready profile and browser approval.", file=sys.stderr)
        coordinator.serve()
        return 0
    except KeyboardInterrupt:
        return 0
    except (handoff.UnavailableError, common.CliError, OSError, ValueError, KeyError, TypeError):
        print("Native credential host unavailable. Check native login, host setup, selected profile and deployment.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
