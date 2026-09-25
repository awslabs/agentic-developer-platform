"""Namespace bootstrap composed with the existing claim/grant/component journals."""

from dataclasses import dataclass

from .authority_backend import BootstrapClients
from .authority_journal import AuthorityJournal, generation_for
from .authority_runtime import BootstrapAuthorityFactory
from .component_journal import ComponentJournal, component_identity
from .components import _namespace_labels
from .errors import BootstrapRefused
from .kube_grants import GENERATION_ANNOTATION, KubeGrants, _payload
from .namespace_admission import (
    ClusterAuthorityReference,
    GATE_LABEL,
    NamespaceAdmission,
)
from .registry import SqlRegistrationStore
from .state import claim_fingerprint
from .temporary_authority import TemporaryAuthority


@dataclass(frozen=True)
class SharedBootstrapServices:
    """Operation-owned effects supplied by the protected service, never a request.

    Credential preparation must call establish_components with the actual issuer's
    SA/RBAC plan, issue separate short-lived reader/mutator credentials and publish
    them through trusted projection. Verification must re-read those projections
    and exercise the tenant admission denial with the delivered identity.
    Withdrawal removes/revokes only this membership's projected credentials.
    """

    verify_membership: object
    prepare_credentials: object
    verify_credentials: object
    withdraw_credentials: object
    verify_cluster_dependencies: object
    tenant_principals: object

    def __post_init__(self):
        if not all(
            callable(getattr(self, field)) for field in self.__dataclass_fields__
        ):
            raise BootstrapRefused("shared bootstrap service composition is incomplete")


class SharedGrantBackend:
    def __init__(
        self, clients, release, reference, membership, services, *, recovery=False
    ):
        self.clients, self.release, self.reference = clients, release, reference
        self.membership, self.services = membership, services
        self.grants = KubeGrants(clients.registrar_kubernetes, clients.target)
        self.gate = NamespaceAdmission(self.grants, reference, membership.namespace)
        self.verify_worker_binding(recovery=recovery)
        clients.installer_access.bind_target(clients.target, clients.binding)

    def verify_worker_binding(self, *, recovery=False):
        self.services.verify_membership(self.membership, recovery=recovery)
        self.reference.verify(self.clients, recovery=recovery)
        self.gate.verify_policy()

    def plan(self, journal):
        # The sole grant is the member namespace. The cluster's principal, EKS
        # access entry, admission policy, CRDs and system services are dependencies
        # and can never enter TemporaryAuthority's revocation loop.
        return {
            "mode": "shared-namespace",
            "membership": self.membership.encode(),
            "cluster_authority_entry": self.reference.access_entry_arn,
            "grants": [
                {
                    "key": "workspace-namespace",
                    "kind": "kubernetes",
                    "actor": "registrar",
                    "cluster_arn": journal.target.cluster_arn,
                    "generation": self.membership.generation,
                    "lifetime": "resource",
                    "body": {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "spec": {"finalizers": ["kubernetes"]},
                        "metadata": {
                            "name": self.membership.namespace,
                            "annotations": {
                                GENERATION_ANNOTATION: self.membership.generation
                            },
                            "labels": {
                                **_namespace_labels(
                                    journal.target, self.release.enforce_version
                                ),
                                GATE_LABEL: "closed",
                            },
                        },
                    },
                }
            ],
        }

    def observe(self, spec):
        self.verify_worker_binding()
        return self.grants.observe(spec)

    def create(self, spec):
        self.verify_worker_binding()
        return self.grants.create(spec)

    def verify(self, spec, identity):
        if identity is None:
            raise BootstrapRefused("member namespace disappeared")
        self.grants.verify(spec, identity)
        expected = spec["body"]["metadata"]["labels"]
        if any(identity.get("labels", {}).get(k) != v for k, v in expected.items()):
            raise BootstrapRefused("member namespace admission labels changed")

    def verify_adoption(self, spec, identity):
        # Labels alone never establish ownership of a pre-existing namespace.
        # Reuse requires this same membership's durable original creation UID.
        import json

        rows = self.journal.store.execute(
            "SELECT progress_json FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id "
            "AND org_id=:org_id AND cluster_arn=:cluster_arn AND generation<>:generation",
            {
                **self.journal.key,
                "org_id": self.membership.org_id,
                "cluster_arn": self.membership.cluster_arn,
            },
        )
        if not any(
            json.loads(row["progress_json"])
            .get("workspace-namespace", {})
            .get("identity")
            == identity
            for row in rows
        ):
            raise BootstrapRefused("member namespace has no original creation journal")
        self.verify(spec, identity)

    def verify_worker_permissions(self):
        self.verify_worker_binding()

    def verify_revoked(self, plan, progress):
        self.verify_worker_binding(recovery=True)
        if any(
            spec["kind"] != "kubernetes" or spec["key"] != "workspace-namespace"
            for spec in plan["grants"]
        ):
            raise BootstrapRefused("shared authority contains non-member grants")

    def delete(self, spec, identity):
        raise BootstrapRefused(
            "namespace removal requires separate verified member retirement"
        )

    def read_component(self, body):
        self._member_body(body)
        return self.grants._get(
            {"cluster_arn": self.membership.cluster_arn, "body": body}
        )

    def create_component(self, body):
        self._member_body(body)
        resource = self.grants._resource(
            {"cluster_arn": self.membership.cluster_arn, "body": body}
        )
        return _payload(resource.create(namespace=self.membership.namespace, body=body))

    def _member_body(self, body):
        if (
            body.get("kind") not in {"ServiceAccount", "Role", "RoleBinding"}
            or body.get("metadata", {}).get("namespace") != self.membership.namespace
        ):
            raise BootstrapRefused(
                "shared delegation may create only member SA and namespaced RBAC"
            )


