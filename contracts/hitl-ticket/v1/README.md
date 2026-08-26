# HITL ticket contract — v1

The shape of an agent asking a human a question, and the answer that comes back.

| | |
|---|---|
| **Contract** | `hitl-ticket` |
| **Version** | `1` |
| **Owner** | `harness/hitl` |
| **Normative validator** | [`models.py`](models.py) |
| **Golden fixture** | [`hitl-ticket.golden.json`](hitl-ticket.golden.json) |
| **Executed by** | [`.github/workflows/hitl-ticket-contract-tests.yml`](../../../.github/workflows/hitl-ticket-contract-tests.yml) |
| **Consumers** | **None yet** — deliberately. See [Status](#status). |

## Why this exists

When an agent needs a human decision — approve this deploy, confirm this
destructive change — there has been no agreed shape for that request. Every place
that needs to ask a human has invented its own format for what it's asking, which
answers count, what happens on a timeout, and who is allowed to answer.

ADP's two existing approval paths disagree, and not stylistically:

| | `skill-agent.ts:111-140` | `services/ApprovalService.ts:16-60` |
|---|---|---|
| Bound | 60 polls × 30s ≈ 30 min | `while (true)` — **none** |
| On expiry | denies (`approved: false`) | unreachable — never expires |
| Who may answer | any non-bot commenter | **any** commenter |
| Answer vocabulary | `{approved: boolean}` | `{approved, rejected}` |
| Binding | positional — "the current wait" | positional |

The right-hand column is an unbounded wait that any commenter can satisfy. The
left-hand column is bounded and fail-closed. That is a difference in security
posture, and this contract is the written answer to "which behaviour is correct."

`skill-agent.ts`'s bounded, deny-on-expiry behaviour is the **reference
behaviour** for timeout semantics. Its positional binding and boolean vocabulary
are not.

## What is borrowed, and what is not

The borrow is from the DeepSeek Harness assessment: take dsh's **vocabulary**,
keep ADP's durable **mechanism**.

**Borrowed — the vocabulary.** dsh's fail-closed permission result set is four
values where ADP's paths have two. The distinction that matters is `unavailable`:
"nobody could be asked" is a different outcome from "someone said no," and
conflating them makes a transport failure silently indistinguishable from a human
denial. dsh also uses **named** approve intents rather than positional ones, so
an approval cannot be misapplied to a different pending request than the one the
human was looking at.

**Not borrowed — the mechanism.** dsh pauses on an in-process blocking Promise.
ADP's durable AIDLC gate is strictly better and is **explicitly unchanged** by
this contract: the worker commits its state, posts a `<!-- aidlc-gate:<stage> -->`
marker comment, and **exits holding zero compute**; a later dispatch mints a
synthetic human turn to resume. This contract describes the *payload* that flows
through that gate. It does not propose replacing the gate.

ORCA's `remote_questions` table (`status IN ('pending','answered','closed')`,
`answer_body`, survives disconnection) is external validation that a durable
ticket is the right shape — two independent investigations converged on this same
missing primitive from opposite ends.

## The answer vocabulary

```
result: 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'
```

| Value | Meaning | Permissive? |
|---|---|---|
| `allowed-once` | Approval, scoped to **this** ticket, non-durable by construction. | **Yes — the only one.** |
| `rejected` | A human, whose identity is recorded, said no. | No |
| `cancelled` | The asking side withdrew the request before it was answered. | No |
| `unavailable` | Nobody could be asked, or the ask failed in transport. **Not** a human denial. | No |

There is deliberately **no `allowed-always`**. A persistent grant is a policy
decision with its own audit and revocation story — it is not an answer to a
question, and v1 has no escape hatch for one.

`unavailable` vs `rejected` is the distinction the whole four-value set exists
for. A retry is reasonable after `unavailable`; it is wrong after `rejected`. A
consumer that cannot tell them apart will either retry a denial or treat an
outage as a decision.

### Human choices vs. system results

`options` lists the answers a **human** may choose for a given ticket.
`cancelled` and `unavailable` are **system results** (`SYSTEM_RESULTS` in
`models.py`) and are always valid answers regardless of `options` — they are facts
about the world, not choices a ticket grants, so a ticket cannot opt out of them
by omission. Both are non-permissive, so this exemption can never widen what is
allowed to proceed; `models.py` and the test suite both assert that.

## The four invariants

