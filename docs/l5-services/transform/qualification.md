# Transform provider and executor qualification

**TF01 record, version 1.0; reviewed design, not a live qualification.** The
[machine-readable capability matrix](qualification-v1.json) declares which paths
this release can use. At this checkpoint every positive provider operation is
unqualified. File-only tool isolation and remote cancellation are *unsupported
by this release*, not proven impossible for all provider versions. Nothing here
authorizes provider work, deployment, a budget change or Transform admission.

| Path | Verdict | What is established and what is missing |
|---|---|---|
| Test executor | Unqualified | The existing credential-free Kubernetes executor checks a pinned archive hash, network policy, image digest and a 120-second per-check limit in local tests. Its public service requires a verified Task attempt; there is no authorized L5 non-blocking launch/read/terminate/collect bridge or pinned node qualification receipt. Transform API and worker effects remain disabled. |
| Provider CLI and invocation | Unqualified | [Official custom-transform documentation](https://docs.aws.amazon.com/transform/latest/userguide/custom.html) and its [command reference](https://docs.aws.amazon.com/transform/latest/userguide/custom-command-reference.html) describe `atx custom def exec` and resume. No current CLI executable was found on this worker's `PATH` at the checkpoint. Its exact version, binary SHA-256, provider-job image digest, supported invocation/auth, definition version, output format and region are therefore **not verified**. Historical `atx 3.19.0 json` evidence is not qualified support. |
| File tool isolation | Unsupported for enablement | No trusted finite provider job currently proves that credentials cannot reach repository hooks, shell/build/test tools, MCP servers, skills, host paths, or unrelated network endpoints. A prompt restriction cannot meet this boundary. Credential-free preparation needs separately approved registry egress; baseline and candidate checks must then share sealed dependencies and deny network. |
| Submit and native identity | Unqualified | No current-interface, admitted-budget submission receipt or captured native session ID exists here. Absence of an acknowledgement is not a no-effect proof. A persisted attempt token/input hash must be reconciled, never submitted again merely because a process restarted. |
| Bounded read and resume | Unqualified | No native readback, original patch or before/after ID continuity receipt exists. A documented `--resume` argument does not prove loss recovery or paid idempotence. Retain `outcome_unknown` and the prior cost liability when reads cannot establish identity. |
| Remote cancel | Unsupported for enablement | No qualified remote-cancel action or matching native stop receipt exists. Local process termination is not remote cancellation. Keep last observed provider state and cost unknown unless independently observed. |

Local evidence: at `a84eec5037ce1f1f7fec79778ca73249ebdcd9d2`,
79 affected L5 tests, seven selected-executor preflight/isolation tests and five
Task-service boundary tests passed. These are synthetic/local observations, not
cluster or provider receipts. The [approved architecture](architecture.md) records
the historical POC and the exact selected executor; no account, region or repo is
assumed. `qualification-v1.json` has no positive receipts or pinned binary.

The [synthetic recovery fixture](../../../modules/L5-services/tests/transform/qualification-scenarios.json)
pins a fixed clock and source hash, isolates a fictional tenant and creates no
external resources to clean up. `test_provider_qualification` compares its
independent expected verdicts with the matrix and a denied API submission;
existing SQL recovery tests check that an ambiguous provider acknowledgement
retains the same durable attempt. Neither test is a native provider receipt.
With Python 3.12+ and `uv` installed, run from `modules/L5-services/`:

```sh
uv run --extra dev pytest -q tests/transform/test_tf01.py -k test_provider_qualification
uv run --extra dev pytest -q tests/integration/test_lambda_execution.py -k 'submit_attempt_survives_uncertain_effect or ambiguous_reconciliation_keeps_one_attempt_and_owed_work or cancel_response_loss_reconciles_status_without_second_cancel'
```

To revise a verdict, TF01 needs the selected, authorized provider connection,
region and admitted cost/time/source policy; an executor-owner bridge with exact
namespace/node UID and qualification revision, image digest and L5 grant; and a
pinned trusted provider job whose tool allowlist can be enforced outside prompts.
Record the exact invocation and digest, scoped auth selection, source and policy
hashes, provider/native IDs, receipt times and costs, each loss/restart/cancel
attempt, bounded polling outcome and owned cleanup in **private** evidence.
Publish only sanitized receipt digests and verdicts after independent review;
never publish operational identities or private output here. No paid probe has
been run or authorized by this PR. TF01 owns qualification and defect repair;
TF07 owns installation and integrated live acceptance. Missing live receipts
remain blocked, not passed by the local synthetic tests.

## Installed provider evidence collector

`qualification_transform_provider.InstalledProvider` consumes only the
post-verification callback from the original source, patch and detached-test
evaluator. It reads the current protected worker/node/image pins and original
provider checkpoint, then validates the bounded provider trace. It also requires
the existing executor-isolation producer to have run or reverified both exact
images during the same release collection. A historical journal row or stored
`qualified` flag cannot substitute for those observations.

The direct `provider-executor-qualified` release check can be issued only after
both original refactor and dependency-upgrade cases show actual original-session
continuation, cumulative usage, allowed source reads/writes, denied shell tools,
and denied source access outside the admitted policy. Each case must retain the
same current installed executor controls and independent before/after tests.
The collector reports missing trace/control components and a specific next
action; it never starts work, changes qualification, obtains provider credentials
or invokes a UI. EC-01 and EC-02 remain incomplete until their other original
requirements are independently observed.

The collector and runtime receipt producer do not bypass admission's initial
qualification requirement. A first-ever qualification still requires a separate
reviewed bootstrap authority. Neither source implementation nor offline tests
authorize installing synthetic qualification records or enabling consumers.
