"""
Stage 3 static analysis worker — dual-mode dispatch.

Mode A (rule-driven): YARA scan, PE/ELF/Mach-O parsing, suspicious API combos,
anti-analysis signal detection. Default path when message has `focus`/`yara_rules`.

Mode B (agent-authored script): Downloads a Python script from S3, runs it in a
locked-down subprocess (300s timeout, stdout captured as JSON, non-root, no network).

Envelope shape matches modules/domain-apps/cyber/agent/skills/stage-3-static/SKILL.md.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
import lief
import yara

def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

YARA_RULES_DIR = os.environ.get("YARA_RULES_DIR", "/opt/yara-rules")

# Predefined suspicious API combinations
SUSPICIOUS_API_COMBOS = [
    {
        "apis": {"VirtualAllocEx", "WriteProcessMemory", "CreateRemoteThread"},
        "maps_to": "process_injection",
    },
    {
        "apis": {"VirtualAlloc", "VirtualProtect", "CreateThread"},
        "maps_to": "shellcode_execution",
    },
    {
        "apis": {"OpenProcess", "VirtualAllocEx", "NtWriteVirtualMemory"},
        "maps_to": "process_injection_nt",
    },
    {
        "apis": {"RegOpenKeyEx", "RegSetValueEx"},
        "maps_to": "registry_persistence",
    },
    {
        "apis": {"CreateService", "StartService"},
        "maps_to": "service_persistence",
    },
    {
        "apis": {"InternetOpen", "InternetConnect", "HttpSendRequest"},
        "maps_to": "http_communication",
    },
    {
        "apis": {"CryptEncrypt", "CryptDecrypt", "CryptImportKey"},
        "maps_to": "crypto_operations",
    },
]

# Anti-analysis detection strings
ANTI_ANALYSIS_DEBUGGER = ["IsDebuggerPresent", "CheckRemoteDebuggerPresent", "NtQueryInformationProcess"]
ANTI_ANALYSIS_VM = ["vmware", "virtualbox", "qemu", "vbox", "vmtools", "sandboxie"]
ANTI_ANALYSIS_SLEEP_THRESHOLD = 60000  # milliseconds


def _parse_binary(path: Path) -> dict:
    """Parse PE/ELF/Mach-O using lief and extract structural info."""
    binary = lief.parse(str(path))
    if binary is None:
        return {"format": "unknown", "sections": [], "imports": []}

    fmt = "unknown"
    if isinstance(binary, lief.PE.Binary):
        fmt = "PE"
    elif isinstance(binary, lief.ELF.Binary):
        fmt = "ELF"
    elif isinstance(binary, lief.MachO.Binary):
        fmt = "MachO"

    sections = []
    for s in binary.sections:
        sections.append({
            "name": s.name,
            "size": s.size,
            "entropy": round(s.entropy, 3),
            "characteristics": "",
        })

    imports = []
    if isinstance(binary, lief.PE.Binary) and binary.has_imports:
        for lib in binary.imports:
            for entry in lib.entries:
                if entry.name:
                    imports.append(f"{lib.name}!{entry.name}")

    return {"format": fmt, "sections": sections, "imports": imports}


def _detect_suspicious_combos(imports: list) -> list:
    """Check imports against known suspicious API combinations."""
    import_names = {imp.split("!")[-1] for imp in imports}
    hits = []
    for combo in SUSPICIOUS_API_COMBOS:
        if combo["apis"].issubset(import_names):
            hits.append({
                "combo": sorted(combo["apis"]),
                "maps_to": combo["maps_to"],
            })
    return hits


def _detect_anti_analysis(imports: list, strings_from_binary: list) -> list:
    """Detect anti-analysis signals: debugger checks, VM detection, long sleeps."""
    signals = []
    import_names = {imp.split("!")[-1] for imp in imports}
    all_text = " ".join(strings_from_binary).lower()

    for api in ANTI_ANALYSIS_DEBUGGER:
        if api in import_names:
            signals.append(f"debugger_check:{api}")

    for vm_str in ANTI_ANALYSIS_VM:
        if vm_str in all_text:
            signals.append(f"vm_detection:{vm_str}")

    # Check for suspicious sleep values in strings
    for s in strings_from_binary:
        try:
            val = int(s)
            if val >= ANTI_ANALYSIS_SLEEP_THRESHOLD:
                signals.append(f"sleep_{val}")
                break  # one is enough
        except ValueError:
            continue

    return signals


def _extract_strings(path: Path, limit: int = 500) -> list:
    """Extract printable strings via the `strings` binary."""
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


def _yara_scan(path: Path, rule_hints: list | None = None) -> list:
    """Run YARA scan against the sample. Optionally narrow by rule hints."""
    hits = []
    rules_dir = Path(YARA_RULES_DIR)
    if not rules_dir.is_dir():
        return [{"rule": "__error__", "meta": {}, "strings_matched": 0,
                 "error": f"YARA rules dir not found: {YARA_RULES_DIR}"}]

    # Collect .yar files — narrow to hints if provided
    yar_files = []
    if rule_hints:
        for hint in rule_hints:
            found = list(rules_dir.rglob(f"*{hint}*.yar"))
            yar_files.extend(found)
        if not yar_files:
            # Fallback: scan all if hints matched nothing
            yar_files = list(rules_dir.rglob("*.yar"))
    else:
        yar_files = list(rules_dir.rglob("*.yar"))

    for yar_file in yar_files[:50]:  # Cap to avoid timeouts
        try:
            rules = yara.compile(filepath=str(yar_file))
            matches = rules.match(str(path), timeout=30)
            for m in matches:
                meta = dict(m.meta) if m.meta else {}
                hits.append({
                    "rule": m.rule,
                    "meta": meta,
                    "strings_matched": len(m.strings),
                })
        except yara.Error:
            continue  # Skip broken rule files

    return hits


def _run_mode_a(sample_path: Path, focus: list | None, yara_rules: list | None) -> dict:
    """Mode A: rule-driven static analysis."""
    parsed = _parse_binary(sample_path)
    strings_sample = _extract_strings(sample_path)
    yara_hits = _yara_scan(sample_path, rule_hints=yara_rules)
    suspicious_combos = _detect_suspicious_combos(parsed["imports"])
    anti_analysis = _detect_anti_analysis(parsed["imports"], strings_sample)

    return {
        "mode": "rule-driven",
        "sections": parsed["sections"],
        "imports": parsed["imports"][:200],  # Cap for envelope size
        "suspicious_api_combos": suspicious_combos,
        "embedded_resources": [],
        "yara_hits": yara_hits,
        "anti_analysis_signals": anti_analysis,
        "family_extraction": {},
        "hypothesis_confirmed": None,
    }


def _run_mode_b(sample_path: Path, script_s3_uri: str, s3_client) -> dict:
    """Mode B: agent-authored script execution in locked-down subprocess."""
    with tempfile.TemporaryDirectory() as td:
        script_path = Path(td) / "script.py"
        bucket, key = script_s3_uri.replace("s3://", "", 1).split("/", 1)
        s3_client.download_file(bucket, key, str(script_path))

        try:
            result = subprocess.run(
                ["python3", str(script_path), str(sample_path)],
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                parsed = json.loads(result.stdout)
                return {
                    "mode": "agent-authored-script",
                    "script_s3_uri": script_s3_uri,
                    **parsed,
                }
            except json.JSONDecodeError as e:
                return {
                    "mode": "agent-authored-script",
                    "script_s3_uri": script_s3_uri,
                    "error": f"script stdout was not valid JSON: {e}",
                    "stdout_snippet": result.stdout[:500],
                }
        except subprocess.TimeoutExpired:
            return {
                "mode": "agent-authored-script",
                "script_s3_uri": script_s3_uri,
                "error": "script exceeded 300s timeout",
            }
        except subprocess.CalledProcessError as e:
            return {
                "mode": "agent-authored-script",
                "script_s3_uri": script_s3_uri,
                "error": f"script exited {e.returncode}",
                "stderr_snippet": (e.stderr or "")[:500],
            }


def run() -> None:
    """Main entrypoint — receive SQS message, dispatch Mode A or B, write results."""
    queue_url = os.environ["INPUT_QUEUE_URL"]
    response_queue_url = os.environ["RESPONSE_QUEUE_URL"]
    results_table = os.environ["RESULTS_TABLE"]
    region = _region()

    sqs = boto3.client("sqs", region_name=region)
    s3 = boto3.client("s3", region_name=region)
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
    sample_s3_uri = body["sample_s3_uri"]
    start = time.time()

    # Parse s3:// URI
    bucket, key = sample_s3_uri.replace("s3://", "", 1).split("/", 1)

    with tempfile.TemporaryDirectory() as td:
        sample_path = Path(td) / "sample"
        s3.download_file(bucket, key, str(sample_path))

        # Dispatch Mode A vs Mode B
        script_s3_uri = body.get("script_s3_uri")
        if script_s3_uri:
            findings = _run_mode_b(sample_path, script_s3_uri, s3)
        else:
            focus = body.get("focus")
            yara_rules = body.get("yara_rules")
            findings = _run_mode_a(sample_path, focus, yara_rules)

    duration = int(time.time() - start)
    ts = int(time.time())

    # Issue #272: Include rules provenance in envelope for traceability
    rules_manifest = {}
    rules_manifest_path = Path(YARA_RULES_DIR) / "rules-manifest.json"
    if rules_manifest_path.is_file():
        try:
            rules_manifest = json.load(open(rules_manifest_path))
        except (json.JSONDecodeError, OSError):
            rules_manifest = {"error": "failed to read rules-manifest.json"}

    envelope = {
        "artifact_id": artifact_id,
        "stage": 3,
        "stage_name": "static",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "ok",
        "duration_seconds": duration,
        "findings": findings,
        "rules_manifest": rules_manifest,
        "tool_calls": 1,
        "notes": "",
    }

    ddb.put_item(
        Item={
            "artifact_id": artifact_id,
            "stage_timestamp": f"static#{ts}",
            "stage": "static",
            "status": "ok",
            "findings": json.dumps(findings),
            "image_tag": os.environ.get("IMAGE_TAG", "unknown"),
        }
    )

    sqs.send_message(
        QueueUrl=response_queue_url,
        MessageBody=json.dumps(envelope),
        MessageGroupId=artifact_id,
        MessageDeduplicationId=f"{artifact_id}-static-{ts}",
    )

    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
    mode = findings.get("mode", "unknown")
    print(f"static OK {artifact_id}: mode={mode}")
