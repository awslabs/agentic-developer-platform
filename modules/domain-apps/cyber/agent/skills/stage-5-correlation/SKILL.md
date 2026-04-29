---
name: stage-5-correlation
description: Cross-reference dynamic findings against threat intel (MISP, VT, web reporting) + MITRE ATT&CK to derive family + TTPs + campaign linkage. Use this skill whenever you have Stage 4 dynamic observations (C2 IPs, mutexes, injection targets) and want to link them to known campaigns, confirm Stage 2's hypothesis, or produce MITRE TTP IDs with specific per-stage evidence. Runs after Stage 4, or after Stage 3 if Stage 4 was unavailable — can work with partial data. Skip on Stage 2 short-circuit.
---

# Stage 5 — Correlation & attribution

Runs in the main cluster. Reuses the OSINT query patterns from Stage 2, but feeds them **dynamic-behavior observations** (actual C2 IPs seen, mutexes created, processes injected) rather than just static fingerprints.

If Stage 4 failed entirely, run Stage 5 with only static + OSINT data — attribution is weaker but still useful.

## Inputs

| Var | Source |
|---|---|
| Stages 1-4 envelopes | DDB |
| OSINT credentials | Secrets Manager (same prefixes as Stage 2) |

## Outputs

```json
{
  "stage": 5,
  "stage_name": "correlation",
  "findings": {
    "c2_matches": [
      {"ioc": "1.2.3.4", "source": "MISP", "feed": "c2-tracker", "confidence": "high", "first_observed": "2026-03-01"}
    ],
    "mitre_ttps_confirmed": [
      {"id": "T1055.002", "name": "Portable Executable Injection", "evidence": "VirtualAllocEx+WriteProcessMemory+CreateRemoteThread chain in Stage 3 + observed injection into explorer.exe in Stage 4"}
    ],
    "family": {"name": "Qakbot", "confidence": "high", "version": "5.x"},
    "campaign_overlap": [
      {"campaign": "Mandiant FIN7-2026-Q1", "shared_iocs": ["1.2.3.4", "mutex:QBOT_V5_MUTEX"], "confidence": "medium"}
    ],
    "hypothesis_outcome": "confirmed | revised | rejected",
    "revised_hypothesis": "null if confirmed, else updated statement"
  }
}
```

## Steps

### 1. Re-query OSINT with fresh IOCs from Stage 4

Stage 4 produced live C2 IPs, DNS lookups, dropped-file hashes. Run these through the same sources:

```bash
# Each C2 IP from Stage 4 → Shodan, VT, MISP (if available)
for IP in $(jq -r '.findings.network.c2_callbacks[].host' <<< "$STAGE4"); do
  # Shodan live check
  curl -sS "https://api.shodan.io/shodan/host/$IP?key=$SHODAN_KEY"
  # VT IP reputation
  curl -sS -H "x-apikey: $VT_KEY" "https://www.virustotal.com/api/v3/ip_addresses/$IP"
  # MISP (if configured)
  [ -n "$MISP_URL" ] && curl -sS -H "Authorization: $MISP_KEY" \
    "$MISP_URL/attributes/restSearch" -d "{\"value\":\"$IP\"}"
done

# Each dropped-file hash from Stage 4 → VT, MalwareBazaar
for HASH in $(jq -r '.findings.dropped_payloads[].hash' <<< "$STAGE4"); do
  curl -sS -H "x-apikey: $VT_KEY" "https://www.virustotal.com/api/v3/files/$HASH"
done
```

### 2. MITRE ATT&CK mapping — map observed behavior to techniques

Use the local matrix data + behavioral patterns:

- `VirtualAllocEx + WriteProcessMemory + CreateRemoteThread` → T1055.002
- Registry write to `Run\*` key → T1547.001
- HTTPS C2 with JA3 fingerprint matching known family → T1071.001
- Process hollowing markers → T1055.012

Produce a list of `mitre_ttps_confirmed` with evidence pointing back to specific Stage 3 / Stage 4 findings.

### 3. Family identification

Given:
- Stage 2's `prior_hypothesis`
- Stage 3's YARA hits + family_extraction
- Stage 4's behavioral signatures

Compare. One of three outcomes:
- **confirmed**: all signals agree → high confidence, family name + version
- **revised**: Stage 2 said Qakbot but Stage 4 behavior matches IcedID → note the revision + reasoning
- **rejected**: Stage 2 hypothesis had no corroborating behavior → drop it, mark family `unknown`

### 4. Campaign correlation (best-effort)

Check if observed IOCs appear in recent campaign reporting:

```bash
# Web search for exact IOCs in recent vendor posts
for IOC in "${CORE_IOCS[@]}"; do
  # use platform web-search skill
  echo "Query: $IOC recent campaign site:mandiant.com OR site:crowdstrike.com OR site:cisa.gov"
done
```

Note any overlap with 3-source+ reporting (single-source hits are noise).

### 5. Write envelope + publish comment

```markdown
## Stage 5 — Correlation & attribution

**Stage 2 hypothesis outcome**: ✅ confirmed | 🔄 revised | ❌ rejected

### Family
`Qakbot v5.x` — **high confidence**. Static (Stage 3) config-block decode matches v5.1.3 schema; dynamic (Stage 4) C2 pattern + mutex match.

### MITRE ATT&CK TTPs confirmed
- **T1055.002** — Portable Executable Injection. Evidence: `VirtualAllocEx+WriteProcessMemory+CreateRemoteThread` (Stage 3) + actual injection into explorer.exe (Stage 4).
- **T1547.001** — Registry Run Keys. Evidence: Write to `HKCU\...\Run\<random>` (Stage 4).
- **T1071.001** — Web Protocols C2. Evidence: HTTPS callback to `1.2.3.4:443` (Stage 4), JA3=`abc123...` matches Qakbot v5 fingerprint.

### C2 infrastructure matches
- `1.2.3.4`: MISP feed `c2-tracker` (first seen 2026-03-01, confidence high) + Shodan shows Cobalt-Strike default cert
- Overlaps with Mandiant 2026-04-15 Qakbot campaign (shared mutex + C2)

<details>Full JSON...</details>
```

## Failure handling

- Same as Stage 2 — degrade gracefully on missing creds. If all OSINT sources are unavailable, correlation becomes "local reasoning" (match Stage 3 YARA hits against a bundled MITRE table).

## Guardrails

- Attribution requires 2+ independent corroborating sources. A single VT family tag or one YARA hit isn't attribution — it's a suggestion. Wrong attribution gets quoted in incident reports and amplified; downstream IR teams end up chasing the wrong actor. "Unattributed" is a better answer than speculative.
- Calibrate confidence to source count. High = 3+ corroborating. Medium = 2. Low = 1 + circumstantial signal. Below that it's `unknown`. The verdict in Stage 6 will inherit these values directly.
- If you don't find prior reporting, say so plainly. Inventing a campaign name to sound confident is worse than admitting the sample is novel or unreported.
