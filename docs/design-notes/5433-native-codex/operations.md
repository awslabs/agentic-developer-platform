# Operating the shared Codex harness

Provision the dashboard and three alarms with both `enable_agent_otel=true` and
`codex_observability_enabled=true` in webhook-ingress Terraform. These settings do
not enable a persona. `codex_alarm_actions` is an explicit list of CloudWatch alarm
action ARNs; an empty list exposes alarm state without delivering notifications.

The dashboard shows observed SDK run counts by outcome, average/maximum duration,
model/tool/completion duration, shared queue age, and recent trace-correlated
execution logs. `adp.codex.run.duration` has exactly one bounded dimension,
`outcome`, with values `completed`, `failed`, `cancelled` or `unknown`. Run IDs and
persona names remain in traces/logs. No exception message, prompt, tool argument,
source content or credential is added by this instrumentation.

An SDK terminal observation is not the final Task result. The host still needs
current gateway authority, durable tool/model receipts, validation of the final
Git tree, publication confirmation, child exit and Task settlement. Never use an
SDK success metric to release a Task reservation or announce a completed change.

The alarms identify:

- Three failed SDK runs within five minutes. Open the relevant Task events and
  follow its trace to the failed operation; inspect authorized evidence rather
  than relying on the redacted telemetry error category.
- One unknown model outcome within five minutes. Reconcile the gateway's durable
  model operation and reservation. Preserve its operation identity; never infer
  that a timeout permits another model call or provider mutation.
- Shared queue messages older than five minutes for three consecutive samples.
  Inspect admission errors, worker capacity and queue delivery. This is the
  existing shared queue, so its age is not attributable solely to Codex personas.

Missing samples are treated as nonbreaching for these activity alarms, not as
proof of zero work. Use existing collector health and AWS delivery monitoring to
investigate telemetry outages. Exporter shutdown remains bounded and cannot hold
cancellation or queue acknowledgement open. No alarm delivery has been qualified
in the live account by these local tests.

CloudWatch EMF may retain a histogram as a statistical set. The dashboard therefore
uses average and maximum, not unsupported p50/p95 claims. Workload qualification
must compute percentiles from retained individual observations or a backend that
preserves the full histogram distribution. The SDK's token evidence is separate
from gateway price-bound accounting; the dashboard does not invent dollar cost.

The local OTLP HTTP test verifies run/operation span and log correlation, all four
outcome metrics, bounded dimensions, secret/content omission, and bounded shutdown
with an unresponsive collector. Terraform tests verify disabled defaults and the
exact unknown-outcome dimension/threshold. Production alert delivery, queue-to-run
coverage, retry-storm and delivery-failure qualification remain separate evidence.
