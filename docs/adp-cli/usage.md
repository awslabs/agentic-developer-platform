# Usage and inference log metadata

`adp usage` reads the existing UsageLog ledger through bounded own/managed server adapters. `adp logs` reads the same redacted inference request metadata, not unrestricted gateway debug logs, prompts, responses, credentials or live agent transcripts. For live explanations use `adp agent logs --follow`; task submission remains `adp task`.

```bash
adp usage summary --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp usage timeline --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp usage models --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp usage requests --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --max-pages 5 --json
adp usage request REQUEST_ID --run INVOCATION_ID --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp admin usage summary --org ORG_ID --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp admin usage users --org ORG_ID --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp admin usage departments --org ORG_ID --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp admin usage requests --org ORG_ID --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp logs show --request-id REQUEST_ID --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp logs list --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --json
adp logs export --start 2026-09-25T00:00:00Z --end 2026-09-26T00:00:00Z --format csv > usage.csv 2> continuation.json
```

Every command uses the selected deployment and existing login. Own reads derive the current workspace identity server-side and include its canonical ID and authenticated login subject. Without `--run`, `scope.coverage=direct_identity_records` explicitly excludes unattributed service-principal hosted calls. For hosted work pass its Activity invocation ID: the server checks canonical owner/root-human and tenant through Activity before reading that run's usage. It never treats policy preference ownership or caller-supplied attribution as authority. Managed reads require an authoritative administrator role and permission; department admins are restricted by a SQL department predicate. No all-tenant data is filtered in the CLI.

Dates must include a timezone and are normalized to UTC. Start is inclusive; end is exclusive. Windows are limited to 90 days; timeline buckets are UTC dates. Request pages sort descending by `(timestamp,id)` and contain at most 100 records. `--max-pages` is 1–100 (default 1), so an export returns at most 10,000 records. Aggregates reject windows exceeding 10,000 records and ask for a narrower range. Pages and aggregate responses report `observed_at`, `retention_days`, `raw_retention_start` and `window_coverage` (`within_retention`, `overlaps_retention`, `before_retention`). These compare requested dates to the configured raw-log horizon at observation time; they never prove that any record was purged or expired. Empty current and older windows remain unknown spend with distinct coverage metadata.

Continue using `detail.next_cursor` with exactly the same scope, dates, request and run filters. Cursor binding rejects changed filters. Exports that reach the page bound return pending/exit 4, retaining continuation metadata; they never claim a complete export. Ordinary paginated reads may return exit 0 with `complete=false`, meaning that the page read succeeded and continuation remains. Keyset pagination is stable for existing rows, but it is not a database snapshot: late records and settlement updates can change the window. Rerun a completed window after settlement when reconciling accounting.

JSON exports are one common envelope. NDJSON contains `type=record` entries followed by one `type=continuation` entry with selected scope/window/completeness. CSV writes only a header and documented record columns to stdout, and writes continuation JSON to stderr. Formula-like cells are prefixed with an apostrophe. CSV fields are `id,timestamp,request_id,org_id,user_id,model,input_tokens,output_tokens,status_code,invocation_id,chain_id,root_human_id,cost_status,cost_amount,currency,settlement`. Prompt/response selection and export are intentionally unavailable on this metadata surface.

Costs remain decimal strings in USD. A pricing decision captured as verified/estimated supports an **estimated** figure; it does not prove a settled budget debit. Legacy costs, including placeholder zeros without pricing provenance, have `status=unknown`, `amount=null`, and a separately labelled `recorded_amount`. A partial aggregate reports a `lower_bound` of recorded estimated figures with an unknown-record count; this is not a guaranteed lower bound on final billed cost. Empty data is unknown spend, never automatically zero. These totals must not be added to flow totals or budget totals covering the same calls.

The adapter exposes recorded billed user/tenant/model and available invocation/chain/model-decision references. `root_human_id` comes only from an authorized Activity lookup, not inferred policy ownership. Missing attribution/debit links remain null with `linkage_status=incomplete`; settlement remains unknown. Use the existing budget own/managed read models to corroborate settled totals separately. Missing/inaccessible/expired/delayed request lookups have the same unavailable result, without an existence oracle. Failed transport/database reads fail rather than returning empty success.

The source adds `/usage/me/{summary,timeline,models,requests}` and `/usage/managed/{org_id}/{summary,users,departments,requests}` because legacy admin-only `/usage/*` endpoints serialize some costs as floats and cannot supply own scope or safe cursor pagination. Existing usage, budget and admin-log behavior is unchanged.

#5628 remains open for live acceptance. Source tests do not establish deployed adapter availability, installed-CLI ordinary/admin isolation, marked real inference charge linkage, or resumed multi-entity export acceptance. Missing debit linkage and settled request-level proof remain explicit limitations; no claim of complete accounting qualification follows from merging this implementation.

The existing remote dispatcher registers `usage_readback` as a diagnostic, outside the full acceptance matrix. Supply the existing installed `cli_path` and session plus `usage_readback={run_id,request_id,start,end}` for an explicitly owned fixture. It runs own summary, request page, lookup and bounded JSON export (one continuation when present), with no inference, remote controls or budget changes. It records scope/linkage/completeness consistency and preserves the full live acceptance hold.
