# Codex/Cognito failure checkpoint — 36219042697

[Run 36219042697](https://github.com/aws-e/adp/actions/runs/36219042697)
finished **0 passed, 2 failed**, with infrastructure cleanup complete. Both D05
and E42 remain failed. The [evidence record](codex-cognito-failure-36219042697.json)
retains the original identities, checks, failures and independent readback.

The downloaded artifact archive matches GitHub's published SHA-256
`dcf7fb92c9ddbf782c647df143c14ab206494c5cdf5867ab5628cc1ee69cf9a7`.
Its original report matches a second download byte for byte (report SHA-256
`bae810c9c4e1a5e5c53761b5cfd1798c168e617a8612b4b7d6903ac7c9f1fc3f`).
The gateway served `8cfd14826112915ee984ebf3b38d2c0a65bedb4c`; the report's
`harness_commit` identifies the pinned tenant-validation dependency, not the
scenario checkout. AWS independently confirmed EC2 `i-0d322f22912ca5ffa`
terminated at the readback recorded in the evidence.

D05 completed all six canonical-principal checks, revoked its two owned aliases
and retired principal `62afe45a-cdee-4091-9647-ec95fa63bbb7`. Its optional Cognito
extension then failed: the gateway role lacked `cognito-idp:CreateUserPoolClient`
on the existing user pool. The exact owned client name is
`owned-cognito-f1fb17a35ba429226f74cbf27ecfc587`, with original registration
`14526ca2-f707-56f4-819d-c61952fc70f1`. Read-only reconciliation found no matching
Cognito client or DynamoDB metadata. SQL retained the original admission receipt
without a completion receipt. This proves client absence after rejection; it
does not prove Cognito retirement, credential delivery or a successful lifecycle.
The receipt was not reset and no replacement client was created for recovery.

E42 Task `tsk_3fa1160d-57ab-4324-b5c1-039773cd317a` failed during Codex startup.
The host omitted the authoritative deadline from the start frame, and the strict
parser did not accept that field. The runner rejected its limits before starting
the model proxy, tool server or Codex binary. Independent strongly consistent
DynamoDB reads found zero `MODEL#` operations and zero `TURN#` records, with no
remaining query pages. The Task remained failed, with child exit confirmed,
recovery not required and queue acknowledgement confirmed. Its two observed
stream events do not establish successful coding or complete replay acceptance.

Only that proven unused `$0.50` Codex hold was reconciled to `$0` provider spend.
The original reservation and failed Task remain recorded; all other spend,
holds, counters and the original `task-api-5792-20260925` `$5` ceiling remain.
Immediately after that reconciliation, the conservative total was `$3.980319`
with `$1.019681` remaining. This is the dated reconciliation checkpoint, not a
reset or a claim that every earlier hold settled.

[#6327](https://github.com/aws-e/adp/pull/6327) fixes deadline forwarding;
[#6328](https://github.com/aws-e/adp/pull/6328) adds the narrowly scoped gateway
Cognito client permissions. [#6329](https://github.com/aws-e/adp/pull/6329)
verifies worker source `f1f776f59fd3be3ba9956d90c646a7d39ce1af02` and digest
`sha256:5d3e952c1be21b1a2656b783fbebf0d653893469d9bc17e7f0ca623cef3f8572`.
Those subsequent fixes do not alter this run's failures. Fresh live execution
and the remaining story criteria are still required.
