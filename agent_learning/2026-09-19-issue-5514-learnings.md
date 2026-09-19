# Learnings — issue #5514 / #5144 (durable continuation: worker handoffs and legacy adoption)

**Deliverable:** PR #5514 on `agent/issue-5144` — continuation of an ended developer run
(326131ee). Four independent-reviewer blockers (F1–F4) plus the issue's two remaining
requirements.
**Persona:** `agent-developer` — implementation

---

## 1. A guard that fixes a defect can create a worse one in the opposite direction

F1 was the reviewer's first blocker: a terminal worker report released the work claim
that its own committed continuation receipt was attributed to. Worth spelling out *why*
that is worse than the bug #5144 closes.

Receipt attribution is not "a receipt exists". `handoff.receipt_for` credits a stored
ref only if it equals `handoff_receipt_ref(identity, record.id)`, and that reference
embeds both `claim_id` **and** `claim_generation`. So releasing the claim does not tidy
anything up — it makes a *correctly committed* continuation permanently
unattributable, and the story is then held on evidence sitting in its own row that
nothing can credit to any attempt.

The original defect punished a worker that exited early. This would have punished a
worker that did everything right.

**Generalizable:** when a fix adds a release/cleanup step, ask what durable evidence was
keyed to the thing being released. If the key includes a generation or version, the
cleanup is a deletion of attribution, not of state.

## 2. Guard both release paths, or the guard is defeated by the original defect's own shape

The obvious place for F1's refusal is the reporting path (`maintain_worker_claim`). But a
worker that hands off correctly and then exits *without* reporting is precisely what
`recover_exited_claims` exists for — and that is where the stranding actually happens in
production. A guard on only the reporting path is defeated by the exact failure mode
#5144 was filed about.

Two different mechanics, though, and the difference mattered:

- reporting path → **raise** (`WorkClaimError("continuation_outstanding")`), which
  `/status` maps to 409 so the worker learns its exit did not end the lane;
- recovery sweep → **skip**, because the sweep is bounded and covers many tenants'
  claims. A raise would abandon every claim after this one.

The test that proves the skip is right seeds a *second, later* claim with no
continuation: a guard that merely stopped the loop early passes without it.

## 3. A lock added for correctness needs a test that fails when the lock is removed

F2 required revalidating receipt authority at result-commit time under a consistent lock
order. My first set of PostgreSQL tests passed *with `for_update=lock` deleted* — they
asserted the outcome, which the unlocked read already produced in an uncontended
transaction. They were worthless and looked fine.

The replacement is genuinely load-bearing: an `asyncio.Event` gates a concurrent
handover so it can only commit *while* the verdict transaction holds the lock, and an
`order` list records which finished first. Remove the lock and the ordering inverts.

**Generalizable:** for a locking change, the test must contain a second concurrent
transaction and an assertion about *ordering or blocking* — not about the final value.
If deleting the lock keeps the test green, the test is asserting something else.

## 4. "Exported and tested" is not "integrated" — and helper coverage hides it

F4, and the same finding as #5224's first lesson, which is why it is worth recording
twice: `adopt_legacy_lane` and `outstanding_block` were fully implemented, fully tested
and had **zero production callers**. `grep` for the symbol found only `__all__` entries,
docstring mentions and tests. The suite was green because it called the helpers
directly.

The fix for finding the right integration site was not architectural taste — it was
reading the issue body, which named it verbatim: "modify ... results.py and controls.py
only for the shared handoff/adoption integration". I had spent a long survey over five
candidate sites (`execution_runner.verify_live_authority`, `dispatch_pass._dispatch_one`,
`agentauth/work_routes.py`, `engine_commands.py`, `tick_handler.py`) before reading the
requirement that decided it in one line.

