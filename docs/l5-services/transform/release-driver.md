# Transform release driver

The shared release calls `qualify-transform-release.py` and
`emit-transform-qualification.py`. Private fixtures select either retained
`runs` or a `bootstrap` list. Both forms require the exact `source_sha` and may
provide the existing protected `runtime` settings.

Each bootstrap entry selects `scope` (`tenant_id`, `product_id`, `project_id`),
the complete typed Transform `input`, a stable `idempotency_key`, and
`max_wait_seconds` between 1 and 1800. One or two requests are supported. Select
the approved refactor and dependency-upgrade fixtures with their own immutable
source revisions and request keys. The driver uses a dedicated authenticated
human fixture identity; it does not obtain a personal session or create users.

The configuration and binding must already be ready. The driver submits through
the Product API, where existing qualification, source, delegation and budget
checks still apply. It does not enable the service or call the provider directly.
It retains a private journal before submission, including the selected source,
request, authenticated actor and original polling deadline. A lost acknowledgment
replays the same actor, key and body through Product idempotency. An acknowledged
run is only read on restart. Changing identity, source or request is refused;
restarting does not replenish the polling allowance.

The driver selects the result belonging to that exact run and passes it to the
existing independent source, native-patch and detached-test evaluator. It never
selects an unrelated latest result. The emitter can read the retained selection
but cannot submit an inquiry. Preserve the evidence directory between phases and
restarts; it must remain private and outside the checkout.

For an unknown submission outcome, reconcile the original Product request key.
For an acknowledged failed or blocked run, inspect that run's blocker and repair
its existing work. Result collection failures do not require native resubmission.
An exhausted polling allowance leaves the original run intact for observation.

The [acceptance harness](acceptance-harness.md) binds these observations to the
exact release attempt and original criterion catalog. With installed selectors,
it also reads the pinned installation and verifies retained patch/test bytes
after receipt-owned scratch resources are absent. Original release acceptance
still requires scope and isolation denials, real recovery traces, client
journeys, later-task reuse, adopted-resource/SCM preservation and portability.
Every missing original criterion remains explicit in the emitted report.
