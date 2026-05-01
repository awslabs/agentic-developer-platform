# Webhook Ingress

Foundational infrastructure for the hosted multi-tenant webhook ingress layer. Receives GitHub webhooks via HTTP API Gateway v2, validates signatures, looks up tenants, and queues work onto SQS FIFO for downstream agent processing.

## Architecture

```
GitHub ──POST /github──► API Gateway HTTP v2 ──► Lambda (HMAC validate + tenant lookup)
                              │                        │
                              │ WAF rate-limit         ├──► DynamoDB (tenant-registry, events, rate-limits)
                              │                        │
                              │                        └──► SQS FIFO (agent-submit)
                              │                                  │
                              │                                  └──► DLQ (after 3 failures)
```

## Resources

| Resource | Name Pattern | Purpose |
|----------|-------------|---------|
| HTTP API v2 | `adp-<env>-webhook-ingress` | Webhook endpoint |
| Lambda | `adp-<env>-github-webhook` | Validate + queue events |
| SQS FIFO | `adp-<env>-agent-submit.fifo` | Agent work queue |
| SQS DLQ | `adp-<env>-agent-submit-dlq.fifo` | Failed message inspection |
| DynamoDB | `adp-<env>-tenant-registry` | Installation → tenant mapping |
| DynamoDB | `adp-<env>-webhook-events` | Audit log of webhook events |
| DynamoDB | `adp-<env>-rate-limits` | Per-tenant rate limiting |
| WAF | `adp-<env>-webhook-waf` | IP-based rate limiting |
| Secret | `adp/<env>/webhook-ingress/github-webhook-secret` | HMAC validation key |

## Deployment

```bash
cd modules/agent-factory/webhook-ingress/infra

terraform init -backend-config=../../../../environments/dev/modules/webhook-ingress-backend.tfvars -input=false
terraform plan -var="environment=dev"
terraform apply -var="environment=dev" -auto-approve
```

## Validation

```bash
# API Gateway exists
aws apigatewayv2 get-apis --query 'Items[?starts_with(Name,`adp-dev-webhook`)].{Name:Name,Endpoint:ApiEndpoint}'

# SQS queue exists
aws sqs get-queue-url --queue-name adp-dev-agent-submit.fifo

# DDB tables exist
aws dynamodb describe-table --table-name adp-dev-tenant-registry --query 'Table.TableStatus'
aws dynamodb describe-table --table-name adp-dev-webhook-events --query 'Table.TableStatus'
aws dynamodb describe-table --table-name adp-dev-rate-limits --query 'Table.TableStatus'

# Stub Lambda responds
ENDPOINT=$(aws apigatewayv2 get-apis --query 'Items[?starts_with(Name,`adp-dev-webhook`)].ApiEndpoint' --output text)
curl -s -X POST "${ENDPOINT}/github" -d '{}' | jq .
```

## Design Decisions

- **HTTP API v2 over REST API v1**: ~71% cheaper, lower latency, sufficient for webhook ingress
- **FIFO queue**: Ordered processing per tenant via `MessageGroupId = installation_id`
- **Content-based deduplication**: Prevents duplicate webhook deliveries (GitHub retries)
- **2-hour visibility timeout**: Matches max agent run time
- **Separate from Bedrock Gateway API**: Different auth model, different blast radius
- **WAF rate-limit**: 1000 req/5min per IP protects against abuse before Lambda executes
- **DynamoDB PAY_PER_REQUEST**: Cost-efficient for bursty webhook traffic

## Related Issues

- #308 — Parent EPIC: Hosted multi-tenant ADP
- #318 — GitHub webhook Lambda (actual HMAC + routing logic)
