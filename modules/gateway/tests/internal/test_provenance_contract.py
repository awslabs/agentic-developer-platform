"""Cross-module contract test for POST /internal/v1/provenance.

Issue #4029. Every worker provenance write 422'd for months because the worker
client and this endpoint disagreed about two fields, and neither side's tests
exercised the other's shape: the worker suite asserted the worker's assumption,
this suite asserted the gateway's, and both were green.

This test closes that gap by validating the SAME golden fixture the worker and
TypeScript suites build their payloads from:

    contracts/provenance/v1/create-provenance-request.golden.json

If the gateway schema tightens, this test fails until the fixture is updated; if
the fixture is updated, the worker/TS builder tests fail until they follow. There
is deliberately ONE artifact — two independent fixtures is how the drift happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.internal.provenance_routes import CreateProvenanceRequest, CreateProvenanceResponse

# ---------------------------------------------------------------------------
# Shared golden fixture
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PATH = _REPO_ROOT / "contracts" / "provenance" / "v1" / "create-provenance-request.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)

GOLDEN_REQUEST = GOLDEN["request"]
GOLDEN_RESPONSE = {k: v for k, v in GOLDEN["response"].items() if not k.startswith("$")}


def _variant(spec: dict) -> dict:
    """Apply a rejected-variant spec to the golden request."""
    body = dict(GOLDEN_REQUEST)
    body.update(spec.get("patch", {}))
    for key in spec.get("unset", []):
        body.pop(key, None)
    return body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGoldenRequestContract:
    def test_golden_fixture_exists(self):
        """A missing fixture must fail loudly, not skip the contract silently."""
        assert GOLDEN_PATH.is_file(), f"golden contract fixture not found at {GOLDEN_PATH}"

    def test_worker_payload_validates_against_schema(self):
        """THE contract assertion: the body the worker builds is accepted here.

        This is the test that would have caught #4029 on the day it was written.
        """
        req = CreateProvenanceRequest.model_validate(GOLDEN_REQUEST)

        # Spot-check the two fields that caused the 422, with their real types.
        assert isinstance(req.source_event, dict)
        assert isinstance(req.org_id, str) and req.org_id

    def test_schema_field_names_match_fixture_exactly(self):
        """No field may exist on one side and not the other."""
        assert set(CreateProvenanceRequest.model_fields) == set(GOLDEN_REQUEST)

    def test_response_fixture_matches_response_model(self):
        """The fixture's documented response is what the endpoint actually returns.

        Guards the latent third defect: clients used to read 'provenance_id',
        which this model has never produced.
        """
        resp = CreateProvenanceResponse.model_validate(GOLDEN_RESPONSE)
        assert resp.id == GOLDEN_RESPONSE["id"]
        assert set(CreateProvenanceResponse.model_fields) == {"id", "created_at"}
        assert "provenance_id" not in CreateProvenanceResponse.model_fields


class TestRejectedVariants:
    """Each variant is a shape that MUST 422, so a revert cannot pass silently."""

    @pytest.mark.parametrize("spec", GOLDEN["rejected_variants"], ids=lambda s: s["name"])
    def test_variant_is_rejected(self, spec):
        with pytest.raises(ValidationError):
            CreateProvenanceRequest.model_validate(_variant(spec))

    def test_string_source_event_is_rejected(self):
        """Explicit regression guard for the primary #4029 defect.

        Named separately from the parametrized sweep so the intent survives even
        if someone edits the fixture's variant list.
        """
        body = dict(GOLDEN_REQUEST)
        body["source_event"] = "worker:entrypoint"
        with pytest.raises(ValidationError):
            CreateProvenanceRequest.model_validate(body)

    def test_null_org_id_is_rejected(self):
        """Explicit regression guard for the second, independent #4029 defect.

        org_id is VARCHAR(255) NOT NULL (migration 016), so it must be rejected
        at the schema boundary — loosening it would trade a clean 422 for a 500.
        """
        body = dict(GOLDEN_REQUEST)
        body["org_id"] = None
        with pytest.raises(ValidationError):
            CreateProvenanceRequest.model_validate(body)
