"""Production bootstrap composer, real SQL/grants/read tokens, doubled transports.

The stateful AWS/Kubernetes transport comes from the canonical bootstrap suite;
none of its authority, registration, component, readiness or isolation machinery
is replaced. HTTP supplies a fresh manager observation under the issued token.
"""

import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from harness_jobs import OperationFacadeService, REQUIRED_PERMISSION, ResolvedPrincipal
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.leases import lock_lease
from superplane_bootstrap.access import ProviderIdentity
from superplane_bootstrap.adapters import (
    AwsObserver,
    IamNodeRoleFacts,
    KubectlClusterAccess,
)
from superplane_bootstrap.admission import required_proofs
from superplane_contracts.provisioning import (
    OperationBinding,
    ResolvedPrincipal as DomainPrincipal,
)

from workspace_bootstrap.tests import conftest as identities
from workspace_bootstrap.tests.authority_integration_support import compose_authority
from workspace_bootstrap.tests.postgres_support import render_migration_ddl
from workspace_bootstrap.tests.test_cli import _outputs
from workspace_bootstrap.tests.test_integration import _FakeCluster
from workspace_provisioning import bootstrap_runtime
from workspace_provisioning.artifacts import canonical
from workspace_provisioning.bootstrap_result import (
    bootstrap_result_anchor,
    read_bootstrap_anchor,
)
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.process import AsyncBridgeStore

from .postgres_bridge import Harness, requires_harness_postgres
from .test_lifecycle_policy import runtime_config
from .test_retirement_execution_postgres import _Approves, _Ledger, _Resolver

pytestmark = requires_harness_postgres


@pytest.fixture
def bootstrap_harness(tmp_path_factory, request):
    with Harness.started(tmp_path_factory, request.node.name) as harness:
        ddl, _ = render_migration_ddl()

        async def migrate():
            async with harness.connect() as connection:
                await connection.execute(ddl)
                await connection.execute(
                    "INSERT INTO organizations (id,name,adp_org_id,billing_plan) VALUES ($1,'test-org',$2,'free')",
                    UUID(identities.ORG_ID),
                    identities.ORG_ID,
                )

        harness.run(migrate())
        yield harness


