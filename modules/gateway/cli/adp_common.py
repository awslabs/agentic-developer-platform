#!/usr/bin/env python3
"""Shared ADP CLI transport, private state and output contract (stdlib only)."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ERROR_CODE_ALIASES = {
    "budget_exceeded": "budget_exhausted",
    "budget_exhausted": "budget_exhausted",
    "revision_conflict": "stale_revision",
    "stale_revision": "stale_revision",
}
ERROR_EXIT_CODES = {
    "budget_exhausted": 4,
    "dependency_pending": 4,
    "request_timeout": 4,
    "stale_revision": 4,
    "unknown_mutation_outcome": 4,
}


def normalize_error_code(code):
    if not isinstance(code, str):
        return "http_error"
    normalized = ERROR_CODE_ALIASES.get(code, code)
    if normalized.endswith("_revision_conflict"):
        return "stale_revision"
    return normalized


class CliError(Exception):
    def __init__(self, message, code="operation_failed", exit_code=5, *, status_code=None):
        super().__init__(message)
        self.code = normalize_error_code(code)
        self.exit_code = ERROR_EXIT_CODES.get(self.code, exit_code)
        self.status_code = status_code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(message, "usage_error", 1)


CAPABILITIES_PATH = "/me/cli-capabilities"
CAPABILITY_SCHEMA_VERSIONS = ("2026-09-21",)
CAPABILITY_CACHE_TTL_SECONDS = 300
CAPABILITY_AXES = ("supported", "enabled", "permitted", "ready")
CAPABILITY_STATES = ("yes", "no", "unknown")
CAPABILITY_FAILURES = {
    "supported": ("no", "unsupported_operation", 5),
    "enabled": ("no", "feature_disabled", 5),
    "permitted": ("no", "permission_denied", 3),
    "ready": ("no", "dependency_pending", 4),
}
CAPABILITY_MESSAGES = {
    "unsupported_operation": "This ADP deployment does not offer that operation.",
    "feature_disabled": "That feature is switched off on this ADP deployment.",
    "permission_denied": "You are not permitted to do that on this ADP deployment.",
    "dependency_pending": "A service this needs is not ready yet.",
}


_UNRESOLVED = object()
_deployment = _UNRESOLVED

# The selection variables the front door exports. Their presence is what tells us
# a selection was actually requested, as opposed to a machine that has simply
# never registered a deployment — the two need opposite treatment when resolution
# fails, so this is checked exactly rather than guessed at.
_SELECTION_VARIABLES = ("ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT")


def deployment():
    """The one deployment this process runs against — resolved ONCE, then cached.

    Caching is the safety property, not an optimization (Issue #5413). A command
    reads the gateway URL and then fetches a token; if each read re-consulted the
    saved default, a concurrent `adp deployment use` in another terminal could
    change the answer in between and send one deployment's token to another
    deployment's gateway. One resolution per process makes that impossible.

    Returns None on a machine with no named deployment and no selection, which is
    what keeps the pre-#5413 paths working untouched for existing users.
    """
    global _deployment
    if _deployment is _UNRESOLVED:
        _deployment = _resolve_deployment()
    return _deployment


def _resolve_deployment():
    selected = any(os.environ.get(variable) for variable in _SELECTION_VARIABLES)
    registered = (Path(os.environ.get("ADP_HOME") or Path.home() / ".adp") / "deployments.json").exists()
    module = load_provider("adp_deployments.py")
    if module is None:
        # A partial install (the sibling file is missing). Degrading to the legacy
        # single-deployment paths keeps `adp login`/`adp update` usable, which is
        # how a user repairs that install.
        if selected or registered:
            raise CliError("Deployment resolver is missing. Reinstall the CLI.", "deployment_state_unreadable")
        return None
    try:
        resolved = module.resolve()
        resolved.validate_config()
        if not resolved.legacy:
            module.lease(resolved, os.getpid())
        return resolved
    except module.DeploymentError as exc:
        if selected or registered or exc.code != "deployment_not_found":
            # A selection WAS requested and could not be honoured. Never fall back:
            # a fallback here is precisely how a credential reaches a deployment
            # the user did not name.
            raise CliError(str(exc), exc.code, exc.exit_code) from None
        return None


def config_path():
    resolved = deployment()
    return (resolved.config_dir if resolved else Path.home() / ".bedrock-gateway") / "config.json"


def state_dir():
    resolved = deployment()
    return resolved.state_dir if resolved else Path.home() / ".adp/state"


def gateway_url():
    """The base URL of the selected deployment's API.

    A registered deployment's URL comes from the registry, because that binding is
    what the user selected and what must stay pinned for the whole command. The
    config file is the fallback, and remains the only source on a legacy machine
    that has no registry at all.
    """
    resolved = deployment()
    try:
        base = (resolved.gateway_url if resolved else "") or json.loads(config_path().read_text())["gateway_url"]
        base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and local)):
            raise ValueError
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        return base if parsed.path.endswith("/api") else base + "/api"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise CliError("No valid gateway configured. Reinstall using the command on your ADP sign-in page.", "gateway_not_configured", 2) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CliError("ADP redirected the request. Check the gateway URL before sending credentials.", "unexpected_redirect")


def access_token():
    helper = Path(__file__).resolve().with_name("bg-cognito-auth.sh")
    # The helper is handed THIS process's already-resolved deployment rather than
    # being left to resolve again (Issue #5413). Re-resolving in the child is the
    # drift window: it would read the saved default a second time, and this call
    # sits between reading the gateway URL and sending the request — the one place
    # a changed answer means a token posted to the wrong gateway.
    resolved = deployment()
    environment = {**os.environ, **(resolved.environment() if resolved else {})}
    try:
        token = subprocess.run(
            ["bash", str(helper), "token"], capture_output=True, text=True, timeout=120, check=True, env=environment
        ).stdout.strip()
        if not token or any(char.isspace() for char in token):
            raise ValueError
        return token
    except (OSError, subprocess.SubprocessError, ValueError):
        raise CliError("Sign in with adp login or adp admin login.", "authentication_required", 2) from None


class Api:
    def __init__(self):
        self.base = gateway_url()
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, *, authenticated=True, token=None, timeout=120):
        if not path.startswith("/") or path.startswith("//"):
            raise CliError("Invalid ADP API path.")
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + (token or access_token())
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                payload = response.read()
                # 204/205 are DEFINED to carry no body, and ADP uses 204 for
                # successful deletes (src/auth/vault_routes.py). Parsing that as
                # JSON raised, and the ValueError arm below relabelled a
                # SUCCESSFUL delete as "gateway_unavailable" (Issue #5039).
                # Keyed on those two statuses and an actually-empty body only:
                # an empty or malformed body on any other status is still a
                # failure, so a truncated response cannot pass as success.
                if not payload.strip() and response.status in (204, 205):
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            code = "http_error"
            try:
                payload = json.load(exc)
                detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
                reason = payload.get("error", payload.get("reason", "")) if isinstance(payload, dict) else ""
                if not reason and isinstance(detail, dict):
                    reason = detail.get("error", detail.get("reason", ""))
                if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason):
                    code = normalize_error_code(reason)
            except (ValueError, AttributeError):
                pass
            hints = {
                401: "Sign in again with adp login or adp admin login.",
                403: "This operation requires an authorized ADP administrator.",
                404: "Check the target; the gateway may need an upgrade.",
                409: "Configuration changed. Read its current status before retrying.",
                429: "Wait a minute before retrying.",
            }
            code_hints = {
                "budget_exhausted": "The spending limit is blocking this operation. Check `adp doctor --checks budget`.",
                "stale_revision": "Configuration changed. Read its current state before trying again.",
            }
            hint = code_hints.get(
                code,
                hints.get(exc.code, "Check status before retrying; an interrupted request may have changed configuration."),
            )
            exit_code = ERROR_EXIT_CODES.get(code, {401: 2, 403: 3}.get(exc.code, 5))
            raise CliError(f"ADP returned HTTP {exc.code} ({code}). {hint}", code, exit_code, status_code=exc.code) from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                raise CliError(
                    "ADP did not confirm whether the change completed. Read the current state and reconcile the "
                    "same operation; do not retry blindly.",
                    "unknown_mutation_outcome",
                    4,
                ) from None
            if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                raise CliError(
                    "ADP did not answer in time. Read the current state before retrying.",
                    "request_timeout",
                    4,
                ) from None
            raise CliError(
                "ADP could not be reached or returned an invalid response. Check status before retrying.",
                "gateway_unavailable",
            ) from None


def api(method, path, body=None, **kwargs):
    return Api().request(method, path, body, **kwargs)


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CliError("Use a private directory owned by you with permissions 0700.", "unsafe_file")
    return path


def write_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".adp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CliError("Credential and state files must be owned by you with permissions 0600.", "unsafe_file")
        return json.load(source)


def state_path(name):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise CliError("Invalid state name.")
    return private_directory(state_dir()) / (name + ".json")


def deployment_stamp():
    """Identity to record in a handoff file, so a later resume can prove it is ours.

    Issue #5413. A handoff directory outlives the command that wrote it, gets
    emailed to an administrator, and comes back minutes or days later. By then the
    saved default may name a different deployment, so the returning `--resume` must
    be able to tell whose setup it is holding. The gateway URL alone is not that
    answer: it is the deployment's current binding, not its identity, and two
    records can be re-pointed or renamed while a handoff is in flight.

    The stable id is recorded because it is the filesystem authority for a
    deployment; the name is recorded only so the error message can say something a
    person recognises. Neither is a secret, which is why they may be written into a
    directory the user is about to hand to somebody else.
    """
    resolved = deployment()
    return {"deployment_id": resolved.id if resolved else "", "deployment": resolved.name if resolved else ""}


def check_handoff_deployment(metadata, what="setup"):
    """Refuse another deployment's handoff BEFORE anything is created or assigned.

    Issue #5413. Ordering is the whole point: this runs while the only thing that
    has happened is reading a file, so a resume aimed at the wrong deployment
    changes nothing anywhere — no role provisioned, no routing rule assigned, no
    state overwritten.

    A handoff written before this change carries no stamp. That is accepted rather
    than rejected: refusing it would strand a setup a user is part-way through, and
    the pre-existing gateway-URL check still applies to it.
    """
    recorded = (metadata or {}).get("deployment_id")
    if not recorded:
        return
    resolved = deployment()
    current_id = resolved.id if resolved else ""
    if recorded != current_id:
        raise CliError(
            f"This {what} belongs to deployment {metadata.get('deployment') or recorded!r}, "
            f"but this command is running against {(resolved.name if resolved else 'the legacy deployment')!r}. "
            f"Nothing was changed. Rerun it with --deployment {metadata.get('deployment') or '<name>'}.",
            "deployment_mismatch",
            1,
        )


def read_state(name):
    path = state_path(name)
    return read_private_json(path) if path.exists() else {}


def write_state(name, value):
    write_json(state_path(name), value)


def _jwt_claims(token):
    """Decode JWT claims only to partition local cache state, never to authorize."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


def authenticated_scope(token=None):
    """Return non-credential cache keys for the current authenticated scope."""
    token = token or access_token()
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    claims = _jwt_claims(token)
    subject = next((claims.get(key) for key in ("sub", "user_id", "username") if isinstance(claims.get(key), str)), "")
    tenant = next(
        (claims.get(key) for key in ("org_id", "custom:org_id", "tenant_id", "custom:tenant_id") if isinstance(claims.get(key), str)),
        "",
    )
    identity_material = f"{claims.get('iss', '')}\0{subject}" if subject else fingerprint
    return {
        "identity": hashlib.sha256(identity_material.encode()).hexdigest(),
        "tenant": tenant or f"session:{fingerprint}",
        "tenant_claim": tenant,
    }


def capability_cache_context(token=None):
    """Resolve the cache key and its scope from one immutable token snapshot."""
    try:
        scope = authenticated_scope(token)
    except CliError:
        return None, None
    key = {**deployment_stamp(), "gateway": gateway_url(), "identity": scope["identity"], "tenant": scope["tenant"]}
    return key, scope


def capability_cache_key():
    """Deployment, gateway, authenticated identity and tenant; never raw credentials."""
    return capability_cache_context()[0]


def validate_capability_document(document, *, scope=None):
    if not isinstance(document, dict):
        raise CliError("ADP returned a capability document this CLI could not read.", "invalid_response")
    if document.get("schema_version") not in CAPABILITY_SCHEMA_VERSIONS:
        raise CliError("ADP returned a capability schema this CLI does not support.", "schema_unsupported")
    gateway = document.get("gateway")
    tenant = document.get("tenant")
    operations = document.get("operations")
    if (
        not isinstance(gateway, dict)
        or gateway.get("state") not in CAPABILITY_STATES
        or not isinstance(gateway.get("release"), str)
        or not isinstance(tenant, dict)
        or not isinstance(tenant.get("org_id"), str)
        or not isinstance(operations, list)
    ):
        raise CliError("ADP returned a capability document this CLI could not read.", "invalid_response")
    operation_ids = set()
    for operation in operations:
        operation_id = operation.get("id") if isinstance(operation, dict) else None
        if not isinstance(operation_id, str) or not operation_id or operation_id in operation_ids:
            raise CliError("ADP returned a capability document this CLI could not read.", "invalid_response")
        operation_ids.add(operation_id)
        if any(operation.get(axis) not in CAPABILITY_STATES for axis in CAPABILITY_AXES):
            raise CliError("ADP reported a capability state this CLI could not read.", "invalid_response")
    if scope and scope.get("tenant_claim") and tenant["org_id"] != scope["tenant_claim"]:
        raise CliError("ADP returned capabilities for a different tenant.", "capability_scope_mismatch")
    return document


def read_capabilities(*, refresh=False, request=None, token=None):
    """Read the authenticated capability document with a bounded scoped cache."""
    requester = request or api
    try:
        token = token or access_token()
    except CliError:
        token = None
        key, scope = capability_cache_context()
    else:
        key, scope = capability_cache_context(token)
    if key and not refresh:
        try:
            cached = read_state("capabilities")
            age = time.time() - cached.get("fetched_at", 0)
            if cached.get("key") == key and 0 <= age <= CAPABILITY_CACHE_TTL_SECONDS:
                return validate_capability_document(cached.get("document"), scope=scope), "cache"
        except (CliError, AttributeError, TypeError):
            pass
    request_options = {"timeout": 30}
    if token:
        request_options["token"] = token
    document = validate_capability_document(requester("GET", CAPABILITIES_PATH, **request_options), scope=scope)
    if key:
        try:
            write_state("capabilities", {"key": key, "fetched_at": int(time.time()), "document": document})
        except (CliError, OSError):
            pass
    return document, "gateway"


def capability_operation(document, operation_id):
    return next((row for row in document.get("operations", []) if row.get("id") == operation_id), None)


def capability_blocking_reason(operation):
    if operation is None:
        return "unsupported_operation"
    for axis in CAPABILITY_AXES:
        expected, code, _exit = CAPABILITY_FAILURES[axis]
        if operation.get(axis) == expected:
            return code
    return None


_capability_preflight = {}


def record_capability_preflight(operation_id, evidence):
    if not evidence.get("checked") or evidence.get("unknown"):
        _capability_preflight[operation_id] = dict(evidence)
        print(f"Capability discovery for {operation_id} is unconfirmed; the server will authorize this request.", file=sys.stderr)
    return evidence


def ensure_can_mutate(operation_id, *, refresh=False, request=None, token=None):
    """Refuse definitive capability failures before a mutation is sent."""
    try:
        document, source = read_capabilities(refresh=refresh, request=request, token=token)
    except Exception:  # noqa: BLE001 - discovery is advisory; the mutation route still authorizes
        return record_capability_preflight(operation_id, {"checked": False, "reason": "", "source": "unavailable"})
    operation = capability_operation(document, operation_id)
    reason = capability_blocking_reason(operation)
    if reason:
        exit_code = next(exit_code for _axis, (_state, code, exit_code) in CAPABILITY_FAILURES.items() if code == reason)
        raise CliError(f"{CAPABILITY_MESSAGES[reason]} Nothing was sent.", reason, exit_code)
    unknown = [axis for axis in CAPABILITY_AXES if operation.get(axis) == "unknown"]
    return record_capability_preflight(operation_id, {"checked": True, "reason": "", "source": source, "unknown": unknown})
@contextlib.contextmanager
def file_lock(path, busy_message, timeout=30):
    """Hold an exclusive lock across processes for the duration of the block.

    `mkdir` is the primitive because it is atomic on every filesystem the CLI runs
    on, including NFS, where `O_CREAT|O_EXCL` on a regular file historically is
    not. Exactly one of two concurrent `mkdir` calls succeeds; the loser waits.

    Why a lock rather than a careful write: the dangerous operations here are
    read-modify-write over a shared state file, and `write_json`'s atomic replace
    makes each individual write atomic without making the *sequence* atomic. Two
    processes can both read, both decide, and the second's write then silently
    discards the first's — which for an operation identity means two identities
    for one intent, and a duplicate of whatever the identity was protecting.

    A stale lock from a killed process is left in place deliberately rather than
    being cleared on a timeout. Breaking it would reintroduce exactly the
    concurrency it prevents, and it is recoverable by hand; a silently duplicated
    paid operation is not. The message names the directory so removing it is
    possible without guessing.
    """
    lock = Path(path)
    private_directory(lock.parent)
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CliError(
                    f"{busy_message} If no other command is running, remove {lock} and retry.",
                    "lock_busy",
                    # 4, not 5: whether the other holder's work succeeded is
                    # unknown from here, and reporting it as a failure invites a
                    # retry of something that may already have happened.
                    4,
                ) from None
            time.sleep(0.1)
    try:
        yield lock
    finally:
        # Best effort: a lock already gone means somebody broke it by hand, which
        # is not a reason to mask the outcome of the work that just completed.
        with contextlib.suppress(OSError):
            lock.rmdir()


def state_lock(name, busy_message, timeout=30):
    """A `file_lock` beside the named state file, so it guards that file alone."""
    return file_lock(state_path(name).with_suffix(".lock"), busy_message, timeout)


def save_session(result):
    directory = private_directory(config_path().parent)
    lock = directory / "refresh.lock"
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CliError("A token refresh is still running. Retry adp admin login.") from None
            time.sleep(0.1)
    try:
        config = json.loads(config_path().read_text()) if config_path().exists() else {"gateway_url": gateway_url()}
        config.update({key: result[key] for key in ("client_id", "user_pool_id", "region")})
        config.update(refresh_via="gateway", identity_pool_id="")
        tokens = {key: result[key] for key in ("access_token", "id_token", "refresh_token")}
        tokens["expires_at"] = int(time.time()) + int(result["expires_in"])
        write_json(config_path(), config)
        write_json(directory / "tokens.json", tokens)
    finally:
        lock.rmdir()


def organization_context(explicit, client):
    return explicit or os.environ.get("ADP_ORG") or client.request("GET", "/auth/cli/admin-session").get("org_id") or None


def envelope(status, command, detail=None, next_action=None):
    result = {"status": status, "command": command, "detail": detail or {}, "next_action": next_action}
    if _capability_preflight:
        result["capability_preflight"] = dict(_capability_preflight)
    return result


def emit(result, as_json=False):
    if as_json:
        print(json.dumps(result))
    else:
        print(f"{result['command']}: {result['status']}")
        if result.get("detail"):
            print(json.dumps(result["detail"], indent=2))
        if result.get("next_action"):
            print(result["next_action"])
        if result.get("error"):
            print(result["error"]["message"], file=sys.stderr)
    return 5 if result["status"] == "failed" else 4 if result["status"] in {"pending", "unavailable"} else 0


def report_error(exc, command, as_json):
    if not isinstance(exc, CliError):
        exc = CliError("Invalid response or local file error. Check the current setup before retrying.")
    result = envelope("failed", command)
    result["error"] = {"code": exc.code, "message": str(exc)}
    emit(result, as_json)
    return exc.exit_code


def load_provider(filename):
    path = Path(__file__).resolve().with_name(filename)
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return None
    return module


def open_browser(url):
    print(url, file=sys.stderr)
    with contextlib.suppress(webbrowser.Error):
        return webbrowser.open(url)
    return False
