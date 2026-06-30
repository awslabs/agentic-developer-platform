# Issue #2527 — Permanent Neptune Wiring (EPIC #2433)

## Date: 2026-06-30
## Agent: @agent-operations

## What Was Done
Safely landed the permanent Neptune Terraform wiring so the graph database survives redeploys without manual patching. This was a state-surgery operation with a critical guardrail (never apply if plan shows destroys).

## Key Decisions

### 1. Import was already done
The 9 Neptune resources were already imported into TF state from a prior run. Always check `terraform state list` before attempting imports — may already be reconciled.

### 2. Targeted applies to avoid ECR destruction
Full `terraform apply` would have destroyed 5 ECR repositories (AES256→KMS encryption drift, forces replacement). Used `-target` to apply ONLY the Neptune-related resources:
- `module.iam.aws_iam_role_policy.neptune[0]` (IRSA inline policy)
- `aws_ssm_parameter.neptune_endpoint[0]` (SSM endpoint)
- `aws_ssm_parameter.neptune_port[0]` (SSM port)

### 3. Redundant managed policy detached
The manual `aws iam attach-role-policy` attachment of `adp-dev-eks-cluster-neptune-access` on the IRSA role was detached since TF now manages an inline policy with equivalent permissions. No gap in coverage — the inline policy `adp-dev-agent-context-neptune` was created first.

## Technical Details

### Resource identifiers
- Neptune cluster: `adp-dev-eks-cluster-graphrag`
- Neptune endpoint: `adp-dev-eks-cluster-graphrag.cluster-civhekhiupfe.us-east-1.neptune.amazonaws.com`
- Neptune SG: `sg-0f35df7343d5a1ed4`
- EKS node SG: `sg-0d08851d0139bb2eb`
- IRSA role: `adp-dev-agent-context-irsa`
- Inline policy name: `adp-dev-agent-context-neptune`
- SSM endpoint: `/adp/dev/agent-context/neptune-endpoint`
- SSM port: `/adp/dev/agent-context/neptune-port`
- TF state bucket: `s3://adp-terraform-state-879318057152/dev/modules/agent-context/terraform.tfstate`
- TF backend config: `environments/dev/modules/agent-context-backend.tfvars`

### Terraform command pattern
```bash
cd modules/agent-context/terraform
terraform init -input=false -backend-config="../../../environments/dev/modules/agent-context-backend.tfvars"
terraform plan -var-file="../../../environments/dev/modules/agent-context.tfvars" -var="neptune_enabled=true"
terraform apply -var-file="..." -target='resource.name' -auto-approve
```

### Pre-existing drift (DO NOT apply without handling)
The agent-context module has ECR repo drift: repositories were created with `AES256` encryption but TF now declares `KMS`. Applying without `-target` would destroy+recreate all 5 ECR repos, losing every image. This needs a separate state surgery (taint/import) or code change to accept AES256.

## Gotchas

1. **The `-var` flag overrides tfvars**: When running `terraform import` or `terraform plan` against a module with `count = var.x ? 1 : 0`, you MUST pass `-var="neptune_enabled=true"` if the tfvars still has `false`. Otherwise TF thinks the resource shouldn't exist.

2. **Import ID format for security group rules**: The import ID for `aws_security_group_rule` is NOT just the rule ID — it's `{sg_id}_{type}_{protocol}_{from_port}_{to_port}_{source}`. E.g., `sg-xxx_ingress_tcp_8182_8182_sg-yyy`.

3. **Agent pod cannot read cross-namespace resources**: The `agent-scaledjob-sa` in `adp-agents` namespace cannot access pods/configmaps in `agent-context` namespace. Verification of configmap injection requires either the deploy workflow to run, or an operator with broader RBAC.

4. **ECR destruction risk is unrelated to Neptune**: The 5 destroys in the plan are ECR repos, not Neptune. Always grep for the specific resource type you care about when evaluating plan safety.

5. **SSM parameter path convention**: `/adp/{env}/agent-context/{param-name}` — same pattern used by `ingestion-queue-url`. Always follow existing patterns.

## What Didn't Work
- Initial assumption that import needed to be run — it was already done. Wasted a few minutes on the import attempt before checking state list.

## Recommendations
1. The ECR encryption drift should be resolved in a separate issue (either update TF to accept AES256, or carefully migrate repos to KMS with image preservation).
2. Issue #2526 (driver staleness) is independent and should be merged separately via its own image build.
3. Issue #2493 (backfill) can run after the next deploy once SSM endpoint is confirmed working in pods.
4. After PR merge, trigger `agent-context-deploy.yml` to verify NEPTUNE_ENDPOINT propagates to pods from SSM.
