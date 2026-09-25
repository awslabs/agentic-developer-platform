# Wave 3 live acceptance collectors

Wave 3 follows accepted Wave 2 and a deployed pause/resume/steer/abort runtime.
All twelve evaluator predicates are implemented. This directory is still being
completed: the PR collector is executable, while the full security and abort
scenario collectors remain outstanding. A passing unit test or populated example
does not establish live acceptance.

## Steered fixture PR (W3-10)

Use an explicitly authorized disposable repository and branch. In the evaluation
config, set `authorized_fixture_repo`, `authorized_fixture_branch`,
`authorized_fixture_base`, `fixture_target_path`, `fixture_expected_content`, and
`fixture_identity`. These are operator choices made before the task; evidence
cannot choose its own repository or expected result.

Before steering, record the original artifact through GitHub:

```sh
python3 platform/scripts/operator/wave3/collect_fixture_pr.py before \
  --config "$EV/fixture-config.json" --out "$EV/pivot-before.json"
```

Steer through the real API or dashboard. Preserve the command ID, SDK handoff and
live comment marker as `steering_delivery`. Review, fix and merge the fixture PR
through the normal authorized process. The collector never performs the merge.
Keep an independently recorded, complete operator interaction audit: its `events`
classify each operation as `setup`, `control`, `pod_exec`, or `github_read`, with a
`phase` of `setup` or `task`. Control events carry `command_id`. The audit requires
`complete: true` and `observed_by` identifying its real source. Missing audit data
is a collection gap; do not reconstruct it by assuming no pod access occurred.

Then collect the real GitHub PR/file readback and run target tests on a temporary
checkout of its merge commit:

```sh
python3 platform/scripts/operator/wave3/collect_fixture_pr.py merged \
  --config "$EV/fixture-config.json" --before "$EV/pivot-before.json" \
  --delivery "$EV/steering-delivery.json" --interaction-audit "$EV/interaction-audit.json" \
  --pr "$FIXTURE_PR_NUMBER" --out "$EV/steer-fixture-pr.json" \
  --junit-relative result.xml --test-command python3 -m pytest tests/test_target.py --junitxml=result.xml
```

The collector checks immutable Git blob bytes, rereads the original revision,
requires an actual changed artifact, and records the target test command and exit
code. A fresh JUnit report must contain at least one passing test and no failures
or errors. Existing reports, missing tests, all-skipped suites, failed commands,
foreign repositories/branches and task-phase pod access are rejected. Test logs
and evidence remain private. The temporary checkout is removed on exit.

Set `artifacts.steer_fixture_pr` to the resulting file. If the PR worker is separate
from the security worker, declare its actual invocation ID as `steer_fixture_run_id`
in the evaluation config. The collector config still names that PR worker as
`fixture_run_id`. This keeps the no-pod-access PR test separate from security
probes that inspect their worker; undeclared or mismatched workers still fail. The evaluator separately
checks its authorized repository, command/handoff linkage, merge identity, file
content/hash, and test revision. Keep the GitHub/fixture cleanup ledger even after
the PR is merged; this command removes only its temporary local checkout.

## All-verb security matrix (W3-05)

The evaluator requires `steer_security_matrix` for the same `fixture_identity` and
both API adapters. Each of pause/resume/steer/abort must be observed for every case:

- `auth_matrix`: anonymous 401; nonowner, other tenant and unknown run 404 with
  indistinguishable response bodies.
- `token_expiry`: expired, missing and wrong listener tokens return 401.
- `malformed_payloads`: malformed JSON and actor/target/token override attempts
  return 400; oversized bodies return 413.
- `terminal_generation`: terminal owner 410; terminal nonowners and other tenants 404.
- `stale_generation`: the previous generation returns 401.

Every request record names adapter, verb, case, unique command ID, invocation ID,
status, observation time and source. `side_effects` must contain observed before
and after `accepted_commands`, `sdk_queries`, and `tool_starts` counters for every
rejected command. Empty or guessed zero counters cannot establish acceptance.
Run this against controlled quiescent fixture work so ordinary concurrent progress
cannot be mistaken for a rejection side effect.

