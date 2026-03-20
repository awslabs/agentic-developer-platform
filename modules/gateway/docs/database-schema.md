# Bedrock Gateway Database Schema

This document describes the PostgreSQL database schema for Bedrock Gateway, including all tables, columns, relationships, and constraints.

## Overview

The database uses PostgreSQL with SQLAlchemy ORM. All tenant-scoped tables include an `org_id` column for multi-tenant isolation (enforced via the `TenantMixin`).

## Entity Relationship Diagram

```
+------------------+       +------------------+       +------------------+
|  organizations   |       |   departments    |       |      teams       |
+------------------+       +------------------+       +------------------+
| PK id            |<------| PK id            |<------| PK id            |
|    name          |  1:N  |    org_id (FK)   |  1:N  |    org_id (FK)   |
|    aws_accounts  |       |    name          |       |    department_id |
|    role_mappings |       |    description   |       |    name          |
|    settings      |       |    budget_limit  |       |    description   |
|    created_at    |       |    cognito_group |       |    created_at    |
+------------------+       |    created_at    |       +------------------+
                           +------------------+               |
                                                              | 1:N
                                                              v
+------------------+       +------------------+       +------------------+
|      tokens      |       |      users       |       | service_accounts |
+------------------+       +------------------+       +------------------+
| PK id            |       | PK id            |       | PK id            |
|    org_id (FK)   |       |    org_id (FK)   |       |    org_id (FK)   |
|    token_hash    |       |    team_id (FK)  |       |    department_id |
|    entity_type   |       |    email         |       |    team_id (FK)  |
|    entity_id     |       |    name          |       |    name          |
|    team_id       |       |    role          |       |    description   |
|    department_id |       |    cognito_sub   |       |    iam_role_arn  |
|    is_admin      |       |    created_at    |       |    created_at    |
|    expires_at    |       +------------------+       +------------------+
|    revoked_at    |
+------------------+

+------------------+       +------------------+       +------------------+
|  budget_configs  |       |  budget_usage    |       |   usage_logs     |
+------------------+       +------------------+       +------------------+
| PK id            |       | PK id            |       | PK id            |
|    org_id (FK)   |       |    org_id (FK)   |       |    org_id (FK)   |
|    entity_type   |       |    entity_type   |       |    timestamp     |
|    entity_id     |       |    entity_id     |       |    department_id |
|    period_type   |       |    period_start  |       |    team_id       |
|    budget_amount |       |    period_type   |       |    user_id       |
|    enforcement   |       |    total_cost    |       |    account_type  |
|    updated_at    |       |    total_tokens  |       |    model         |
+------------------+       |    request_count |       |    input_tokens  |
                           +------------------+       |    output_tokens |
                                                      |    cost_usd      |
+------------------+       +------------------+       |    latency_ms    |
| rate_limit_configs|      | bedrock_pool_accts|      |    status_code   |
+------------------+       +------------------+       +------------------+
| PK id            |       | PK id            |
|    org_id (FK)   |       |    account_id    |       +------------------+
|    entity_type   |       |    role_arn      |       |  model_aliases   |
|    entity_id     |       |    region        |       +------------------+
|    rpm           |       |    is_healthy    |       | PK id            |
|    tpm           |       |    last_check    |       |    org_id (FK)   |
|    concurrent    |       |    created_at    |       |    alias_name    |
|    updated_at    |       +------------------+       |    bedrock_model |
+------------------+                                  +------------------+

+------------------+
|  model_pricing   |
+------------------+
| PK model_id      |
|    input_price   |
|    output_price  |
|    updated_at    |
+------------------+
```

## Tables

### organizations

Core tenant table. Each organization is a separate tenant with isolated data.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Organization unique identifier |
| `name` | VARCHAR(255) | NOT NULL, UNIQUE | Organization name |
| `aws_accounts` | JSON | NOT NULL, DEFAULT [] | List of AWS account IDs associated with org |
| `role_mappings` | JSON | NOT NULL, DEFAULT {} | IAM role to department/team/admin mappings |
| `settings` | JSON | NOT NULL, DEFAULT {} | Organization-level settings (model access, etc.) |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Creation timestamp |

**Indexes:**
- Primary key on `id`
- Unique index on `name`

---

### departments

Organizational departments within a tenant. Created via Admin API or synced from Cognito groups.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Department unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `name` | VARCHAR(255) | NOT NULL | Department name |
| `description` | TEXT | NULLABLE | Department description |
| `budget_limit` | NUMERIC(15,2) | NULLABLE | Department budget limit (USD) |
| `cognito_group_name` | VARCHAR(255) | NULLABLE | Cognito User Pool group name |
| `identity_center_group_id` | VARCHAR(255) | NULLABLE | Legacy: Identity Center group ID |
| `synced_at` | TIMESTAMP WITH TZ | NULLABLE | Last sync from identity provider |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Creation timestamp |
| `updated_at` | TIMESTAMP WITH TZ | ON UPDATE NOW() | Last update timestamp |

