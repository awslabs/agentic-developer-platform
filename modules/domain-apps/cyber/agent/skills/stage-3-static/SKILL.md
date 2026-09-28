---
name: stage-3-static
description: Static analysis of a malware sample inside the sandboxed worker — PE/ELF/Mach-O parsing, IAT + suspicious API combinations, embedded resources, YARA scanning against Florian Roth's corpus, anti-analysis detection, family-specific config extraction. Use this skill whenever Stage 1 triage is done and a sample isn't known-benign. Has two modes: rule-driven (default, used Stage 2's focus + YARA rule hints) and agent-authored-script (you write a Python script tailored to Stage 2's hypothesis, the worker runs it in a locked-down subprocess). Always run after Stage 2 unless Stage 2 short-circuited.
---

# Stage 3 — Static analysis

Runs in the sandboxed worker — you never touch sample bytes. The worker image has lief, pefile, yara-python (with Neo23x0/signature-base bundled at `/opt/yara-rules/`), capstone, oletools, magika, iocextract, upx-ucl, osslsigncode, file, strings, binwalk.

Use Stage 2's `recommended_static_focus` + `recommended_yara_rules` to narrow the scan. If Stage 2 has no hypothesis (first time seeing this hash), fall back to a broad default scan.

## Two modes

### Mode A — rule-driven (default)

Send a message with a focus list; the worker runs its built-in checks filtered by those focus areas.

### Mode B — agent-authored script (for specific hypotheses)

When Stage 2's hypothesis is specific (e.g. "Qakbot v5 — extract config block"), author a Python script tailored to the hypothesis, upload to S3, enqueue with `script_s3_uri`. The worker downloads + runs it in a locked-down subprocess + returns structured JSON.

Use Mode B when:
- Hypothesis names a family and you want family-specific extraction
- Generic YARA scan won't produce the evidence you need
- You have a specific structural check in mind (e.g. "decode base64 section X with key derived from mutex name")

## Inputs

| Var | Source | Shape |
|---|---|---|
| Stage 1 envelope | DDB | file_type, sections |
| Stage 2 envelope | DDB | prior_hypothesis, recommended_static_focus, recommended_yara_rules |
| `CYBER_STATIC_QUEUE` / `_RESPONSE_QUEUE` | env | FIFO URLs |
| `CYBER_ARTIFACTS_BUCKET` | env | S3 bucket (for Mode B script upload) |

## Outputs

Stage envelope:

```json
{
  "stage": 3,
  "stage_name": "static",
  "findings": {
    "mode": "rule-driven | agent-authored-script",
    "sections": [{"name":".text", "size": 12345, "entropy": 6.2, "characteristics": "..."}],
    "imports": ["kernel32!VirtualAllocEx", "..."],
    "suspicious_api_combos": [
      {"combo": ["VirtualAllocEx", "WriteProcessMemory", "CreateRemoteThread"], "maps_to": "process_injection"}
    ],
    "embedded_resources": [...],
    "yara_hits": [{"rule": "qakbot_v5", "meta": {"family":"Qakbot"}, "strings_matched": 12}],
    "anti_analysis_signals": ["debugger_check", "sleep_100000"],
    "family_extraction": {
      "config_block": {...},
      "c2_urls": [...]
    },
    "hypothesis_confirmed": true|false|null,
    "script_s3_uri": "s3://... (if Mode B)"
  }
}
```

## Steps

### 1. Decide mode

```text
if Stage 2 prior_hypothesis mentions a specific family AND family_extraction would be valuable:
  → Mode B
else:
  → Mode A
```

### 2a. Mode A — send focus list

```bash
aws sqs send-message --region us-east-1 \
  --queue-url "$CYBER_STATIC_QUEUE" \
  --message-group-id "$ARTIFACT_ID" \
  --message-deduplication-id "${ARTIFACT_ID}-static-$(date +%s)" \
  --message-body "$(jq -n --arg a "$ARTIFACT_ID" --arg s "$SAMPLE_S3_URI" --argjson focus '["config_block_extraction","c2_pattern_confirmation"]' --argjson rules '["qakbot_v5","banking_trojan_generic"]' \
    '{artifact_id:$a, sample_s3_uri:$s, stage:"static", mode:"rule-driven", focus:$focus, yara_rules:$rules}')"
```

### 2b. MANDATORY — Read the worker manifest first

Before writing a Mode B script, pull the manifest for the currently-deployed worker image:

```bash
# The deployed image tag comes from the worker's ScaledJob env or from a known SSM parameter
IMAGE_TAG=$(aws ssm get-parameter --name /adp/dev/cyber/worker-image-tag \
  --query Parameter.Value --output text --region us-east-1)

aws s3 cp "s3://${CYBER_ARTIFACTS_BUCKET}/worker-manifests/by-tag/${IMAGE_TAG}.json" \
  /tmp/worker-manifest.json --region us-east-1

cat /tmp/worker-manifest.json | jq .
```

