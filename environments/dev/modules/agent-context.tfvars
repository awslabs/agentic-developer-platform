environment    = "dev"
account_id     = "879318057152"
aws_region     = "us-east-1"

# GraphRAG: Neptune Serverless + OpenSearch Serverless.
# Enabled for this env. Expect ~$800/month idle (Neptune ~$105, OpenSearch ~$700).
# Repo default stays `false` in variables.tf to prevent accidental enablement in other envs.
graphrag_enabled = true
