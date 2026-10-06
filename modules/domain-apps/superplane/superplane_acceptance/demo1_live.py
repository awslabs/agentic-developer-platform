"""Private Demo 1 live preflight and crash-safe original-request checkpoint."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Self

from .demo1_browser import CreationCheckpoint, checked_origin
from .demo1_evidence import DemoInput, EvidenceError, digest, identifier, instant, text
from .demo1_runtime import RuntimeTarget

CHECKPOINT_VERSION = "demo1-checkpoint-v2"


@dataclass(frozen=True)
class LiveEnvelope:
    origin: str
    broker_label: str
    authority_ref: str
    max_runtime_seconds: int
    cleanup_deadline: datetime
    runtime_target: RuntimeTarget | None = None

    @classmethod
    def parse(
        cls, value: object, selected: DemoInput, *, now: datetime | None = None
    ) -> LiveEnvelope:
        expected = {
            "version",
            "origin",
            "broker_label",
            "authority_ref",
            "release_source",
            "image_digest",
            "schema_revision",
            "connection_id",
            "role",
            "account",
            "region",
            "org_id",
            "requester_id",
            "approver_id",
            "request_id",
            "plan_revision",
            "budget_usd",
            "authorized_at",
            "deadline",
            "cleanup_owner",
            "cleanup_deadline",
            "max_runtime_seconds",
            "recovery_checkpoint",
        }
        if isinstance(value, dict) and value.get("version") == "demo1-live-v2":
            expected.add("runtime_target")
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["version"] not in ("demo1-live-v1", "demo1-live-v2")
        ):
            raise EvidenceError("live: versioned private authority envelope required")
        bindings = {
            "release_source": selected.release_source,
            "image_digest": selected.image_digest,
            "schema_revision": selected.schema_revision,
            "connection_id": selected.connection_id,
            "role": selected.role,
            "account": selected.account,
            "region": selected.region,
            "org_id": selected.org_id,
            "requester_id": selected.requester_id,
            "approver_id": selected.approver_id,
            "request_id": selected.request_id,
            "plan_revision": selected.plan_revision,
            "budget_usd": str(selected.budget_usd),
            "authorized_at": selected.authorized_at.isoformat(),
            "deadline": selected.deadline.isoformat(),
            "cleanup_owner": selected.cleanup_owner,
            "recovery_checkpoint": selected.recovery_checkpoint,
        }
        if any(value[key] != binding for key, binding in bindings.items()):
            raise EvidenceError("live: authority differs from exact private selection")
        now = now or datetime.now(UTC)
        if not selected.authorized_at <= now < selected.deadline:
            raise EvidenceError("live: authorization expired or not yet effective")
        runtime = value["max_runtime_seconds"]
        if (
            type(runtime) is not int
            or not 0 < runtime <= 14_400
            or selected.deadline - now < timedelta(seconds=runtime)
        ):
            raise EvidenceError("live: finite runtime must fit the remaining deadline")
        cleanup = instant(value["cleanup_deadline"], "cleanup deadline")
        if not selected.deadline <= cleanup <= selected.deadline + timedelta(hours=24):
            raise EvidenceError("live: finite cleanup deadline required")
        label = text(value["broker_label"], "broker label")
        if (
            not label.isascii()
            or not all(character.isalnum() or character in "._-" for character in label)
            or not label[0].isalnum()
        ):
            raise EvidenceError("live: invalid broker label")
        if not isinstance(value["origin"], str):
            raise EvidenceError("live: exact HTTPS origin required")
        return cls(
            checked_origin(value["origin"]),
            label,
            identifier(value["authority_ref"], "authority_ref"),
            runtime,
            cleanup,
            RuntimeTarget.parse(value["runtime_target"])
            if value["version"] == "demo1-live-v2"
            else None,
        )


def validate_browser_state(value: object, origin: str) -> None:
    from .demo1_session import browser_state_parts

    browser_state_parts(value, origin)


class PrivateCheckpoint:
    """One cooperating process per private checkpoint, with atomic durable replacement."""

    state_type = CreationCheckpoint

    def __init__(
        self,
        path: str,
        selected: DemoInput,
        origin: str,
        *,
        envelope: LiveEnvelope | None = None,
    ):
        self.path = Path(path)
        if (
            not self.path.is_absolute()
            or not self.path.parent.is_dir()
            or self.path.parent.is_symlink()
        ):
            raise EvidenceError("checkpoint: absolute private path required")
        parent = self.path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise EvidenceError("checkpoint: owner-only directory required")
        self.selected = selected
        binding = {
            **asdict(selected),
            "origin": checked_origin(origin),
            "budget_usd": str(selected.budget_usd),
            "authorized_at": selected.authorized_at.isoformat(),
            "deadline": selected.deadline.isoformat(),
        }
        self.version = CHECKPOINT_VERSION
        if envelope is not None:
            if envelope.origin != origin or envelope.runtime_target is None:
                raise EvidenceError("checkpoint: execution runtime selection required")
            self.version = "demo1-checkpoint-v3"
            binding["execution"] = {
                **asdict(envelope),
                "cleanup_deadline": envelope.cleanup_deadline.isoformat(),
            }
        self.scope = sha256(
            json.dumps(
                binding, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.lock_fd: int | None = None

    def __enter__(self) -> Self:
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                raise EvidenceError("checkpoint: private lock file required")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = descriptor
        except (OSError, EvidenceError):
            if "descriptor" in locals():
                os.close(descriptor)
            raise EvidenceError("checkpoint: another runner or unsafe lock") from None
        return self

    def __exit__(self, *_unused) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def _validate(self, state: CreationCheckpoint) -> None:
        if (
            not isinstance(state, CreationCheckpoint)
            or type(state.submitted) is not bool
        ):
            raise EvidenceError("checkpoint: invalid saved state")
        for name in (
            "request_id",
            "workspace_id",
            "approval_id",
            "retirement_request_id",
        ):
            identifier(getattr(state, name), "checkpoint identity")
        digest(state.plan_revision, "checkpoint plan")
        if (
            state.request_id != self.selected.request_id
            or state.plan_revision != self.selected.plan_revision
            or len(
                {
                    state.request_id,
                    state.workspace_id,
                    state.approval_id,
                    state.retirement_request_id,
                }
            )
            != 4
        ):
            raise EvidenceError("checkpoint: original request lineage differs")

    def load(self) -> CreationCheckpoint | None:
        if self.lock_fd is None:
            raise EvidenceError("checkpoint: lock required")
        if not self.path.exists() and not self.path.is_symlink():
            return None
        from .demo1_cli import _read_private

        value = _read_private(str(self.path))
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "scope", "checkpoint"}
            or value["version"] != self.version
            or value["scope"] != self.scope
        ):
            raise EvidenceError("checkpoint: private selection differs")
        saved = value["checkpoint"]
        if (
            not isinstance(saved, dict)
            or set(saved) != set(self.state_type.__dataclass_fields__)
            or type(saved["submitted"]) is not bool
        ):
            raise EvidenceError("checkpoint: invalid saved state")
        state = self.state_type(**saved)
        self._validate(state)
        return state

    def save(self, checkpoint: CreationCheckpoint) -> None:
        if self.lock_fd is None:
            raise EvidenceError("checkpoint: lock required")
        previous = self.load()
        if previous and (
            {**asdict(previous), "submitted": checkpoint.submitted}
            != asdict(checkpoint)
            or (previous.submitted and not checkpoint.submitted)
        ):
            raise EvidenceError("checkpoint: request replay or identity change refused")
        self._validate(checkpoint)
        temporary = None
        try:
            descriptor, filename = tempfile.mkstemp(
                prefix=".demo1-checkpoint-", dir=self.path.parent
            )
            temporary = Path(filename)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "version": self.version,
                        "scope": self.scope,
                        "checkpoint": asdict(checkpoint),
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise EvidenceError(
                "checkpoint: private write failed; retain original request"
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def preflight_report(
    selected: DemoInput, envelope: LiveEnvelope, session: dict
) -> dict:
    validate_browser_state(session, envelope.origin)
    from .demo1_report import reference

    return {
        "version": "demo1-live-preflight-v1",
        "evidence_mode": "live-selection-unverified",
        "live_acceptance": False,
        "status": "BLOCKED",
        "reason": "released runtime identity and positive retirement capability not independently verified; no effects",
        "source_ref": reference(selected.release_source),
        "image_ref": reference(selected.image_digest),
        "schema_ref": reference(selected.schema_revision),
        "request_ref": reference(selected.request_id),
        "authority_ref": reference(envelope.authority_ref),
        "criteria": {
            "AC-01": "NOT RUN",
            "AC-02": "BLOCKED",
            "AC-03": "NOT RUN",
            "AC-04": "NOT RUN",
        },
    }
