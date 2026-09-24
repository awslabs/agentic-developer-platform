"""Bind the public bootstrap/recovery path to trusted clients and a real claim."""

from __future__ import annotations

from dataclasses import dataclass

from .authority_backend import BootstrapClients, BootstrapGrantBackend
from .authority_journal import AuthorityJournal, generation_for
from .errors import BootstrapRefused
from .registry import SqlRegistrationStore
from .state import claim_fingerprint
from .temporary_authority import TemporaryAuthority

READS = frozenset(
    {
        "custom_resource_definitions",
        "controller_deployments",
        "namespace",
        "workload",
        "node_taints",
        "controller_handover",
        "controller_permissions",
        "can_tenant_change_admission_labels",
        "bootstrap_permission",
        "tenant_scheduling_denied",
    }
)
INSTALL = frozenset(
    {
        "create_namespace",
        "establish_crds",
        "establish_controller_rbac",
        "install_controller",
        "place_system_workloads",
        "dry_run_pod",
        "imds_reachable_from_tenant_pod",
        "cni_credential_scope",
        "tenant_scheduling_denied",
    }
)
INTERLOCK = frozenset({"remove_bootstrap_taint", "restore_bootstrap_taint"})


class FencedClusterAccess:
    def __init__(self, authority, *, supervisor=False):
        self.authority, self.supervisor = authority, supervisor
        self.raw = (
            authority.backend.clients.supervisor_access
            if supervisor
            else authority.backend.clients.installer_access
        )
        from .adapters import KubectlClusterAccess

        if not supervisor and isinstance(self.raw, KubectlClusterAccess):
            from .component_journal import ComponentJournal

            self.raw.component_journal = ComponentJournal(authority, self.raw)

    def __getattr__(self, name):
        value = getattr(self.raw, name)
        if not callable(value):
            return value
        if name not in READS | (INTERLOCK if self.supervisor else INSTALL):
            raise BootstrapRefused(
                "cluster operation is outside the selected bootstrap capability"
            )

        def call(*args, **kwargs):
            lease = self.authority
            if not self.supervisor:
                if (
                    name in {"establish_controller_rbac", "install_controller"}
                    and getattr(self.raw, "component_journal", None) is not None
                ):
                    # Each component commits intent before taking its I/O lock.
                    # An outer transaction here would undo that durability.
                    return value(*args, **kwargs)
                return lease.mutate(value, *args, **kwargs)
            # The bounded supervisor owns only inventory and the scheduling
            # interlock. It stays usable after all installation grants are gone.
            with lease.journal.fenced(recovery=name == "restore_bootstrap_taint"):
                plan, progress = lease.journal.read_locked()
                if progress.get("phase") != "revoked" or not progress.get(
                    "retain_workspace"
                ):
                    raise BootstrapRefused(
                        "installation authority has not been positively revoked"
                    )
                lease.backend.clients.verify(recovery=name == "restore_bootstrap_taint")
                lease.backend.verify_revoked(plan, progress)
                return value(*args, **kwargs)

        return call


class WorkspaceAuthority(TemporaryAuthority):
    def record_components(self):
        from .adapters import KubectlClusterAccess
        from .component_journal import expected_component_keys

        if not isinstance(self.backend.clients.installer_access, KubectlClusterAccess):
            return
        release = self.backend.release
        management = (
            getattr(self.backend.clients.installer_access, "controller_mode", None)
            == "management"
        )
        expected = expected_component_keys(
            release.namespace,
            release.service_account,
            None if management else release.controller,
        )
        with self.journal.fenced():
            _, progress = self.journal.read_locked()
            self._require_phase(progress, "active")
            components = progress.get("components", {})
            if set(components) != expected or any(
                record.get("phase") not in {"owned", "adopted"}
                or not record.get("identity", {}).get("uid")
                for record in components.values()
            ):
                raise BootstrapRefused("durable component inventory is incomplete")
            progress["component_inventory_complete"] = True
            progress["component_inventory_mode"] = (
                "management" if management else "legacy"
            )
            # Read-only discovery before canonical publication. These facts come
            # from the verified target and real claim, never a request body. The
            # API additionally checks the current shared operation lease.
            target = self.journal.target
            progress["management_observation"] = {
                "workspace_id": target.workspace_id,
                "org_id": target.org_id,
                "operation_id": self.journal.binding.operation_id,
                "registration_claim": self.journal.claim,
                "cluster_arn": target.cluster_arn,
                "namespace": release.namespace,
                "endpoint": target.endpoint,
            }
            self.journal.write_locked(progress)

    def configure_management_observation(self):
        from .management_observation import ManagementObservation

        access = self.backend.clients.installer_access
        if getattr(access, "controller_mode", None) != "management":
            return
        resolver = self.backend.resolve_observation
        observer = resolver(self)
        if not isinstance(observer, ManagementObservation):
            raise BootstrapRefused(
                "bootstrap requires the concrete scoped management observer"
            )
        target = self.journal.target
        expected = {
            "workspace_id": target.workspace_id,
            "org_id": target.org_id,
            "operation_id": self.journal.binding.operation_id,
            "cluster_arn": target.cluster_arn,
            "namespace": self.backend.release.namespace,
            "registration_claim": self.journal.claim,
        }
        if observer._expected != expected:
            raise BootstrapRefused("management observer names another bootstrap claim")
        access.management_observation = observer
        self.backend.clients.supervisor_access.management_observation = observer

    def record_prerequisites(self, inventory):
        from dataclasses import asdict
        from .prerequisites import require_inventory

        require_inventory(
            inventory,
            workspace_id=self.journal.target.workspace_id,
            what="durable bootstrap ownership",
        )
        with self.journal.fenced():
            _, progress = self.journal.read_locked()
            self._require_phase(progress, "active")
            progress["prerequisite_inventory"] = asdict(inventory)
            self.journal.write_locked(progress)

    @property
    def installer(self):
        return FencedClusterAccess(self)

    @property
    def supervisor(self):
        return FencedClusterAccess(self, supervisor=True)

    def prerequisite(self):
        with self.journal.fenced():
            return self.backend.workspace_prerequisite()


