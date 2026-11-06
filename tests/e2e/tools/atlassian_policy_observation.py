"""Observe effective MCP read/search policy through an existing Identity grant.

The documented denial is from Atlassian's permission boundary:
https://support.atlassian.com/security-and-access-policies/docs/configure-atlassian-mcp-server-permissions/

This is not an administrator-action audit or an OAuth redirect-domain probe.
A successful read alone never proves that an administrator changed a policy.
Only a same-binding denial followed by a fresh, independently verified read
establishes an effective permission transition. No token or provider body is
retained, and the caller still owns scenario acceptance and fixture cleanup.
"""

import fcntl
import hashlib
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from tests.e2e.tools.atlassian_flow_summary import _decode, canonical
from tests.e2e.tools.atlassian_readback_grant import (
    IdentityReadbackGrant,
    MCPReadSession,
    TOOLS,
)

MAX_EVENTS = 32
MAX_BYTES = 65536
FRESH_SECONDS = 300
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9:_-]{1,255}\Z")
BINDING_FIELDS = {
    "run_id",
    "tenant_id",
    "owner_id",
    "site_id",
    "provider_account_id",
    "client_sha256",
    "configuration_sha256",
    "grant_configuration_sha256",
    "permission",
    "probe_sha256",
}
EVENT_FIELDS = {"id", "started_at", "completed_at", "status", "response_sha256"}


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def binding_for(
    grant,
    *,
    run_id,
    tenant_id,
    site_id,
    client_sha256,
    configuration_sha256,
    permission,
    path,
    expected_result_sha256,
):
    """Pin the existing grant, observed registered client and exact read expectation."""
    if (
        type(grant) is not IdentityReadbackGrant
        or site_id not in grant.config["site_ids"]
        or permission not in {"read", "search"}
        or not isinstance(path, str)
        or not path.startswith("/ex/")
        or len(path.encode()) > 8192
        or not isinstance(expected_result_sha256, str)
        or not DIGEST.fullmatch(expected_result_sha256)
    ):
        raise ValueError("An existing owner grant and exact read probe are required")
    binding = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "owner_id": grant.owner_id,
        "site_id": site_id,
        "provider_account_id": grant.config["provider_account_id"],
        "client_sha256": client_sha256,
        "configuration_sha256": configuration_sha256,
        "grant_configuration_sha256": digest(grant.config),
        "permission": permission,
        "probe_sha256": digest({"path": path, "result_sha256": expected_result_sha256}),
    }
    validate_binding(binding)
    return binding


def validate_binding(binding):
    if (
        type(binding) is not dict
        or set(binding) != BINDING_FIELDS
        or binding["permission"] not in {"read", "search"}
        or any(
            not isinstance(value, str)
            or not (DIGEST if key.endswith("_sha256") else IDENTIFIER).fullmatch(value)
            for key, value in binding.items()
            if key != "permission"
        )
    ):
        raise ValueError("Policy observation needs one complete protected binding")


def policy_denied(reply, permission):
    """Classify only the documented provider error, never arbitrary HTTP failures."""
    expected = f"Access denied: Your organization admin has not authorized the {permission} permission."
    if type(reply) is not dict:
        return False
    if "error" in reply:
        error = reply["error"]
        return (
            "result" not in reply
            and type(error) is dict
            and error.get("message") == expected
        )
    result = reply.get("result")
    if (
        type(result) is not dict
        or result.get("isError") is not True
        or "structuredContent" in result
    ):
        return False
    content = result.get("content")
    return (
        type(content) is list
        and len(content) == 1
        and type(content[0]) is dict
        and content[0].get("type") == "text"
        and content[0].get("text") == expected
    )


