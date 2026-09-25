"""Shared placement branch of bootstrap_workspace; existing journals own effects."""

from .admission import prove_tenant_isolation
from .components import _installation_record
from .errors import BootstrapRefused
from .management_observation import ManagementObservation
from .membership import SharedMembership
from .readiness import ReadinessCheck, RuntimeReadiness, _system_workload_checks
from .registration import (
    reserve_registration,
    finalize_registration,
    WorkspaceRegistration,
)
from .retire import CleanupPlan
from .shared_authority import SharedBootstrapAuthorityFactory
from .target import verify_target
from .state import NamespaceRecord, claim_fingerprint, load_state, record


def shared_dependency_checks(authority):
    backend, release = authority.backend, authority.backend.release
    backend.verify_worker_binding()
    backend.services.verify_cluster_dependencies(authority)
    checks = _system_workload_checks(
        backend.clients.installer_access, release.system_workloads
    )
    for name in release.crds:
        actual = backend.grants._get(
            {
                "cluster_arn": backend.membership.cluster_arn,
                "body": {
                    "apiVersion": "apiextensions.k8s.io/v1",
                    "kind": "CustomResourceDefinition",
                    "metadata": {"name": name},
                },
            }
        )
        established = actual is not None and any(
            item.get("type") == "Established" and item.get("status") == "True"
            for item in actual.get("status", {}).get("conditions", [])
        )
        checks.append(
            ReadinessCheck(
                "shared_crd_established:" + name,
                established,
                "" if established else "required cluster-owned CRD is not established",
            )
        )
    return checks


def shared_readiness(authority, observer):
    backend = authority.backend
    checks = shared_dependency_checks(authority)
    expected = {
        "workspace_id": authority.journal.target.workspace_id,
        "org_id": authority.journal.target.org_id,
        "operation_id": authority.journal.binding.operation_id,
        "cluster_arn": authority.journal.target.cluster_arn,
        "namespace": backend.membership.namespace,
        "registration_claim": authority.journal.claim,
    }
    if (
        not isinstance(observer, ManagementObservation)
        or observer._expected != expected
    ):
        raise BootstrapRefused(
            "shared management observation names another member or claim"
        )
    observer.observe()
    backend.services.verify_credentials(authority, backend.gate.namespace_uid)
    checks.append(ReadinessCheck("member_credentials_and_management_observed", True))
    closed = backend.gate.is_closed()
    checks.append(
        ReadinessCheck(
            "member_admission_closed",
            closed,
            "" if closed else "member admission opened before verification",
        )
    )
    return RuntimeReadiness(backend.membership.namespace, tuple(checks))


