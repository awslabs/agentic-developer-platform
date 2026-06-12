environment    = "dev"
account_id     = "879318057152"
aws_region     = "us-east-1"

# GraphRAG: Neptune Serverless + OpenSearch Serverless.
# DISABLED for initial deploy (Issue #1406). Saves ~$800/month idle cost.
# Re-enable when GraphRAG is explicitly needed and CONFIRM_GRAPHRAG_COST=yes is set.
graphrag_enabled = false

# RDS Bootstrap: requires gateway module deployed first.
# Gateway was skipped in this environment — disable until gateway is deployed.
rds_enabled = false
