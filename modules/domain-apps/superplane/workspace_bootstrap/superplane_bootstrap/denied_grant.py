"""Finish cleanup after a positively denied first grant, retaining its journal.

This is a trusted installation-owner composition, never a worker retry or an
arbitrary journal repair. The resolver must independently authenticate the owner,
qualify the stopped source and AWS denial, and establish expired execution and
session authority. The two callbacks are service dependencies, not request data.
No provider mutation is available to this cleanup backend.
"""

import hashlib
import json

from .errors import BootstrapRefused
from .registry import SqlRegistrationStore, registration_lock
from .temporary_authority import TemporaryAuthority

KIND = "bootstrap-denied-first-grant-v1"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def denied_progress(progress, *, complete=False):
    expected = {
        "phase": "revoked" if complete else "revoking",
        "registrar-entry": {"phase": "revoked", "identity": None},
    }
    if complete:
        expected["complete"] = True
    if progress != expected:
        raise BootstrapRefused(
            "journal is not an otherwise untouched denied first grant"
        )


def recover_denied_grant(journal, registry, *, revalidate, verify_absence):
    """Revoke and release under the native claim lock; never delete the journal.

    A repeated call after release refuses the stale claim. An interruption after
    revocation but before release may resume with the exact same denied proof.
    Any extra progress, observed grant identity or changed proof is refused.
    """
    if (
        not isinstance(registry, SqlRegistrationStore)
        or registry.store is not journal.store
    ):
        raise BootstrapRefused(
            "denied grant recovery requires the native registration store"
        )
    evidence = revalidate()

    def check(plan, progress):
        if (
            evidence.get("evidence_kind") != KIND
            or evidence.get("original_authority_expired") is not True
            or evidence.get("issued_sessions_expired") is not True
            or evidence.get("denied_request_verified") is not True
            or any(
                evidence.get(key) != expected
                for key, expected in {
                    "operation_id": journal.binding.operation_id,
                    "org_id": journal.target.org_id,
                    "workspace_id": journal.target.workspace_id,
                    "cluster_arn": journal.target.cluster_arn,
                    "generation": journal.generation,
                    "claim": journal.claim,
                    "plan_sha256": digest(plan),
                }.items()
            )
        ):
            raise BootstrapRefused("denied first grant proof differs from its journal")
        denied_progress(progress, complete=progress.get("phase") == "revoked")
        grants = plan.get("grants", [])
        if (
            not grants
            or grants[0].get("key") != "registrar-entry"
            or grants[0].get("kind") != "eks-entry"
            or grants[0].get("lifetime") != "temporary"
            or any(g.get("generation") != journal.generation for g in grants)
            or len({g.get("key") for g in grants}) != len(grants)
        ):
            raise BootstrapRefused(
                "denied grant plan is not the qualified first-grant plan"
            )

    class AbsenceOnlyBackend:
        def verify_revoked(self, plan, progress):
            check(plan, progress)
            if revalidate() != evidence:
                raise BootstrapRefused("denied first grant authority changed")
            if verify_absence(plan) is not True:
                raise BootstrapRefused("denied bootstrap grant absence is unproven")

    # Hold the existing registration lock throughout validation, native
    # revocation and compare-and-release. Nested native calls use the same store.
    with registration_lock(journal.store, journal.target.workspace_id):
        with journal.fenced(recovery=True):
            plan, progress = journal.read_locked()
            check(plan, progress)
            AbsenceOnlyBackend().verify_revoked(plan, progress)
        TemporaryAuthority(journal, AbsenceOnlyBackend()).revoke()

        def release_check():
            plan, progress = journal.read()
            denied_progress(progress, complete=True)
            AbsenceOnlyBackend().verify_revoked(plan, progress)
            return True

        matched, restored, released = registry.recover_claim(
            journal.target.workspace_id, journal.claim, restore=release_check
        )
        if (matched, restored, released) != (True, True, True):
            raise BootstrapRefused("denied bootstrap claim release is unresolved")
    return {
        "evidence_kind": KIND,
        "generation": journal.generation,
        "plan_sha256": digest(plan),
        "revoked": True,
        "claim_released": True,
        "journal_retained": True,
    }