def bootstrap_shared_workspace(
    *,
    binding,
    provider,
    observed_cluster,
    expected_account_id,
    expected_region,
    expected_cluster_name,
    expected_cluster_arn,
    expected_certificate_authority_data,
    cluster_ownership,
    namespace,
    enforce_version,
    store,
    state_store,
    authority_factory,
    membership,
    expected_cni_role_arn,
    contract_version,
    screen,
    declared_proofs,
):
    from .workspace import BootstrapOutcome

    target = reservation = authority = installation = readiness = evidence = None
    opened = restored = restore_failed = False
    try:
        if not isinstance(membership, SharedMembership) or not isinstance(
            authority_factory, SharedBootstrapAuthorityFactory
        ):
            raise BootstrapRefused(
                "shared bootstrap requires trusted namespace-only authority composition"
            )
        membership = SharedMembership.read(membership.encode())
        factory = authority_factory
        if (
            factory.membership != membership
            or namespace != membership.namespace
            or factory.release.enforce_version != enforce_version
            or cluster_ownership != "adopted"
        ):
            raise BootstrapRefused(
                "shared bootstrap differs from approved membership release"
            )
        target = verify_target(
            binding=binding,
            provider=provider,
            observed=observed_cluster,
            expected_account_id=expected_account_id,
            expected_region=expected_region,
            expected_cluster_name=expected_cluster_name,
            expected_cluster_arn=expected_cluster_arn,
            expected_certificate_authority_data=expected_certificate_authority_data,
            cluster_ownership=cluster_ownership,
        )
        factory.services.verify_membership(membership, recovery=False)
        reservation = reserve_registration(
            store=store, target=target, namespace=namespace, membership=membership
        )
        if reservation.replayed:
            existing = store.read(target.workspace_id)
            if existing is None:
                raise BootstrapRefused("shared registration replay lost its target")
            return BootstrapOutcome(
                target=target,
                reservation=reservation,
                registration=WorkspaceRegistration(existing, True),
            )
        state = load_state(
            state_store,
            workspace_id=target.workspace_id,
            cluster_arn=target.cluster_arn,
        )
        state = record(
            state_store,
            state,
            registration_reserved=True,
            registration_claim=claim_fingerprint(reservation.attempt_token),
        )
        authority = factory.create(
            binding=binding,
            target=target,
            reservation=reservation,
            store=store,
            state_store=state_store,
        )
        # Read global dependencies before the first namespace effect. Their
        # schemas/controller compatibility are deployment-owned pinned facts.
        if any(not check.verified for check in shared_dependency_checks(authority)):
            raise BootstrapRefused("shared cluster dependencies are not ready")
        authority.acquire()
        namespace_uid = authority.bind_namespace()
        state = record(
            state_store, state, namespace=NamespaceRecord(namespace, namespace_uid)
        )
        access = authority.backend.clients.installer_access
        observed = access.namespace(namespace)
        if observed is None or observed.uid != namespace_uid:
            raise BootstrapRefused("namespace observation differs from creation UID")
        installation = _installation_record(
            target, observed, True, factory.release.crds
        )
        if not authority.backend.gate.is_closed():
            raise BootstrapRefused(
                "member admission was not closed atomically at creation"
            )
        reference = factory.services.prepare_credentials(authority, namespace_uid)
        if not isinstance(reference, str) or not reference.strip():
            raise BootstrapRefused("shared credential issuance produced no reference")
        screen(reference, what="shared workspace credential reference")
        authority.record_components(reference)
        observer = factory.resolve_observation(authority)
        readiness = shared_readiness(authority, observer)
        # These existing probes create only bounded Pods in this namespace; they
        # do not patch nodes, CNI, CRDs, kube-system or a controller deployment.
        evidence = authority.mutate(
            prove_tenant_isolation,
            access=access,
            namespace=namespace,
            enforce_version=enforce_version,
            expected_cni_role_arn=expected_cni_role_arn,
            declared_proofs=declared_proofs,
        )
        if not readiness.usable or not evidence.may_clear_taint:
            raise BootstrapRefused(
                "shared namespace readiness or isolation is unverified"
            )
        authority.revoke(retain_workspace=True)
        readiness = shared_readiness(authority, observer)
        if not readiness.usable:
            raise BootstrapRefused("shared dependencies changed before activation")
        authority.gate(closed=False)
        opened = True
        registration = finalize_registration(
            store=store,
            reservation=reservation,
            target=target,
            installation=installation,
            evidence=evidence,
            readiness=readiness,
            credential_reference_id=reference,
            contract_version=contract_version,
            screen=screen,
            membership=membership,
        )
        record(state_store, state, registration_finalized=True)
        return BootstrapOutcome(
            target=target,
            reservation=reservation,
            installation=installation,
            readiness=readiness,
            evidence=evidence,
            registration=registration,
            namespace_gate_open=True,
        )
    except Exception as error:
        refusal = (
            error
            if isinstance(error, BootstrapRefused)
            else BootstrapRefused(
                "shared bootstrap failed; original namespace ownership and claim retained"
            )
        )
        if refusal is not error:
            refusal.__cause__ = error
        if authority is not None:
            try:
                opened = opened or authority.gate_may_be_open()
            except Exception:
                pass  # No journal exists if identity validation refused before acquisition.
            try:
                authority.recover_member()
                restored = True
            except Exception:
                restore_failed = True
            if installation is None and authority.backend.gate.namespace_uid:
                from .access import ObservedNamespace

                try:
                    actual = authority.backend.gate.namespace_body()["metadata"]
                    installation = _installation_record(
                        target,
                        ObservedNamespace(namespace, actual["uid"], actual["labels"]),
                        True,
                        factory.release.crds,
                    )
                except Exception:
                    pass  # Unknown incarnation retains the journal, never a guessed UID.
        # Retain the original claim even after revocation. Recovery/retirement
        # must reconcile any lost namespace/delegation/projection response before
        # another operation may take ownership of this namespace generation.
        cleanup = None
        if installation is not None:
            cleanup = CleanupPlan(
                workspace_id=target.workspace_id,
                cluster_arn=target.cluster_arn,
                cluster_ownership="adopted",
                remove_namespace=namespace,
                remove_namespace_uid=installation.namespace_uid,
                preserved=(
                    target.cluster_arn,
                    "cluster issuer EKS access entry",
                    "cluster admission policy",
                    "cluster CRDs and system workloads",
                    "peer namespaces and credentials",
                ),
            )
        return BootstrapOutcome(
            target=target,
            reservation=reservation,
            installation=installation,
            readiness=readiness,
            evidence=evidence,
            cleanup=cleanup,
            refusal=refusal,
            namespace_gate_open=opened,
            namespace_gate_restored=restored,
            namespace_gate_restore_failed=restore_failed,
        )


