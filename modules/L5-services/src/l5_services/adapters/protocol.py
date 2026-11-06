"""Typed boundary: provider adapters never own worker state or queue delivery."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AdapterOutcome:
    classification: str
    native_job_ref: dict | None = None
    observation: dict | None = None
    result: dict | None = None


class AssessmentAdapter(Protocol):
    async def submit(self, run, manifest: dict, attempt, session) -> AdapterOutcome: ...

    async def reconcile_submit(self, run, attempt, session) -> AdapterOutcome: ...

    async def observe(self, run, native_job_ref: dict, attempt, session) -> AdapterOutcome: ...

    async def collect(self, run, native_job_ref: dict, attempt, session) -> AdapterOutcome: ...

    async def cancel(self, run, native_job_ref: dict, attempt, session) -> AdapterOutcome: ...
