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
import base64
import binascii
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
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

# The AWS profile the auth helper's Identity Pool exchange has always written.
# Kept verbatim for the legacy deployment; see Deployment.aws_profile.
LEGACY_AWS_PROFILE = "bedrock-gateway"

REGISTRY_LOCK_TIMEOUT_SECONDS = 10


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
    override = os.environ.get("ADP_LEGACY_CONFIG_DIR") or os.environ.get("BG_CONFIG_DIR")
    if override:
        path = Path(override).absolute()
        # BG_CONFIG_DIR is derived for named children, not a new legacy store.
        if path != deployments_root().absolute() and deployments_root().absolute() not in path.parents:
            return path
    return legacy_home_config_dir()


def legacy_home_config_dir():
    """Where the legacy store lives on this machine, ignoring BG_CONFIG_DIR.

    legacy_config_dir() deliberately honours the inherited override so both halves
    agree; the crossing check needs the un-overridden location, because comparing an
    inherited path against a value derived from that same variable can only ever be
    equal (see _reject_crossed_context).
    """
    return Path.home() / ".bedrock-gateway"


def legacy_state_dir():
    return adp_home() / "state"


def private_directory(path, *, allow_readable=False):
    """Create (0700) and verify a directory we are about to keep secrets in.

    The registry's non-secret parent may already be 0755 from installing
    ~/.adp/bin; the legacy auth root may also already be 0755. Allow that
    only for these existing roots. Foreign writes and symlinks are refused.
    """
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    forbidden = 0o022 if allow_readable else 0o077
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & forbidden:
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
    if any(char.isspace() or ord(char) < 32 for char in url):
        raise DeploymentError("A deployment URL cannot contain whitespace or control characters.", "usage_error", 1)
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise DeploymentError("Give a valid gateway hostname and port.", "usage_error", 1) from None
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
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = host + (f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else "")
    base = urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", "")).rstrip("/")
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
    private_directory(path.parent, allow_readable=path == registry_path())
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

    The persistent file is locked through the Python fcntl API available on the
    supported macOS/Linux platforms. The kernel releases ownership when the
    process exits, so no age-based reclamation can remove a current owner's lock.
    Deliberately NOT held across a login, a network call or a token refresh: those
    take minutes, and blocking every other terminal's `deployment list` behind
    one browser approval would be its own bug.
    """

    def __init__(self):
        self._path = adp_home() / "registry.lock"
        self._handle = None

    def __enter__(self):
        private_directory(self._path.parent, allow_readable=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(self._path, flags, 0o600)
            handle = os.fdopen(descriptor, "r+")
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise DeploymentError(f"Could not open the deployment registry lock {self._path}: {exc}", "unsafe_file") from None
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise DeploymentError(f"Use a private lock file owned by you with permissions 0600: {self._path}", "unsafe_file")
            deadline = time.monotonic() + REGISTRY_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DeploymentError(
                            "Another adp command is updating the deployment registry. Retry after it finishes.",
                            "deployment_busy",
                        ) from None
                    time.sleep(0.05)
        except BaseException:
            handle.close()
            raise

    def __exit__(self, *_):
        if self._handle is not None:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
        return False


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
    bindings = {}
    for name, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not isinstance(record.get("gateway_url"), str):
            raise DeploymentError(
                f"The deployment record for {name!r} in {registry_path()} is incomplete. Fix or move the file, then re-run.",
                "deployment_state_unreadable",
            )
        valid_id = record["id"] == LEGACY_NAME if record.get("legacy") else re.fullmatch(r"d[0-9a-f]{16}", record["id"])
        if not NAME_PATTERN.fullmatch(name) or not valid_id:
            raise DeploymentError("The deployment registry contains an unsafe name or storage id.", "deployment_state_unreadable")
        if not record.get("legacy"):
            try:
                url = canonical_url(record["gateway_url"])
            except DeploymentError:
                raise DeploymentError("The deployment registry contains an invalid gateway URL.", "deployment_state_unreadable") from None
            if url != record["gateway_url"] or bindings.get(record["id"], url) != url:
                raise DeploymentError("One deployment store has inconsistent gateway bindings.", "deployment_state_unreadable")
            bindings[record["id"]] = url
    if raw.get("default") is not None and raw["default"] not in records:
        raise DeploymentError("The saved default is missing from the deployment registry.", "deployment_state_unreadable")
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
    """Include the original store without adopting it on a read.

    The implicit legacy name follows its existing config for compatibility.
    Explicit aliases retain the URL they were registered with: if the original
    store is rebound, validate_config refuses to use that alias's credentials.
    """
    view, _ = adopt_legacy(registry)
    live = legacy_gateway_url()
    if not live:
        return view
    refreshed = {
        name: ({**record, "gateway_url": live} if name == LEGACY_NAME and record.get("legacy") and record.get("gateway_url") != live else record)
        for name, record in view["deployments"].items()
    }
    if refreshed != view["deployments"]:
        view = {**view, "deployments": refreshed}
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
        self._legacy_root = legacy_config_dir()
        self._root = self._legacy_root if self.legacy else deployments_root() / self.id
        self._state = legacy_state_dir() if self.legacy else self._root / "state"
        self._logs = adp_home() / "logs" if self.legacy else self._root / "logs"

    @property
    def root(self):
        return self._root

    @property
    def config_dir(self):
        """Where config.json and tokens.json live — the auth helper's BG_CONFIG_DIR."""
        return self.root

    @property
    def state_dir(self):
        return self._state

    @property
    def runtime_dir(self):
        """Proxy pidfile, published proxy identity and this deployment's locks.

        The legacy deployment keeps these in `~/.bedrock-gateway`, where they have
        always lived. That is not cosmetic: `proxy.pid` and `proxy-spawn.lock` are
        how a RUNNING proxy is discovered. Relocating them during an upgrade would
        make an already-running proxy invisible to the new CLI, which would then
        try to start a second one on the same port and fail with an opaque
        "address already in use" — the upgrade breaking the thing it should leave
        alone. Named deployments, having no such history, keep theirs together
        with the rest of their private files.
        """
        return self.root if self.legacy else self.root / "runtime"

    @property
    def log_dir(self):
        return self._logs

    def validate_config(self):
        config = _read_json(self.config_dir / "config.json")
        if config is None:
            return
        if not isinstance(config, dict):
            raise DeploymentError("The session configuration is not a JSON object.", "deployment_state_unreadable")
        stored = config.get("gateway_url")
        if self.gateway_url and (not stored or canonical_url(stored) != self.gateway_url):
            raise DeploymentError(
                f"The session store for {self.name!r} belongs to another gateway. Repair its configuration before continuing.",
                "deployment_mismatch",
            )

    def ensure_directories(self):
        """Create this deployment's private directories, 0700.

        The legacy config dir is the one exception, and adopting it must not be
        stricter than living with it was: install.sh and the auth helper have
        always created `~/.bedrock-gateway` with a plain `mkdir -p`, so on a normal
        umask it is 0755 on real machines. Demanding 0700 there would make every
        pre-existing user's first command after upgrading fail with a permissions
        error about a directory they never chose the mode of — a regression, not a
        security win, since we are not the ones who created it and the token file
        itself is written 0600. Unsafe ownership, symlinks and group/world writes
        are still refused.
        """
        if self.legacy:
            private_directory(self.config_dir, allow_readable=True)
        else:
            private_directory(self.config_dir)
        private_directory(self.state_dir)
        if not self.legacy:
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
            "aws_profile": self.aws_profile,
        }
        if include_paths:
            detail["config_dir"] = str(self.config_dir)
            detail["state_dir"] = str(self.state_dir)
        return detail

    @property
    def aws_profile(self):
        """The AWS profile name this deployment's Cognito credentials write to.

        The auth helper's Identity Pool exchange writes a profile into the user's
        own ~/.aws/credentials. That name was fixed, so three deployments would
        each overwrite the other two's AWS credentials — cross-deployment
        interference of exactly the kind this change exists to stop.

        The legacy deployment keeps the original name, because an existing user
        has `AWS_PROFILE=bedrock-gateway` in their shell profile, scripts and
        muscle memory; renaming it would break them for no benefit. Named
        deployments use their stable id, so aliases share credentials and removing
        one name cannot change the profile identity of the remaining aliases.
        """
        return LEGACY_AWS_PROFILE if self.legacy else f"adp-deployment-{self.id}"

    def alias_aws_profiles(self):
        """Profiles written by older CLI builds using aliases instead of ids."""
        if self.legacy:
            return []
        records = load_registry()["deployments"]
        return [f"{LEGACY_AWS_PROFILE}-{name}" for name, record in records.items() if record["id"] == self.id]

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
            "ADP_LEGACY_CONFIG_DIR": str(self._legacy_root),
            "BG_CONFIG_DIR": str(self.config_dir),
            "ADP_STATE_DIR": str(self.state_dir),
            "ADP_RUNTIME_DIR": str(self.runtime_dir),
            "ADP_LOG_DIR": str(self.log_dir),
            "BG_AWS_PROFILE": self.aws_profile,
            "BG_AWS_RETIRED_PROFILES": json.dumps(self.alias_aws_profiles()),
        }

    def __repr__(self):
        return f"Deployment({self.name!r}, id={self.id!r}, source={self.selection_source!r})"


