# Claude Agent SDK cyber Tasks

`agent-task-cyber` is a separate Task persona using Claude Agent SDK 0.3.220.
It adapts the existing malware-analysis stages and URL-analysis skill text to
Task invocation. Model requests go through the Python Task host and ADP gateway;
the SDK never receives provider, AWS, GitHub or gateway credentials.

The implementation is feature-gated off by default. It has scripted SDK and
cross-layer integration coverage, but has not been deployed or qualified against
live malware-analysis backends. The existing AI-DLC engine need not run for Task
invocation and should retain its current pause state.

## Submission

Submit through the existing authenticated `POST /v1/tasks` endpoint with an
idempotency key and a service principal authorized for `adp-tasks/submit`:

```json
{
  "schema_version": "1.0",
  "persona": "agent-task-cyber",
  "instructions": "Analyze the supplied sample. Report supported findings and any unavailable analysis stages.",
  "inputs": {
    "sample_s3_uri": "s3://<sample-bucket>/o/<tenant>/t/task-service/u/sp-<canonical-principal>/s/<session>/<sample>/in/sample.bin",
    "sha256": "<64 lowercase hex characters>"
  }
}
```

An authorized upload process must place the sample in that reserved service
principal namespace. This change does not introduce a sample-upload API. Samples
must be versioned, nonempty and at most 64 MiB. The broker pins the first verified
S3 version and digest for all subsequent stages and attempts. `sha256` is optional
for sample submission but, when supplied, must match the bytes.

For URL analysis use `inputs.url` or `inputs.urls`; for hash enrichment supply
`inputs.sha256`. The broker only accepts URLs and sample references supplied with
the Task, and hashes supplied by the caller or established by sample analysis.
Follow-up text does not grant access to a new sample or URL. Private destinations
are rejected at ingress; the configured browser service must additionally enforce
DNS and redirect restrictions.

Results use the existing Task status, event and result endpoints. Reports cite
real Task artifact IDs for backend evidence. Missing stages are uncertainties,
not fabricated findings.

## Operations and controls

| MCP operation | Backend |
| --- | --- |
| `triage`, `static` | Existing cyber SQS worker manifests with versioned sample download |
| `dynamic` | Configured CAPE HTTPS service |
| `result` | Scoped worker result records or CAPE status/report |
| `url_analysis` | Configured internal browser analysis service |
| `enrich` | VirusTotal SHA256 lookup |

Additional tools read packaged skills, publish authored progress, request caller
input and submit a grounded report. Arbitrary shell, filesystem, web and subagent
tools are disabled. Legacy skills' shell/GitHub/AWS delivery instructions are
superseded by these operations; the analytical guidance is retained.

Remote-control epic #3959 was reviewed. The existing `ClaudeControlAdapter`,
`AttemptInputChannel` and `PauseGate` are not imported: they implement a different
human-attributed steering/session contract. The driver reuses the existing Task
host's durable input/cancel path and investigator protocol validators, together
with SDK-native abort and session close. Task input arrives as the result of an
MCP `request_input` call. Public Task pause/resume and unsolicited live steering
are not added by this change.

Cancellation and every other terminal outcome require host-confirmed downstream
cleanup. Cleanup fences new operations, checks earlier attempts' jobs, and does
not equate a lost response with a stopped job. CAPE cancellation is not claimed
as supported: an unresolved CAPE job remains pending until positive terminal
evidence is observed. Unknown submissions are not resent automatically. An
unreconcilable browser/submission outcome requires operator investigation rather
than false completion or queue acknowledgement.

## Enablement requirements

1. Build and deploy compatible gateway and worker images. The worker Dockerfile
   includes the pinned SDK package and the eight packaged cyber skills. Qualify
   the new worker digest under existing Task workload identity controls before
   adding it to `ADP_TASK_WORKER_IMAGE_DIGESTS`.
2. Give the service principal an explicit `agent-task-cyber` model preference and
   enrollment in Task policy `allowed_personas`. Existing Task scope, budget,
   duration and turn limits still apply.
3. Obtain fresh successful model evidence for the separate cyber Messages/tool
   contract in `task_model_binding.py`: persona `agent-task-cyber`, transport
   `anthropic_messages`, revision `task-cyber-sdk-messages-v1`, and the exact
   `TASK_CYBER_REQUEST_SHAPE`. Run `TASK_CYBER_PROBE_BODY` against the selected
   account/region/model and validate the requested tool-use response. A CLI probe
   or investigator text-only probe cannot certify this transport. This change
   defines and enforces that contract; it does not add an operator probe command.
4. Configure the required backends and narrowly scoped gateway IAM access below.
   Verify actual endpoints and worker manifest/result compatibility. Do not assume
   that the legacy browser broker is deployed merely because AgentCore Browser is
   available in the GitHub-bound malware agent.
5. Enable `ADP_TASK_CYBER_ENABLED=true` on the gateway only after qualification.
   Normal admission and cyber operations are gated; stop-only cleanup remains
   available when this flag is disabled.

| Gateway configuration | Required access/use |
| --- | --- |
| `CYBER_SAMPLE_BUCKET` | Read versioned samples in reserved principal prefixes |
| `CYBER_TRIAGE_QUEUE`, `CYBER_STATIC_QUEUE` | Send manifests to configured FIFO queues |
| `CYBER_RESULTS_TABLE` | Read scoped stage result records |
| `CYBER_CAPE_ALB`, `CYBER_CAPE_TOKEN_SECRET` | Reach fixed HTTPS CAPE endpoint and read its token |
| `TASK_CYBER_BROWSER_ENDPOINT` | Reach guarded internal `/v1/analyze` service |
| `CYBER_VT_TOKEN_SECRET` | Read token for fixed VirusTotal hash lookup |

The gateway also uses the existing Task authority table and artifact store.
Missing backend configuration produces an explicit partial result. If a request
may have been sent, the broker records an unknown outcome instead.

The SDK is a trusted orchestration process with loopback networking, a fresh
HOME/cwd, no settings/session persistence and an exact MCP allowlist. It does not
inherit the investigator's OS-enforced no-network boundary. Credentials remain
in the trusted host/gateway; samples execute only in the configured backend.

## Verification evidence

Tests cover actual SDK subprocess/MCP execution against a scripted model, full
Python host-to-SDK report/artifact delivery, unknown-model no-replay, admission
and model contracts, DynamoDB operation races, sample pinning, backend adapters,
cleanup fences and workload authentication. They do not substitute for live
provider, container, browser or CAPE qualification.

On 2026-09-25, the development gateway configuration was inspected read-only in
AWS account `879318057152`. Neither the cyber enablement flag nor the new backend
configuration keys were present in its environment/config maps. No production
readiness or deployed cyber persona is claimed.
