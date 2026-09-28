"""Root-principal namespace helpers for the budget lambdas (Issue #4391).

This is a MIRROR of ``src/budget/enforcement_service.py``. That module is the
authoritative implementation; this file exists only because a Lambda cannot
import gateway ``src``: it is a separate deploy artifact, and the containing
directory is literally named ``lambda`` (a Python keyword), so no import path
exists in either direction.

The agreement is pinned by a parity test rather than by the type system —
``tests/lambda/test_budget_usage_tracker.py::TestRootPrincipalHelperParity``
(T17) asserts this copy and ``src.budget.enforcement_service`` produce identical
output. That is the same mechanism ``_ROOT_USER_ENTITY_TYPE`` /
``_ORGANIZATION_ENTITY_TYPE`` use (T15/T16), and for the same reason: a
one-sided edit here has no compile-time consequence, and its runtime symptom is
silent — the ledger quietly double-counts (Issue #4391) or quietly stops being
readable by enforcement (Issue #4322). The test turns drift into a CI failure.

If you change the semantics, change ``src/budget/enforcement_service.py`` FIRST
and let the parity test tell you this file is stale.
"""

# Issue #4344: only the SERVICE side of a root principal id is namespace-qualified.
# Human ids stay bare, which is what keeps every human-rooted ledger key
# byte-identical to #4300 and is why that change needed no migration.
#
# Must stay equal to ``_SERVICE_PRINCIPAL_PREFIX`` in
# ``src/budget/enforcement_service.py`` — pinned by T17.
SERVICE_PRINCIPAL_PREFIX = "service:"


def unqualify_root_principal_id(entity_id: str) -> str:
    """Strip the service namespace back off, for COMPARISON ONLY (Issue #4344).

    Answers "is the root principal the same party as the authenticated caller?".
    That question is about the PRINCIPAL, not about the namespace it was written
    in: for a service-rooted run the registry row puts the same service identity
    key in both ``user_id`` and ``root_human_id``, so comparing the qualified id
    against a bare ``user_id`` would find them different and settle the same
    dollar on a SECOND ledger line (Issue #4391).

    Comparison only — never write the return value to the ledger. The entity id
    the ledger and enforcement agree on is the QUALIFIED one.

    Only an exact leading ``service:`` is stripped; an id that merely contains a
    colon elsewhere is returned unchanged.
    """
    if entity_id.startswith(SERVICE_PRINCIPAL_PREFIX):
        return entity_id[len(SERVICE_PRINCIPAL_PREFIX) :]
    return entity_id
