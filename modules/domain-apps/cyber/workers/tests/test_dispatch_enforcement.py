"""No-unguarded-dispatch tests for both cyber workers (issue #5616).

The most likely way this fix fails is being wired into one execution/download
path and missed in another, leaving the original exposure open while the issue
looks resolved. These tests enumerate the dispatch modes and assert, for each,
that a refused job performs **no fetch at all** — asserting the absence of the
storage call, not merely that an error came back.

Dispatch modes covered:
  triage                      — sample download
  static Mode A (rule-driven) — sample download
  static Mode B (script)      — sample download + script download + execution
"""

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _import_handler(module_name: str):
    """Import a worker handler for authorization testing.

    The triage handler imports ``magic`` (libmagic) at module scope. libmagic is
    a native library that is present in the worker image but not necessarily on
    a developer machine, and an authorization regression test must not be
    silently unrunnable because an unrelated *analysis* dependency is absent —
    that is how a security test quietly stops protecting anything.

    So if (and only if) the real library is missing, stand in a stub for the
    duration of the import. The tests below refuse jobs before any analysis code
    runs, so nothing here depends on libmagic's behaviour. The stub is then
    removed and the module dropped from ``sys.modules``, so other test files in
    the same session re-import against the real dependency and cannot be handed
    a stubbed one.
    """
    stubbed = []
    for dependency in ("magic",):
        if dependency in sys.modules:
            continue
        try:
            importlib.import_module(dependency)
        except ImportError:
            sys.modules[dependency] = types.ModuleType(dependency)
            stubbed.append(dependency)
    try:
        return importlib.reload(importlib.import_module(module_name))
    finally:
        for dependency in stubbed:
            sys.modules.pop(dependency, None)
        sys.modules.pop(module_name, None)

BUCKET = "adp-dev-chat-artifacts"
TENANT = "o/acme/t/team-a/u/user-1"
IDENTITY = {"org_id": "acme", "team_id": "team-a", "user_id": "user-1"}

# A different tenant's object — the cross-tenant read from finding #4730.
VICTIM_URI = f"s3://{BUCKET}/o/victim-org/t/team-x/u/user-9/in/confidential.bin"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CYBER_ALLOWED_BUCKETS", BUCKET)
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.test/in")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.test/out")
    monkeypatch.setenv("RESULTS_TABLE", "test-results")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("CYBER_SCRIPT_PREFIXES", raising=False)


def _message(body: dict) -> dict:
    return {"Messages": [{"Body": json.dumps(body), "ReceiptHandle": "rh-1"}]}


class _Harness:
    """Runs a worker's run() with AWS clients replaced by mocks."""

    def __init__(self, module):
        self.module = module
        self.s3 = MagicMock()
        self.sqs = MagicMock()
        self.table = MagicMock()

    def run(self, body: dict):
        self.sqs.receive_message.return_value = _message(body)
        ddb = MagicMock()
        ddb.Table.return_value = self.table

        def _client(name, **kwargs):
            return {"sqs": self.sqs, "s3": self.s3}[name]

        with patch.object(self.module.boto3, "client", side_effect=_client), patch.object(
            self.module.boto3, "resource", return_value=ddb
        ):
            self.module.run()
        return self

    @property
    def downloads(self):
        return self.s3.download_file.call_args_list

    def sent_envelope(self) -> dict:
        assert self.sqs.send_message.called, "worker sent no response"
        return json.loads(self.sqs.send_message.call_args.kwargs["MessageBody"])

    def ddb_item(self) -> dict:
        assert self.table.put_item.called, "worker wrote no result row"
        return self.table.put_item.call_args.kwargs["Item"]


@pytest.fixture
def triage():
    return _Harness(_import_handler("triage.handler"))


@pytest.fixture
def static():
    return _Harness(_import_handler("static.handler"))


# ---------------------------------------------------------------------------
# Cross-tenant sample — every mode must refuse before fetching
# ---------------------------------------------------------------------------

CROSS_TENANT_JOBS = {
    "triage": {"artifact_id": "a1", "sample_s3_uri": VICTIM_URI, **IDENTITY},
    "static_mode_a": {
        "artifact_id": "a1",
        "sample_s3_uri": VICTIM_URI,
        "focus": ["config_block_extraction"],
        **IDENTITY,
    },
    "static_mode_b": {
        "artifact_id": "a1",
        "sample_s3_uri": VICTIM_URI,
        "script_s3_uri": f"s3://{BUCKET}/{TENANT}/scripts/a1/s.py",
        "script_sha256": "0" * 64,
        **IDENTITY,
    },
}


@pytest.mark.parametrize("mode", ["triage"])
def test_triage_refuses_cross_tenant_sample_without_fetching(triage, mode):
    triage.run(CROSS_TENANT_JOBS[mode])
    assert triage.downloads == [], "worker fetched another tenant's object"
    assert triage.sent_envelope()["status"] == "failed"


@pytest.mark.parametrize("mode", ["static_mode_a", "static_mode_b"])
def test_static_refuses_cross_tenant_sample_without_fetching(static, mode):
    static.run(CROSS_TENANT_JOBS[mode])
    assert static.downloads == [], "worker fetched another tenant's object"
    assert static.sent_envelope()["status"] == "failed"


# ---------------------------------------------------------------------------
# Missing / untrusted identity
# ---------------------------------------------------------------------------


def test_triage_refuses_job_without_identity(triage):
    """Old-format jobs carry no identity and are refused by design."""
    triage.run({"artifact_id": "a1", "sample_s3_uri": f"s3://{BUCKET}/{TENANT}/in/s.bin"})
    assert triage.downloads == []
    assert triage.sent_envelope()["findings"]["reason"] == "identity_missing"


