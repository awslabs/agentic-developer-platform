"""Owner qualification of retained components after failed-path revocation.

Read-only throughout. The original journal and its ownership are preserved;
the authorized replacement worker re-observes those identities before reuse.
"""

import importlib.util
import json
from pathlib import Path

from .bootstrap_denial import verify_absence
from .bootstrap_recovery import collect, source_contract
from .bootstrap_revoked import acquisition_contract, expired, verify_cleanup_absence
from .provider_authority import require


def components_contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_components.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_components_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_components(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_snapshot,
    read_expiry,
    **source_options,
):
    from harness_jobs.settled_bootstrap import journal_bound

    before, expiry = read_snapshot(), read_expiry()
    expired(descriptor, expiry)
    require("settled_bootstrap" in descriptor and "attempt_extension" not in descriptor)
    evidence = collect(enrollment, original, descriptor, session, **source_options)
    contract = components_contract()
    contract.validate_source(
        source_contract().qualified_source(evidence["source_image"])
    )
    evidence.update(
        evidence_kind=contract.KIND, revoked_bootstrap=before["revoked_bootstrap"]
    )
    identity = contract.validate(
        evidence, grant_identity=acquisition_contract().journal_identity
    )
    rows = before["journals"]
    require(len(rows) == 1)
    row = rows[0]
    journal_bound(evidence["settled_bootstrap"], row)
    require(
        identity
        == contract.journal_identity(
            row, grant_identity=acquisition_contract().journal_identity
        )
    )
    eks = session.client("eks", region_name=enrollment["authority"]["region"])
    verify_absence(
        eks,
        json.loads(row["plan_json"]),
        cluster_name=row["cluster_arn"].split("/")[-1],
    )
    verify_cleanup_absence(session, row)
    require(before == read_snapshot() and expiry == read_expiry())
    return evidence