class BootstrapAuthorityFactory:
    """`resolve_clients` belongs to the service's operation/vault composition.

    It returns real SDK/transport clients bound to the supplied verified target and
    operation. The CLI loads this service factory, never role ARNs or policy JSON
    from its request. No ambient-credential fallback exists in this factory.
    """

    def __init__(self, resolve_clients, release, *, resolve_observation=None):
        self.resolve_clients, self.release = resolve_clients, release
        self.resolve_observation = resolve_observation
        self._resolved = []

    def _backend(self, binding, target, state_store):
        clients = self.resolve_clients(binding, target, self.release)
        if (
            not isinstance(clients, BootstrapClients)
            or clients.binding != binding
            or clients.target != target
        ):
            raise BootstrapRefused(
                "trusted bootstrap client composition returned another operation or target"
            )
        self._resolved.append(clients)
        backend = BootstrapGrantBackend(clients, self.release, state_store)
        backend.resolve_observation = self.resolve_observation
        clients.installer_access.tenant_identity_reader = (
            lambda: backend.tenant_principals(temporary=True)
        )
        clients.supervisor_access.tenant_identity_reader = (
            lambda: backend.tenant_principals()
        )
        return backend

    def create(self, *, binding, target, reservation, store, state_store):
        if not isinstance(store, SqlRegistrationStore):
            raise BootstrapRefused(
                "production bootstrap authority requires the transactional registration store"
            )
        journal = AuthorityJournal(
            store.store,
            binding,
            target,
            generation_for(binding, reservation),
            claim_fingerprint(reservation.attempt_token),
        )
        backend = self._backend(binding, target, state_store)
        if getattr(
            backend.clients.installer_access, "controller_mode", None
        ) == "management" and not callable(self.resolve_observation):
            raise BootstrapRefused(
                "management bootstrap requires operation-scoped observation composition"
            )
        backend.journal = journal
        return WorkspaceAuthority(journal, backend)

    def close(self):
        for clients in self._resolved:
            for access in (clients.installer_access, clients.supervisor_access):
                access.close()
        self._resolved.clear()

    def recover(self, *, binding, target, store, state_store, claim):
        if not isinstance(store, SqlRegistrationStore):
            raise BootstrapRefused(
                "production bootstrap recovery requires the transactional registration store"
            )
        rows = store.store.execute(
            "SELECT generation FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id "
            "AND operation_id=:operation_id AND org_id=:org_id AND cluster_arn=:cluster_arn AND claim=:claim",
            {
                "workspace_id": target.workspace_id,
                "operation_id": binding.operation_id,
                "org_id": target.org_id,
                "cluster_arn": target.cluster_arn,
                "claim": claim,
            },
        )
        if len(rows) != 1:
            raise BootstrapRefused(
                "bootstrap recovery has no unique operation-bound authority journal"
            )
        journal = AuthorityJournal(
            store.store, binding, target, rows[0]["generation"], claim
        )
        with journal.fenced(recovery=True):
            journal.read_locked()  # Refuse stale claims before even resolving provider clients.
        backend = self._backend(binding, target, state_store)
        backend.journal = journal
        return WorkspaceAuthority(journal, backend)


@dataclass(frozen=True)
class BootstrapRuntime:
    """Trusted CLI composition, shared with the owning provisioning service."""

    authority: BootstrapAuthorityFactory
    observer: object
    access: object
    prerequisite_access: object
    registration_store: SqlRegistrationStore

    def __post_init__(self):
        if not isinstance(self.authority, BootstrapAuthorityFactory) or not isinstance(
            self.registration_store, SqlRegistrationStore
        ):
            raise BootstrapRefused(
                "bootstrap runtime requires production authority and registration composition"
            )
