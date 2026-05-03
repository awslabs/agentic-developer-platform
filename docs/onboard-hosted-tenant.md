# Onboarding a Hosted Tenant

This runbook covers the steps to onboard a new organization (tenant) to the ADP hosted platform.

## Prerequisites

- Gateway admin API is running and accessible
- Identity-index DynamoDB table exists (`adp-dev-identity-index`)
- GitHub App is installed on the tenant's org

## Step 1: Create the Organization

```bash
curl -X POST "${GATEWAY_API_URL}/api/admin/identity/organizations" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "acme-corp",
    "name": "Acme Corp",
    "channels": {
      "github": [{"installation_id": "12345678"}]
    },
    "settings": {
      "user_auto_provision_mode": "strict"
    }
  }'
```

This creates:
- Organization record in Postgres
- Default department + team
- `channel_tenant_map` row
- DynamoDB identity-index entry: `identity_type=github_installation_id, identity_value=12345678 -> org_id=acme-corp`

## Step 2: Seed Users

**This is required for webhooks to be accepted.** Without seeded users, the webhook Lambda will reject all events with `403 unknown_user`.

For each user who should be able to trigger agents:

```bash
curl -X POST "${GATEWAY_API_URL}/api/admin/identity/organizations/acme-corp/users" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@acme.com",
    "name": "Alice Smith",
    "role": "developer",
    "identities": [
      {
        "provider": "github",
        "provider_user_id": "999",
        "provider_username": "alice"
      }
    ],
    "send_invite": true
  }'
```

This creates:
- User record in Postgres
- `user_identities` row linking GitHub ID to user
- DynamoDB identity-index entry: `identity_type=github_user, identity_value=999 -> org_id=acme-corp, user_id=u_xxx`
- Cognito user + email invite (if `send_invite: true`)

## Step 3: Verify Identity Resolution

Test that the webhook Lambda can resolve the tenant + user:

```bash
# Check installation entry
aws dynamodb get-item \
  --table-name adp-dev-identity-index \
  --key '{"identity_type":{"S":"github_installation_id"},"identity_value":{"S":"12345678"}}' \
  --query 'Item.org_id.S'

# Check user entry
aws dynamodb get-item \
  --table-name adp-dev-identity-index \
  --key '{"identity_type":{"S":"github_user"},"identity_value":{"S":"999"}}' \
  --query 'Item.{org_id:org_id.S,user_id:user_id.S}'
```

## Step 4: Test Webhook Flow

Label an issue with `developer` in the tenant's repo. Expected:
- Webhook Lambda returns `202 Accepted`
- SQS message includes `actor.user_id` and `actor.org_id`
- Agent pod picks up the job

## Auto-Provision Mode (Optional)

For demo tenants or orgs that want automatic user creation on first webhook:

```bash
curl -X PATCH "${GATEWAY_API_URL}/api/admin/identity/organizations/acme-corp" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {
      "user_auto_provision_mode": "auto_provision"
    }
  }'
```

With this mode, unknown GitHub senders trigger automatic user creation via the Gateway admin API. The webhook is accepted (202) after provisioning succeeds.

**Default is `strict`** — unknown senders get `403 Forbidden`.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `403 unknown_installation` | Installation ID not in identity-index | Re-run org creation or check `channels.github` |
| `403 unknown_user` | Sender's GitHub ID not in identity-index | Seed the user (Step 2) or enable auto-provision |
| `403 cross_tenant_identity` | User exists but in different org | Check user's `org_id` matches installation's `org_id` |
| `500` after auto-provision | Gateway API unreachable | Check `GATEWAY_API_URL` env var and network connectivity |
