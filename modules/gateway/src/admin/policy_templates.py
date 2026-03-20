"""Policy templates and configurations for the Policy Scoping Service.

Issue #255: This module defines the mapping from agent types to AWS actions,
hierarchy short codes, dangerous actions to deny, and observability actions.
These configurations are stored separately from the service logic for easy extension.

See: aidlc-docs/inception/application-design/agent-platform-architecture.md section 4
"""

from typing import Final

# =============================================================================
# Agent Type to AWS Actions Mapping
# =============================================================================

AGENT_TYPE_ACTIONS: Final[dict[str, list[str]]] = {
    "infra": [
        # EC2 - full access for infrastructure provisioning
        "ec2:*",
        # RDS - database management
        "rds:*",
        # S3 - object storage
        "s3:*",
        # Lambda - serverless functions
        "lambda:*",
        # CloudFormation - infrastructure as code
        "cloudformation:*",
        # IAM PassRole - allow passing roles to services (scoped to prefix)
        "iam:PassRole",
        # ELB - load balancing
        "elasticloadbalancing:*",
        # ECS - container orchestration
        "ecs:*",
        # EKS - Kubernetes (tagging only for resource organization)
        "eks:TagResource",
        "eks:UntagResource",
        "eks:ListTagsForResource",
        # SNS - notifications
        "sns:*",
        # SQS - message queuing
        "sqs:*",
    ],
    "app-dev": [
        # ECR - container registry
        "ecr:*",
        # ECS - container orchestration
        "ecs:*",
        # CodeBuild - build service
        "codebuild:*",
        # S3 - artifact storage
        "s3:*",
        # Lambda - serverless functions
        "lambda:*",
        # CloudWatch Logs - application logging
        "logs:*",
        # CodePipeline - CI/CD pipelines
        "codepipeline:*",
        # CodeDeploy - deployment automation
        "codedeploy:*",
    ],
    "monitoring": [
        # CloudWatch - metrics and dashboards
        "cloudwatch:*",
        # CloudWatch Logs - log analysis
        "logs:*",
        # X-Ray - distributed tracing
        "xray:*",
        # SNS - alert notifications (publish only)
        "sns:Publish",
        # Synthetics - canary tests
        "synthetics:*",
        # RUM - real user monitoring
        "rum:*",
    ],
    "security": [
        # IAM - read-only for security auditing
        "iam:Get*",
        "iam:List*",
        # AWS Config - compliance rules
        "config:*",
        # Security Hub - security findings
        "securityhub:*",
        # GuardDuty - threat detection (read-only)
        "guardduty:Get*",
        # Inspector - vulnerability scanning
        "inspector2:*",
        # Access Analyzer - IAM policy analysis
        "access-analyzer:*",
    ],
    "general": [
        # LLM-only agents - just gateway access
        "execute-api:Invoke",
    ],
}

# =============================================================================
# Hierarchy Short Codes
# =============================================================================
# Maps full hierarchy names to short codes to keep prefixes within AWS name limits.
# Explicit mappings take precedence over truncation.

HIERARCHY_SHORT_CODES: Final[dict[str, str]] = {
    # Platform names
    "bedrockgateway": "bgw",
    "bedrockgw": "bgw",
    "bedrock-gateway": "bgw",
    # Common org names
    "engineering": "eng",
    "operations": "ops",
    "marketing": "mkt",
    "sales": "sls",
    "finance": "fin",
    "humanresources": "hr",
    "research": "res",
    "development": "dev",
    "production": "prod",
    "infrastructure": "infra",
    # Common team names
    "platform": "plat",
    "frontend": "fe",
    "backend": "be",
    "fullstack": "fs",
    "devops": "dops",
    "security": "sec",
    "analytics": "anlyt",
    "machine-learning": "ml",
    "data-science": "ds",
    # Common app names
    "payments": "pay",
    "checkout": "chk",
    "authentication": "auth",
    "authorization": "authz",
    "notification": "notif",
    "messaging": "msg",
    "reporting": "rpt",
}

# Default short code length when truncating
SHORT_CODE_LENGTH: Final[int] = 3