`unsafe_targets` covers unregistered IP, wrong port, metadata, link-local,
loopback, public and redirect destinations on every verb/adapter. Each record
requires an observed block and zero transport attempts to that forbidden target
(the redirect case measures the redirect destination), with invocation/time/source.
Collect this through an isolated transport probe, never by rewriting an ordinary
worker registration. The full executable scenario producer is still outstanding.

The gateway half is executable without replaying successful commands:

```sh
python3 platform/scripts/operator/wave3/collect_security.py capture \
  --config "$EV/fixture-config.json" --out "$EV/security-gateway.json"
```

This sends 96 rejecting requests across both adapters. It never sends a valid
owner command to the live run. The config's `identity_env` names environment
variables containing the owner, nonowner and other-tenant session tokens; token
values are not written to the capture. Set `fixture_run_id` to the same run named
by `fixture_identity.run_id`.

To measure side effects during those requests, configure
`runtime_observer_command` as an argv list invoking `observe_runtime.py --config`
with the same config file. Set `runtime_run_id` to the owned resource label
(`adp.io/w2-fixture`), separate from the invocation UUID in `fixture_run_id`.
Also supply `kubeconfig`, `runtime_namespace`,
`runtime_pod_name`, `runtime_pod_uid`, `runtime_progress_path`, `runtime_revision`
and integer `runtime_generation`. The observer reads the new private registered
fixture progress file and the authenticated gateway journal. It requires the
owned worker to remain running without restarts and paused with zero active work.
UID checks surround each observation; progress reads bracket the gateway read.
No credential is passed in argv or emitted by the observer.

The collector observes before and after every rejection, preserves raw snapshots,
and stops if measurement fails. Incomplete counters, dropped events, foreign
identity and disappearing journal entries are refused. The remaining runtime
capture then supplies side effects only for its token/generation probes; assembly
rejects attempts to overwrite measured gateway side effects. This observer does
not yet produce the listener-token or forbidden-target scenarios.

`listener_probe.py` runs inside the UID-verified fixture worker. Its config names
the exact run, generation, pod IP, port 8770, private lease path and a new private
receipt path. The address must equal the pod's downward-API `POD_IP`. `--case
prepare` first proves the current token works with GET `/agent/ping`, then retains
it only in that private local receipt. Subsequent `missing`, `wrong`, `stale`, and
`expired` cases each perform a positive ping before sending one rejecting verb
request with an explicit command ID. `expired` refuses until the previously
accepted token's actual expiry or bounded renewal-overlap deadline has elapsed.
It never alters the renewal lease and never prints tokens. The receipt is an
owned fixture artifact to remove at teardown.

Observe expiry before lengthy gateway matrices. The probe archives an observed
renewal overlap in a separate private `.rotation.json` beside the original
acceptance receipt, without rewriting that receipt. Subsequent rotations cannot
erase an already observed deadline; a missing historical window is never inferred.
Use an attempt-local cached Kubernetes credential for repeated measurements and
keep its expiry beyond the planned run. Repeated CLI authentication can otherwise
consume the fixture's runtime budget before the expiry window is captured.

These are shared-listener observations, not requests through either gateway API
route. The caller must bracket them with runtime observations and preserve their
source accurately; this helper alone does not establish both-route security or
the forbidden-target matrix.

`probe_transport.py` exercises the deployed shared `ControlService` directly with
synthetic targets, without changing registration rows. Run it inside the owned
gateway pod with its actual `POD_IP` and the expected `control_service.py` SHA256.
It temporarily binds a local HTTP server to that pod's port 8770, exercises all
four verbs against six forbidden destination families, and returns real HTTP 307
redirects for the seventh. A containment transport counts and refuses any attempt
outside the local server, so a regression cannot contact metadata or public IPs.
The server is closed in `finally`. These observations prove shared transport
behavior; the output deliberately does not label them as gateway-route requests
or complete wave acceptance.

After collecting actual listener-token, stale-generation, forbidden-target and
runtime-counter observations for those command IDs, assemble them without
reissuing the gateway requests:

```sh
python3 platform/scripts/operator/wave3/collect_security.py assemble \
  --gateway-capture "$EV/security-gateway.json" \
  --runtime-capture "$EV/security-runtime.json" --out "$EV/steer-security-matrix.json"
