---
name: stage-1-triage
description: Lightweight fingerprinting of a malware sample — hashes, file type, entropy, strings, candidate IOCs. Use this skill whenever you start a 7-stage malware analysis pipeline, need a quick file identification before deeper analysis, or want to check a sample against known-hash databases before committing compute. This is always the first stage and must run before Stage 2 (OSINT), Stage 3 (static), or Stage 4 (dynamic).
---

# Stage 1 — Triage

You run in the main ADP cluster; sample bytes live in S3. You never download them — the sandboxed triage worker does. Send a message, poll for the response, parse the result.

If a Stage 1 envelope for this artifact already exists in DDB (re-run scenario), read the cached one instead of re-queueing.

## Inputs

| Var | Source | Shape |
|---|---|---|
| `ARTIFACT_ID` | issue body | string, stable across stages |
| `SAMPLE_S3_URI` | issue body | `s3://bucket/key` pointing at raw sample |
| `CYBER_TRIAGE_QUEUE` | env | FIFO queue URL |
| `CYBER_TRIAGE_RESPONSE_QUEUE` | env | FIFO response queue URL |
| `CYBER_RESULTS_TABLE` | env | DDB table name |

## Outputs

Stage envelope (same shape for every stage):

```json
{
  "artifact_id": "<ARTIFACT_ID>",
  "stage": 1,
  "stage_name": "triage",
  "timestamp": "<ISO8601 UTC>",
  "status": "ok | partial | failed",
  "duration_seconds": <int>,
  "findings": {
    "hashes": {"md5": "...", "sha1": "...", "sha256": "..."},
    "file_type": "<from `file` command>",
    "file_type_magika": "<from magika>",
    "file_type_disagreement": true|false,
    "compile_timestamp": "<ISO8601 or null>",
    "signature_status": "valid | invalid | unsigned | unknown",
    "sections": [{"name": "...", "entropy": 7.23, "packed_flag": true}],
    "strings_sample": ["...", "..."],
    "candidate_iocs": {
      "domains": [],
      "ips": [],
      "urls": [],
      "registry_paths": [],
      "mutexes": []
    }
  },
  "tool_calls": <int>,
  "notes": "free text"
}
```

## Steps

### 1. Check DDB for cached result

```bash
aws dynamodb query --region us-east-1 \
  --table-name "$CYBER_RESULTS_TABLE" \
  --key-condition-expression "artifact_id = :a AND begins_with(stage_timestamp, :s)" \
  --expression-attribute-values '{":a":{"S":"'"$ARTIFACT_ID"'"},":s":{"S":"triage#"}}' \
  --query 'Items[0]' --output json
```

If a row exists and is recent (< 7 days), reuse it — skip to step 4.

### 2. Send to triage queue

```bash
MSG_ID=$(aws sqs send-message --region us-east-1 \
  --queue-url "$CYBER_TRIAGE_QUEUE" \
  --message-group-id "$ARTIFACT_ID" \
  --message-deduplication-id "${ARTIFACT_ID}-triage-$(date +%s)" \
  --message-body "{\"artifact_id\":\"$ARTIFACT_ID\",\"sample_s3_uri\":\"$SAMPLE_S3_URI\",\"stage\":\"triage\"}" \
  --query 'MessageId' --output text)
echo "Enqueued triage: $MSG_ID"
```

### 3. Poll the response queue

```bash
# Poll up to 3 minutes (triage is fast)
for i in $(seq 1 36); do
  RESP=$(aws sqs receive-message --region us-east-1 \
    --queue-url "$CYBER_TRIAGE_RESPONSE_QUEUE" \
    --max-number-of-messages 1 \
    --wait-time-seconds 5 \
    --visibility-timeout 10 \
    --query 'Messages[0]' --output json)

  if [ "$RESP" != "null" ] && [ -n "$RESP" ]; then
    BODY=$(echo "$RESP" | jq -r '.Body')
    RECEIPT=$(echo "$RESP" | jq -r '.ReceiptHandle')
    # Check it's for our artifact
    if echo "$BODY" | jq -e --arg a "$ARTIFACT_ID" '.artifact_id == $a' > /dev/null; then
      aws sqs delete-message --region us-east-1 \
        --queue-url "$CYBER_TRIAGE_RESPONSE_QUEUE" \
        --receipt-handle "$RECEIPT"
      echo "$BODY"
      break
    fi
  fi
  sleep 5
done
```

If no response in 3 min, fail the stage with `status: "failed"` + `notes: "triage worker did not respond"`. Don't block the pipeline — move to Stage 2 with the fingerprint fields set to `null`.

### 4. Write envelope to DDB

```bash
TS=$(date -u +%s)
aws dynamodb put-item --region us-east-1 \
  --table-name "$CYBER_RESULTS_TABLE" \
  --item "$(jq -n --arg a "$ARTIFACT_ID" --arg sk "triage#$TS" --argjson findings "$BODY" \
    '{artifact_id:{S:$a}, stage_timestamp:{S:$sk}, stage:{S:"triage"}, status:{S:"ok"}, findings:{S:($findings|tostring)}}')"
```

### 5. Post stage comment to the issue

Use the `issue-comment` skill (or invoke `gh issue comment` directly) with:

```markdown
## Stage 1 — Triage

**Status**: ✅ complete | ⚠️ partial | ❌ failed

| Hash | Value |
|------|-------|
| MD5 | `<md5>` |
| SHA1 | `<sha1>` |
| SHA256 | `<sha256>` |

- **File type (file)**: ...
- **File type (magika)**: ...
- **Disagreement?**: yes/no — _if yes, flag it as a signal_
- **Compile timestamp**: ...
- **Signature**: valid / unsigned / invalid
- **High-entropy sections** (>7.0): ...

**Candidate IOCs** (for Stage 2):
- Domains: ...
- IPs: ...
- URLs: ...

<details><summary>Full JSON</summary>

```json
{...full envelope...}
```
</details>
```

## Failure handling

- Queue send fails → retry 3× with 5s backoff, then fail the stage
- No response in 3 min → mark stage `failed`, proceed to Stage 2 with null fingerprint
- DDB write fails → retry 3×, then write envelope to S3 at `s3://$CYBER_ARTIFACTS_BUCKET/reports/$ARTIFACT_ID/stage-1-triage.json` as fallback
- GH comment fails → retry 3×, then continue (don't block pipeline on comment failures)

## Guardrails

- The sandboxed worker handles sample bytes; your skill only passes an S3 URI over SQS. Keeping bytes out of this pod is what preserves the blast-radius boundary between the main cluster and the Threat Research VPC.
- Publish the stage comment before moving to Stage 2. Users watch these comments stream in as the pipeline runs; a skipped comment looks like a stall.
- Reuse cached envelopes when `artifact_id` was seen recently. Re-queueing wastes worker time and produces duplicate DDB rows that Stage 7's report assembly then has to deduplicate.
