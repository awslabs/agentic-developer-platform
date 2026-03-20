"""Tests for the Policy Scoping Service.

Issue #255: Comprehensive unit tests for the PolicyScopingService that generates
IAM policy documents, permission boundaries, and IRSA trust policies.
"""

import json

import pytest

from src.admin.policy_scoping_service import PolicyScopingService
from src.admin.policy_templates import (
    AGENT_TYPE_ACTIONS,
    DANGEROUS_ACTIONS,
    HIERARCHY_LEVELS,
    HIERARCHY_SHORT_CODES,
    IAM_POLICY_VERSION,
    MAX_PREFIX_LENGTH,
    OBSERVABILITY_ACTIONS,
)


@pytest.fixture
def service() -> PolicyScopingService:
    """Create a PolicyScopingService instance."""
    return PolicyScopingService()


class TestGenerateResourcePrefix:
    """Tests for generate_resource_prefix method."""

    def test_platform_level_prefix(self, service: PolicyScopingService):
        """Test prefix generation for platform level."""
        prefix = service.generate_resource_prefix(
            level="platform",
            hierarchy={"platform": "bedrockgw"},
        )
        assert prefix == "bgw-"

    def test_org_level_prefix(self, service: PolicyScopingService):
        """Test prefix generation for org level."""
        prefix = service.generate_resource_prefix(
            level="org",
            hierarchy={"platform": "bedrockgw", "org": "engineering"},
        )
        assert prefix == "bgw-eng-"

    def test_team_level_prefix(self, service: PolicyScopingService):
        """Test prefix generation for team level."""
        prefix = service.generate_resource_prefix(
            level="team",
            hierarchy={"platform": "bedrockgw", "org": "engineering", "team": "platform"},
        )
        assert prefix == "bgw-eng-plat-"

    def test_app_level_prefix(self, service: PolicyScopingService):
        """Test prefix generation for app level."""
        prefix = service.generate_resource_prefix(
            level="app",
            hierarchy={
                "platform": "bedrockgw",
                "org": "engineering",
                "team": "platform",
                "app": "payments",
            },
        )
        assert prefix == "bgw-eng-plat-pay-"

    def test_user_level_prefix(self, service: PolicyScopingService):
        """Test prefix generation for user level.

        Note: The prefix is truncated to fit within MAX_PREFIX_LENGTH (20 chars).
        Full prefix would be bgw-eng-plat-pay-pra- (21 chars) but is truncated.
        """
        prefix = service.generate_resource_prefix(
            level="user",
            hierarchy={
                "platform": "bedrockgw",
                "org": "engineering",
                "team": "platform",
                "app": "payments",
                "user": "pranav",
            },
        )
        # Verify it ends with hyphen and is within limit
        assert prefix.endswith("-")
        assert len(prefix) <= MAX_PREFIX_LENGTH
        # Should contain all level short codes (possibly truncated)
        assert prefix.startswith("bgw-")

    def test_explicit_short_code_mapping(self, service: PolicyScopingService):
        """Test that explicit short codes are used when available."""
        prefix = service.generate_resource_prefix(
            level="org",
            hierarchy={"platform": "bedrockgateway", "org": "marketing"},
        )
        # bedrockgateway -> bgw, marketing -> mkt
        assert prefix == "bgw-mkt-"

    def test_truncation_fallback(self, service: PolicyScopingService):
        """Test that names without explicit mapping are truncated."""
        prefix = service.generate_resource_prefix(
            level="org",
            hierarchy={"platform": "myplatform", "org": "myorganization"},
        )
        # myplatform -> myp, myorganization -> myo
        assert prefix == "myp-myo-"

    def test_prefix_stays_within_limit(self, service: PolicyScopingService):
        """Test that prefix stays within MAX_PREFIX_LENGTH."""
        # Use very long names to test truncation
        prefix = service.generate_resource_prefix(
            level="user",
            hierarchy={
                "platform": "verylongplatformname",
                "org": "verylongorganizationname",
                "team": "verylongteamname",
                "app": "verylongappname",
                "user": "verylongusername",
            },
        )
        assert len(prefix) <= MAX_PREFIX_LENGTH

    def test_invalid_level_raises_error(self, service: PolicyScopingService):
        """Test that invalid level raises ValueError."""
        with pytest.raises(ValueError) as excinfo:
            service.generate_resource_prefix(
                level="invalid",
                hierarchy={"platform": "test"},
            )
        assert "Invalid level" in str(excinfo.value)

    def test_missing_hierarchy_value_raises_error(self, service: PolicyScopingService):
        """Test that missing required hierarchy value raises ValueError."""
        with pytest.raises(ValueError) as excinfo:
            service.generate_resource_prefix(
                level="org",
                hierarchy={"platform": "test"},  # Missing org
            )
        assert "Missing required hierarchy value" in str(excinfo.value)

    def test_all_hierarchy_levels_valid(self, service: PolicyScopingService):
        """Test that all defined hierarchy levels are valid."""
        for level in HIERARCHY_LEVELS:
            # Build hierarchy dict with all required levels
            hierarchy = {}
            for lvl in HIERARCHY_LEVELS[: HIERARCHY_LEVELS.index(level) + 1]:
                hierarchy[lvl] = f"test{lvl}"

            prefix = service.generate_resource_prefix(level=level, hierarchy=hierarchy)
            assert prefix.endswith("-")
            assert len(prefix) <= MAX_PREFIX_LENGTH


