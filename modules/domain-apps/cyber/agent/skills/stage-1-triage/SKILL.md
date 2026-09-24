---
name: stage-1-triage
description: Lightweight fingerprinting of a malware sample — hashes, file type, entropy, strings, candidate IOCs. Use this skill whenever you start a 7-stage malware analysis pipeline, need a quick file identification before deeper analysis, or want to check a sample against known-hash databases before committing compute. This is always the first stage and must run before Stage 2 (OSINT), Stage 3 (static), or Stage 4 (dynamic).
---

# Stage 1 — Triage

Use the authenticated job client. The gateway verifies the GitHub workflow and
runner identity, resolves the human's current tenant/team membership, and
registers a read of exactly one immutable version of their uploaded sample.
Sample bytes stay in the isolated analysis plane.

Required inputs: `ARTIFACT_ID`, `SAMPLE_S3_URI`, `CYBER_JOB_CLIENT` (installed by
the workflow), and `ADP_AGENT_CONTROL_ENDPOINT`. GitHub OIDC and temporary AWS
runner credentials are provided by the workflow. Never supply org, team, user,
script digest, S3 download capability, or a queue URL as authority.

```bash
JOB=$(python3 "$CYBER_JOB_CLIENT" submit \
  --artifact-id "$ARTIFACT_ID" --sample-uri "$SAMPLE_S3_URI" --stage triage)
JOB_ID=$(printf '%s' "$JOB" | jq -er '.job_id')
python3 "$CYBER_JOB_CLIENT" wait --job-id "$JOB_ID" > /tmp/triage-result.json
```

A result with `status: failed` is a failed stage, never evidence of an empty or
benign sample. Report refusal or timeout explicitly. Re-register an expired job
only while the original authenticated workflow is still live. Results can only
be retrieved by the same workflow run and attempt that registered the job.

Only versioned samples at the authenticated human's canonical chat upload path
are accepted. A URI copied from another tenant, team or user is rejected before
a storage read. A reusable service workflow cannot borrow its registrant's
private samples; use a human-triggered registered malware workflow for these.

Consume the fingerprint's hashes, file type, sections, strings and candidate IOCs
before Stage 2. Do not use direct DynamoDB reads or shared response-queue polling
for cached results; those interfaces do not authorize a caller's ownership.
