"""Load the canonical harness request decoder from the checkout or staged image.

scripts/stage-contracts.sh stages the original source on every build. No copy is
maintained here, and the Docker build verifies that the staged decoder imports.
"""

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def identity_contract():
    here = Path(__file__).resolve()
    # Prefer the canonical source in a full checkout; the image only has staging.
    candidates = [here.parents[2] / "contracts/harness-operation/identity.py"]
    if len(here.parents) > 4:
        candidates.insert(0, here.parents[4] / "modules/harness/jobs/harness_jobs/identity.py")
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise RuntimeError("canonical operation contract is unavailable")
    spec = importlib.util.spec_from_file_location("_adp_harness_identity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    identity = identity_contract()
    request = identity.OperationRequest(
        action="provision",
        idempotency_key="build-check",
        parameters={
            "credential_id": "id",
            "credential_service": "aws",
            "credential_label": "default",
            "provider": "aws",
            "provider_account_id": "123456789012",
        },
    )
    assert identity.admitted_credential_reference(identity.encode_payload(request), identity.payload_digest(request)) == ("id", "aws", "default")

    assert identity.admitted_credential_target(identity.encode_payload(request), identity.payload_digest(request)) == ("aws", "123456789012")
