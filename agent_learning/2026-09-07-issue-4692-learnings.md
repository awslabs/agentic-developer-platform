# Learnings — issue #4692 (architect spike: per-principal Bedrock account routing)

**Deliverable:** `docs/design-notes/4692-per-principal-bedrock-account-routing.md` (PR #4693, docs-only)
**Persona:** `agent-architect` — design note, no implementation

---

## 1. "Verify the current shape, don't design against an assumed one" changed every answer

The operator's instruction was to ground the mechanics in the existing linked-account
machinery and **cite it** rather than assume its shape. That instruction is what
produced the note's value. Five of the issue's premises turned out to be wrong, and
every one of them was only visible by reading code:

| Issue premise | Reality |
|---|---|
| Build a mapping table with a user→team→org ladder | `CredentialResolver` already is that ladder, and `aws_role` creds already live in `user_credentials` |
| Reuse "the existing linked-account/assume machinery" (singular) | Two different things — one live, one dead code |
| Validate mappings against org-linked accounts | The obvious validator (`organizations.aws_accounts`) is unusable: shape collision |
| The resolved account may lack the model | The resolved account lacks **`bedrock:InvokeModel` entirely** — `ReadOnlyAccess` only |
| Routing is the mechanism for choosing whose account pays | `ADP_BEDROCK_VIA=user` already does this, unmetered |

**Generalizable:** for a spike whose issue body already proposes a design, budget most
of the time for *falsifying the proposal* rather than elaborating it. The issue author
wrote the design from memory of the architecture; the architecture had moved. A spike
that only elaborates is worth much less than one that finds the two blockers.

## 2. Reusing an existing ladder beat building the proposed sibling table

The single highest-leverage finding: the issue asked for a new table, and the answer was
"the table exists, add a label." That converted a multi-migration feature into a
**zero-migration** one with flag-flip rollback, and — more importantly — avoided a second
source of truth that could disagree with the first about the same question.

**Heuristic:** when an issue proposes a new config surface with a scope hierarchy, grep for
existing scope-walk code before accepting the premise. In this repo `_SCOPE_ORDER` was the
tell.

## 3. Two ladders in one repo had opposite semantics — mirroring the wrong one is a silent bug

- `CredentialResolver`: **first-match** — user rung wins outright, wider rungs never consulted.
- Budget hierarchy (`_check_entity_budget`): **conjunction of ceilings** — every rung
  evaluated independently, missing row = allow.

Both look like "user > team > org" in prose. My first draft cited the budget hierarchy as
the precedent for a first-match ladder, which would have been wrong. Caught it on
self-review.

**Generalizable:** "hierarchy" in prose hides at least two incompatible semantics
(precedence vs. conjunction). When citing a precedent for a ladder, state *which* it is
and read the evaluation loop, not the schema.

## 4. Structural vs. test-only prevention is the standard worth internalizing

The issue's Validation section demanded that the wrong-account and fail-open blast-radius
rows each have a **named structural prevention, not a test-only one**. This forced better
design rather than better test lists:

- wrong-account → shadow mode + cache key widened to the full identity tuple + an `org_id`
  query predicate (the query *cannot* return another org's row)
- fail-open → fail-closed, with **no fallback code path existing to reach**

That last phrasing is the test: if the prevention is "we have a test asserting we don't do
X," it's test-only. If it's "there is no code path that does X," it's structural.

## 5. Proving a non-interaction is a different exercise than proving a behavior

Question 3 — "the budget arc's invariants must be provably untouched" — was answered not
by describing what routing does to metering, but by showing routing and settlement
**share no state**. The strongest single line: the cost-backfill Lambda reads Cost Explorer
and *cannot see the routing decision even in principle*.

Counter-intuitively, discovering that there are **three** cost-bearing paths (not one)
*strengthened* the proof rather than weakening it. My draft had over-simplified to "one
metering path"; the correction made the argument better. Worth remembering: when a
correction seems to threaten your thesis, re-derive before retreating — it may support it.

**Also surfaced by this exercise:** the load-bearing rule that routing must key off
`org_id` (authenticated) and never `attributed_org_id` (caller-influenced). Two fields
that look interchangeable at a call site, where one is an authorization field and the
other is deliberately caller-influenced. The codebase had an explicit "Never
`context.org_id`" comment for the opposite direction — those comments are load-bearing
and reading them saved a security bug.

## 6. An interface with no parameters is the real cost signal

`IPoolService.get_client()` takes no arguments. That single fact is what makes this feature
a seam change across 8 call sites rather than a config addition — and it's a two-line read.

**Heuristic for scoping spikes:** find the function that would need to know the new thing,
and check whether it *can* receive it. A no-arg signature on the hot path is a reliable
proxy for "this is bigger than the issue implies."

Adjacent trap found the same way: the dead cross-account path builds its boto3 client with
**no `Config`**, so naively reviving it would silently regress tuned timeouts
(`read_timeout=3600` invoke / `300` streaming). Dead code is not a free template — it
predates the tuning that the live path has.

## 7. Distinguishing "live" from "dead" code with the same name mattered

`src/pool/` contains both `SimplePoolService` (live) and `PoolService` (dead). Both are
"the pool." The dead one *is* cross-account, so it reads as exactly what this feature
needs — but it answers "which account has headroom," not "which account should pay."
Recommendation was to harvest its STS cache and discard its selector.

The confirming detail: its IAM scopes to a `*BedrockGateway-Pool*` role-name convention
that **no connected account matches**. Checking whether a code path's IAM can actually
match live resources is a cheap and decisive liveness test.

**Generalizable:** `app.py` wiring (what's actually constructed at startup) is the
ground truth for liveness, not file existence or class names.

## 8. Escalate blockers to the top of the note, not into the relevant section

Both blockers were found while answering *other* questions — the `ReadOnlyAccess` finding
came out of question 5 (model access), and the shape collision out of question 4 (tenant
isolation). Left in place, each would have read as a footnote in a section an operator
might skim. Promoting them into the executive summary and the verdict line
("design-complete **with two prerequisite blockers**") is what makes them actionable.

**Generalizable:** a spike's verdict line should state what is *not* ready. A note that
says "design complete" when two prerequisites are unmet is misleading even if the details
are all present somewhere.

## 9. Process notes

- Reading a sibling design note (`docs/design-notes/4620-cross-org-person-budgets.md`) as
  a format template before writing was worth it — matched section conventions and the
  verdict-line style without inventing a format.
- Explorer subagents surfaced two findings I'd have missed (the three metering paths, the
  worker bypass), but **both needed independent verification** — I re-grepped `cost_usd`
  call sites and read the CFN template myself before citing. Subagent findings are leads,
  not citations.
- Self-review caught four accuracy errors in my own draft (ladder semantics, metering-path
  count, existence-gate precedent, attribution field). All four were cases of citing a
  precedent from memory of the summary rather than re-reading it. Cite from the file open
  in front of you.
- "Propose child issues; do not file them" — followed exactly. Also: no `@agent-` mention
  and no `agent-*` label on the summary comment, since either would re-trigger a persona.
- Docs-only diff → the per-module lint/test matrix is N/A. Said so explicitly in the PR
  body rather than silently skipping it, so a reviewer doesn't have to wonder.
