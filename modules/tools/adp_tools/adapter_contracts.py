"""Token-free wire contracts for the separate tool-adapter service.

These shapes carry references and signed invocation proofs, never OAuth access
or refresh tokens. Schema validation is not authorization: core admission and
dispatch must re-resolve the signed owner and consume the durable receipt.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.0"
MAX_WORK_MILLISECONDS = 20_000
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reference = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
Generation = Annotated[int, Field(strict=True, ge=1)]
Timestamp = Annotated[int, Field(strict=True, ge=1)]
Connector = Literal["atlassian", "github", "slack", "jira"]
Purpose = Literal["tool_call", "connection_observe", "policy_observe", "reconcile"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class AdapterRelease(WireModel):
    contract_version: Literal["1.0"]
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    group: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    group_generation: Generation
    backend_generation: Generation


class OperationBinding(WireModel):
    operation_id: Reference
    receipt_id: Reference
    payload_sha256: Digest
    release: AdapterRelease


class AuthorizeRequest(WireModel):
    schema_version: Literal["1.0"]
    binding: OperationBinding
    purpose: Purpose
    connector: Connector
    operation: Reference
    signed_assertion: str = Field(min_length=1, max_length=16_384, repr=False)
    deadline_at_ms: Timestamp


class IdentityReference(WireModel):
    """Only the core's resolved Identity references; no caller-selected token."""

    workload: Reference
    provider: Reference
    canonical_user_id: Reference


class LegacyBinding(WireModel):
    provider_account_id: Reference
    observed_scopes: tuple[Reference, ...] = Field(max_length=256)
    resources_sha256: Digest | None = None
    app_id: Reference | None = None
    site_scopes: tuple[Reference, ...] = Field(default=(), max_length=256)


class AuthorizedContext(WireModel):
    tenant_id: Reference
    canonical_user_id: Reference
    connection_id: Reference | None = None
    binding_revision: Reference | None = None
    site_id: Reference | None = None
    scopes: tuple[Reference, ...] = Field(max_length=256)
    legacy: LegacyBinding | None = None


class Admission(WireModel):
    schema_version: Literal["1.0"]
    admission_id: Reference
    binding: OperationBinding
    purpose: Purpose
    connector: Connector
    operation: Reference
    identity: IdentityReference
    context: AuthorizedContext
    issued_at_ms: Timestamp
    expires_at_ms: Timestamp
    deadline_at_ms: Timestamp

    @model_validator(mode="after")
    def bounded_authority(self):
        if not self.issued_at_ms < self.expires_at_ms <= self.deadline_at_ms:
            raise ValueError("Admission must expire within the signed operation deadline")
        if self.expires_at_ms - self.issued_at_ms > MAX_WORK_MILLISECONDS:
            raise ValueError("Admission exceeds the shared operation budget")
        if self.identity.canonical_user_id != self.context.canonical_user_id:
            raise ValueError("Identity and canonical owner differ")
        if self.purpose != "connection_observe" and (
            self.context.connection_id is None or self.context.binding_revision is None
        ):
            raise ValueError("Tool and reconciliation admission require a current connection")
        if (
            self.purpose == "tool_call"
            and self.connector in {"github", "slack", "jira"}
            and self.context.legacy is None
        ):
            raise ValueError("Legacy tool admission requires current provider binding")
        if self.connector == "atlassian" and self.context.legacy is not None:
            raise ValueError("Atlassian cannot carry legacy provider authority")
        return self


class DispatchRequest(WireModel):
    schema_version: Literal["1.0"]
    admission_id: Reference
    binding: OperationBinding
    binding_revision: Reference


class DispatchReceipt(WireModel):
    """Core receipt consumption, not a claim that the provider effect finished."""

    schema_version: Literal["1.0"]
    admission_id: Reference
    binding: OperationBinding
    binding_revision: Reference
    consumed_at_ms: Timestamp
    deadline_at_ms: Timestamp

    @model_validator(mode="after")
    def live_dispatch(self):
        if not self.consumed_at_ms < self.deadline_at_ms:
            raise ValueError("Dispatch authority has expired")
        return self


class ObserveRequest(WireModel):
    schema_version: Literal["1.0"]
    purpose: Literal["connection_observe"]
    binding: OperationBinding
    connector: Connector
    attempt_id: Reference
    config_revision: Digest
    signed_assertion: str = Field(min_length=1, max_length=16_384, repr=False)
    deadline_at_ms: Timestamp


class ObservedSite(WireModel):
    site_id: Reference
    # A reference is sufficient for core binding. The adapter never returns a
    # provider-controlled URL that another component could mistake for authority.
    scopes: tuple[Reference, ...] = Field(max_length=256)


class PolicyObserveRequest(WireModel):
    schema_version: Literal["1.0"]
    purpose: Literal["policy_observe"]
    binding: OperationBinding
    connector: Literal["atlassian"]
    connection_id: Reference
    binding_revision: Reference
    site_id: Reference
    config_revision: Digest
    probes: dict[str, dict]
    expected_results: dict[str, Digest]
    signed_assertion: str = Field(min_length=1, max_length=16_384, repr=False)
    deadline_at_ms: Timestamp

    @model_validator(mode="after")
    def bounded_reads(self):
        if (
            set(self.probes)
            != {
                "get_jira_issue",
                "search_jira",
                "get_confluence_page",
                "search_confluence",
            }
            or set(self.expected_results) != set(self.probes)
            or len(json.dumps(self.probes).encode()) > 8192
            or any(arguments.get("cloudId") != self.site_id for arguments in self.probes.values())
        ):
            raise ValueError("Four site-bound read-only policy probes are required")
        return self


class ConnectionObservation(WireModel):
    schema_version: Literal["1.0"]
    purpose: Literal["connection_observe"]
    binding: OperationBinding
    attempt_id: Reference
    config_revision: Digest
    provider_user_id: Reference
    sites: tuple[ObservedSite, ...] = Field(max_length=1000)
    granted_scopes: tuple[Reference, ...] = Field(max_length=256)
    observed_at_ms: Timestamp


class AdapterFailure(WireModel):
    schema_version: Literal["1.0"]
    code: Literal[
        "adapter_unavailable",
        "authority_unavailable",
        "identity_unavailable",
        "adapter_contract_mismatch",
    ]
    retryable: bool = Field(strict=True)


WIRE_TYPES = (
    LegacyBinding,
    AuthorizeRequest,
    Admission,
    DispatchRequest,
    DispatchReceipt,
    ObserveRequest,
    PolicyObserveRequest,
    ConnectionObservation,
    AdapterFailure,
)


def contract_metadata() -> dict:
    """Non-user contract identity; contains no configured owners or providers."""
    schemas = {model.__name__: model.model_json_schema() for model in WIRE_TYPES}
    encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {
        "contract_version": CONTRACT_VERSION,
        "schema_sha256": hashlib.sha256(encoded).hexdigest(),
    }