**Indexes:**
- Primary key on `id`
- Index on `org_id`

---

### teams

Teams within departments. Teams are the primary unit for budget and rate limit assignment.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Team unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `department_id` | VARCHAR(255) | NOT NULL, INDEX | Department foreign key |
| `name` | VARCHAR(255) | NOT NULL | Team name |
| `description` | TEXT | NULLABLE | Team description |
| `identity_center_group_id` | VARCHAR(255) | NULLABLE | Legacy: Identity Center group ID |
| `synced_at` | TIMESTAMP WITH TZ | NULLABLE | Last sync from identity provider |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Creation timestamp |
| `updated_at` | TIMESTAMP WITH TZ | ON UPDATE NOW() | Last update timestamp |

**Indexes:**
- Primary key on `id`
- Index on `org_id`
- Index on `department_id`

---

### users

Human users within teams. User identity sourced from Cognito.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | User unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `team_id` | VARCHAR(255) | NOT NULL, INDEX | Team foreign key |
| `email` | VARCHAR(255) | NOT NULL | User email address |
| `name` | VARCHAR(255) | NULLABLE | User display name |
| `role` | VARCHAR(64) | NULLABLE | User role (admin, user, etc.) |
| `cognito_sub` | VARCHAR(255) | NULLABLE, INDEX | Cognito user sub (unique identifier) |
| `cognito_username` | VARCHAR(255) | NULLABLE | Cognito username |
| `identity_center_user_id` | VARCHAR(255) | NULLABLE | Legacy: Identity Center user ID |
| `synced_at` | TIMESTAMP WITH TZ | NULLABLE | Last sync from identity provider |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Creation timestamp |
| `updated_at` | TIMESTAMP WITH TZ | ON UPDATE NOW() | Last update timestamp |

**Indexes:**
- Primary key on `id`
- Index on `org_id`
- Index on `team_id`
- Index on `cognito_sub`

---

### service_accounts

Machine identities for automated agents (CI/CD pipelines, EKS containers).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Service account unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `department_id` | VARCHAR(255) | NOT NULL | Department assignment |
| `team_id` | VARCHAR(255) | NOT NULL | Team assignment |
| `name` | VARCHAR(255) | NOT NULL | Service account name |
| `description` | TEXT | NULLABLE | Description of service account purpose |
| `iam_role_arn` | VARCHAR(512) | NOT NULL, UNIQUE | IAM role ARN for authentication |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Creation timestamp |

**Indexes:**
- Primary key on `id`
- Index on `org_id`
- Unique index on `iam_role_arn`

---

### tokens

Authentication tokens (legacy - gateway tokens). Note: With Cognito JWT auth, this table is used for token revocation tracking only.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Token unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `token_hash` | VARCHAR(64) | NOT NULL, UNIQUE, INDEX | SHA-256 hash of token |
| `entity_type` | VARCHAR(20) | NOT NULL | Type: 'user' or 'service_account' |
| `entity_id` | VARCHAR(255) | NOT NULL | User ID or service account ID |
| `team_id` | VARCHAR(255) | NOT NULL | Team assignment |
| `department_id` | VARCHAR(255) | NOT NULL | Department assignment |
| `is_admin` | BOOLEAN | DEFAULT FALSE | Admin privileges flag |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Token creation time |
| `expires_at` | TIMESTAMP WITH TZ | NOT NULL | Token expiration time |
| `revoked_at` | TIMESTAMP WITH TZ | NULLABLE | Revocation timestamp (if revoked) |

**Indexes:**
- Primary key on `id`
- Index on `org_id`
- Unique index on `token_hash`

---

### budget_configs

Budget configuration at organization, department, team, or user level.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Budget config unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL | Organization foreign key |
| `entity_type` | VARCHAR(20) | NOT NULL | Type: org/department/team/user/service_account |
| `entity_id` | VARCHAR(255) | NOT NULL | Entity ID for budget assignment |
| `period_type` | VARCHAR(10) | NOT NULL | Period: daily/weekly/monthly |
| `budget_amount_usd` | NUMERIC(10,2) | NOT NULL | Budget amount in USD |
| `enforcement_mode` | VARCHAR(10) | NOT NULL, DEFAULT 'hard' | Mode: soft (warn) or hard (block) |
| `updated_at` | TIMESTAMP WITH TZ | DEFAULT NOW(), ON UPDATE NOW() | Last update timestamp |

**Constraints:**
- UNIQUE on (`org_id`, `entity_type`, `entity_id`, `period_type`)

**Indexes:**
- Primary key on `id`
- Unique constraint index

---

### budget_usage

Aggregated budget usage per entity per period. Updated after each request.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Usage record unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL | Organization foreign key |
| `entity_type` | VARCHAR(20) | NOT NULL | Type: org/department/team/user/service_account |
| `entity_id` | VARCHAR(255) | NOT NULL | Entity ID |
| `period_start` | DATE | NOT NULL | Start of budget period |
| `period_type` | VARCHAR(10) | NOT NULL | Period: daily/weekly/monthly |
| `total_cost_usd` | NUMERIC(10,2) | NOT NULL, DEFAULT 0 | Total spend in period |
| `total_tokens` | INTEGER | DEFAULT 0 | Total tokens used |
| `request_count` | INTEGER | DEFAULT 0 | Number of requests |

