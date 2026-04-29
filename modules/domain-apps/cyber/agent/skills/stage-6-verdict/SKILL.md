---
name: stage-6-verdict
description: Synthesize severity (malicious/suspicious/benign), confidence (high/medium/low), STIX 2.1 IOC bundle, CSV feed, and concrete recommended actions (host isolation, token revocation, perimeter blocks, fleet-wide hunt queries) from all prior stage envelopes. Use this skill whenever earlier stages have produced findings (or on a Stage 2 short-circuit to benign) and you need a machine-readable verdict that IR teams can ingest into Splunk/Elastic/Sentinel or block at the perimeter. Always runs — even short-circuits produce a minimal clean-sample verdict. Runs after all other analysis stages; Stage 7 depends on this output.
---

# Stage 6 — Verdict & IOCs

Pure synthesis. No external calls. Takes envelopes from Stages 1-5 (or just 1-2 if Stage 2 short-circuited), produces a machine-readable verdict + IOC bundle that IR teams can ingest.

## Inputs

All prior stage envelopes from DDB. Query:

```bash
aws dynamodb query --region us-east-1 \
  --table-name "$CYBER_RESULTS_TABLE" \
  --key-condition-expression "artifact_id = :a" \
  --expression-attribute-values '{":a":{"S":"'"$ARTIFACT_ID"'"}}' \
  --query 'Items' --output json
```

## Outputs

```json
{
  "stage": 6,
  "stage_name": "verdict",
  "verdict": {
    "severity": "malicious | suspicious | benign",
    "confidence": "high | medium | low",
    "reasoning": "1-2 sentence synthesis",
    "family": "Qakbot v5.x | null",
    "short_circuit": false
  },
  "iocs_stix_2_1": {
    "type": "bundle",
    "id": "bundle--<uuid>",
    "objects": [
      {"type": "file", "hashes": {"SHA-256": "..."}, "name": "..."},
      {"type": "ipv4-addr", "value": "1.2.3.4"},
      {"type": "domain-name", "value": "evil.example"},
      {"type": "indicator", "pattern": "[file:hashes.'SHA-256' = '...']", "valid_from": "..."}
    ]
  },
  "iocs_csv": "indicator_type,value,source,confidence,first_seen\nhash-sha256,abc123,static,high,2026-04-29\n...",
  "recommended_actions": [
    {"action": "host_isolation", "priority": "immediate", "scope": "hosts with matching process-injection telemetry"},
    {"action": "token_revocation", "priority": "immediate", "scope": "credentials accessed by injected explorer.exe"},
    {"action": "perimeter_block", "priority": "high", "iocs": ["1.2.3.4", "evil.example"]},
    {"action": "fleet_hunt", "priority": "high", "query": "hash:<sha256> OR mutex:<mutex>"}
  ]
}
```

## Steps

### 1. Determine severity

Simple rule engine:
- **benign** — Stage 2 short-circuit, OR Stage 3 all-clean + Stage 4 all-clean + signed Microsoft binary
- **malicious** — Any of: ≥3 VT hits + family identified (Stage 2), YARA family rule hit (Stage 3), observed C2 callback to known-bad IP (Stage 4/5), ≥2 MITRE TTPs confirmed (Stage 5)
- **suspicious** — In between: high entropy + no signature + ≥1 IOC match, but no strong malicious signal

### 2. Determine confidence

- **high** — severity = malicious AND ≥3 independent signals corroborate (e.g. VT + YARA + MITRE + MISP)
- **medium** — 2 corroborating signals, OR family identified but no campaign attribution
- **low** — 1 signal only, OR severity = suspicious, OR Stage 4 was skipped/failed

### 3. Build STIX 2.1 bundle

One Python one-liner or short script to emit a valid STIX bundle:

```python
import json, uuid
from datetime import datetime

bundle = {
    "type": "bundle",
    "id": f"bundle--{uuid.uuid4()}",
    "objects": []
}

# File observable
bundle["objects"].append({
    "type": "file",
    "id": f"file--{uuid.uuid4()}",
    "hashes": {"SHA-256": sha256, "SHA-1": sha1, "MD5": md5}
})

# IP observables
for ip in c2_ips:
    bundle["objects"].append({
        "type": "ipv4-addr",
        "id": f"ipv4-addr--{uuid.uuid4()}",
        "value": ip
    })

# Indicators (STIX indicators, pattern-based)
bundle["objects"].append({
    "type": "indicator",
    "id": f"indicator--{uuid.uuid4()}",
    "pattern": f"[file:hashes.'SHA-256' = '{sha256}']",
    "pattern_type": "stix",
    "valid_from": datetime.utcnow().isoformat() + "Z"
})

print(json.dumps(bundle, indent=2))
```

### 4. Build CSV

```csv
indicator_type,value,source,confidence,first_seen,notes
hash-sha256,<sha256>,triage,high,<date>,"Main sample"
hash-sha256,<dropped_hash>,dynamic,high,<date>,"Dropped payload"
ipv4,<c2_ip>,dynamic+misp,high,<date>,"C2 callback + MISP feed"
domain,<c2_domain>,static+dynamic,medium,<date>,"Seen in strings + DNS query"
```

### 5. Generate recommended actions

Tailor to severity:

- **benign** → no actions, short confirmation message
- **suspicious** → 1 action: "quarantine for further review"
- **malicious** → full action set: isolate, revoke tokens, perimeter block, fleet hunt

### 6. Write envelope + publish comment

Comment format (lead with the verdict so on-call readers see it first):

```markdown
## Stage 6 — Verdict

# 🔴 MALICIOUS — high confidence

**Family**: Qakbot v5.x
**Reasoning**: Confirmed across 4 signals: VT (42/73, family match), YARA (qakbot_v5 rule), dynamic C2 callback (1.2.3.4 matches MISP feed), MITRE T1055.002 + T1071.001 confirmed.

### Immediate actions
1. **Host isolation** — any host with process-injection telemetry matching `VirtualAllocEx→explorer.exe`
2. **Token revocation** — credentials cached by explorer.exe on suspect hosts
3. **Perimeter block** — `1.2.3.4`, `evil.example` (both directions)
4. **Fleet hunt** — Splunk query in Stage 7 report

<details><summary>IOCs (STIX 2.1)</summary>

```json
{STIX bundle...}
```
</details>

<details><summary>IOCs (CSV)</summary>

```csv
indicator_type,value,source,confidence,first_seen
...
```
</details>
```

## Failure handling

- Missing prior stages → work with what's there. Minimum to produce a verdict: Stage 1 + Stage 2.
- If only Stage 1 ran (Stage 2 failed completely + no OSINT) → verdict = `suspicious, low confidence` with a note that it needs a human review.

## Guardrails

- Lead with severity + confidence in the first line of the comment. On-call readers scan for the answer; burying it three paragraphs in costs minutes per sample across a shift.
- Tag every IOC with its source stage. When an IR analyst blocks an IP and later asks "how confident are we this is actually bad?" the answer needs to be "Stage 4 observed the callback plus Stage 5 matched the MISP feed" — not a stripped list of numbers.
- Use real STIX 2.1 object types (`file`, `ipv4-addr`, `ipv6-addr`, `domain-name`, `url`, `mutex`, `process`, `indicator`, `malware`). Invented types break downstream STIX consumers silently and look like a parser bug to the receiver.
- Recommended actions must be executable. "Investigate further" is not a recommendation; "Block 1.2.3.4 + evil.example at the perimeter, revoke tokens cached by explorer.exe on hosts with matching injection telemetry" is.
