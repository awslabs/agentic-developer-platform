# FINDINGS: Issue #42 — VPC Link v2 + ALB Direct Integration

**Date**: 2026-04-19
**PR**: #46
**Issue**: #42
**Reviewer**: @agent-reviewer

## Summary

Replaced the v1 VPC Link (NLB-only, `aws_api_gateway_vpc_link`) with a v2 VPC Link
(`aws_apigatewayv2_vpc_link`) that connects the REST API directly to the ALB, eliminating
the NLB from the data path.

## Key Finding: `integrationTarget` in OpenAPI Body

The issue and user comment #8 stated that `x-amazon-apigateway-integration` does NOT
support `integrationTarget` in the OpenAPI body, requiring a `null_resource + local-exec`
post-deploy workaround.

**Actual behavior**: API Gateway REQUIRES `integrationTarget` in the body when using a v2
VPC Link. Without it, `put-rest-api` rejects the body with:
```
IntegrationTarget is required for VpcLinkV2 <id>
```

This means the inline approach is not only simpler but mandatory. No `null_resource` is needed.

## Architecture Before (Wrong)

```
Client -> REST API -> v1 VPC Link (FAILED, nt4vl8) -> ALB (rejected: "NLB ARN is malformed")
                   -> v1 VPC Link (epm39t) -> NLB (adp-dev-apigw-nlb) -> ALB -> EKS
```

- v1 VPC Link `nt4vl8`: Created by Terraform with ALB ARN — permanently FAILED
- v1 VPC Link `epm39t`: Runtime workaround from #40 agent, pointed at manually-created NLB
- NLB `adp-dev-apigw-nlb`: Runtime drift, unnecessary extra hop

## Architecture After (Correct)

```
Client -> REST API -> v2 VPC Link (apigatewayv2) -> ALB -> EKS
```

- Single v2 VPC Link with dedicated SG
- `integrationTarget = ALB ARN` set inline in each integration
- No NLB in the path

## Changes Made

| File | Change |
|------|--------|
| `modules/gateway/infra/modules/api-gateway/main.tf` | Replace `aws_api_gateway_vpc_link` with `aws_apigatewayv2_vpc_link`; add VPC Link SG + ALB ingress rule; switch OpenAPI to Swagger 2.0 with inline `integrationTarget` |
| `modules/gateway/infra/modules/api-gateway/variables.tf` | Add `alb_security_group_ids` variable |
| `modules/gateway/infra/modules/api-gateway/outputs.tf` | Update outputs for v2 VPC Link + add `vpc_link_security_group_id` |
| `modules/gateway/infra/main.tf` | Thread `internal_alb_dns` and `alb_security_group_ids` to api-gateway module |
| `modules/gateway/infra/variables.tf` | Add `internal_alb_dns` and `alb_security_group_ids` root variables |
| `modules/agent-factory/infra/modules/runner-iam/main.tf` | Add `execute-api:Invoke` to identity policy; add `execute-api:*` to boundary |

## Reviewer Fixes (Applied in Review)

1. **Contradictory comment**: Lines 25-29 said integrationTarget is NOT supported and referenced
   null_resource, but the code uses it inline. Fixed comment to match actual behavior.
2. **Empty SG edge case**: `aws_security_group.vpc_link` had inline `egress` block with
   `security_groups = var.alb_security_group_ids`. When default `[]` is used (initial deploy),
   this creates an egress rule with no destination. Changed to `dynamic "egress"` block that
   only creates the rule when ALB SG IDs are provided.

## Post-Merge Cleanup Required

After `terraform apply`:
- Delete v1 VPC Link `nt4vl8` (FAILED): `aws apigateway delete-vpc-link --vpc-link-id nt4vl8`
- Delete v1 VPC Link `epm39t` (NLB workaround): `aws apigateway delete-vpc-link --vpc-link-id epm39t`
- Delete NLB `adp-dev-apigw-nlb` + its target group
- Delete runtime IAM inline policy `execute-api-invoke-issue40` (Terraform now manages this)

## Verification Commands

```bash
# No v1 VPC Links
aws apigateway get-vpc-links --region us-east-1

# One v2 VPC Link, AVAILABLE
aws apigatewayv2 get-vpc-links --region us-east-1

# No orphan NLB
aws elbv2 describe-load-balancers --region us-east-1 \
  --query 'LoadBalancers[?contains(LoadBalancerName,`apigw-nlb`)]'

# Probes
API_URL=https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev
curl -sS "$API_URL/v1/health"                          # expect 200
curl -sS -o /dev/null -w "%{http_code}" "$API_URL/agent/v1/health"  # expect 403
```

## Manual Proof

The user ran the manual proof from issue section 2 (comment #8):
- Created throwaway v2 VPC Link `e241au`
- Swapped `/{proxy+}` integration to v2 link + ALB `--integration-target`
- `curl /health` returned HTTP 200 `{"status":"healthy"}` from the backend
- Probe VPC Link cleaned up after verification
