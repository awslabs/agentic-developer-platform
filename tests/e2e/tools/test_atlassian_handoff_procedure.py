"""Generated SCN-03 review binds two procedures without live promotion."""

import hashlib
import json

import pytest

from tests.e2e.tools.atlassian_catalogue import canonical
from tests.e2e.tools.atlassian_contract import CONTRACT_PATH, load_contract
from tests.e2e.tools.atlassian_handoff import MATRIX_PATH
from tests.e2e.tools.atlassian_oauth_probe import IDENTITY_SCOPES, ProbeBlocked, proof_auth_lifecycle, proof_capability_contract, proof_handoff_review
from tests.e2e.tools.test_atlassian_capability_procedure import DOCUMENTED_METHODS, inputs
from tests.e2e.tools.test_atlassian_catalogue import Pages, ready_ledger
from tests.e2e.tools.test_atlassian_lifecycle_procedure import ConsentFixture, ExpiringIdentity, IndependentOracle
from tests.e2e.tools.test_atlassian_oauth_probe import IdentityFixture
from tests.e2e.tools.test_atlassian_probe_ledger import ledger_for


SOURCE = "a" * 40
CONTRACT_DIGEST = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()


def generated_reports(tmp_path):
    oracle = IndependentOracle()
    ledger = ledger_for(tmp_path / "lifecycle.json", IdentityFixture("core"), ExpiringIdentity(oracle))
    lifecycle = proof_auth_lifecycle(ledger, "owner-a", "owner-b", ConsentFixture(), oracle, oracle, "subject-a", "site-a", source_revision=SOURCE)
    capability_ledger, adapter = ready_ledger(tmp_path)
    contract = load_contract()
    pages = Pages(contract)
    capability = proof_capability_contract(capability_ledger, **{**inputs(contract, pages), "source_revision": SOURCE})
    return lifecycle, capability


def test_proof_handoff_review_requires_both_bound_generated_procedures(tmp_path):
    lifecycle, capability = generated_reports(tmp_path)
    result = proof_handoff_review(lifecycle, capability, SOURCE, CONTRACT_DIGEST)
    assert result["scenario_id"] == "SCN-03" and result["controlled_status"] == "pass"
    assert result["handoff_readiness"] == "candidate"
    assert result["source_revision"] == SOURCE and result["contract_sha256"] == CONTRACT_DIGEST
    assert result["live_status"] == "unproven" and len(result["activation_blockers"]) == 3
    assert "subject-a" not in str(result) and "private-fixture-site" not in str(result)


@pytest.mark.parametrize("alter", [
    lambda lifecycle, capability: lifecycle.update(source_revision="b" * 40),
    lambda lifecycle, capability: lifecycle.update(remote_revocation="pass"),
    lambda lifecycle, capability: lifecycle.update(cleanup="confirmed"),
    lambda lifecycle, capability: lifecycle.update(provider_status="pass"),
    lambda lifecycle, capability: lifecycle.update(controlled_status="blocked"),
    lambda lifecycle, capability: capability.update(live_status="pass"),
    lambda lifecycle, capability: capability.update(design_revision="b" * 40),
    lambda lifecycle, capability: capability["schema_sha256"].pop("getJiraIssue"),
    lambda lifecycle, capability: capability.update(contract_sha256="b" * 64),
])
def test_mismatched_revisions_missing_schema_or_inferred_live_pass_fail_closed(tmp_path, alter):
    lifecycle, capability = generated_reports(tmp_path)
    alter(lifecycle, capability)
    with pytest.raises(ProbeBlocked):
        proof_handoff_review(lifecycle, capability, SOURCE, CONTRACT_DIGEST)


def test_handoff_rejects_undelivered_setup_recovery(tmp_path):
    lifecycle, capability = generated_reports(tmp_path)
    matrix = json.loads(MATRIX_PATH.read_text())
    matrix["forward_dependencies"][1]["deliverable"] = "setup, recovery later"
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix))
    with pytest.raises(ProbeBlocked):
        proof_handoff_review(lifecycle, capability, SOURCE, CONTRACT_DIGEST, path)
