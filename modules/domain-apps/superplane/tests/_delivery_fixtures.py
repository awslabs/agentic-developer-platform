"""Test doubles and factories for the U10 provider-executor suite.

Issue #5048 (U10), EPIC #4910.

Split out of `test_executor_delivery.py` so that file reads as a list of acceptances
rather than as 200 lines of scaffolding followed by them. The module is
underscore-prefixed for the same reason `_contracts_path.py` and `_release_path.py`
are: it is a helper imported by tests, not a test module pytest should collect.

## Why the doubles are hand-written

A `unittest.mock.Mock` satisfies any attribute access, so a test using one passes
whether the executor calls `fetch_material`, `fetchMaterial`, or a method that no
longer exists — which makes the mock unable to detect the drift it stands in for.
`test_provisioning_adapter.py` records the same reasoning for U17a's facade.

There is a second, sharper reason here. The central acceptance is that delivery is
consumed from B's scoped contract and **not** from a vault credential-management
endpoint or a raw secret read. A double that simply lacks those methods would prove
only that the double lacks them: the executor reaching for one would fail with
`AttributeError`, which a broad `except` upstream could turn into a retry, and the
test would pass for the wrong reason.

So `RecordingChannel` deliberately *offers* the forbidden surface — the management
calls and the raw read a real vault client would expose — and records any use of it.
The acceptance is then asserted positively: those recordings are empty because the
executor never reached for them, not because reaching was impossible.

## These are mocks, and they say so

`is_mock` is True on both doubles. B's scoped trusted-delivery contract does not
exist in ADP, so nothing here integrates against a real channel and no test using
them establishes live delivery (`acceptance-split.md` rules 2 and 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
from superplane_contracts.connections import CredentialReference
from superplane_contracts.delivery import (
    DELIVERY_PERMISSION,
    DeliveryLease,
    ExecutorIdentity,
    RevocationState,
    RunBinding,
    SecretMaterial,
    file_is_executor_only,
)
from superplane_contracts.delivery_executor import DeliveryRequest
from superplane_contracts.provisioning import ResolvedPrincipal

# A fixed, timezone-aware instant, matching `conftest.py`'s OBSERVED_AT discipline:
# these tests assert on validation branches and refusals, never on "now". The clock is
# injected into the executor, so no test patches time.
NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=15)
AFTER_EXPIRY = LATER + timedelta(seconds=1)

# The bound tenant. Deliberately distinct values, so a test asserting "the org came
# from the binding" cannot pass by coincidence against the workspace.
SUBJECT = "principal-abc"
ORG = "org-acme"
WORKSPACE = "ws-w1"
OTHER_WORKSPACE = "ws-w2"

EXECUTOR_ID = "provider-executor-1"
OTHER_EXECUTOR_ID = "provider-executor-2"
OPERATION_ID = "op-01JQ8ZKRW4X7N2VYB3M6E5T9DA"
PROVIDER_ACCOUNT_ID = "123456789012"

# The credential value the default channel hands over. Shaped like an AWS access key
# id on purpose: `secrets.py` recognizes that shape, so the redaction and scrubbing
# assertions exercise a value the guards actually classify as secret rather than an
# innocuous string they would pass through regardless.
#
# Test-only material. It authenticates nothing: it is the example key id from AWS's
# own public documentation, and no live credential appears in this repository.
FAKE_KEY_VALUE = "AKIAIOSFODNN7EXAMPLE"

# What the permitted provider operation reports back. A mapping, because the outcome
# is the provider's observation rather than a status flag.
PROVIDER_OBSERVATION: dict[str, Any] = {"regions": ["us-east-1"]}


def clock() -> datetime:
    """The executor's clock, fixed at `NOW`."""
    return NOW


def expired_clock() -> datetime:
    """A clock past every lease's expiry, for the expiry-refusal branch."""
    return AFTER_EXPIRY


def make_reference(credential_id: str = "cred-1") -> CredentialReference:
    """A vault pointer. Never an ARN and never a value — `CredentialReference` refuses both."""
    return CredentialReference(
        credential_id=credential_id, service="aws", label="delivery-test"
    )


def make_binding(
    *,
    principal_workspace: str = WORKSPACE,
    recipient_id: str = EXECUTOR_ID,
    expires_at: datetime = LATER,
    permission: str = DELIVERY_PERMISSION,
) -> RunBinding:
    """A run binding as B's contract would issue it.

    Returned as the real `RunBinding` rather than a stand-in: the executor refuses
    duck-typed leases, so a test double here would only be testing the double.
    """
    return RunBinding(
        operation_id=OPERATION_ID,
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
        operation="describe_regions",
        principal=ResolvedPrincipal(
            subject=SUBJECT, org_id=ORG, workspace_id=principal_workspace
        ),
        recipient=ExecutorIdentity(executor_id=recipient_id),
        permission=permission,
        expires_at=expires_at,
    )


def make_lease(
    *,
    credential_id: str = "cred-1",
    workspace_id: str = WORKSPACE,
    principal_workspace: str | None = None,
    recipient_id: str = EXECUTOR_ID,
    expires_at: datetime = LATER,
    provenance: dict[str, str] | None = None,
) -> DeliveryLease:
    """A lease as B's scoped trusted-delivery contract would issue it.

    `principal_workspace` defaults to `workspace_id`, so the ordinary case is
    consistent and a test has to *ask* for the cross-workspace mismatch — which is the
    case `DeliveryLease` refuses at construction.
    """
    return DeliveryLease(
        lease_id="lease-01JQ8ZKRW4X7N2VYB3M6E5T9DB",
        reference=make_reference(credential_id),
        workspace_id=workspace_id,
        binding=make_binding(
            principal_workspace=(
                workspace_id if principal_workspace is None else principal_workspace
            ),
            recipient_id=recipient_id,
            expires_at=expires_at,
        ),
        provenance=provenance if provenance is not None else {},
    )