def test_static_refuses_job_without_identity(static):
    static.run({"artifact_id": "a1", "sample_s3_uri": f"s3://{BUCKET}/{TENANT}/in/s.bin"})
    assert static.downloads == []
    assert static.sent_envelope()["findings"]["reason"] == "identity_missing"


def test_static_refuses_bucket_outside_configuration(static):
    static.run(
        {
            "artifact_id": "a1",
            "sample_s3_uri": f"s3://attacker-bucket/{TENANT}/in/s.bin",
            **IDENTITY,
        }
    )
    assert static.downloads == []
    assert static.sent_envelope()["findings"]["reason"] == "bucket_not_allowed"


# ---------------------------------------------------------------------------
# Mode B: script location and registration
# ---------------------------------------------------------------------------


def test_mode_b_refuses_cross_tenant_script_without_fetching(static):
    """A script location cannot escape the requester's own space either."""
    static.run(
        {
            "artifact_id": "a1",
            "sample_s3_uri": f"s3://{BUCKET}/{TENANT}/in/s.bin",
            "script_s3_uri": f"s3://{BUCKET}/o/victim-org/t/t/u/u/scripts/x.py",
            "script_sha256": "0" * 64,
            **IDENTITY,
        }
    )
    assert static.downloads == [], "worker fetched a cross-tenant script"
    assert static.sent_envelope()["findings"]["reason"] == "outside_tenant_prefix"


def test_mode_b_refuses_script_outside_script_prefix(static):
    """An uploaded object inside the tenant's own space is not executable."""
    static.run(
        {
            "artifact_id": "a1",
            "sample_s3_uri": f"s3://{BUCKET}/{TENANT}/in/s.bin",
            "script_s3_uri": f"s3://{BUCKET}/{TENANT}/in/uploaded.py",
            "script_sha256": "0" * 64,
            **IDENTITY,
        }
    )
    assert static.downloads == []
    assert static.sent_envelope()["findings"]["reason"] == "script_prefix_not_allowed"


def test_mode_b_unregistered_script_is_not_executed(static, tmp_path, monkeypatch):
    """Reaches download (location is fine) but must not execute."""
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"python_packages": {}, "system_binaries": {}}))
    monkeypatch.setenv("WORKER_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv(
        "CYBER_VALIDATOR_PATH",
        str(
            Path(__file__).resolve().parents[2]
            / "agent"
            / "skills"
            / "stage-3-static"
            / "validate_script.py"
        ),
    )

    def _fake_download(bucket, key, dest):
        Path(dest).write_bytes(b"print('hello')\n")

    static.s3.download_file.side_effect = _fake_download

    # Patch the module object the harness holds, not a path string: each harness
    # imports its own fresh handler instance, so a string target would patch a
    # different object than the one under test and the assertion would pass
    # regardless of behaviour.
    with patch.object(static.module.subprocess, "run") as spawned:
        static.run(
            {
                "artifact_id": "a1",
                "sample_s3_uri": f"s3://{BUCKET}/{TENANT}/in/s.bin",
                "script_s3_uri": f"s3://{BUCKET}/{TENANT}/scripts/a1/s.py",
                # No script_sha256 — never registered.
                **IDENTITY,
            }
        )
        assert not spawned.called, "unregistered script was executed"

    envelope = static.sent_envelope()
    assert envelope["status"] == "failed"
    assert envelope["findings"]["reason"] == "script_registration_missing"


# ---------------------------------------------------------------------------
# Refusals must not echo the rejected location
# ---------------------------------------------------------------------------


# Each of these asserts the refusal happened FIRST. Without that assertion the
# test passes against the unfixed worker: a handler that cheerfully reads the
# victim's object also "leaks no key names", so a bare no-leak check would be
# satisfied by the exact vulnerability it is meant to detect.


def test_refusal_does_not_leak_rejected_location(static):
    static.run(CROSS_TENANT_JOBS["static_mode_a"])
    envelope = static.sent_envelope()
    assert envelope["status"] == "failed"
    assert envelope["findings"]["reason"] == "outside_tenant_prefix"

    rendered = json.dumps(envelope) + json.dumps(static.ddb_item(), default=str)
    for fragment in ("victim-org", "team-x", "user-9", "confidential", VICTIM_URI):
        assert fragment not in rendered, f"refusal leaked {fragment!r}"


def test_triage_refusal_does_not_leak_rejected_location(triage):
    triage.run(CROSS_TENANT_JOBS["triage"])
    envelope = triage.sent_envelope()
    assert envelope["status"] == "failed"
    assert envelope["findings"]["reason"] == "outside_tenant_prefix"

    rendered = json.dumps(envelope) + json.dumps(triage.ddb_item(), default=str)
    for fragment in ("victim-org", "team-x", "user-9", "confidential", VICTIM_URI):
        assert fragment not in rendered, f"refusal leaked {fragment!r}"


# ---------------------------------------------------------------------------
# Happy path still works through the full dispatch
# ---------------------------------------------------------------------------


def test_static_mode_a_own_sample_is_analysed(static, monkeypatch):
    """The legitimate path must survive all of the above."""
    monkeypatch.setattr(
        static.module, "_run_mode_a", lambda *a, **k: {"mode": "rule-driven", "yara_hits": []}
    )
    static.s3.download_file.side_effect = lambda b, k, d: Path(d).write_bytes(b"MZ")
    static.run(
        {
            "artifact_id": "a1",
            "sample_s3_uri": f"s3://{BUCKET}/{TENANT}/s/s1/t1/in/s.bin",
            **IDENTITY,
        }
    )
    assert len(static.downloads) == 1, "legitimate sample was not fetched"
    envelope = static.sent_envelope()
    assert envelope["status"] == "ok"
    assert envelope["findings"]["mode"] == "rule-driven"
