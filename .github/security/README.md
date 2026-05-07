# Security Scanning

This directory contains configuration and baseline files for the ADP security scanning pipeline (`.github/workflows/security-scan.yml`).

## How It Works

The security scan workflow runs **8 tools** in parallel on every PR and push to `main`:

| Tool | Purpose | Runner |
|------|---------|--------|
| Checkov | IaC scanning (Terraform, CFN, Dockerfile, K8s) | arc-runner-org |
| Semgrep | SAST (Python, TypeScript, JavaScript) | arc-runner-org |
| detect-secrets | Credential/secret detection | arc-runner-org |
| Grype | Container CVE scanning | ubuntu-latest |
| Bandit | Python-specific SAST | arc-runner-org |
| cfn-nag | CloudFormation security | arc-runner-org |
| npm audit | Node.js dependency vulnerabilities | arc-runner-org |
| Syft | SBOM generation (CycloneDX) | ubuntu-latest |

## Baselines

Baseline files store the "known state" of findings. PRs only report **new** findings (not the full backlog).

### How baselines are updated

Baselines are updated via **nightly bot PR**:
1. The scheduled run (2 AM UTC daily) scans `main`
2. If findings differ from the current baseline, a PR is opened: `chore(security): refresh baselines YYYY-MM-DD`
3. A human reviews and merges (or rejects) the baseline update

**PR-time scans never update baselines.** They only read them.

### Baseline files

- `checkov-baseline.json` — Checkov findings snapshot
- `semgrep-baseline.sarif` — Semgrep SARIF findings
- `grype-baseline.json` — Grype CVE findings (keyed by image + CVE)
- `bandit-baseline.json` — Bandit Python findings
- `cfn-nag-baseline.json` — cfn-nag CloudFormation findings
- `npm-audit-baseline.json` — npm audit vulnerability findings
- `.secrets.baseline` — detect-secrets native baseline

## Configuration Files

- `checkov.yml` — Checkov framework/directory/skip-check config
- `semgrep.yml` — Semgrep custom rules (auto config is primary)
- `.banditrc` — Bandit skip rules and exclusions
- `cfn-nag-suppressions.yml` — Documented false-positive suppressions

## Running Locally

```bash
# Checkov
pip install checkov==3.2.346
checkov --directory . --config-file .github/security/checkov.yml --quiet

# Semgrep
pip install semgrep==1.80.0
semgrep scan --config auto

# detect-secrets
pip install detect-secrets==1.5.0
detect-secrets scan --baseline .github/security/.secrets.baseline

# Grype (requires Docker)
docker run --rm -v "$PWD:/src" anchore/grype:v0.80.2 dir:/src

# Bandit
pip install bandit==1.7.9
bandit -r . --ini .github/security/.banditrc

# cfn-nag (requires Ruby)
gem install cfn-nag
cfn_nag_scan --input-path modules/agent-factory/agent-worker-image/aws/

# npm audit
cd modules/gateway/frontend && npm audit --audit-level=high

# Syft (SBOM, requires Docker)
docker run --rm -v "$PWD:/src" anchore/syft:v1.11.1 dir:/src -o cyclonedx-json
```

## SARIF Categories

Findings appear in GitHub Security tab under:
- `security/checkov`
- `security/semgrep`
- `security/grype-<image-slug>`
- `security/bandit`

## Regenerating Baselines Manually

If you need to refresh baselines outside the nightly schedule:

```bash
gh workflow run security-scan.yml
```

This triggers the workflow on `main`, which will open a baseline-refresh PR if findings have changed.
