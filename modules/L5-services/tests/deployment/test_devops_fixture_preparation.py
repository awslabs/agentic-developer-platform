"""Private preparation preserves pinned inputs and never applies or submits work."""

import base64
import importlib.util
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("prepare_devops_fixtures", ROOT / "modules/L5-services/scripts/prepare-devops-fixtures.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


@pytest.fixture
def inputs(monkeypatch):
    calls = []
    sources = {"index.mjs": b"export async function handler() { return {batchItemFailures: []}; }\n",
               "main.tf": b'resource "aws_s3_bucket" "receipts" { bucket = var.bucket_name }\n'}

    def run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["git", "-C", "/selected/repository"]
        assert command[3] == "show"
        return SimpleNamespace(stdout=sources[command[4].split(":")[1]])

    monkeypatch.setattr(prepare.subprocess, "run", run)
    config = {"schema_version": 1, "account_id": "000000000000", "region": "us-east-1",
              "queue": {"repository": "/selected/repository", "commit": "1" * 40, "fixture_id": "1" * 16},
              "archive": {"repository": "/selected/repository", "commit": "2" * 40, "fixture_id": "2" * 16}}
    return config, calls, sources


def test_prepare_pins_artifacts_account_and_phases_without_cloud_calls(tmp_path, inputs):
    config, calls, sources = inputs
    output = tmp_path / "private"
    manifest = prepare.prepare(config, output)
    assert manifest["aws_calls"] == manifest["native_submissions"] == 0
    assert manifest["deployed"] is False
    assert len(calls) == 2 and all(command[0] == "git" for command in calls)
    assert (output / "archive/main.tf").read_bytes() == sources["main.tf"]
    artifact = (output / "queue/worker.zip").read_bytes()
    assert base64.b64encode(sha256(artifact).digest()).decode() == manifest["queue"]["code_sha256_base64"]
    for name, digest in manifest["files"].items():
        assert sha256((output / name).read_bytes()).hexdigest() == digest
    for phase in ("bootstrap", "sealed", "active"):
        values = json.loads((output / f"queue/{phase}.tfvars.json").read_text())
        assert values["phase"] == phase and values["source_commit"] == "1" * 40
        assert values["aws_account_id"] == config["account_id"]
    provider = json.loads((output / "archive/provider.tf.json").read_text())
    assert provider["provider"]["aws"]["allowed_account_ids"] == [config["account_id"]]
    assert json.loads((output / "queue/backend.json").read_text())["path"] == str(output / "queue/terraform.tfstate")


def test_existing_bundle_and_state_are_never_replaced(tmp_path, inputs):
    config, _, _ = inputs
    output = tmp_path / "private"
    output.mkdir()
    (output / "terraform.tfstate").write_text("retained state")
    with pytest.raises(FileExistsError):
        prepare.prepare(config, output)
    assert (output / "terraform.tfstate").read_text() == "retained state"


@pytest.mark.parametrize("invalid", ["main", "latest", "1" * 39, "1" * 40 + ":index.mjs"])
def test_mutable_or_ambiguous_source_is_rejected(tmp_path, inputs, invalid):
    config, calls, _ = inputs
    config["queue"]["commit"] = invalid
    with pytest.raises(ValueError, match="immutable"):
        prepare.prepare(config, tmp_path / "private")
    assert not calls and not (tmp_path / "private").exists()


def test_shared_identity_and_extra_configuration_are_rejected(tmp_path, inputs):
    config, calls, _ = inputs
    config["archive"]["fixture_id"] = config["queue"]["fixture_id"]
    with pytest.raises(ValueError, match="distinct"):
        prepare.prepare(config, tmp_path / "private")
    config["credentials"] = "not accepted"
    with pytest.raises(ValueError, match="exact"):
        prepare.prepare(config, tmp_path / "private")
    assert not calls
