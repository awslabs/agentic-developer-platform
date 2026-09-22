"""
Stage 1 triage worker — produces a structured fingerprint of the sample.

Replaces the M1 stub with real analysis logic using tooling baked into the
cyber worker image: file, magika, pefile, iocextract, strings.

Envelope shape matches modules/domain-apps/cyber/agent/skills/stage-1-triage/SKILL.md.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
import sys

from isolation import IsolationError, run_isolated

from job_delivery import download_sample, registered_job

from sample_access import AccessDenied, job_context, resolve_sample


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def _hashes(path: Path) -> dict:
    """Compute MD5, SHA1, SHA256 of the file."""
    # MD5/SHA1 used for malware sample fingerprinting (IOC identifiers), not security
    md5 = hashlib.md5(usedforsecurity=False)  # nosec B324
    sha1 = hashlib.sha1(usedforsecurity=False)  # nosec B324
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


def _file_type(path: Path) -> dict:
    """Identify file type via libmagic and magika; flag disagreement."""
    import magic
    from magika import Magika

    libmagic_desc = magic.from_file(str(path))
    m = Magika()
    with open(path, "rb") as f:
        magika_result = m.identify_bytes(f.read())
    return {
        "libmagic": libmagic_desc,
        "magika_label": magika_result.output.label,
        "magika_mime": magika_result.output.mime_type,
        "disagreement": _check_disagreement(libmagic_desc, magika_result.output.label),
    }


def _check_disagreement(libmagic_desc: str, magika_label: str) -> bool:
    """Check if libmagic and magika disagree on whether a file is PE."""
    libmagic_says_pe = "pe" in libmagic_desc.lower() or "executable" in libmagic_desc.lower()
    magika_says_pe = "pe" in magika_label.lower() or "executable" in magika_label.lower()
    return libmagic_says_pe != magika_says_pe


def _is_pe(libmagic_desc: str) -> bool:
    """Check if the file looks like a PE based on libmagic."""
    desc_lower = libmagic_desc.lower()
    return "pe32" in desc_lower or "pe32+" in desc_lower or "ms-dos executable" in desc_lower


def _pe_fields(path: Path) -> dict:
    """Extract PE-specific fields: compile timestamp, sections, signature status."""
    import pefile

    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_SECURITY"]]
        )
        compile_ts = pe.FILE_HEADER.TimeDateStamp
        # Convert to ISO8601
        compile_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(compile_ts))

        sections = [
            {
                "name": s.Name.decode(errors="replace").rstrip("\x00"),
                "size": s.SizeOfRawData,
                "entropy": round(s.get_entropy(), 3),
                "packed_flag": s.get_entropy() > 7.0,
            }
            for s in pe.sections
        ]

        has_sig = bool(getattr(pe, "DIRECTORY_ENTRY_SECURITY", None))
        # We can detect presence but not validity without full Authenticode verification
        signature_status = "unsigned"
        if has_sig:
            signature_status = "unknown"  # present but can't verify validity in pure pefile

        pe.close()
        return {
            "compile_timestamp": compile_iso,
            "sections": sections,
            "signature_status": signature_status,
        }
    except Exception as e:
        return {
            "compile_timestamp": None,
            "sections": [],
            "signature_status": "unknown",
            "pe_parse_error": str(e),
        }


def _strings(path: Path, limit: int = 500) -> list:
    """Extract printable strings via the `strings` binary, capped to limit."""
    try:
        result = subprocess.run(
            ["strings", "-n", "6", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout.splitlines()[:limit]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _iocs(strings_sample: list) -> dict:
    """Extract candidate IOCs from strings using iocextract."""
    import iocextract

    blob = "\n".join(strings_sample)
    urls = sorted(set(iocextract.extract_urls(blob, refang=True)))
    ips = sorted(set(iocextract.extract_ips(blob, refang=True)))

    # Derive domains from extracted URLs (iocextract has no extract_domains)
    domains = set()
    for url in urls:
        try:
            # Strip protocol and path to get domain
            host = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
            if "." in host and not host.replace(".", "").isdigit():
                domains.add(host)
        except (IndexError, ValueError):
            continue

    return {
        "domains": sorted(domains),
        "ips": ips,
        "urls": urls,
        "registry_paths": [
            s
            for s in strings_sample
            if s.startswith("HKEY_") or s.startswith("HKLM\\") or s.startswith("HKCU\\")
        ][:50],
        "mutexes": [],
    }


def _fingerprint(sample_path: Path) -> dict:
    """Build a complete triage fingerprint of the sample."""
    hashes = _hashes(sample_path)
    ft = _file_type(sample_path)

    pe_info = _pe_fields(sample_path) if _is_pe(ft["libmagic"]) else {}

    all_strings = _strings(sample_path)
    iocs = _iocs(all_strings)

    return {
        "hashes": hashes,
        "file_type": ft["libmagic"],
        "file_type_magika": ft["magika_label"],
        "file_type_disagreement": ft["disagreement"],
        "compile_timestamp": pe_info.get("compile_timestamp"),
        "signature_status": pe_info.get("signature_status", "unsigned"),
        "sections": pe_info.get("sections", []),
        "strings_sample": all_strings[:100],
        "candidate_iocs": iocs,
    }


def _fail_stage(
    sqs,
    ddb,
    msg: dict,
    artifact_id: str,
    response_queue_url: str,
    queue_url: str,
    reason: str,
    start: float,
) -> None:
    """Record an authorization refusal as an explicit failed stage.

    Issue #5616. Mirrors the static worker: the requester gets a real failure
    with a reason code rather than empty findings, the rejected location is
    never echoed (it could name another tenant's object), and the message is
    deleted because the refusal is deterministic and would be repeated.
    """
    ts = int(time.time())
    findings = {"status": "rejected", "reason": reason}

    envelope = {
        "artifact_id": artifact_id,
        "stage": 1,
        "stage_name": "triage",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "failed",
        "duration_seconds": int(time.time() - start),
        "findings": findings,
        "tool_calls": 0,
        "notes": f"stage refused: {reason}",
    }

    ddb.put_item(
        Item={
            "artifact_id": artifact_id,
            "stage_timestamp": f"triage#{ts}",
            "stage": "triage",
            "status": "failed",
            "findings": json.dumps(findings),
            "image_tag": os.environ.get("IMAGE_TAG", "unknown"),
        }
    )
    sqs.send_message(
        QueueUrl=response_queue_url,
        MessageBody=json.dumps(envelope),
        MessageGroupId=artifact_id,
        MessageDeduplicationId=f"{artifact_id}-triage-{ts}",
    )
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
    print(f"triage REFUSED {artifact_id}: {reason}")


def run() -> None:
    """Main entrypoint — receive SQS message, triage sample, write results."""
    queue_url = os.environ["INPUT_QUEUE_URL"]
    response_queue_url = os.environ["RESPONSE_QUEUE_URL"]
    results_table = os.environ["RESULTS_TABLE"]
    region = _region()

    sqs = boto3.client("sqs", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region).Table(results_table)

    resp = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        MessageAttributeNames=["All"],
    )
    msgs = resp.get("Messages", [])
    if not msgs:
        print("No message, exiting.")
        return

    msg = msgs[0]
    body = json.loads(msg["Body"])
    artifact_id = body["artifact_id"]
    start = time.time()

    # Issue #5616: authorize the sample against the job's trusted identity
    # before any download. Same shared resolver as the static worker — the
    # weakest remaining path is what an attacker would use, so both enforce.
    try:
        registered_job(body, "triage")
        ctx = job_context(body)
        sample_ref = resolve_sample(body, ctx)
    except AccessDenied as denied:
        _fail_stage(
            sqs, ddb, msg, artifact_id, response_queue_url, queue_url,
            reason=denied.reason, start=start,
        )
        return

    try:
        with tempfile.TemporaryDirectory() as td:
            sample_path = Path(td) / "sample"
            download_sample(body, sample_ref, sample_path)
            options = Path(td) / "options.json"
            options.write_text("{}")
            findings = run_isolated(
                [sys.executable, "-I", str(Path(__file__).resolve().parents[1] / "analyze.py"),
                 "triage", str(sample_path), str(options)], [sample_path, options],
            )
    except (IsolationError, AccessDenied):
        _fail_stage(sqs, ddb, msg, artifact_id, response_queue_url, queue_url,
                    reason="analysis_isolation_failed", start=start)
        return

    duration = int(time.time() - start)
    ts = int(time.time())

    envelope = {
        "artifact_id": artifact_id,
        "stage": 1,
        "stage_name": "triage",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "ok",
        "duration_seconds": duration,
        "findings": findings,
        "tool_calls": 5,
        "notes": "",
    }

    ddb.put_item(
        Item={
            "artifact_id": artifact_id,
            "stage_timestamp": f"triage#{ts}",
            "stage": "triage",
            "status": "ok",
            "findings": json.dumps(findings),
            "image_tag": os.environ.get("IMAGE_TAG", "unknown"),
        }
    )

    sqs.send_message(
        QueueUrl=response_queue_url,
        MessageBody=json.dumps(envelope),
        MessageGroupId=artifact_id,
        MessageDeduplicationId=f"{artifact_id}-triage-{ts}",
    )

    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
    print(f"triage OK {artifact_id}: {findings['hashes']['sha256'][:16]}")
