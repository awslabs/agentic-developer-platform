# Evaluation Issue Template

> Per-wave deterministic evaluation. Operations persona runs checks after wave
> deploy — never implements anything. Every check MUST be mechanical (exit codes,
> HTTP status, resource existence, query output). NO judgment calls.
> Reference: #3334, #3335, #3336 (Phase 0 GitLab CE evaluations).

---

```markdown
> Part of the delivery loop ([PHASE_EPIC_REF]). Run AFTER [WAVE_LABEL]
> stories are merged AND deployed. Operations persona runs the checks;
> do not implement anything here.

## Description
Deterministic post-deploy evaluation for [WAVE_LABEL] ([story refs]).
Proves [what this wave delivers is live and correct] before dependent
waves start.

## Checks (all must pass — no judgment calls)
1. [COMMAND or API call] returns [EXPECTED_OUTPUT].
   Source: #[STORY]'s Validation, check [N].
2. [COMMAND or API call] returns [EXPECTED_OUTPUT].
   Source: #[STORY]'s Validation, check [N].
3. [Cumulative constraint assertion — e.g. zero diff on path X].
   Source: intent acceptance criterion [N].

## Defect protocol
If any check fails:
1. File a NEW five-section defect issue (use the defect template) quoting:
   - The failing check's command + actual output
   - The owning story reference
2. Attach the defect as a native sub-issue of [PHASE_EPIC_REF].
3. Dispatch the developer persona on the DEFECT issue (single mention, on
   the defect issue only — never re-mention merged stories).
4. Re-run this evaluation after the defect PR merges + deploys.
5. This issue closes ONLY when ALL checks pass in a single run.
6. Post the passing transcript as a comment before closing.
```

---

## Emitter instructions

When generating an evaluation issue from a delivery plan:

1. **One evaluation per wave** — never combine waves.
2. **Title format**: `[Phase-slug] Wave [N] Evaluation — [what it proves]`
3. **Label**: `evaluation` (do not add `epic` or `story`).
4. **Checks derivation**:
   - Pull from each wave-story's `## Validation` section (smoke tests, CI checks).
   - Pull from the intent's acceptance criteria (cross-cutting invariants).
   - Convert every check to a **concrete command + expected output** pair.
5. **Deterministic-only rule — REJECT if any check contains**:
   - "Verify it works"
   - "Ensure the feature is functional"
   - "Confirm correct behavior"
   - "Test the integration" (without a named test command)
   - Any phrasing that requires human judgment
6. **Defect protocol is MANDATORY** — every eval must include the defect section.
7. **Size limit**: body <= 8KB (same as story issues).
8. **Create BEFORE the orchestrator** — the orchestrator references the eval's
   issue number.
9. **Live API-contract check (Rule 5 — cross-boundary waves):**
   When the wave's stories span the frontend/backend boundary (a frontend
   component declares a TypeScript response type for a backend endpoint), the
   evaluation MUST include a contract check that:
   - Calls the REAL deployed endpoint (authed, via `adp-cred` + gateway token).
   - Extracts the response key set via `jq`.
   - Asserts every field in the frontend TS interface exists in the live
     response (field list enumerated at emission time from the TS interface).
   - Fails the wave on any missing/renamed key.

   This prevents the "closed-loop self-consistent wrongness" failure mode
   (#3675) where frontend types invent fields the backend never sends, and
   story-level tests + MSW mocks + the eval all validate the invented shape.

   **Check format in the Checks section:**
   ```
   N. Live API-contract: `curl -s -H "Authorization: Bearer $TOKEN" \
      https://<domain>/api/<path> | jq '<extract-expr>'` contains keys \
      [<field1>, <field2>, ...].
      Source: frontend type `<InterfaceName>` in `<file-path>`.
      Expected fields: <comma-separated list from the TS interface>.
      Rule: every field in the frontend type MUST exist in the live response.
   ```

   **Fixture-provenance sub-check** (if the wave adds MSW mocks/test fixtures):
   ```
   N+1. Fixture provenance: fixture keys ⊆ live response keys.
      Source: fixture derived from backend schema `<schema-file>`.
      Rule: no invented fields — fixture keys must be a subset of live keys.
   ```
