# Learnings: Issue #1736 — Knowledge Asset Registry UX + API Refinement

**Date:** 2026-06-23
**Agent:** @agent-architect
**Issue:** #1736 (Child EPIC #1345) — Knowledge Asset Management
**Task:** Incorporate operator's UX direction (NotebookLM model) + finalize API surface

## Key Decisions Made

1. **New page, not IndexingStatus extension** — IndexingStatus is admin-only (`isPlatformAdmin()` gate in `Navigation.tsx`), read-only, and run-centric. The Knowledge Assets page needs to be user-facing and write-capable. These are fundamentally different surfaces with different auth models.

2. **Agent-context APIRouter with `get_current_user` (not `require_admin`)** — The existing `indexing_router.py` uses `require_admin` but this page is user-facing. Solution: mount the assets router separately in `app.py` with `dependencies=[Depends(get_current_user)]`. Both routers still share the same `AGENT_CONTEXT_ENABLED` gate and DI override pattern.

3. **Two-step bulk upload (preview + commit) is v1** — Not a fast-follow. The operator explicitly specified this as the safe pattern. This means two separate endpoints, not one.

## Technical Patterns Discovered

### Frontend Architecture
- **React 19 + React Router 7 + Tailwind CSS** — no MUI/Chakra.
- **Component library** in `components/ui/` (Modal, Card, Badge, Button, Table, Tabs, Toast, Select, Input, Spinner, Dropdown).
- **Modal pattern**: portal-rendered, focus-trap, escape-to-close, sizes (sm/md/lg/xl). `ModalFooter` for button alignment.
- **Navigation**: `NavItem[]` array built conditionally based on `usePermissions()` hook. Items are pushed in order.
- **Routing**: lazy-loaded pages in `App.tsx`. All protected routes nested under `ProtectedRoute > OnboardingGuard > MainLayout`.
- **Service layer**: `services/admin.ts` pattern — fetch from gateway, snake_case → camelCase transform, consistent pagination (`items`, `total`, `page`, `pageSize`, `hasMore`).

### Backend Architecture
- **`TokenContext`** (from `src/shared/schemas/auth.py`): fields are `user_id` (cognito sub), `org_id` (tenant), `team_id`, `department_id`, `account_type`, `is_admin`, `expires_at`, `auth_source`.
- **`org_id` in TokenContext = `tenant_id` in the registry** — the gateway uses `org_id` throughout but the knowledge layer uses `tenant_id` (aligned with #1721). The mapping happens at the API layer.
- **`user_id` in TokenContext = `owner_sub` in the registry** — same value, different column name. Both are Cognito `sub` strings (region-prefixed, like `us-east-1:abc-123`), VARCHAR(128).
- **DI override pattern**: `app.dependency_overrides[get_indexing_db] = get_db` — the gateway injects its own session factory into the agent-context router's dependency placeholder.

## Gotchas

1. **`owner_sub` must be VARCHAR(128), NOT UUID** — Cognito subs are region-prefixed strings like `us-east-1:abc-123`, not UUIDs. The #1721 design note mentions `UUID` in some places but the actual code uses `VARCHAR(128)`. Always check the gateway `TokenContext` definition for the ground truth.

2. **Admin gate is at mount-time, not endpoint-time** for the indexing router — `app.include_router(indexing_router, dependencies=[Depends(require_admin)])`. For the assets router, we need per-endpoint guards (some endpoints are user-facing, some are admin-only like bulk upload). Solution: mount with `get_current_user`, then check `is_admin` inside specific endpoints.

3. **The `AGENT_CONTEXT_ENABLED` gate uses `.lower() == "true"` check** — must be exactly `true` (case-insensitive). Not `1`, not `yes`.

4. **IndexingStatus uses 6 canonical stages** but references 8 in `STAGE_LABELS` — `sbom_image` and `graphrag` exist as labels but aren't in the canonical list. The assets status chips should reference both the canonical stages AND any additional stages that appear in the data.

## What Worked Well

- Reading the actual `TokenContext` schema and `Navigation.tsx` before proposing auth patterns — avoided proposing patterns that don't match the live code.
- Checking the existing `IndexingStatus.tsx` `statusColor()` function — allows direct reuse of the chip styling pattern in the new components.
- Looking at `BudgetFormModal.tsx` as the closest existing CRUD-form modal — provides the exact pattern for AddAssetDialog.

## Recommendations for Implementation

1. **Start with Issue A (migration) immediately** — it has no blockers if #1721's migration 004 has landed.
2. **Mock the API in frontend work** — Issue E (UI) can proceed in parallel with Issue B (CRUD API) using mock data that matches the contracts in §8.7.
3. **The stub for `dispatch_to_trigger()`** should be trivial: `UPDATE knowledge_assets SET status = 'queued' WHERE id = :id`. This unblocks the entire registry + UI from shipping before #1672.
4. **Zone 3 (project context)** should be built as a collapsible panel with a feature flag — when #1728's API exists, flip the flag. Don't skip building the layout structure.
