---
name: stage-3-static
description: Static analysis of a malware sample inside the sandboxed worker — PE/ELF/Mach-O parsing, IAT + suspicious API combinations, embedded resources, YARA scanning against Florian Roth's corpus, anti-analysis detection, family-specific config extraction. Use this skill whenever Stage 1 triage is done and a sample isn't known-benign. Has two modes: rule-driven (default, used Stage 2's focus + YARA rule hints) and agent-authored-script (you write a Python script tailored to Stage 2's hypothesis, the worker runs it in a locked-down subprocess). Always run after Stage 2 unless Stage 2 short-circuited.
---

# Stage 3 — Static

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
  --artifact-id "$ARTIFACT_ID" --sample-uri "$SAMPLE_S3_URI" --stage static)
JOB_ID=$(printf '%s' "$JOB" | jq -er '.job_id')
python3 "$CYBER_JOB_CLIENT" wait --job-id "$JOB_ID" > /tmp/static-result.json
```

A result with `status: failed` is a failed stage, never evidence of an empty or
benign sample. Report refusal or timeout explicitly. Re-register an expired job
only while the original authenticated workflow is still live. Results can only
be retrieved by the same workflow run and attempt that registered the job.

Only versioned samples at the authenticated human's canonical chat upload path
are accepted. A URI copied from another tenant, team or user is rejected before
a storage read. A reusable service workflow cannot borrow its registrant's
private samples; use a human-triggered registered malware workflow for these.

Mode A is the default. Add repeated `--focus` and `--yara-rule` options to narrow
the rule-driven analysis. The worker runs YARA, binary parsing and string
extraction in the same kernel confinement used for scripts.

For Mode B, author a Python script locally. Read the sample filename from
`sys.argv[1]`, use the installed analysis libraries described by the worker
manifest, and print one JSON object. Pass `--script /tmp/stage-3.py` to `submit`.
The client uploads those bytes to the broker. The broker records their digest;
the worker checks integrity and its installed-tool validator before executing.
Do not upload a script to S3 or enqueue `script_s3_uri` jobs.

The runtime permits only the staged sample/script, immutable tools/rules and a
private scratch directory. Network access, tokens, other jobs, process inspection
and writes to application code are denied by the kernel, including in child
tools. Scripts have a 300-second deadline and a 1-MiB output limit. AST validation
is a compatibility check; it is not the security boundary.
