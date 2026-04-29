---
name: stage-4-dynamic
description: Detonate the sample in CAPE's instrumented Windows VM and retrieve the behavioral report — file/registry events, network callbacks, process injection chains, dropped payloads, API trace, screenshots, timeline. Use this skill whenever static analysis alone isn't enough and you need to see what the sample actually does at runtime — C2 callbacks, persistence mechanisms, dropped payloads, injection targets. Run after Stage 3 unless Stage 2 short-circuited, and skip if Stage 3 conclusively proved the sample is benign (all YARA clean, valid Microsoft Authenticode signature). Pre-M4 deployments where the Windows qcow2 isn't wired into CAPE yet: this will fail with a note, which is expected.
---

# Stage 4 — Dynamic sandbox (CAPE)

Submits the sample to CAPE running on its isolated EC2 host. CAPE executes the sample in a Windows VM on the sandbox network (no internet — INetSim simulates callbacks) and returns a structured behavioral report.

## Inputs

| Var | Source | Shape |
|---|---|---|
| `SAMPLE_S3_URI` | Stage 1 | S3 pointer |
| `CYBER_CAPE_ALB` | env | `https://internal-adp-dev-cyber-alb-....elb.amazonaws.com` |
| `CYBER_CAPE_TOKEN_SECRET` | env | `adp/cape/api-token` in Secrets Manager |
| `CYBER_STAGE4_TIMEOUT_SECONDS` | env | default 1800 (30 min) |

## Outputs

```json
{
  "stage": 4,
  "stage_name": "dynamic",
  "findings": {
    "cape_task_id": 12345,
    "file_events": [{"action":"write", "path":"..."}],
    "registry_events": [{"action":"set", "key":"...", "value":"..."}],
    "network": {
      "dns": [...],
      "http": [...],
      "c2_callbacks": [{"host":"...", "port":443, "protocol":"tls"}]
    },
    "processes": {
      "spawned": [...],
      "injected_into": [...],
      "killed": [...]
    },
    "api_trace_summary": {"total_calls": 12345, "notable": [...]},
    "memory_dumps": [{"pid": 1234, "s3_ref": "..."}],
    "dropped_payloads": [{"hash":"...", "s3_ref":"...", "size":12345}],
    "screenshots": ["s3://..."],
    "sandbox_report_s3": "s3://.../reports/<artifact>/cape-full.json",
    "timeline": [...]
  }
}
```

## Steps

### 1. Get CAPE API token from Secrets Manager

```bash
CAPE_TOKEN=$(aws secretsmanager get-secret-value --region us-east-1 \
  --secret-id "$CYBER_CAPE_TOKEN_SECRET" \
  --query SecretString --output text)
```

### 2. Download sample to a temp location (the ALB accepts file uploads, not S3 URIs)

Since we must not hold sample bytes in this pod, **this is one of the only places where we briefly touch the sample**. The download is straight to a stream piped into the curl upload — the bytes never land on disk.

```bash
# Stream from S3 → curl multipart → CAPE
aws s3 cp "$SAMPLE_S3_URI" - --region us-east-1 | \
  curl -sSk -H "Authorization: Bearer $CAPE_TOKEN" \
    -F "file=@-;filename=sample.bin" \
    -F "package=exe" \
    -F "timeout=300" \
    "$CYBER_CAPE_ALB/apiv2/tasks/create/file/" | tee /tmp/cape-submit.json

TASK_ID=$(jq -r '.data.task_id // .task_id' /tmp/cape-submit.json)
[ -n "$TASK_ID" ] || { echo "submit failed"; exit 1; }
echo "CAPE task: $TASK_ID"
```

**Alternative if streaming fails** (e.g. large file buffer issues): download to tmpfs (RAM-backed, never hits disk), upload, immediately `rm`:

```bash
mount -t tmpfs -o size=100M tmpfs /mnt/ramdisk 2>/dev/null || true
aws s3 cp "$SAMPLE_S3_URI" /mnt/ramdisk/sample.bin --region us-east-1
curl -sSk -H "Authorization: Bearer $CAPE_TOKEN" \
  -F "file=@/mnt/ramdisk/sample.bin" \
  -F "package=exe" -F "timeout=300" \
  "$CYBER_CAPE_ALB/apiv2/tasks/create/file/"
rm -f /mnt/ramdisk/sample.bin
```

