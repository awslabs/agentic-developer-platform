#!/usr/bin/env python3
"""Native human credential delivery; encrypted responses never reach CLI output."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common
import adp_private_credentials as private

PREFIX = "/auth/native-credentials"
STATES = {"created", "awaiting_approval", "approved", "challenged", "unknown", "delivered", "denied", "expired", "acknowledged"}
IDS = ("request_id", "execution_id", "receipt_id")


def parser():
    root = common.Parser(prog="adp credential", description=__doc__)
    actions = root.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("host-setup", help="Install the pinned private-host Python runtime")
    setup.add_argument("--json", action="store_true")
    status = actions.add_parser("delivery-status", help="Read original delivery metadata; never retrieve credentials")
    status.add_argument("--request-id", required=True)
    status.add_argument("--json", action="store_true")
    use = actions.add_parser("use", help="Approve and run one command with a selected credential")
    use.add_argument("--credential-id", required=True)
    use.add_argument("--service", required=True)
    use.add_argument("--label", required=True)
    use.add_argument("--operation", required=True, choices=("assume_role", "materialize"))
    use.add_argument("--purpose")
    use.add_argument("--request-id")
    use.add_argument("--cwd", default=os.getcwd())
    use.add_argument("--use-seconds", type=int, default=900)
    use.add_argument("--no-browser", action="store_true")
    use.add_argument("--json", action="store_true")
    use.add_argument("command", nargs=argparse.REMAINDER)
    return root


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError
    return value


def https_origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    return value.rstrip("/")


class NativeTransport:
    """Dedicated authenticated HTTPS channel; no retries/redirects/body logging.

    Pins one resolved deployment and native login token for this operation. It
    has no SigV4, worker credential, browser approval or generic tool fallback.
    The backend verifies the native JWT/client/current human independently.
    """

    def __init__(self):
        self.base = common.gateway_url()
        if urllib.parse.urlsplit(self.base).scheme != "https":
            raise common.CliError("Private credential delivery requires HTTPS.", "native_transport_unavailable")
        self.token = common.access_token()
        self.opener = urllib.request.build_opener(common.NoRedirect())

    def request(self, method, path, body=None):
        if not path.startswith(PREFIX + "/") or "?" in path or "#" in path:
            raise ValueError
        request = urllib.request.Request(
            self.base + path,
            data=private.canonical(body) if body is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Cache-Control": "no-store"},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                payload = response.read(private.MAX_WIRE + 1)
                return private._decode(payload)
        except urllib.error.HTTPError as error:
            # Never parse, copy, retain or print a private endpoint's error body.
            code = {
                401: "authentication_required",
                403: "permission_denied",
                409: "native_delivery_conflict",
                410: "native_delivery_expired",
                503: "native_delivery_unavailable",
            }.get(error.code, "native_delivery_refused")
            raise common.CliError("Private delivery was refused; inspect the original request status.", code, status_code=error.code) from None
        except common.CliError:
            raise common.CliError("Private delivery transport refused a redirect.", "unexpected_redirect") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise common.CliError(
                "The response was not confirmed. Inspect the original request; issuance is never retried.",
                "unknown_mutation_outcome" if method != "GET" else "native_delivery_unavailable",
                4,
            ) from None


TERMINAL_CODES = {
    "child_exited",
    "child_start_failed",
    "host_cancelled",
    "host_failed",
    "host_disconnected",
    "delivery_rejected",
    "local_lifetime_ended",
    "delivery_acknowledgement_lost",
}
LOCAL_CODES = TERMINAL_CODES | {
    "existing_request_requires_status_read",
    "local_approval_window_ended",
    "native_delivery_not_confirmed",
    "native_delivery_unavailable",
    "authentication_required",
    "permission_denied",
    "native_delivery_conflict",
    "native_delivery_expired",
    "unexpected_redirect",
}


def terminal_metadata(value, original):
    required = {"type", "delivery_state", "code", "cleanup", *IDS}
    if (
        not isinstance(value, dict)
        or not required <= value.keys() <= required | {"exit_code"}
        or any(not isinstance(value.get(key), str) for key in ("type", "delivery_state", "code", "cleanup"))
        or value["type"] != "result"
        or value["delivery_state"] not in {"completed", "unknown", "failed", "cancelled", "expired"}
        or value["code"] not in TERMINAL_CODES
        or value["cleanup"] not in {"complete", "unknown"}
        or any(value[key] != original.get(key) for key in IDS)
        or ("exit_code" in value and (type(value["exit_code"]) is not int or not -255 <= value["exit_code"] <= 255))
    ):
        raise ValueError
    return dict(value)


def metadata(value, request_id):
    if not isinstance(value, dict) or value.get("request_id") != request_id or value.get("status") not in STATES:
        raise ValueError
    result = {key: identifier(value[key]) for key in IDS}
    if type(value.get("expires_at")) is not int:
        raise ValueError
    result.update(status=value["status"], expires_at=value["expires_at"])
    snapshot = value.get("snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, str) or (snapshot and (len(snapshot) != 64 or any(c not in "0123456789abcdef" for c in snapshot))):
            raise ValueError
        result["snapshot"] = snapshot
    if ("acknowledgment" in value) != ("acknowledgment_source" in value):
        raise ValueError
    if "acknowledgment" in value:
        if value["acknowledgment_source"] != "client_reported":
            raise ValueError
        result["acknowledgment_source"] = "client_reported"
        acknowledgment = terminal_metadata(value["acknowledgment"], result)
        result["acknowledgment"] = acknowledgment
        result.update({key: acknowledgment[key] for key in ("delivery_state", "code", "cleanup", "exit_code") if key in acknowledgment})
        result["acknowledgement"] = "confirmed" if result["status"] == "acknowledged" else "unknown"
        if result["status"] == "unknown":
            result["delivery_state"] = "unknown"
    return result


@contextlib.contextmanager
def reference_lock(request_id):
    # Native delivery is Linux-only. This persistent empty lock file is never
    # replaced/unlinked, so all writers lock the same inode; kernel release on
    # process death avoids a stale directory lock preventing metadata recovery.
    path = common.state_path("native-credential-" + identifier(request_id)).with_suffix(".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise common.CliError(
                        "Original delivery metadata is busy; inspect the same request again.", "native_delivery_unavailable"
                    ) from None
                time.sleep(0.02)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError
        yield
    finally:
        os.close(fd)


def save_reference(request_id, value):
    # Preserve finite local observations across metadata-only reads. All stored
    # bytes remain nonsecret; refuse corrupt records rather than echoing them.
    def project(item):
        if not isinstance(item, dict) or item.get("request_id", request_id) != request_id:
            raise ValueError
        result = {key: identifier(item[key]) for key in IDS if key in item}
        validators = {
            "status": lambda v: isinstance(v, str) and v in STATES,
            "expires_at": lambda v: type(v) is int,
            "snapshot": lambda v: isinstance(v, str) and (not v or len(v) == 64 and all(c in "0123456789abcdef" for c in v)),
            "delivery_state": lambda v: isinstance(v, str) and v in {"completed", "unknown", "failed", "cancelled", "expired", "unavailable"},
            "cleanup": lambda v: isinstance(v, str) and v in {"complete", "unknown"},
            "code": lambda v: isinstance(v, str) and v in LOCAL_CODES,
            "exit_code": lambda v: type(v) is int and -255 <= v <= 255,
            "acknowledgement": lambda v: isinstance(v, str) and v in {"confirmed", "unknown"},
        }
        for key, valid in validators.items():
            if key in item:
                if not valid(item[key]):
                    raise ValueError
                result[key] = item[key]
        if ("acknowledgment" in item) != ("acknowledgment_source" in item):
            raise ValueError
        if "acknowledgment" in item:
            if item["acknowledgment_source"] != "client_reported":
                raise ValueError
            result["acknowledgment_source"] = "client_reported"
            result["acknowledgment"] = terminal_metadata(item["acknowledgment"], result)
        return result

    name = "native-credential-" + request_id
    incoming = project(value)
    with reference_lock(request_id):
        previous = project(common.read_state(name))
        if any(key in previous and key in incoming and previous[key] != incoming[key] for key in IDS):
            raise ValueError
        result = {"request_id": request_id, **previous, **incoming}
        if result.get("status") == "unknown":
            result["delivery_state"] = "unknown"
        common.write_state(name, result)
        return result


def reserve_reference(request_id):
    path = common.state_path("native-credential-" + request_id)
    with reference_lock(request_id):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as output:
            json.dump({"request_id": request_id, "status": "created"}, output)
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True


def volatile_root():
    # One owned tmpfs root; consumer creates per-operation directories and uses
    # descriptor-relative no-symlink checks again before exposing an offer.
    path = Path("/dev/shm") / ("adp-native-credentials-" + str(os.getuid()))
    path.mkdir(mode=0o700, exist_ok=True)
    fd = private._directory(str(path), volatile=True)
    os.close(fd)
    return str(path)


def runtime_dir():
    return common.private_directory(common.state_dir() / "native-host") / "runtime"


def setup_runtime():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise common.CliError("Private delivery currently supports Linux x86_64 hosts.", "native_host_unavailable")
    target = runtime_dir()
    if target.exists():
        check = subprocess.run(
            [str(target / "bin/python"), "-I", "-c", "import cryptography; assert cryptography.__version__ == '50.0.0'"],
            capture_output=True,
            timeout=10,
        )
        if check.returncode == 0:
            return {"runtime": "ready", "cryptography": "50.0.0"}
        raise common.CliError(
            "The private host runtime needs repair; remove its incomplete runtime directory before setup.", "native_host_unavailable"
        )
    staging = Path(tempfile.mkdtemp(prefix=".runtime-", dir=target.parent))
    try:
        subprocess.run([sys.executable, "-I", "-m", "venv", str(staging)], check=True, capture_output=True, timeout=120)
        subprocess.run(
            [
                str(staging / "bin/python"),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--only-binary=:all:",
                "--require-hashes",
                "-r",
                str(Path(__file__).with_name("native-host-requirements.txt")),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        subprocess.run(
            [str(staging / "bin/python"), "-I", "-c", "import cryptography; assert cryptography.__version__ == '50.0.0'"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        os.rename(staging, target)
    except (OSError, subprocess.SubprocessError):
        raise common.CliError("Private host runtime installation failed; no credential request was made.", "native_host_unavailable") from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"runtime": "ready", "cryptography": "50.0.0"}


def ensure_runtime(argv):
    try:
        import cryptography

        if cryptography.__version__ == "50.0.0":
            return
    except ImportError:
        pass
    target = runtime_dir() / "bin/python"
    if target.is_file() and Path(sys.prefix) != target.parent.parent:
        os.execv(str(target), [str(target), "-I", str(Path(__file__).resolve()), *argv])
    raise common.CliError("Run adp credential host-setup to install the pinned private host runtime.", "native_host_unavailable")


def request_body(args, request_id):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.use_seconds <= 900 or (args.operation == "assume_role" and args.use_seconds != 900):
        raise common.CliError("Select a command and valid lifetime; AWS sessions require exactly 900 seconds.", "usage_error", 1)
    if args.operation != "assume_role" and args.purpose is not None:
        raise common.CliError("Purpose applies only to AWS role assumption.", "usage_error", 1)
    executable = shutil.which(command[0])
    if not executable:
        raise common.CliError("The selected executable is unavailable.", "native_host_unavailable")
    result = {
        "request_id": request_id,
        "credential_id": args.credential_id,
        "service": args.service,
        "label": args.label,
        "operation": args.operation,
        "argv": [str(Path(executable).resolve()), *command[1:]],
        "cwd": str(Path(args.cwd).resolve(strict=True)),
        "use_seconds": args.use_seconds,
    }
    if args.purpose is not None:
        result["purpose"] = args.purpose
    return result


def validate_selection(selection, body, config):
    private._validate_selection(selection)
    if any(selection[key] != body[key] for key in ("request_id", "credential_id", "service", "label", "operation", "argv", "cwd", "use_seconds")):
        raise ValueError
    if any(selection[key] != config[key] for key in ("issuer", "audience", "key_id")):
        raise ValueError
    arguments = {"service": body["service"], "label": body["label"]}
    if body["operation"] == "assume_role":
        arguments["purpose"] = body.get("purpose", "")
    if selection["argument_digest"] != hashlib.sha256(private.canonical(arguments)).hexdigest():
        raise ValueError


def use_credential(args, client, *, notify=common.open_browser, host_factory=private.PrivateHost.prepare, sleep=time.sleep):
    request_id = identifier(args.request_id) if args.request_id else str(uuid.uuid4())
    body = request_body(args, request_id)
    if not reserve_reference(request_id):
        original = metadata(client.request("GET", PREFIX + "/requests/by-request/" + request_id), request_id)
        save_reference(request_id, original)
        return {**original, "code": "existing_request_requires_status_read"}
    original = {"request_id": request_id}
    phase = "configuration"
    host = None

    def finish(outcome):
        if host is not None:
            try:
                terminal = terminal_metadata(host.cancel(), original)
                # Cleanup/exit are observations. They never turn an unknown
                # provider or delivery acknowledgment into successful issuance.
                for key in ("cleanup", "exit_code"):
                    if key in terminal:
                        outcome[key] = terminal[key]
            except (private.HostUnavailableError, OSError, ValueError, KeyError, TypeError):
                outcome["cleanup"] = "unknown"
        save_reference(request_id, outcome)
        return outcome

    try:
        config = client.request("GET", PREFIX + "/config")
        if config.get("version") != private.VERSION or not isinstance(config.get("issuer_key_pem"), str):
            raise ValueError
        origin = https_origin(config["approval_origin"])
        phase = "metadata_intent"
        prepared = client.request("POST", PREFIX + "/requests", body)
        original = metadata(prepared, request_id)
        save_reference(request_id, original)
        if prepared["status"] != "created":
            return finish({**original, "code": "existing_request_requires_status_read"})
        selection = prepared["selection"]
        validate_selection(selection, body, config)
        if any(selection[key] != original[key] for key in IDS):
            raise ValueError
        phase = "host_preparation"
        host_deadline = time.monotonic() + private.PREPARE_SECONDS - 5
        host = host_factory(selection, issuer_key_pem=config["issuer_key_pem"].encode(), volatile_root=volatile_root())
        receipt = original["receipt_id"]
        path = PREFIX + "/requests/" + receipt
        phase = "selection_approval"
        offer = client.request("POST", path + "/offer", {"offer": host.offer})
        if offer.get("receipt_id") != receipt or offer.get("status") != "awaiting_approval" or type(offer.get("expires_at")) is not int:
            raise ValueError
        if time.monotonic() >= host_deadline:
            return finish({**original, "status": "expired", "code": "local_approval_window_ended"})
        print("Request: " + receipt + "\nRecipient: " + host.offer["recipient_fingerprint"], file=sys.stderr)
        approval_url = origin + "/credentials/native/" + receipt
        if args.no_browser:
            print(approval_url, file=sys.stderr)
        else:
            notify(approval_url)
        deadline = min(host_deadline, time.monotonic() + max(0, offer["expires_at"] - time.time()))
        while time.monotonic() < deadline:
            current = metadata(client.request("GET", path), request_id)
            if any(current[key] != original[key] for key in IDS):
                raise ValueError
            save_reference(request_id, current)
            if current["status"] == "approved":
                break
            if current["status"] != "awaiting_approval":
                return finish(current)
            sleep(1)
        else:
            return finish({**original, "status": "expired", "code": "local_approval_window_ended"})
        challenge = client.request("POST", path + "/challenge", {})
        if (
            challenge.get("receipt_id") != receipt
            or type(challenge.get("expires_at")) is not int
            or not time.time() < challenge["expires_at"] <= time.time() + 31
        ):
            raise ValueError
        proof = host.prove(challenge["challenge"])
        # Single private issue call. Unknown acknowledgment is terminal locally;
        # status recovery cannot retry this call or retrieve its envelope.
        phase = "private_issuance"
        encrypted = client.request("POST", path + "/issue", proof)
        first = host.start(encrypted)
        del encrypted, proof
        outcome = host.wait() if first.get("type") == "started" else first
        if outcome.get("type") != "result" or any(outcome.get(key) != original[key] for key in IDS):
            raise ValueError
        save_reference(request_id, outcome)
        try:
            acknowledged = client.request("POST", path + "/ack", outcome)
            if acknowledged.get("receipt_id") != receipt or acknowledged.get("status") not in {"acknowledged", "unknown"}:
                raise ValueError
            outcome = {
                **outcome,
                "status": acknowledged["status"],
                "acknowledgement": "confirmed" if acknowledged["status"] == "acknowledged" else "unknown",
            }
            if acknowledged["status"] == "unknown":
                outcome["delivery_state"] = "unknown"
        except (common.CliError, ValueError):
            outcome = {**outcome, "acknowledgement": "unknown"}
        return finish(outcome)
    except (common.CliError, private.HostUnavailableError, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt) as error:
        state = "unknown" if phase in {"private_issuance", "metadata_intent"} else "unavailable"
        safe_codes = {
            "native_delivery_unavailable",
            "authentication_required",
            "permission_denied",
            "native_delivery_conflict",
            "native_delivery_expired",
            "unexpected_redirect",
        }
        code = error.code if isinstance(error, common.CliError) and error.code in safe_codes else "native_delivery_not_confirmed"
        outcome = {**original, "delivery_state": state, "code": code}
        if state == "unknown":
            outcome["status"] = "unknown"
        return finish(outcome)
    finally:
        if host is not None:
            try:
                host.cancel()
            except (private.HostUnavailableError, OSError, ValueError, KeyError, TypeError):
                pass  # finish() already retained unknown cleanup on its result.


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        try:
            args = parser().parse_args(argv)
        except common.CliError:
            raise common.CliError("Invalid native credential arguments; use --help.", "usage_error", 1) from None
        if args.action == "host-setup":
            result = setup_runtime()
        else:
            if args.action == "use":
                ensure_runtime(argv)
            client = NativeTransport()
            if args.action == "delivery-status":
                request_id = identifier(args.request_id)
                result = metadata(client.request("GET", PREFIX + "/requests/by-request/" + request_id), request_id)
                result = save_reference(request_id, result)
            else:
                result = use_credential(args, client)
        okay = result.get("runtime") == "ready" or (
            result.get("delivery_state") == "completed" and result.get("exit_code") == 0 and result.get("acknowledgement") == "confirmed"
        )
        status = "ok" if okay or args.action == "delivery-status" else "pending"
        if result.get("delivery_state") == "unavailable":
            status = "unavailable"
        elif result.get("delivery_state") == "failed" or (result.get("delivery_state") == "completed" and result.get("exit_code") != 0):
            status = "failed"
        return common.emit(common.envelope(status, "adp credential " + args.action, result), as_json)
    except (common.CliError, OSError, ValueError, TypeError, KeyError) as error:
        hints = {
            "native_host_unavailable": "Run adp credential host-setup on a supported Linux x86_64 host.",
            "authentication_required": "Sign in with adp login, then inspect the original request status.",
            "native_transport_unavailable": "Select an ADP deployment with an HTTPS gateway.",
            "usage_error": "Use adp credential use --help to select a credential and command.",
        }
        code = error.code if isinstance(error, common.CliError) and error.code in hints else "native_credential_unavailable"
        return common.emit(
            common.envelope(
                "unavailable",
                "adp credential",
                {
                    "code": code,
                    "message": hints.get(code, "Check native login, private host setup and this deployment's native delivery readiness."),
                },
            ),
            as_json,
        )


if __name__ == "__main__":
    sys.exit(main())
