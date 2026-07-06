environment    = "dev"
account_id     = "879318057152"
aws_region     = "us-east-1"

# GraphRAG OpenSearch Serverless (~$700/mo). Keep DISABLED — not used by the
# code call-graph use case. Re-enable only for full lexical/entity GraphRAG.
graphrag_enabled = false

# Neptune Serverless (~$105/mo) — the code call-graph store (EPIC #1512).
# Decoupled from graphrag_enabled so Neptune can run WITHOUT OpenSearch.
# ENABLED: cluster imported into TF state (issue #2489/#2527); 0-destroy verified.
# CONFIRM_GRAPHRAG_COST=yes repo var required for CI apply.
neptune_enabled = true

# RDS Bootstrap: requires gateway module deployed first.
# Gateway was skipped in this environment — disable until gateway is deployed.
rds_enabled = false