class SharedWorkspaceAuthority(TemporaryAuthority):
    def establish_components(self, specs):
        """Called by trusted issuer composition after observing the namespace UID."""
        journal = ComponentJournal(self, self.backend)
        records = []
        for spec in specs:
            body = spec["body"]
            self.backend._member_body(body)
            if spec.get("cluster_arn") != self.backend.membership.cluster_arn:
                raise BootstrapRefused("member delegation targets another cluster")
            records.append(journal.ensure(body))
        return tuple(records)

    def bind_namespace(self, *, recovery=False):
        with self.journal.fenced(recovery=recovery):
            plan, progress = self.journal.read_locked()
            identity = progress.get("workspace-namespace", {}).get("identity", {})
            if not identity.get("uid") and recovery:
                status = progress.get("workspace-namespace", {})
                if status.get("phase") == "grant_intent":
                    spec = plan["grants"][0]
                    identity = self.backend.grants.observe(spec)
                    if identity is not None:
                        self.backend.verify(spec, identity)
                        progress["workspace-namespace"] = {
                            "phase": "granted",
                            "identity": identity,
                        }
                        self.journal.write_locked(progress)
                if not identity:
                    return None
            if not identity.get("uid"):
                raise BootstrapRefused("member namespace ownership has no observed UID")
            self.backend.gate.namespace_uid = identity["uid"]
        self.backend.gate.namespace_body()
        return self.backend.gate.namespace_uid

    def gate(self, *, closed, recovery=False):
        # Intent commits before the provider request. A lost response is recovered
        # by closing this exact namespace UID, never by touching cluster nodes.
        with self.journal.fenced(recovery=recovery):
            _, progress = self.journal.read_locked()
            if not closed and (
                progress.get("phase") != "revoked"
                or not progress.get("complete")
                or not progress.get("component_inventory_complete")
                or not progress.get("retain_workspace")
            ):
                raise BootstrapRefused(
                    "member admission cannot open before bootstrap completion"
                )
            self.backend.verify_worker_binding(recovery=recovery)
            progress["member_gate_intent"] = "closed" if closed else "open"
            self.journal.write_locked(progress)

        with self.journal.fenced(recovery=recovery):
            _, progress = self.journal.read_locked()
            self.backend.verify_worker_binding(recovery=recovery)
            self.backend.gate.set_closed(closed)
            progress["member_gate"] = "closed" if closed else "open"
            progress.pop("member_gate_intent", None)
            self.journal.write_locked(progress)

    def gate_may_be_open(self):
        with self.journal.fenced(recovery=True):
            _, progress = self.journal.read_locked()
            return (
                progress.get("member_gate") == "open"
                or progress.get("member_gate_intent") == "open"
            )

    def record_components(self, reference):
        with self.journal.fenced():
            _, progress = self.journal.read_locked()
            components = progress.get("components", {})
            if not components or any(
                item.get("phase") != "owned"
                or component_identity(self.backend.read_component(item["desired"]))
                != item["identity"]
                for item in components.values()
            ):
                raise BootstrapRefused(
                    "member credential component inventory is incomplete"
                )
            progress.update(
                component_inventory_complete=True,
                component_inventory_mode="shared-namespace",
                member_credential_reference=reference,
                management_observation={
                    "workspace_id": self.journal.target.workspace_id,
                    "org_id": self.journal.target.org_id,
                    "operation_id": self.journal.binding.operation_id,
                    "registration_claim": self.journal.claim,
                    "cluster_arn": self.journal.target.cluster_arn,
                    "namespace": self.backend.membership.namespace,
                    "endpoint": self.journal.target.endpoint,
                },
            )
            self.journal.write_locked(progress)

    def recover_member(self):
        namespace_uid = self.bind_namespace(recovery=True)
        if namespace_uid is not None:
            self.gate(closed=True, recovery=True)
            # This callback must withdraw exact namespace/generation/revision Secret
            # projections and token authority. Neither metadata nor expiry is proof.
            self.backend.services.withdraw_credentials(self, namespace_uid)
            self.remove_owned_components()
        self.revoke()

    def remove_owned_components(self):
        """Reconcile original component intents, then delete by UID and version.

        This is bootstrap recovery under its retained claim. Successful member
        retirement must load the same ownership records under separate admitted
        retirement authority; it cannot reuse this bootstrap claim.
        """
        with self.journal.fenced(recovery=True):
            _, progress = self.journal.read_locked()
            keys = tuple(progress.get("components", {}))
        for key in reversed(keys):
            with self.journal.fenced(recovery=True):
                self.backend.verify_worker_binding(recovery=True)
                _, progress = self.journal.read_locked()
                record = progress["components"][key]
                if record.get("phase") == "adopted":
                    continue
                body = record["desired"]
                self.backend._member_body(body)
                observed = self.backend.read_component(body)
                if observed is not None:
                    identity = component_identity(observed)
                    if (
                        identity.get("creation") != record.get("creation")
                        or not record.get("creation")
                        or (
                            record.get("phase") == "owned"
                            and identity != record.get("identity")
                        )
                    ):
                        raise BootstrapRefused(
                            "member component no longer has original creation identity"
                        )
                    ComponentJournal._matches(observed, body)
                    version = observed.get("metadata", {}).get("resourceVersion")
                    if not version:
                        raise BootstrapRefused(
                            "member component lacks deletion preconditions"
                        )
                    spec = {
                        "cluster_arn": self.backend.membership.cluster_arn,
                        "body": body,
                    }
                    resource = self.backend.grants._resource(spec)
                    resource.delete(
                        **self.backend.grants._args(spec),
                        body={
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "preconditions": {
                                "uid": identity["uid"],
                                "resourceVersion": version,
                            },
                        },
                    )
                    if self.backend.read_component(body) is not None:
                        raise BootstrapRefused(
                            "member component deletion remains unresolved"
                        )
                progress.setdefault("component_cleanup", {})[key] = "absent"
                self.journal.write_locked(progress)


