"""Production EKS/Kubernetes composition for the bootstrap authority journal."""

from __future__ import annotations

from dataclasses import dataclass
import json

from .components import BOOTSTRAP_OWNER_LABEL
from .eks_grants import EksGrants, _pages
from .errors import BootstrapRefused
from .grant_plan import compile_grants
from .kube_grants import KubeGrants
from .permissions import verify_install_permissions
from .state import NamespaceRecord, load_state, record
from .target import _binding_identity


@dataclass(frozen=True)
class BootstrapClients:
    """Clients delivered by the trusted operation/vault composer (#5534/#5535).

    The resolver reauthenticates the operation; identity readers perform authoritative
    STS/IAM reads using each actor's own credentials. Neither accepts request claims
    as authority. SDK clients and kubeconfigs never enter journal/registration JSON.
    """

    binding: object
    target: object
    principals: dict
    binding_resolver: object
    identity_readers: dict
    eks: object
    entry_client: object
    registrar_kubernetes: object
    supervisor_kubernetes: object
    installer_access: object
    supervisor_access: object

    def verify(self, *, recovery=False):
        from superplane_contracts.provisioning import OperationBinding

        current = self.binding_resolver(self.binding.operation_id, recovery=recovery)
        if not isinstance(current, OperationBinding) or (
            current.operation_id,
            current.principal,
            current.action,
            current.permission,
        ) != (
            self.binding.operation_id,
            self.binding.principal,
            self.binding.action,
            self.binding.permission,
        ):
            raise BootstrapRefused("bootstrap credential operation binding changed")
        if not recovery:
            _binding_identity(current)
        if (current.principal.org_id, current.principal.workspace_id) != (
            self.target.org_id,
            self.target.workspace_id,
        ):
            raise BootstrapRefused("bootstrap credential target differs")
        if set(self.identity_readers) != set(self.principals):
            raise BootstrapRefused(
                "bootstrap actor credential identities are incomplete"
            )
        for actor, reader in self.identity_readers.items():
            identity = reader()
            if (identity.account_id, identity.principal_arn) != (
                self.target.account_id,
                self.principals[actor],
            ):
                raise BootstrapRefused(
                    "bootstrap actor credentials belong to a different role"
                )
        if (
            getattr(getattr(self.eks, "meta", None), "region_name", None)
            != self.target.region
        ):
            raise BootstrapRefused("bootstrap EKS client targets a different region")
        cluster = self.eks.describe_cluster(name=self.target.cluster_name).get(
            "cluster", {}
        )
        if (
            cluster.get("arn"),
            cluster.get("name"),
            cluster.get("endpoint"),
            cluster.get("certificateAuthority", {}).get("data"),
        ) != (
            self.target.cluster_arn,
            self.target.cluster_name,
            self.target.endpoint,
            self.target.certificate_authority_data,
        ):
            raise BootstrapRefused("bootstrap EKS client observes a different target")
        if not recovery and cluster.get("status") != "ACTIVE":
            raise BootstrapRefused("bootstrap EKS target is not active")
        if (
            not recovery
            and cluster.get("accessConfig", {}).get("authenticationMode") != "API"
        ):
            raise BootstrapRefused(
                "bootstrap requires EKS API authentication; legacy aws-auth mappings are not covered by the tenant inventory"
            )


