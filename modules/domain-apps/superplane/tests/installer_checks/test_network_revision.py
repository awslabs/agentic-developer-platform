"""Network revision preserves immutable workload and prior installation evidence."""

import copy
from unittest.mock import Mock

import pytest

from installation import network_revision
from installation.config import Refusal, digest, identity
from .test_private_service_egress import configured
from .test_paid_worker import native

__all__ = ["native"]


def inputs(native, monkeypatch):
    env, release = configured(native)
    prior_env = copy.deepcopy(env)
    del prior_env["paid_worker"]["egress"]["interface_endpoints"]
    previous = {
        "status": "failed",
        "public_route_enabled": False,
        "adapter_stage": {"state": "disabled-restored"},
        "lifecycle_foundations": {"retained": True},
        "bootstrap": {"token_secret_removed": True},
        "completed": ["adapter-stage-quiescence"],
        "environment": prior_env,
        "release_lock": copy.deepcopy(release),
        "release_id": digest(release),
        "run_id": "a" * 16,
        "installation_id": identity(env),
        "plan_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        network_revision, "source_proof", lambda revision: {"source": revision}
    )
    return env, release, previous


def test_new_review_links_unchanged_prior_receipt_and_workload(native, monkeypatch):
    env, release, previous = inputs(native, monkeypatch)
    originals = copy.deepcopy((env, release, previous))
    review = network_revision.review_inputs(env, release, previous)
    assert review["prior_run_id"] == previous["run_id"]
    assert review["prior_receipt_sha256"] == digest(previous)
    assert review["release_sha256"] == previous["release_id"]
    assert review["prior_plan_sha256"] == previous["plan_sha256"]
    assert "approved_plan_sha256" not in review
    assert review["environment_sha256"] != review["prior_environment_sha256"]
    assert (env, release, previous) == originals


@pytest.mark.parametrize(
    "fault",
    [
        "release",
        "release-id",
        "cluster",
        "image",
        "database",
        "identity",
        "old-endpoints",
        "active",
        "lock",
        "lock-attempt",
        "cleanup",
        "route",
        "adapter",
        "volume",
        "token",
        "quiescence",
    ],
)
def test_revision_rejects_unrelated_changes_or_unsafe_prior_state(
    native, monkeypatch, fault
):
    env, release, previous = inputs(native, monkeypatch)
    if fault == "release":
        release["source_revision"] = "c" * 40
    elif fault == "release-id":
        previous["release_id"] = "c" * 64
    elif fault == "cluster":
        env["cluster"] += "-other"
    elif fault == "image":
        env["paid_worker"]["image"] = "other"
    elif fault == "database":
        env["paid_worker"]["egress"]["database"]["cidr"] = "10.0.1.6/32"
    elif fault == "identity":
        previous["installation_id"] = "foreign"
    elif fault == "old-endpoints":
        previous["environment"] = copy.deepcopy(env)
    elif fault == "active":
        previous["status"] = "installed-and-verified"
    elif fault == "lock":
        previous["remote_lock"] = {"etag": "held"}
    elif fault == "lock-attempt":
        previous["lock_attempt"] = {"pending": True}
    elif fault == "cleanup":
        previous["temporary_preflight"] = {"cleanup_required": True}
    elif fault == "route":
        previous["public_route_enabled"] = True
    elif fault == "adapter":
        previous["adapter_stage"]["state"] = "active"
    elif fault == "volume":
        previous["lifecycle_foundations"]["retained"] = False
    elif fault == "token":
        previous["bootstrap"]["token_secret_removed"] = False
    elif fault == "quiescence":
        previous["completed"] = []
    with pytest.raises(Refusal):
        network_revision.review_inputs(env, release, previous)


def test_changed_operator_source_is_refused_before_target_access(monkeypatch):
    installer = object.__new__(network_revision.NetworkRevisionInstaller)
    installer.lock = {"source_revision": "a" * 40}
    installer.revision_review = {"source": {"reviewed": True}}
    monkeypatch.setattr(
        network_revision, "source_proof", lambda revision: {"changed": True}
    )
    target = Mock()
    monkeypatch.setattr(network_revision.Installer, "target", target)
    with pytest.raises(Refusal, match="source changed"):
        installer.target()
    target.assert_not_called()


def test_cli_passes_independently_reviewed_new_plan_to_normal_execution(
    monkeypatch, tmp_path
):
    """Environment-bound plans necessarily change; old plan must not be forced."""
    import json
    import sys

    env_path, release_path = tmp_path / "env.json", tmp_path / "release.json"
    env_path.write_text("{}")
    release_path.write_text("{}")
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "receipt.json").write_text("{}")
    output = tmp_path / "new"
    review = {"source": {}, "prior_plan_sha256": "a" * 64}
    monkeypatch.setattr(network_revision, "validate", lambda *args, **kw: None)
    monkeypatch.setattr(network_revision, "review_inputs", lambda *args: review)
    execute = Mock()

    class FakeInstaller:
        def __init__(self, env, release, path):
            self.receipt = {"status": "planned"}
            self.run_id = "new-run"

        def plan(self):
            (output / "receipt.json").write_text(json.dumps(self.receipt))

        def execute(self, plan, token):
            execute(plan, token)
            self.receipt["status"] = "installed-and-verified"

    monkeypatch.setattr(network_revision, "NetworkRevisionInstaller", FakeInstaller)
    monkeypatch.setenv("SUPERPLANE_VERIFICATION_TOKEN", "authenticated")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "network_revision",
            "--environment",
            str(env_path),
            "--release-lock",
            str(release_path),
            "--prior-output",
            str(prior),
            "--output",
            str(output),
            "--execute",
            "--approved-review-sha256",
            digest(review),
            "--approved-plan-sha256",
            "b" * 64,
        ],
    )
    assert network_revision.main() == 0
    execute.assert_called_once_with("b" * 64, "authenticated")
