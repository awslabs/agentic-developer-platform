# Learnings — issue #5224 (bounded, explicitly accepted coordinator authority)

**Deliverable:** PR #5234 on `agent/issue-5224` — coordination vocabulary in the accepted
policy, coordinator/child dispatch through the existing protected route, owner-facing
scope summary.
**Persona:** `agent-developer` — implementation

---

## 1. A branch with no production caller is not a feature, and the tests will not tell you

The largest single finding. Prior segments of this work had landed
`authorize_coordinator_child_request`, `CoordinationScope`, `authorize_child_request`
and a full `runtime_action` resolution — all tested, all passing — while
`graph_dispatch` still opened with a blanket `if coordinates: raise PolicyError(409,
"unsupported autonomous coordinator capability")`. Every coordination check in the
codebase was unreachable from any route. The suite was green because it exercised the
exported helpers directly.

The tell was not a failing test. It was reading the production caller and finding the
refusal above the code that would have used the result.

**Generalizable:** for any authorization change, write at least one test that goes
through the *route*, and grep the caller for an early refusal before believing helper
coverage. Restoring the blanket refusal afterwards made 13 of 18 new route tests fail —
which is the number that tells you the tests are load-bearing. Helper-only coverage
would have reported zero.

## 2. Mutation testing found two defects that 118 passing tests did not

Used twice, both times productively:

- **Removing the new `NON_DELEGABLE_CHILD_ACTIONS` guard left the whole suite green.**
  The guard was the fix for a docstring claiming an invariant the code did not enforce.
  Adding the guard and stopping there would have shipped an untested guard. The missing
  case was specific: both neighbouring tests were satisfied by the
  `allowed_actions`/`human_gates` re-check (one action absent, one gated), so neither
  reached the guard. Only a policy that *autonomously permits* merge/deploy/evaluate to a
  directly admitted worker — a perfectly valid document — isolates it.
- **Flipping `credential_scope=SCOPED` to `UNSCOPABLE` failed 10 tests**, proving the
  value load-bearing and the trailing sentence of its own comment ("Claiming SCOPED here
  would assert a credential nobody issued") false. The code was right; the comment
  described the opposite behavior. Fixed the comment.

**Heuristic:** after adding a guard, delete it and re-run. If nothing fails, the guard
is undefended. Do the same to any *value* whose comment explains why it is what it is —
a comment arguing against its own line is a strong signal one of the two is wrong.

## 3. A test asserting the wrong deny reason found a real production defect

The coordinator-child branch started as a bare `else: coordinator_anchor_id = own_eval.id`.
A test expecting `action_not_permitted` got `child_persona_not_permitted`, which traced
to the branch firing for the coordinator's own **evaluation** dispatch (persona
`operations`) — not a `ChildPersona` request at all. Narrowing to
`elif body.persona in {"developer", "reviewer"}` fixed it.

The wrong-reason failure mattered more than a wrong-status failure would have. Both
would have been 409s. Only the typed reason revealed that the request had been routed
through the coordination scope, which would have implied coordination authority is what
governs concluding an evaluation — the exact conflation the issue forbids.

**Generalizable:** assert on the typed deny reason, not just the status code. Two
refusals with the same status can mean opposite things about which check fired.

## 4. Acceptance-time and admission-time enforcement are not redundant

`CoordinationScope`'s validator refuses a scope naming `merge`, and
`authorize_child_request` now refuses the request again. That reads as belt-and-braces
until you notice the model can be *rehydrated* — `model_construct` skips validators, and
a document loaded from a stored `plan_document` is a build the validators never saw.
Naming the set once (`NON_DELEGABLE_CHILD_ACTIONS`) and enforcing it twice is what makes
the docstring's claim true rather than aspirational.

**Heuristic:** "validated at construction" is a claim about the constructor, not about
every instance. If a model is ever rehydrated from storage, the invariant needs a second
home at the point of use.

## 5. Test-fixture failures that reveal states production cannot produce

Three failures were fixture bugs, and each one taught something about the production
invariant:

| Failure | What it revealed |
|---|---|
| `allowed_child_actions names action(s) absent from allowed_actions` | Acceptance requires scope ⊆ `allowed_actions`. A test narrowing the policy must narrow the scope, or it asserts against a document no owner could accept. |
| `MultipleResultsFound` on the second accepted version | `load_in_force_policy` uses `scalar_one_or_none()` on `superseded_at IS NULL`. Exactly one plan is ever in force, so a test adding a version without superseding builds a state acceptance cannot create. |
| `credential_scope_unavailable` on the happy path | A developer child's token is only scopable when the policy permits `MERGE`. Adding `MERGE` to the default fixture also made it the adversarial case for §2's guard — the policy genuinely authorizes merging, so only the non-delegable set stops a *delegated* merge. |

**Generalizable:** when a fixture fails validation, the first question is whether the
state it was building is reachable in production. Twice here the answer was no, and
forcing the state would have produced tests asserting against impossible inputs.

## 6. "Cancellation remains available" needed the primitive, not the verb

The checklist asked that status and cancellation stay available after a refusal. My
first test posted to `/internal/v1/agent/cancel` and got 404 — no such route. Reading
`routes.py` and `_child_grant` showed why the obvious substitute was also wrong: a
coordinator's grant carries `{MONITOR, DISPATCH}` only, so `/control/{run}/pause` is 404
by *delegation*, and all `LIVE_CONTROL_ACTIONS` are 501 in this deployment anyway. A test
on those verbs would have asserted grant composition, not cancellation.

Rewrote it against grant revocation — the primitive the engine and operator surfaces
actually act through — asserting status → 404 and the next child request → 404.

**Heuristic:** when a requirement names a capability rather than an endpoint, find the
primitive the real callers use before picking a route. A 404 from the route you guessed
is not evidence the capability is missing.

## 7. Two address-shaped fields, one non-renderability rule

`PolicySummary` counts `evaluation_acceptance` keys because they are internal
`flow/epic/wave/node` addresses. `coordination.assigned_node_addresses` is the same
shape and got the same treatment — but the pre-existing frontend assertion
(`textContent` must not match `/\w+\/\w+\/\w+\/\w+/`) would have passed regardless,
because it only proves *some* rendering path does not leak. Asserted the count
projection separately at both layers, naming which field, so dropping either one fails a
test that identifies it.

**Generalizable:** a negative assertion over a whole block does not survive the addition
of a second field of the same kind. When adding a field that a blanket "must not
contain" rule already covers, add the field-specific assertion too.

## 8. Environment traps

- `uv sync --frozen` installs runtime deps only — no pytest, no ruff. `--all-extras --dev`
  is required. Both `python` and `.venv/bin/python` report `No module named pytest`
  otherwise, which reads like a broken venv rather than a flag omission.
- `NODE_ENV=production` is set in this environment, so `npm ci` silently omits
  devDependencies. `vitest` and `@vitejs/plugin-react` were missing and the failure
  surfaced as `Could not resolve 'vitest/config'` in the config file — which looks like a
  config bug. `npm ci --include=dev` fixes it.
- The coordinator's own launch legitimately publishes one SQS message. Five refusal tests
  asserting `messages(ctx) == []` failed on the *parent's* envelope and would have read
  as "the child was published." A `drain()` helper after launch made the later assertion
  about a genuinely new publication.
