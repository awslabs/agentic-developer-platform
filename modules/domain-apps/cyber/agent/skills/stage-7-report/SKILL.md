---
name: stage-7-report
description: Produce the final deliverables — executive summary (for leadership), structured technical report (for IR), STIX 2.1 + CSV IOC feeds (for SIEM/firewall), copy-pasteable Splunk/Elastic/Sentinel hunt queries — upload them all to S3 with presigned URLs, and post a single consolidated comment linking everything. Use this skill whenever the pipeline has reached a verdict (Stage 6 done) and the issue needs a handoff-ready set of artifacts for IR follow-up. Always the last stage; runs on benign short-circuits too (produces a concise clean-sample report).
---

# Stage 7 — Report

Final stage. Produces artifacts, uploads them, posts the links. No external calls except S3.

## Inputs

All prior stage envelopes from DDB + Stage 6 verdict.

## Outputs

Set of files at `s3://$CYBER_ARTIFACTS_BUCKET/reports/$ARTIFACT_ID/`:

- `executive_summary.md` — 3-5 sentences for non-technical readers
- `technical_report.md` — full structured report
- `iocs.stix.json` — STIX 2.1 bundle
- `iocs.csv` — CSV feed
- `timeline.json` — Stage 4 timeline, if available
- `hunt_queries.splunk.txt`
- `hunt_queries.elastic.txt`
- `hunt_queries.sentinel.txt`
- `pipeline_envelope.json` — all 7 stage envelopes merged (for automation consumers)

DDB final row with `status: "reported"` + `artifact_uris` list.

## Steps

### 1. Assemble the artifacts (all local temp)

```bash
WORKDIR=/tmp/report-$ARTIFACT_ID
mkdir -p $WORKDIR
```

**executive_summary.md** — you write this. Non-technical. Example:

```markdown
# Analysis of <filename> (<sha256_short>) — 2026-04-29

**Verdict**: Confirmed Qakbot v5.x banking trojan (high confidence).

The sample is an active malware loader that attempts to inject into
`explorer.exe` and contact a known command-and-control server
(`1.2.3.4`). On a compromised host, it would give the attacker the
ability to steal browser credentials and install additional payloads.
Recommend immediate host isolation and perimeter block of the C2
infrastructure listed in the IOC feed.
```

**technical_report.md** — structured dump, one section per stage:

```markdown
# Technical Report — <artifact_id>

## Metadata
- Sample SHA256: ...
- File type: ...
- Analysis date: 2026-04-29T...
- Pipeline run ID: ...

## Stage 1 — Triage
(quote Stage 1 envelope findings in human-readable form)

## Stage 2 — OSINT recon
...

## Stage 3 — Static analysis
...

(etc., through Stage 6)

## Appendix A — IOCs (see iocs.csv + iocs.stix.json for machine formats)
| Type | Value | Source | Confidence |
|---|---|---|---|

## Appendix B — MITRE ATT&CK coverage
| Technique | ID | Evidence |
|---|---|---|
```

**iocs.stix.json** — copy Stage 6's STIX bundle verbatim.

**iocs.csv** — copy Stage 6's CSV verbatim.

**timeline.json** — Stage 4's event timeline if it exists, else empty `[]`.

**hunt_queries.splunk.txt** — use the template:

```
index=* (hash=<sha256> OR hash=<md5>)
index=* dest_ip IN (1.2.3.4)
index=sysmon EventCode=10 TargetImage="*explorer.exe" GrantedAccess="0x1FFFFF"
index=sysmon EventCode=13 TargetObject="*\\CurrentVersion\\Run\\*" Image="<process>"
```

**hunt_queries.elastic.txt**:

```
hash:("<sha256>" OR "<md5>") OR destination.ip:"1.2.3.4"
process.name:explorer.exe AND event.code:10 AND winlog.event_data.GrantedAccess:"0x1FFFFF"
```

**hunt_queries.sentinel.txt** (KQL):

```kusto
DeviceFileEvents | where SHA256 == "..."
DeviceNetworkEvents | where RemoteIP == "1.2.3.4"
DeviceProcessEvents | where InitiatingProcessFileName == "explorer.exe"
```

