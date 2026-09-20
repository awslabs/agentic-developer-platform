"""Explicit unsupported boundaries. These entries can never produce PASS.

Q2 cannot safely create a fault by changing a shared deployment, revoking the
operator's connection or editing a budget ledger. Missing disposable fixtures
remain named qualification blockers, with the owning native contract linked.
"""

from .http import Unsupported

UNAVAILABLE = {
    "stale-image": (
        5152,
        "No inventoried disposable deployment target; altering the registered shared gateway would affect other runs",
    ),
    "failed-deploy": (
        5151,
        "No inventoried disposable deployment workflow/target for a real failed rollout",
    ),
    "revocation": (
        5128,
        "No inventoried disposable authority credential; revoking the registered owner connection is outside fixture scope",
    ),
    "fanout-budget": (
        5128,
        "The public flow-cost response omits in-flight reservations; no isolated allowance fixture provides reserved-plus-settled boundary evidence",
    ),
    "repair-budget": (
        5128,
        "The public flow-cost response omits in-flight reservations; repair allowance exhaustion cannot be inferred from settled cost",
    ),
    "halt-stop": (
        3963,
        "No separately inventoried halt-control worker; graph halt alone is not proof of terminated work",
    ),
}


def unavailable(name, session):
    owner, reason = UNAVAILABLE[name]
    observation = {
        "status": "NOT_RUN",
        "criterion": name,
        "reason": reason,
        "owner": f"https://github.com/aws-e/adp/issues/{owner}",
        "fixture_kinds": sorted({r.kind for r in session.inventory.fixtures}),
    }
    artifact = session.evidence.save(
        "unavailable-" + name, observation, observation["owner"]
    )
    error = Unsupported(f"{reason}. Owning contract: {observation['owner']}")
    error.evidence = artifact
    raise error