def recover_shared_bootstrap(*, factory, binding, target, store, state_store, state):
    """Resume namespace recovery through the public bootstrap recovery entrypoint."""
    from .workspace import BootstrapOutcome

    authority = None
    try:
        if target is None or (target.workspace_id, target.cluster_arn) != (
            state.workspace_id,
            state.cluster_arn,
        ):
            raise BootstrapRefused("shared recovery target differs from original state")
        journals = store.store.execute(
            "SELECT generation FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id "
            "AND operation_id=:operation_id AND claim=:claim",
            {
                "workspace_id": target.workspace_id,
                "operation_id": binding.operation_id,
                "claim": state.registration_claim,
            },
        )
        if not journals:
            from .authority_backend import BootstrapClients

            clients = factory.resolve_clients(binding, target, factory.release)
            if (
                not isinstance(clients, BootstrapClients)
                or clients.binding != binding
                or clients.target != target
            ):
                raise BootstrapRefused(
                    "unstarted shared recovery lacks original authority"
                )
            clients.verify(recovery=True)
            factory.services.verify_membership(factory.membership, recovery=True)
            # acquire() commits the authority journal before any namespace effect.
            # Dependency refusal before that commit has no provider ownership to
            # revoke. The original claim must still match before release.
            matched, _, released = store.recover_claim(
                target.workspace_id, state.registration_claim, restore=lambda: True
            )
            if not matched or not released:
                raise BootstrapRefused("unstarted shared claim is no longer current")
            record(
                state_store, state, registration_reserved=False, registration_claim=""
            )
            return BootstrapOutcome(reservation_released=True)
        authority = factory.recover(
            binding=binding,
            target=target,
            store=store,
            state_store=state_store,
            claim=state.registration_claim,
        )
        authority.recover_member()
        # The closed namespace remains owned. Only the revoked claim is released;
        # a retry can adopt its original UID using the prior authority journal.
        matched, restored, released = store.recover_claim(
            target.workspace_id,
            state.registration_claim,
            restore=lambda: authority.backend.gate.namespace_uid is None
            or authority.backend.gate.is_closed(),
        )
        if not matched or not restored or not released:
            raise BootstrapRefused(
                "shared recovery could not release its original claim"
            )
        record(state_store, state, registration_reserved=False, registration_claim="")
        return BootstrapOutcome(namespace_gate_restored=True, reservation_released=True)
    except Exception as error:
        refusal = BootstrapRefused(
            "shared bootstrap recovery remains unresolved; namespace ownership retained"
        )
        refusal.__cause__ = error
        return BootstrapOutcome(refusal=refusal, namespace_gate_restore_failed=True)
