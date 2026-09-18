#!/usr/bin/env python3
"""Named ADP deployments and the one selection rule (Issue #5413).

WHY THIS FILE EXISTS
--------------------
The CLI is half bash (`adp`, `bg-cognito-auth.sh`) and half python (`adp_common.py`
and the per-area helpers). Before this module, each half found the gateway URL and
the token store by hardcoding `~/.bedrock-gateway`, so there was exactly one
deployment per machine and exactly one session.

Supporting three deployments at once could have been done by teaching each half its
own precedence rules. That would be a bug factory: the failure mode is not "the
wrong message is printed", it is "this deployment's token is sent to that
deployment's gateway". So there is ONE implementation of

  * what a valid deployment name and URL are,
  * where a deployment's private files live,
  * and which deployment a command is running against,

and both halves call it. The bash side calls this file as a subprocess once at
entry (`resolve --format env`) and exports the result; the python side imports
`resolve()` directly. Neither re-derives the rule.

SELECTION (in order, first match wins)
--------------------------------------
  1. an explicit `--deployment NAME` before the verb
  2. an already-resolved parent context (ADP_DEPLOYMENT_ID), so children and
     helpers of one command cannot drift mid-command
  3. a non-empty ADP_DEPLOYMENT environment variable
  4. the saved default
  5. the legacy implicit deployment — the pre-#5413 `~/.bedrock-gateway` store

An unknown explicit selection FAILS. It never falls back to another deployment:
falling back is how a credential reaches an environment the user did not name.

STORAGE
-------
  ~/.adp/deployments.json                  registry: schema version, default, records
  ~/.adp/deployments/<stable-id>/
      config.json                          gateway URL + Cognito metadata
      tokens.json                          this deployment's session
      state/                               AWS, Bedrock, GitHub, admin area state
      runtime/                             proxy port/identity, spawn + refresh locks
      logs/                                proxy.log

The filesystem authority is a RANDOM STABLE ID, not the name. Rebinding a removed
name to a different URL therefore gets a different directory, so an old process
holding the old path cannot silently start writing into the new target's store.

Two names for the same canonical URL are ALIASES: one id, one session, no copied
token. Copying a rotating refresh token into two caches means whichever copy is
used second is already dead.

LEGACY
------
The existing single-deployment store is adopted as the record named `default`
IN PLACE — its config dir stays `~/.bedrock-gateway` and its state dir stays
`~/.adp/state`. Nothing is moved and no token is duplicated, so a user who only
ever wanted one deployment needs no new login and no new commands.

stdlib only, and no imports from adp_common: the bash front door runs this file
directly and must not depend on the rest of the python surface being loadable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

SCHEMA_VERSION = 1

# The version range this code understands. A registry written by a NEWER CLI is
# an error, not something to reinterpret: guessing at an unknown schema is how a
# deployment silently loses its binding.
SUPPORTED_SCHEMA_VERSIONS = (1,)

LEGACY_NAME = "default"

# Lowercase, starts with a letter, 1-63 chars. Rejects path separators, dots,
# whitespace, traversal and control characters by construction.
#
# \Z, not $: `$` also matches just BEFORE a trailing newline, so `^...$` would
# accept "dev\n". A name becomes a registry key, a directory component and a
# shell `export` value, so a trailing newline is not cosmetic.
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

REGISTRY_LOCK_TIMEOUT_SECONDS = 10
REGISTRY_LOCK_STALE_SECONDS = 30


class DeploymentError(Exception):
    """A deployment selection or registry failure, with a stable machine code.

    Mirrors adp_common.CliError's shape (message, code, exit_code) without
    importing it, so this module stays loadable on its own.
    """

    def __init__(self, message, code="operation_failed", exit_code=5):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def adp_home():
    """The CLI's private root. ADP_HOME exists for tests, not for users."""
    return Path(os.environ.get("ADP_HOME") or (Path.home() / ".adp"))


def registry_path():
    return adp_home() / "deployments.json"


def deployments_root():
    return adp_home() / "deployments"


