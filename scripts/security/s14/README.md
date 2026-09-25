# S14 shared-runner acceptance

This is a staged, manual acceptance job for #5613. Merging this PR does not
change IAM or dispatch it. Run only after the separately reviewed shared-role
cutover, current prerequisites and operational compatibility review are accepted.
The broader S13 audit and deployment identity work remain separate closure items.

The workflow has no user inputs. It runs on `arc-runner-org`, rejects a non-main
manual ref and any repeated Actions run attempt, and does not switch AWS identity.
The script requires the actual web-identity credential provider and exact shared
STS role/account. EC2 admin and the dedicated developer role cannot satisfy it.
The config checksum is pinned in the script; resource or acceptance-key changes
require a reviewed code change. The smoke archive is pinned independently to
`c6f20b35491541a99bc48e83b8e801296ffc645f`, which contains the reviewed
`codebuild/bs-gateway-smoke.yml`. Only committed bytes from that SHA are archived.

The bounded probes read the exact endpoint parameter, six existing App transport
secrets in memory, an existing ECR manifest and at most 1 KiB of a layer, and the
signed platform config route. The route check proves transport, not tenant
isolation. They write one harmless own-log marker and invoke the existing Haiku
profile with `max_tokens=1`. Three synthetic foreign-resource reads pass only
on AccessDenied; resource-not-found and throttling do not pass. No customer object,
agent startup, App token exchange, deployment, IAM mutation or secret output is
part of the job. Negative resource names are deliberately nonexistent fixtures.

The exact existing nonpublishing gateway PR project/role/buildspec are fixed. The
source archive upload must return a non-null S3 VersionId; StartBuild pins that
version so later overwrites of a key cannot change the bytes. The receipt stores
the archive hash and reviewed Git SHA. Polling verifies build ID, role, project,
source location, buildspec, object version and exact relevant environment
overrides. Only a successful build with that contract and all runtime checks
can yield `complete=true`. Failed, stopped, timed-out and ambiguous runs never
count as acceptance.

## Durable intent and reconciliation

The full receipt is stored at the fixed, reviewed `receipt_key` in the existing
PR source prefix. It contains metadata and booleans, no credentials or secret
values. The shared policy already allows GetObject/PutObject in that prefix;
no receipt-specific grant is added. The script needs a boto3/botocore version
supporting S3 conditional PutObject (`IfMatch` and `IfNoneMatch`), and the source
bucket must retain object versions. Failure of those prerequisites stops it.

A local fsynced receipt and conditional remote journal are committed **before**
the model call, log event or StartBuild. An uncertain journal write stops further
writes; the error handler cannot replay it. Conditional ETags reject competing
updates. A lost runner cannot replace the fixed remote journal, even from a new
Actions run. Existing model/log/build-start intents never replay automatically.
StartBuild retains the exact request, token, timestamp and accepted handle for
operator reconciliation; the SDK makes one attempt. Polling uses only the saved
build handle. There is no `--reset`, `--resume`, `--force`, new-key input, or
automatic recovery workflow. Review first; do not delete a receipt or edit its
intent flags to manufacture a new attempt.

An operator reconciling an interrupted run reads the private journal, inspects
its recorded log stream/source version/build ID and relevant service status,
and records the disposition. If StartBuild returned ambiguously without a saved
ID, reconcile the original token/request and service evidence; do not submit a
new request or invent a new token. A separate reviewed continuation is needed
for any further execution. The draft does not promise to recover an unknown
model response or infer its success.

Only `summary.json` is uploaded as an Actions artifact, with 14-day retention.
The full journal (including the non-secret CodeBuild idempotency token) and source
archive are not uploaded to Actions or committed. The operator owns cleanup of
the exact source object/version and harmless test log stream after the build is
terminal and review is complete. Preserve the journal as the acceptance/replay
record; do not delete it to rerun the fixed acceptance. No broad delete grant or
automatic source deletion is introduced.

## Offline checks

```bash
python3 -m unittest discover -s scripts/security/s14 -p 'test_*.py' -v
python3 scripts/security/s14/diagnostic.py --config scripts/security/s14/config.json --receipt /tmp/unused-s14.json --stage runtime
```

Tests mock AWS and prohibit network/DNS calls. They cover uncertain remote writes,
competing journals, lost pods, model/build replay refusal, wrong-role rejection,
secret output exclusion, exact source packaging/version/build acceptance, and
negative authorization outcomes. The PR workflow runs only these offline tests;
live acceptance requires a manual dispatch from main after review.
