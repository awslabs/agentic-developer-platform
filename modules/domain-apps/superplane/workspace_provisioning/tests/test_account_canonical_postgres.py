"""New-account admission through real canonical bootstrap and terminal SQL anchor.

Only cloud, process and HTTP transports are doubled. The creation/bootstrap
producers, RPC/executor, artifacts, canonical bootstrap and grant/registration
journals run unchanged against disposable remote PostgreSQL.
"""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
from uuid import UUID

from harness_jobs import (
    OperationFacadeService,
    OperationStore,
    REQUIRED_PERMISSION,
    ResolvedPrincipal,
)
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import decode_payload
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
from workspace_bootstrap.tests.test_cli import _outputs
from workspace_bootstrap.tests.test_integration import _FakeCluster
from workspace_provisioning import account_runtime, bootstrap_runtime, runtime
from workspace_provisioning.artifacts import (
    canonical,
    continuation_parameters,
    read_artifact,
)
from workspace_provisioning.process import AsyncBridgeStore

from .postgres_bridge import requires_harness_postgres
from .test_account_bootstrap_postgres import Child
from .test_account_creation_postgres import AccountScenario
from .test_bootstrap_runtime_postgres import bootstrap_harness as canonical_harness
from .test_retirement_execution_postgres import _Approves, _Resolver

pytestmark = requires_harness_postgres
bootstrap_harness = canonical_harness


class CanonicalScenario(AccountScenario):
    def __init__(self, harness, tmp_path, monkeypatch):
        super().__init__(harness, tmp_path, monkeypatch)
        self.child_account_id = identities.ACCOUNT_ID
        self.request = replace(self.request, workspace_id=identities.WORKSPACE_ID)
        self.authorization = replace(
            self.authorization,
            workspace_id=identities.WORKSPACE_ID,
            operation_org_id=identities.ORG_ID,
        )
        self.principal = ResolvedPrincipal(
            identities.ORG_ID,
            identities.WORKSPACE_ID,
            "fixture-original-worker",
            frozenset({REQUIRED_PERMISSION}),
        )
        self.facade = OperationFacadeService(
            connect=harness.connect,
            resolver=_Resolver(self.principal),
            approvals=_Approves(),
            ledger=self.ledger,
        )
        self.policy["runtime"].update(
            namespace=identities.NAMESPACE,
            enforce_version=identities.ENFORCE_VERSION,
            management_security_group_id=identities.MANAGEMENT_SG_ID,
            bootstrap_credential_reference_id=identities.CREDENTIAL_ID,
        )
        self.revise()
        describe = self.management.describe_create_account_status

        def creation_status(**arguments):
            result = describe(**arguments)
            result["CreateAccountStatus"]["AccountName"] = (
                "adp-" + self.request.workspace_id
            )
            return result

        self.management.describe_create_account_status = creation_status

        def assume(source, *, role_arn, region, verify, external_id=None):
            verify()
            self.assumed_roles.append(role_arn)
            if source is self.context.base_session:
                assert role_arn == "arn:aws:iam::000000000001:role/provider"
                return self.management
            assert source is self.management
            assert role_arn in {
                f"arn:aws:iam::{identities.ACCOUNT_ID}:role/OrganizationAccountAccessRole",
                f"arn:aws:iam::{identities.ACCOUNT_ID}:role/provider",
            }
            self.child._superplane_source_session = self.management
            self.child._superplane_role_arn = role_arn
            self.child._superplane_external_id = None
            return self.child

        monkeypatch.setattr("workspace_provisioning.credentials.assume_session", assume)
        monkeypatch.setattr(account_runtime, "assume_session", assume)

    async def admit(self, parameters):
        self.loop = asyncio.get_running_loop()
        progress = await self.facade.open_operation(
            action="provision",
            workspace_id=identities.WORKSPACE_ID,
            org_id=identities.ORG_ID,
            permission=REQUIRED_PERMISSION,
            parameters=parameters,
        )
        lease = await self.harness.lease(progress.operation_id)
        async with self.harness.connect() as connection:
            record = await OperationStore().get(
                connection, self.principal, progress.operation_id
            )
        operation = SimpleNamespace(
            grant=ExecutionGrant(replace(self.principal, subject=lease.holder), lease),
            job_id=record.job_id,
            plan_digest=record.plan_digest,
            request_payload=record.request_payload,
            request=decode_payload(record.request_payload),
            reservation_state="confirmed",
            max_runtime_seconds=900,
        )
        self.operations[progress.operation_id] = operation
        return operation

    async def row(self, artifact_id):
        return await read_artifact(
            self.harness.connect,
            artifact_id=artifact_id,
            org_id=identities.ORG_ID,
            workspace_id=identities.WORKSPACE_ID,
        )


