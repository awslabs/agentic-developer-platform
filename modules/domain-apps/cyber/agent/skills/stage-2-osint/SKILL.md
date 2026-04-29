---
name: stage-2-osint
description: Consult OSINT sources and synthesize a prior hypothesis that steers downstream analysis. M1 scope is MalwareBazaar (no auth, no vault dependency) + web search + MITRE ATT&CK; VirusTotal + Shodan + urlscan are commented out in the skill until vault credentials are provisioned. Use this skill whenever you have a sample's hashes + candidate IOCs (from Stage 1 triage) and want to check reputation, find prior reporting, or decide whether deeper analysis is worth the compute. Always run after Stage 1 and before Stage 3 — the hypothesis this produces narrows Stage 3's YARA scan and Stage 4's sandbox profile. Can short-circuit known-benign samples straight to a Stage 6 verdict.
---

# Stage 2 — OSINT recon & prior-hypothesis

You run in the main ADP cluster with Bedrock, internet, and vault credentials. Sample bytes never reach you; you only see hashes + IOCs from Stage 1. Convert that raw fingerprint into an **informed investigation plan**.

Short-circuit to Stage 6 with a benign verdict if OSINT returns a known-benign hit (EICAR, Microsoft-signed clean binary, VT 0/70 malicious with high harmless consensus). Skipping Stages 3-5 on known-clean samples saves ~30 min of compute per analysis.

## Inputs

| Var | Source | Shape |
|---|---|---|
| `ARTIFACT_ID` | persistent | string |
| Stage 1 envelope | DDB | hashes + candidate_iocs |
| `VIRUSTOTAL_SECRET_PREFIX` | env | secretsmanager path prefix (per-org key) |
| `SHODAN_SECRET_PREFIX` | env | secretsmanager path prefix |

## Outputs

Stage envelope:

```json
{
  "artifact_id": "...",
  "stage": 2,
  "stage_name": "osint",
  "status": "ok | partial | failed",
  "findings": {
    "vt_hits": 42,
    "vt_families": ["Qakbot", "Qbot"],
    "vt_first_seen": "2026-01-14",
    "vt_last_seen": "2026-04-28",
    "malwarebazaar_tags": ["qakbot", "banker", "loader"],
    "c2_live_checks": [
      {"ioc": "1.2.3.4", "live": true, "port": 443, "banner_snippet": "..."}
    ],
    "prior_reporting": [
      {"source": "Mandiant", "url": "...", "summary": "..."}
    ],
    "mitre_ttps_suggested": ["T1055.002", "T1071.001"],
    "prior_hypothesis": "Likely Qakbot v5.x based on VT family + recent Mandiant report",
    "recommended_static_focus": ["config_block_extraction", "c2_pattern_confirmation"],
    "recommended_yara_rules": ["qakbot_v5", "banking_trojan_generic"],
    "short_circuit": false
  },
  "notes": "free text — especially note skipped sources due to missing creds"
}
```

## Steps

> **M1 scope note**: for the first end-to-end pipeline tests, only MalwareBazaar is enabled (no auth, no vault dependency). VirusTotal, Shodan, and urlscan are documented below but commented out — re-enable them once the corresponding credentials are provisioned in vault.

### 1. (M1 skip) Fetch OSINT credentials from vault

Commented out until vault creds are configured. Re-enable this block when VT/Shodan keys land in Secrets Manager.

```bash
# VT_KEY=$(aws secretsmanager get-secret-value --region us-east-1 \
#   --secret-id "${VIRUSTOTAL_SECRET_PREFIX}/public" \
#   --query SecretString --output text 2>/dev/null || echo "")
#
# SHODAN_KEY=$(aws secretsmanager get-secret-value --region us-east-1 \
#   --secret-id "${SHODAN_SECRET_PREFIX}/public" \
#   --query SecretString --output text 2>/dev/null || echo "")
```

### 2. (M1 skip) VirusTotal hash lookup

Commented out for M1 — no VT credentials yet. Re-enable alongside step 1. Note in `notes`: `"vt skipped — no credentials provisioned (M1)"`.

```bash
# if [ -n "$VT_KEY" ]; then
#   SHA256="<from Stage 1>"
#   curl -sS -H "x-apikey: $VT_KEY" \
#     "https://www.virustotal.com/api/v3/files/$SHA256" | tee /tmp/vt.json
#
#   jq '{
#     malicious: .data.attributes.last_analysis_stats.malicious,
#     total: (.data.attributes.last_analysis_stats | to_entries | map(.value) | add),
#     family: .data.attributes.popular_threat_classification.suggested_threat_label,
#     first_seen: .data.attributes.first_submission_date,
#     last_seen: .data.attributes.last_analysis_date
#   }' /tmp/vt.json
# fi
```

### 3. MalwareBazaar (ENABLED — no auth, free) — the M1 primary source

```bash
SHA256="<from Stage 1 envelope>"
curl -sS -X POST --data "query=get_info&hash=$SHA256" \
  https://mb-api.abuse.ch/api/v1/ > /tmp/mb.json

# MalwareBazaar returns query_status = "ok" (hit) or "hash_not_found" (miss).
STATUS=$(jq -r '.query_status' /tmp/mb.json)

if [ "$STATUS" = "ok" ]; then
  jq '{
    query_status: .query_status,
    signature: .data[0].signature,
    tags: .data[0].tags,
    file_type: .data[0].file_type,
    first_seen: .data[0].first_seen,
    reporter: .data[0].reporter,
    intelligence: .data[0].intelligence,
    yara_rules: .data[0].yara_rules
  }' /tmp/mb.json
else
  # hash_not_found — unknown sample, no prior reporting
  echo '{"query_status":"hash_not_found","note":"sample not in MalwareBazaar"}'
fi
```

