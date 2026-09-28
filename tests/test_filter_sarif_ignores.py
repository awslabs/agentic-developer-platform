"""CLI and scanner integration preserve raw evidence before any exclusions."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types
import yaml

ROOT = Path(__file__).parents[1]
FILTER = ROOT / "codebuild/filter-sarif-ignores.py"


def test_cli_keeps_raw_bytes_and_scoped_summary(tmp_path):
    rid = "CVE-2026-1234-stdlib"
    source = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {
                                "id": rid,
                                "help": {"text": "Package: stdlib\nType: go-module\n"},
                            }
                        ]
                    }
                },
                "results": [{"ruleId": rid}, {"ruleId": "CVE-2026-9999-other"}],
            }
        ]
    }
    p = tmp_path / "input.json"
    p.write_bytes(json.dumps(source, indent=2).replace("\n", "\r\n").encode())
    raw = tmp_path / "raw.json"
    summary = tmp_path / "summary.json"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "ignore": [
                    {
                        "vulnerability": "CVE-2026-1234",
                        "package": {"name": "stdlib", "type": "go-module"},
                    }
                ]
            }
        )
    )
    original = p.read_bytes()
    subprocess.run(
        [
            sys.executable,
            str(FILTER),
            "--sarif",
            str(p),
            "--config",
            str(config),
            "--output",
            str(p),
            "--raw-output",
            str(raw),
            "--summary-output",
            str(summary),
        ],
        check=True,
    )
    assert raw.read_bytes() == original
    assert len(json.loads(p.read_text())["runs"][0]["results"]) == 1
    assert json.loads(summary.read_text())["total_suppressed"] == 1


def test_scanner_bypasses_internal_ignores(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "codebuild"))
    spec = importlib.util.spec_from_file_location(
        "scan_raw", ROOT / "codebuild/scan_security_images.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    (tmp_path / ".grype.yaml").write_text(
        "ignore:\n- vulnerability: CVE-2026-1234\nonly-fixed: true\n"
    )
    output = tmp_path / "result.sarif"
    calls = []
    monkeypatch.setenv("GRYPE_IGNORE_WONTFIX", "true")
    monkeypatch.setenv("GRYPE_ONLY_FIXED", "true")

    def command(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(stdout="sha256:" + "1" * 64)
        if args[0] == "grype":
            descriptor_path = Path(next(a[5:] for a in args if a.startswith("json=")))
            db_path = tmp_path / "test.db"
            db_path.write_bytes(b"test database")
            descriptor_path.write_text(json.dumps({"descriptor": {
                "name": "grype", "version": "0.119.0", "timestamp": "2026-09-25T15:00:00Z",
                "configuration": {"ignore": [], "match": {}},
                "db": {"status": {"schemaVersion": "v6.1.9", "built": "2026-09-25T06:00:00Z",
                                  "valid": True, "path": str(db_path)},
                       "providers": {"nvd": {"input": "xxh64:1234"}}}}}))
            config = yaml.safe_load(Path(args[-1]).read_text())
            assert config["ignore"] == [] and config["only-fixed"] is False
            assert (
                "GRYPE_ONLY_FIXED" not in kwargs["env"]
                and "GRYPE_IGNORE_WONTFIX" not in kwargs["env"]
            )
            kwargs["stdout"].write(
                '{"runs":[{"results":[{"ruleId":"CVE-2026-1234"}]}]}'
            )
        return types.SimpleNamespace(stdout="")

    monkeypatch.setattr(m, "command", command)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: None)
    assert (
        m.scan(
            {"image": "example/image@sha256:" + "1" * 64, "name": "example"},
            "grype",
            output,
            tmp_path,
        )
        == "sha256:" + "1" * 64
    )
    assert any("--raw-output" in args for args in calls)


def test_retired_policy_preserves_every_original_selector():
    """Policy retirement must retain the complete original review population."""
    import hashlib

    review = ROOT / "docs/security/runs/2026-09-25/grype-ignore-review"
    ledger = json.loads((review / "decisions.json").read_text())
    historical = (review / "historical-config.txt").read_bytes()
    assert hashlib.sha256(historical).hexdigest() == ledger["original_config_sha256"]
    rules = yaml.safe_load(historical)["ignore"]
    assert len(rules) == len(ledger["records"]) == 189
    assert [r["configured_rule"] for r in ledger["records"]] == rules
    assert [r["selector"] for r in ledger["records"]] == [
        f"grype-ignore|.grype.yaml|entry={i}" for i in range(189)
    ]
    assert all(r["reasons"] and not r["risk_accepted"] for r in ledger["records"])
    assert yaml.safe_load((ROOT / ".grype.yaml").read_text())["ignore"] == []