class PolicyObservationLedger:
    """Private crash-safe history of read probes; no authorization or retry authority."""

    def __init__(self, path, binding, *, clock=time.time):
        validate_binding(binding)
        self.path, self.binding, self.clock = (
            Path(path),
            _decode(canonical(binding)),
            clock,
        )
        parent = self.path.parent.lstat()
        if (
            not self.path.is_absolute()
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError("Policy observations require a private owned directory")

    @staticmethod
    def _owned(metadata):
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o077 == 0
            and metadata.st_nlink == 1
        )

    def _now(self):
        now = int(self.clock())
        if now <= 0:
            raise ValueError("Policy observation clock unavailable")
        return now

    @contextmanager
    def _locked(self):
        descriptor = os.open(
            str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            if not self._owned(os.fstat(descriptor)):
                raise ValueError("Policy observation lock is not private")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"version": 1, "binding": self.binding, "events": []}
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not self._owned(metadata) or metadata.st_size > MAX_BYTES:
                raise ValueError("Policy observation history is not private or bounded")
            state = _decode(stream.read(MAX_BYTES + 1))
        if (
            type(state) is not dict
            or set(state) != {"version", "binding", "events"}
            or type(state["version"]) is not int
            or state["version"] != 1
            or state["binding"] != self.binding
            or type(state["events"]) is not list
            or len(state["events"]) > MAX_EVENTS
        ):
            raise ValueError("Policy observation belongs to another protected binding")
        ids, previous = set(), 0
        for event in state["events"]:
            if (
                type(event) is not dict
                or set(event) != EVENT_FIELDS
                or not isinstance(event["id"], str)
                or not IDENTIFIER.fullmatch(event["id"])
                or event["id"] in ids
                or type(event["started_at"]) is not int
                or event["started_at"] < previous
                or event["status"]
                not in {"pending", "unavailable", "policy_denied", "allowed"}
                or event["status"] == "pending"
                and (
                    event["completed_at"] is not None
                    or event["response_sha256"] is not None
                )
                or event["status"] != "pending"
                and (
                    type(event["completed_at"]) is not int
                    or event["completed_at"] < event["started_at"]
                )
                or event["response_sha256"] is not None
                and (
                    not isinstance(event["response_sha256"], str)
                    or not DIGEST.fullmatch(event["response_sha256"])
                )
                or event["status"] in {"allowed", "policy_denied"}
                and event["response_sha256"] is None
            ):
                raise ValueError("Policy observation history is invalid")
            ids.add(event["id"])
            previous = event["started_at"]
        return state

    def _write(self, state):
        encoded = canonical(state)
        if len(encoded) > MAX_BYTES:
            raise ValueError("Policy observation history exceeds its bound")
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".policy-"
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _begin(self):
        with self._locked():
            state, now = self._read(), self._now()
            if len(state["events"]) >= MAX_EVENTS:
                raise ValueError("Policy probe allowance exhausted")
            if state["events"] and now < state["events"][-1]["started_at"]:
                raise ValueError("Policy observation clock moved backwards")
            # Reads have no mutation effect. Recover interrupted reads as unknown
            # before a separately recorded new probe; never promote them to success.
            for event in state["events"]:
                if event["status"] == "pending":
                    event.update(status="unavailable", completed_at=now)
            identifier = str(uuid4())
            state["events"].append(
                {
                    "id": identifier,
                    "started_at": now,
                    "completed_at": None,
                    "status": "pending",
                    "response_sha256": None,
                }
            )
            self._write(state)
            return identifier

    def _finish(self, identifier, *, reply, verified_read):
        with self._locked():
            state, now = self._read(), self._now()
            event = state["events"][-1]
            if (
                event["id"] != identifier
                or event["status"] != "pending"
                or now < event["started_at"]
            ):
                raise ValueError(
                    "Policy read completion differs from the pending observation"
                )
            status = "unavailable"
            if verified_read is True:
                status = "allowed"
            elif policy_denied(reply, self.binding["permission"]):
                status = "policy_denied"
            event.update(
                status=status,
                completed_at=now,
                response_sha256=digest(reply) if reply is not None else None,
            )
            self._write(state)

    def evidence(self):
        with self._locked():
            state, now = self._read(), self._now()
        events = state["events"]
        last = events[-1] if events else None
        fresh = bool(
            last
            and type(last["completed_at"]) is int
            and 0 <= now - last["completed_at"] <= FRESH_SECONDS
        )
        status = (
            last["status"]
            if fresh and last["status"] in {"allowed", "policy_denied"}
            else "unknown"
        )
        denial = next(
            (
                event
                for event in reversed(events[:-1])
                if event["status"] == "policy_denied"
            ),
            None,
        )
        transitioned = status == "allowed" and denial is not None
        return {
            "source": "direct-atlassian-provider-policy",
            "binding_sha256": digest(self.binding),
            "permission": self.binding["permission"],
            "status": status,
            "policy_transition_verified": transitioned,
            "denial_sha256": digest(denial) if transitioned else None,
            "allow_sha256": digest(last) if status == "allowed" else None,
            "history_sha256": digest(state),
        }


def observe_read_policy(grant, ledger, *, path, expected_result_sha256):
    """Run one actual schema-pinned read, capturing errors before normalization.

    The independently prepared fixture supplies expected_result_sha256. A token,
    connection readiness, a caller's success flag, or an arbitrary successful JSON
    response cannot establish effective permission for that fixture.
    """
    if type(ledger) is not PolicyObservationLedger:
        raise ValueError("A private policy observation ledger is required")
    binding = binding_for(
        grant,
        **{
            key: ledger.binding[key]
            for key in (
                "run_id",
                "tenant_id",
                "site_id",
                "client_sha256",
                "configuration_sha256",
                "permission",
            )
        },
        path=path,
        expected_result_sha256=expected_result_sha256,
    )
    if binding != ledger.binding:
        raise ValueError("Policy probe differs from its original protected binding")
    identifier = ledger._begin()
    captured = []
    verified = False

    def observe(session):
        if (
            type(session) is not MCPReadSession
            or session.config != grant.config
            or session.subject != binding["provider_account_id"]
        ):
            raise ValueError("Policy probe received another owner's read session")
        transport = session.transport

        class Capture:
            def send(self, method, params, token):
                permission = (
                    "search" if params.get("name") == TOOLS["search"] else "read"
                )
                if (
                    method != "tools/call"
                    or params.get("name") not in TOOLS.values()
                    or params.get("arguments", {}).get("cloudId") != binding["site_id"]
                    or permission != binding["permission"]
                    or captured
                ):
                    raise ValueError("Policy probe escaped the selected read boundary")
                reply = transport.send(method, params, token)
                if len(canonical(reply)) > MAX_BYTES:
                    raise ValueError("Policy probe response exceeds its bound")
                captured.append(reply)
                return reply

        session.transport = Capture()
        try:
            result = session.get(path)
            if digest(result) != expected_result_sha256:
                raise ValueError(
                    "Policy read differs from the independently prepared fixture"
                )
            return True
        finally:
            session.transport = transport

    try:
        verified = grant.with_access_token(grant.owner_id, observe) is True
    except Exception:
        # Never retain raw exception text, Identity credentials or provider content.
        pass
    ledger._finish(
        identifier,
        reply=captured[0] if captured else None,
        verified_read=verified and len(captured) == 1,
    )
    return ledger.evidence()