def legacy_config_dir():
    """The pre-#5413 auth store.

    BG_CONFIG_DIR is the auth helper's own override and is honoured here so the
    two agree about where the legacy store is; a test harness that redirects one
    must not end up with the two halves looking at different directories.
    """
    return Path(os.environ.get("BG_CONFIG_DIR") or (Path.home() / ".bedrock-gateway"))


def legacy_state_dir():
    return adp_home() / "state"


def private_directory(path):
    """Create (0700) and verify a directory we are about to keep secrets in.

    Same contract as adp_common.private_directory, duplicated rather than imported
    for the standalone-loadability reason in the module docstring.
    """
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeploymentError(f"Use a private directory owned by you with permissions 0700: {path}", "unsafe_file")
    return path


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def validate_name(name):
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise DeploymentError(
            "A deployment name is 1-63 characters, lowercase, starting with a letter, using only letters, digits, '-' and '_'.",
            "usage_error",
            1,
        )
    return name


def canonical_url(url):
    """Canonicalize a gateway URL, or refuse it.

    The canonical form ends in `/api` because that is what bg-cognito-auth.sh and
    adp_common already append to reach the gateway's routes; normalizing here keeps
    `https://gw`, `https://gw/`, `https://gw/api` and `https://gw/api/` as ONE
    deployment instead of four, which is what makes alias detection work.

    HTTP is accepted on loopback only — that is how the deterministic tests point a
    real CLI at a real recording gateway without inventing a TLS fixture.
    """
    if not isinstance(url, str) or not url.strip():
        raise DeploymentError("Give the deployment's URL with --url https://<host>.", "usage_error", 1)
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.hostname:
        raise DeploymentError(f"{url!r} is not a usable URL. Use the form https://<host>.", "usage_error", 1)
    loopback = parsed.hostname in LOOPBACK_HOSTS
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise DeploymentError(
            f"A deployment URL must be https (http is allowed only on loopback): {url!r}.",
            "usage_error",
            1,
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeploymentError(
            "A deployment URL carries no credentials, query string or fragment — give the plain base URL.",
            "usage_error",
            1,
        )
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).rstrip("/")
    return base if base.endswith("/api") else base + "/api"


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DeploymentError(
            f"{path} could not be read as JSON ({exc}). Fix or move it — it is not overwritten automatically.",
            "deployment_state_unreadable",
        ) from None


def _write_json_private(path, value):
    path = Path(path)
    private_directory(path.parent)
    handle, temporary = tempfile.mkstemp(prefix=".adp-", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _RegistryLock:
    """Brief exclusive hold on the registry, for read-modify-write only.

    mkdir is the primitive because it is atomic on POSIX and needs no flock(1)
    (macOS does not ship one). Deliberately NOT held across a login, a network
    call or a token refresh: those take minutes, and blocking every other
    terminal's `deployment list` behind one browser approval would be its own bug.
    """

    def __init__(self):
        self._path = adp_home() / "registry.lock"
        self._held = False

    def __enter__(self):
        private_directory(self._path.parent)
        deadline = time.monotonic() + REGISTRY_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self._path.mkdir(mode=0o700)
                self._held = True
                return self
            except FileExistsError:
                if self._stale():
                    self._remove()
                    continue
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"Another adp command is updating the deployment registry. Retry, or remove {self._path} if nothing is running.",
                        "deployment_busy",
                    ) from None
                time.sleep(0.05)

    def __exit__(self, *_):
        if self._held:
            self._remove()
            self._held = False
        return False

    def _stale(self):
        try:
            return (time.time() - self._path.stat().st_mtime) >= REGISTRY_LOCK_STALE_SECONDS
        except OSError:
            return False

    def _remove(self):
        try:
            self._path.rmdir()
        except OSError:
            pass


def _empty_registry():
    return {"schema_version": SCHEMA_VERSION, "default": None, "deployments": {}}