def _find_by_id(registry, stable_id):
    inherited_name = os.environ.get("ADP_DEPLOYMENT_NAME")
    inherited = registry["deployments"].get(inherited_name)
    if inherited and inherited["id"] == stable_id:
        return inherited_name, inherited
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
        resolved = Deployment(explicit, record, "flag")
        _reject_crossed_legacy_store(registry, resolved)
        return resolved

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
        inherited_url = os.environ.get("ADP_DEPLOYMENT_URL")
        if inherited_url and canonical_url(inherited_url) != resolved.gateway_url:
            raise DeploymentError("The inherited deployment URL has changed. Start a new command.", "deployment_mismatch")
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
        resolved = Deployment(selected, record, "environment")
        _reject_crossed_legacy_store(registry, resolved)
        return resolved

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
    # A LEGACY record's config_dir IS legacy_config_dir(), which itself reads
    # BG_CONFIG_DIR — so for the legacy deployment this comparison used to be the
    # inherited path against itself, always equal, and the guard could never fire.
    # That made the one deployment whose store is a plain fixed path the only one
    # with no crossing protection: with another deployment's BG_CONFIG_DIR
    # inherited, `--deployment default` resolved to that store, handed out its
    # token, and `logout` deleted its session. Compare against the legacy store's
    # REAL location instead, so an inherited path pointing elsewhere is a crossing.
    resolved_path = (legacy_home_config_dir() if resolved.legacy else resolved.config_dir).absolute()
    if inherited_path == resolved_path:
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


