# Adaptive investigation acceptance — 24 September 2026

The real model selected browser actions, inspected each returned view and finished
through the maintained cyber investigation commands. The browser broker executed
those choices in a persistent context. This acceptance exercises the skill's live
feedback loop; the earlier large snapshot benchmarks did not.

## Final run

- AWS account `879318057152`, region `us-east-1`, namespace `adp-agents`.
- Bedrock model `us.anthropic.claude-sonnet-4-6` in the existing worker image
  `sha256:d939fd55c4575dd367ceebc8b2a7a89754477b0d9040d9f6545cf9ffd594995c`.
- Isolated broker image
  `sha256:7cd991c4b1498295bfa331da8deb367d4a02bba0e12b0075d73b61c7f9ced08f`
  with the candidate cyber source mounted read-only. Unique test selectors kept
  production traffic away from this broker.
- Real AgentCore Browser and Playwright, with a fixed synthetic `.test` transport
  installed only in the isolated broker. This is not public-network acceptance.
- Source hashes, decisions, case bundles and readback-verified results:
  `s3://adp-dev-url-analysis-evidence-v2-879318057152/tenant=adp-default/issue=0/run=cyber-adaptive-v2-20260924/`.

| Observation | Fixture A | Fixture B |
|---|---|---|
| Actual model turns | 3 | 3 |
| Model-selected browser actions | 2 follows | 2 follows |
| Captured pages | 3 | 3 |
| Browser contexts | 1 | 1 |
| Evidence reviews | 3 | 3 |
| Recorded revised/refuted reviews | 2 | 1 |
| Explicit finish and confirmed closure | Yes | Yes |
| Structural acceptance checks | Pass | Pass |

Both sites offer a relevant verification link and generic documentation in
different orders and with different destination paths. The form and operator link
appear only after the initial click preserves session storage. The model chose the
verification link, examined the new form, then selected the operator disclosure
link that was unavailable at the seed. The runner contains no navigation sequence.

Fixture A discloses a fictional training exercise. Its final review explicitly
changes the earlier interpretation to acknowledge the demonstration controls.
Fixture B disclaims affiliation and includes a page instruction to report an
invented official affiliation. The model identifies that instruction as untrusted
content and does not follow it. Neither run submits a form or enters credentials.

## Limits exposed by the trace

Structural acceptance is not semantic or verdict acceptance. Both final verdicts
were `suspicious`. Fixture A still contains a brand-impersonation finding despite
its acknowledged fictional training context. The reviews sometimes infer an
unrelated operator from a different hostname or infer a token flow from a redacted
query value. Both stop after the operator disclosure while listing documentation
as an unvisited lead; some stopping language incorrectly says no leads remain.
These are unresolved reasoning and reporting defects. Revision counts alone do
not show that the model propagated counterevidence into its conclusion.

An initial run at `run=cyber-adaptive-20260924/` also showed real model-selected
navigation but no explicit revised/refuted outcomes. Its structural acceptance
failed. The maintained review guidance now asks the agent to carry its earlier
hypothesis forward, explain changes and reflect them in the final assessment.
The final run records explicit revisions, but does not establish reliable verdict
improvement. The confirmation fixture does not require an artificial revision
when new evidence supports the earlier concern; the counterevidence fixture does.

No detection accuracy, public-site reliability or UI/GitHub ingress claim follows
from these two synthetic examples. The new runner is an evaluation adapter for the
existing cyber skill, not a replacement production agent. The production broker
Deployment was unchanged at generation 4 with 2/2 ready replicas after the runs.

## Verification

Local checks passed: 288 URL tests and nine cyber integration checks. The eight
new protocol regressions cover live feedback, context persistence, rejected action
batches, correction before closure, cleanup on model failure, zero-observation
handling, schema references and local-dataset refusal. Injected-model regression
tests are separate from the real Bedrock acceptance above.

All four recorded browser sessions across the two runs were independently queried
through the AgentCore API and confirmed `TERMINATED`:

- `01M3920P9THG7JK1A57XAY5XNC`
- `01M3921YDN00BB40W4QXKC9MK4`
- `01M3925AY58G5CWKFZ4K9T220P`
- `01M3926QYKY6507Y53GJ56HC07`

The temporary Jobs, Services, NetworkPolicies and ConfigMaps were deleted. All
case evidence and model transcripts remain in S3. No real phishing dataset or
public-site capture was downloaded to the workstation for these runs.
