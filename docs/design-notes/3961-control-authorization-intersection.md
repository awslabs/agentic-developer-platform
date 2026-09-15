# #3961 — The control-authorization intersection: two callers, one gate

**Status:** blocker found during #3961 implementation. The pause barrier itself is
built and proven; **end-to-end pause on the human dashboard path cannot work**
until this is resolved. Filed as the ADR-1 change proposal the story's stop
condition requires.
**Issue:** [#3961](https://github.com/aws-e/adp/issues/3961) (S2 — harness-neutral
pause/resume), wave 2 of live-controls EPIC #3959
**Touches:** [#5028](https://github.com/aws-e/adp/issues/5028) delegated authority
(`docs/design/agent-delegated-authority.md`), #3960 revival design
**Live acceptance:** evaluation [#3968](https://github.com/aws-e/adp/issues/3968)

## The problem in plain terms

An operator presses **Pause** on a running agent from the dashboard. The worker
refuses it with "not authorized," even though the operator owns the run, is
logged in, and the pause machinery inside the worker is working correctly. Every
human-initiated pause fails this way, in every deployment, for the same reason —
so the button cannot ship even though the feature behind it is finished.

The cause is that two separate pieces of work each solved authorization for their
own caller, and nobody designed the point where they meet. #5028 built
authorization for *one agent commanding another* and requires every command to
carry a signed permission slip. #3960 built authorization for *a human commanding
their own run* and proves ownership a different way, with no permission slip.
Pause is the first verb that actually does something, so it is the first time
those two designs are forced to agree — and they don't.

**The fix in one line:** teach the gateway to sign a permission slip for a
logged-in human who owns the run, as its own kind of authority, instead of either
faking an agent's credentials or removing the check.

## Why it surfaced now and not in #5028

The worker listener decides whether a command needs a signed envelope with
(`modules/agent-factory/agent/src/control-listener.ts`):

```ts
private requiresEnvelope(action: ControlAction): boolean {
  return this.config.store.capabilities()[action] === true;
}
```

This coupling is deliberate and its comment explains why: *"When a verb joins
`SUPPORTED_ACTIONS`, it becomes envelope-gated by that fact alone, with no second
edit to remember here. That coupling is the point; a separate opt-in list is a
list someone forgets to add to."*

The same comment states the consequence that has now expired: *"this returns
false for every verb today, so the envelope path ships tested but dormant."* #3961
is the first story to enable a verb, so it is the first story for which
`requiresEnvelope` returns true — and the first to discover that the human path
has nothing to put in the header.

`grep -i envelope` over `modules/gateway/src/activity/` returns nothing.

## The four independent blockers

Enabling pause/resume is not one gap but four. Two are downstream of envelope
*minting*, so "make the gateway sign something" does not clear them.

| # | Blocker | Evidence |
|---|---|---|
| 1 | Human path sends no envelope. `_request_pod` sends only `Authorization: Bearer <control token>` and `X-Adp-Control-Generation`. | `modules/gateway/src/activity/control_service.py` |
| 2 | Nothing for a human to cite as authority. `sign_envelope` requires `grant_id` + `revocation_epoch`; `_sign` raises when `authorized.grant is None`; `policy.authorize` refuses a grantless mutation. | `src/agentauth/envelope.py`, `src/agentauth/adapter.py`, `src/agentauth/policy.py` |
| 3 | The pre-delivery re-check refuses pause regardless. `deliverAuthorized` → `/internal/v1/agent/revalidate` → `require_supported(action)`, and `SUPPORTED_AGENT_ACTIONS = frozenset({MONITOR})`. A perfectly-minted envelope yields 202 then a journaled `rejected`. | `src/agentauth/policy.py`, `src/agentauth/revalidation.py`, `control-state.ts` |
| 4 | Verification keys are not provisioned by default. Envelope keys exist only when `agent_authority_enabled` is true, default **false**; with no keys the listener returns `not_configured` → 403. The revalidation client also hard-requires `ADP_AGENT_AUTHORITY_ENABLED=true`. | `webhook-ingress/infra/agent-authority-bootstrap.tf`, `variables.tf`, `control-revalidation.ts` |

Blockers 3 and 4 mean pause cannot work end-to-end in a **default** deployment
even with blockers 1 and 2 solved.

## Why the obvious workarounds are wrong

**Fabricate a grant for the dashboard user.** Forbidden in terms, twice.
`docs/design/agent-delegated-authority.md` Decision 2: *"Another human initiation
path must add a `kind` with an equivalent verified record rather than loosen this
one."* And `grants.py`: *"Nothing here is ever built from request data — a
caller-constructible grant is not a grant."* There is also nothing real to cite:
`orchestration_decisions` is the only verified human-authority table, its
`flow_id` is `NOT NULL` with an FK to `orchestration_flows` (an ordinary webhook
run has no flow), it is append-only, and every `actor_kind=HUMAN` writer is a
gate/plan/node decision — none is "a human asked to pause run X." The invocation
row is explicitly rejected as authority and is worker-writable.

**Enforce the envelope only when the header is present.** Unsound: an attacker
omits the header and is admitted unconditionally. Strictly worse than either
extreme.

**Distinguish the human gateway by some marker.** Structurally impossible. The
listener sees one bearer token; the human gateway and an attacker holding a stolen
token present byte-identical requests. Any marker the gateway can add, a
token-holder can add too — there is no keyed material on the human path, because
the worker deliberately holds public keys only.

**Drop envelope enforcement for supported verbs.** This loses a real, documented
property: cross-run control by a compromised worker. The worker role holds
unconditioned `dynamodb:UpdateItem` on the webhook-events table, whose own
comment concedes *"any worker may write any run's row… No condition key fixes it:
the row key is data."* `envelope.py` states the read direction: a compromised
worker that reads another run's row learns that run's token. The NetworkPolicy
backstop is not enforced by default (`enable_network_policy_controller` defaults
false, and evaluation #3967 W1-04 measured a non-gateway caller still getting HTTP
200). So in a default deployment the token is the only barrier and it is
obtainable — and worker-to-worker denial is an explicit AC8 requirement.

## Proposal: `human_session` as a second authority kind (ADR-1 change)

The envelope currently conflates two claims:

- **(a)** the trusted gateway sent this command;
- **(b)** a delegated grant authorized it.

Only **(b)** needs a grant. Only **(a)** defeats the stolen-token attacker and the
compromised worker — and **(a)** is the entire property that removing the gate
would lose.

Add a `human_session` envelope variant carrying `principal` = the authenticated
user and a signed `authority_kind` claim, with **no** `grant_id` /
`revocation_epoch` rather than faked ones. This preserves (a) exactly, fabricates
nothing, and stays inside Decision 2's letter: Decision 2 governs *delegated
agent* authority derived from a past human act, whereas a logged-in user who owns
the run **is** the human, directly authenticated (tenant AND human owner).
Revocation stays bounded because the session is re-checked per request and the
envelope lives 30 seconds.

Cost, stated honestly: `grant_id`/`revocation_epoch` become conditional in
`_REQUIRED_CLAIMS` in **both** verifiers (Python `envelope.py` and TypeScript
`control-envelope.ts`), plus new shared negative vectors. The variant must be
keyed on the signed `authority_kind` claim so a *grant-path* envelope can never
omit its epoch — otherwise this becomes the hole it is meant to avoid. Blocker 3
additionally requires a decision about `SUPPORTED_AGENT_ACTIONS`, and blocker 4 a
decision about whether pause requires `agent_authority_enabled`.

That is a change to a verifier #5028 hardened deliberately, spanning two
languages, an IAM/Terraform default and a second action allowlist. It is its own
story, not a line in #3961.

## What #3961 does about it

- The barrier, gate, adapter wiring and worker runtime are built and unit-proven;
  that is what the story is actually about and it is unaffected.
- The gap is **executable**, not just asserted: `control-listener.test.ts`
  pins supported-verb + no-envelope → 403 with nothing journaled, in
  `describe('envelope enforcement boundaries')`.
- The gate is **not** weakened. Every option that would let the dashboard through
  today either admits stolen tokens or fabricates authority the design forbids.
- Per the story's stop condition, pause capability must not be advertised as
  accepted on the strength of unit tests alone.

## References

- `docs/design/agent-delegated-authority.md` — #5028 design, Decision 2
- `docs/runbooks/agent-control-evaluation.md` — wave-2 checks W2-03/04/05
- `docs/runbooks/network-policy-enforcement.md` — the unenforced backstop
- `modules/agent-factory/agent/src/control-listener.ts` — `requiresEnvelope`
- `.github/workflows/agent-control-ci.yml` — the two `EXPECTED_SUPPORTED` gates