class SharedBootstrapAuthorityFactory(BootstrapAuthorityFactory):
    def __init__(
        self,
        resolve_clients,
        release,
        *,
        membership,
        cluster_authority,
        services,
        resolve_observation,
    ):
        from .membership import SharedMembership

        if (
            not isinstance(membership, SharedMembership)
            or not isinstance(cluster_authority, ClusterAuthorityReference)
            or not isinstance(services, SharedBootstrapServices)
            or not callable(resolve_observation)
            or release.namespace != membership.namespace
        ):
            raise BootstrapRefused(
                "shared namespace bootstrap composition is incomplete"
            )
        super().__init__(
            resolve_clients, release, resolve_observation=resolve_observation
        )
        self.membership = SharedMembership.read(membership.encode())
        self.cluster_authority, self.services = cluster_authority, services

    def _backend(self, binding, target, state_store):
        clients = self.resolve_clients(binding, target, self.release)
        if (
            not isinstance(clients, BootstrapClients)
            or clients.binding != binding
            or clients.target != target
        ):
            raise BootstrapRefused("shared bootstrap clients name another admission")
        self._resolved.append(clients)
        return SharedGrantBackend(
            clients,
            self.release,
            self.cluster_authority,
            self.membership,
            self.services,
            recovery=getattr(self, "_recovering", False),
        )

    def create(self, *, binding, target, reservation, store, state_store):
        if not isinstance(store, SqlRegistrationStore):
            raise BootstrapRefused(
                "shared bootstrap requires the transactional registration store"
            )
        journal = AuthorityJournal(
            store.store,
            binding,
            target,
            generation_for(binding, reservation),
            claim_fingerprint(reservation.attempt_token),
        )
        backend = self._backend(binding, target, state_store)
        backend.journal = journal
        authority = SharedWorkspaceAuthority(journal, backend)
        clients = backend.clients
        clients.installer_access.tenant_identity_reader = (
            lambda: self.services.tenant_principals(authority)
        )
        return authority

    def recover(self, **kwargs):
        # Reuse the original claim/generation lookup and recovery fencing.
        self._recovering = True
        try:
            authority = super().recover(**kwargs)
        finally:
            self._recovering = False
        return SharedWorkspaceAuthority(authority.journal, authority.backend)
