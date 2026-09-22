"""Pod-security policy tests for the cyber worker ScaledJobs (issue #5616).

Runtime isolation is a property of the deployed manifests, so it regresses
silently: nothing fails, the pods just run with more privilege than intended.
These tests assert the required settings on the rendered manifests, so removing
one breaks the build instead of quietly widening the blast radius.

Scope note: this checks the two cyber worker ScaledJobs only. Agent-worker and
CI roles belong to other work packages.
"""

from pathlib import Path

import pytest
import yaml

K8S_DIR = Path(__file__).resolve().parents[2] / "k8s"

MANIFESTS = {
    "static": K8S_DIR / "cyber-static-scaledjob.yaml",
    "triage": K8S_DIR / "cyber-triage-scaledjob.yaml",
}

# Deploy-time substitutions performed by .github/workflows/cyber-k8s-deploy.yml.
# Tests render the manifests the same way the pipeline does, so a placeholder
# the pipeline forgets to substitute is visible here too.
SUBSTITUTIONS = {
    "REPLACE_WITH_CYBER_WORKER_IMAGE": "1234.dkr.ecr.us-east-1.amazonaws.com/adp-cyber-worker:latest",
    "REPLACE_WITH_TRIAGE_TASKS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1234/triage-tasks.fifo",
    "REPLACE_WITH_TRIAGE_RESPONSES_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1234/triage-resp.fifo",
    "REPLACE_WITH_STATIC_TASKS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1234/static-tasks.fifo",
    "REPLACE_WITH_STATIC_RESPONSES_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1234/static-resp.fifo",
    "REPLACE_WITH_ALLOWED_SAMPLE_BUCKETS": "adp-dev-chat-artifacts,adp-dev-cape-assets",
}


def _render(path: Path) -> list:
    text = path.read_text()
    for placeholder, value in SUBSTITUTIONS.items():
        text = text.replace(placeholder, value)
    return [doc for doc in yaml.safe_load_all(text) if doc]


def _pod_spec(path: Path) -> dict:
    for doc in _render(path):
        if doc.get("kind") == "ScaledJob":
            return doc["spec"]["jobTargetRef"]["template"]["spec"]
    raise AssertionError(f"no ScaledJob in {path.name}")


def _worker_container(path: Path) -> dict:
    """The analysis container (not the rule-fetch initContainer)."""
    containers = _pod_spec(path)["containers"]
    assert len(containers) == 1, "expected exactly one worker container"
    return containers[0]


@pytest.fixture(params=sorted(MANIFESTS), ids=sorted(MANIFESTS))
def worker(request):
    path = MANIFESTS[request.param]
    return request.param, path


class TestNoPlaceholderReachesTheCluster:
    def test_every_placeholder_is_substituted(self, worker):
        """An unsubstituted placeholder is a silent outage.

        REPLACE_WITH_ALLOWED_SAMPLE_BUCKETS would be parsed as a bucket name, so
        the worker would deny every read and the pipeline would fail closed with
        no obvious cause.
        """
        _, path = worker
        rendered = path.read_text()
        for placeholder, value in SUBSTITUTIONS.items():
            rendered = rendered.replace(placeholder, value)
        assert "REPLACE_WITH_" not in rendered, (
            "manifest has a placeholder the deploy pipeline does not substitute"
        )


class TestContainerPrivilege:
    def test_no_privilege_escalation(self, worker):
        _, path = worker
        sc = _worker_container(path).get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False
        assert sc.get("privileged") is not True

    def test_all_capabilities_dropped(self, worker):
        """Analysis reads a file and runs parsers; it needs no capabilities."""
        _, path = worker
        sc = _worker_container(path).get("securityContext", {})
        assert sc.get("capabilities", {}).get("drop") == ["ALL"]

    def test_root_filesystem_is_read_only(self, worker):
        _, path = worker
        sc = _worker_container(path).get("securityContext", {})
        assert sc.get("readOnlyRootFilesystem") is True

    def test_runs_as_a_non_root_user(self, worker):
        """Asserted in the manifest, not just the image.

        The image declares USER 1001, but relying on that alone means the
        guarantee depends on an image rebuild nobody re-checks.
        """
        _, path = worker
        sc = _pod_spec(path).get("securityContext", {})
        assert sc.get("runAsNonRoot") is True
        assert sc.get("runAsUser") == 1001
        assert sc.get("runAsUser") != 0

    def test_default_seccomp_profile_is_applied(self, worker):
        _, path = worker
        sc = _pod_spec(path).get("securityContext", {})
        assert sc.get("seccompProfile", {}).get("type") == "RuntimeDefault"


