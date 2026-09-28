# Live API contract capture — wave 3 ground truth (adp-dev-embark1)

Captured by ops during ORCH #4415, BEFORE dispatching #4402, so the frontend
is written against the real response rather than mocks (the #3675 guard, and
eval #4412 checks 12-14 which compare the FE type to the LIVE key set).

Endpoint: GET /api/me/budget?period_type=monthly (member token)

Top-level keys:

```
band,binding,cap_status,cap_usd,combined_informational,enforcement_mode,entity_type,identity_status,lines,period,remaining_usd,spend_usd,utilization_pct
```

Line-object keys (union across lines):

```
band,cap_status,cap_usd,enforcement_mode,entity_type,label,principal_kind,remaining_usd,source,spend_usd,utilization_pct
```

Full body:

```json
{
  "period": {
    "period_type": "monthly",
    "period_start": "2026-08-01",
    "period_end": "2026-08-31",
    "resets_in_days": 1
  },
  "entity_type": "user",
  "cap_usd": null,
  "spend_usd": "0.000000",
  "remaining_usd": null,
  "utilization_pct": null,
  "band": null,
  "cap_status": "uncapped",
  "enforcement_mode": null,
  "identity_status": "unresolved",
  "binding": null,
  "lines": [
    {
      "entity_type": "user",
      "label": "Direct usage (my machine)",
      "source": "direct",
      "principal_kind": "human",
      "cap_usd": null,
      "spend_usd": "0.000000",
      "remaining_usd": null,
      "utilization_pct": null,
      "band": null,
      "cap_status": "uncapped",
      "enforcement_mode": null
    }
  ],
  "combined_informational": null
}
```

Endpoint: GET /api/me/budget/runs?period_type=monthly (member token)

Top-level keys:

```
identity_status,items,next_cursor,period,subtotal,total_run_count
```

Full body:

```json
{
  "items": [],
  "subtotal": {
    "status": "unknown",
    "amount_usd": null,
    "reason": "lineage_unavailable",
    "scope": "agent run costs only; excludes build/infra",
    "partial": true
  },
  "total_run_count": 0,
  "next_cursor": null,
  "period": {
    "period_type": "monthly",
    "period_start": "2026-08-01",
    "period_end": "2026-08-31",
    "resets_in_days": 1
  },
  "identity_status": "unresolved"
}
```

## Findings

- **`freshness` is ABSENT** from the envelope, though eval #4412 check 12 requires
  it, `api-contract.md` specifies it as `{"cost_backfill_lag": bool}`, NFR-5
  mandates the affordance, and #4402 declares it on `BudgetEnvelopeResponse`.
  Filed as defect **#4477** (backend-owned; neither #4397 nor #4399 ever mentioned
  the field, so it fell between units). This is the ONLY missing envelope key.
- **Eval check 13 (line objects) passes as-is** — all ten required line keys are
  present live. No defect needed there.
- **Dev degradation values are BY DESIGN and must be rendered, not assumed away**
  (wave-2 handoff carry-forward 2, confirmed live here):
  - `identity_status: "unresolved"` on both endpoints
  - `cap_status: "uncapped"`, so `cap_usd`, `remaining_usd`, `utilization_pct`,
    `band`, `enforcement_mode` are all `null`
  - `binding: null` and `combined_informational: null` — an uncapped line cannot
    bind, and a "combined" total over one line is that line restated
  - `lines` has exactly ONE entry in dev (direct only), not the two-line shape the
    contract example shows
  - runs: `subtotal.status: "unknown"`, `amount_usd: null`,
    `reason: "lineage_unavailable"`, `partial: true`, `items: []`
  Any UI or eval assertion expecting a capped two-line response will FALSE-FAIL in
  dev. Money stays a STRING (`spend_usd: "0.000000"`), never a float.
- **Fixture provenance (eval check 14):** fixtures must be derived from
  `modules/gateway/src/budget/schemas.py` or from THIS captured response — never
  from the frontend TypeScript type. Fixture keys must be a SUBSET of the keys
  above.

## Post-merge capture — after #4477 and #4401 shipped (2026-08-30)

Re-read against the same target after both backend merges deployed, so U-5 is
built against observed keys rather than a TypeScript type (#3675 guard).

### `GET /api/me/budget?period_type=monthly` — member token, HTTP 200

Top-level keys now **fourteen** (was thirteen); the added key is `freshness`:

```
band, binding, cap_status, cap_usd, combined_informational, enforcement_mode,
entity_type, freshness, identity_status, lines, period, remaining_usd,
spend_usd, utilization_pct
```

`freshness` is an OBJECT, not a boolean: `{"cost_backfill_lag": false}`. It is
required and non-nullable in the schema, so U-5 may read
`freshness.cost_backfill_lag` without a presence guard — but must NOT treat
`freshness` itself as the flag.

The dev degradation values are unchanged and still by design (wave-2
carry-forward): `identity_status: unresolved`, `binding: null`,
`combined_informational: null`, one entry in `lines`. U-5 must RENDER these,
not assume them away.

### `GET /api/budget/scope/{entity_type}/{entity_id}?period_type=monthly` — new

Observed live, not inferred:

| caller | target | status | body |
|--------|--------|--------|------|
| none | admin's own sub | `401` | from `get_current_user` |
| member | admin's sub | `403` | exactly `{"detail":"Not authorized to read budget data for the requested scope."}` |
| admin | admin's own sub | `200` | envelope below |
| admin | `run/abc` | `422` | entity_type outside the allow-list |

200 envelope top-level keys — note these are a DIFFERENT set from `/me/budget`;
this response has `line` (singular object) and `rollup`, and has NO `freshness`,
NO `lines`, NO `identity_status`:

```
binding, entity_id, entity_type, line, period, rollup
```

`line` object keys:

```
band, cap_status, cap_usd, enforcement_mode, entity_type, label,
principal_kind, remaining_usd, source, spend_usd, utilization_pct
```

For the dev admin caller: `binding: null` (uncapped — an uncapped line cannot
bind), `rollup: []` (an individual target has no members). A U-5 assertion that
requires a non-empty `rollup` or a non-null `binding` for a user target will
false-fail in dev.