def install_infrastructure(scenario):
    outputs = {key: value["value"] for key, value in _outputs().items()}
    outputs.update(
        cluster_endpoint=identities.ENDPOINT,
        workspace_api_security_group_id=identities.CLUSTER_SG_ID,
        sts_endpoint_rule_id=identities.MANAGEMENT_RULE_ID,
        sts_endpoint_id="vpce-00000000000000000",
    )
    outputs["tenant_scheduling_prerequisites"].update(
        required_proofs=list(required_proofs()), cni_addon_version="v1.19.2-eksbuild.1"
    )
    outputs["workspace_node_group"] = {
        "name": "nodes",
        "arn": identities.CLUSTER_ARN.replace(":cluster/", ":nodegroup/")
        + "/nodes/fixture",
        "launch_template_id": "lt-00000000000000000",
        "launch_template_version": "2",
    }
    scenario.outputs = {key: {"value": value} for key, value in outputs.items()}
    rules = [
        {
            "SecurityGroupRuleId": identities.MANAGEMENT_RULE_ID,
            "GroupId": outputs["sts_endpoint_security_group_id"],
            "GroupOwnerId": identities.ACCOUNT_ID,
            "IsEgress": False,
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "ReferencedGroupInfo": {
                "GroupId": outputs["workspace_node_security_group_id"],
                "UserId": identities.ACCOUNT_ID,
            },
        }
    ]

    class Infrastructure:
        def get_caller_identity(self):
            return {"Account": identities.ACCOUNT_ID}

        def describe_cluster(self, *, name):
            assert name == identities.CLUSTER_NAME
            return {
                "cluster": {
                    "name": name,
                    "arn": identities.CLUSTER_ARN,
                    "endpoint": identities.ENDPOINT,
                    "certificateAuthority": {"data": identities.CA_DATA},
                    "status": "ACTIVE",
                    "accessConfig": {"authenticationMode": "API"},
                    "resourcesVpcConfig": {
                        "vpcId": outputs["vpc_id"],
                        "clusterSecurityGroupId": outputs[
                            "workspace_node_security_group_id"
                        ],
                        "securityGroupIds": [
                            outputs["workspace_api_security_group_id"]
                        ],
                    },
                }
            }

        def list_nodegroups(self, **kwargs):
            return {"nodegroups": ["nodes"]}

        def describe_nodegroup(self, **kwargs):
            return {
                "nodegroup": {
                    "nodegroupName": "nodes",
                    "nodegroupArn": outputs["workspace_node_group"]["arn"],
                    "clusterName": identities.CLUSTER_NAME,
                    "status": "ACTIVE",
                    "nodeRole": outputs["node_role_arn"],
                    "launchTemplate": {"id": "lt-00000000000000000", "version": "2"},
                    "taints": [
                        {"key": identities.BOOTSTRAP_TAINT_KEY, "effect": "NO_SCHEDULE"}
                    ],
                }
            }

        def describe_addon(self, **kwargs):
            return {
                "addon": {
                    "status": "ACTIVE",
                    "serviceAccountRoleArn": identities.CNI_ROLE_ARN,
                    "addonVersion": "v1.19.2-eksbuild.1",
                }
            }

        def describe_launch_template_versions(self, **kwargs):
            return {
                "LaunchTemplateVersions": [
                    {
                        "LaunchTemplateId": "lt-00000000000000000",
                        "VersionNumber": 2,
                        "LaunchTemplateData": {
                            "MetadataOptions": {
                                "HttpTokens": "required",
                                "HttpPutResponseHopLimit": 1,
                            }
                        },
                    }
                ]
            }

        def describe_vpc_endpoints(self, **kwargs):
            return {
                "VpcEndpoints": [
                    {
                        "VpcEndpointId": outputs["sts_endpoint_id"],
                        "OwnerId": identities.ACCOUNT_ID,
                        "VpcId": outputs["sts_endpoint_vpc_id"],
                        "State": "available",
                        "ServiceName": "com.amazonaws.us-east-1.sts",
                        "VpcEndpointType": "Interface",
                        "PrivateDnsEnabled": True,
                        "Groups": [
                            {"GroupId": outputs["sts_endpoint_security_group_id"]}
                        ],
                    }
                ]
            }

        def describe_security_groups(self, *, GroupIds):
            return {
                "SecurityGroups": [
                    {
                        "GroupId": group,
                        "OwnerId": identities.ACCOUNT_ID,
                        "VpcId": outputs["vpc_id"],
                    }
                    for group in GroupIds
                ]
            }

        def describe_security_group_rules(self, *, Filters):
            return {
                "SecurityGroupRules": deepcopy(
                    [rule for rule in rules if rule["GroupId"] in Filters[0]["Values"]]
                )
            }

        def authorize_security_group_ingress(self, **arguments):
            rule = {
                "SecurityGroupRuleId": identities.ENDPOINT_RULE_ID,
                "GroupId": arguments["GroupId"],
                "GroupOwnerId": identities.ACCOUNT_ID,
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "ReferencedGroupInfo": arguments["IpPermissions"][0][
                    "UserIdGroupPairs"
                ][0],
                "Tags": arguments["TagSpecifications"][0]["Tags"],
            }
            assert not any(
                item["SecurityGroupRuleId"] == identities.ENDPOINT_RULE_ID
                for item in rules
            )
            rules.append(rule)
            scenario.creates.append(arguments)
            return {"SecurityGroupRules": [deepcopy(rule)]}

        def __getattr__(self, name):
            return getattr(scenario.cloud, name)

    class Provider(Child):
        actor = "provider"

        def client(self, service, **kwargs):
            return (
                Infrastructure()
                if service in {"sts", "eks", "ec2"}
                else super().client(service, **kwargs)
            )

    scenario.child = scenario.session = Provider(scenario)
    return outputs


