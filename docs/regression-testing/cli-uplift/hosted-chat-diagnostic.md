# Two-turn hosted chat diagnostic

The shipped dispatcher purpose `hosted_chat` is an explicit paid diagnostic.
Select `login,hosted-chat` with the workflow `fixtures_json` input to run D01
through normal reporting and EC2 cleanup. It is excluded from default nightly/full suites.
It does not replace or upgrade the read-only E40 readiness/history case, and
neither success nor cleanup claims shared-spend reconciliation.

Run it only on the harness-owned installed EC2 fixture using the existing
`_run_worker`/SSM dispatcher transport. Supply the ordinary journey payload
(`evaluation_id`, `cli_path`, `work_dir`, gateway/account/instance binding, `test_user_id`, and
the existing on-instance session-vault reference) plus:

```json
{"human_task_chat":{
  "enrollment_verified":true,
  "shared_budget_authorized":true,
  "max_tasks":2,
  "max_task_usd":0.25,
  "login_user_id":"INSTALLED_FIXTURE_LOGIN_USER_ID",
  "canonical_user_id":"SELECTED_TENANT_FIXTURE_USER_ID",
  "tenant_id":"aws-e"
}}
```

The real standing human policy must enforce the stated per-task budget. Check
remaining shared qualification headroom before running; the diagnostic never
resets counters or grants enrollment. Rooting it in the operator login instead
of the installed fixture is refused by the expected login check. The fixture's
selected tenant is resolved normally by the served CLI (`ADP_TENANT`), and the
server's chat capabilities must match the exact canonical human and tenant.

The diagnostic asks the investigator to remember a unique label, then asks for
that label in a second task without repeating it in the second prompt. Both
requests derive stable IDs and the label from the evaluation/fixture identity,
so restarting the same diagnostic cannot manufacture replacement turns. They
have bounded replay, exact task-correlated completion and
one user/assistant history pair each. It refuses to start turn two if turn one
is uncertain or needs clarification. It does not guess an answer to a pending
question or generate replacement request IDs.

Before invoking the worker, construct `payload["recovery_plan"]` with
`tests.e2e.cli_uplift.remote.chat_plan.recovery_plan(payload)` and pass the
existing externally backed run manifest to
`run_worker(instance_id, "hosted_chat", payload, manifest=manifest)`.
The worker transport critically pushes the immutable plan through the manifest's
existing durable sink before sending SSM. Missing sinks, changed request inputs,
and failed pushes refuse dispatch. The remote script independently checks the
same plan against its fixture and messages. Diagnostic intents are retained
recovery evidence, not resource entries that the generic sweeper deletes.

After losing an instance or its entire SSM reply, recover the plan from
`diagnostic_intents["hosted_chat:" + evaluation_id]`. Reauthenticate the original
fixture, select the recorded tenant, and preview the original start request ID
and message to recover its session ID without dispatch. Never mint another ID.
Read that owned session and reconcile only a recorded attempted original turn; do not dispatch
the second turn merely to perform cleanup. An unknown attempt boundary remains
pending until the original session and Task state establish what happened.
The generic cleanup sweep does not automate these chat reconciliation steps.

On the worker, each original endpoint, body and request ID are also saved in a
private recovery file and emitted in `detail`. A lost receipt replays the same
request at most once and can recover the committed task from owned session
readback. Failure cleanup cancels only retained owned tasks and waits for a
terminal snapshot. Unknown acceptance or pending cleanup remains a failure;
its original request inputs survive in the caller manifest even when no dispatcher output returns.
No passwords, tokens or session vault content enter recovery evidence.

Collect the returned `detail` into the diagnostic result/report before removing
the fixture. Full source code, patch publication and model billing are outside
this chat diagnostic. No inference was performed by its offline regression
suite.