**Generalizable:** when an integration point is ambiguous, re-read the issue's own
"Files and integration" section *before* surveying the code. The constraints section also
ruled out my instinct to add a route ("No public mutation endpoint or worker database
grant", "No additional database schema or HTTP endpoint").

## 5. Some evidence has no server-side source, and that decides where the code lives

`force_handover` requires `effects_reconciled` and `credentials_reconciled`. A grep
showed both appear *only* in `work_claims.py` and `handoff.py` — there is no production
source of reconciliation evidence anywhere in the platform, and there cannot be one: a
database fence cannot revoke a GitHub installation token that has already been issued.

That single fact settled the design. An engine-initiated adoption from a scheduled pass
would have to invent the attestation or default it `True`, and defaulting it `True`
makes `force_handover`'s guards 3 and 4 unreachable. The only honest source is a human,
so adoption has to hang off a human control — which is also why `resume_node` was the
right host rather than merely a convenient one.

`_RESUMABLE_STATES` already contained `AWAITING_MERGE`, the exact state a #5144
missing-receipt hold leaves a story in. That was the confirmation, not the reason.

**Generalizable:** before choosing where a guarded operation lives, check whether its
required evidence has any producer. An unsourceable parameter is a statement about which
actor may invoke the operation.

## 6. Mutation testing, seven times, on the integration itself

The new route tests all passed on the first run, which is exactly when to distrust them.
Seven mutations of the F4 integration, all caught:

| Mutation | Tests failed |
|---|---|
| Integration removed entirely (the F4 blocker state) | 8 |
| Refusal resumes the node anyway | 6 |
| `"issue": "5144"` block shape drifts | 4 |
| Attestation defaulted `True` | 1 |
| `org_id` filter dropped from lane lookup | 1 |
| `DIRECT_DISPATCH` filter dropped | 1 |
| Unreadable-policy refusal ignored | 1 |

The four single-failure mutations are the interesting ones: each is covered by exactly
one test, so each of those tests is the only thing standing between a real guard and a
silent regression. Worth knowing which tests those are.

## 7. Refusal is not absence — and the same distinction recurs per call site

F3's rule (`AdmissionInputs.refusal` vs `policy is None`) had already been applied in
`dispatch_pass`. It had to be applied *again*, independently, in the new adoption path:
an unreadable policy must block adoption rather than fall through to a legacy-style
transfer. The type makes the distinction *available*; it does not make it happen. Every
new consumer of `load_in_force_policy` is a fresh opportunity to collapse the two.

## 8. Operational notes that cost real time

- **Skipped PostgreSQL cases are not a pass.** `pgserver` ships wheels for Python ≤3.12
  only, so the default 3.13 venv silently skips them. Ran the 20 PG cases on a second
  `.venv312` and verified each printed `PASSED`, not `SKIPPED`.
- **`ruff format` line length differs per module**: gateway `pyproject.toml` is 150,
  agent-factory is 100. `ruff check` passed while `ruff format --check` wanted a
  reformat; run both.
- **`git rev-parse origin/agent/issue-5144` fails in a detached worktree** (no
  remote-tracking ref). Use `git ls-remote origin refs/heads/...` to confirm the remote
  still descends from the expected head before a fast-forward push.
- **Bash cwd resets between tool calls**, so every command must `cd` itself.
- **Piping pytest through `grep` buffers everything** until the run completes; redirect
  to a file when the run is minutes long.
- SQLAlchemy savepoint rollback **expires loaded attributes**, so re-reading one inside
  an exception handler issues a lazy SELECT on an unwinding session.
- `OrchestrationDecision` is append-only at the ORM `before_update` flush boundary, so a
  test cannot mutate a seeded decision row — append a new one.

## 9. Scope discipline

The issue's coding guidelines say: "if a file or line in your diff doesn't trace to an
acceptance criterion in the issue, delete it before opening the PR." The final diff
touches six files across four commits, each tracing to a named blocker or requirement.
No new endpoint, schema, permission or database grant was added; adoption remains
disabled by default behind `ADP_ENGINE_ADOPTION_ENABLED`, read per call.

Deployment, adoption activation, Terraform/IAM apply, self-approval and merge were all
out of scope and were not performed.
