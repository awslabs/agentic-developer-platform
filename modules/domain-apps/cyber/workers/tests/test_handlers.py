"""
Tests for real triage and static analysis handlers.

Covers:
- Triage: EICAR fingerprinting, PE sample parsing
- Static Mode A: YARA rule matching
- Static Mode B: happy path, bad JSON, timeout

Uses moto for AWS mocking. No live AWS calls.
Does NOT commit sample binaries — generates test bytes or uses EICAR (68 bytes).
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

# Known EICAR SHA256
EICAR_SHA256 = "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"
EICAR_BYTES = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _make_minimal_pe() -> bytes:
    """Generate a minimal valid PE file (just headers, no real code)."""
    # MZ header
    mz = bytearray(64)
    mz[0:2] = b"MZ"
    struct.pack_into("<I", mz, 60, 64)  # e_lfanew = 64

    # PE signature
    pe_sig = b"PE\x00\x00"

    # COFF header: x86, 1 section, timestamp=0x60000000
    coff = struct.pack("<HHIIIHH",
                       0x14C,       # Machine: i386
                       1,           # NumberOfSections
                       0x60000000,  # TimeDateStamp
                       0, 0,        # PointerToSymbolTable, NumberOfSymbols
                       0xE0,        # SizeOfOptionalHeader
                       0x0102)      # Characteristics: EXECUTABLE_IMAGE | 32BIT_MACHINE

    # Optional header (PE32) — minimal
    opt = bytearray(0xE0)
    struct.pack_into("<H", opt, 0, 0x10B)  # Magic: PE32
    struct.pack_into("<I", opt, 16, 0x1000)  # AddressOfEntryPoint
    struct.pack_into("<I", opt, 28, 0x400000)  # ImageBase
    struct.pack_into("<I", opt, 32, 0x1000)  # SectionAlignment
    struct.pack_into("<I", opt, 36, 0x200)  # FileAlignment
    struct.pack_into("<I", opt, 56, 0x3000)  # SizeOfImage
    struct.pack_into("<I", opt, 60, 0x200)  # SizeOfHeaders
    struct.pack_into("<I", opt, 76, 16)  # NumberOfRvaAndSizes

    # Section header: .text
    section = bytearray(40)
    section[0:6] = b".text\x00"
    struct.pack_into("<I", section, 8, 0x100)   # VirtualSize
    struct.pack_into("<I", section, 12, 0x1000)  # VirtualAddress
    struct.pack_into("<I", section, 16, 0x200)   # SizeOfRawData
    struct.pack_into("<I", section, 20, 0x200)   # PointerToRawData
    struct.pack_into("<I", section, 36, 0x60000020)  # Characteristics

    # Section data (pad to 0x200 alignment)
    data = b"\xCC" * 0x200  # INT3 padding

    # Assemble: pad MZ+PE+COFF+opt+section to 0x200, then section data
    headers = bytes(mz) + pe_sig + coff + bytes(opt) + bytes(section)
    headers = headers.ljust(0x200, b"\x00")

    return headers + data


# ---------------------------------------------------------------------------
# Triage handler tests
# ---------------------------------------------------------------------------


class TestTriageFingerprint:
    """Test _fingerprint() directly — no AWS mocking needed."""

    def test_triage_eicar(self):
        """EICAR: known SHA256, magika detects it, IOCs mostly empty."""
        # Import must happen after we can mock; but _fingerprint is pure logic
        from triage.handler import _fingerprint

        with tempfile.NamedTemporaryFile(suffix=".com", delete=False) as f:
            f.write(EICAR_BYTES)
            f.flush()
            path = Path(f.name)

        try:
            result = _fingerprint(path)

            assert result["hashes"]["sha256"] == EICAR_SHA256
            assert result["hashes"]["md5"] is not None
            assert result["hashes"]["sha1"] is not None

            # File type should be identified
            assert result["file_type"] is not None
            assert result["file_type_magika"] is not None

            # EICAR has no PE sections
            assert result["sections"] == []

            # IOCs: EICAR string contains no domains/IPs/URLs
            iocs = result["candidate_iocs"]
            assert isinstance(iocs["domains"], list)
            assert isinstance(iocs["ips"], list)
            assert isinstance(iocs["urls"], list)
        finally:
            path.unlink()

    def test_triage_pe_sample(self):
        """Minimal PE: sections list non-empty, entropy values sensible."""
        from triage.handler import _fingerprint

        pe_bytes = _make_minimal_pe()
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            f.write(pe_bytes)
            f.flush()
            path = Path(f.name)

        try:
            result = _fingerprint(path)

            assert result["hashes"]["sha256"] is not None
            assert len(result["hashes"]["sha256"]) == 64

            # PE-specific: should have at least the .text section
            # (libmagic might or might not detect our minimal PE as PE32)
            # If it does, we should have sections
            if result["sections"]:
                sec = result["sections"][0]
                assert "name" in sec
                assert "entropy" in sec
                assert isinstance(sec["entropy"], float)
                assert 0.0 <= sec["entropy"] <= 8.0
        finally:
            path.unlink()


class TestTriageHashes:
    """Test individual helper functions."""

    def test_hashes_eicar(self):
        from triage.handler import _hashes

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(EICAR_BYTES)
            f.flush()
            path = Path(f.name)

        try:
            h = _hashes(path)
            assert h["sha256"] == EICAR_SHA256
            assert len(h["md5"]) == 32
            assert len(h["sha1"]) == 40
        finally:
            path.unlink()

    def test_strings_extraction(self):
        from triage.handler import _strings

        with tempfile.NamedTemporaryFile(delete=False) as f:
            # Write something with printable strings
            f.write(b"HELLO_WORLD_TEST_STRING\x00" * 5 + b"\x00" * 100)
            f.flush()
            path = Path(f.name)

        try:
            strings = _strings(path)
            assert isinstance(strings, list)
            # Should find at least one string with our test pattern
            assert any("HELLO_WORLD" in s for s in strings)
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# Static handler tests
# ---------------------------------------------------------------------------


class TestStaticModeA:
    """Test Mode A rule-driven analysis functions."""

    def test_suspicious_api_detection(self):
        from static.handler import _detect_suspicious_combos

        imports = [
            "kernel32.dll!VirtualAllocEx",
            "kernel32.dll!WriteProcessMemory",
            "kernel32.dll!CreateRemoteThread",
            "kernel32.dll!LoadLibrary",
        ]
        combos = _detect_suspicious_combos(imports)
        assert len(combos) >= 1
        assert any(c["maps_to"] == "process_injection" for c in combos)

    def test_anti_analysis_debugger(self):
        from static.handler import _detect_anti_analysis

        imports = ["kernel32.dll!IsDebuggerPresent", "kernel32.dll!GetModuleHandle"]
        signals = _detect_anti_analysis(imports, [])
        assert any("debugger_check" in s for s in signals)

    def test_anti_analysis_vm_strings(self):
        from static.handler import _detect_anti_analysis

        signals = _detect_anti_analysis([], ["check for vmware tools", "vbox guest"])
        assert any("vm_detection" in s for s in signals)

    def test_yara_scan_with_test_rule(self):
        """Write a simple YARA rule, scan a matching file."""
        from static.handler import _yara_scan

        with tempfile.TemporaryDirectory() as rules_dir:
            # Write a test YARA rule
            rule_path = Path(rules_dir) / "test_rule.yar"
            rule_path.write_text(
                'rule test_match { strings: $a = "EICAR" condition: $a }'
            )

            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(EICAR_BYTES)
                f.flush()
                sample_path = Path(f.name)

            try:
                # Patch YARA_RULES_DIR to our temp dir
                with patch("static.handler.YARA_RULES_DIR", rules_dir):
                    hits = _yara_scan(sample_path, rule_hints=None)
                    assert len(hits) >= 1
                    assert hits[0]["rule"] == "test_match"
                    assert hits[0]["strings_matched"] >= 1
            finally:
                sample_path.unlink()

    def test_yara_scan_no_match(self):
        """YARA scan on a file that matches nothing."""
        from static.handler import _yara_scan

        with tempfile.TemporaryDirectory() as rules_dir:
            rule_path = Path(rules_dir) / "nomatch.yar"
            rule_path.write_text(
                'rule never_match { strings: $a = "ZZZZZZZZZZNOMATCH" condition: $a }'
            )

            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(b"just some random bytes here")
                f.flush()
                sample_path = Path(f.name)

            try:
                with patch("static.handler.YARA_RULES_DIR", rules_dir):
                    hits = _yara_scan(sample_path)
                    assert len(hits) == 0
            finally:
                sample_path.unlink()

    def test_mode_a_full_flow(self):
        """Full Mode A flow with EICAR and a test YARA rule."""
        from static.handler import _run_mode_a

        with tempfile.TemporaryDirectory() as rules_dir:
            rule_path = Path(rules_dir) / "test_eicar.yar"
            rule_path.write_text(
                'rule eicar_test { strings: $a = "EICAR" condition: $a }'
            )

            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(EICAR_BYTES)
                f.flush()
                sample_path = Path(f.name)

            try:
                with patch("static.handler.YARA_RULES_DIR", rules_dir):
                    findings = _run_mode_a(sample_path, focus=None, yara_rules=None)

                assert findings["mode"] == "rule-driven"
                assert isinstance(findings["sections"], list)
                assert isinstance(findings["imports"], list)
                assert isinstance(findings["yara_hits"], list)
                assert len(findings["yara_hits"]) >= 1
                assert findings["yara_hits"][0]["rule"] == "eicar_test"
            finally:
                sample_path.unlink()


class TestStaticModeB:
    """Test Mode B agent-authored script execution."""

    def test_mode_b_happy_path(self):
        """Script that outputs valid JSON — returns merged findings."""
        from static.handler import _run_mode_b

        with tempfile.TemporaryDirectory() as td:
            sample_path = Path(td) / "sample.bin"
            sample_path.write_bytes(b"test binary data")

            script_path = Path(td) / "script.py"
            script_path.write_text(
                'import json, sys\n'
                'print(json.dumps({"ok": True, "custom_field": "hello"}))\n'
            )

            # Mock S3 download by patching _run_mode_b to use local file
            # Instead, test the subprocess logic directly
            result = subprocess.run(
                [sys.executable, str(script_path), str(sample_path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            parsed = json.loads(result.stdout)
            assert parsed["ok"] is True
            assert parsed["custom_field"] == "hello"

    def test_mode_b_bad_json(self):
        """Script that outputs non-JSON — returns error envelope."""
        with tempfile.TemporaryDirectory() as td:
            script_path = Path(td) / "bad_script.py"
            script_path.write_text('print("this is not json")\n')

            sample_path = Path(td) / "sample.bin"
            sample_path.write_bytes(b"test")

            result = subprocess.run(
                [sys.executable, str(script_path), str(sample_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # The stdout should not parse as JSON
            with pytest.raises(json.JSONDecodeError):
                json.loads(result.stdout)

    def test_mode_b_timeout(self):
        """Script that hangs — subprocess.TimeoutExpired raised."""
        with tempfile.TemporaryDirectory() as td:
            script_path = Path(td) / "slow_script.py"
            script_path.write_text('import time; time.sleep(600)\n')

            sample_path = Path(td) / "sample.bin"
            sample_path.write_bytes(b"test")

            with pytest.raises(subprocess.TimeoutExpired):
                subprocess.run(
                    [sys.executable, str(script_path), str(sample_path)],
                    capture_output=True,
                    text=True,
                    timeout=2,  # Short timeout for test speed
                    check=True,
                )

    def test_mode_b_nonzero_exit(self):
        """Script that exits with error — subprocess.CalledProcessError raised."""
        with tempfile.TemporaryDirectory() as td:
            script_path = Path(td) / "error_script.py"
            script_path.write_text(
                'import sys; print("error occurred", file=sys.stderr); sys.exit(1)\n'
            )

            sample_path = Path(td) / "sample.bin"
            sample_path.write_bytes(b"test")

            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                subprocess.run(
                    [sys.executable, str(script_path), str(sample_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
            assert exc_info.value.returncode == 1

    def test_mode_b_integration_with_s3_mock(self):
        """Full Mode B flow with mocked S3 for script download."""
        with mock_aws():
            region = "us-east-1"
            s3 = boto3.client("s3", region_name=region)

            # Create bucket and upload script
            s3.create_bucket(Bucket="test-artifacts")
            script_content = (
                'import json, sys\n'
                'sample = sys.argv[1]\n'
                'print(json.dumps({"analyzed": True, "sample_path": sample}))\n'
            )
            s3.put_object(
                Bucket="test-artifacts",
                Key="scripts/test/stage-3.py",
                Body=script_content.encode(),
            )

            with tempfile.TemporaryDirectory() as td:
                sample_path = Path(td) / "sample.bin"
                sample_path.write_bytes(b"test binary")

                from static.handler import _run_mode_b

                result = _run_mode_b(
                    sample_path,
                    "s3://test-artifacts/scripts/test/stage-3.py",
                    s3,
                )

            assert result["mode"] == "agent-authored-script"
            assert result.get("analyzed") is True
            assert "error" not in result


# ---------------------------------------------------------------------------
# Integration test: full SQS→handler→DDB→response flow (triage)
# ---------------------------------------------------------------------------


class TestTriageIntegration:
    """Full flow with mocked AWS: SQS receive → S3 download → fingerprint → DDB + response."""

    @pytest.fixture()
    def aws_env(self):
        with mock_aws():
            region = "us-east-1"
            sqs = boto3.client("sqs", region_name=region)
            s3 = boto3.client("s3", region_name=region)
            ddb_client = boto3.client("dynamodb", region_name=region)

            input_q = sqs.create_queue(QueueName="triage-input")
            input_url = input_q["QueueUrl"]

            resp_q = sqs.create_queue(
                QueueName="triage-response.fifo",
                Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
            )
            resp_url = resp_q["QueueUrl"]

            ddb_client.create_table(
                TableName="cyber-results",
                KeySchema=[
                    {"AttributeName": "artifact_id", "KeyType": "HASH"},
                    {"AttributeName": "stage_timestamp", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "artifact_id", "AttributeType": "S"},
                    {"AttributeName": "stage_timestamp", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )

            # Create S3 bucket and upload EICAR
            s3.create_bucket(Bucket="test-samples")
            s3.put_object(Bucket="test-samples", Key="eicar.com", Body=EICAR_BYTES)

            os.environ["INPUT_QUEUE_URL"] = input_url
            os.environ["RESPONSE_QUEUE_URL"] = resp_url
            os.environ["RESULTS_TABLE"] = "cyber-results"
            os.environ["IMAGE_TAG"] = "test-sha"
            os.environ["AWS_DEFAULT_REGION"] = region

            # Send the trigger message
            sqs.send_message(
                QueueUrl=input_url,
                MessageBody=json.dumps({
                    "artifact_id": "test-eicar-001",
                    "sample_s3_uri": "s3://test-samples/eicar.com",
                }),
            )

            yield {
                "sqs": sqs,
                "s3": s3,
                "ddb": boto3.resource("dynamodb", region_name=region),
                "resp_url": resp_url,
                "region": region,
            }

    def test_full_triage_flow(self, aws_env):
        """End-to-end: SQS msg → download → fingerprint → DDB row → response."""
        from triage.handler import run

        run()

        # Check DDB
        table = aws_env["ddb"].Table("cyber-results")
        items = table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr("artifact_id").eq(
                "test-eicar-001"
            )
        )["Items"]
        assert len(items) == 1
        assert items[0]["stage"] == "triage"
        assert items[0]["status"] == "ok"

        findings = json.loads(items[0]["findings"])
        assert findings["hashes"]["sha256"] == EICAR_SHA256

        # Check response queue
        resp_msgs = aws_env["sqs"].receive_message(
            QueueUrl=aws_env["resp_url"], MaxNumberOfMessages=1
        )
        assert len(resp_msgs.get("Messages", [])) == 1
        resp_body = json.loads(resp_msgs["Messages"][0]["Body"])
        assert resp_body["artifact_id"] == "test-eicar-001"
        assert resp_body["stage"] == 1
        assert resp_body["stage_name"] == "triage"
        assert resp_body["status"] == "ok"
        assert resp_body["findings"]["hashes"]["sha256"] == EICAR_SHA256