def load_registry():
    """Read the registry, or return an empty one. Never invents records."""
    raw = _read_json(registry_path())
    if raw is None:
        return _empty_registry()
    if not isinstance(raw, dict):
        raise DeploymentError(
            f"{registry_path()} is not a deployment registry. Fix or move it, then re-run.",
            "deployment_state_unreadable",
        )
    version = raw.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DeploymentError(
            f"{registry_path()} uses deployment registry schema {version!r}, which this CLI does not understand. "
            "Update the CLI with 'adp update' — your deployments and sessions are left untouched.",
            "deployment_schema_unsupported",
        )
    records = raw.get("deployments")
    if not isinstance(records, dict):
        raise DeploymentError(
            f"{registry_path()} has no readable deployment records. Fix or move it, then re-run.",
            "deployment_state_unreadable",
        )
    for name, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not isinstance(record.get("gateway_url"), str):
            raise DeploymentError(
                f"The deployment record for {name!r} in {registry_path()} is incomplete. Fix or move the file, then re-run.",
                "deployment_state_unreadable",
            )
    return {"schema_version": version, "default": raw.get("default"), "deployments": records}


def save_registry(registry):
    _write_json_private(registry_path(), registry)


def new_stable_id():
    """A random id, so the filesystem authority is never the user-chosen name."""
    return "d" + secrets.token_hex(8)


# --------------------------------------------------------------------------
# legacy adoption
# --------------------------------------------------------------------------


def legacy_store_exists():
    return (legacy_config_dir() / "config.json").is_file()