**Constraints:**
- UNIQUE on (`org_id`, `entity_type`, `entity_id`, `period_start`, `period_type`)

**Indexes:**
- Primary key on `id`
- Unique constraint index

---

### usage_logs

Per-request usage logging for audit and analytics.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Log entry unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `timestamp` | TIMESTAMP WITH TZ | DEFAULT NOW(), INDEX | Request timestamp |
| `department_id` | VARCHAR(255) | NOT NULL | Department of requester |
| `team_id` | VARCHAR(255) | NOT NULL | Team of requester |
| `user_id` | VARCHAR(255) | NOT NULL, INDEX | User or service account ID |
| `account_type` | VARCHAR(20) | NOT NULL, DEFAULT 'human' | Type: human/service |
| `model` | VARCHAR(255) | NOT NULL | Bedrock model ID |
| `input_tokens` | INTEGER | NOT NULL | Input token count |
| `output_tokens` | INTEGER | NOT NULL | Output token count |
| `cost_usd` | NUMERIC(10,6) | NOT NULL | Request cost in USD |
| `latency_ms` | INTEGER | NOT NULL | Request latency in milliseconds |
| `status_code` | INTEGER | NOT NULL | HTTP response status code |
| `request_id` | VARCHAR(255) | NULLABLE | Request correlation ID |
| `bedrock_account_id` | VARCHAR(12) | NULLABLE | Bedrock pool account used |

**Indexes:**
- Primary key on `id`
- Index on `org_id`
- Index on `timestamp`
- Index on `user_id`

---

### rate_limit_configs

Rate limit configuration per entity.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Config unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `entity_type` | VARCHAR(20) | NOT NULL | Type: organization/department/team/user/service_account |
| `entity_id` | VARCHAR(255) | NOT NULL | Entity ID |
| `rpm` | INTEGER | NULLABLE | Requests per minute limit |
| `tpm` | INTEGER | NULLABLE | Tokens per minute limit |
| `concurrent_requests` | INTEGER | NULLABLE | Max concurrent requests |
| `updated_at` | TIMESTAMP WITH TZ | DEFAULT NOW(), ON UPDATE NOW() | Last update timestamp |

**Indexes:**
- Primary key on `id`
- Index on `org_id`

---

### bedrock_pool_accounts

Cross-account Bedrock pool configuration.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Pool entry unique identifier |
| `account_id` | VARCHAR(12) | NOT NULL | AWS account ID |
| `role_arn` | VARCHAR(512) | NOT NULL, UNIQUE | Cross-account IAM role ARN |
| `region` | VARCHAR(20) | NOT NULL | AWS region |
| `is_healthy` | BOOLEAN | DEFAULT TRUE | Health status |
| `last_health_check` | TIMESTAMP WITH TZ | NULLABLE | Last health check timestamp |
| `created_at` | TIMESTAMP WITH TZ | DEFAULT NOW() | Creation timestamp |

**Indexes:**
- Primary key on `id`
- Unique index on `role_arn`

---

### model_aliases

Organization-specific model aliases for friendly naming.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | VARCHAR(255) | PRIMARY KEY, DEFAULT uuid | Alias unique identifier |
| `org_id` | VARCHAR(255) | NOT NULL, INDEX | Organization foreign key |
| `alias_name` | VARCHAR(255) | NOT NULL | Friendly model alias name |
| `bedrock_model_id` | VARCHAR(255) | NOT NULL | Actual Bedrock model ID |

**Indexes:**
- Primary key on `id`
- Index on `org_id`

---

### model_pricing

Bedrock model pricing for cost calculation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `model_id` | VARCHAR(255) | PRIMARY KEY | Bedrock model ID |
| `input_price_per_1k` | NUMERIC(10,6) | NOT NULL | Input token price per 1K tokens (USD) |
| `output_price_per_1k` | NUMERIC(10,6) | NOT NULL | Output token price per 1K tokens (USD) |
| `updated_at` | TIMESTAMP WITH TZ | DEFAULT NOW(), ON UPDATE NOW() | Last price update |

**Indexes:**
- Primary key on `model_id`

---

## Tenant Isolation

All queries against tenant-scoped tables MUST include the `org_id` filter. The `TenantMixin` class adds the `org_id` column to all tenant-aware models:

```python
class TenantMixin:
    org_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
```

**Tenant-scoped tables:**
- departments
- teams
- users
- service_accounts
- tokens
- budget_configs
- budget_usage
- usage_logs
- rate_limit_configs
- model_aliases

**Global tables (no tenant isolation):**
- organizations
- bedrock_pool_accounts
- model_pricing

## Migrations

Database migrations are managed with Alembic. Migration files are in `alembic/versions/`.

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback one migration
alembic downgrade -1
```
