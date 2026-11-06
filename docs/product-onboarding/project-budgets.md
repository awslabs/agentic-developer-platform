# Project budgets and base Projects

Every Product has a base Project. Product work uses it automatically, with an
optional Project selection for separately funded work. Existing Products retain
their earliest active Project and its configuration as the base; Products without
one receive a new Base Project. The base Project cannot be unlinked.

In the Product workspace, select a Project and open **Budget & spend**. The Product
owner or a verified platform administrator can set its total AI budget. Current
Product team members can read spend. The panel shows the budget, settled spend,
reserved usage and remaining amount, plus a breakdown by delivery flow. Its
creation links carry the selected Product and Project into a new flow or agent
conversation.

Flow planning accepts `product_id` and optional `project_id`. The same Project is
captured on the resulting flow and inherited by its agents. Human Task submission
accepts the same fields; omitting `project_id` selects the Product base. The CLI
supports `adp flow start --product PRODUCT_ID --project PROJECT_ID` and
`adp task submit request.json --human-login --key REQUEST_KEY --product PRODUCT_ID`.
A Project selected outside its Product or tenant is refused. Session and Task
retries retain their captured scope; a started flow cannot change Project.
Unscoped personal work remains personal; the platform does not guess a Product
from a repository, account or browser workspace.

The API exposes:

- `GET /v1/projects/{project_id}/budget`: current balance and per-flow spend.
- `PUT /v1/projects/{project_id}/budget`, body `{"budget_usd":"3000.00"}`:
  set the total allowance, including usage already incurred.
- `GET /v1/products/{product_id}/spend`: aggregate usage captured for that Product.
- `PUT /chat/sessions/{session_id}/project`: select the Product and optional
  Project for an owned session before its first message.

Budgets share the Gateway's reservation and settlement services. Concurrent flows
and agents compete for one Project allowance. Known provider ceilings remain
reserved across interruptions until a trusted receipt or proven unsent outcome
settles them. A confirmed settlement is idempotent. A missing accounting store
never resets the allowance to zero. Existing organization, person, run and flow
limits continue to apply. Turning off enforcement retains accounting obligations.

The SQL usage ledger is the durable history. Project reservation accumulators and
settlement proofs must be retained in the configured Redis store; loss is reported
as unavailable accounting rather than a fresh balance. Ordinary code rollback
retains the additive SQL schema and financial attribution. No historical usage is
assigned to a Project merely because a migration ran.

This budget covers metered AI usage. Financial planning allocations and cloud
infrastructure costs remain separate. Initiative and Portfolio reporting follow
the existing [Engineering Insights lineage](../engineering-insights/lineage-schema.md#511-product-execution-projects-and-budget-attribution).

Verification uses real PostgreSQL migrations and concurrent transactions,
Redis Lua execution, API membership checks, and frontend tests:

```bash
cd modules/gateway
pytest tests/products/test_project_budgets.py tests/migrations/test_146_project_budgets.py
npm --prefix frontend test -- --run src/__tests__/pages/ProductBudget.test.tsx src/__tests__/pages/StartFlow.test.tsx
```
