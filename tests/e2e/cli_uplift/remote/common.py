#!/usr/bin/env python3
"""Shared helpers for the on-instance scripts. Stdlib only.

Every module in this directory runs on the disposable EC2 instance, where there
is no pip install step and no third-party Python. That constraint is why this is
plain stdlib and why it is duplicated here rather than imported from the
orchestrator package — the instance receives `remote/` and nothing else.

Three invariants live here because every script needs them and none should be
allowed to forget one:

- `assert_owned_instance()` refuses to run anywhere but the instance the harness
  created, in the platform account. Without it a script that leaked onto a
  developer machine would run real CLI commands against real credentials.
- `clean_env()` strips every inherited AWS/provider variable. The runner's own
  identity must never be borrowable by the CLI under test: if `adp` could fall
  back to ambient credentials, a broken connect flow would still show working
  inference and the evaluation would pass on nothing.
- `sanitize()` is the only way a command reaches the transcript. Flag names are
  kept because they are the evidence; values are dropped because a value can be
  a password, a token or an ExternalId.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REDACTED = "<redacted>"

# Keys whose values never appear in evidence, matched case-insensitively at any
# depth. Mirrors the orchestrator's own redaction list.
SENSITIVE_KEY = re.compile(
    r"password|token|secret|access.?key|external.?id|private.?key|cookie|credential|authorization",
    re.I,
)

# Anything JWT-shaped, wherever it appears in a string.
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")

# The on-instance session vault.
#
# WHY THIS EXISTS
#
# `install_auth` establishes the session every later journey reuses, but the only
# way evidence leaves the instance is `emit()`, which redacts every
# credential-shaped value by design. So the session's tokens arrived downstream as
# the literal string "<redacted>": truthy, ten characters, and not authentication.
# Every `require(config.get("access_token"))` guard passed on it, so the failure
# surfaced as a confusing routing or parity error instead of "the session never
# arrived".
#
# The fix keeps the tokens on the instance that produced them and passes a
# reference instead. Only the reference crosses `emit()`, the run document, the
# report and the SSM payload, so:
#
# - redaction is unchanged. Nothing was weakened to make the flow work; the
#   tokens simply never enter the channel that redacts them.
# - the tokens stop travelling through SSM command text at all, which is a
#   strictly better posture than before this fix, not merely an equivalent one.
# - a missing session is now LOUD. The reference either resolves to real material
#   or `load_session()` raises naming the journey that should have produced it,
#   which is the ambiguity this harness exists to remove.
#
# The file is 0600 under the 0700 run directory owned by ec2-user, on a
# disposable instance that is terminated during cleanup. It deliberately does NOT
# go to S3: the instance role holds no `s3:PutObject` (only GetObject on its own
# bundle), so an S3 handoff would require widening a deliberately narrow boundary.
SESSION_VAULT = "session.json"


def session_vault_path(work_dir):
    """Where this run's session material lives on the instance.

    Under `work_dir` because that is the durable, run-owned, 0700 directory the
    ec2 stage creates — the journey's own temp HOME is deleted when it returns,
    which is the same reason the CLI itself is copied out of it.
    """
    require(work_dir, "No work_dir was supplied; the session vault has no home")
    return Path(work_dir) / SESSION_VAULT


def save_session(session, *, work_dir):
    """Persist real session material privately and return its reference.

    The reference is a non-secret locator plus the non-secret identity fields the
    orchestrator legitimately reports on (`cli_path`, `username`, `user_id`,
    `org_id`). No token, and nothing matching `SENSITIVE_KEY`, is in it.
    """
    path = session_vault_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600 BEFORE any content lands, so the tokens are never briefly
    # readable by another local user through a default-mode file.
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as stream:
        json.dump(session, stream)
    os.chmod(str(path), 0o600)
    return {
        "session_ref": str(path),
        "cli_path": session.get("cli_path", ""),
        "expires_at": session.get("expires_at", 0),
        "username": session.get("username", ""),
        "user_id": session.get("user_id", ""),
        "org_id": session.get("org_id", ""),
        "created_username": session.get("created_username", ""),
    }


def load_session(config):
    """Resolve the session reference a journey payload carries.

    Raises rather than returning empties: a journey that cannot find the session
    must fail naming that, not proceed with a placeholder and be graded as a
    routing or inference defect.
    """
    reference = config.get("session_ref")
    require(
        reference,
        "This journey received no session_ref; install_auth must run before it",
    )
    # The reference arrives in a payload, so it is treated as input rather than
    # trusted: it must be exactly the vault inside this run's own work directory.
    # Checking the basename alone would still accept `/etc/session.json` or
    # `../../elsewhere/session.json`, so the whole resolved path is compared
    # against the one path this run is allowed to read.
    path = Path(reference)
    expected = session_vault_path(config.get("work_dir"))
    try:
        allowed = path.resolve() == expected.resolve()
    except OSError:
        allowed = False
    require(
        path.name == SESSION_VAULT and allowed,
        "The supplied session_ref does not name this run's session vault",
    )
    try:
        session = json.loads(path.read_text())
    except (OSError, ValueError):
        raise RemoteError(
            f"The session established by install_auth is not readable at {reference}; "
            "it did not survive the stage boundary"
        ) from None
    require(
        session.get("access_token"),
        "The stored session carries no access token; the login did not persist one",
    )
    return session


def session_tokens(config):
    """The token trio plus expiry, for a journey materializing its own HOME."""
    session = load_session(config)
    return {
        "access_token": session["access_token"],
        "id_token": session.get("id_token", ""),
        "refresh_token": session.get("refresh_token", ""),
        "expires_at": session.get("expires_at", 0),
    }


def clear_session(work_dir):
    """Remove the vault. Safe to call twice, and safe to call having never saved."""
    try:
        session_vault_path(work_dir).unlink()
    except (OSError, RemoteError):
        return False
    return True


class RemoteError(RuntimeError):
    """A remote step failed for a reason we are willing to publish verbatim."""


def require(condition, message):
    if not condition:
        raise RemoteError(message)


def redact(value):
    """Recursively drop credential-shaped values from evidence."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if SENSITIVE_KEY.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return JWT.sub(REDACTED, value)
    return value