class TestGeneratePermissionBoundary:
    """Tests for generate_permission_boundary method."""

    def test_permission_boundary_structure(self, service: PolicyScopingService):
        """Test that permission boundary has correct structure."""
        boundary = service.generate_permission_boundary(
            resource_prefix="bgw-eng-plat-",
            region="us-east-1",
            account_id="123456789012",
        )

        assert boundary["Version"] == IAM_POLICY_VERSION
        assert "Statement" in boundary
        assert len(boundary["Statement"]) == 3

        # Check statement Sids
        sids = {stmt["Sid"] for stmt in boundary["Statement"]}
        assert sids == {"AllowScopedResources", "AllowGlobalReadOnly", "DenyDangerous"}

    def test_permission_boundary_includes_deny_dangerous(self, service: PolicyScopingService):
        """Test that permission boundary includes deny for dangerous actions."""
        boundary = service.generate_permission_boundary(
            resource_prefix="bgw-eng-",
            region="us-east-1",
            account_id="123456789012",
        )

        deny_stmt = next(s for s in boundary["Statement"] if s["Sid"] == "DenyDangerous")
        assert deny_stmt["Effect"] == "Deny"
        assert deny_stmt["Action"] == DANGEROUS_ACTIONS
        assert deny_stmt["Resource"] == "*"

    def test_permission_boundary_scoped_resources(self, service: PolicyScopingService):
        """Test that permission boundary scopes resources correctly."""
        boundary = service.generate_permission_boundary(
            resource_prefix="bgw-eng-plat-pay-",
            region="us-west-2",
            account_id="987654321098",
        )

        scoped_stmt = next(s for s in boundary["Statement"] if s["Sid"] == "AllowScopedResources")
        assert scoped_stmt["Effect"] == "Allow"
        assert scoped_stmt["Action"] == "*"
        # Check that resource ARNs contain the prefix
        assert any("bgw-eng-plat-pay-" in r for r in scoped_stmt["Resource"])
        assert any("us-west-2" in r for r in scoped_stmt["Resource"])
        assert any("987654321098" in r for r in scoped_stmt["Resource"])

    def test_permission_boundary_includes_observability(self, service: PolicyScopingService):
        """Test that permission boundary includes observability actions."""
        boundary = service.generate_permission_boundary(
            resource_prefix="bgw-",
            region="us-east-1",
            account_id="123456789012",
        )

        readonly_stmt = next(s for s in boundary["Statement"] if s["Sid"] == "AllowGlobalReadOnly")
        assert readonly_stmt["Effect"] == "Allow"
        # Check that execute-api:Invoke is included
        assert "execute-api:Invoke" in readonly_stmt["Action"]
        # Check that observability actions are included
        for action in OBSERVABILITY_ACTIONS[:3]:  # Check first few
            assert action in readonly_stmt["Action"]

    def test_permission_boundary_is_valid_json(self, service: PolicyScopingService):
        """Test that generated permission boundary is valid JSON."""
        boundary = service.generate_permission_boundary(
            resource_prefix="bgw-eng-",
            region="us-east-1",
            account_id="123456789012",
        )

        # Should not raise
        json_str = json.dumps(boundary)
        parsed = json.loads(json_str)
        assert parsed == boundary