class TestReadOnlyRootIsActuallyWorkable:
    """A read-only root that breaks the worker would just get reverted."""

    def test_writable_scratch_is_mounted(self, worker):
        _, path = worker
        mounts = {m["mountPath"] for m in _worker_container(path).get("volumeMounts", [])}
        assert "/tmp" in mounts, "no writable /tmp — tempfile would fail"

    def test_scratch_is_size_limited(self, worker):
        """A hostile sample that inflates on unpack must not fill the node."""
        _, path = worker
        volumes = {v["name"]: v for v in _pod_spec(path).get("volumes", [])}
        scratch = [
            v for name, v in volumes.items() if name == "scratch"
        ]
        assert scratch, "no scratch volume defined"
        assert scratch[0].get("emptyDir", {}).get("sizeLimit"), "scratch has no sizeLimit"

    def test_home_is_writable(self, worker):
        """HOME must point at the writable volume, not the read-only root.

        Required unconditionally rather than "if HOME is set": with a read-only
        root, any library that caches under HOME (magika's model, for one) fails
        on write, and the image's default HOME is not writable by uid 1001. An
        `if HOME in env` guard would make this test pass on exactly the manifest
        that breaks in production.
        """
        _, path = worker
        env = {e["name"]: e.get("value") for e in _worker_container(path).get("env", [])}
        assert "HOME" in env, "HOME not redirected to a writable path"
        assert env["HOME"].startswith("/tmp")


class TestSampleBucketAllowlistIsConfigured:
    def test_allowed_buckets_env_is_present(self, worker):
        """Without this the worker denies everything; with a wrong value it
        would widen what a bypassed code check could reach."""
        _, path = worker
        env = {e["name"]: e.get("value") for e in _worker_container(path).get("env", [])}
        assert "CYBER_ALLOWED_BUCKETS" in env
        assert env["CYBER_ALLOWED_BUCKETS"].strip()


class TestInitContainerIsAlsoConstrained:
    """The init container shares the pod's identity, so it is in scope too."""

    def test_init_containers_drop_capabilities(self):
        spec = _pod_spec(MANIFESTS["static"])
        for container in spec.get("initContainers", []):
            sc = container.get("securityContext", {})
            assert sc.get("allowPrivilegeEscalation") is False, container["name"]
            assert sc.get("capabilities", {}).get("drop") == ["ALL"], container["name"]

    def test_init_container_can_write_its_home(self):
        """Inherits runAsNonRoot; aws-cli defaults HOME=/root, unwritable for 1001.

        If this regresses the YARA rule fetch fails and the static worker starts
        with no rules — a silent loss of detection, not a crash.
        """
        spec = _pod_spec(MANIFESTS["static"])
        for container in spec.get("initContainers", []):
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}
            assert env.get("HOME", "").startswith("/tmp"), container["name"]
            assert "/tmp" in mounts, container["name"]


class TestEgressRemainsRestricted:
    """Pre-existing invariant; asserted so this change cannot relax it."""

    def test_network_policy_denies_public_egress(self, worker):
        name, path = worker
        policies = [d for d in _render(path) if d.get("kind") == "NetworkPolicy"]
        assert policies, f"no NetworkPolicy alongside {name} worker"
        for policy in policies:
            for rule in policy["spec"].get("egress", []):
                for target in rule.get("to", []):
                    cidr = target.get("ipBlock", {}).get("cidr", "")
                    assert not cidr.startswith("0.0.0.0/0"), "public egress allowed"