**1. `allowed-once` is the only thing that means proceed.**
Any other result is non-permissive. **Absence of a response is non-permissive.**
There is no value other than `allowed-once` that permits an action. Consumers
should branch on `HitlResponse.is_permissive`, never on a denial list — a
`result !== 'rejected'` check turns every future result value, and every typo,
into an accidental permit.

The contract cannot represent absence, so "no answer is not an answer" is a
consumer obligation. Practically: a consumer must have a deadline and must apply
`timeout.on_expiry` when it passes.

**2. A response MUST carry the `ticket_id` it answers.**
A response without one is invalid. This is the named-vs-positional rule: with
positional approval, a human looking at request X can have their approval applied
to request Y that arrived in the meantime. Use `answers_ticket(ticket, response)`
to check the binding — and note a `True` return is necessary but **not sufficient**
to proceed, because it does not check identity (see invariant 4).

**3. `timeout.on_expiry` may only be a non-permissive result.**
Silence must never become consent. Left unspecified, two implementations pick
opposite defaults — which is exactly what happened above. `expires_at` is an
absolute, timezone-aware instant; relative durations are excluded because a
duration is ambiguous across the process restart a durable ticket is designed to
survive.

**4. Answer identity MUST be recorded, and MUST be checked against `approvers`.**
`answered_by` is required for **every** result, including the system-produced
ones, so the question "was this principal permitted to answer this ticket?" is
always answerable.

**The contract cannot enforce this half, so it is stated as an obligation:**
verifying `answered_by` against `approvers` requires the consumer's own notion of
principals and repo maintainership, which lives outside a schema. `models.py`
deliberately does **not** pretend to do it. A consumer that validates the shape
and skips the identity check has reproduced today's bug — where any commenter's
`/approve` counts — against a contract that looks like it prevented it. **Schema
validity is not authorization.**

## Field → mechanism mapping

Adopters should not be implementing against a fiction, so every ticket field maps
to something ADP's gate already carries:

| Contract field | What carries it today |
|---|---|
| `ticket_id` | the gate marker `<!-- aidlc-gate:<stage> -->` — stage-scoped, so unique per issue+stage |
| `scope` | implicit: `gate-stage` for every AIDLC gate |
| `prompt` | the body of the gate comment the worker posts |
| `options` | implicit and undocumented: `/approve` or `/reject` |
| `timeout` | **absent** in the gate; `skill-agent.ts` has it, `ApprovalService.ts` does not |
| `approvers` | **absent** — the gap this contract names |
| `context` | `aidlc/` committed state + the correlation/invocation trailer already on agent comments |
| response `answered_by` | the GitHub comment author (checked only for bot-ness today) |
| response `answered_at` | the comment `created_at` |

The two `absent` rows are the substantive additions. Everything else is
formalizing what already flows.

## Status

**No consumer implements this yet, and that is intentional.** The contract lands
first so the two divergent implementations can be reconciled against a written
shape rather than against each other, and so a revert costs nothing while nothing
depends on it.

There is no human-run integration test here because there is nothing to
integrate with. That is stated plainly rather than papered over with a test that
asserts against a mock of a consumer that does not exist.

**When the first consumer arrives:** add it to the fixture's `$comment` consumer
list and to the workflow's `paths:` filter. That is what keeps the fixture
load-bearing rather than decorative — an unreferenced fixture drifts.

## Conventions

This directory follows the repo's **working** contract convention
(`contracts/provenance/v1/` + `.github/workflows/provenance-contract-tests.yml`):
a golden fixture, a validator, and a CI job that **executes** the fixture on every
PR touching it.

Note it does *not* follow the JSON-Schema envelope sketched in
`modules/harness/contracts/README.md:41-57`. No `*.schema.json` file and no
JSON-Schema validator library exists anywhere in this repo; adopting one here
would add a dependency and a second, unexecuted convention. The `name` /
`version` / `owner` envelope from that README **is** honoured — see
`ContractEnvelope` in `models.py`, which pins all three to literal values so a
document from a different contract, or a future v2, cannot validate as v1.

Per the harness contracts README's versioning rules: additive changes (new
optional fields) do not bump the version; removing, renaming, retyping, or
changing the semantics of a field requires a `v2/` directory beside this one.
Widening the `result` vocabulary is a **breaking** change even though it looks
additive — every consumer's exhaustive branch becomes non-exhaustive.

## Running the tests locally

```bash
pip install pytest 'pydantic>=2'
python3 -m pytest contracts/hitl-ticket/v1/ -v
```