class BootstrapGrantBackend:
    def __init__(self, clients: BootstrapClients, release, state_store):
        self.clients, self.release, self.state_store = clients, release, state_store
        clients.verify()
        self.eks = EksGrants(
            clients.eks, clients.target, entry_client=clients.entry_client
        )
        self.registrar = KubeGrants(clients.registrar_kubernetes, clients.target)
        self.supervisor = KubeGrants(clients.supervisor_kubernetes, clients.target)
        self.journal = None
        self.registrar_removed = False
        for access in (clients.installer_access, clients.supervisor_access):
            access.bind_target(clients.target, clients.binding)

    def plan(self, journal):
        if (
            journal.target != self.clients.target
            or journal.binding != self.clients.binding
        ):
            raise BootstrapRefused("bootstrap clients do not match the reservation")
        self.journal = journal
        plan = compile_grants(
            journal,
            self.release,
            self.clients.principals,
            controller_mode=getattr(
                self.clients.installer_access, "controller_mode", "legacy"
            ),
        )
        # Only a completed, same-workspace journal can authorize adoption of an
        # existing operational supervisor. Tags or caller-provided ARNs cannot.
        with journal.fenced():
            rows = journal.store.execute(
                "SELECT plan_json, progress_json FROM workspace_bootstrap_authority "
                "WHERE workspace_id=:workspace_id AND org_id=:org_id AND cluster_arn=:cluster_arn AND revoked=true",
                {
                    "workspace_id": journal.target.workspace_id,
                    "org_id": journal.target.org_id,
                    "cluster_arn": journal.target.cluster_arn,
                },
            )
        prior = {}
        for row in rows:
            old, progress = (
                json.loads(row["plan_json"]),
                json.loads(row["progress_json"]),
            )
            if not progress.get("retain_workspace"):
                continue
            for spec in old["grants"]:
                if spec.get("lifetime") != "workspace":
                    continue
                identity = progress.get(spec["key"], {}).get("identity")
                if identity is None:
                    raise BootstrapRefused(
                        "workspace supervisor ownership is incomplete"
                    )
                candidate = {**spec, "adopted_identity": identity}
                if spec["key"] in prior and prior[spec["key"]] != candidate:
                    raise BootstrapRefused(
                        "workspace supervisor ownership is ambiguous"
                    )
                prior[spec["key"]] = candidate
        for i, spec in enumerate(plan["grants"]):
            old = prior.get(spec["key"])
            if old is not None:
                # Recompile the role against the old generation to compare exact
                # release permissions, names and subjects, not just object UIDs.
                from dataclasses import replace

                expected = next(
                    s
                    for s in compile_grants(
                        replace(journal, generation=old["generation"]),
                        self.release,
                        self.clients.principals,
                        controller_mode=getattr(
                            self.clients.installer_access, "controller_mode", "legacy"
                        ),
                    )["grants"]
                    if s["key"] == spec["key"]
                )
                if {
                    k: v for k, v in old.items() if k != "adopted_identity"
                } != expected:
                    raise BootstrapRefused(
                        "workspace supervisor release or principal changed"
                    )
                plan["grants"][i] = old
        return plan

    def _adapter(self, spec):
        if (
            not spec["kind"].startswith("eks-")
            and not self.registrar_removed
            and self.journal is not None
        ):
            # Recovery may start after the registrar policy was removed but before
            # the final journal write. Discover that state through EKS, not an
            # in-process flag whose value was lost with the old process.
            from .eks_grants import _absent

            try:
                policies = _pages(
                    self.clients.eks,
                    "list_associated_access_policies",
                    "associatedAccessPolicies",
                    clusterName=self.clients.target.cluster_name,
                    principalArn=self.clients.principals["registrar"],
                )
            except Exception as exc:
                if not _absent(exc):
                    raise
                policies = []
            from .grant_plan import ADMIN_POLICY

            self.registrar_removed = not any(
                p.get("policyArn") == ADMIN_POLICY for p in policies
            )
        return (
            self.eks
            if spec["kind"].startswith("eks-")
            else (self.supervisor if self.registrar_removed else self.registrar)
        )

    def observe(self, spec):
        if spec["key"] == "workspace-namespace":
            self._verify_controller_handover()
        return self._adapter(spec).observe(spec)

    def _verify_controller_handover(self):
        from .components import CONTROLLER_IMAGE_MARKER
        from .adapters import KubectlClusterAccess

        self.registrar._verify_transport()
        response = self.clients.registrar_kubernetes.resources.get(
            api_version="apps/v1", kind="Deployment"
        ).get()
        payload = response.to_dict() if hasattr(response, "to_dict") else response
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise BootstrapRefused("controller handover inventory was not answered")
        if payload.get("metadata", {}).get("continue"):
            raise BootstrapRefused("controller handover inventory is incomplete")
        controllers = [
            item
            for item in payload["items"]
            if any(
                CONTROLLER_IMAGE_MARKER in c.get("image", "")
                for c in item.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            )
        ]
        state = load_state(
            self.state_store,
            workspace_id=self.clients.target.workspace_id,
            cluster_arn=self.clients.target.cluster_arn,
        )
        if (
            controllers
            and getattr(self.clients.installer_access, "controller_mode", None)
            == "management"
        ):
            raise BootstrapRefused(
                "legacy workspace controller requires explicit handover before management bootstrap"
            )
        if controllers and isinstance(
            self.clients.installer_access, KubectlClusterAccess
        ):
            from .component_journal import (
                ComponentJournal,
                component_key,
                component_identity,
                merge_component_records,
            )

            if len(controllers) != 1:
                raise BootstrapRefused(
                    "multiple workspace controllers require explicit handover"
                )
            body = controllers[0]
            if (
                body.get("metadata", {}).get("namespace"),
                body.get("metadata", {}).get("name"),
            ) != (
                self.release.namespace,
                self.release.controller,
            ):
                raise BootstrapRefused(
                    "existing controller belongs to another workspace"
                )
            key = component_key(body)
            rows = self.journal.store.execute(
                "SELECT progress_json FROM workspace_bootstrap_authority "
                "WHERE workspace_id=:workspace_id AND org_id=:org_id AND cluster_arn=:cluster_arn",
                {
                    "workspace_id": self.clients.target.workspace_id,
                    "org_id": self.clients.target.org_id,
                    "cluster_arn": self.clients.target.cluster_arn,
                },
            )
            records = [
                candidate
                for row in rows
                if (
                    candidate := json.loads(row["progress_json"])
                    .get("components", {})
                    .get(key)
                )
            ]
            owned = merge_component_records(records)
            if owned is None or owned["phase"] not in {"owned", "intended"}:
                raise BootstrapRefused(
                    "existing controller has no durable creation ownership"
                )
            ComponentJournal._matches(body, owned["desired"])
            identity = component_identity(body)
            if (owned["phase"] == "owned" and identity != owned.get("identity")) or (
                owned["phase"] == "intended"
                and identity["creation"] != owned.get("creation")
            ):
                raise BootstrapRefused("existing controller immutable identity changed")
            # A response lost after Deployment creation leaves the old boolean
            # unset. The durable creation intent plus exact provider read repairs
            # that state; a name or image alone cannot authorize this handover.
            record(self.state_store, state, controller_installed=True)
            return
        if controllers and not (state.controller_installed and len(controllers) == 1):
            raise BootstrapRefused(
                "existing workspace controller requires completed handover before namespace creation"
            )

    def verify(self, spec, identity):
        if identity is None:
            raise BootstrapRefused("owned bootstrap grant is missing")
        self._adapter(spec).verify(spec, identity)
        if spec["key"] == "workspace-namespace":
            self._namespace_labels(spec, identity)
            state = load_state(
                self.state_store,
                workspace_id=self.clients.target.workspace_id,
                cluster_arn=self.clients.target.cluster_arn,
            )
            record(
                self.state_store,
                state,
                namespace=NamespaceRecord(self.release.namespace, identity["uid"]),
            )

    @staticmethod
    def _namespace_labels(spec, identity):
        required = spec["body"]["metadata"]["labels"]
        if any(
            identity.get("labels", {}).get(k) != v
            for k, v in required.items()
            if k != BOOTSTRAP_OWNER_LABEL
        ):
            raise BootstrapRefused(
                "existing workspace namespace admission labels differ"
            )

    def verify_adoption(self, spec, identity):
        if spec["key"] == "workspace-namespace":
            self._namespace_labels(spec, identity)
            return  # Existing namespace stays adopted even with a forged owner label.
        if spec.get("adopted_identity") != identity:
            raise BootstrapRefused("workspace access has no matching durable ownership")
        self.verify(spec, identity)

    def create(self, spec):
        self.clients.verify()
        return self._adapter(spec).create(spec)

    def delete(self, spec, identity):
        self.clients.verify(recovery=True)
        self._adapter(spec).delete(spec, identity)
        if spec["key"] == "registrar-entry":
            self.registrar_removed = True

    def verify_worker_binding(self):
        self.clients.verify()
        plan, progress = self.journal.read_locked()
        for spec in plan["grants"]:
            if spec.get("lifetime") == "resource":
                continue
            observed = self.observe(spec)
            if observed != progress.get(spec["key"], {}).get("identity"):
                raise BootstrapRefused(
                    "bootstrap grant identity or permissions changed"
                )
            self.verify(spec, observed)
            if spec["kind"] == "eks-entry":
                policies = _pages(
                    self.clients.eks,
                    "list_associated_access_policies",
                    "associatedAccessPolicies",
                    clusterName=self.clients.target.cluster_name,
                    principalArn=spec["principal_arn"],
                )
                allowed = [
                    s["policy_arn"]
                    for s in plan["grants"]
                    if s["kind"] == "eks-policy"
                    and s["principal_arn"] == spec["principal_arn"]
                ]
                if sorted(p.get("policyArn", "") for p in policies) != sorted(allowed):
                    raise BootstrapRefused(
                        "bootstrap EKS entry has unplanned additive policy authority"
                    )

    def verify_worker_permissions(self):
        self.verify_worker_binding()
        r, access = self.release, self.clients.installer_access
        verify_install_permissions(
            access, r.namespace, r.service_account, r.controller, r.crds
        )
        if getattr(access, "controller_mode", None) == "management":
            for verb, name in (("create", None), ("patch", r.controller)):
                if (
                    access.bootstrap_permission(
                        verb=verb,
                        resource="deployments.apps",
                        namespace=r.namespace,
                        name=name,
                    )
                    is not False
                ):
                    raise BootstrapRefused(
                        "management bootstrap installer can write workspace deployments"
                    )
        for actor in ("installer", "supervisor"):
            access = getattr(self.clients, actor + "_access")
            for verb, resource, namespace, name in (
                ("patch", "namespaces", None, r.namespace),
                ("create", "namespaces", None, None),
                ("get", "secrets", r.namespace, None),
                ("create", "pods/exec", r.namespace, None),
                ("impersonate", "users", None, None),
                (
                    "bind",
                    "clusterroles.rbac.authorization.k8s.io",
                    None,
                    "cluster-admin",
                ),
                (
                    "escalate",
                    "clusterroles.rbac.authorization.k8s.io",
                    None,
                    "cluster-admin",
                ),
            ):
                if (
                    access.bootstrap_permission(
                        verb=verb, resource=resource, namespace=namespace, name=name
                    )
                    is not False
                ):
                    raise BootstrapRefused(
                        "bootstrap actor has forbidden or unanswered permissions"
                    )
        self.verify_supervisor_permissions()

    def verify_supervisor_permissions(self):
        access = self.clients.supervisor_access
        policies = _pages(
            self.clients.eks,
            "list_associated_access_policies",
            "associatedAccessPolicies",
            clusterName=self.clients.target.cluster_name,
            principalArn=self.clients.principals["supervisor"],
        )
        if policies:
            raise BootstrapRefused(
                "workspace supervisor has unplanned EKS policy authority"
            )
        for verb, resource, namespace in (
            ("list", "clusterrolebindings.rbac.authorization.k8s.io", None),
            ("list", "clusterroles.rbac.authorization.k8s.io", None),
            ("create", "subjectaccessreviews.authorization.k8s.io", None),
            ("list", "rolebindings.rbac.authorization.k8s.io", self.release.namespace),
            ("list", "serviceaccounts", self.release.namespace),
            ("patch", "nodes", None),
        ):
            if (
                access.bootstrap_permission(
                    verb=verb, resource=resource, namespace=namespace
                )
                is not True
            ):
                raise BootstrapRefused(
                    "workspace supervisor permissions were not verified"
                )
        for verb, resource in (
            ("create", "pods"),
            ("create", "roles.rbac.authorization.k8s.io"),
            ("get", "secrets"),
            ("patch", "deployments.apps"),
        ):
            if (
                access.bootstrap_permission(
                    verb=verb, resource=resource, namespace=self.release.namespace
                )
                is not False
            ):
                raise BootstrapRefused(
                    "workspace supervisor retains installation authority"
                )

    def verify_revoked(self, plan, progress):
        self.clients.verify(recovery=True)
        retained = progress.get("retain_workspace") is True
        self.registrar_removed = True
        for spec in plan["grants"]:
            status = progress.get(spec["key"])
            if not status:
                continue
            if spec.get("lifetime") == "resource":
                continue
            if spec.get("lifetime") == "workspace" and retained:
                observed = self.observe(spec)
                if observed != status.get("identity"):
                    raise BootstrapRefused(
                        "retained workspace grant immutable identity changed"
                    )
                self.verify(spec, observed)
                continue
            if status.get("phase") == "adopted":
                continue
            # Kube deletion already had an immediate positive absence read before
            # deleting the parent registrar entry. On success, independently read
            # again through the surviving, bounded supervisor.
            if spec["kind"].startswith("eks-") or retained:
                if self.observe(spec) is not None:
                    raise BootstrapRefused("temporary bootstrap authority remains")
            if status.get("phase") != "revoked":
                raise BootstrapRefused("temporary grant revocation is unverified")
        # An EKS group must not be reachable through some other access entry.
        temporary_groups = {
            g
            for s in plan["grants"]
            if s["kind"] == "eks-entry" and s.get("lifetime") == "temporary"
            for g in s["groups"]
        }
        for principal in _pages(
            self.clients.eks,
            "list_access_entries",
            "accessEntries",
            clusterName=self.clients.target.cluster_name,
        ):
            response = self.clients.eks.describe_access_entry(
                clusterName=self.clients.target.cluster_name, principalArn=principal
            )
            entry = response.get("accessEntry", {})
            if entry.get("principalArn") != principal or not isinstance(
                entry.get("kubernetesGroups", []), list
            ):
                raise BootstrapRefused("final EKS access inventory is unanswered")
            if temporary_groups.intersection(entry.get("kubernetesGroups", [])):
                raise BootstrapRefused("temporary bootstrap group remains reachable")
        if retained:
            self.verify_supervisor_permissions()

    def _aws_inventory(self, service, action, *args):
        if service != "eks":
            raise BootstrapRefused("unexpected bootstrap inventory provider")
        values = dict(zip(args[::2], args[1::2], strict=True))
        if values.get("--cluster-name") != self.clients.target.cluster_name:
            raise BootstrapRefused("inventory targets a different cluster")
        params = {"clusterName": self.clients.target.cluster_name}
        if "--principal-arn" in values:
            params["principalArn"] = values["--principal-arn"]
        keys = {
            "list-identity-provider-configs": "identityProviderConfigs",
            "list-access-entries": "accessEntries",
            "list-associated-access-policies": "associatedAccessPolicies",
        }
        method = action.replace("-", "_")
        if action in keys:
            return {
                keys[action]: _pages(self.clients.eks, method, keys[action], **params)
            }
        if action == "describe-access-entry":
            return self.clients.eks.describe_access_entry(**params)
        raise BootstrapRefused("unexpected bootstrap inventory operation")

    def _registrar_exemption(self, principal, entry, policies):
        plan, progress = self.journal.read_locked()
        if (
            progress.get("phase") != "active"
            or principal != self.clients.principals["registrar"]
        ):
            raise BootstrapRefused("temporary registrar lifecycle is not active")
        specs = {s["key"]: s for s in plan["grants"]}
        spec = specs["registrar-entry"]
        if self.eks._entry_identity(spec, entry) != progress["registrar-entry"].get(
            "identity"
        ):
            raise BootstrapRefused("temporary registrar immutable identity differs")
        if len(policies) != 1 or self.eks.observe(
            specs["registrar-policy"]
        ) != progress["registrar-policy"].get("identity"):
            raise BootstrapRefused(
                "temporary registrar policy is not the journaled grant"
            )

    def tenant_principals(self, *, temporary=False):
        from .tenant_authorization import eks_tenant_principals

        return eks_tenant_principals(
            self._aws_inventory,
            self.clients.target.cluster_name,
            self.clients.principals["registrar"] if temporary else "",
            registrar_authority=self._registrar_exemption if temporary else None,
        )

    def workspace_prerequisite(self):
        from .inventory import OwnedPrerequisite, ADP_CREATED
        from .prerequisites import ACCESS_ENTRY

        plan, progress = self.journal.read_locked()
        spec = next(s for s in plan["grants"] if s["key"] == "supervisor-entry")
        identity = self.observe(spec)
        if identity != progress[spec["key"]].get("identity"):
            raise BootstrapRefused("workspace supervisor entry changed")
        self.verify(spec, identity)
        return OwnedPrerequisite(
            kind=ACCESS_ENTRY,
            identifier=identity["arn"],
            workspace_id=self.clients.target.workspace_id,
            ownership=ADP_CREATED,
            reason="Service-owned bounded RBAC supervisor; exact grants and immutable UIDs recorded in the bootstrap authority journal",
        )
