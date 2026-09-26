# Two-turn hosted chat diagnostic

The shipped dispatcher purpose `hosted_chat` is an explicit paid diagnostic.
It does not replace or upgrade the read-only E40 readiness/history case, and
neither success nor cleanup claims shared-spend reconciliation.

Run it only on the harness-owned installed EC2 fixture using the existing
`_run_worker`/SSM dispatcher transport. Supply the ordinary journey payload
(`cli_path`, `work_dir`, gateway/account/instance binding, `test_user_id`, and
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
requests have stable IDs, bounded replay, exact task-correlated completion and
one user/assistant history pair each. It refuses to start turn two if turn one
is uncertain or needs clarification. It does not guess an answer to a pending
question or generate replacement request IDs.

Before each dispatch the original endpoint, body and request ID are saved in a
private recovery file and emitted in `detail`. A lost receipt replays the same
request at most once and can recover the committed task from owned session
readback. Failure cleanup cancels only retained owned tasks and waits for a
terminal snapshot. Unknown acceptance or pending cleanup remains a failure;
its exact request data survives in dispatcher evidence even if EC2 is removed.
No passwords, tokens or session vault content enter recovery evidence.

Collect the returned `detail` into the diagnostic result/report before removing
the fixture. Full source code, patch publication and model billing are outside
this chat diagnostic. No inference was performed by its offline regression
suite.