def _reject_crossed_legacy_store(registry, resolved):
    """Refuse a LEGACY selection whose store is another deployment's, on any route.

    _reject_crossed_context guards the inherited-pin route, where the id and the
    path must agree. This is the narrower case that an EXPLICIT selection can still
    reach: only the legacy deployment reads its store location from BG_CONFIG_DIR,
    so only it can be aimed at another deployment's directory by an inherited
    environment. A non-legacy record derives its path from its stable id and simply
    overrides whatever it inherited, which is why selecting a named deployment from
    inside another deployment's session stays legitimate and is NOT touched here.

    Without this, `adp --deployment dev claude` (which exports dev's BG_CONFIG_DIR)
    followed by `adp --deployment default <verb>` inside that session resolved
    'default' onto DEV's store: it printed dev's URL, handed out dev's token, and
    `logout` deleted dev's session while the legacy one sat untouched.
    """
    if not resolved.legacy:
        return
    inherited = (os.environ.get("BG_CONFIG_DIR") or "").strip()
    if not inherited:
        return
    inherited_path = Path(inherited).absolute()
    if inherited_path == legacy_home_config_dir().absolute():
        return
    for other_name, other_record in registry["deployments"].items():
        if other_record.get("legacy"):
            continue
        other = Deployment(other_name, other_record, "crosscheck")
        if other.config_dir.absolute() == inherited_path:
            raise DeploymentError(
                f"This command inherited a mixed deployment context: it selected {resolved.name!r} "
                f"but the storage path it inherited belongs to {other_name!r}. Start a fresh command rather than continuing.",
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


def _process_start(pid):
    """This process's start time, used to make a PID-reuse collision harmless.

    Two sources, because `ps` is NOT universally present: it ships in base macOS
    but is a separate `procps` package on slim Linux images, and a lease is taken
    by EVERY command against a named deployment. Reading /proc directly first
    keeps those images working; `ps` remains the fallback for macOS, which has no
    /proc. Neither available is reported by the caller, not silently ignored.

    The value is only ever compared to another reading of the SAME pid on the same
    machine, so the two formats never need to agree with each other. They are
    tagged so a reading from one source can never accidentally compare equal to a
    reading from the other.
    """
    if pid <= 0:
        return None
    # Linux: field 22 of /proc/<pid>/stat is the start time in clock ticks since
    # boot. Parsed from the LAST ')' because a process name may contain ')'.
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
        return "proc:" + stat_line[stat_line.rindex(")") + 2 :].split()[19]
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return "ps:" + result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def setup_port(deployment):
    """Keep bare Codex's saved endpoint stable across proxy restarts."""
    if deployment.legacy:
        return 9191
    with _RegistryLock():
        path = deployment.runtime_dir / "setup-port.json"
        saved = _read_json(path)
        if saved:
            return int(saved["port"])
        identity = _read_json(deployment.runtime_dir / "proxy.json") or {}
        port = identity.get("port") if identity.get("deployment_id") == deployment.id else None
        if not port:
            reserved = {int(value["port"]) for item in deployments_root().glob("*/runtime/setup-port.json") if (value := _read_json(item))}
            # Holding the registry lock excludes other setup commands. Binding
            # tests external availability; an unrelated later bind fails closed.
            for _ in range(100):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    candidate = probe.getsockname()[1]
                if candidate not in reserved:
                    port = candidate
                    break
            if not port:
                raise DeploymentError("Could not reserve a distinct local proxy port.", "deployment_busy")
        _write_json_private(path, {"port": port})
        return int(port)


def helper_command(deployment, adp_path):
    environment = deployment.environment()
    environment["ADP_DEPLOYMENT_SOURCE"] = "setup"
    return shlex.join(["env", "-u", "ADP_DEPLOYMENT", *[f"{key}={value}" for key, value in environment.items()], adp_path, "token"])


def lease(deployment, pid):
    """Protect a running command, including the tool that replaces it via exec.

    Registration and removal share the registry lock. Process start time makes a
    stale lease harmless after PID reuse; no cleanup handler must survive exec.
    """
    with _RegistryLock():
        records = _registry_with_implicit_legacy(load_registry())["deployments"]
        current = records.get(deployment.name) or {}
        if current.get("id") != deployment.id or current.get("gateway_url") != deployment.gateway_url:
            raise DeploymentError("The selected deployment changed before this command started.", "deployment_mismatch")
        start = _process_start(pid)
        if not start:
            raise DeploymentError(
                "Could not establish the running command's identity: neither /proc nor 'ps' is available. "
                "Install 'ps' (the procps package) to use named deployments.",
                "deployment_busy",
            )
        directory = deployment.runtime_dir / "leases"
        for old in directory.glob("*.json"):
            if old.stem.isdigit() and int(old.stem) > 0:
                try:
                    os.kill(int(old.stem), 0)
                except ProcessLookupError:
                    old.unlink(missing_ok=True)
                except PermissionError:
                    pass
        _write_json_private(directory / f"{pid}.json", {"pid": pid, "start": start})


def _codex_key_parts(expression):
    """Parse TOML dotted keys without requiring Python 3.11's tomllib.

    Values are left to Codex. Quoted key escapes must be decoded before comparing
    a path with ADP-managed fields; stripping quote characters is insufficient.
    """
    part = re.compile(r"""[ \t]*(?:"((?:[^"\\\r\n]|\\.)*)"|'([^'\r\n]*)'|([A-Za-z0-9_-]+))[ \t]*([.=])""")
    position, result = 0, []
    while True:
        matched = part.match(expression, position)
        if not matched:
            raise DeploymentError("Invalid Codex config key. Use a TOML key=value override.", "invalid_arguments")
        basic, literal, bare, separator = matched.groups()
        value = literal if literal is not None else bare
        if basic is not None:
            value, index = "", 0
            escapes = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
            while index < len(basic):
                if basic[index] != "\\":
                    value += basic[index]
                    index += 1
                    continue
                index += 1
                escape = basic[index]
                index += 1
                if escape in escapes:
                    value += escapes[escape]
                elif escape in ("u", "U"):
                    length = 4 if escape == "u" else 8
                    digits = basic[index : index + length]
                    if len(digits) != length or not re.fullmatch("[0-9a-fA-F]+", digits):
                        raise DeploymentError("Invalid Unicode escape in Codex config key.", "invalid_arguments")
                    codepoint = int(digits, 16)
                    if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                        raise DeploymentError("Invalid Unicode scalar in Codex config key.", "invalid_arguments")
                    value += chr(codepoint)
                    index += length
                else:
                    raise DeploymentError("Unsupported escape in Codex config key; use an unescaped key.", "invalid_arguments")
        result.append(value)
        if separator == "=":
            return result
        position = matched.end()


def check_codex_args(arguments):
    managed = {"base_url", "wire_api", "env_key", "experimental_bearer_token", "requires_openai_auth"}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        expression = None
        if argument == "--":
            break
        if argument in ("--oss", "--local-provider") or argument.startswith("--local-provider="):
            raise DeploymentError("This option replaces the selected ADP transport.", "invalid_arguments")
        if argument in ("-c", "--config"):
            if index >= len(arguments):
                raise DeploymentError("Codex config override requires key=value.", "invalid_arguments")
            expression = arguments[index]
            index += 1
        elif argument.startswith("--config="):
            expression = argument[len("--config=") :]
        elif argument.startswith("-c"):
            expression = argument[2:]
            if expression.startswith("="):
                expression = expression[1:]
        if expression is None:
            continue
        path = _codex_key_parts(expression)
        if path[0] == "model_provider" or (
            path[0] == "model_providers" and (len(path) == 1 or (path[1] == "adp-gateway" and (len(path) == 2 or path[2] in managed)))
        ):
            raise DeploymentError("ADP manages the selected Codex transport. Remove that config override.", "invalid_arguments")


def proxy_owner(runtime, deployment_id, gateway_url):
    """Return a proxy record only when its process identity still matches.

    PID existence alone is never ownership: an unclean exit followed by PID
    reuse must not block recovery or suggest terminating another process.
    """
    try:
        value = _read_json(Path(runtime) / "proxy.json") or {}
        pid = value.get("pid")
        if type(pid) is not int or pid <= 0 or value.get("proxy") != "adp-gateway-proxy":
            return None
        if value.get("deployment_id", "") != deployment_id or canonical_url(value.get("gateway_url", "")) != canonical_url(gateway_url):
            return None
        start = value.get("process_start")
        if not start or start != _process_start(pid):
            return None
        return value
    except (OSError, ValueError, TypeError, DeploymentError):
        return None


def _busy_reason(deployment):
    """Why a deployment must not be removed right now, or None.

    Only evidence of CURRENT use counts. A stale pidfile from a killed session is
    not use, so PID plus process start time and deployment metadata establish
    ownership, rather than the file's existence or a successful kill -0.
    """
    runtime = deployment.runtime_dir
    for path in (runtime / "leases").glob("*.json"):
        value = _read_json(path) or {}
        start = _process_start(int(value.get("pid", 0)))
        if start and start == value.get("start"):
            return f"a command is running (pid {value['pid']})"
    daemon = Path.home() / "Library/LaunchAgents" / f"com.adp.gateway-proxy.{deployment.id}.plist"
    if daemon.exists():
        return "an always-on proxy is installed; run adp daemon uninstall for this deployment"
    owner = proxy_owner(runtime, deployment.id, deployment.gateway_url)
    if owner:
        return f"a proxy is running (pid {owner['pid']})"
    if (deployment.config_dir / "refresh.lock").exists():
        return "a token refresh or login is in progress"
    return None


def update_aws_profile(operation, profile, region="", values=(), *, retired_profiles=()):
    """Serialize complete-file AWS profile changes across all deployments.

    Retire old alias-derived sections while publishing the stable profile. The
    same primitive is used by auth and local removal, including their lock.
    """
    credentials = Path.home() / ".aws" / "credentials"
    config = Path.home() / ".aws" / "config"
    try:
        if operation not in ("write", "delete") or any(c in profile for c in "\r\n[]") or any(c in region for c in "\r\n"):
            raise ValueError("invalid profile metadata")
        if operation == "write" and (len(values) != 3 or not all(values) or any("\n" in v or "\r" in v for v in values)):
            raise ValueError("invalid credential fields")
        if operation == "delete" and not credentials.exists() and not config.exists():
            return
        directory = Path(credentials).parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(directory / ".adp-profiles.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock, "r+") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("unsafe lock")
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("another profile update is still running")
                    time.sleep(0.05)
            updates = []
            for path, section in ((Path(credentials), profile), (Path(config), "profile " + profile)):
                if operation == "delete" and not path.exists():
                    continue
                sections = {section, *(p if path == Path(credentials) else "profile " + p for p in retired_profiles)}
                existing = path.read_text() if path.exists() else ""
                lines, skip = [], False
                for line in existing.splitlines(keepends=True):
                    header = re.match(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?$", line.strip())
                    if header:
                        skip = header.group(1) in sections
                    if not skip:
                        lines.append(line)
                content = "".join(lines)
                if operation == "write":
                    content += "\n[" + section + "]\n"
                    if path == Path(credentials):
                        for key, value in zip(("aws_access_key_id", "aws_secret_access_key", "aws_session_token"), values):
                            content += key + " = " + value + "\n"
                    else:
                        content += "region = " + region + "\noutput = json\n"
                updates.append((path, content))
            for path, content in updates:
                fd, temporary = tempfile.mkstemp(prefix=".adp-profile-", dir=path.parent)
                try:
                    with os.fdopen(fd, "w") as output:
                        output.write(content)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
    except (OSError, ValueError):
        raise DeploymentError(
            "Could not update the shared AWS profiles. Check file permissions or retry after the other update finishes.",
            "aws_profile_update_failed",
        ) from None


def remove(name):
    """Forget a deployment LOCALLY. Deletes nothing in the cloud, signs no one out.

    Refuses the saved default and a deployment in active use, because both make the
    next command's behaviour unpredictable rather than merely inconvenient.
    """
    validate_name(name)
    cleanup = None
    profiles_to_remove = []
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
        if not aliases_left and not deployment.legacy and deployment.root.exists():
            if deployment.root.is_symlink() or deployments_root().is_symlink():
                raise DeploymentError("Refusing to remove a deployment store through a symlink.", "unsafe_file")
            private_directory(deployment.root)
            cleanup = deployment.root
        if not deployment.legacy:
            profiles_to_remove = [f"{LEGACY_AWS_PROFILE}-{name}"]
            if not aliases_left:
                profiles_to_remove.append(deployment.aws_profile)
        registry = {**registry, "deployments": remaining}
        # Publish before destructive cleanup. A failed publication leaves the
        # entire registered store intact; interruption after publication leaves
        # only an unregistered, recoverable private directory. New registrations
        # use fresh stable ids, so they cannot reuse this cleanup target.
        save_registry(registry)
    try:
        if profiles_to_remove:
            update_aws_profile("delete", profiles_to_remove[0], retired_profiles=profiles_to_remove[1:])
        if cleanup is not None:
            shutil.rmtree(cleanup)
    except (OSError, DeploymentError):
        raise DeploymentError(
            f"The registration was removed, but local cleanup is incomplete at {cleanup or Path.home() / '.aws'}. "
            "Remove residual AWS profile sections and any unregistered directory after resolving the filesystem error.",
            "deployment_cleanup_incomplete",
        ) from None
    return {
        # `_emit_result` keys its wording off this status, and its default is
        # "configured" — so omitting it made a successful `adp deployment remove
        # integration` print "Deployment 'integration' is registered for ." and
        # then invite the user to sign in to the deployment they just removed.
        "status": "removed",
        "deployment": name,
        "gateway_url": record.get("gateway_url") or "",
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


def session_status():
    """Read local session metadata without refreshing or exposing credentials."""
    try:
        selected = resolve()
        selected.validate_config()
    except DeploymentError as exc:
        if exc.code != "deployment_not_found" or registry_path().exists() or os.environ.get("ADP_DEPLOYMENT_ID") or os.environ.get("ADP_DEPLOYMENT"):
            raise
        selected = None
    root = selected.config_dir if selected else legacy_config_dir()
    config = _read_json(root / "config.json")
    tokens = _read_json(root / "tokens.json")
    if (config is not None and not isinstance(config, dict)) or (tokens is not None and not isinstance(tokens, dict)):
        raise DeploymentError("The local session files must contain JSON objects.", "deployment_state_unreadable")
    signed_in = config is not None and tokens is not None
    config, tokens = config or {}, tokens or {}
    try:
        expiry = int(tokens.get("expires_at") or 0)
    except (ValueError, TypeError, OverflowError):
        raise DeploymentError("The local session expiry is invalid.", "deployment_state_unreadable") from None
    user = None
    try:
        payload = str(tokens.get("access_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        candidate = claims.get("username") or claims.get("sub")
        if isinstance(candidate, str):
            user = candidate
    except (IndexError, ValueError, AttributeError, binascii.Error):
        pass
    detail = selected.describe() if selected else {"deployment": None, "deployment_id": None, "selection_source": "legacy"}
    detail.update(
        gateway_url=(selected.gateway_url if selected else "") or config.get("gateway_url") or None,
        signed_in=signed_in,
        user=user,
        refresh_mode=config.get("refresh_via") or "cognito",
        access_token_state=("valid" if expiry > time.time() else "expired") if signed_in else "absent",
        expires_at=expiry if signed_in else None,
    )
    return {
        "status": "configured" if signed_in else "unavailable",
        "command": "status",
        "detail": detail,
        "next_action": None if signed_in else f"Run adp --deployment {selected.name} login" if selected else "Run adp login",
    }, 0 if signed_in else 1


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

    # `--json` accepted on either side of the verb, because every other `adp`
    # command takes its flags AFTER the subcommand and a user (or a script) has no
    # reason to expect this one to be different. `SUPPRESS` is what makes the two
    # positions coexist: an absent flag leaves the attribute unset here instead of
    # writing False over a `--json` that was given before the verb, which is the
    # argparse behaviour that would otherwise make the earlier form silently
    # produce prose.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    subparsers = parser.add_subparsers(dest="verb", parser_class=argparse.ArgumentParser)

    add_parser = subparsers.add_parser("add", parents=[shared], help="register a deployment by name and URL")
    add_parser.add_argument("name")
    add_parser.add_argument("--url", required=True)

    subparsers.add_parser("list", parents=[shared], help="show registered deployments and the effective selection")
    subparsers.add_parser("status", parents=[shared], help="print local session metadata (internal)")

    use_parser = subparsers.add_parser("use", parents=[shared], help="change the saved default")
    use_parser.add_argument("name")

    remove_parser = subparsers.add_parser("remove", parents=[shared], help="forget a deployment locally")
    remove_parser.add_argument("name")

    resolve_parser = subparsers.add_parser("resolve", parents=[shared], help="print the effective selection (internal)")
    resolve_parser.add_argument("--deployment", default=None)
    resolve_parser.add_argument("--format", choices=("env", "json"), default="json")
    resolve_parser.add_argument("--ensure", action="store_true", help="create the private directories")
    resolve_parser.add_argument("--lease-pid", type=int, help="protect the calling command from concurrent removal")
    resolve_parser.add_argument("--validate-config", action="store_true")

    # Exposed so the bash front door can compare two URLs the way alias detection
    # does. `https://gw`, `https://gw/` and `https://gw/api/` are ONE gateway, so a
    # login check written as a string comparison would reject a correct URL over a
    # trailing slash while still missing a genuinely different host.
    canonicalize_parser = subparsers.add_parser("canonicalize", parents=[shared], help="print a URL's canonical form (internal)")
    canonicalize_parser.add_argument("url")
    helper_parser = subparsers.add_parser("helper-command", help="print a pinned Claude token helper (internal)")
    helper_parser.add_argument("adp_path")
    subparsers.add_parser("setup-port", help="reserve the selected deployment's bare Codex port (internal)")
    owner_parser = subparsers.add_parser("proxy-owner", help="print a verified proxy PID (internal)")
    owner_parser.add_argument("runtime")
    owner_parser.add_argument("deployment_id")
    owner_parser.add_argument("gateway_url")
    codex_parser = subparsers.add_parser("check-codex-args", help="validate Codex transport overrides (internal)")
    codex_parser.add_argument("arguments", nargs=argparse.REMAINDER)

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
        elif args.verb == "status":
            detail, exit_code = session_status()
            print(json.dumps(detail))
            return exit_code
        elif args.verb == "canonicalize":
            print(canonical_url(args.url))
        elif args.verb == "helper-command":
            print(helper_command(resolve(), args.adp_path))
        elif args.verb == "setup-port":
            print(setup_port(resolve()))
        elif args.verb == "proxy-owner":
            owner = proxy_owner(args.runtime, args.deployment_id, args.gateway_url)
            if owner:
                print(owner["pid"])
        elif args.verb == "check-codex-args":
            check_codex_args(args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments)
        elif args.verb == "resolve":
            deployment = resolve(args.deployment)
            if args.validate_config:
                deployment.validate_config()
            if args.lease_pid and not deployment.legacy:
                lease(deployment, args.lease_pid)
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
    """Report a mutation. JSON for scripts; for a person, prose — not a JSON dump.

    A human running `adp deployment add dev` wants to know what happened and what
    to do next, so the interesting consequences are spelled out: that a name is an
    alias sharing another's session, that this is now the default, and that the
    terminal's own ADP_DEPLOYMENT still overrides that default.
    """
    status = detail.pop("status", "configured")
    if as_json:
        print(json.dumps({"status": status, "command": command, "detail": detail, "next_action": None}))
        return

    name = detail.get("deployment", "")
    if status == "unchanged":
        print(f"Deployment '{name}' is already registered for {detail.get('gateway_url', '')} — nothing to change.")
    elif status == "removed":
        print(f"Deployment '{name}' has been forgotten locally. Nothing in the cloud was changed.")
        if detail.get("aliases_remaining"):
            print("Its session is kept, because another name still points at the same gateway.")
    elif command == "deployment use":
        print(f"Saved default is now '{name}' ({detail.get('gateway_url', '')}). New terminals will use it.")
    else:
        print(f"Deployment '{name}' is registered for {detail.get('gateway_url', '')}.")
        if detail.get("alias_of"):
            print(f"It is another name for '{detail['alias_of']}' and shares that deployment's sign-in.")
        elif detail.get("default") == name:
            print("It is the saved default, so commands use it unless you select another.")
        else:
            print(f"Sign in to it with: adp --deployment {name} login")

    override = detail.get("effective_override")
    if override and override != name:
        print(f"Note: this terminal has ADP_DEPLOYMENT={override}, which still takes precedence here.")


if __name__ == "__main__":
    sys.exit(main())
