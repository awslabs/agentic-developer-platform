"""Periodic vulnerability scanner — download stored SBOMs, scan, upsert findings.

Runs as a CronJob (daily). For each repo with a stored SBOM in S3:
  1. Download the CycloneDX SBOM to /tmp
  2. Run OSV-Scanner (ecosystem vulns) + Trivy (OS layer)
  3. Normalize findings via pipeline/vuln_scanner/normalize.py
  4. Upsert into the `vulnerabilities` Postgres table (dedup: cve_id)

Idempotent: re-runs update existing rows, never duplicate.

Usage:
    python scan-vulns.py [--max-repos N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Pipeline modules available at /app/ in the container
sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).parent))

from config import Settings  # noqa: E402

settings = Settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("scan-vulns")


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def _get_s3_client():
    """Create an S3 client using the configured region."""
    import boto3

    return boto3.client("s3", region_name=settings.aws_region)


def list_sbom_keys(s3_client, bucket: str, prefix: str) -> list[str]:
    """List all SBOM keys under the given S3 prefix.

    Returns keys matching *.cdx.json pattern.
    """
    keys: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".cdx.json"):
                keys.append(key)

    log.info("Found %d SBOMs under s3://%s/%s", len(keys), bucket, prefix)
    return keys


def download_sbom(s3_client, bucket: str, key: str, dest_dir: str) -> str:
    """Download a single SBOM from S3 to a local file. Returns local path."""
    filename = key.replace("/", "_")
    local_path = os.path.join(dest_dir, filename)

    s3_client.download_file(bucket, key, local_path)
    return local_path


# ---------------------------------------------------------------------------
# Database upsert
# ---------------------------------------------------------------------------


def upsert_vulnerabilities(conn, findings: list) -> int:
    """Upsert normalized vulnerability findings into the vulnerabilities table.

    Dedup key: cve_id (UNIQUE constraint). On conflict, updates fields to
    latest scanner output (severity may change as CVSS is revised).

    Returns number of rows upserted.
    """
    if not findings:
        return 0

    sql = """
        INSERT INTO vulnerabilities (id, cve_id, package, affected_versions, safe_version, severity, details, discovered_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (cve_id) DO UPDATE SET
            package = EXCLUDED.package,
            affected_versions = EXCLUDED.affected_versions,
            safe_version = EXCLUDED.safe_version,
            severity = EXCLUDED.severity,
            details = EXCLUDED.details,
            discovered_at = LEAST(vulnerabilities.discovered_at, EXCLUDED.discovered_at)
    """

    cursor = conn.cursor()
    total = 0
    try:
        for f in findings:
            # Build PURL-style package identifier for the package column
            ecosystem = f.package_ecosystem or "unknown"
            pkg = f"pkg:{ecosystem}/{f.package_name}" if f.package_name else f.cve_id

            details = json.dumps(
                {
                    "aliases": f.aliases,
                    "cvss_score": f.cvss_score,
                    "summary": f.summary,
                    "source_scanner": f.source_scanner,
                    "source_sbom_type": f.source_sbom_type,
                    "raw_id": f.raw_id,
                }
            )

            cursor.execute(
                sql,
                (
                    str(uuid.uuid4()),
                    f.cve_id,
                    pkg,
                    f.affected_versions or "all",
                    f.fixed_version,
                    f.severity or "UNKNOWN",
                    details,
                    f.detected_at or datetime.now(timezone.utc),
                ),
            )
            total += 1

        conn.commit()
        log.info("Upserted %d vulnerability rows", total)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()

    return total


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------


def scan_single_sbom(sbom_path: str, sbom_type: str = "source") -> list:
    """Run both scanners against a single SBOM file. Returns combined findings.

    Tolerates individual scanner failures (logs warning, continues).
    """
    from pipeline.vuln_scanner.scanner import (
        ScanError,
        scan_sbom_osv,
        scan_sbom_trivy,
    )

    findings = []

    # OSV-Scanner (ecosystem packages)
    try:
        osv_results = scan_sbom_osv(sbom_path, sbom_type=sbom_type)
        findings.extend(osv_results)
        log.info("OSV-Scanner: %d findings from %s", len(osv_results), Path(sbom_path).name)
    except ScanError as e:
        log.warning("OSV-Scanner failed for %s: %s", sbom_path, e)
    except Exception as e:
        log.warning("OSV-Scanner unexpected error for %s: %s", sbom_path, e)

    # Trivy (OS layer + language packages)
    try:
        trivy_results = scan_sbom_trivy(sbom_path, sbom_type=sbom_type)
        findings.extend(trivy_results)
        log.info("Trivy: %d findings from %s", len(trivy_results), Path(sbom_path).name)
    except ScanError as e:
        log.warning("Trivy failed for %s: %s", sbom_path, e)
    except Exception as e:
        log.warning("Trivy unexpected error for %s: %s", sbom_path, e)

    return findings


def deduplicate_findings(findings: list) -> list:
    """Deduplicate findings by cve_id, keeping the highest severity."""
    severity_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
    seen: dict[str, object] = {}

    for f in findings:
        key = f.cve_id
        if key not in seen:
            seen[key] = f
        else:
            existing = seen[key]
            if severity_rank.get(f.severity, 0) > severity_rank.get(existing.severity, 0):
                seen[key] = f

    return list(seen.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Periodic vulnerability scanner")
    parser.add_argument("--max-repos", type=int, default=0, help="Max repos to scan (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Scan but don't write to DB")
    parser.add_argument("--sbom-prefix", type=str, default=None, help="S3 prefix for SBOMs")
    args = parser.parse_args()

    bucket = settings.s3_bucket_name
    sbom_prefix = args.sbom_prefix or settings.sbom_s3_prefix

    if not bucket:
        log.error("S3_BUCKET_NAME not configured — cannot list SBOMs")
        sys.exit(1)

    log.info("=== Vulnerability Scan Started (%s) ===", datetime.now(timezone.utc).isoformat())
    log.info("Bucket: %s, Prefix: %s", bucket, sbom_prefix)

    # Step 1: List stored SBOMs
    s3_client = _get_s3_client()
    sbom_keys = list_sbom_keys(s3_client, bucket, f"{sbom_prefix}/repos/")

    if not sbom_keys:
        log.warning("No SBOMs found — nothing to scan. Exiting.")
        sys.exit(0)

    if args.max_repos and len(sbom_keys) > args.max_repos:
        log.info("Limiting to %d repos (of %d available)", args.max_repos, len(sbom_keys))
        sbom_keys = sbom_keys[: args.max_repos]

    # Step 2: Download and scan each SBOM
    all_findings = []
    scan_ok = 0
    scan_fail = 0

    with tempfile.TemporaryDirectory(prefix="vuln-scan-") as tmp_dir:
        for key in sbom_keys:
            repo_name = _extract_repo_from_key(key)
            log.info("Scanning: %s", repo_name or key)

            try:
                local_path = download_sbom(s3_client, bucket, key, tmp_dir)
                findings = scan_single_sbom(local_path)
                all_findings.extend(findings)
                scan_ok += 1
            except Exception as e:
                log.error("Failed to process %s: %s", key, e)
                scan_fail += 1

    log.info(
        "Scan complete: %d/%d repos scanned, %d total findings",
        scan_ok,
        scan_ok + scan_fail,
        len(all_findings),
    )

    # Step 3: Deduplicate
    deduped = deduplicate_findings(all_findings)
    log.info("After dedup: %d unique vulnerabilities", len(deduped))

    if not deduped:
        log.info("No vulnerabilities found across all SBOMs.")
        sys.exit(0)

    # Step 4: Upsert to database
    if args.dry_run:
        log.info("[DRY RUN] Would upsert %d vulnerabilities. Skipping DB write.", len(deduped))
        _print_summary(deduped)
        sys.exit(0)

    import db

    conn = db.get_connection()
    try:
        count = upsert_vulnerabilities(conn, deduped)
        log.info("=== Done: %d vulnerabilities upserted ===", count)
    finally:
        conn.close()

    _print_summary(deduped)


def _extract_repo_from_key(key: str) -> str:
    """Extract org/repo from an S3 key like 'sbom/repos/org/repo/source.cdx.json'."""
    parts = key.split("/")
    # Expected: sbom/repos/<org>/<repo>/source.cdx.json
    try:
        repos_idx = parts.index("repos")
        if repos_idx + 2 < len(parts):
            return f"{parts[repos_idx + 1]}/{parts[repos_idx + 2]}"
    except ValueError:
        pass
    return key


def _print_summary(findings: list) -> None:
    """Print severity breakdown."""
    from collections import Counter

    severity_counts = Counter(f.severity for f in findings)
    log.info("Severity breakdown:")
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"):
        if severity_counts.get(sev, 0) > 0:
            log.info("  %s: %d", sev, severity_counts[sev])


if __name__ == "__main__":
    main()
