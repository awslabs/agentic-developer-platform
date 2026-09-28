# Engine-command envelope — v1

The cryptographic binding between a verified GitHub webhook delivery and the
`@agent-engine` command the orchestration engine later acts on.

| | |
|---|---|
| **Contract** | `engine-command-envelope` |
| **Version** | `1` |
| **Owner** | `agent-factory/webhook-ingress` + `gateway/orchestration` |
| **Golden fixture** | [`engine-command-envelope.golden.json`](engine-command-envelope.golden.json) |
| **Signer** | [`modules/agent-factory/webhook-ingress/lambda/common/command_signing.py`](../../../modules/agent-factory/webhook-ingress/lambda/common/command_signing.py) |
| **Verifier** | [`modules/gateway/src/orchestration/command_attribution.py`](../../../modules/gateway/src/orchestration/command_attribution.py) |
| **Executed by** | the signer's suite (`lambda/common/tests/test_command_signing.py`) and the verifier's suite (`modules/gateway/tests/orchestration/test_command_attribution.py`) — both read this fixture |

## Why this exists

A human comments `@agent-engine halt` on an issue. That comment does not become a
call: the webhook Lambda verifies GitHub's HMAC, writes the command onto the
`adp-<env>-webhook-events` row it already writes, and returns. The orchestration
tick finds the row on its next wake and applies the command. The two components
share no network path by design — the Lambda holds no VPC, no database and no
gateway reach, and #4303's closed-routes table forbids it gaining any.

So the **row** is the carrier of *who asked for what, on which plan*. Before this
contract those authority fields — tenant, installation, repository, issue,
commenter id, author kind, command body — were ordinary mutable attributes.
GitHub's signature was checked on the delivery and then nothing carried that
check forward. Anything that could write the row could choose the acting identity
and the routing target of a human approval, and the tick had no way to tell a
delivered tuple from an authored one.

This contract makes the binding explicit. The signer commits to the exact
authority-and-routing tuple with HMAC-SHA256 under a dedicated key; the verifier
reproduces the canonical bytes and refuses anything that does not match, **before**
it resolves an identity or uses any row field for a side effect.

## What the signature does and does not establish

It establishes exactly one thing: *this tuple came from a GitHub delivery that
passed webhook signature verification, and has not been altered since.*

It establishes **nothing about authorization**. A valid signature is not a
substitute for current tenant membership, for the `PLAN_APPROVE` permission, or
for the human-only gate checks in `state.py`. Those run unchanged afterwards, and
an authorized-but-refused command keeps the existing uniform, tagless refusal — a
distinguishable refusal would be an oracle for which plans exist in other tenants.
A signature also confers no freshness: replay protection comes from the row's own
idempotent `pending -> consumed` consume, not from the envelope.

## Two implementations, one fixture

The signer is packaged into a Lambda zip; the verifier runs inside the gateway
container image. Separate deploy units cannot import each other, which is the same
constraint that already forces `ENGINE_COMMAND_STATUS_*` to be re-declared on both
sides rather than shared.

Canonicalization is therefore written twice, and this fixture is what keeps the
pair honest: both suites assert byte-for-byte canonical output and signature
equality against these vectors. A canonicalization change on one side without the
other breaks CI here, instead of silently refusing every real human command in
production.

The `rejected_variants` in the fixture are not hypotheticals. Each is a shape that
would compile, read plausibly in review, and either open a forgery path or break
in production only for some users' input.

## Keys

The signing key lives in Secrets Manager as a small JSON keyring:

```json
{
  "active_key_id": "2026-09",
  "keys": {"2026-09": "<random>", "2026-06": "<random>"},
  "previous_valid_until": "2026-09-22T00:00:00Z"
}
```

The signer uses `active_key_id` only. The verifier accepts the active key always,
and a non-active key only while `previous_valid_until` is still in the future —
that bounded overlap is what lets events signed before a rotation still be
consumed. Unknown or stale key ids fail closed on both sides, and an un-rotated
placeholder value is treated as *no key at all* (a confident verdict computed
under a secret that ships in the repo reports success, which is worse than no
verdict — the same reasoning as #4128 for marker signing).

**The key is dedicated and must stay so.** It is deliberately not the
marker-signing key: the agent worker role holds a purpose-scoped `kms:Decrypt`
for that one, so reusing it would grant the ability to mint human approval
attribution to the identity the authority boundary exists to constrain.

Rotation procedure: [`docs/runbooks/engine-command-signing-key-rotation.md`](../../../docs/runbooks/engine-command-signing-key-rotation.md).

## Changing this contract

Adding, removing or retyping a field, or changing canonical form, is a **breaking
protocol change**: bump `ENVELOPE_VERSION` in the signer, teach the verifier the
new version, add vectors for it, and keep the old version accepted until no
unconsumed rows carry it. A verifier that meets an unknown `protocol_version`
refuses — it never guesses at a layout.

Any field the tick reads as authority or routing **must** be in the signed set. A
field the tick trusts but the signature does not cover is precisely the
forgeable-attribution defect this contract removes.
