"""Local transaction-contract checks for armed, provably unsent attempts."""

from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import rfc8785

from src.budget.metering_admission import MeteringAdmission, MeteringAdmissionError, MeteringDispatchPermit
from src.orchestration.provider_quotes import request_digest
from src.proxy.metered_transport import MeteredProvenUnsentError, MeteredUnsentCancelled


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("sql_fails", [False, True])
async def test_armed_proven_unsent_cancellation_commits_and_revokes_permit(cancelled, sql_fails):
    attempt = SimpleNamespace(
        receipt=SimpleNamespace(
            tenant_id="local-tenant", request_id="local-attempt", owner_id="local-owner", version_id="receipt-v1", sha256="a" * 64
        ),
        producer_epoch=1,
        scope_revisions={"local-scope": 1},
        submitted_sha256=request_digest(b"{}"),
        quote_sha256=request_digest(rfc8785.dumps({})),
    )
    submitted, destination = b"{}", "https://local.invalid/openai/v1/responses"
    quote = SimpleNamespace(request_sha256="b" * 64, binds=lambda **kwargs: None)
    attempt.quote = quote
    permit = MeteringDispatchPermit(
        tenant_id="local-tenant",
        request_id="local-attempt",
        revision=3,
        producer_epoch=1,
        quote=quote,
        submitted_sha256=request_digest(submitted),
        destination_sha256=request_digest(destination.encode()),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        monotonic_deadline=monotonic() + 30,
    )
    row = SimpleNamespace(
        state="dispatch_armed",
        owner_id="local-owner",
        request_sha256=quote.request_sha256,
        submitted_sha256=attempt.submitted_sha256,
        quote_snapshot={},
        revision=3,
        producer_epoch=1,
        admission_version="receipt-v1",
        admission_sha256="a" * 64,
        final_version=None,
        reconciliation_status=None,
    )
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalar.side_effect = [object(), row]
    admission = MeteringAdmission.__new__(MeteringAdmission)
    admission._session_factory = lambda: session
    evidence = {
        "admission_sha256": attempt.receipt.sha256,
        "admission_version_id": attempt.receipt.version_id,
        "revision": permit.revision,
        "producer_epoch": permit.producer_epoch,
        "request_sha256": quote.request_sha256,
        "submitted_sha256": attempt.submitted_sha256,
        "quote_sha256": attempt.quote_sha256,
        "destination_sha256": permit.destination_sha256,
    }
    admission._journal = SimpleNamespace(
        write_unsent=AsyncMock(return_value=attempt.receipt),
        read_unsent=AsyncMock(return_value=evidence),
        discover_final=AsyncMock(return_value=None),
    )
    proof = MeteredUnsentCancelled(permit) if cancelled else MeteredProvenUnsentError(permit)

    if sql_fails:
        session.__aenter__.side_effect = TimeoutError("local SQL failure after proof")
        with pytest.raises(TimeoutError, match="after proof"):
            await admission.cancel_armed_unsent(attempt, proof)
        assert row.state == "dispatch_armed"
        session.commit.assert_not_awaited()
    else:
        await admission.cancel_armed_unsent(attempt, proof)
        assert (row.state, row.revision, row.reconciliation_status) == ("cancelled_unsubmitted", 4, "proven_unsubmitted")
        session.commit.assert_awaited_once()
    admission._journal.write_unsent.assert_awaited_once()
    assert permit.proven_unsent()
    with pytest.raises(MeteringAdmissionError, match="already consumed"):
        permit.consume(submitted=submitted, model="local-model", destination=destination, pricing_revision="local")


async def test_armed_cancellation_rejects_ambiguous_write_and_retains_exposure():
    attempt = SimpleNamespace(receipt=SimpleNamespace(tenant_id="local-tenant", request_id="local-attempt"), producer_epoch=1)
    permit = MeteringDispatchPermit(
        tenant_id="local-tenant",
        request_id="local-attempt",
        revision=3,
        producer_epoch=1,
        quote=SimpleNamespace(request_sha256="hash", binds=lambda **kwargs: None),
        submitted_sha256=request_digest(b"{}"),
        destination_sha256=request_digest(b"https://local.invalid"),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        monotonic_deadline=monotonic() + 30,
    )
    permit.consume(submitted=b"{}", model="local-model", destination="https://local.invalid", pricing_revision="local")
    permit.mark_physical_submission()
    session = AsyncMock()
    admission = MeteringAdmission.__new__(MeteringAdmission)
    admission._session_factory = lambda: session
    with pytest.raises(MeteringAdmissionError, match="cannot be proved"):
        await admission.cancel_armed_unsent(attempt, MeteredProvenUnsentError(permit))
    session.__aenter__.assert_not_awaited()
