# Embedded Task lifecycle fixture evidence

Date: 2026-09-25. Official Codex SDK: 0.155.1. Node: 24.8.0.
Scope: actual TaskHost subprocess lifecycle, packaged shared Codex entrypoint,
actual SDK binary and Responses bridge; fixture gateway and inference receipts.
No live provider requests, billed operations, story completion or deployment.

The integration test relocates the built application to a temporary `/app`-style
layout, preserving installed SDK dependencies. It cannot resolve shared protocol
code through relative imports into the development checkout. The worker
Dockerfile now contains the same packaged files; a full image build is separate.

| Scenario | Verified behavior |
| --- | --- |
| Valid grounded report | One model operation; artifact publication before finalization; validated process exit before acknowledgement |
| Malformed first output | One correction on a distinct canonical turn, then successful completion |
| Repeated invalid output | Stops after two model operations and fails |
| Cancellation during inference | Cancelled terminal outcome; no successful report |
| Committed amendment during inference | Second canonical turn contains amendment text and its evidence identity |
| Unknown provider outcome | One child model request; fails without child replay |
| Tampered persona instructions | Fails before any model call |

The shared IPC tests also verify correlated current-authority receipts, rejection
of unsolicited input unless enabled, exact replay deduplication, and rejection of
malformed or overlapping turns without partial evidence/turn-state mutation.
Task adapter tests bind persona/model/deadline/budget and reject forged citations.

Reproduce from the harness directory after building investigator and harness:

```sh
python3 test/run-isolated.py -- python3 -m pytest integration/test_task_codex_host.py -q
python3 test/run-isolated.py -- node --test dist/*.test.js test/task-bridge.mjs
```

Use Node >=24 on PATH, or pass `env ADP_CODEX_TEST_NODE=/absolute/node` inside the
isolation wrapper for Python fixtures. Python dependencies: pytest, requests,
rfc8785, boto3. The CI job performs the builds and runs this fixture explicitly.
All test configuration and credential stores are temporary. The previous
session's authentication baseline differed at the first comparison (token file
mtime 21:21:03 UTC); no attribution is made from that comparison. Its baseline
was preserved. A new read-only fingerprint at 21:28 UTC remained unchanged in
subsequent testing; no live login/settings files were rewritten.

Gateway snapshot admission is now implemented and covered by the combined
runtime fixture below. Remaining acceptance work includes executable capability brokers, persona-specific completion policies, OTLP
export/operations, live model qualification, cost/latency/quality evaluation and
real story runs. Structural report grounding does not establish semantic truth.


## Combined gateway and worker runtime

`integration/test_gateway_runtime.py` adds five passing scenarios using actual
TaskAdmission, protected grants, TaskRuntime bootstrap, TaskHost child execution,
the packaged official SDK, TaskTurnStore, TaskModel, DynamoTaskReadStore and
TaskCommands. Moto supplies DynamoDB and S3. The Task client preserves bootstrap,
credential handling and request augmentation; its HTTP delivery is substituted.

| Scenario | Durable assertions |
| --- | --- |
| Success | Report bytes stored; validated exit, terminal state and acknowledgement; admission reservation reconciled |
| Repair | Invalid report corrected using two distinct canonical model turns |
| Amendment | Committed follow-up included in the next model request |
| Cancellation | Cancelled outcome persisted; no accepted report |
| Unknown model outcome | Failed outcome without replay; admission budget hold retained |

These scenarios use fixture inference and budget/usage sinks. They do not qualify
HTTP/IAM/TokenReview delivery, live model readiness, billed inference or cloud
operations. Run separately from `test_task_codex_host.py`: the gateway and worker
fixture helpers both publish a Python `tests` package.

From the repository root, after installing gateway development dependencies in
the dedicated fixture virtual environment and building the runtime:

```sh
python3 modules/agent-factory/codex-harness/test/run-isolated.py -- env \
  AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 \
  ADP_CODEX_TEST_NODE=/absolute/node24 \
  /absolute/fixture-venv/bin/python -m pytest \
  modules/agent-factory/codex-harness/integration/test_gateway_runtime.py -q
```

CI now runs this combined fixture separately. Formatting fixes address the six
files flagged by the gateway CI format gate. Investigator corpus tests explicitly
exercise rejection of the new unsupported Codex process extensions; all 109
investigator tests pass without changing its runtime behavior.

## CI Python loader correction

The combined fixture initially crashed at its first `asyncio.run()` under CI's
Python 3.12.14. The exact `actions/python-versions` Linux 24.04 build reproduces
the crash locally with a minimal `asyncio.sleep(0)` program when the isolation
wrapper removes its loader path. Ubuntu's older libpython has the same SONAME.
Selecting the distributed Python library makes the same program pass.

The wrapper now derives only the running interpreter's own `base_prefix/lib`
when it contains the matching libpython; it does not inherit arbitrary loader
paths or preload settings. Configuration/token isolation remains intact. The
real child-process isolation regression now executes asyncio as well as writing
temporary login stores, and passes under both system and CI Python builds.

All five combined scenarios also pass in a freshly installed fixture environment
using that exact CI Python 3.12.14 distribution after the loader correction.