# Maximum prefix length to stay within AWS naming limits
# IAM role: 64 chars, S3 bucket: 63 chars - leave room for agent-specific suffixes
MAX_PREFIX_LENGTH: Final[int] = 20

# =============================================================================
# Dangerous Actions (Always Denied)
# =============================================================================
# These actions are ALWAYS denied in permission boundaries regardless of agent type.
# They prevent privilege escalation and protect critical AWS infrastructure.

DANGEROUS_ACTIONS: Final[list[str]] = [
    # IAM user/role creation - prevent privilege escalation
    "iam:CreateUser",
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:DeleteUser",
    "iam:AttachUserPolicy",
    "iam:AttachRolePolicy",
    "iam:PutUserPolicy",
    "iam:PutRolePolicy",
    "iam:UpdateAssumeRolePolicy",
    "iam:CreateAccessKey",
    # Organizations - prevent org structure modification
    "organizations:*",
    # Account management - prevent billing/account changes
    "account:*",
    # STS role assumption - prevent lateral movement (except within allowed scope)
    "sts:AssumeRole",
    # KMS key management - prevent key creation/deletion
    "kms:CreateKey",
    "kms:DeleteKey",
    "kms:ScheduleKeyDeletion",
    # CloudTrail - prevent audit trail tampering
    "cloudtrail:DeleteTrail",
    "cloudtrail:StopLogging",
    "cloudtrail:UpdateTrail",
    # GuardDuty - prevent threat detection bypass
    "guardduty:DeleteDetector",
    "guardduty:DisassociateFromMasterAccount",
]

# =============================================================================
# Observability Actions (Always Allowed)
# =============================================================================
# These read-only actions are always allowed for all agents.
# They enable observability without granting any modification permissions.

OBSERVABILITY_ACTIONS: Final[list[str]] = [
    # CloudWatch - metrics and dashboards (read-only)
    "cloudwatch:GetMetricData",
    "cloudwatch:GetMetricStatistics",
    "cloudwatch:ListMetrics",
    "cloudwatch:GetDashboard",
    "cloudwatch:ListDashboards",
    "cloudwatch:DescribeAlarms",
    # CloudWatch Logs - log analysis (read-only)
    "logs:GetLogEvents",
    "logs:GetLogRecord",
    "logs:GetLogGroupFields",
    "logs:GetQueryResults",
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:FilterLogEvents",
    "logs:StartQuery",
    "logs:StopQuery",
    # X-Ray - distributed tracing (read-only)
    "xray:GetTraceSummaries",
    "xray:GetTraceGraph",
    "xray:GetServiceGraph",
    "xray:GetTimeSeriesServiceStatistics",
    # STS - identity verification
    "sts:GetCallerIdentity",
]

# =============================================================================
# Hierarchy Levels
# =============================================================================
# Valid hierarchy levels from most privileged to least privileged

HIERARCHY_LEVELS: Final[list[str]] = [
    "platform",  # Platform admin - can touch everything
    "org",  # Organization level - can touch org resources
    "team",  # Team level - can touch team resources
    "app",  # App level - can touch app resources
    "user",  # User level - personal agent, most restricted
]

# =============================================================================
# Policy Version
# =============================================================================
# AWS IAM policy version - should always be "2012-10-17"

IAM_POLICY_VERSION: Final[str] = "2012-10-17"

# =============================================================================
# Agent Type Descriptions
# =============================================================================
# Human-readable descriptions for each agent type (used by API endpoints)

AGENT_TYPE_DESCRIPTIONS: Final[dict[str, str]] = {
    "infra": "Infrastructure provisioning and management (EC2, RDS, S3, Lambda, CloudFormation, etc.)",
    "app-dev": "Application development and deployment (ECR, ECS, CodeBuild, CodePipeline, etc.)",
    "monitoring": "Observability and alerting (CloudWatch, X-Ray, Synthetics, RUM, etc.)",
    "security": "Security auditing and scanning (IAM read-only, Config, SecurityHub, GuardDuty, etc.)",
    "general": "LLM-only tasks with no AWS service access (gateway invoke only)",
}
