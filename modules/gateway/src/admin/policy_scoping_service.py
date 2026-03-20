"""Policy Scoping Service - IAM Policy & Permission Boundary Generator.

Issue #255: This service generates IAM policy documents, permission boundaries,
and IRSA trust policies based on agent attributes (level, agent_type, hierarchy).
It is a standalone Python module that generates JSON policy documents only -
it does NOT create IAM resources.

See: aidlc-docs/inception/application-design/agent-platform-architecture.md section 4
"""

from src.admin.policy_templates import (
    AGENT_TYPE_ACTIONS,
    DANGEROUS_ACTIONS,
    HIERARCHY_LEVELS,
    HIERARCHY_SHORT_CODES,
    IAM_POLICY_VERSION,
    MAX_PREFIX_LENGTH,
    OBSERVABILITY_ACTIONS,
    SHORT_CODE_LENGTH,
)


class PolicyScopingService:
    """Service for generating IAM policies, permission boundaries, and trust policies.

    This service centralizes all IAM policy generation logic. It takes agent attributes
    and returns complete IAM policy documents. The onboarding orchestrator calls this
    service - it never constructs policies itself.

    Three-Layer Security Model:
    1. Permission Boundary - Hard ceiling based on hierarchy level
    2. Agent IAM Policy - Actual permissions based on agent_type + resource prefix
    3. Tag Requirements - Tags that must be applied to created resources
    """

    def generate_resource_prefix(self, level: str, hierarchy: dict[str, str]) -> str:
        """Build a short-code prefix from hierarchy.

        The prefix is constructed from hierarchy values using short codes to stay
        within AWS naming limits. Each level adds its short code with a hyphen.

        Args:
            level: The hierarchy level (platform, org, team, app, user)
            hierarchy: Dictionary with hierarchy values. Keys are level names,
                      values are the actual names (e.g., {"platform": "bedrockgw", "org": "engineering"})

        Returns:
            A resource prefix string ending with hyphen (e.g., "bgw-eng-plat-pay-")

        Raises:
            ValueError: If level is invalid or required hierarchy values are missing

        Example:
            >>> service = PolicyScopingService()
            >>> service.generate_resource_prefix(
            ...     level="app",
            ...     hierarchy={"platform": "bedrockgw", "org": "engineering", "team": "platform", "app": "payments"}
            ... )
            'bgw-eng-plat-pay-'
        """
        if level not in HIERARCHY_LEVELS:
            raise ValueError(f"Invalid level: {level}. Must be one of {HIERARCHY_LEVELS}")

        # Get the index of the level to know how many parts to include
        level_index = HIERARCHY_LEVELS.index(level)
        levels_to_include = HIERARCHY_LEVELS[: level_index + 1]

        # Build prefix from hierarchy values
        parts: list[str] = []
        for lvl in levels_to_include:
            value = hierarchy.get(lvl)
            if not value:
                raise ValueError(f"Missing required hierarchy value for level: {lvl}")
            short_code = self._get_short_code(value)
            parts.append(short_code)

        prefix = "-".join(parts) + "-"

        # Validate prefix length
        if len(prefix) > MAX_PREFIX_LENGTH:
            # Truncate each part if needed
            prefix = self._truncate_prefix(parts) + "-"

        return prefix

    def generate_permission_boundary(
        self,
        resource_prefix: str,
        region: str,
        account_id: str,
    ) -> dict:
        """Generate an IAM Permission Boundary policy document.

        The permission boundary is the hard ceiling for what an agent can do.
        Even if `AdministratorAccess` is attached, the boundary blocks anything
        outside scope.

        Args:
            resource_prefix: The resource prefix (e.g., "bgw-eng-plat-pay-")
            region: AWS region (e.g., "us-east-1")
            account_id: AWS account ID (12 digits)

        Returns:
            IAM permission boundary policy document as a dictionary

        Structure:
        - AllowScopedResources: Actions on {prefix}* resources
        - AllowGlobalReadOnly: Observability + gateway access
        - DenyDangerous: Explicit deny for dangerous actions
        """
        return {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Sid": "AllowScopedResources",
                    "Effect": "Allow",
                    "Action": "*",
                    "Resource": [
                        f"arn:aws:*:{region}:{account_id}:*{resource_prefix}*",
                        f"arn:aws:*:{region}:{account_id}:{resource_prefix}*",
                    ],
                },
                {
                    "Sid": "AllowGlobalReadOnly",
                    "Effect": "Allow",
                    "Action": [
                        "execute-api:Invoke",
                        *OBSERVABILITY_ACTIONS,
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "DenyDangerous",
                    "Effect": "Deny",
                    "Action": DANGEROUS_ACTIONS,
                    "Resource": "*",
                },
            ],
        }

    def generate_agent_policy(
        self,
        agent_type: str,
        resource_prefix: str,
        region: str,
        account_id: str,
        api_gateway_arn: str,
    ) -> dict:
        """Generate an IAM policy document for an agent.

        Returns an IAM policy with actions based on agent_type, scoped to
        {prefix}* resources. The policy has four sections:
        1. App-scoped resources
        2. Tag enforcement
        3. Gateway access
        4. Read-only observability

        Args:
            agent_type: The agent type (infra, app-dev, monitoring, security, general)
            resource_prefix: The resource prefix (e.g., "bgw-eng-plat-pay-")
            region: AWS region
            account_id: AWS account ID
            api_gateway_arn: API Gateway ARN for gateway access

        Returns:
            IAM policy document as a dictionary

        Raises:
            ValueError: If agent_type is not valid
        """
        if agent_type not in AGENT_TYPE_ACTIONS:
            valid_types = list(AGENT_TYPE_ACTIONS.keys())
            raise ValueError(f"Invalid agent_type: {agent_type}. Must be one of {valid_types}")

        actions = AGENT_TYPE_ACTIONS[agent_type]
        statements: list[dict] = []

        # 1. Scoped resources - actions on {prefix}* resources
        if agent_type != "general":
            # General agents only get gateway access, no AWS service access
            statements.append(
                {
                    "Sid": "ScopedResources",
                    "Effect": "Allow",
                    "Action": actions,
                    "Resource": [
                        f"arn:aws:*:{region}:{account_id}:*{resource_prefix}*",
                        f"arn:aws:*:{region}:{account_id}:{resource_prefix}*",
                    ],
                }
            )

            # Special case: iam:PassRole needs to be scoped to prefix roles
            if "iam:PassRole" in actions:
                statements.append(
                    {
                        "Sid": "PassRoleScoped",
                        "Effect": "Allow",
                        "Action": "iam:PassRole",
                        "Resource": f"arn:aws:iam::{account_id}:role/{resource_prefix}*",
                    }
                )

        # 2. Gateway access - always included
        statements.append(
            {
                "Sid": "GatewayAccess",
                "Effect": "Allow",
                "Action": "execute-api:Invoke",
                "Resource": api_gateway_arn,
            }
        )

        # 3. Read-only observability - always included
        statements.append(
            {
                "Sid": "Observability",
                "Effect": "Allow",
                "Action": OBSERVABILITY_ACTIONS,
                "Resource": "*",
            }
        )

        return {
            "Version": IAM_POLICY_VERSION,
            "Statement": statements,
        }

    def generate_trust_policy(
        self,
        oidc_provider_arn: str,
        oidc_issuer: str,
        namespace: str,
        service_account_name: str,
    ) -> dict:
        """Generate an IRSA trust policy for the agent's IAM role.

        This trust policy allows the agent's Kubernetes ServiceAccount to
        assume the IAM role using IRSA (IAM Roles for Service Accounts).

        Args:
            oidc_provider_arn: The OIDC provider ARN for the EKS cluster
            oidc_issuer: The OIDC issuer URL (without https://)
            namespace: Kubernetes namespace where the agent runs
            service_account_name: The Kubernetes ServiceAccount name

        Returns:
            IAM trust policy document as a dictionary

        Example OIDC issuer: "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
        """
        return {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Federated": oidc_provider_arn,
                    },
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                            f"{oidc_issuer}:aud": "sts.amazonaws.com",
                            f"{oidc_issuer}:sub": f"system:serviceaccount:{namespace}:{service_account_name}",
                        },
                    },
                },
            ],
        }

    def generate_required_tags(self, level: str, hierarchy: dict[str, str]) -> dict[str, str]:
        """Generate tags that must be applied to resources created by the agent.

        These tags are required for cost tracking, audit, and governance.
        They are NOT used for access control (which uses name prefixes).

        Args:
            level: The hierarchy level (platform, org, team, app, user)
            hierarchy: Dictionary with hierarchy values

        Returns:
            Dictionary of tag key-value pairs

        Example:
            >>> service = PolicyScopingService()
            >>> service.generate_required_tags(
            ...     level="app",
            ...     hierarchy={"platform": "bedrockgw", "org": "engineering", "team": "platform", "app": "payments"}
            ... )
            {'platform': 'bedrockgw', 'org': 'engineering', 'team': 'platform', 'app': 'payments', 'managed-by': 'bedrock-gateway'}
        """
        if level not in HIERARCHY_LEVELS:
            raise ValueError(f"Invalid level: {level}. Must be one of {HIERARCHY_LEVELS}")

        tags: dict[str, str] = {}
        level_index = HIERARCHY_LEVELS.index(level)
        levels_to_include = HIERARCHY_LEVELS[: level_index + 1]

        for lvl in levels_to_include:
            value = hierarchy.get(lvl)
            if value:
                tags[lvl] = value

        # Always add managed-by tag
        tags["managed-by"] = "bedrock-gateway"

        return tags

    def generate_tag_enforcement_statements(self, hierarchy: dict[str, str]) -> list[dict]:
        """Generate IAM policy statements requiring tags on resource creation.

        These statements enforce that resources created by the agent have
        the proper hierarchy tags applied. This is for observability and
        cost tracking, not access control.

        Args:
            hierarchy: Dictionary with hierarchy values (only non-empty values are used)

        Returns:
            List of IAM policy statements for tag enforcement

        Note:
            This returns a list of statements, NOT a full policy document.
            The caller should combine these with other statements.
        """
        # Build condition for required tags
        tag_conditions: dict[str, str] = {}
        for key, value in hierarchy.items():
            if value:  # Only add non-empty values
                tag_conditions[f"aws:RequestTag/{key}"] = value

        # Always require managed-by tag
        tag_conditions["aws:RequestTag/managed-by"] = "bedrock-gateway"

        if not tag_conditions:
            return []

        # Common create actions that support tagging
        taggable_actions = [
            # EC2
            "ec2:CreateTags",
            "ec2:RunInstances",
            "ec2:CreateVolume",
            "ec2:CreateSecurityGroup",
            "ec2:CreateVpc",
            "ec2:CreateSubnet",
            # RDS
            "rds:CreateDBInstance",
            "rds:CreateDBCluster",
            "rds:AddTagsToResource",
            # S3
            "s3:CreateBucket",
            "s3:PutBucketTagging",
            # Lambda
            "lambda:CreateFunction",
            "lambda:TagResource",
            # ECS
            "ecs:CreateCluster",
            "ecs:CreateService",
            "ecs:TagResource",
            # SQS
            "sqs:CreateQueue",
            "sqs:TagQueue",
            # SNS
            "sns:CreateTopic",
            "sns:TagResource",
        ]

        return [
            {
                "Sid": "RequireTagsOnCreate",
                "Effect": "Allow",
                "Action": taggable_actions,
                "Resource": "*",
                "Condition": {
                    "StringEquals": tag_conditions,
                },
            },
        ]

    def _get_short_code(self, name: str) -> str:
        """Get the short code for a hierarchy name.

        Uses the explicit mapping if available, otherwise truncates to
        SHORT_CODE_LENGTH characters.

        Args:
            name: The full name to convert

        Returns:
            Short code string (lowercase)
        """
        # Normalize name for lookup
        normalized = name.lower().replace("_", "-").replace(" ", "-")

        # Check explicit mapping first
        if normalized in HIERARCHY_SHORT_CODES:
            return HIERARCHY_SHORT_CODES[normalized]

        # Fall back to truncation
        return normalized[:SHORT_CODE_LENGTH]

    def _truncate_prefix(self, parts: list[str]) -> str:
        """Truncate prefix parts to fit within MAX_PREFIX_LENGTH.

        Args:
            parts: List of prefix parts

        Returns:
            Truncated prefix without trailing hyphen
        """
        # Calculate target length per part
        # Account for hyphens between parts and trailing hyphen
        available_length = MAX_PREFIX_LENGTH - len(parts)  # hyphens
        chars_per_part = max(2, available_length // len(parts))

        truncated_parts = [part[:chars_per_part] for part in parts]
        return "-".join(truncated_parts)