Short-circuit check: if `signature` contains `EICAR` or if the hash matches the known EICAR signature (`275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f`), set `short_circuit: true` + verdict benign.

### 4. (M1 skip) Shodan — for each candidate IP from Stage 1

Commented out for M1 — no Shodan credentials yet.

```bash
# for IP in $(jq -r '.candidate_iocs.ips[]' <<< "$STAGE1"); do
#   curl -sS "https://api.shodan.io/shodan/host/$IP?key=$SHODAN_KEY" | \
#     jq --arg ip "$IP" '{ioc:$ip, live:(.ip_str != null), ports:.ports, hostnames:.hostnames, tls:.ssl.cert.issuer}'
# done
```

### 5. (M1 skip) urlscan.io — for each candidate domain/URL

Commented out for M1 — rate limit concerns with unauthenticated requests during testing. Re-enable post-M1.

```bash
# for TARGET in $(jq -r '.candidate_iocs.domains[], .candidate_iocs.urls[]' <<< "$STAGE1"); do
#   curl -sS "https://urlscan.io/api/v1/search/?q=domain:$TARGET&size=3" | \
#     jq '{results: [.results[] | {url:.page.url, verdict:.verdicts.malicious, scanned_at:.task.time}]}'
# done
```

### 6. Web search — prior reporting

If VT/MB returned a family name, search for it:

```bash
# Use the platform's web-search skill if available, else WebSearch tool directly
# Queries to run:
#   - "<family_name> malware analysis"
#   - "<sha256_first_16> <family>"
#   - "<c2_domain> threat intel"
# Fetch top 3 results each, extract title + URL + short excerpt
```

### 7. MITRE ATT&CK mapping

If a family name is known:

```bash
# Look up family in MITRE's known-software database
curl -sS "https://attack.mitre.org/api.php?action=ask&query=[[Category:Software]][[Has+display+name::$FAMILY_NAME]]|?Has+technique&format=json"
# Or use the bundled enterprise-matrix.json if offline
```

### 8. Short-circuit check

Before synthesizing, check for known-benign:

- Hash matches EICAR signature (`275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f`) → test file, benign (works without any OSINT)
- MalwareBazaar returned `query_status = "hash_not_found"` AND Stage 1 showed a valid Authenticode signature → probably benign (but note: MB miss ≠ benign in general; only combine with signature check)
- (Post-M1) VT `malicious` count = 0 AND `harmless` > 10 → benign
- (Post-M1) Microsoft-signed with valid Authenticode + clean VT score → benign

If any triggers, set `short_circuit: true` and synthesize minimal findings. The persona will skip Stages 3-5 and jump to Stage 6.

### 9. Synthesize prior_hypothesis

This is the reasoning step — use the LLM (you) to produce 1-2 sentences + recommended focus. Example:

> "Likely Qakbot v5.x — matches Mandiant 2026-04-15 report hash prefix AND C2 infrastructure from VT. Stage 3 should prioritize config-block extraction + Qakbot-family YARA rules, skip generic injection-signature scan."

### 10. Write envelope + publish stage comment

Same pattern as Stage 1: write DDB, post GH comment.

Comment format (M1 — MalwareBazaar only):

```markdown
## Stage 2 — OSINT recon

**Status**: ✅ | **Short-circuit**: no
**Scope (M1)**: MalwareBazaar only. VT / Shodan / urlscan deferred until credentials are provisioned.

**Sources consulted:**
- MalwareBazaar: `query_status=ok` — signature=`<family>`, tags=`[...]`, first_seen=`<date>`, reporter=`<username>`
  _(or `query_status=hash_not_found` — sample unknown to MalwareBazaar)_
- ~~VirusTotal~~ — deferred (no credentials)
- ~~Shodan~~ — deferred
- ~~urlscan.io~~ — deferred
- Web search — `<n>` hits matching `<family>` / hash prefix
- MITRE ATT&CK: `<T1055.002>`, `<T1071.001>` (if family known)

### Prior hypothesis
Likely `<family>` based on MalwareBazaar signature + web reporting. _(If MB miss + no reporting: "Unknown sample — no prior reporting; Stage 3 should run default YARA corpus.")_

### Recommended for Stage 3
- Focus: `<list>`
- YARA rules: `<list>` (or `default` if no hypothesis)

<details>...full JSON...</details>
```

## Failure handling

- Any individual source fails/rate-limits → skip it, note in `notes`, continue with others
- All sources fail → stage status `partial`, prior_hypothesis = "unknown — no OSINT available"
- Don't block the pipeline. Stage 3 can run with an empty brief (generic YARA scan).

## Rate limits

- VirusTotal Public API: 4 req/min, 500 req/day. Cache per-hash in DDB.
- MalwareBazaar: no published limit, be polite (< 1 req/sec).
- Shodan free: 100 req/month. Only hit for IOCs likely to matter (skip private IPs, localhost).
- urlscan: 100 req/day.

## Guardrails

- Degrade gracefully on missing credentials. A missing VT key isn't a stage failure — it's a reduced brief. Fail-hard on missing OSINT would make the pipeline unusable for tenants who haven't yet configured every intel source.
- Short-circuit eagerly on known-benign. Running the full pipeline on EICAR or a signed Microsoft binary wastes compute and floods the issue with stage comments that all say "clean."
- Synthesize the hypothesis; don't dump raw API responses. Stage 3's YARA ruleset and Stage 4's sandbox profile depend on a crisp hypothesis — a wall of JSON teaches Stage 3 nothing.
- Cache per-hash in DDB. VirusTotal's free tier is 4 req/min; repeat lookups on the same hash in a single pipeline run will rate-limit you and slow every subsequent stage.