The manifest tells you:
- Which Python packages you can `import` (exact names + pinned versions)
- Which system binaries you can `subprocess.run()` (exact paths)
- Where YARA rules live, how many are available
- Runtime limits: timeout, memory, CPU, storage, network policy

**Do NOT use any tool not in the manifest.** If your hypothesis needs a tool that isn't there, either fall back to Mode A or open an issue to add the tool to the worker image — do NOT author a script against tools you hope are available.

### 2c. Mode B — author script + upload + send

Author a short Python script. It MUST:
- Read sample path from `sys.argv[1]`
- Write a single JSON line to stdout matching the `findings` schema above
- Exit 0 on success, nonzero on failure
- Not call out to the network (the worker pod has no internet to sandbox env — but stay safe anyway)
- Use only tools listed in the worker manifest (see step 2b above)

Example skeleton:

```python
#!/usr/bin/env python3
"""Stage 3 static script — generated by cyber agent for artifact <ID>.
Hypothesis: Qakbot v5 config-block extraction."""
import json, sys, hashlib, re
import pefile

sample_path = sys.argv[1]
pe = pefile.PE(sample_path)

findings = {
    "mode": "agent-authored-script",
    "sections": [{"name": s.Name.decode().rstrip('\0'), "entropy": s.get_entropy()} for s in pe.sections],
    "imports": [f"{imp.dll.decode()}!{api.name.decode()}" for imp in pe.DIRECTORY_ENTRY_IMPORT for api in imp.imports if api.name],
    # ... family-specific extraction here
}
print(json.dumps(findings))
```

### 2d. Validate the script against the manifest

Before uploading, parse your script's imports and subprocess calls. Fail fast if any aren't in the manifest:

```bash
python3 modules/domain-apps/cyber/agent/skills/stage-3-static/validate_script.py \
  /tmp/stage-3.py /tmp/worker-manifest.json
```

Exit 0 means safe to upload. Nonzero means you referenced something the worker doesn't have — fix the script, don't proceed.

### 2e. Upload + enqueue

```bash
SCRIPT_URI="s3://$CYBER_ARTIFACTS_BUCKET/scripts/$ARTIFACT_ID/stage-3.py"
aws s3 cp /tmp/stage-3.py "$SCRIPT_URI" --region us-east-1

aws sqs send-message --region us-east-1 \
  --queue-url "$CYBER_STATIC_QUEUE" \
  --message-group-id "$ARTIFACT_ID" \
  --message-deduplication-id "${ARTIFACT_ID}-static-$(date +%s)" \
  --message-body "$(jq -n --arg a "$ARTIFACT_ID" --arg s "$SAMPLE_S3_URI" --arg u "$SCRIPT_URI" \
    '{artifact_id:$a, sample_s3_uri:$s, stage:"static", mode:"agent-authored-script", script_s3_uri:$u}')"
```

### 3. Poll response queue (up to 10 min)

Same pattern as Stage 1, but longer wait (static can take minutes on big binaries).

### 4. Interpret findings

Don't just dump — synthesize:
- If `yara_hits` has family rules matching Stage 2's hypothesis → `hypothesis_confirmed: true`
- If combinations like `VirtualAllocEx + WriteProcessMemory + CreateRemoteThread` → flag `process_injection`
- If UPX packer detected → the worker auto-unpacks and re-scans; include both pre/post findings

### 5. Write envelope + publish comment

Comment format:

```markdown
## Stage 3 — Static analysis

**Mode**: rule-driven | agent-authored-script
**Stage 2 hypothesis**: confirmed | revised | rejected

### Structural
- PE / ELF / Mach-O specifics
- Suspicious API combinations: `VirtualAllocEx + WriteProcessMemory + CreateRemoteThread` → **process injection**

### YARA hits (N rules matched)
- `qakbot_v5` (family=Qakbot) — 12 strings matched ← confirms Stage 2 hypothesis

### Family-specific extraction
- Config block: `{decoded_c2: "...", version: "5.1.3"}`

### Anti-analysis signals
- Debugger check at 0x401234
- Sleep 100000 (delay)

<details>Full JSON...</details>
```

## Failure handling

- Worker error → stage status `failed`, post error in comment, continue to Stage 4
- Script timeout (worker caps at 5 min) → mode=agent-authored-script runs get a `status:timeout` envelope; try Mode A as fallback
- Mode B script has a Python error → worker returns the traceback in `notes`; fix script and retry once

## Guardrails

- Leave sample bytes in S3. `aws s3 head-object` is fine for existence checks; `aws s3 cp` into this pod breaks the sandbox boundary that protects the main cluster from malformed-binary exploits in parsers like pefile.
- Mode B scripts run with no network — the sandbox namespace has egress blocked to everything except AWS VPC endpoints. Writing a script that assumes network access is code that works on your laptop and silently produces empty findings in the sandbox.
- Keep Mode B scripts under ~200 lines. A 500-line script doing 5 different extractors means one bug in line 480 throws away findings from line 20. Multiple targeted skills or a second Stage 3 invocation produce better signal than one mega-script.
