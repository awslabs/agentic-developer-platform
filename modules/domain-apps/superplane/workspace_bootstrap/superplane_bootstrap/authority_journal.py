"""Durable, reservation-fenced temporary bootstrap authority.

Intent commits precede provider calls. The reservation lock stays held while a
call is in flight, so recovery and a successor cannot race a running mutation.
An interrupted call leaves its intent for observation/revocation on restart.
The journal is domain-owned and never contains credentials or bearer tokens.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256

from .errors import BootstrapRefused
from .registry import _SELECT_FOR_UPDATE, RESERVED, registration_lock
from .state import claim_fingerprint
from .target import _binding_identity

_READ = """
SELECT operation_id, org_id, cluster_arn, claim, plan_json, progress_json
FROM workspace_bootstrap_authority
WHERE workspace_id = :workspace_id AND generation = :generation
FOR UPDATE
"""
_PENDING = """
SELECT generation FROM workspace_bootstrap_authority
WHERE workspace_id = :workspace_id AND revoked = false
"""
_INSERT = """
INSERT INTO workspace_bootstrap_authority
(workspace_id, generation, operation_id, org_id, cluster_arn, claim, plan_json,
 progress_json, revoked)
VALUES (:workspace_id, :generation, :operation_id, :org_id, :cluster_arn, :claim,
 :plan_json, '{}', false)
"""
_UPDATE = """
UPDATE workspace_bootstrap_authority SET progress_json = :progress_json,
 revoked = :revoked
WHERE workspace_id = :workspace_id AND generation = :generation
"""


def generation_for(binding, reservation):
    """An attempt generation, derived from an actual exclusive database claim."""
    _binding_identity(binding)
    if not reservation.attempt_token:
        raise BootstrapRefused("temporary authority requires an active reservation")
    return sha256(
        (
            binding.operation_id + ":" + claim_fingerprint(reservation.attempt_token)
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class AuthorityJournal:
    store: object
    binding: object
    target: object
    generation: str
    claim: str
    original_allocation_id: str | None = None

    @property
    def key(self):
        return {"workspace_id": self.target.workspace_id, "generation": self.generation}

    def _validate_binding(self, *, recovery=False):
        if recovery:
            # Expiry prevents new grants, not removal of this operation's grants.
            # Provenance still belongs to the trusted service composition.
            from superplane_contracts.provisioning import OperationBinding

            if not isinstance(self.binding, OperationBinding):
                raise BootstrapRefused("authority recovery requires OperationBinding")
            identity = (
                self.binding.principal.org_id,
                self.binding.principal.workspace_id,
            )
        else:
            identity = _binding_identity(self.binding)
        if identity != (self.target.org_id, self.target.workspace_id):
            raise BootstrapRefused(
                "temporary authority has a different operation target"
            )

    @contextmanager
    def fenced(self, *, recovery=False):
        """Keep release/finalize/reserve excluded through the external operation."""
        self._validate_binding(recovery=recovery)
        with registration_lock(self.store, self.target.workspace_id):
            rows = self.store.execute(_SELECT_FOR_UPDATE, self.key)
            if (
                len(rows) != 1
                or rows[0]["state"] != RESERVED
                or claim_fingerprint(rows[0]["attempt_token"]) != self.claim
            ):
                raise BootstrapRefused("temporary authority generation is stale")
            identity = json.loads(rows[0]["identity_json"])
            for name in ("workspace_id", "org_id", "cluster_arn"):
                if identity.get(name) != getattr(self.target, name):
                    raise BootstrapRefused(
                        "temporary authority reservation target differs"
                    )
            yield

    def begin(self, plan):
        """Persist the complete immutable grant plan before the first grant."""
        encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        with self.fenced():
            rows = self.store.execute(_READ, self.key)
            if rows:
                row = self._checked(rows[0])
                if row["plan_json"] != encoded:
                    raise BootstrapRefused(
                        "temporary authority plan changed on restart"
                    )
                return False
            if self.store.execute(_PENDING, self.key):
                raise BootstrapRefused("temporary authority recovery is outstanding")
            self.store.execute(
                _INSERT,
                {
                    **self.key,
                    "operation_id": self.binding.operation_id,
                    "org_id": self.target.org_id,
                    "cluster_arn": self.target.cluster_arn,
                    "claim": self.claim,
                    "plan_json": encoded,
                },
            )
            self.write_locked({"phase": "acquiring"})
            return True

    def _checked(self, row):
        expected = {
            "operation_id": self.binding.operation_id,
            "org_id": self.target.org_id,
            "cluster_arn": self.target.cluster_arn,
            "claim": self.claim,
        }
        if any(row.get(k) != v for k, v in expected.items()):
            raise BootstrapRefused("temporary authority journal binding differs")
        return row

    def read(self):
        with self.fenced(recovery=True):
            return self.read_locked()

    def read_locked(self):
        """Read while the caller holds fenced(), including operation provenance."""
        rows = self.store.execute(_READ, self.key)
        if len(rows) != 1:
            raise BootstrapRefused("temporary authority journal is missing")
        row = self._checked(rows[0])
        return json.loads(row["plan_json"]), json.loads(row["progress_json"])

    def write_locked(self, progress):
        """Commit progress atomically with a fenced provider result."""
        self.store.execute(
            _UPDATE,
            {
                **self.key,
                "progress_json": json.dumps(progress, sort_keys=True),
                "revoked": progress.get("phase") == "revoked",
            },
        )

    def update(self, name, value, *, recovery=False, revoked=False):
        with self.fenced(recovery=recovery):
            _, progress = self.read_locked()
            progress[name] = value
            if revoked:
                progress["phase"] = "revoked"
            self.write_locked(progress)