def legacy_gateway_url():
    """The URL the legacy store is bound to, canonicalized, or None.

    Best-effort by design: `adp status` on a half-written legacy config must say
    something useful rather than raise, and the caller decides what an absent URL
    means. That is why this does NOT use _read_json — that reader is strict on
    purpose (a corrupt registry must never be silently rewritten), but applying
    the same strictness here would make a truncated legacy config crash even
    `deployment list`, which is the first command a confused user runs. The store
    is still never modified; only the unreadable URL is reported as unknown.
    """
    try:
        config = json.loads((legacy_config_dir() / "config.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    try:
        return canonical_url(config.get("gateway_url") or "")
    except DeploymentError:
        return None


def _legacy_record():
    """The in-memory record for the pre-#5413 store. `legacy` pins its paths.

    `legacy: True` is what stops adoption from becoming a migration: the record
    points AT the existing directories rather than at a new id-keyed one, so the
    user's rotating refresh token continues to live in exactly one place.
    """
    return {"id": LEGACY_NAME, "gateway_url": legacy_gateway_url() or "", "legacy": True}


def adopt_legacy(registry):
    """Register the legacy store as `default` if it exists and is unregistered.

    Returns (registry, changed). Idempotent, and safe to interrupt: the only write
    is one atomic registry replacement.
    """
    if not legacy_store_exists() or LEGACY_NAME in registry["deployments"]:
        return registry, False
    registry = {
        "schema_version": SCHEMA_VERSION,
        "default": registry.get("default") or LEGACY_NAME,
        "deployments": {**registry["deployments"], LEGACY_NAME: _legacy_record()},
    }
    return registry, True


def _registry_with_implicit_legacy(registry):
    """A READ-ONLY view including the legacy store, without writing anything.

    `status` and `deployment list` must be able to show an unadopted legacy store
    without mutating the machine, so resolution reads through this view and only
    the first genuinely mutating command calls adopt_legacy().
    """
    view, _ = adopt_legacy(registry)
    return view


# --------------------------------------------------------------------------
# the resolved context
# --------------------------------------------------------------------------


class Deployment:
    """One resolved, immutable selection. Created once per process, then pinned.

    Every path a command may touch is derived here, so no caller re-decides where
    a token or a state file lives partway through.
    """

    def __init__(self, name, record, selection_source):
        self.name = name
        self.id = record["id"]
        self.gateway_url = record.get("gateway_url") or ""
        self.legacy = bool(record.get("legacy"))
        self.selection_source = selection_source

    @property
    def root(self):
        return legacy_config_dir() if self.legacy else deployments_root() / self.id

    @property
    def config_dir(self):
        """Where config.json and tokens.json live — the auth helper's BG_CONFIG_DIR."""
        return self.root

    @property
    def state_dir(self):
        return legacy_state_dir() if self.legacy else self.root / "state"

    @property
    def runtime_dir(self):
        """Proxy port/identity and this deployment's locks.

        The legacy deployment keeps its runtime under ~/.adp so the pre-#5413
        ~/.bedrock-gateway layout (which other tooling greps) gains no new files.
        """
        return (adp_home() / "runtime" / LEGACY_NAME) if self.legacy else self.root / "runtime"

    @property
    def log_dir(self):
        return (adp_home() / "logs") if self.legacy else self.root / "logs"

    def ensure_directories(self):
        private_directory(self.config_dir)
        private_directory(self.state_dir)
        private_directory(self.runtime_dir)
        private_directory(self.log_dir)
        return self

    def describe(self, *, include_paths=False):
        """Secret-free metadata for `status`, `deployment list` and --json output."""
        detail = {
            "deployment": self.name,
            "deployment_id": self.id,
            "gateway_url": self.gateway_url,
            "selection_source": self.selection_source,
        }
        if include_paths:
            detail["config_dir"] = str(self.config_dir)
            detail["state_dir"] = str(self.state_dir)
        return detail

    def environment(self):
        """The pin handed to children, so one command cannot drift mid-flight.

        ADP_DEPLOYMENT_ID is the authority; the path variables are DERIVED from it
        and re-derived (and cross-checked) by resolve(), so an inherited path
        variable alone can never point one helper at another deployment's tokens.
        """
        return {
            "ADP_DEPLOYMENT_ID": self.id,
            "ADP_DEPLOYMENT_NAME": self.name,
            "ADP_DEPLOYMENT_SOURCE": self.selection_source,
            "ADP_DEPLOYMENT_URL": self.gateway_url,
            "BG_CONFIG_DIR": str(self.config_dir),
            "ADP_STATE_DIR": str(self.state_dir),
            "ADP_RUNTIME_DIR": str(self.runtime_dir),
            "ADP_LOG_DIR": str(self.log_dir),
        }

    def __repr__(self):
        return f"Deployment({self.name!r}, id={self.id!r}, source={self.selection_source!r})"


def _find_by_id(registry, stable_id):
    for name, record in registry["deployments"].items():
        if record["id"] == stable_id:
            return name, record
    return None, None


def _no_deployment_error():
    return DeploymentError(
        "No ADP deployment is configured. Register one with:\n"
        "  adp deployment add dev --url https://<your-gateway>\n"
        "or install against a gateway with the command on its sign-in page.",
        "deployment_not_found",
        1,
    )


def resolve(explicit=None, *, registry=None):
    """Decide which deployment this command runs against. See module docstring.

    Called ONCE per process. The result is pinned into the environment for every
    child, so a concurrent `adp deployment use` in another terminal cannot change
    where an in-flight request or a running login ends up.
    """
    registry = _registry_with_implicit_legacy(registry if registry is not None else load_registry())
    records = registry["deployments"]

    if explicit:
        validate_name(explicit)
        record = records.get(explicit)
        if record is None:
            raise DeploymentError(
                f"No deployment named {explicit!r}. Registered: {', '.join(sorted(records)) or 'none'}. "
                f"Add it with: adp deployment add {explicit} --url https://<host>",
                "deployment_not_found",
                1,
            )
        return Deployment(explicit, record, "flag")

    pinned = (os.environ.get("ADP_DEPLOYMENT_ID") or "").strip()
    if pinned:
        name, record = _find_by_id(registry, pinned)
        if record is None:
            raise DeploymentError(
                "This command inherited a deployment that is no longer registered. Start a new command, or re-add the deployment.",
                "deployment_not_found",
                1,
            )
        resolved = Deployment(name, record, os.environ.get("ADP_DEPLOYMENT_SOURCE") or "inherited")
        _reject_crossed_context(registry, resolved)
        return resolved

    selected = (os.environ.get("ADP_DEPLOYMENT") or "").strip()
    if selected:
        validate_name(selected)
        record = records.get(selected)
        if record is None:
            raise DeploymentError(
                f"ADP_DEPLOYMENT names {selected!r}, which is not registered. Registered: {', '.join(sorted(records)) or 'none'}. "
                f"Add it with: adp deployment add {selected} --url https://<host>",
                "deployment_not_found",
                1,
            )
        return Deployment(selected, record, "environment")

    default = registry.get("default")
    if default and default in records:
        # "legacy" is reported when the only reason there IS a default is the
        # pre-#5413 store the read-only view synthesized — a user who never ran
        # `deployment use` should see why their commands have a target.
        implicit_legacy = records[default].get("legacy") and not registry_path().exists()
        return Deployment(default, records[default], "legacy" if implicit_legacy else "default")

    if len(records) == 1:
        name, record = next(iter(records.items()))
        return Deployment(name, record, "only")

    raise _no_deployment_error()


def _reject_crossed_context(registry, resolved):
    """Refuse a context whose pinned id and inherited storage path disagree.

    The danger is narrow and specific: an inherited BG_CONFIG_DIR that belongs to
    a DIFFERENT registered deployment would aim the auth helper at that
    deployment's tokens while every message still named this one — the exact
    "token reaches the wrong gateway" failure this module exists to prevent.

    An inherited path that matches no registered deployment is NOT that bug. It is
    ordinary ambient environment: the legacy store, a test harness redirect, or a
    user who exports BG_CONFIG_DIR for the auth helper's own sake. Treating those
    as fatal would break the legacy user this change promises not to disturb, so
    the check looks for a real crossing rather than for mere inequality.
    """
    inherited = (os.environ.get("BG_CONFIG_DIR") or "").strip()
    if not inherited:
        return
    inherited_path = Path(inherited).absolute()
    if inherited_path == resolved.config_dir.absolute():
        return
    for other_name, other_record in registry["deployments"].items():
        if other_name == resolved.name:
            continue
        other = Deployment(other_name, other_record, "crosscheck")
        if other.config_dir.absolute() == inherited_path and other.id != resolved.id:
            raise DeploymentError(
                f"This command inherited a mixed deployment context: it is pinned to {resolved.name!r} "
                f"but its storage path belongs to {other_name!r}. Start a fresh command rather than continuing.",
                "deployment_mismatch",
            )


def current(explicit=None):
    """resolve() plus directory creation — the entry point a mutating command wants."""
    return resolve(explicit).ensure_directories()


# --------------------------------------------------------------------------
# mutating commands
# --------------------------------------------------------------------------


def _alias_of(registry, url):
    for name, record in registry["deployments"].items():
        if record.get("gateway_url") == url:
            return name, record
    return None, None


def add(name, url):
    """Register a deployment locally. No network, no sign-in, no token written.

    Three outcomes, all deliberate:
      * same name + same URL      -> unchanged (idempotent; re-running is safe)
      * same name + different URL -> REFUSED (silent rebinding sends a token to
                                     an environment the user did not name)
      * new name + existing URL   -> ALIAS: same stable id, same session
    """
    validate_name(name)
    url = canonical_url(url)
    with _RegistryLock():
        registry, _ = adopt_legacy(load_registry())
        existing = registry["deployments"].get(name)
        if existing is not None:
            if existing.get("gateway_url") == url:
                return _result("unchanged", name, registry, alias_of=None)
            raise DeploymentError(
                f"Deployment {name!r} is already bound to {existing.get('gateway_url')!r}. "
                f"A name is never silently rebound — remove it first, or choose another name for {url!r}.",
                "deployment_conflict",
                1,
            )
        alias_name, alias_record = _alias_of(registry, url)
        record = (
            {"id": alias_record["id"], "gateway_url": url, **({"legacy": True} if alias_record.get("legacy") else {})}
            if alias_record
            else {"id": new_stable_id(), "gateway_url": url}
        )
        registry = {
            "schema_version": SCHEMA_VERSION,
            # First deployment on a fresh machine becomes the default, so a
            # single-deployment user never has to run `deployment use`.
            "default": registry.get("default") or name,
            "deployments": {**registry["deployments"], name: record},
        }
        save_registry(registry)
    Deployment(name, record, "flag").ensure_directories()
    return _result("configured", name, registry, alias_of=alias_name)


def use(name):
    """Change ONLY the saved default. Never edits a shell rc or a live process."""
    validate_name(name)
    with _RegistryLock():
        registry, _ = adopt_legacy(load_registry())
        if name not in registry["deployments"]:
            raise DeploymentError(
                f"No deployment named {name!r}. Registered: {', '.join(sorted(registry['deployments'])) or 'none'}.",
                "deployment_not_found",
                1,
            )
        registry = {**registry, "default": name}
        save_registry(registry)
    return _result("configured", name, registry, alias_of=None)


def _busy_reason(deployment):
    """Why a deployment must not be removed right now, or None.

    Only evidence of CURRENT use counts. A stale pidfile from a killed session is
    not use, so ownership is established by signalling the process rather than by
    the file's existence.
    """
    runtime = deployment.runtime_dir
    for marker in ("proxy.json", "proxy.pid"):
        path = runtime / marker
        if not path.exists():
            continue
        try:
            raw = path.read_text().strip()
            pid = int(json.loads(raw)["pid"]) if marker.endswith(".json") else int(raw)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            return f"a proxy is running (pid {pid})"
        return f"a proxy is running (pid {pid})"
    if (deployment.config_dir / "refresh.lock").exists():
        return "a token refresh or login is in progress"
    return None


def remove(name):
    """Forget a deployment LOCALLY. Deletes nothing in the cloud, signs no one out.

    Refuses the saved default and a deployment in active use, because both make the
    next command's behaviour unpredictable rather than merely inconvenient.
    """
    validate_name(name)
    with _RegistryLock():
        registry, _ = adopt_legacy(load_registry())
        record = registry["deployments"].get(name)
        if record is None:
            raise DeploymentError(
                f"No deployment named {name!r}. Registered: {', '.join(sorted(registry['deployments'])) or 'none'}.",
                "deployment_not_found",
                1,
            )
        if registry.get("default") == name:
            others = sorted(set(registry["deployments"]) - {name})
            hint = f" Point the default elsewhere first: adp deployment use {others[0]}" if others else ""
            raise DeploymentError(
                f"{name!r} is the saved default, so removing it would leave commands with no target.{hint}",
                "deployment_busy",
            )
        deployment = Deployment(name, record, "flag")
        reason = _busy_reason(deployment)
        if reason:
            raise DeploymentError(
                f"{name!r} is in use — {reason}. Stop it, then remove the deployment.",
                "deployment_busy",
            )
        remaining = {key: value for key, value in registry["deployments"].items() if key != name}
        # Other aliases share this id and this session, so the store survives until
        # the last name pointing at it is gone.
        aliases_left = any(other["id"] == record["id"] for other in remaining.values())
        registry = {**registry, "deployments": remaining}
        save_registry(registry)
    return {
        "deployment": name,
        "removed_store": not aliases_left and not record.get("legacy"),
        "aliases_remaining": aliases_left,
        "store_retained_reason": "legacy store is never deleted" if record.get("legacy") else None,
    }


def listing():
    """Every registered deployment plus the effective selection. Read-only.

    Deliberately does NOT adopt, refresh a token or reach the network: listing is
    the command a confused user runs first, and it must be safe.
    """
    registry = _registry_with_implicit_legacy(load_registry())
    try:
        effective = resolve(registry=registry)
    except DeploymentError:
        effective = None
    return {
        "default": registry.get("default"),
        "effective": effective.name if effective else None,
        "selection_source": effective.selection_source if effective else None,
        "deployments": [
            {
                "name": name,
                "gateway_url": record.get("gateway_url") or "",
                "deployment_id": record["id"],
                "is_default": registry.get("default") == name,
                "is_effective": bool(effective) and effective.name == name,
                "legacy": bool(record.get("legacy")),
                "signed_in": (Deployment(name, record, "flag").config_dir / "tokens.json").is_file(),
            }
            for name, record in sorted(registry["deployments"].items())
        ],
    }


def _result(status, name, registry, *, alias_of):
    record = registry["deployments"][name]
    detail = {
        "status": status,
        "deployment": name,
        "gateway_url": record.get("gateway_url") or "",
        "deployment_id": record["id"],
        "default": registry.get("default"),
    }
    if alias_of:
        detail["alias_of"] = alias_of
    override = (os.environ.get("ADP_DEPLOYMENT") or "").strip()
    if override and override != name:
        # Saying "default is now X" while this terminal keeps using Y would be a
        # true statement that misleads. Report both.
        detail["effective_override"] = override
    return detail


# --------------------------------------------------------------------------
# command-line surface (used by the bash front door and by tests)
# --------------------------------------------------------------------------


def _emit_env(deployment):
    """Shell-quoted exports. The bash front door eval's this once, at entry."""
    for key, value in deployment.environment().items():
        print(f"export {key}={shlex.quote(value)}")


def _print_listing(data, as_json):
    if as_json:
        print(json.dumps(data))
        return 0
    if not data["deployments"]:
        print("No deployments registered.")
        print("Add one with: adp deployment add dev --url https://<your-gateway>")
        return 0
    width = max(len(entry["name"]) for entry in data["deployments"])
    for entry in data["deployments"]:
        marks = "".join(("*" if entry["is_default"] else " ", ">" if entry["is_effective"] else " "))
        signed = "signed in" if entry["signed_in"] else "not signed in"
        print(f"{marks} {entry['name']:<{width}}  {entry['gateway_url']}  ({signed})")
    print("")
    print("* saved default   > selected for this terminal")
    if data["effective"]:
        print(f"Selected: {data['effective']} (from {data['selection_source']})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="adp deployment", description="Register and select named ADP deployments (Issue #5413).")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="verb")

    add_parser = subparsers.add_parser("add", help="register a deployment by name and URL")
    add_parser.add_argument("name")
    add_parser.add_argument("--url", required=True)

    subparsers.add_parser("list", help="show registered deployments and the effective selection")

    use_parser = subparsers.add_parser("use", help="change the saved default")
    use_parser.add_argument("name")

    remove_parser = subparsers.add_parser("remove", help="forget a deployment locally")
    remove_parser.add_argument("name")

    resolve_parser = subparsers.add_parser("resolve", help="print the effective selection (internal)")
    resolve_parser.add_argument("--deployment", default=None)
    resolve_parser.add_argument("--format", choices=("env", "json"), default="json")
    resolve_parser.add_argument("--ensure", action="store_true", help="create the private directories")

    args = parser.parse_args(argv)
    if not args.verb:
        parser.print_help()
        return 1

    try:
        if args.verb == "add":
            detail = add(args.name, args.url)
            _emit_result("deployment add", detail, args.json)
        elif args.verb == "use":
            detail = use(args.name)
            _emit_result("deployment use", detail, args.json)
        elif args.verb == "remove":
            detail = remove(args.name)
            _emit_result("deployment remove", detail, args.json)
        elif args.verb == "list":
            return _print_listing(listing(), args.json)
        elif args.verb == "resolve":
            deployment = resolve(args.deployment)
            if args.ensure:
                deployment.ensure_directories()
            if args.format == "env":
                _emit_env(deployment)
            else:
                print(json.dumps(deployment.describe(include_paths=True)))
        return 0
    except DeploymentError as exc:
        if args.json:
            print(json.dumps({"status": "failed", "error": {"code": exc.code, "message": str(exc)}}))
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return exc.exit_code


def _emit_result(command, detail, as_json):
    status = detail.pop("status", "configured")
    if as_json:
        print(json.dumps({"status": status, "command": command, "detail": detail, "next_action": None}))
        return
    print(f"{command}: {status}")
    print(json.dumps(detail, indent=2))


if __name__ == "__main__":
    sys.exit(main())