def instance_identity():
    """IMDSv2 identity document.

    `ProxyHandler({})` is deliberate: with an inherited `http_proxy` the request
    could be answered by something that is not the metadata service, which would
    let the ownership assertion below be spoofed from the environment.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with opener.open(request, timeout=5) as response:
        token = response.read().decode()
    request = urllib.request.Request(
        "http://169.254.169.254/latest/dynamic/instance-identity/document",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def assert_owned_instance(config):
    """Refuse to act unless this is the instance the harness launched."""
    identity = instance_identity()
    require(
        identity.get("instanceId") == config.get("instance_id"),
        "This script is not running on the instance the harness created",
    )
    require(
        identity.get("accountId") == str(config.get("platform_account")),
        "This script is not running in the platform account",
    )
    return identity


def clean_env(config, **extra):
    """Environment for every child process: no inherited identity at all."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("AWS_", "ADP_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_", "BG_")
        )
    }
    env.update(
        AWS_DEFAULT_REGION=config["region"],
        AWS_REGION=config["region"],
        AWS_PAGER="",
        AWS_CLI_AUTO_PROMPT="off",
        # The approved private subnet has required regional FIPS endpoints.
        AWS_ENDPOINT_URL_STS=config["sts_endpoint"],
    )
    env.update({key: str(value) for key, value in extra.items()})
    return env


def sanitize(argv):
    """Transcript form of a command: keep the shape, drop every value."""
    parts, previous_was_flag = [], False
    for index, item in enumerate(argv):
        text = str(item)
        if text.startswith("-"):
            parts.append(text.split("=", 1)[0])
            previous_was_flag = "=" not in text
            continue
        if index == 0 or not previous_was_flag:
            # Program name and bare subcommands are structural, not secret.
            parts.append(Path(text).name if index == 0 else text)
            previous_was_flag = False
            continue
        parts.append(REDACTED)
        previous_was_flag = False
    return " ".join(parts)


