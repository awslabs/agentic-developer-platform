"""Compose canonical management-observed bootstrap from verified worker credentials."""

import json
from dataclasses import replace
from pathlib import Path
import time

from .cluster_clients import kubernetes_client, scoped_entry_client, write_kubeconfig
from .credentials import assume_session, canonical_role_identity
from .network import OwnedNetworkObservations
from .process import AsyncBridgeStore, WorkerProcesses
from .runtime_config import LifecycleRefused


def bootstrap(
    operation, context, config, request, row, session, process, loop, verify, network
):
    from superplane_bootstrap.adapters import (
        AwsObserver,
        AwsPrerequisiteAccess,
        IamNodeRoleFacts,
        KubectlClusterAccess,
    )
    from superplane_bootstrap.authority_backend import BootstrapClients
    from superplane_bootstrap.authority_runtime import BootstrapAuthorityFactory
    from superplane_bootstrap.cli import (
        _cni_role_arn,
        _declared_proofs,
        _expected_prerequisites,
        _taint_key,
    )
    from superplane_bootstrap.components import WORKSPACE_CRDS
    from superplane_bootstrap.grant_plan import BootstrapRelease
    from superplane_bootstrap.management_observation import ManagementObservation
    from superplane_bootstrap.read_tokens import issue_bootstrap_read_token
    from superplane_bootstrap.registry import SqlRegistrationStore
    from superplane_bootstrap.state import FileStateStore
    from superplane_bootstrap.workspace import bootstrap_workspace
    from superplane_contracts.provisioning import OperationBinding, ResolvedPrincipal
    from superplane_contracts.secrets import assert_no_secret_material

    if row is None:
        raise LifecycleRefused(
            "bootstrap requires a separately reviewed discovery or apply artifact"
        )
    outputs = {
        key: value["value"]
        for key, value in json.loads(row["artifact_metadata_json"])["outputs"].items()
    }
    lease = operation.grant.lease
    if (
        outputs["account_id"],
        outputs["aws_region"],
        outputs["org_id"],
        outputs["workspace_id"],
    ) != (row["account_id"], request.region, lease.org_id, lease.workspace_id):
        raise LifecycleRefused(
            "bootstrap outputs differ from the original reviewed workspace"
        )
    binding = OperationBinding(
        operation_id=lease.operation_id,
        principal=ResolvedPrincipal(
            subject=lease.holder, org_id=lease.org_id, workspace_id=lease.workspace_id
        ),
        action="provision",
        permission="workspace:provision",
        expires_at=lease.runtime_deadline,
    )
    release = BootstrapRelease(
        namespace=config["namespace"],
        service_account="superplane-controller",
        controller="superplane-controller",
        enforce_version=config["enforce_version"],
        crds=tuple(WORKSPACE_CRDS),
    )
    artifact_root = Path(__file__).with_name("_data")
    crds = artifact_root / "crds.yaml"
    if not crds.is_file():
        crds = (
            Path(__file__).resolve().parents[1]
            / "src/superplane-controller/deploy/crds.yaml"
        )
    if not crds.is_file():
        raise LifecycleRefused("maintained CRD manifest is absent from worker image")
    bridge = AsyncBridgeStore(context.domain_connect, loop)
    store = SqlRegistrationStore(bridge)
    state = FileStateStore(process.directory / "bootstrap-state.json")
    observer = AwsObserver(process, outputs["aws_region"])
    roles = {
        actor: f"arn:aws:iam::{outputs['account_id']}:role/{name}"
        for actor, name in config["actor_role_names"].items()
    }

    def binding_resolver(operation_id, *, recovery=False):
        if operation_id != lease.operation_id:
            raise LifecycleRefused("bootstrap cannot resolve another operation")
        # Canonical normal revocation also requests recovery=True to select its
        # cleanup checks. This closure still requires the original live execution
        # grant; the flag cannot deliver or substitute a separate RecoveryGrant.
        verify()
        return binding

    def actor_process(actor):
        actor_session = assume_session(
            session, role_arn=roles[actor], region=request.region, verify=verify
        )
        directory = process.directory / actor
        directory.mkdir(mode=0o700)
        runner = WorkerProcesses(
            binaries=config["binaries"],
            directory=directory,
            session=actor_session,
            region=request.region,
            verify=verify,
        )
        return actor_session, runner

    sessions, runners = {}, {}
    for actor in ("registrar", "installer", "supervisor"):
        sessions[actor], runners[actor] = actor_process(actor)
    identities = {
        actor: (
            lambda actor=actor: canonical_role_identity(
                sessions[actor], session, roles[actor], verify=verify
            )
        )
        for actor in roles
    }
    manifests = {name: crds for name in WORKSPACE_CRDS}

    def access(actor):
        runner = runners[actor]
        return KubectlClusterAccess(
            runner=runner,
            kubeconfig=write_kubeconfig(runner, outputs, name="workspace"),
            controller_namespace=release.namespace,
            controller_service_account=release.service_account,
            controller_image=config["controller_image"],
            node_role=IamNodeRoleFacts(process, outputs["node_role_arn"]),
            manifests=manifests,
            imds_probe_image=config["imds_probe_image"],
        )

    installer, supervisor = access("installer"), access("supervisor")
    dynamic = {
        actor: kubernetes_client(runners[actor], outputs, name="workspace")
        for actor in ("registrar", "supervisor")
    }

    def resolve_clients(actual_binding, target, actual_release):
        if actual_binding != binding or actual_release != release:
            raise LifecycleRefused(
                "bootstrap client recipe differs from its original operation"
            )
        # The SDK mutator identity was privately delivered for this admission.
        # Narrow revocation sessions are assumed from that delivered role, never
        # from worker-selected role references or a local AWS profile.
        return BootstrapClients(
            binding=binding,
            target=target,
            principals=roles,
            binding_resolver=binding_resolver,
            identity_readers=identities,
            eks=session.client("eks", region_name=request.region),
            entry_client=scoped_entry_client(
                session._superplane_source_session,
                role_arn=session._superplane_role_arn,
                region=request.region,
                verify=verify,
                external_id=session._superplane_external_id,
            ),
            registrar_kubernetes=dynamic["registrar"],
            supervisor_kubernetes=dynamic["supervisor"],
            installer_access=installer,
            supervisor_access=supervisor,
        )

    class PollingObservation(ManagementObservation):
        def observe(self):
            from superplane_bootstrap.errors import BootstrapRefused

            deadline = time.monotonic() + 30
            while True:
                verify()
                try:
                    return super().observe()
                except BootstrapRefused:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)

    def observation(authority):
        def credential():
            verify()
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
            namespace=release.namespace,
            claim=authority.journal.claim,
        )

    factory = BootstrapAuthorityFactory(
        resolve_clients, release, resolve_observation=observation
    )
    try:
        verify()
        result = bootstrap_workspace(
            binding=binding,
            provider=observer.provider_identity(),
            access=installer,
            prerequisite_access=OwnedNetworkObservations(
                AwsPrerequisiteAccess(process, request.region), network
            ),
            store=store,
            authority_factory=factory,
            state_store=state,
            observed_cluster=observer.cluster_identity(outputs["cluster_name"]),
            expected_account_id=outputs["account_id"],
            expected_region=request.region,
            expected_cluster_name=outputs["cluster_name"],
            expected_cluster_arn=outputs["cluster_arn"],
            expected_certificate_authority_data=outputs[
                "cluster_certificate_authority_data"
            ],
            expected_cni_role_arn=_cni_role_arn(outputs),
            expected_prerequisites=replace(
                _expected_prerequisites(
                    outputs, config["management_security_group_id"]
                ),
                retained_sts_rule_id=outputs["sts_endpoint_rule_id"],
            ),
            cluster_ownership=request.cluster_ownership.value,
            namespace=release.namespace,
            enforce_version=release.enforce_version,
            credential_reference_id=config["bootstrap_credential_reference_id"],
            contract_version="v1",
            screen=assert_no_secret_material,
            controller_name=release.controller,
            required_system_workloads=release.system_workloads,
            declared_proofs=_declared_proofs(outputs) or None,
            taint_key=_taint_key(outputs),
        )
        verify()
        return result
    finally:
        factory.close()
        installer.close()
        supervisor.close()
        for client in dynamic.values():
            client.client.close()