def make_request(parameters: tuple[tuple[str, str], ...] = ()) -> DeliveryRequest:
    """A permitted, read-only provider operation request carrying no identity."""
    return DeliveryRequest(
        provider="aws", operation="describe_regions", parameters=parameters
    )


@dataclass
class RecordingChannel:
    """A stand-in for B's scoped trusted-delivery contract that records every call.

    **This is a mock.** See the module docstring for why it offers the forbidden
    management surface rather than omitting it.
    """

    is_mock: bool = True
    """Asserted by the suite's recorded-mock tests. Not decoration."""

    material: Any = field(default_factory=lambda: SecretMaterial(FAKE_KEY_VALUE))
    """Overridden by one test to a bare `str`, which the executor must refuse."""

    material_by_credential: dict[str, SecretMaterial] = field(default_factory=dict)
    """Set by the rotation test so each credential id hands back distinct material."""

    revocation: Any = field(default_factory=lambda: RevocationState(admits_work=True))
    """Overridden to a denial, and once to a malformed report."""

    calls: list[str] = field(default_factory=list)
    """Permitted calls, in order, so the suite can assert the check ran first."""

    management_calls: list[str] = field(default_factory=list)
    """Any use of the credential-management surface. Must stay empty."""

    raw_reads: list[str] = field(default_factory=list)
    """Any raw read of a stored secret value. Must stay empty."""

    # -- the surface the executor is allowed to use -------------------------

    def revocation_state(self, lease: DeliveryLease) -> Any:
        self.calls.append("revocation_state")
        return self.revocation

    def fetch_material(self, lease: DeliveryLease) -> Any:
        self.calls.append("fetch_material")
        if self.material_by_credential:
            return self.material_by_credential[lease.reference.credential_id]
        return self.material

    def record_delivered(self, lease: DeliveryLease) -> None:
        self.calls.append("record_delivered")

    # -- the surface it must never use, offered so its use is recorded ------

    def register_credential(self, *args: Any, **kwargs: Any) -> None:
        """`POST /auth/credentials`. No run binding, no expiry, no run-tied revocation."""
        self.management_calls.append("register_credential")

    def delete_credential(self, *args: Any, **kwargs: Any) -> None:
        """`DELETE /auth/credentials/{id}`. Credential management, not delivery."""
        self.management_calls.append("delete_credential")

    def read_secret_value(self, *args: Any, **kwargs: Any) -> str:
        """A raw read of the stored value — a credential no revocation reaches."""
        self.raw_reads.append("read_secret_value")
        return FAKE_KEY_VALUE


@dataclass
class RecordingOperation:
    """A permitted read-only provider operation. **This is a mock.**

    Records what it observed about the credential file *while the file existed*: its
    content, and whether it and its directory were owner-only. Those readings have to
    be taken here, inside the materialization window, because the acceptance is about
    the file the provider actually reads — after the window closes the file is gone and
    there is nothing left to inspect.
    """

    is_mock: bool = True
    provider: str = "aws"
    provider_account_id: str = PROVIDER_ACCOUNT_ID
    operation: str = "describe_regions"
    observation: dict[str, Any] = field(
        default_factory=lambda: dict(PROVIDER_OBSERVATION)
    )
    performed: list[dict[str, Any]] = field(default_factory=list)

    def perform(self, credential_path: Path, *, lease: DeliveryLease) -> dict[str, Any]:
        self.performed.append(
            {
                "path": credential_path,
                "content": credential_path.read_text(),
                "executor_only": file_is_executor_only(credential_path),
                "parent_executor_only": file_is_executor_only(credential_path.parent),
                "lease_id": lease.lease_id,
            }
        )
        return dict(self.observation)


@dataclass
class FailingOperation:
    """A provider operation that raises, to prove cleanup is not on the happy path."""

    is_mock: bool = True
    provider: str = "aws"
    provider_account_id: str = PROVIDER_ACCOUNT_ID
    operation: str = "describe_regions"
    path: Path | None = None

    def perform(self, credential_path: Path, *, lease: DeliveryLease) -> dict[str, Any]:
        # Recorded before raising, so the test can assert the file that existed is gone
        # rather than asserting about a path it guessed.
        self.path = credential_path
        raise RuntimeError("provider exploded")


class SpyDatabaseSession:
    """A DB session that records any attempt to touch it, and refuses every use.

    Passed nowhere. That is the point: the acceptance is "no direct domain-DB write",
    and the executor has no field a session could occupy, so this spy stays untouched.
    It exists so the assertion is made against an object that *would* have recorded a
    write, rather than against a comment claiming none happens.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []
        self.commits = 0

    def __getattr__(self, name: str) -> Any:
        # Reached only for attributes not set in __init__, so `touched` and `commits`
        # resolve normally and there is no recursion.
        self.touched.append(name)
        raise AssertionError(
            f"the provider executor must not reach a domain database session "
            f"(attempted {name!r}); domain records are written by the upstream API"
        )
