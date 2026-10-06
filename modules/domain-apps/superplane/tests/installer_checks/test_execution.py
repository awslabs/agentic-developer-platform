import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from installation.config import LABEL, Refusal
from installation.runner import Installer, local_lock


class ToolProcess:
    """Records actual tool argv/stdin and supplies external observations."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def call(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail and self.fail in args:
            raise Refusal("external stage failed")
        if "get-caller-identity" in args:
            value = {
                "Account": "879318057152",
                "Arn": "arn:aws:sts::879318057152:assumed-role/test-installer/fixture",
                "UserId": "AROA" + "A" * 17 + ":fixture",
            }
        elif "get-role" in args:
            value = {
                "Role": {
                    "Arn": "arn:aws:iam::879318057152:role/deployment/test-installer",
                    "RoleId": "AROA" + "A" * 17,
                }
            }
        elif "put-object" in args:
            value = {"ETag": '"conditional-etag"'}
        elif "get-object" in args:
            Path(args[-1]).write_text(json.dumps({"installation_id": "other-machine"}))
            value = {"ETag": '"foreign-etag"'}
        elif "describe-images" in args:
            digest = args[args.index("--image-ids") + 1].split("=", 1)[1]
            value = {"imageDetails": [{"imageDigest": digest, "imageTags": ["a" * 40]}]}
        elif "capabilities" in args:
            value = {
                "capabilities": {
                    "credential_evidence": False,
                    "operation_facade": False,
                }
            }
        elif "inspect" in args:
            value = [
                {"Config": {"Labels": {"org.opencontainers.image.revision": "a" * 40}}}
            ]
        else:
            value = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")


def test_production_capability_refusal_precedes_mutation(
    tmp_path, environment, release
):
    tools = ToolProcess()
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal, match="credential_evidence"):
        installer.images()
    assert all(
        not any(x in args for x in ("apply", "put-object", "put-parameter"))
        for args, _ in tools.calls
    )


def test_global_lock_is_conditional_and_retained_on_failure(
    tmp_path, environment, release
):
    tools = ToolProcess()
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(RuntimeError):
        with installer.exclusive():
            raise RuntimeError("failure")
    put = tools.calls[2][0]
    assert "--if-none-match" in put and "*" in put
    assert len(tools.calls) == 3
    assert installer.receipt["status"] == "recovery-required"
    assert installer.receipt["remote_lock"]["etag"] == '"conditional-etag"'
    assert environment["environment"] + "/modules/superplane/installation.lock" in put


def test_no_mutation_if_other_machine_holds_lock(tmp_path, environment, release):
    tools = ToolProcess(fail="put-object")
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal, match="another attempt"):
        with installer.exclusive():
            pytest.fail("entered a held lock")
    assert len(tools.calls) == 4
    assert "get-object" in tools.calls[-1][0]
    assert "remote_lock" not in installer.receipt
    assert not any("delete-object" in args for args, _ in tools.calls)


def test_same_receipt_cannot_be_used_concurrently(tmp_path):
    with local_lock(tmp_path):
        with pytest.raises(Refusal):
            with local_lock(tmp_path):
                pytest.fail("duplicate installer")


def test_refuse_foreign_kubernetes_object(tmp_path, environment, release, monkeypatch):
    tools = ToolProcess()
    installer = Installer(environment, release, tmp_path, tools)
    monkeypatch.setattr(
        installer, "existing", lambda _: {"metadata": {"labels": {LABEL: "foreign"}}}
    )
    with pytest.raises(Refusal, match="owned"):
        installer.apply([installer.docs[0]])
    assert tools.calls == []


def test_failed_stage_has_durable_non_success_receipt(tmp_path, environment, release):
    installer = Installer(environment, release, tmp_path, ToolProcess())

    def fail():
        raise Refusal("migration failed")

    with pytest.raises(Refusal):
        installer.phase("migration", fail)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["stage"] == "migration"
    assert "migration" not in receipt["completed"]
