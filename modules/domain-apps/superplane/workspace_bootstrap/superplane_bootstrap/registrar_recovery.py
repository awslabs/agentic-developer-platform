"""Owner-only cleanup of a stopped bootstrap before its first Kubernetes intent.

The trusted resolver qualifies stopped source, expired sessions, every retained
journal and the exact original claim. This composition exposes no create path.
Deletion still uses EksGrants' immutable-entry-scoped credentials.
"""

from .denied_grant import digest
from .errors import BootstrapRefused
from .registry import SqlRegistrationStore, registration_lock
from .temporary_authority import TemporaryAuthority

KIND = "bootstrap-pre-namespace-revoked-v1"
KEYS = {"registrar-entry", "registrar-policy"}


def recover_registrar(journal, registry, eks, *, revalidate, verify_absence):
    if (
        not isinstance(registry, SqlRegistrationStore)
        or registry.store is not journal.store
    ):
        raise BootstrapRefused(
            "registrar recovery requires the native registration store"
        )
    evidence = revalidate()

    def check(plan, progress):
        expected = {
            "evidence_kind": KIND,
            "original_authority_expired": True,
            "issued_sessions_expired": True,
            "operation_id": journal.binding.operation_id,
            "org_id": journal.target.org_id,
            "workspace_id": journal.target.workspace_id,
            "cluster_arn": journal.target.cluster_arn,
            "generation": journal.generation,
            "claim": journal.claim,
            "plan_sha256": digest(plan),
        }
        grants = plan.get("grants", [])
        complete = progress.get("phase") == "revoked"
        if (
            any(evidence.get(k) != v for k, v in expected.items())
            or progress.get("phase") not in {"revoking", "revoked"}
            or set(progress) != KEYS | {"phase"} | ({"complete"} if complete else set())
            or complete
            and progress.get("complete") is not True
            or len(grants) < 3
            or [(g.get("key"), g.get("kind"), g.get("lifetime")) for g in grants[:2]]
            != [
                ("registrar-entry", "eks-entry", "temporary"),
                ("registrar-policy", "eks-policy", "temporary"),
            ]
            or grants[2].get("key") != "workspace-namespace"
            or grants[2].get("kind") != "kubernetes"
            or grants[2].get("body", {}).get("kind") != "Namespace"
            or len({g.get("key") for g in grants}) != len(grants)
            or any(
                g.get("generation") != journal.generation
                or g.get("cluster_arn") != journal.target.cluster_arn
                for g in grants
            )
            or not progress["registrar-entry"].get("identity")
            or any(
                set(progress[k]) - {"phase", "identity"}
                or progress[k].get("phase")
                not in (
                    {"revoked"} if complete else {"granted", "revoke_intent", "revoked"}
                )
                for k in KEYS
            )
        ):
            raise BootstrapRefused(
                "registrar cleanup differs from its qualified prefix"
            )

    class Backend:
        def current(self):
            if revalidate() != evidence:
                raise BootstrapRefused("registrar cleanup authority changed")
            plan, progress = journal.read_locked()
            check(plan, progress)

        def observe(self, spec):
            self.current()
            return eks.observe(spec)

        def verify(self, spec, identity):
            self.current()
            _, progress = journal.read_locked()
            if identity.get("arn") != progress["registrar-entry"]["identity"].get(
                "arn"
            ):
                raise BootstrapRefused("registrar immutable entry changed")
            eks.verify(spec, identity)

        def delete(self, spec, identity):
            self.current()
            eks.delete(spec, identity)

        def verify_revoked(self, plan, progress):
            self.current()
            if verify_absence(plan) is not True:
                raise BootstrapRefused("bootstrap actor absence is unproven")

    backend = Backend()
    with registration_lock(journal.store, journal.target.workspace_id):
        with journal.fenced(recovery=True):
            backend.current()
        TemporaryAuthority(journal, backend).revoke()

        def release_check():
            plan, progress = journal.read()
            check(plan, progress)
            if not progress.get("complete"):
                raise BootstrapRefused("registrar revocation is incomplete")
            backend.verify_revoked(plan, progress)
            return True

        if registry.recover_claim(
            journal.target.workspace_id, journal.claim, restore=release_check
        ) != (True, True, True):
            raise BootstrapRefused("registrar claim release is unresolved")
    return {
        "evidence_kind": KIND,
        "generation": journal.generation,
        "plan_sha256": evidence["plan_sha256"],
        "revoked": True,
        "claim_released": True,
        "journal_retained": True,
    }
