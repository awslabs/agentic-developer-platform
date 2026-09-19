# Superplane reasoning-session hosting

Issue #5050 (U5), EPIC #4910. Requirement **R10**.

This directory is the *hosting configuration* for Superplane reasoning sessions: the
recorded hosting decision, the ordering rule for work that must outlive an agent
process, and the operator-alert emission path.

It contains no Kubernetes manifest and no Terraform, and that absence is the decision —
see below.

## The hosting decision: Agent Factory, no dedicated lane (R10 acc. 1)

**Reasoning sessions run on the existing ADP Agent Factory hosting lane. This story adds
no Superplane-specific queue and no Superplane-specific `ScaledJob`.**

R10 states the rule that forces this to be an argued choice rather than a default:
design note §11 line 778 says to *use ADP Agent Factory* for reasoning sessions, so the
existing lane is the default and a dedicated lane must be justified by *an actual
implementation need — a genuinely different isolation, concurrency, IAM or image
requirement that the existing lane cannot express*.

Agent Factory's lane (`modules/agent-factory/webhook-ingress/infra/scaledjob.tf`) already
expresses every property acceptance 1 asks for:

| R10 acc. 1 property | Where the existing lane provides it |
|---|---|
| Scale to zero | `minReplicaCount: 0` (`scaledjob.tf:174`) |
| One job per message | `jobTargetRef` with `parallelism`/`completions` of 1, driven by the `aws-sqs-queue` trigger |
| Per-message isolation | A fresh pod per message; nothing is shared between sessions |
| Failure evidence | `failedJobsHistoryLimit: 5` (`scaledjob.tf:187`) — the value acc. 1 names |
| No mid-run node reclaim | `karpenter.sh/do-not-disrupt: "true"` (`scaledjob.tf:206`) — the annotation acc. 1 names |

So the two precedents acceptance 1 offers (`cyber/k8s/cyber-triage-scaledjob.yaml` and
the canonical `scaledjob.tf`) are precedents for *how to build a lane if one is needed*.
Superplane does not need one:

- **Isolation** — identical. A reasoning session needs a fresh pod per message, which is
  what the lane already does. Superplane has no stricter requirement; notably it needs no
  network-egress lockdown of the kind that gives `cyber` a reason for its own
  `NetworkPolicy`.
- **Concurrency** — identical. Per-message isolation with scale-to-zero.
- **IAM** — identical *for hosting*. The Superplane-specific runtime identities
  (`adp-<env>-superplane-control-plane`, `adp-<env>-superplane-skypilot-api`) belong to
  the Superplane **control plane** pods and are owned by U3's
  `infra/control-plane/irsa.tf`. They are not the reasoning agent's identity: the
  reasoning agent is an Agent Factory agent and uses Agent Factory's `agent-scaledjob-sa`.
- **Image** — identical, and this is the strongest evidence. The two Superplane personas
  are registered in Agent Factory's own catalogue
  (`webhook-ingress/lambda/common/personas.py:46-47`, added by U4) and their prompt files
  and skills stage into the *same* `adp-agent-runtime` image through
  `agent-worker-image/stage-personas.sh` and `Dockerfile:41`
  (`COPY modules/domain-apps/ /source/domain-apps/`). There is no second image to host.

**The upstream deployment shape is explicitly not a reason.** That Superplane ships its
own SQS + KEDA `ScaledJob` upstream describes how the component being retired was built.
It is R19 parity evidence, not a requirement ADP inherits. R10 says so directly.

### Why the absence is tested rather than only written down

A second hosting path is not a neutral cost: it diverges from Agent Factory's and
inherits none of its fixes. `tests/test_hosting_choice.py` asserts that this module
introduces no `ScaledJob` manifest — so adding one becomes a deliberate act that fails a
test and forces the reason into the diff, which is what R10 asks for. The test is
conditional in the same way the requirement is: if a lane is ever genuinely justified,
the test requires it to carry `failedJobsHistoryLimit: 5` and
`karpenter.sh/do-not-disrupt: "true"`.