### 3. Poll status

```bash
START=$(date +%s)
TIMEOUT="${CYBER_STAGE4_TIMEOUT_SECONDS:-1800}"

while true; do
  STATUS=$(curl -sSk -H "Authorization: Bearer $CAPE_TOKEN" \
    "$CYBER_CAPE_ALB/apiv2/tasks/status/$TASK_ID/" | jq -r '.data.status')
  echo "CAPE $TASK_ID: $STATUS"

  case "$STATUS" in
    reported|completed) break ;;
    failed|error) echo "CAPE task failed"; break ;;
  esac

  ELAPSED=$(( $(date +%s) - START ))
  if [ "$ELAPSED" -gt "$TIMEOUT" ]; then
    echo "CAPE timed out after $TIMEOUT seconds"
    break
  fi

  sleep 20
done
```

### 4. Fetch report

```bash
curl -sSk -H "Authorization: Bearer $CAPE_TOKEN" \
  "$CYBER_CAPE_ALB/apiv2/tasks/report/$TASK_ID/" > /tmp/cape-report.json

# Upload full report to artifacts bucket for long-term storage
aws s3 cp /tmp/cape-report.json \
  "s3://$CYBER_ARTIFACTS_BUCKET/reports/$ARTIFACT_ID/cape-full.json" \
  --region us-east-1

# Extract just what we need for the DDB envelope
jq '{
  file_events: .behavior.summary.files,
  registry_events: .behavior.summary.keys,
  network: {dns: .network.dns, http: .network.http, c2_callbacks: .network.tcp},
  processes: .behavior.processes | map({pid, process_name, parent_id}),
  dropped_payloads: .dropped | map({sha256, type, size}),
  screenshots: .screenshots | map(.path)
}' /tmp/cape-report.json
```

### 5. Write envelope + publish stage comment

Comment format:

```markdown
## Stage 4 — Dynamic analysis (CAPE)

**Task**: #12345
**Status**: reported | timeout | failed
**Duration**: 18m 42s

### Observed behaviors
- **Files**: 23 created, 4 modified, 1 deleted (key: `%APPDATA%\<random>\loader.exe`)
- **Registry persistence**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\<random>`
- **Network**: 3 DNS queries, 2 HTTPS callbacks to `1.2.3.4:443` (C2 confirmed)
- **Processes**: injected into `explorer.exe` (PID 1234)
- **Dropped**: 1 new PE at `%TEMP%\<hash>.exe`

### Notable API trace
- `VirtualAllocEx` → `WriteProcessMemory` → `CreateRemoteThread` on explorer.exe (process injection confirmed)

### Screenshots
- [Sample running at T+30s](...)
- [T+120s — after injection](...)

### Full CAPE report
[`cape-full.json`](s3://...)

<details>Full JSON envelope...</details>
```

## Failure handling

- CAPE unreachable (network error, ALB 5xx) → status `failed`, post in comment, continue to Stage 5 with empty dynamic findings
- Task `failed` in CAPE → fetch whatever partial report exists, status `partial`
- Timeout (>30 min) → status `partial`, fetch what's available, note in `notes`
- Sample too big (CAPE has 100 MB limit by default) → status `failed`, note the size issue

## Guardrails

- Sample bytes through this pod are transient. The stream-pipe `aws s3 cp - | curl` pattern keeps bytes in memory buffers, not on disk. If tmpfs is needed, `rm` immediately after upload — a sample lingering in the main cluster's filesystem would break the blast-radius boundary.
- 30-min hard cap matches the spread of real sandbox runs (5-15 min typical, 30 min for sleep-delay evaders). Past 30 min, the marginal data isn't worth blocking the pipeline or tying up a CAPE task slot that the next analysis could use.
- Full CAPE reports go to S3, not DDB. A full CAPE JSON can be 10+ MB; DDB item size limit is 400 KB and the item attribute retrieval costs scale with size.
- Pre-#228 deployments (Windows qcow2 not yet wired into CAPE) return 5xx from `/tasks/create/file/`. Record the failure in the envelope with `notes: "CAPE not yet wired to analysis VM — M4 deliverable"` and continue to Stage 5 with empty dynamic data. Static + OSINT alone can still produce a useful verdict.