def install_canonical_transports(scenario, operation, outputs, tmp_path, monkeypatch):
    cluster = _FakeCluster()
    config, lease = scenario.policy["runtime"], operation.grant.lease
    bridge = AsyncBridgeStore(scenario.harness.connect, asyncio.get_running_loop())
    binding = OperationBinding(
        operation_id=lease.operation_id,
        principal=DomainPrincipal(
            subject=lease.holder, org_id=lease.org_id, workspace_id=lease.workspace_id
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
    factory = compose_authority(
        cluster,
        tmp_path,
        {
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
        },
    )
    transport = factory.resolve_clients(None)
    scenario.cloud = cloud = cluster.authority_cloud
    terraform_process = runtime.WorkerProcesses

    class Actor:
        def __init__(self, actor):
            self.actor = actor

        def client(self, service, **kwargs):
            assert service == "sts"
            return self

        def get_caller_identity(self):
            return {
                "Account": identities.ACCOUNT_ID,
                "UserId": scenario.child.roles[self.actor]["RoleId"]
                + ":fixture-session",
            }

    def assume(source, *, role_arn, region, verify):
        assert source is scenario.child and region == identities.REGION
        actor = role_arn.rsplit("/", 1)[1]
        assert role_arn == scenario.child.roles[actor]["Arn"]
        verify()
        return Actor(actor)

    class Process:
        def __init__(self, *, binaries, directory, session, region, verify):
            self.binaries, self.directory, self.verify = binaries, directory, verify
            self.actor = session.actor
            self.runner = (
                getattr(transport, self.actor + "_access").runner
                if self.actor in {"installer", "supervisor"}
                else cluster
            )
            self.terraform = (
                terraform_process(
                    binaries=binaries,
                    directory=directory,
                    session=session,
                    region=region,
                    verify=verify,
                )
                if self.actor == "provider"
                else None
            )

        def checked(self, *args, **kwargs):
            return self.terraform.checked(*args, **kwargs)

        def run(self, argv, **kwargs):
            self.verify()
            return self.runner.run(argv, **kwargs)

    def dynamic(process, actual_outputs, *, name):
        assert actual_outputs == outputs
        certificate = process.directory / (name + ".ca.pem")
        certificate.write_bytes(base64.b64decode(identities.CA_DATA))
        client = cloud.kube(
            f"arn:aws:iam::{identities.ACCOUNT_ID}:role/{process.actor}", certificate
        )
        client.client.close = lambda: None
        return client

    def scoped(source, *, role_arn, region, verify, external_id):
        assert source is scenario.management
        assert role_arn == f"arn:aws:iam::{identities.ACCOUNT_ID}:role/provider"
        verify()
        return cloud.entry_client

    class Http:
        def open(self, request, timeout):
            assert request.get_header("Authorization").startswith("sp-bootstrap-read-")
            claim = bridge.execute(
                "SELECT claim FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id AND operation_id=:operation_id",
                {
                    "workspace_id": identities.WORKSPACE_ID,
                    "operation_id": lease.operation_id,
                },
            )[0]["claim"]
            now = datetime.now(UTC)
            return BytesIO(
                canonical(
                    {
                        "workspace_id": identities.WORKSPACE_ID,
                        "org_id": identities.ORG_ID,
                        "operation_id": lease.operation_id,
                        "cluster_arn": identities.CLUSTER_ARN,
                        "namespace": identities.NAMESPACE,
                        "registration_claim": claim,
                        "registry_ready": True,
                        "target_status": "observed_execution_unavailable",
                        "last_reconciled": now.isoformat(),
                        "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
                    }
                ).encode()
            )

    monkeypatch.setattr(runtime, "WorkerProcesses", Process)
    monkeypatch.setattr(bootstrap_runtime, "WorkerProcesses", Process)
    monkeypatch.setattr(bootstrap_runtime, "assume_session", assume)
    monkeypatch.setattr(bootstrap_runtime, "kubernetes_client", dynamic)
    monkeypatch.setattr(bootstrap_runtime, "scoped_entry_client", scoped)
    monkeypatch.setattr(
        "superplane_bootstrap.management_observation.build_opener", lambda *args: Http()
    )


def test_new_account_reaches_real_canonical_registration_and_terminal_anchor(
    bootstrap_harness, tmp_path, monkeypatch
):
    # Capture the maintained composer before the older boundary-only fixture installs
    # its intentional refusal. The final phase below restores this actual function.
    real_bootstrap = bootstrap_runtime.bootstrap
    scenario = CanonicalScenario(bootstrap_harness, tmp_path, monkeypatch)
    outputs = install_infrastructure(scenario)

    async def run():
        creation, row = await scenario.created()
        account = await scenario.admit(continuation_parameters(row))
        result = await account_runtime.run_account_bootstrap(account, scenario.context)
        row = await scenario.row(result["artifact_id"])
        prepare = await scenario.admit(continuation_parameters(row))
        result = await account_runtime.run_account_infrastructure(
            prepare, scenario.context
        )
        row = await scenario.row(result["artifact_id"])
        apply = await scenario.admit(continuation_parameters(row))
        result = await account_runtime.run_account_infrastructure(
            apply, scenario.context
        )
        row = await scenario.row(result["artifact_id"])
        final = await scenario.admit(continuation_parameters(row))
        install_canonical_transports(scenario, final, outputs, tmp_path, monkeypatch)
        monkeypatch.setattr(bootstrap_runtime, "bootstrap", real_bootstrap)
        result = await account_runtime.run_account_infrastructure(
            final, scenario.context
        )
        assert result["status"] == "ready"
        terminal = await scenario.row(result["result_artifact_id"])
        metadata = json.loads(terminal["artifact_metadata_json"])
        assert metadata["next_phase"] == "complete"
        assert (
            metadata["created_account_registration"]["operation_id"]
            == creation.grant.lease.operation_id
        )
        assert (
            metadata["bootstrap_anchor"]["registration"]["account_id"]
            == identities.ACCOUNT_ID
        )
        assert len(scenario.accounts) == 1 and len(scenario.operations) == 5
        async with bootstrap_harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_operations WHERE state='succeeded'"
                )
                == 5
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspaces WHERE id=$1",
                    UUID(identities.WORKSPACE_ID),
                )
                == 1
            )
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
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 5
            )
        assert metadata["bootstrap_anchor"]["current_generation"]

    bootstrap_harness.run(run())
