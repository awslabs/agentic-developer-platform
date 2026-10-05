"""Installed issuer transport composition for namespace-only workspace bootstrap.

The API capability stays disabled until deployment supplies SharedRuntimeHooks.
Hooks are service objects; lifecycle input/configuration cannot construct them.
"""

from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from superplane_bootstrap.errors import BootstrapRefused

from .process import AsyncBridgeStore, WorkerProcesses
from .runtime_config import LifecycleRefused


@dataclass(frozen=True)
class SharedRuntimeHooks:
    management_source_session: object

    def __post_init__(self):
        if self.management_source_session is None:
            raise LifecycleRefused(
                "shared runtime requires installed management transport"
            )


def bootstrap(
    operation,
    context,
    config,
    request,
    row,
    session,
    process,
    loop,
    verify,
    membership,
    hooks,
):
    from superplane_bootstrap.adapters import (
        AwsObserver,
        IamNodeRoleFacts,
        KubectlClusterAccess,
    )
    from superplane_bootstrap.authority_backend import BootstrapClients
    from superplane_bootstrap.cli import _cni_role_arn, _declared_proofs
    from superplane_bootstrap.components import WORKSPACE_CRDS
    from superplane_bootstrap.grant_plan import BootstrapRelease
    from superplane_bootstrap.management_observation import ManagementObservation
    from superplane_bootstrap.read_tokens import issue_bootstrap_read_token
    from superplane_bootstrap.registry import SqlRegistrationStore
    from superplane_bootstrap.shared_authority import SharedBootstrapAuthorityFactory
    from superplane_bootstrap.state import FileStateStore
    from superplane_bootstrap.workspace import bootstrap_workspace
    from superplane_contracts.provisioning import OperationBinding, ResolvedPrincipal
    from superplane_contracts.secrets import assert_no_secret_material

    from .cluster_clients import write_kubeconfig
    from .credential_controller.registry import load_authority
    from .credential_controller.transports import compose_transports
    from .credentials import assume_session, canonical_role_identity
    from .shared_bootstrap_credentials import SharedCredentialServices
    from .shared_dependencies import SharedDependencyVerifier
    from .shared_tenant_inventory import installed_tenant_principals

    if not isinstance(hooks, SharedRuntimeHooks) or row is None:
        raise LifecycleRefused("shared bootstrap has no installed runtime composition")
    bridge = AsyncBridgeStore(context.domain_connect, loop)
    lease = operation.grant.lease
    outputs = {
        key: value["value"]
        for key, value in json.loads(row["artifact_metadata_json"])["outputs"].items()
    }
    binding = OperationBinding(
        operation_id=lease.operation_id,
        principal=ResolvedPrincipal(
            subject=lease.holder, org_id=lease.org_id, workspace_id=lease.workspace_id
        ),
        action="provision",
        permission="workspace:provision",
        expires_at=lease.runtime_deadline,
    )

    async def installed():
        if bridge.connection is not None:
            return await load_authority(
                bridge.connection, membership.org_id, membership.cluster_id
            )
        async with context.domain_connect() as connection:
            return await load_authority(
                connection, membership.org_id, membership.cluster_id
            )

    verify()
    installation = bridge.wait(installed())
    if installation.document.get("version") != 2:
        raise LifecycleRefused(
            "shared bootstrap requires installed version-2 dependency pins"
        )
    target = installation.target(lease.workspace_id)
    expected = {
        "account_id": target.account_id,
        "aws_region": target.region,
        "cluster_name": target.cluster_name,
        "cluster_arn": target.cluster_arn,
        "cluster_endpoint": target.endpoint,
        "cluster_certificate_authority_data": target.certificate_authority_data,
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
    }
    if any(outputs.get(key) != value for key, value in expected.items()) or (
        membership.cluster_arn != target.cluster_arn
        or membership.endpoint != target.endpoint
        or membership.org_id != lease.org_id
        or membership.workspace_id != lease.workspace_id
        or row["account_id"] != target.account_id
        or request.region != target.region
    ):
        raise LifecycleRefused(
            "shared discovery differs from the installed cluster target"
        )

    def current():
        verify()
        if bridge.wait(installed()) != installation:
            raise BootstrapRefused("installed shared bootstrap authority changed")

    temporary = TemporaryDirectory(prefix="shared-bootstrap-", dir=process.directory)
    directory = Path(temporary.name)
    issuer = management = access = factory = None
    try:
        issuer, management = compose_transports(
            installation,
            session,
            current,
            directory,
            lease.workspace_id,
            management_source_session=hooks.management_source_session,
        )
        reference = installation.cluster_reference()
        actor_session = assume_session(
            session,
            role_arn=reference.principal_arn,
            region=request.region,
            verify=current,
        )
        runner = WorkerProcesses(
            binaries=config["binaries"],
            directory=directory,
            session=actor_session,
            region=request.region,
            verify=current,
        )
        release = BootstrapRelease(
            membership.namespace,
            "member-reader",
            "superplane-controller",
            config["enforce_version"],
            tuple(WORKSPACE_CRDS),
        )
        access = KubectlClusterAccess(
            runner=runner,
            kubeconfig=write_kubeconfig(runner, outputs, name="member"),
            controller_namespace=membership.namespace,
            controller_service_account=release.service_account,
            controller_image=config["controller_image"],
            node_role=IamNodeRoleFacts(process, outputs["node_role_arn"]),
            manifests={},
            imds_probe_image=config["imds_probe_image"],
        )
        dependencies = SharedDependencyVerifier(
            installation, issuer, management, bridge, process, config, current
        )
        services = SharedCredentialServices(
            installation,
            issuer,
            management,
            bridge,
            directory,
            current,
            dependencies,
            installed_tenant_principals,
        )

        def resolve_binding(operation_id, *, recovery=False):
            if operation_id != lease.operation_id:
                raise BootstrapRefused(
                    "shared bootstrap cannot resolve another operation"
                )
            current()
            return binding

        def resolve_clients(actual_binding, actual_target, actual_release):
            if actual_binding != binding or actual_release != release:
                raise BootstrapRefused(
                    "shared client recipe differs from admitted operation"
                )
            current()
            return BootstrapClients(
                binding,
                actual_target,
                {"registrar": reference.principal_arn},
                resolve_binding,
                {
                    "registrar": lambda: canonical_role_identity(
                        actor_session, session, reference.principal_arn, verify=current
                    )
                },
                actor_session.client("eks", region_name=request.region),
                None,
                issuer.client,
                issuer.client,
                access,
                access,
            )

        class PollingObservation(ManagementObservation):
            def observe(self):
                deadline = time.monotonic() + 30
                while True:
                    current()
                    try:
                        return super().observe()
                    except BootstrapRefused:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(1)

        def observation(authority):
            def credential():
                current()
                return bridge.wait(
                    issue_bootstrap_read_token(
                        connect=context.connect,
                        domain_connect=context.domain_connect,
                        grant=operation.grant,
                    )
                )

            return PollingObservation(
                origin=config["management_api_origin"],
                credential=credential,
                binding=binding,
                target=authority.journal.target,
                namespace=membership.namespace,
                claim=authority.journal.claim,
            )

        factory = SharedBootstrapAuthorityFactory(
            resolve_clients,
            release,
            membership=membership,
            cluster_authority=reference,
            services=services.services(),
            resolve_observation=observation,
        )
        observer = AwsObserver(process, request.region)
        result = bootstrap_workspace(
            binding=binding,
            provider=observer.provider_identity(),
            observed_cluster=observer.cluster_identity(target.cluster_name),
            access=access,
            prerequisite_access=None,
            store=SqlRegistrationStore(bridge),
            authority_factory=factory,
            state_store=FileStateStore(process.directory / "bootstrap-state.json"),
            expected_account_id=target.account_id,
            expected_region=target.region,
            expected_cluster_name=target.cluster_name,
            expected_cluster_arn=target.cluster_arn,
            expected_certificate_authority_data=target.certificate_authority_data,
            expected_cni_role_arn=_cni_role_arn(outputs),
            expected_prerequisites=None,
            cluster_ownership="adopted",
            namespace=membership.namespace,
            enforce_version=release.enforce_version,
            credential_reference_id="membership:" + membership.generation,
            contract_version="v1",
            screen=assert_no_secret_material,
            declared_proofs=_declared_proofs(outputs) or None,
            membership=membership,
        )
        current()
        return result
    finally:
        if factory is not None:
            factory.close()
        if access is not None:
            access.close()
        for grants in (issuer, management):
            if grants is not None:
                grants.client.client.close()
        temporary.cleanup()
