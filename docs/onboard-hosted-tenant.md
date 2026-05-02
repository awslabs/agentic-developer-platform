# Onboarding a Hosted Tenant

This document describes the single-API-call path for onboarding a new tenant
to the ADP platform. As of Issue #377 (Phase C tenant-identity migration),
all tenant identity is managed through the organizations admin API.

## Prerequisites

- Admin access token (platform_admin role or admins Cognito group)
- Gateway endpoint URL
- Tenant's GitHub App installation ID(s) (if using Agent Factory)
- Tenant's AWS account details (if using cross-account Bedrock access)

## Create Organization

```bash
curl -X POST https://<gateway>/api/admin/organizations \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "acme-corp",
    "name": "ACME Corp",
    "github_installation_ids": ["12345678"],
    "cognito_client_ids": [],
    "aws_accounts": [{"role_arn": "arn:aws:iam::123456789012:role/adp-cross-account", "external_id": "acme-ext-id"}]
  }'
```

### Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique org identifier (slug format, e.g. `acme-corp`) |
| `name` | string | Yes | Human-readable organization name |
| `github_installation_ids` | string[] | No | GitHub App installation IDs for webhook routing |
| `cognito_client_ids` | string[] | No | Cognito App Client IDs associated with this org |
| `aws_accounts` | object[] | No | Cross-account AWS roles for Bedrock access |

### Response

```json
{
  "id": "acme-corp",
  "name": "ACME Corp",
  "github_installation_ids": ["12345678"],
  "cognito_client_ids": [],
  "aws_accounts": [{"role_arn": "arn:aws:iam::123456789012:role/adp-cross-account", "external_id": "acme-ext-id"}],
  "created_at": "2026-05-02T12:00:00Z",
  "status": "active"
}
```

## Verify Webhook Routing

After creating the organization, verify that webhook events from the tenant's
GitHub installation are routed correctly:

```bash
# Trigger a test webhook (e.g., create a branch in the tenant's repo)
# Then check the webhook events:
curl -s https://<gateway>/api/admin/organizations/acme-corp/events \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq '.items[0]'
```

## Rollback: Delete Organization

If something goes wrong, a single DELETE call tears down all identity entries:

```bash
curl -X DELETE https://<gateway>/api/admin/organizations/acme-corp \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

This removes:
- The organization record from the identity-index
- All associated GitHub installation ID mappings
- All associated Cognito client ID mappings
- All associated AWS account role mappings

It does **not** remove:
- The tenant's GitHub App installation (managed in GitHub)
- Any Cognito App Clients (must be deleted separately via the agents API)
- Any data already processed (webhook events, agent runs)

## Add Cognito App Clients (Agents)

To create M2M (service-to-service) credentials for the tenant:

```bash
curl -X POST https://<gateway>/api/admin/agents \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "acme-data-pipeline",
    "org_id": "acme-corp",
    "team_id": "platform",
    "scopes": ["bedrockgw/invoke"],
    "description": "ACME data pipeline agent"
  }'
```

The response includes `client_id` and instructions for retrieving `client_secret`.

## Troubleshooting

### Webhook events not routing

1. Verify the GitHub installation ID matches: `gh api /app/installations/<id>`
2. Check the organization has the correct `github_installation_ids`
3. Check CloudWatch logs for the webhook ingress Lambda

### Token missing org claims

1. Verify the Cognito App Client ID is listed in the organization's `cognito_client_ids`
2. The gateway resolves org context at request time from the identity-index
3. Check gateway logs for identity resolution errors
