"""The accepted node, never a tool argument, selects controlled proof dispatch."""

import pytest

from src.orchestration.proposal import OperationalReleaseBinding

BINDING = {
    "predecessors": [{"address": "atlassian-adapters-8445-parallel/epic-8445/w01-proof/story-8446", "receipt_kind": "verified_merge"}],
    "build_prerequisites": ["atlassian-adapters-8445-parallel/epic-8445/w01-proof/story-8446"],
    "repository": "aws-e/adp",
    "target": "adp-dev-gateway",
    "component": "gateway-backend",
    "workflows": ["gateway-deploy.yml"],
    "default_branch": "main",
}


def test_reviewed_binding_selects_exact_identity_proof_phase():
    binding = OperationalReleaseBinding.model_validate({**BINDING, "proof_phase": "stage.identity-proof"})
    assert binding.proof_phase == "stage.identity-proof"
    assert binding.model_dump()["proof_phase"] == "stage.identity-proof"
    assert "proof_phase" not in OperationalReleaseBinding.model_validate(BINDING).model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"proof_phase": "custom-phase"},
        {"proof_phase": "stage.identity-proof", "workflows": ["tools-live-evaluation.yml"]},
        {"proof_phase": "stage.identity-proof", "workflows": ["gateway-deploy.yml", "tools-live-evaluation.yml"]},
        {"proof_phase": "stage.identity-proof", "product_cases": "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"},
    ],
)
def test_proof_phase_cannot_target_other_workflows_or_combine_product_cases(changes):
    with pytest.raises(ValueError):
        OperationalReleaseBinding.model_validate({**BINDING, **changes})