class TestGenerateAgentPolicy:
    """Tests for generate_agent_policy method."""

    def test_infra_agent_policy(self, service: PolicyScopingService):
        """Test that infra agent gets correct actions including PassRole."""
        policy = service.generate_agent_policy(
            agent_type="infra",
            resource_prefix="bgw-eng-plat-",
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )

        assert policy["Version"] == IAM_POLICY_VERSION

        # Check that scoped resources statement exists
        scoped_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "ScopedResources"),
            None,
        )
        assert scoped_stmt is not None
        assert "ec2:*" in scoped_stmt["Action"]
        assert "rds:*" in scoped_stmt["Action"]

        # Check that PassRole is scoped separately
        passrole_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "PassRoleScoped"),
            None,
        )
        assert passrole_stmt is not None
        assert passrole_stmt["Action"] == "iam:PassRole"
        assert "bgw-eng-plat-" in passrole_stmt["Resource"]

    def test_app_dev_agent_policy(self, service: PolicyScopingService):
        """Test that app-dev agent gets correct actions."""
        policy = service.generate_agent_policy(
            agent_type="app-dev",
            resource_prefix="bgw-eng-",
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )

        scoped_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "ScopedResources"),
            None,
        )
        assert scoped_stmt is not None
        assert "ecr:*" in scoped_stmt["Action"]
        assert "codebuild:*" in scoped_stmt["Action"]
        assert "codepipeline:*" in scoped_stmt["Action"]

    def test_monitoring_agent_policy(self, service: PolicyScopingService):
        """Test that monitoring agent gets correct actions."""
        policy = service.generate_agent_policy(
            agent_type="monitoring",
            resource_prefix="bgw-",
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )

        scoped_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "ScopedResources"),
            None,
        )
        assert scoped_stmt is not None
        assert "cloudwatch:*" in scoped_stmt["Action"]
        assert "logs:*" in scoped_stmt["Action"]
        assert "xray:*" in scoped_stmt["Action"]

    def test_security_agent_policy(self, service: PolicyScopingService):
        """Test that security agent gets read-only IAM actions."""
        policy = service.generate_agent_policy(
            agent_type="security",
            resource_prefix="bgw-",
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )

        scoped_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "ScopedResources"),
            None,
        )
        assert scoped_stmt is not None
        assert "iam:Get*" in scoped_stmt["Action"]
        assert "iam:List*" in scoped_stmt["Action"]
        # Should NOT have iam:Create* or iam:Delete*
        assert "iam:Create*" not in scoped_stmt["Action"]
        assert "iam:Delete*" not in scoped_stmt["Action"]

    def test_general_agent_only_gateway_access(self, service: PolicyScopingService):
        """Test that general agent has only execute-api:Invoke."""
        policy = service.generate_agent_policy(
            agent_type="general",
            resource_prefix="bgw-eng-",
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )

        # Should NOT have ScopedResources statement
        scoped_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "ScopedResources"),
            None,
        )
        assert scoped_stmt is None

        # Should have GatewayAccess
        gateway_stmt = next(
            (s for s in policy["Statement"] if s.get("Sid") == "GatewayAccess"),
            None,
        )
        assert gateway_stmt is not None
        assert gateway_stmt["Action"] == "execute-api:Invoke"

    def test_all_agent_types_have_gateway_access(self, service: PolicyScopingService):
        """Test that all agent types include gateway access."""
        for agent_type in AGENT_TYPE_ACTIONS.keys():
            policy = service.generate_agent_policy(
                agent_type=agent_type,
                resource_prefix="bgw-",
                region="us-east-1",
                account_id="123456789012",
                api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
            )

            gateway_stmt = next(
                (s for s in policy["Statement"] if s.get("Sid") == "GatewayAccess"),
                None,
            )
            assert gateway_stmt is not None, f"Agent type {agent_type} missing gateway access"

    def test_all_agent_types_have_observability(self, service: PolicyScopingService):
        """Test that all agent types include observability actions."""
        for agent_type in AGENT_TYPE_ACTIONS.keys():
            policy = service.generate_agent_policy(
                agent_type=agent_type,
                resource_prefix="bgw-",
                region="us-east-1",
                account_id="123456789012",
                api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
            )

            obs_stmt = next(
                (s for s in policy["Statement"] if s.get("Sid") == "Observability"),
                None,
            )
            assert obs_stmt is not None, f"Agent type {agent_type} missing observability"

    def test_invalid_agent_type_raises_error(self, service: PolicyScopingService):
        """Test that invalid agent type raises ValueError."""
        with pytest.raises(ValueError) as excinfo:
            service.generate_agent_policy(
                agent_type="invalid",
                resource_prefix="bgw-",
                region="us-east-1",
                account_id="123456789012",
                api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
            )
        assert "Invalid agent_type" in str(excinfo.value)

    def test_agent_policy_is_valid_json(self, service: PolicyScopingService):
        """Test that generated agent policies are valid JSON."""
        for agent_type in AGENT_TYPE_ACTIONS.keys():
            policy = service.generate_agent_policy(
                agent_type=agent_type,
                resource_prefix="bgw-eng-",
                region="us-east-1",
                account_id="123456789012",
                api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
            )

            # Should not raise
            json_str = json.dumps(policy)
            parsed = json.loads(json_str)
            assert parsed == policy


