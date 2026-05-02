# =============================================================================
# DynamoDB Table for Agent Client Metadata — DECOMMISSIONED (Issue #377)
# =============================================================================
#
# The agent-clients DynamoDB table was removed in Phase C of the tenant-identity
# migration. Agent/service client metadata is now stored in the organizations
# identity-index and resolved via the gateway admin API.
#
# PITR snapshot exported to S3 before destruction (see PR description).
# =============================================================================
