"""The gateway service role stays inside IAM's two hard limits.

``adp-<env>-role-gateway-service`` is written to by four stacks (platform/infra,
modules/gateway/infra, modules/agent-factory infra + webhook-ingress, and the
Superplane domain-app stack). IAM caps a role at 10,240 non-whitespace characters
of INLINE policy in aggregate and at 20 ATTACHED managed policies (the account
quota is already at the 20 maximum). On dev the inline aggregate reached 10,144
bytes and every gateway apply that touched an inline policy failed with
``LimitExceeded`` (#7944).

The gateway module's grants now live in three consolidated customer managed
policies (``modules/gateway/infra/gateway-role-policies.tf``). This test renders
them -- and the inline policies the other stacks still put on the role -- from the
``.tf`` source with ``hcl_policy_render`` against a fixture of realistic values,
and fails when:

  AC-01  any consolidated managed policy exceeds 6,144 characters, or the gateway
         module declares an unconditional inline policy on the role again;
  AC-02  the role's inline aggregate (platform + webhook-ingress, dev switches)
         exceeds 8,192 characters -- 2,048 of headroom under the 10,240 limit;
  AC-03  the attachments every stack declares for the role (plus the two live-only
         attachments on dev) can exceed 20, or a new attachment resource appears
         in one of those stacks without being classified here;
  AC-04  a statement from a retired inline policy is missing, or a Sid repeats
         inside a consolidated document.

Sizes are measured on the compact JSON ``jsonencode`` emits, which is what IAM
counts. The fixture uses the longest plausible values (``ap-southeast-2``,
``staging``, three pool accounts, every optional switch on) so the budget holds for
any environment, and a second fixture mirrors dev so the numbers in the PR are
reproducible. No AWS, no terraform binary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.infra import hcl_policy_render as hcl

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = GATEWAY_ROOT.parents[1]
GATEWAY_INFRA = GATEWAY_ROOT / "infra"
PLATFORM_EKS_TF = REPO_ROOT / "platform" / "infra" / "modules" / "eks" / "main.tf"
WEBHOOK_INGRESS_INFRA = REPO_ROOT / "modules" / "agent-factory" / "webhook-ingress" / "infra"
AGENT_FACTORY_INFRA = REPO_ROOT / "modules" / "agent-factory" / "infra"
SUPERPLANE_INFRA = REPO_ROOT / "modules" / "domain-apps" / "superplane" / "infra"
AUTOMATION_INFRA = REPO_ROOT / "platform" / "automation-infra"

MANAGED_POLICY_LIMIT = 6144
INLINE_AGGREGATE_LIMIT = 10240
INLINE_AGGREGATE_BUDGET = 8192
ATTACHED_LIMIT = 20
GROUPS = ("identity", "data", "agents")

KMS_KEY = "key/271615d4-2e8f-4b9d-af44-33427bf8e38d"


def gateway_fixture(environment: str, region: str, account: str, *, everything_on: bool) -> dict:
    kms = f"arn:aws:kms:{region}:{account}:{KMS_KEY}"
    return {
        "var.environment": environment,
        "var.aws_region": region,
        # dev: environments/dev/modules/gateway.tfvars + variables.tf defaults
        "var.enable_chat_logging": True,
        "var.chat_logging_scrub_level": "standard",
        "var.enable_rds_iam_auth": True,
        "var.rds_username": "bgadmin",
        "var.enable_redis": True,
        "var.enable_elasticache_iam_auth": True,
        "var.enable_agent_context_sqs": True,
        "var.enable_l5_security": everything_on,
        "var.agent_context_ingestion_queue_arn": "",
        "var.enable_mantle_passthrough": True,
        # off on dev today; on in the worst case
        "var.enable_xray_tracing": everything_on,
        "var.pool_account_arns": ["111111111111", "222222222222", "333333333333"] if everything_on else [],
        "var.crew_checkpoints_enabled": everything_on,
        "var.crew_checkpoint_kms_key_arn": kms if everything_on else "",
        "var.agentcore_memory_enabled": everything_on,
        "data.aws_region.current.name": region,
        "data.aws_caller_identity.current.account_id": account,
        "data.aws_partition.current.partition": "aws",
        "data.aws_kms_alias.webhook_secrets.target_key_arn": kms,
        "module.cognito.cognito_user_pool_id": f"{region}_AbCdEfGhI",
        "module.cognito.agent_clients_table_arn": f"arn:aws:dynamodb:{region}:{account}:table/bedrockgw-{environment}-agent-clients",
        "module.frontend_s3.bucket_arn": f"arn:aws:s3:::bedrockgw-{environment}-frontend-{account}",
        "module.s3_chat_logs[0].bucket_arn": f"arn:aws:s3:::bedrockgw-{environment}-chat-logs-{account}",
        "module.redis[0].replication_group_id": f"bedrockgw-{environment}-redis",
        "module.l5_security[0].work_queue_arn": f"arn:aws:sqs:{region}:{account}:adp-{environment}-l5-work",
        "module.redis[0].redis_iam_user_id": f"bedrockgw-{environment}-gateway-iam-user",
        "aws_dynamodb_table.identity_index.arn": f"arn:aws:dynamodb:{region}:{account}:table/adp-{environment}-identity-index",
        "aws_dynamodb_table.user_identity_index.arn": f"arn:aws:dynamodb:{region}:{account}:table/adp-{environment}-user-identity-index",
        "aws_kms_key.dynamodb.arn": kms,
        "aws_secretsmanager_secret.atlassian_mcp_config.arn": (
            f"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/connections/atlassian-mcp/config-ABC123"
        ),
        "aws_secretsmanager_secret.atlassian_mcp_client.arn": (
            f"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/connections/atlassian-mcp/client-ABC123"
        ),
        "aws_s3_bucket.crew_checkpoints[0].arn": f"arn:aws:s3:::adp-{environment}-crew-checkpoints-{account}",
        "aws_bedrockagentcore_memory.gateway_user_memory[0].arn": (
            f"arn:aws:bedrock-agentcore:{region}:{account}:memory/adp_{environment}_gateway_user_memory-AbCdEfGhIj"
        ),
        "local.cluster_name": f"adp-{environment}-eks-cluster",
        "local.worker_events_table": f"adp-{environment}-webhook-events",
        "local.worker_events_key": kms,
    }


def platform_fixture(environment: str, region: str, account: str, *, everything_on: bool) -> dict:
    # platform/infra/main.tf leaves every gateway feature switch off; only the
    # customer-connection role list is operator-supplied.
    customers = [
        f"arn:aws:iam::{n}:role/ADP-Agent-{environment}-customer-{i}" for i, n in enumerate(("444444444444", "555555555555", "666666666666"))
    ]
    return {
        "var.name_prefix": f"adp-{environment}",
        "var.environment": environment,
        "var.gateway_customer_role_arns": customers if everything_on else [],
        "var.enable_rds_iam_auth": False,
        "var.enable_elasticache_iam_auth": False,
        "var.redis_replication_group_id": "",
        "var.redis_iam_user_id": "",
        "var.rds_db_username": "bgadmin",
        "var.pool_account_arns": [],
        "var.cognito_user_pool_id": "",
        "var.chat_logs_bucket_arn": "",
        "var.enable_comprehend_pii": False,
        "data.aws_region.current.name": region,
        "data.aws_region.current.region": region,
        "data.aws_caller_identity.current.account_id": account,
        "aws_iam_role.gateway_service_irsa.id": f"adp-{environment}-role-gateway-service",
    }


def webhook_ingress_fixture(environment: str, region: str, account: str) -> dict:
    return {
        "var.environment": environment,
        "var.aws_region": region,
        "var.identity_index_table_arn": "",
        # environments/dev/modules/webhook-ingress.tfvars: managed policies for the
        # authority grants, so task-storage is an attachment rather than inline.
        "var.gateway_authority_managed_policies": True,
        "local.agent_authority_provisioned": True,
        "local.account_id": account,
        "aws_dynamodb_table.webhook_events.arn": f"arn:aws:dynamodb:{region}:{account}:table/adp-{environment}-webhook-events",
        "aws_dynamodb_table.agent_authority.arn": f"arn:aws:dynamodb:{region}:{account}:table/adp-{environment}-agent-authority",
        "aws_kms_key.dynamodb.arn": f"arn:aws:kms:{region}:{account}:{KMS_KEY}",
    }


WORST = ("staging", "ap-southeast-2", "123456789012")
DEV = ("dev", "us-east-1", "123456789012")


def gateway_role_reference(role_expression: str) -> bool:
    markers = ("gateway_service_irsa_role_name", "role-gateway-service", "gateway_service_irsa.id")
    return any(marker in role_expression for marker in markers)


def consolidated_documents(environment: str, region: str, account: str, *, everything_on: bool) -> dict[str, dict]:
    scope = hcl.Scope(gateway_fixture(environment, region, account, everything_on=everything_on), sorted(GATEWAY_INFRA.glob("*.tf")))
    statements = scope.local("gateway_role_policy_statements")
    return {group: {"Version": "2012-10-17", "Statement": statements[group]} for group in GROUPS}


def other_stack_inline_policies(environment: str, region: str, account: str, *, everything_on: bool) -> list[hcl.RenderedPolicy]:
    platform_scope = hcl.Scope(platform_fixture(environment, region, account, everything_on=everything_on), [PLATFORM_EKS_TF])
    webhook_scope = hcl.Scope(webhook_ingress_fixture(environment, region, account), sorted(WEBHOOK_INGRESS_INFRA.glob("*.tf")))
    rendered = hcl.render_role_policies([PLATFORM_EKS_TF], platform_scope, role_matches=gateway_role_reference)
    rendered += hcl.render_role_policies(sorted(WEBHOOK_INGRESS_INFRA.glob("*.tf")), webhook_scope, role_matches=gateway_role_reference)
    return rendered


# ---------------------------------------------------------------------------
# AC-01: the consolidated managed policies
# ---------------------------------------------------------------------------


class TestConsolidatedManagedPolicies:
    @pytest.mark.parametrize("target", [WORST, DEV], ids=["worst-case", "dev"])
    def test_each_group_is_under_the_managed_policy_limit(self, target):
        documents = consolidated_documents(*target, everything_on=target is WORST)
        sizes = {group: hcl.policy_size(document) for group, document in documents.items()}
        assert set(sizes) == set(GROUPS)
        over = {group: size for group, size in sizes.items() if size > MANAGED_POLICY_LIMIT}
        assert not over, f"managed policies over {MANAGED_POLICY_LIMIT} chars: {over} (all: {sizes})"

    def test_groups_have_headroom_for_growth(self):
        """A group within 1,024 characters of the limit needs to be split before the next grant."""
        documents = consolidated_documents(*WORST, everything_on=True)
        sizes = {group: hcl.policy_size(document) for group, document in documents.items()}
        tight = {group: size for group, size in sizes.items() if size > MANAGED_POLICY_LIMIT - 1024}
        assert not tight, f"consolidated policies within 1,024 chars of the limit: {tight}"

    def test_product_retirement_verifies_only_the_current_instance_role(self):
        documents = consolidated_documents(*DEV, everything_on=False)
        get_role = [statement for document in documents.values() for statement in document["Statement"] if "iam:GetRole" in statement["Action"]]
        assert get_role == [
            {
                "Sid": "ProductEvaluatorRoleIdentity",
                "Effect": "Allow",
                "Action": ["iam:GetRole"],
                "Resource": "arn:aws:iam::123456789012:role/adp-*-instance",
                "Condition": {"StringEquals": {"iam:ResourceTag/adp:managed-by": "evaluator-setup"}},
            },
            {
                "Sid": "ProductLegacyEvaluatorRoleIdentity",
                "Effect": "Allow",
                "Action": ["iam:GetRole"],
                "Resource": "arn:aws:iam::123456789012:role/adp-cli-uplift-eval-instance",
                "Condition": {
                    "StringEquals": {
                        "iam:ResourceTag/Project": "adp",
                        "iam:ResourceTag/Environment": "dev",
                        "iam:ResourceTag/Purpose": "cli-uplift-eval",
                    }
                },
            },
        ]
        worst = consolidated_documents(*WORST, everything_on=True)
        assert not any(statement["Sid"] == "ProductLegacyEvaluatorRoleIdentity" for document in worst.values() for statement in document["Statement"])

    @pytest.mark.parametrize("enabled", [True, False])
    def test_l5_grant_is_optional_send_only_on_its_work_queue(self, enabled):
        fixture = gateway_fixture(*WORST, everything_on=True)
        fixture["var.enable_l5_security"] = enabled
        queue = fixture.pop("module.l5_security[0].work_queue_arn")
        if enabled:
            fixture["module.l5_security[0].work_queue_arn"] = queue
        scope = hcl.Scope(fixture, sorted(GATEWAY_INFRA.glob("*.tf")))
        groups = scope.local("gateway_role_policy_statements")
        grants = [
            (group, statement) for group, statements in groups.items() for statement in statements if statement["Sid"] == "L5SecuritySQSPublish"
        ]
        assert grants == (
            [("data", {"Sid": "L5SecuritySQSPublish", "Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": queue})] if enabled else []
        )

    def test_three_groups_are_declared_with_the_adp_names(self):
        tf = (GATEWAY_INFRA / "gateway-role-policies.tf").read_text()
        assert 'resource "aws_iam_policy" "gateway_role"' in tf
        assert "for_each = local.gateway_role_policy_statements" in tf
        assert 'name        = "adp-${var.environment}-policy-gateway-${each.key}"' in tf
        assert 'resource "aws_iam_role_policy_attachment" "gateway_role"' in tf
        assert "for_each = aws_iam_policy.gateway_role" in tf

    def test_the_gateway_module_declares_no_unconditional_inline_policy_on_the_role(self):
        """Only the pass-1 legacy carrier may put an inline policy on the role, and it is empty by default."""
        inline = []
        for path in sorted(GATEWAY_INFRA.glob("*.tf")):
            for block in hcl.blocks(path):
                if block.kind != "resource" or block.labels[0] != "aws_iam_role_policy":
                    continue
                if gateway_role_reference(block.attributes.get("role", "")):
                    inline.append((block.address, block.attributes.get("for_each") or block.attributes.get("count")))
        assert inline == [
            (
                "aws_iam_role_policy.gateway_role_legacy",
                "var.gateway_role_policy_cutover_keep_legacy ? var.gateway_role_policy_cutover_inline_documents : {}",
            ),
        ], inline


# ---------------------------------------------------------------------------
# AC-02: the inline aggregate the other stacks still use
# ---------------------------------------------------------------------------


class TestInlineAggregate:
    @pytest.mark.parametrize("target", [WORST, DEV], ids=["worst-case", "dev"])
    def test_other_stacks_inline_total_stays_under_budget(self, target):
        rendered = other_stack_inline_policies(*target, everything_on=target is WORST)
        total = sum(policy.size for policy in rendered)
        breakdown = {policy.name: policy.size for policy in rendered}
        assert rendered, "no inline policies rendered for the gateway role; the renderer lost the other stacks"
        detail = f"inline aggregate {total} > budget {INLINE_AGGREGATE_BUDGET} (limit {INLINE_AGGREGATE_LIMIT}): {breakdown}"
        assert total <= INLINE_AGGREGATE_BUDGET, detail

    def test_known_inline_owners(self):
        """platform/infra and webhook-ingress are the only inline writers left; name them so a new one is reviewed."""
        rendered = other_stack_inline_policies(*DEV, everything_on=False)
        names = sorted(policy.name for policy in rendered)
        assert names == [
            "adp-dev-policy-gateway-activity-read",
            "adp-dev-policy-gateway-agent-authority",
            "adp-dev-policy-gateway-agent-self-write",
            "adp-dev-policy-gateway-bedrock",
            "adp-dev-policy-gateway-dynamodb",
            "adp-dev-policy-gateway-irsa-logs",
            "adp-dev-policy-gateway-service-approval",
            "adp-dev-policy-gateway-ssm-read",
            "adp-dev-policy-gateway-sts",
        ], names


# ---------------------------------------------------------------------------
# AC-03: attachments across every stack that shares the role
# ---------------------------------------------------------------------------

# Every aws_iam_role_policy_attachment resource that targets the gateway role,
# with the number of attachments it produces on dev. A new resource in one of the
# scanned stacks fails test_every_attachment_resource_is_classified until it is
# added here with its cost.
DECLARED_ATTACHMENTS: dict[str, int] = {
    # modules/gateway/infra
    "aws_iam_role_policy_attachment.gateway_role": 3,  # identity, data, agents
    "aws_iam_role_policy_attachment.gateway_task_api": 1,  # task_api_prerequisites_enabled on dev
    "aws_iam_role_policy_attachment.gateway_tools_catalogue_read": 0,  # pass-1 only
    "aws_iam_role_policy_attachment.gateway_cognito_lifecycle": 0,  # pass-1 only, only if already attached
    # modules/agent-factory/infra
    "aws_iam_role_policy_attachment.gateway_intake_access": 1,
    "aws_iam_role_policy_attachment.gateway_chat_pending_publish": 1,
    "aws_iam_role_policy_attachment.gateway_chat_response_publish": 1,
    "aws_iam_role_policy_attachment.gateway_chat_completion": 1,
    # modules/agent-factory/webhook-ingress/infra (gateway_authority_managed_policies = true)
    "aws_iam_role_policy_attachment.gateway_authorized_dispatch": 1,
    "aws_iam_role_policy_attachment.gateway_task_source": 1,
    "aws_iam_role_policy_attachment.gateway_task_storage": 1,  # declared, not yet attached on dev
    # modules/domain-apps/superplane/infra
    "aws_iam_role_policy_attachment.gateway_domain_operations": 1,
    "aws_iam_role_policy_attachment.gateway_route_read": 1,
    "aws_iam_role_policy_attachment.gateway": 1,  # provider-authority
    "aws_iam_role_policy_attachment.gateway_access": 4,  # domain-provider: one per provider; dev has four
    # platform/automation-infra (modules/gateway/infra/deployment-evidence module)
    "aws_iam_role_policy_attachment.read": 1,  # reader_role = gateway role
}
# Attached on dev but declared nowhere in this repository. The probe is of
# unknown ownership (platform/scripts/operator/wave2/README.md) and stays; the
# hand-created predecessor of bedrockgw-dev-gateway-tools-catalogue-read is
# detached as the first cutover step (deployment-reference.md, "Gateway service
# role IAM budget") because pass 1 needs its slot.
LIVE_ONLY_ATTACHMENTS = {"adp-dev-authority-probe-20260920": 1}
DETACHED_BEFORE_CUTOVER = {"adp-dev-tools-catalogue-read": 1}


def attachment_resources() -> dict[str, Path]:
    found: dict[str, Path] = {}
    scan = [
        *sorted(GATEWAY_INFRA.glob("*.tf")),
        *sorted(AGENT_FACTORY_INFRA.glob("*.tf")),
        *sorted(WEBHOOK_INGRESS_INFRA.glob("*.tf")),
        *sorted(SUPERPLANE_INFRA.rglob("*.tf")),
        *sorted((GATEWAY_INFRA / "deployment-evidence").glob("*.tf")),
    ]
    for path in scan:
        for block in hcl.blocks(path):
            if block.kind != "resource" or block.labels[0] != "aws_iam_role_policy_attachment":
                continue
            role = block.attributes.get("role", "")
            if gateway_role_reference(role) or "var.gateway_role_arn" in role or "data.aws_iam_role.gateway" in role or role == "var.reader_role":
                found[block.address] = path
    return found


class TestAttachedBudget:
    def test_every_attachment_resource_is_classified(self):
        unknown = sorted(set(attachment_resources()) - set(DECLARED_ATTACHMENTS))
        assert not unknown, f"new gateway-role attachments need a cost in DECLARED_ATTACHMENTS: {unknown}"
        stale = sorted(set(DECLARED_ATTACHMENTS) - set(attachment_resources()))
        assert not stale, f"DECLARED_ATTACHMENTS lists resources that no longer exist: {stale}"

    def test_declared_plus_live_only_attachments_fit_the_quota(self):
        declared = sum(DECLARED_ATTACHMENTS.values())
        live_only = sum(LIVE_ONLY_ATTACHMENTS.values())
        total = declared + live_only
        assert total <= ATTACHED_LIMIT, f"{total} attachments ({declared} declared + {live_only} live-only) exceed {ATTACHED_LIMIT}"

    def test_the_budget_is_full_without_the_cutover_detach(self):
        """Pass 1 adds three attachments to a role holding 18; the duplicate catalogue policy must go first.

        Kept as an explicit assertion so the Superplane per-provider follow-up stays visible: with
        the duplicate gone, dev sits at exactly the quota once webhook-ingress attaches task-storage,
        and any further attachment needs a consolidated Superplane policy (4 -> 1) first.
        """
        with_duplicate = sum(DECLARED_ATTACHMENTS.values()) + sum(LIVE_ONLY_ATTACHMENTS.values()) + sum(DETACHED_BEFORE_CUTOVER.values())
        assert with_duplicate == ATTACHED_LIMIT + 1
        reference = (REPO_ROOT / "docs" / "adp-platform-deployment" / "deployment-reference.md").read_text()
        for name in DETACHED_BEFORE_CUTOVER:
            assert name in reference, f"{name} must be named in the cutover steps"
        assert "adp-<env>-spp-" in reference, "the Superplane per-provider attachment cost must be documented"

    def test_gateway_module_share_is_four_or_fewer(self):
        """Three groups plus the Task API policy; a fourth gateway-owned attachment needs a freed slot first."""
        share = sum(
            count
            for address, count in DECLARED_ATTACHMENTS.items()
            if address
            in (
                "aws_iam_role_policy_attachment.gateway_role",
                "aws_iam_role_policy_attachment.gateway_task_api",
                "aws_iam_role_policy_attachment.gateway_tools_catalogue_read",
            )
        )
        assert share == 4

    def test_evidence_reader_is_the_gateway_role(self):
        tf = (AUTOMATION_INFRA / "deployment-evidence.tf").read_text()
        assert 'reader_role = "adp-${var.environment}-role-gateway-service"' in tf


# ---------------------------------------------------------------------------
# AC-04: the statements that moved are all still there, once each
# ---------------------------------------------------------------------------

RETIRED_SIDS = {
    "identity": {
        "CognitoReadUsers",
        "CognitoIdentityLifecycle",
        "CognitoMachineClientLifecycle",
        "CognitoCliLoginMint",
        "IdentityIndexReadWrite",
        "AgentClientRegistryRead",
        "DynamoDBKMSAccess",
        "UserIdentityIndexAccess",
        "UserScopedWorkloadToken",
        "UserOauthConsentAndToken",
        "SecretsManagerReadAgentCreds",
    },
    "data": {
        "VaultSecretsCRUD",
        "GitHubAppKeyActivation",
        "VaultSecretsList",
        "WebhookSecretsKMSReadWrite",
        "CfnTemplateGet",
        "CfnTemplateList",
        "ChatLogsS3Write",
        "ChatLogsS3BucketAccess",
        "ComprehendPiiDetection",
        "RdsIamConnect",
        "ElastiCacheIamConnect",
        "XRayTracing",
        "IngestionSQSPublish",
    },
    "agents": {
        "MantleBedrockInvoke",
        "MantleCreateInference",
        "BedrockPoolAssumeRole",
        "ToolsCatalogueListGateways",
        "ToolsCatalogueReadTargets",
        "AgentRunLogsRead",
        "CrewRunBindingTable",
        "CrewRunBindingKms",
        "CrewCheckpointBucket",
        "CrewCheckpointObjects",
        "CrewCheckpointKms",
        "GatewayOwnedMemoryDataPlane",
    },
}


class TestStatementsSurviveTheMove:
    def test_existing_agents_policy_description_and_oauth_grants_survive_staged_update(self):
        scope = hcl.Scope(gateway_fixture(*DEV, everything_on=True), sorted(GATEWAY_INFRA.glob("*.tf")))
        assert scope.local("gateway_role_policy_descriptions")["agents"] == (
            "Gateway model invocation, tool catalogue, run transcripts, crew bindings and memory"
        )
        documents = consolidated_documents(*DEV, everything_on=True)
        agents = {statement["Sid"] for statement in documents["agents"]["Statement"]}
        assert {"OauthAppExternalSecrets", "OauthAppRegistration", "OauthAppWorkloadSetup", "SlackAppSetupProgress"} <= agents
        identity = {statement["Sid"] for statement in documents["identity"]["Statement"]}
        assert {"AtlassianMcpProviderSetup", "AtlassianMcpWorkloadSetup"} <= identity

    def test_every_retired_statement_is_present_exactly_once_with_everything_on(self):
        documents = consolidated_documents(*WORST, everything_on=True)
        for group, expected in RETIRED_SIDS.items():
            sids = [statement["Sid"] for statement in documents[group]["Statement"]]
            # New app-registration grants were not part of the historical move.
            added = (
                {"AtlassianMcpProviderSetup", "AtlassianMcpWorkloadSetup", "AtlassianMcpUserTokenAccess", "AtlassianMcpVaultBootstrap"}
                if group == "identity"
                else set()
            )
            if group == "agents":
                added = {
                    "ProductEvaluatorRoleIdentity",
                    "OauthAppExternalSecrets",
                    "OauthAppRegistration",
                    "OauthAppWorkloadSetup",
                    "SlackAppSetupProgress",
                    "JiraSystemProviderSetup",
                }
            elif group == "data":
                added = {
                    "L5SecuritySQSPublish",
                    "InsightsResultsRead",
                    "InsightsResultsDecrypt",
                    "OauthAppGithubReplacement",
                    "GithubAppSecretRotation",
                    "AtlassianMcpPlanRead",
                    "AtlassianMcpClientState",
                    "JiraSystemWorkloadSetup",
                }
            assert sorted(sids) == sorted(expected | added), f"{group}: {sorted(set(sids) ^ (expected | added))}"
            assert len(sids) == len(set(sids)), f"{group} repeats a Sid: {sids}"

    def test_switch_gated_statements_disappear_with_their_switches(self):
        documents = consolidated_documents(*DEV, everything_on=False)
        sids = {statement["Sid"] for document in documents.values() for statement in document["Statement"]}
        gated_off = {
            "XRayTracing",
            "BedrockPoolAssumeRole",
            "CrewCheckpointBucket",
            "CrewCheckpointObjects",
            "CrewCheckpointKms",
            "GatewayOwnedMemoryDataPlane",
        }
        assert not sids & gated_off, sids & gated_off
        assert {"MantleBedrockInvoke", "ChatLogsS3Write", "IngestionSQSPublish", "RdsIamConnect", "ElastiCacheIamConnect"} <= sids

    def test_every_statement_is_allow_and_names_a_resource(self):
        documents = consolidated_documents(*WORST, everything_on=True)
        for group, document in documents.items():
            for statement in document["Statement"]:
                assert statement["Effect"] == "Allow", (group, statement["Sid"])
                assert statement.get("Resource"), (group, statement["Sid"])
                assert statement.get("Action"), (group, statement["Sid"])

    def test_dynamodb_kms_statement_is_the_one_both_index_policies_carried(self):
        documents = consolidated_documents(*WORST, everything_on=True)
        kms = [statement for statement in documents["identity"]["Statement"] if statement["Sid"] == "DynamoDBKMSAccess"]
        assert len(kms) == 1
        assert kms[0]["Action"] == ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        assert kms[0]["Resource"] == [f"arn:aws:kms:{WORST[1]}:{WORST[2]}:{KMS_KEY}"]

    def test_legacy_names_cover_every_moved_inline_policy(self):
        """Every `moved` into the pass-1 carrier has a name entry, and vice versa."""
        tf = (GATEWAY_INFRA / "gateway-role-policies.tf").read_text()
        moved = set(re.findall(r'to\s*=\s*aws_iam_role_policy\.gateway_role_legacy\["([a-z0-9-]+)"\]', tf))
        names_block = tf[tf.index("gateway_role_legacy_inline_names = {") :]
        names_block = names_block[: names_block.index("\n  }")]
        names = set(re.findall(r"^\s+([a-z0-9-]+)\s+=", names_block, re.MULTILINE))
        assert moved == names, sorted(moved ^ names)
        assert len(moved) == 20


def test_budget_report(capsys):
    """Not an assertion: prints the numbers an operator compares with `aws iam` output."""
    for label, target in (("dev", DEV), ("worst-case", WORST)):
        documents = consolidated_documents(*target, everything_on=target is WORST)
        inline = other_stack_inline_policies(*target, everything_on=target is WORST)
        print(f"[{label}] managed:", {group: hcl.policy_size(document) for group, document in documents.items()})
        print(f"[{label}] inline aggregate:", sum(policy.size for policy in inline), {policy.name: policy.size for policy in inline})
    print("attached:", sum(DECLARED_ATTACHMENTS.values()), "declared +", sum(LIVE_ONLY_ATTACHMENTS.values()), "live-only")
    assert json.dumps(DECLARED_ATTACHMENTS)  # keep the report in the junit output