class TestGenerateTrustPolicy:
    """Tests for generate_trust_policy method."""

    def test_trust_policy_structure(self, service: PolicyScopingService):
        """Test that trust policy has correct IRSA structure."""
        trust = service.generate_trust_policy(
            oidc_provider_arn="arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            oidc_issuer="oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            namespace="agents",
            service_account_name="payment-deployer",
        )

        assert trust["Version"] == IAM_POLICY_VERSION
        assert len(trust["Statement"]) == 1

        stmt = trust["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"

    def test_trust_policy_oidc_conditions(self, service: PolicyScopingService):
        """Test that trust policy has correct OIDC conditions."""
        oidc_issuer = "oidc.eks.us-east-1.amazonaws.com/id/ABC123"
        trust = service.generate_trust_policy(
            oidc_provider_arn="arn:aws:iam::123456789012:oidc-provider/" + oidc_issuer,
            oidc_issuer=oidc_issuer,
            namespace="default",
            service_account_name="my-agent",
        )

        stmt = trust["Statement"][0]
        conditions = stmt["Condition"]["StringEquals"]

        # Check audience condition
        assert conditions[f"{oidc_issuer}:aud"] == "sts.amazonaws.com"

        # Check subject condition (namespace:service_account)
        assert conditions[f"{oidc_issuer}:sub"] == "system:serviceaccount:default:my-agent"

    def test_trust_policy_federated_principal(self, service: PolicyScopingService):
        """Test that trust policy uses correct federated principal."""
        oidc_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC123"
        trust = service.generate_trust_policy(
            oidc_provider_arn=oidc_arn,
            oidc_issuer="oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            namespace="agents",
            service_account_name="test",
        )

        stmt = trust["Statement"][0]
        assert stmt["Principal"]["Federated"] == oidc_arn

    def test_trust_policy_is_valid_json(self, service: PolicyScopingService):
        """Test that generated trust policy is valid JSON."""
        trust = service.generate_trust_policy(
            oidc_provider_arn="arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            oidc_issuer="oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            namespace="agents",
            service_account_name="my-agent",
        )

        # Should not raise
        json_str = json.dumps(trust)
        parsed = json.loads(json_str)
        assert parsed == trust


class TestGenerateRequiredTags:
    """Tests for generate_required_tags method."""

    def test_required_tags_include_hierarchy(self, service: PolicyScopingService):
        """Test that required tags include all hierarchy levels."""
        tags = service.generate_required_tags(
            level="app",
            hierarchy={
                "platform": "bedrockgw",
                "org": "engineering",
                "team": "platform",
                "app": "payments",
            },
        )

        assert tags["platform"] == "bedrockgw"
        assert tags["org"] == "engineering"
        assert tags["team"] == "platform"
        assert tags["app"] == "payments"

    def test_required_tags_include_managed_by(self, service: PolicyScopingService):
        """Test that required tags always include managed-by."""
        tags = service.generate_required_tags(
            level="platform",
            hierarchy={"platform": "test"},
        )

        assert tags["managed-by"] == "bedrock-gateway"

    def test_required_tags_only_include_relevant_levels(self, service: PolicyScopingService):
        """Test that required tags only include levels up to the specified level."""
        tags = service.generate_required_tags(
            level="team",
            hierarchy={
                "platform": "bgw",
                "org": "eng",
                "team": "plat",
                "app": "pay",  # Should NOT be included
                "user": "bob",  # Should NOT be included
            },
        )

        assert "platform" in tags
        assert "org" in tags
        assert "team" in tags
        assert "app" not in tags
        assert "user" not in tags

    def test_required_tags_invalid_level(self, service: PolicyScopingService):
        """Test that invalid level raises ValueError."""
        with pytest.raises(ValueError) as excinfo:
            service.generate_required_tags(
                level="invalid",
                hierarchy={"platform": "test"},
            )
        assert "Invalid level" in str(excinfo.value)


class TestGenerateTagEnforcementStatements:
    """Tests for generate_tag_enforcement_statements method."""

    def test_tag_enforcement_includes_all_hierarchy_levels(self, service: PolicyScopingService):
        """Test that tag enforcement includes all provided hierarchy levels."""
        statements = service.generate_tag_enforcement_statements(
            hierarchy={
                "platform": "bgw",
                "org": "eng",
                "team": "plat",
                "app": "pay",
            }
        )

        assert len(statements) == 1
        stmt = statements[0]

        conditions = stmt["Condition"]["StringEquals"]
        assert conditions["aws:RequestTag/platform"] == "bgw"
        assert conditions["aws:RequestTag/org"] == "eng"
        assert conditions["aws:RequestTag/team"] == "plat"
        assert conditions["aws:RequestTag/app"] == "pay"
        assert conditions["aws:RequestTag/managed-by"] == "bedrock-gateway"

    def test_tag_enforcement_includes_taggable_actions(self, service: PolicyScopingService):
        """Test that tag enforcement includes common taggable actions."""
        statements = service.generate_tag_enforcement_statements(hierarchy={"platform": "test"})

        stmt = statements[0]
        actions = stmt["Action"]

        # Should include common create/tag actions
        assert "ec2:CreateTags" in actions
        assert "ec2:RunInstances" in actions
        assert "s3:CreateBucket" in actions
        assert "lambda:CreateFunction" in actions

    def test_tag_enforcement_skips_empty_values(self, service: PolicyScopingService):
        """Test that tag enforcement skips empty hierarchy values."""
        statements = service.generate_tag_enforcement_statements(
            hierarchy={
                "platform": "bgw",
                "org": "",  # Empty, should be skipped
                "team": "plat",
            }
        )

        stmt = statements[0]
        conditions = stmt["Condition"]["StringEquals"]

        assert "aws:RequestTag/platform" in conditions
        assert "aws:RequestTag/org" not in conditions  # Skipped
        assert "aws:RequestTag/team" in conditions

    def test_tag_enforcement_empty_hierarchy(self, service: PolicyScopingService):
        """Test that empty hierarchy still requires managed-by tag."""
        statements = service.generate_tag_enforcement_statements(hierarchy={})

        # Should still have one statement for managed-by
        assert len(statements) == 1
        stmt = statements[0]
        conditions = stmt["Condition"]["StringEquals"]
        assert conditions["aws:RequestTag/managed-by"] == "bedrock-gateway"


class TestPolicyTemplates:
    """Tests for policy_templates.py configuration."""

    def test_all_agent_types_have_actions(self):
        """Test that all agent types have non-empty action lists."""
        for agent_type, actions in AGENT_TYPE_ACTIONS.items():
            assert len(actions) > 0, f"Agent type {agent_type} has no actions"

    def test_dangerous_actions_list(self):
        """Test that dangerous actions list contains expected dangerous actions."""
        # Critical security actions that should be denied
        expected_dangerous = [
            "iam:CreateUser",
            "iam:CreateRole",
            "organizations:*",
            "account:*",
        ]
        for action in expected_dangerous:
            assert action in DANGEROUS_ACTIONS, f"Expected {action} in DANGEROUS_ACTIONS"

    def test_observability_actions_are_read_only(self):
        """Test that observability actions are read-only."""
        for action in OBSERVABILITY_ACTIONS:
            # Should not contain write operations
            assert "Put" not in action or "GetPut" in action
            assert "Create" not in action
            assert "Delete" not in action
            assert "Update" not in action

    def test_hierarchy_short_codes_lowercase(self):
        """Test that all short codes are lowercase."""
        for name, code in HIERARCHY_SHORT_CODES.items():
            assert code == code.lower(), f"Short code {code} for {name} should be lowercase"

    def test_hierarchy_levels_order(self):
        """Test that hierarchy levels are in correct order (most to least privileged)."""
        expected_order = ["platform", "org", "team", "app", "user"]
        assert HIERARCHY_LEVELS == expected_order


class TestIntegration:
    """Integration tests combining multiple service methods."""

    def test_full_policy_generation_workflow(self, service: PolicyScopingService):
        """Test generating all policies for a typical agent."""
        hierarchy = {
            "platform": "bedrockgw",
            "org": "engineering",
            "team": "platform",
            "app": "payments",
        }

        # Generate prefix
        prefix = service.generate_resource_prefix(level="app", hierarchy=hierarchy)
        assert prefix == "bgw-eng-plat-pay-"

        # Generate permission boundary
        boundary = service.generate_permission_boundary(
            resource_prefix=prefix,
            region="us-east-1",
            account_id="123456789012",
        )
        assert boundary["Version"] == IAM_POLICY_VERSION

        # Generate agent policy
        agent_policy = service.generate_agent_policy(
            agent_type="infra",
            resource_prefix=prefix,
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )
        assert agent_policy["Version"] == IAM_POLICY_VERSION

        # Generate trust policy
        trust_policy = service.generate_trust_policy(
            oidc_provider_arn="arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            oidc_issuer="oidc.eks.us-east-1.amazonaws.com/id/ABC123",
            namespace="agents",
            service_account_name="payment-deployer",
        )
        assert trust_policy["Version"] == IAM_POLICY_VERSION

        # Generate tags
        tags = service.generate_required_tags(level="app", hierarchy=hierarchy)
        assert "managed-by" in tags

        # All outputs should be valid JSON
        for policy in [boundary, agent_policy, trust_policy]:
            json.dumps(policy)  # Should not raise

    def test_general_agent_minimal_permissions(self, service: PolicyScopingService):
        """Test that general agent has minimal permissions (LLM only)."""
        prefix = service.generate_resource_prefix(
            level="user",
            hierarchy={
                "platform": "bgw",
                "org": "eng",
                "team": "plat",
                "app": "chat",
                "user": "bob",
            },
        )

        policy = service.generate_agent_policy(
            agent_type="general",
            resource_prefix=prefix,
            region="us-east-1",
            account_id="123456789012",
            api_gateway_arn="arn:aws:execute-api:us-east-1:123456789012:abc123/*",
        )

        # Count statements - should only have GatewayAccess and Observability
        sids = {s.get("Sid") for s in policy["Statement"]}
        assert "ScopedResources" not in sids
        assert "GatewayAccess" in sids
        assert "Observability" in sids