def bounded(argv, *, env, timeout, cwd=None, stdin=None):
    """Run a command in its own process group and kill the group on timeout.

    A plain `subprocess.run(timeout=...)` kills only the direct child, so a CLI
    that spawned a proxy or a model process would leave it running on the
    instance after the harness moved on. `start_new_session` plus `killpg` is
    what makes the bound real.
    """
    process = subprocess.Popen(  # noqa: S603 - argv is built here, never shell
        [str(item) for item in argv],
        env=env,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(input=stdin, timeout=timeout)
        return process.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except OSError:
            pass
        process.communicate()
        # 124 matches timeout(1), so a caller can distinguish "took too long"
        # from "the command decided to fail".
        return 124, "", "timeout"


class Cli:
    """Runs the installed `adp` CLI and records a sanitized transcript.

    The CLI's JSON envelope is the contract, not the exit code: `adp` uses 4 for
    'pending' and 5 for a refused operation, both of which are expected results
    in several cases. Callers assert on `status`.
    """

    def __init__(self, binary, env, transcript, *, timeout=900):
        self.binary = str(binary)
        self.env = env
        self.transcript = transcript
        self.timeout = timeout

    def run(
        self,
        args,
        *,
        expected=0,
        env=None,
        timeout=None,
        json_output=True,
        stdin_text=None,
    ):
        argv = [self.binary, *[str(a) for a in args]]
        if json_output:
            argv.append("--json")
        self.transcript.append(sanitize(argv))
        code, out, err = bounded(
            argv,
            env=env or self.env,
            timeout=timeout or self.timeout,
            # Credentials arrive on stdin precisely so they never appear in a
            # process listing or in this transcript.
            stdin=stdin_text,
        )
        payload = None
        if json_output:
            for line in reversed((out or "").splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                        break
                    except ValueError:
                        continue
        if expected is not None and code != expected:
            raise RemoteError(
                f"`{sanitize(argv)}` exited {code}, expected {expected}"
                + (
                    f" (status={payload.get('status')!r})"
                    if isinstance(payload, dict)
                    else ""
                )
                # stderr is NOT included: it can quote a token or a password prompt.
                + (" [stderr suppressed]" if err else "")
            )
        return code, payload

    def json(self, args, *, expected=0, env=None, timeout=None, stdin_text=None):
        """Run and require a JSON envelope back."""
        code, payload = self.run(
            args, expected=expected, env=env, timeout=timeout, stdin_text=stdin_text
        )
        require(
            isinstance(payload, dict),
            f"`{sanitize([self.binary, *args])}` returned no JSON envelope",
        )
        return payload


def aws_cli(config, env, args, *, missing_ok=False, timeout=300):
    """One `aws` call, returning parsed JSON. Errors carry the CODE only."""
    argv = ["aws", "--region", config["region"], "--output", "json", *args]
    code, out, err = bounded(argv, env=env, timeout=timeout)
    if code:
        if missing_ok and re.search(
            r"(NoSuchEntity|ValidationError|ResourceNotFound|does not exist)", err or ""
        ):
            return None
        found = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", err or "")
        raise RemoteError(
            "aws "
            + " ".join(args[:2])
            + " failed: "
            + (found.group(1) if found else f"exit {code}")
        )
    return json.loads(out) if (out or "").strip() else {}


def api(config, path, token, *, method="GET", body=None, expect=(200,)):
    """Authenticated gateway request. Used for live-record assertions.

    The evaluation must compare what the CLI reports against what the product's
    own API records; a CLI that agrees only with itself proves nothing.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        config["gateway_url"].rstrip("/") + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RemoteError(f"{path} unreachable: {type(exc).__name__}") from None
    if expect is not None and status not in expect:
        raise RemoteError(f"{path} returned HTTP {status}, expected {expect}")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


_SECRET_CACHE = {}

# The established dev Cognito fixture (`adp/dev/gateway/test-admin-credentials`,
# tagged Purpose=e2e-testing) stores the admin identity under the unprefixed
# `username`/`password` keys, while these journeys ask for `admin_username`/
# `admin_password`. That is a naming mismatch between two things that already
# exist, not a missing fixture — but because a missing key RAISES here (by
# design, so a typo cannot degrade a check into a skip), the login checkpoint
# failed on a fixture that was otherwise correct and correctly owned.
#
# Resolved by accepting the existing key as an alias rather than by recreating
# the user or rewriting the shared secret: the fixture is Terraform-managed and
# owned by the gateway team, and rotating it would break other e2e consumers.
# Deliberately narrow — only the `admin_` role prefix is aliased, and only when
# the canonical key is absent, so a fixture that does carry `admin_username`
# still wins and a `non_admin_*` key can never silently resolve to the admin.
FIXTURE_KEY_ALIASES = {
    "admin_username": ("username",),
    "admin_password": ("password",),
}


def fixture_secret(config, env, key, *, default=_SECRET_CACHE):
    """Fetch one fixture credential from Secrets Manager, by reference.

    Credentials never travel in the SSM payload or the config: only the secret's
    NAME does, and the instance role's single GetSecretValue grant is what lets
    this read it. The value is returned to the caller and never logged.

    Cached per secret name because a journey reads several keys from one secret
    and each miss is another API call carrying the same value. Absent `default`,
    a missing key raises: an optional fixture must be opted into explicitly, so a
    typo in a key name cannot silently degrade a check into a skip.

    `FIXTURE_KEY_ALIASES` is consulted only after the canonical key misses, so an
    alias can never shadow an explicitly provisioned value.
    """
    name = config.get("credential_secret")
    require(name, "No credential_secret reference was supplied for this journey")
    if name not in _SECRET_CACHE:
        payload = aws_cli(
            config,
            env,
            [
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                name,
                "--endpoint-url",
                config["secrets_endpoint"],
            ],
        )
        try:
            _SECRET_CACHE[name] = json.loads(payload["SecretString"])
        except (KeyError, ValueError):
            raise RemoteError("The fixture secret is not a JSON document") from None
    document = _SECRET_CACHE[name]
    if key not in document:
        for alias in FIXTURE_KEY_ALIASES.get(key, ()):
            if alias in document:
                return document[alias]
        # Sentinel comparison, not a falsy check: "" is a legitimate default.
        require(
            default is not _SECRET_CACHE,
            f"The fixture secret has no {key!r} entry",
        )
        return default
    return document[key]


def install_release(config, env, home, transcript):
    """Install the served CLI release into an isolated prefix.

    Uses the release's own `install.sh` — the same one-line path a user runs —
    rather than copying files, because what E01 asserts is that the published
    installer works, not that we can place files ourselves.
    """
    prefix = Path(home) / "bin"
    source = Path(config["source_dir"])
    installer = source / "install.sh"
    require(installer.is_file(), f"No install.sh in the staged release at {source}")
    argv = [
        "sh",
        str(installer),
        "--prefix",
        str(prefix),
        "--gateway-url",
        config["gateway_url"],
        "--no-path-edit",
    ]
    transcript.append(sanitize(argv))
    code, _out, _err = bounded(argv, env=env, timeout=180)
    require(code == 0, f"install.sh exited {code}; the release did not install")
    return prefix


def process_state(pid):
    """The single-letter process state from procfs, or None where there is none.

    Read from the end of the line: field 2 is the executable name in parentheses
    and may itself contain spaces or a bracket, so splitting from the left is how
    this check misreads a process name as a state.
    """
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    _, _, tail = stat.rpartition(")")
    fields = tail.split()
    return fields[0] if fields else None


def process_alive(pid):
    """Is this pid a process that is still doing something?

    `kill(pid, 0)` alone is not enough. `adp codex` starts the proxy under
    `setsid nohup`, so it is reparented to pid 1; once it dies it stays a ZOMBIE
    until pid 1 reaps it, and a zombie still accepts signal 0. Waiting on
    `kill(pid, 0)` therefore reports a proxy we have already killed as running
    forever — observed against the real CLI, where a stop that had in fact
    succeeded was reported as a failure to stop.

    So the process state decides, with the signal probe as the fallback for a
    platform with no procfs.
    """
    state = process_state(pid)
    if state is not None:
        # Z: exited, awaiting a reap. X/x: released. Neither can do any more work.
        return state not in ("Z", "X", "x")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Running but not ours to signal — still running.
        return True
    return True


def stop_proxy(home):
    """Stop the auth proxy a launcher started, in the LEGACY store layout.

    Lives here because both E08 and E14 start that proxy and neither may leave it
    running: a lingering listener holds its port against a resumed attempt on the
    same instance.

    `~/.bedrock-gateway` is where a legacy single-deployment CLI keeps its runtime
    files. A named deployment (#5413) keeps them in its own runtime directory
    instead, so a caller with three of those calls `stop_proxy_runtime` directly.
    """
    return stop_proxy_runtime(Path(home) / ".bedrock-gateway")


def stop_proxy_runtime(runtime_dir):
    """Stop the proxy whose pidfile lives in this runtime directory.

    Split from `stop_proxy` for #5413: with three named deployments there are three
    proxies to account for, each publishing into its own
    `~/.adp/deployments/<id>/runtime`, and none of them is at the legacy path.
    Taking the directory rather than a HOME is what lets one implementation serve
    both layouts — the alternative was a second copy of the signal-and-confirm
    logic below, which is the part that is easy to get subtly wrong.

    There is no `adp serve --stop`. The core's `serve` accepts `--port` and
    `--foreground` only, and the `adp` wrapper merely READS this pidfile, so the
    recorded pid is the only handle — and it is the proxy itself, because
    `cmd_serve` writes `$$` and then execs python3.

    Returns True when nothing is listening on that pid any more, including the case
    where there was no pidfile to begin with (nothing was started).
    """
    import signal

    pidfile = Path(runtime_dir) / "proxy.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, number)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if wait_for(lambda: not process_alive(pid), timeout=10, interval=1):
            return True
    return not process_alive(pid)


def hashes_of(directory):
    """SHA-256 every file in a directory, for release-integrity assertions."""
    import hashlib

    found = {}
    for path in sorted(Path(directory).iterdir()):
        if path.is_file():
            found[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def wait_for(predicate, *, timeout, interval=5, clock=time.monotonic, sleep=time.sleep):
    """Bounded poll. Returns the first truthy result, or None on timeout.

    Every wait in this harness is bounded: an unbounded poll on an instance that
    bills by the second is a cost bug as well as a hang.
    """
    deadline = clock() + timeout
    while clock() < deadline:
        result = predicate()
        if result:
            return result
        sleep(interval)
    return None


def emit(evidence):
    """Print exactly one JSON document — the orchestrator reads the last line."""
    print(json.dumps(redact(evidence), sort_keys=True))
    return int(not evidence.get("success"))


def run_script(execute, argv=None):
    """Shared entry point: read the payload, execute, always emit evidence.

    A script that crashes must still print evidence, because an empty result is
    indistinguishable from a script that was never delivered — the exact
    ambiguity that let missing workers look like healthy skips.
    """
    argv = sys.argv[1:] if argv is None else argv
    evidence = {"success": False, "stage": "start", "checks": [], "transcript": []}
    try:
        require(argv, "No payload path was supplied")
        config = json.loads(Path(argv[0]).read_text())
        assert_owned_instance(config)
        execute(config, evidence)
    except Exception as exc:  # noqa: BLE001 - evidence must always be emitted
        evidence["error_type"] = type(exc).__name__
        if isinstance(exc, RemoteError):
            evidence["error"] = str(exc)
    return emit(evidence)
