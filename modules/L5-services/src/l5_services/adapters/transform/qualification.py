"""Pinned qualification verdicts and conservative paid-attempt readback."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from l5_services.work_store import AttemptContext


class CapabilityVerdict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["supported", "unsupported", "unqualified"]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    receipt_sha256: Annotated[str, Field(pattern="^[a-f0-9]{64}$")] | None = None


class ProviderQualification(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal["1.0"]
    interface: str | None
    cli_version: str | None
    binary_sha256: Annotated[str, Field(pattern="^[a-f0-9]{64}$")] | None
    image_digest: Annotated[str, Field(pattern="^sha256:[a-f0-9]{64}$")] | None
    definition_revision: str | None
    region_qualification: Literal["unqualified", "qualified"]
    executor_qualification: Literal["unqualified", "qualified"]
    tool_isolation: CapabilityVerdict
    submit: CapabilityVerdict
    read: CapabilityVerdict
    resume: CapabilityVerdict
    cancel: CapabilityVerdict

    @model_validator(mode="after")
    def supported_operations_need_pins_and_receipts(self):
        operations = (self.tool_isolation, self.submit, self.read, self.resume, self.cancel)
        if any(entry.status == "supported" for entry in operations):
            if not all((self.interface, self.cli_version, self.binary_sha256,
                        self.image_digest, self.definition_revision)):
                raise ValueError("supported operations require exact provider pins")
        if any(entry.status == "supported" and entry.receipt_sha256 is None for entry in operations):
            raise ValueError("supported operations require independently retained receipts")
        if any(entry.status == "unqualified" and entry.receipt_sha256 is not None for entry in operations):
            raise ValueError("unqualified operations cannot claim live receipts")
        return self

    @property
    def ready_for_submission(self):
        return (self.region_qualification == "qualified"
                and self.executor_qualification == "qualified"
                and all(entry.status == "supported" for entry in
                        (self.tool_isolation, self.submit, self.read, self.resume)))


class NativeReadback(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    native_id: str
    attempt_token: str
    input_hash: str
    state: Literal["running", "completed", "failed", "stopped", "unknown"]


class RecoveryVerdict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["observed", "outcome_unknown"]
    native_id: str | None = None
    provider_state: Literal["running", "completed", "failed", "stopped", "unknown"]
    replacement_submit_allowed: Literal[False] = False


def reconcile_attempt(attempt: AttemptContext, qualification: ProviderQualification,
                      readback: NativeReadback | None) -> RecoveryVerdict:
    if (not attempt.can_reconcile or qualification.read.status != "supported"
            or readback is None or not readback.native_id
            or readback.attempt_token != attempt.idempotency_token
            or readback.input_hash != attempt.input_hash):
        return RecoveryVerdict(status="outcome_unknown", provider_state="unknown")
    if readback.state == "unknown":
        return RecoveryVerdict(status="outcome_unknown", provider_state="unknown")
    return RecoveryVerdict(status="observed", native_id=readback.native_id,
                           provider_state=readback.state)


def remote_cancel_verdict(qualification: ProviderQualification, recovery: RecoveryVerdict,
                          *, cancel_receipt_native_id: str | None = None) -> bool:
    return (qualification.cancel.status == "supported" and recovery.status == "observed"
            and recovery.provider_state == "stopped" and cancel_receipt_native_id is not None
            and recovery.native_id == cancel_receipt_native_id)
