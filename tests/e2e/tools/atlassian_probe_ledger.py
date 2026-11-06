"""Restricted, owner-bound local state for an isolated Identity probe.

Only an Identity session reference and nonsecret attempt metadata are durable.
An ambiguous SDK response remains pending until explicitly reconciled.
"""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from tempfile import mkstemp

from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked


class ProbeLedger:
    def __init__(self, path: Path, lifecycle):
        self.path = Path(path)
        self.lifecycle = lifecycle
        self.config_digest = hashlib.sha256(json.dumps({
            "workload": lifecycle.config.workload_name,
            "provider": lifecycle.config.provider_name,
            "return_url": lifecycle.config.return_url,
            "scopes": lifecycle.config.scopes,
            "resource": lifecycle.config.resource,
            "issuer": lifecycle.config.issuer,
            "metadata_sha256": lifecycle.config.metadata_sha256,
        }, sort_keys=True).encode()).hexdigest()
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError("The probe ledger directory must be private (0700)")
        if self.path.is_symlink():
            raise ValueError("The probe ledger cannot be a symlink")

    @contextmanager
    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        handle = os.open(str(self.path) + ".lock", flags, 0o600)
        try:
            if os.fstat(handle).st_mode & 0o077:
                raise ValueError("The probe lock must be private (0600)")
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("The probe ledger cannot be a symlink")
        try:
            handle = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(handle) as file:
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise ValueError("The probe ledger must be private (0600)")
            record = json.load(file)
        if record.get("config_digest") != self.config_digest:
            raise ProbeBlocked("A different Identity configuration owns this attempt")
        return record

    def _save(self, record):
        handle, temporary = mkstemp(dir=self.path.parent, prefix=".identity-attempt-")
        try:
            with os.fdopen(handle, "w") as file:
                json.dump(record, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _require(self, owner, *statuses, tenant=None):
        record = self._read()
        if record is None or record["owner"] != owner or record.get("tenant") != tenant or record["status"] not in statuses:
            raise ProbeBlocked("The owner or attempt phase does not authorize this action")
        return record

    def begin(self, owner: str, tenant: str | None = None) -> dict:
        if not owner or (tenant is not None and (not isinstance(tenant, str) or not tenant)):
            raise ValueError("A consenting owner and valid tenant are required")
        with self._locked():
            if self._read() is not None:
                raise ProbeBlocked("An attempt already exists; reconcile it before starting another")
            state = token_urlsafe(32)
            record = {
                "owner": owner,
                "config_digest": self.config_digest,
                "state_digest": hashlib.sha256(state.encode()).hexdigest(),
                "status": "starting",
            }
            if tenant is not None:
                record["tenant"] = tenant
            self._save(record)
            response = self.lifecycle.start(owner, state)
            record.update(status="consent_pending", session_uri=response["session_uri"])
            self._save(record)
            return {"state": state, "authorization_url": response["authorization_url"]}

    def complete(self, owner: str, state: str, *, tenant: str | None = None) -> None:
        with self._locked():
            record = self._require(owner, "consent_pending", tenant=tenant)
            if not isinstance(state, str) or not compare_digest(record["state_digest"], hashlib.sha256(state.encode()).hexdigest()):
                raise ProbeBlocked("The consent return state does not match this attempt")
            record["status"] = "completing"
            self._save(record)
            self.lifecycle.complete(owner, record["session_uri"])
            record["status"] = "completed"
            self._save(record)

    def observe(self, owner: str, dispatch, *, tenant: str | None = None):
        with self._locked():
            self._require(owner, "completed", tenant=tenant)
            return self.lifecycle.with_access_token(owner, dispatch)

    def deny(self, owner: str, *, tenant: str | None = None) -> None:
        with self._locked():
            record = self._require(owner, "consent_pending", "completing", "completed", tenant=tenant)
            record["status"] = "cleanup_pending"
            self._save(record)

    def status(self, owner: str, *, tenant: str | None = None) -> str:
        with self._locked():
            return self._require(owner, "starting", "consent_pending", "completing", "completed", "cleanup_pending", tenant=tenant)["status"]