```

Assembly refuses foreign fixture identity, missing runtime fields and counters
that do not cover the exact rejected command IDs. The evaluator then checks every
case, status, timestamp and counter. The first capture's exit code only reports
that collection completed; its partial artifact cannot pass W3-05. Automated
listener/transport/runtime observation and abort scenario collection remain to be
completed before this directory can support full Wave 3 live acceptance.

Registered fixture handoff now observes the owned pod immediately after releasing
its collected result. It writes `pod-termination-observations.jsonl` and
`pod-terminal.json` before the Job TTL can remove the pod. Only an observed
terminated `agent-worker` container supplies an exit code. Absence, a replacement
UID, or a terminal phase without container termination details fails collection;
none proves an exit code or an SQS acknowledgement. If observation times out,
continue observing the same pod into a fresh evidence directory rather than
publishing the invocation again.

## Protected acknowledgement receipt

`collect_ack_receipt.py` reads the durable task-delivery tombstone using the
operator's existing AWS credentials. Pass the shared cleanup ledger, observed
worker identity, published dispatch intent, authority table and region. It
performs no task dispatch, SQS receive/delete, or row mutation.

A receipt requires the published SQS message ID, observed pod UID, matching
invocation, a successful AWS DeleteMessage response, one reserved attempt and
zero SDK retries. The gateway retains only the receipt hash and response
metadata after acknowledgement, not the reusable receipt or task body. A crash
before calling SQS can increment the reservation count; multiple reservations
are therefore ambiguous and cannot establish a single clean delete. Older
records without this evidence fail collection rather than receiving defaults.
The receipt alone does not prove the worker's exit or lack of redelivery; those
need the terminal-pod observation and a measured visibility-timeout window.

## Cleanup policy scope

When a run includes temporary isolation canaries as well as later workers, retain
all resources in the creation ledger. An optional `selection_observation` on a
NetworkPolicy entry records its actual Kubernetes object as `{command,
retrieved_at, body}`. Supply the same observation on every Pod and Deployment
entry checked against it. Object kind, name, UID and namespace must match the
ledger; deployment selection uses pod-template labels. The evaluator applies
Kubernetes namespace and label-selector semantics before checking that a selected
workload ended before the policy was removed. Canary policies therefore constrain
the canary pods they selected, including their required deletion order.

Missing policy scope preserves the previous conservative ordering against every
workload. Supplying policy scope without workload observations fails; unknown or
malformed selectors cannot exempt resources. These observations do not establish
that anything was deleted: independent removal receipts and absence checks remain
required for every ledger entry, including canaries removed earlier.


## Focused retry acceptance (W3-11)

For an authenticated disposable registered-control fixture, set
`ADP_CONTROL_RETRY_EVAL=true` on its worker template before launch. The existing
`21-run-registered-control.sh` entrypoint then runs only
`control-retry.integration.ts`; it does not rerun the pause SDK suite. Pin the
source bundle in the protected dispatch and collect `registered-runtime.json`
through the existing handoff collector.

The experiment counts actual SDK inputs across two attempts, records observed
session identities, injects a lost handoff acknowledgement after a real push,
and aborts a second real SDK query during retry backoff. The local journal's
revalidator is controlled by the experiment: these results do not establish
gateway authorization or the W3-05 security matrix. Failed or absent observations
remain failures. The private output contains session IDs and must be redacted
before publishing an acceptance summary.