**pipeline_envelope.json** — merge all DDB rows for this artifact_id:

```bash
aws dynamodb query --region us-east-1 \
  --table-name "$CYBER_RESULTS_TABLE" \
  --key-condition-expression "artifact_id = :a" \
  --expression-attribute-values '{":a":{"S":"'"$ARTIFACT_ID"'"}}' \
  --query 'Items' --output json > $WORKDIR/pipeline_envelope.json
```

### 2. Upload everything to S3

```bash
S3_PREFIX="s3://$CYBER_ARTIFACTS_BUCKET/reports/$ARTIFACT_ID"
aws s3 sync $WORKDIR/ "$S3_PREFIX/" --region us-east-1
```

### 3. Generate presigned URLs (7-day expiry)

```bash
for f in $WORKDIR/*; do
  NAME=$(basename "$f")
  URL=$(aws s3 presign "$S3_PREFIX/$NAME" --expires-in 604800 --region us-east-1)
  echo "$NAME → $URL"
done
```

### 4. Write final DDB envelope

```bash
aws dynamodb put-item --region us-east-1 \
  --table-name "$CYBER_RESULTS_TABLE" \
  --item "$(jq -n --arg a "$ARTIFACT_ID" --arg sk "report#$(date -u +%s)" --arg s3 "$S3_PREFIX" \
    '{artifact_id:{S:$a}, stage_timestamp:{S:$sk}, stage:{S:"report"}, status:{S:"reported"}, s3_prefix:{S:$s3}}')"
```

### 5. Publish final comment

```markdown
## Stage 7 — Report 📋

**Analysis complete.** Total pipeline wall time: 23m 14s | Model: claude-opus-4-6 | Tool calls: 42

### Executive summary
> <copy executive_summary.md content inline — 3-5 sentences>

### Verdict
🔴 **MALICIOUS** — high confidence — Qakbot v5.x

### Artifacts
- [Executive summary](<presigned>) — 1 KB, 3-5 sentences
- [Technical report](<presigned>) — full structured report
- [IOCs STIX 2.1](<presigned>) — machine-readable feed
- [IOCs CSV](<presigned>) — spreadsheet-friendly
- [Timeline JSON](<presigned>) — event sequence from CAPE
- [Splunk hunt queries](<presigned>)
- [Elastic hunt queries](<presigned>)
- [Sentinel KQL queries](<presigned>)
- [Full pipeline envelope](<presigned>) — all 7 stage outputs merged

All links expire in 7 days.

### Stage-by-stage comments
- [Stage 1 — Triage](#)
- [Stage 2 — OSINT recon](#)
- [Stage 3 — Static analysis](#)
- [Stage 4 — Dynamic analysis](#)
- [Stage 5 — Correlation](#)
- [Stage 6 — Verdict](#)

Pipeline complete. Close the issue when findings have been acted on.
```

## Failure handling

- S3 upload fails → retry each file individually; if persistent, fail the stage and post the files inline in the GH comment as fenced blocks (degraded delivery)
- Presigned URL generation fails → post S3 URIs without presigned (`s3://...`) with a note that the user needs AWS access to download
- DDB write fails → retry 3×, then continue (the artifacts are in S3, that's what matters)

## Guardrails

- Link every artifact in the comment. A user who sees "executive_summary.md generated" but no link has to dig through S3 themselves — and will ask "is this even done?" in a follow-up comment. Link them all.
- Treat prior runs as immutable. If `artifact_id` is re-analyzed, write under `/reports/<artifact_id>/<timestamp>/` rather than overwriting. Audit trail and chain-of-custody depend on historical reports staying reproducible.
- Keep the executive summary to 5 sentences. Leadership reads it on a phone; any more and they ask their team to summarize the summary.
- Fill hunt queries with actual IOCs before writing the files. A Splunk query with `hash=<sha256>` left as a placeholder is net-negative — it gets pasted into production search and returns zero, which looks like "nothing matches" rather than a broken query.