Because no lane is added, **no Terraform applies for this story** and there is no
infrastructure to destroy on rollback. Reverting the PR is sufficient.

## Ordering for work that outlives the agent (R10 acc. 2)

`handoff.py` implements one ordered sequence, copied from the established precedent in
`modules/domain-apps/cyber/workers/triage/handler.py:178-248`:

1. **durable state written** — the session's result is persisted;
2. **durable handoff sent** — the downstream consumer is notified through a durable channel;
3. **input message deleted last** — the inbox message is acknowledged.

The order is the whole point. Deleting the input message is the irreversible
acknowledgement that the work is safely recorded, so it can only be correct as the final
step. A crash at any earlier point leaves the message unacknowledged; its visibility
timeout expires, the message returns to the queue, and KEDA spawns a replacement job. The
inverse order loses work silently: the message is gone and nothing durable records what
it was for.

`complete_session()` therefore returns a *step log* rather than a bare success flag, so
the ordering itself is assertable and not merely the fact that three calls happened.

## Operator alerts that outlive the agent (R10 acc. 3)

`alerts.py` implements the emission path for overdue-cleanup and budget alerts.

**The requirement is about lifetime, not about calling an API.** Acceptance 3 asks that an
alert be observable *after the agent process has exited*. Anything emitted from inside the
agent — a log line, a direct notify call — fails by construction: if the agent dies before
that statement runs, no alert ever existed, and the case where the agent died is precisely
the case an operator most needs to hear about.

So the path splits ownership of the alert's lifetime in two:

- **Inside the agent**, `record_alert()` writes a durable alert record. This happens as
  part of step 1 of the ordering above — before the input message is deleted — so a crash
  leaves both the work and the alert recoverable.
- **Outside the agent**, a sink whose `lifetime_owner` is not the agent reads durable
  records and delivers them. The agent's responsibility ends at "the record is durable".

`emit()` refuses a sink whose lifetime is the agent's, and refuses a log-only sink. Those
are not stylistic checks: they are the two ways this requirement gets accidentally
un-met, and a rejected sink at the boundary is what stops a log line from being presented
as an alert.

### This path is net-new, and the sink resource is not deployed by this story

There is nothing to wire up. `put_events`/`putEvents` has **zero production callers**
repo-wide — the only occurrences are assertions in `.github/scripts/tests/` that ops
dispatch does *not* use it — and no custom event bus exists. Run completion today is
log-only. Describing this as "wiring up existing eventing" would under-size the work and
ship nothing observable.

What this story delivers is the **emission path and its boundary**: the durable record,
the sink contract, and the rejection of the two sink shapes that would silently fail the
requirement. Deploying the actual sink resource is outside the paths allocated to U5
(`repo-path-allocation.md` row U5: `agent/` hosting config plus the `PHASE_REGISTRY`
entry) and is part of the unresolved gate on the live criterion below.

## Out of scope (R10 acc. 4)

Hosting hosts reasoning sessions and nothing more. This directory implements **no**
job/attempt state machine, **no** queue, **no** approval store and **no** budget ledger.
Agent inboxes are not workload queues: the durable workload lifecycle belongs to **B**.

Where B's surface is needed it is mocked behind the protocols in `handoff.py` and
`alerts.py` and **recorded as a mock**, per `acceptance-split.md` rule 2 — a green run
against a mock closes no live criterion.

Concretely, the `DurableStore`, `HandoffChannel` and `AgentInbox` protocols are ports.
This story owns the *ordering between them*, which is R10 acc. 2. It does not own their
implementations, and defining a port is not the same as taking ownership of the lifecycle
behind it.

## Deferred live criterion

- [ ] **U5-L1 / R10 acceptance 3 (live)** — an overdue-cleanup or budget alert is
      **observed after the agent process has exited**. The tests here assert that the
      emission path is durable and that its sink's lifetime is independent of the agent;
      that is not the same claim. The criterion is the alert *arriving* with the agent
      gone. **Gate: a named account/environment, the deployed sink, and a named owner to
      receive it — all unresolved** (`validation-mapping.md` §"Inputs that remain
      unresolved"). No account ID or credential label is invented here.
