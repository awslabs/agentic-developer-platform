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

Remaining acceptance work includes authoritative gateway snapshot admission,
executable capability brokers, persona-specific completion policies, OTLP
export/operations, live model qualification, cost/latency/quality evaluation and
real story runs. Structural report grounding does not establish semantic truth.
