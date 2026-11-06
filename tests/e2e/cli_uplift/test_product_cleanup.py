"""The remote cleanup caller keeps credentials local and refuses unowned files."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import bundle

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common  # noqa: E402
import product_cleanup  # noqa: E402


def test_reviewed_product_driver_is_shipped_and_required(tmp_path):
    remote = tmp_path / "remote"
    for name, content in bundle.sources():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for source in bundle.PRODUCT_DRIVER_SOURCES:
        assert (remote / source).read_bytes() == (
            bundle.PACKAGE_DIR.parents[2] / source
        ).read_bytes()

    interpreter = [sys.executable, "-S", "-B"]
    command = [
        *interpreter,
        "-c",
        "from tests.e2e.product_onboarding.live import run; assert callable(run)",
    ]
    imported = subprocess.run(
        command, cwd=remote, capture_output=True, text=True, timeout=30
    )
    assert imported.returncode == 0, imported.stderr

    checked = subprocess.run(
        [*interpreter, str(remote / "dispatcher.py"), "--self-check"],
        cwd=remote,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not any(
        key.startswith("product_v01") for key in json.loads(checked.stdout)["problems"]
    )

    (remote / "tests/e2e/product_onboarding/product_recovery.py").unlink()
    refusal = subprocess.run(
        [*interpreter, str(remote / "dispatcher.py"), "--self-check"],
        cwd=remote,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refusal.returncode != 0
    assert "product_v01_driver" in json.loads(refusal.stdout)["problems"]
    assert "product_v01_state" not in json.loads(refusal.stdout)["problems"]

    state = remote / "product_state.py"
    state.unlink()
    refusal = subprocess.run(
        [*interpreter, str(remote / "dispatcher.py"), "--self-check"],
        cwd=remote,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refusal.returncode != 0
    assert "product_v01_state" in json.loads(refusal.stdout)["problems"]

    (remote / "product_http.py").unlink()
    refusal = subprocess.run(
        [*interpreter, str(remote / "dispatcher.py"), "--self-check"],
        cwd=remote,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refusal.returncode != 0
    assert "product_v01_actor" in json.loads(refusal.stdout)["problems"]

    (remote / "product_v01.py").unlink()
    refusal = subprocess.run(
        [*interpreter, str(remote / "dispatcher.py"), "--self-check"],
        cwd=remote,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refusal.returncode != 0
    assert "product_v01_runner" in json.loads(refusal.stdout)["problems"]


@pytest.fixture
def config(tmp_path):
    return {
        "work_dir": str(tmp_path),
        "evaluation_id": "adp-e2e-20261019-000000-abcdef",
        "product_run_id": "product-123456-2",
        "workflow_run_id": "product-123456-2",
        "platform_account": "000000000101",
        "region": "us-east-1",
        "internal_invoke_url": "https://abc123.execute-api.us-east-1.amazonaws.com/dev",
        "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
    }


@pytest.fixture
def manifest(config):
    path = Path(config["work_dir"]) / "product-ownership.json"
    path.write_text(
        json.dumps(
            {
                "run_id": config["product_run_id"],
                "tenant_id": "eval-tenant",
                "actor_id": "owned-actor",
                "source_revision": "a" * 40,
                "idempotency_key": "product-create-originalkey",
            }
        )
    )
    path.chmod(0o600)
    return path


def test_remote_cleanup_uses_private_file_and_temporary_instance_signature(
    config, manifest, monkeypatch
):
    dispatched = []

    def run(argv, *, stdin=None, **kwargs):
        dispatched.append((argv, stdin))
        if argv[0] == "aws":
            return (
                0,
                json.dumps(
                    {
                        "AccessKeyId": "ASIAEXAMPLE",
                        "SecretAccessKey": "secret+/=",
                        "SessionToken": "session+/=",
                    }
                ),
                "",
            )
        return (
            0,
            json.dumps(
                {
                    "run_id": config["product_run_id"],
                    "product_id": "owned-product",
                    "project_id": "owned-project",
                    "fixture_removed": True,
                    "cleanup_revision": 3,
                }
            ),
            "",
        )

    monkeypatch.setattr(common, "bounded", run)
    evidence = {}
    product_cleanup.execute(config, evidence)
    assert evidence["success"] is True and evidence["detail"]["fixture_removed"] is True
    assert "product_cleanup" in bundle.purposes()
    assert dispatched[0][0] == [
        "aws",
        "configure",
        "export-credentials",
        "--format",
        "process",
    ]
    assert (
        dispatched[1][0][-1]
        == config["internal_invoke_url"] + "/internal/v1/product-evaluation/retire"
    )
    assert json.loads(dispatched[1][0][-2]) == json.loads(manifest.read_text())
    assert "ASIAEXAMPLE:secret+/=" in dispatched[1][1]
    assert "session+/=" in dispatched[1][1]
    assert all("secret+/=" not in " ".join(argv) for argv, _ in dispatched)
    assert "secret+/=" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "foreign",
        "permissions",
        "symlink",
        "url",
        "directory",
        "wrong_workflow",
    ],
)
def test_remote_cleanup_refuses_before_credentials_or_request(
    config, manifest, monkeypatch, bad
):
    if bad == "missing":
        manifest.unlink()
    elif bad == "foreign":
        manifest.write_text(json.dumps({"run_id": "other-evaluation"}))
    elif bad == "directory":
        Path(config["work_dir"]).chmod(0o755)
    elif bad == "wrong_workflow":
        config["workflow_run_id"] = "product-123456-3"
    elif bad == "permissions":
        manifest.chmod(0o644)
    elif bad == "symlink":
        manifest.unlink()
        manifest.symlink_to(Path(config["work_dir"]) / "other-file")
    else:
        config["internal_invoke_url"] = "https://foreign.example.invalid"
    monkeypatch.setattr(
        common,
        "bounded",
        lambda *_a, **_kw: pytest.fail("credentials read before ownership check"),
    )
    with pytest.raises((common.RemoteError, OSError, ValueError)):
        product_cleanup.execute(config, {})


@pytest.mark.parametrize(
    "failure",
    [
        "credentials",
        "denied",
        "wrong_run",
        "missing_receipt",
        "unverified",
        "bad_revision",
    ],
)
def test_remote_cleanup_cannot_pass_without_authorized_verified_retirement(
    config, manifest, monkeypatch, failure
):
    calls = []

    def run(argv, *, stdin=None, **kwargs):
        calls.append(argv[0])
        if argv[0] == "aws":
            if failure == "credentials":
                return 1, "", "secret provider failure"
            return (
                0,
                json.dumps(
                    {
                        "AccessKeyId": "ASIAEXAMPLE",
                        "SecretAccessKey": "secret+/=",
                        "SessionToken": "session+/=",
                    }
                ),
                "",
            )
        if failure == "denied":
            return 22, '{"detail":"denied"}', ""
        receipt = {
            "run_id": config["product_run_id"],
            "product_id": "owned-product",
            "project_id": "owned-project",
            "fixture_removed": True,
            "cleanup_revision": 3,
        }
        if failure == "wrong_run":
            receipt["run_id"] = "different-run"
        elif failure == "missing_receipt":
            del receipt["product_id"]
        elif failure == "unverified":
            receipt["fixture_removed"] = False
        elif failure == "bad_revision":
            receipt["cleanup_revision"] = 0
        return 0, json.dumps(receipt), ""

    monkeypatch.setattr(common, "bounded", run)
    evidence = {}
    with pytest.raises((common.RemoteError, KeyError, ValueError)):
        product_cleanup.execute(config, evidence)
    assert not evidence
    assert calls == (["aws"] if failure == "credentials" else ["aws", "curl"])


def test_remote_recovery_refuses_different_durable_intent_before_credentials(
    config, manifest, monkeypatch
):
    record = json.loads(manifest.read_text())
    config["expected_revision"] = record["source_revision"]
    config["product_recovery_intent"] = {
        key: record[key]
        for key in (
            "run_id",
            "tenant_id",
            "actor_id",
            "source_revision",
            "idempotency_key",
        )
    }
    config["product_recovery_intent"]["idempotency_key"] = "product-create-foreign"
    monkeypatch.setattr(
        common,
        "bounded",
        lambda *_args, **_kwargs: pytest.fail(
            "credentials read before ownership check"
        ),
    )
    with pytest.raises(common.RemoteError, match="durable recovery intent"):
        product_cleanup.execute(config, {})