def test_production_bootstrap_composer_registers_with_retained_sts_and_revoked_grants(
    bootstrap_harness, tmp_path, monkeypatch
):
    harness = bootstrap_harness
    cluster = _FakeCluster()
    config = runtime_config()
    config.update(
        namespace=identities.NAMESPACE,
        enforce_version=identities.ENFORCE_VERSION,
        management_security_group_id=identities.MANAGEMENT_SG_ID,
        bootstrap_credential_reference_id=identities.CREDENTIAL_ID,
        controller_image="fixture/superplane-controller@sha256:" + "a" * 64,
    )
    outputs = {key: value["value"] for key, value in _outputs().items()}
    outputs.update(
        cluster_endpoint=identities.ENDPOINT,
        workspace_api_security_group_id=identities.CLUSTER_SG_ID,
        sts_endpoint_rule_id=identities.MANAGEMENT_RULE_ID,
    )
    outputs["tenant_scheduling_prerequisites"]["required_proofs"] = list(
        required_proofs()
    )
    evidence = {
        "management-api-rule": {
            "rule_id": identities.ENDPOINT_RULE_ID,
            "created": True,
        },
        "private-sts-rule": {
            "rule_id": identities.MANAGEMENT_RULE_ID,
            "created": False,
        },
    }

    async def run():
        principal = ResolvedPrincipal(
            identities.ORG_ID,
            identities.WORKSPACE_ID,
            "fixture-worker",
            frozenset({REQUIRED_PERMISSION}),
        )
        facade = OperationFacadeService(
            connect=harness.connect,
            resolver=_Resolver(principal),
            approvals=_Approves(),
            ledger=_Ledger(),
        )
        progress = await facade.open_operation(
            action="provision",
            workspace_id=identities.WORKSPACE_ID,
            org_id=identities.ORG_ID,
            permission=REQUIRED_PERMISSION,
            parameters={"idempotency_key": "canonical-bootstrap-composition"},
        )
        lease = await harness.lease(progress.operation_id)
        grant = ExecutionGrant(replace(principal, subject=lease.holder), lease)
        operation = SimpleNamespace(
            grant=grant,
            request=SimpleNamespace(
                parameters={
                    "lifecycle_inputs": json.dumps({"cluster_placement": "dedicated"})
                }
            ),
        )
        context = SimpleNamespace(
            connect=harness.connect, domain_connect=harness.connect
        )
        loop = asyncio.get_running_loop()
        bridge = AsyncBridgeStore(harness.connect, loop)
        binding = OperationBinding(
            operation_id=lease.operation_id,
            principal=DomainPrincipal(
                subject=lease.holder,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
            ),
            action="provision",
            permission="workspace:provision",
            expires_at=lease.runtime_deadline,
        )
        manifest = tmp_path / "fixture-crds.yaml"
        manifest.write_text("---\n")
        access = KubectlClusterAccess(
            runner=cluster,
            kubeconfig=tmp_path / "fixture.kubeconfig",
            controller_namespace=identities.NAMESPACE,
            controller_service_account="superplane-controller",
            controller_image=config["controller_image"],
            imds_probe_image=config["imds_probe_image"],
            node_role=IamNodeRoleFacts(cluster, outputs["node_role_arn"]),
            manifests={name: manifest for name in identities.WORKSPACE_CRDS},
        )
        observer = AwsObserver(cluster, identities.REGION)
        arguments = {
            "binding": binding,
            "provider": observer.provider_identity(),
            "observed_cluster": observer.cluster_identity(identities.CLUSTER_NAME),
            "expected_account_id": identities.ACCOUNT_ID,
            "expected_region": identities.REGION,
            "expected_cluster_name": identities.CLUSTER_NAME,
            "expected_cluster_arn": identities.CLUSTER_ARN,
            "expected_certificate_authority_data": identities.CA_DATA,
            "cluster_ownership": "adp-created",
            "access": access,
            "namespace": identities.NAMESPACE,
            "enforce_version": identities.ENFORCE_VERSION,
            "required_crds": identities.WORKSPACE_CRDS,
            "required_system_workloads": ("coredns",),
        }
        transport_factory = compose_authority(cluster, tmp_path, arguments)
        transport = transport_factory.resolve_clients(None)
        cloud = cluster.authority_cloud

        async def check():
            async with harness.connect() as connection, connection.transaction():
                assert await lock_lease(connection, lease)

        def verify():
            bridge.wait(check())

        class Session:
            def __init__(self, actor):
                self.actor = actor
                self._superplane_source_session = self
                self._superplane_role_arn = (
                    f"arn:aws:iam::{identities.ACCOUNT_ID}:role/provider"
                )
                self._superplane_external_id = None

            def client(self, service, **kwargs):
                assert service == "eks"
                return cloud

        provider = Session("provider")
        actors, credentials, requests = [], [], []

        def assume(source, *, role_arn, region, verify):
            assert source is provider and region == identities.REGION
            actor = role_arn.rsplit("/", 1)[1]
            assert actor in {"registrar", "installer", "supervisor"}
            verify()
            actors.append(actor)
            return Session(actor)

        def identity(session, source, role_arn, *, verify):
            assert source is provider and role_arn.endswith("/" + session.actor)
            verify()
            return ProviderIdentity(identities.ACCOUNT_ID, role_arn)

        class Process:
            def __init__(self, *, binaries, directory, session, region, verify):
                self.binaries, self.directory, self.verify = binaries, directory, verify
                self.actor = session.actor
                self.runner = (
                    getattr(transport, self.actor + "_access").runner
                    if self.actor in {"installer", "supervisor"}
                    else cluster
                )

            def run(self, argv, **kwargs):
                self.verify()
                return self.runner.run(argv, **kwargs)

        def dynamic(process, actual_outputs, *, name):
            assert actual_outputs == outputs
            certificate = process.directory / (name + ".ca.pem")
            certificate.write_bytes(base64.b64decode(identities.CA_DATA))
            client = cloud.kube(
                f"arn:aws:iam::{identities.ACCOUNT_ID}:role/{process.actor}",
                certificate,
            )
            client.client.close = lambda: None
            return client

        def scoped(source, *, role_arn, region, verify, external_id):
            assert source is provider and role_arn == provider._superplane_role_arn
            verify()
            return cloud.entry_client

        class Http:
            def open(self, request, timeout):
                credentials.append(request.get_header("Authorization"))
                requests.append(request.full_url)
                assert credentials[-1].startswith("sp-bootstrap-read-")
                row = bridge.execute(
                    "SELECT claim FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id AND operation_id=:operation_id",
                    {
                        "workspace_id": identities.WORKSPACE_ID,
                        "operation_id": lease.operation_id,
                    },
                )[0]
                now = datetime.now(UTC)
                return BytesIO(
                    canonical(
                        {
                            "workspace_id": identities.WORKSPACE_ID,
                            "org_id": identities.ORG_ID,
                            "operation_id": lease.operation_id,
                            "cluster_arn": identities.CLUSTER_ARN,
                            "namespace": identities.NAMESPACE,
                            "registration_claim": row["claim"],
                            "registry_ready": True,
                            "target_status": "observed_execution_unavailable",
                            "last_reconciled": now.isoformat(),
                            "lease_expires_at": (
                                now + timedelta(seconds=30)
                            ).isoformat(),
                        }
                    ).encode()
                )

        monkeypatch.setattr(bootstrap_runtime, "assume_session", assume)
        monkeypatch.setattr(bootstrap_runtime, "canonical_role_identity", identity)
        monkeypatch.setattr(bootstrap_runtime, "WorkerProcesses", Process)
        monkeypatch.setattr(bootstrap_runtime, "kubernetes_client", dynamic)
        monkeypatch.setattr(bootstrap_runtime, "scoped_entry_client", scoped)
        monkeypatch.setattr(
            "superplane_bootstrap.management_observation.build_opener",
            lambda *args: Http(),
        )
        directory = tmp_path / "worker"
        directory.mkdir(mode=0o700)
        process = Process(
            binaries=config["binaries"],
            directory=directory,
            session=provider,
            region=identities.REGION,
            verify=verify,
        )
        outcome = await asyncio.to_thread(
            bootstrap_runtime.bootstrap,
            operation,
            context,
            config,
            SimpleNamespace(
                region=identities.REGION,
                cluster_ownership=SimpleNamespace(value="adp-created"),
            ),
            {
                "account_id": identities.ACCOUNT_ID,
                "artifact_metadata_json": canonical(
                    {
                        "outputs": {
                            key: {"value": value} for key, value in outputs.items()
                        }
                    }
                ),
            },
            provider,
            process,
            loop,
            verify,
            evidence,
        )
        assert outcome.ready, repr(outcome.refusal)
        anchor = await bootstrap_result_anchor(operation, context, outcome)
        assert (
            anchor["registration"]["namespace_uid"]
            == outcome.registration.target.namespace_uid
        )
        assert len(anchor["authority"]) == 1
        assert anchor["current_generation"] == anchor["authority"][0]["generation"]
        assert actors == ["registrar", "installer", "supervisor"]
        assert requests and all(
            url.endswith("/bootstrap-observation") for url in requests
        )
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_bootstrap_reservations WHERE state='registered'"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_bootstrap_authority WHERE NOT revoked"
                )
                == 0
            )
            rows = await connection.fetch(
                "SELECT progress_json FROM workspace_bootstrap_authority"
            )
            assert rows and all(
                json.loads(row["progress_json"])["retain_workspace"] is True
                for row in rows
            )
            token = await connection.fetchrow(
                "SELECT token_hash FROM workspace_bootstrap_read_tokens"
            )
            assert token and all(
                value not in token["token_hash"] for value in credentials
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspaces WHERE id=$1",
                    UUID(identities.WORKSPACE_ID),
                )
                == 1
            )

        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE workspace_bootstrap_authority SET progress_json=jsonb_set(progress_json::jsonb,'{retain_workspace}','false'::jsonb)::text"
            )
        with pytest.raises(LifecycleRefused, match="retained component"):
            await read_bootstrap_anchor(
                context,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                registration=anchor["registration"],
                claim=anchor["authority"][0]["claim"],
            )

    harness.run(run())
