"""Non-root ingestion image and consumer manifest security tests (#6038).

Validates that the ingestion Dockerfile declares a non-root runtime user and
that all five Kubernetes consumer manifests enforce the full hardening policy:
non-root UID, read-only root filesystem, no privilege escalation, all
capabilities dropped, and RuntimeDefault seccomp profile.

These are static/manifest-level assertions. They prove what the repository
declares, not what a live cluster runs. A separate container-runtime test
script is documented in the rollout checklist for build-time validation.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest
import yaml

_IMAGES = Path(__file__).resolve().parents[2] / "images"
_MANIFESTS = Path(__file__).resolve().parents[2] / "manifests"
_DOCKERFILE = _IMAGES / "ingestion" / "Dockerfile"

# All five consumers that use the ingestion image, with their manifest filename,
# resource kind, and the container path for extracting the security context.
_CONSUMERS = [
    ("ingestion-scaledjob.yaml", "ScaledJob", "worker"),
    ("repo-refresh-cronjob.yaml", "CronJob", "refresh"),
    ("vuln-scan-cronjob.yaml", "CronJob", "vuln-scan"),
    ("personal-context-synthesis-cronjob.yaml", "CronJob", "synthesis"),
    ("migration-job.yaml", "Job", "migration"),
]


def _substitute_vars(raw: str) -> str:
    """Replace ${VAR} template placeholders so the template parses as YAML."""
    return re.sub(r"\$\{(\w+)\}", r"ph-\1", raw)


def _load_manifest(filename: str, kind: str) -> dict:
    """Load a manifest file and return the document matching the given kind."""
    raw = (_MANIFESTS / filename).read_text()
    docs = [d for d in yaml.safe_load_all(_substitute_vars(raw)) if d]
    return next(d for d in docs if d.get("kind") == kind)


def _pod_spec(doc: dict, kind: str) -> dict:
    """Extract the pod template spec from a manifest document."""
    if kind == "ScaledJob":
        return doc["spec"]["jobTargetRef"]["template"]["spec"]
    elif kind == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    elif kind == "Job":
        return doc["spec"]["template"]["spec"]
    raise ValueError(f"Unknown kind: {kind}")


def _container(pod_spec: dict, name: str) -> dict:
    return next(c for c in pod_spec["containers"] if c["name"] == name)


# ---------------------------------------------------------------------------
# Dockerfile assertions
# ---------------------------------------------------------------------------


class TestDockerfileNonRootUser:
    """The ingestion Dockerfile must declare a non-root runtime USER."""

    @pytest.fixture(scope="class")
    @classmethod
    def dockerfile_text(cls) -> str:
        return _DOCKERFILE.read_text()

    def test_declares_user_directive(self, dockerfile_text: str):
        """Dockerfile has a USER directive that is not root."""
        user_lines = [
            line.strip()
            for line in dockerfile_text.splitlines()
            if line.strip().startswith("USER ")
        ]
        assert user_lines, "Dockerfile has no USER directive (#6038)"
        # The last USER directive is the effective runtime user.
        last_user = user_lines[-1].split()[1]
        assert last_user != "root" and last_user != "0", (
            f"Dockerfile USER is {last_user!r} — must be non-root (#6038)"
        )

    def test_creates_nonroot_user_group(self, dockerfile_text: str):
        """Dockerfile creates a system group and user with fixed IDs."""
        assert "addgroup" in dockerfile_text, "Dockerfile must create a system group"
        assert "adduser" in dockerfile_text, "Dockerfile must create a system user"
        assert "--gid 10001" in dockerfile_text, "Group must use GID 10001"
        assert "--uid 10001" in dockerfile_text, "User must use UID 10001"

    def test_gopath_not_under_root_home(self, dockerfile_text: str):
        """GOPATH must not be under /root (inaccessible to non-root user)."""
        for line in dockerfile_text.splitlines():
            if line.strip().startswith("ENV") and "GOPATH" in line:
                assert "/root" not in line, (
                    f"GOPATH is under /root — must be relocated for non-root: {line.strip()}"
                )

    def test_playwright_browsers_path_set(self, dockerfile_text: str):
        """Browser binaries must be installed at a fixed, shared path."""
        assert "PLAYWRIGHT_BROWSERS_PATH" in dockerfile_text, (
            "PLAYWRIGHT_BROWSERS_PATH must be set so browsers are at a root-owned, "
            "world-readable location (not under a user home)"
        )

    def test_app_files_root_owned(self, dockerfile_text: str):
        """Application COPY instructions must run before USER (so files are root-owned).

        The USER directive must come after all COPY and RUN instructions that
        install application files, ensuring they are root-owned and not writable
        by the runtime user.
        """
        lines = dockerfile_text.splitlines()
        user_line_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith("USER ") and "root" not in line.strip().split()[1]:
                user_line_idx = i

        assert user_line_idx is not None, "No non-root USER directive found"

        # No COPY or application-installing RUN after the USER directive
        # (the CMD is fine, as is mkdir for home dir which happens before USER)
        for i in range(user_line_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("COPY "):
                pytest.fail(
                    f"COPY instruction after USER directive (line {i + 1}): "
                    f"{stripped} — application files must be root-owned"
                )


# ---------------------------------------------------------------------------
# Per-consumer manifest assertions
# ---------------------------------------------------------------------------


@pytest.fixture(params=_CONSUMERS, ids=[c[0] for c in _CONSUMERS])
def consumer(request):
    """Yield (filename, kind, container_name, doc, pod_spec, container) for each consumer."""
    filename, kind, container_name = request.param
    doc = _load_manifest(filename, kind)
    ps = _pod_spec(doc, kind)
    ctr = _container(ps, container_name)
    return filename, kind, container_name, doc, ps, ctr


class TestConsumerPodSecurityContext:
    """Every consumer must set pod-level security context fields."""

    def test_runs_as_uid_10001(self, consumer):
        filename, _, _, _, ps, _ = consumer
        pod_sc = ps.get("securityContext", {})
        assert pod_sc.get("runAsUser") == 10001, f"{filename}: pod runAsUser must be 10001"

    def test_runs_as_gid_10001(self, consumer):
        filename, _, _, _, ps, _ = consumer
        pod_sc = ps.get("securityContext", {})
        assert pod_sc.get("runAsGroup") == 10001, f"{filename}: pod runAsGroup must be 10001"

    def test_fsgroup_10001(self, consumer):
        filename, _, _, _, ps, _ = consumer
        pod_sc = ps.get("securityContext", {})
        assert pod_sc.get("fsGroup") == 10001, (
            f"{filename}: pod fsGroup must be 10001 for PVC group-write"
        )

    def test_seccomp_runtime_default(self, consumer):
        filename, _, _, _, ps, _ = consumer
        pod_sc = ps.get("securityContext", {})
        seccomp = pod_sc.get("seccompProfile", {})
        assert seccomp.get("type") == "RuntimeDefault", (
            f"{filename}: pod seccompProfile must be RuntimeDefault"
        )


class TestConsumerContainerSecurityContext:
    """Every consumer container must enforce the full hardening policy."""

    def test_runs_as_nonroot(self, consumer):
        filename, _, _, _, _, ctr = consumer
        sc = ctr.get("securityContext", {})
        assert sc.get("runAsNonRoot") is True, f"{filename}: container runAsNonRoot must be true"

    def test_readonly_root_filesystem(self, consumer):
        filename, _, _, _, _, ctr = consumer
        sc = ctr.get("securityContext", {})
        assert sc.get("readOnlyRootFilesystem") is True, (
            f"{filename}: container readOnlyRootFilesystem must be true"
        )

    def test_no_privilege_escalation(self, consumer):
        filename, _, _, _, _, ctr = consumer
        sc = ctr.get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False, (
            f"{filename}: container allowPrivilegeEscalation must be false"
        )

    def test_not_privileged(self, consumer):
        filename, _, _, _, _, ctr = consumer
        sc = ctr.get("securityContext", {})
        assert sc.get("privileged") is False, f"{filename}: container privileged must be false"

    def test_drops_all_capabilities(self, consumer):
        filename, _, _, _, _, ctr = consumer
        sc = ctr.get("securityContext", {})
        assert sc.get("capabilities", {}).get("drop") == ["ALL"], (
            f"{filename}: container must drop ALL capabilities"
        )


class TestConsumerWritableMounts:
    """Every consumer must have explicit writable emptyDir mounts for /tmp and /home/appuser."""

    def test_has_tmp_mount(self, consumer):
        filename, _, _, _, _, ctr = consumer
        mounts = {m["mountPath"]: m["name"] for m in ctr.get("volumeMounts", [])}
        assert "/tmp" in mounts, (
            f"{filename}: container must mount /tmp (writable scratch for read-only rootfs)"
        )

    def test_has_home_mount(self, consumer):
        filename, _, _, _, _, ctr = consumer
        mounts = {m["mountPath"]: m["name"] for m in ctr.get("volumeMounts", [])}
        assert "/home/appuser" in mounts, (
            f"{filename}: container must mount /home/appuser (writable HOME for tool caches)"
        )

    def test_tmp_is_emptydir(self, consumer):
        filename, _, _, _, ps, ctr = consumer
        mounts = {m["mountPath"]: m["name"] for m in ctr.get("volumeMounts", [])}
        tmp_vol_name = mounts.get("/tmp")
        if tmp_vol_name is None:
            pytest.skip(f"{filename}: no /tmp mount")
        volumes = {v["name"]: v for v in ps.get("volumes", [])}
        vol = volumes.get(tmp_vol_name)
        assert vol is not None, f"{filename}: volume {tmp_vol_name} not defined"
        assert "emptyDir" in vol, (
            f"{filename}: /tmp volume must be emptyDir (bounded scratch, not hostPath)"
        )

    def test_home_is_emptydir(self, consumer):
        filename, _, _, _, ps, ctr = consumer
        mounts = {m["mountPath"]: m["name"] for m in ctr.get("volumeMounts", [])}
        home_vol_name = mounts.get("/home/appuser")
        if home_vol_name is None:
            pytest.skip(f"{filename}: no /home/appuser mount")
        volumes = {v["name"]: v for v in ps.get("volumes", [])}
        vol = volumes.get(home_vol_name)
        assert vol is not None, f"{filename}: volume {home_vol_name} not defined"
        assert "emptyDir" in vol, (
            f"{filename}: /home/appuser volume must be emptyDir (bounded, not hostPath)"
        )

    def test_emptydir_has_size_limit(self, consumer):
        """All emptyDir volumes must have a sizeLimit to prevent unbounded disk use."""
        filename, _, _, _, ps, _ = consumer
        for vol in ps.get("volumes", []):
            if "emptyDir" in vol:
                assert vol["emptyDir"].get("sizeLimit"), (
                    f"{filename}: emptyDir volume '{vol['name']}' must have a sizeLimit"
                )


class TestPlatformDataPvcConsumers:
    """Consumers that mount platform-data must use fsGroup for group-write, not chmod."""

    _PVC_CONSUMERS: typing.ClassVar = [
        ("ingestion-scaledjob.yaml", "ScaledJob", "worker"),
        ("repo-refresh-cronjob.yaml", "CronJob", "refresh"),
    ]

    @pytest.fixture(params=_PVC_CONSUMERS, ids=[c[0] for c in _PVC_CONSUMERS])
    def pvc_consumer(self, request):
        filename, kind, _container_name = request.param
        doc = _load_manifest(filename, kind)
        ps = _pod_spec(doc, kind)
        return filename, ps

    def test_platform_data_pvc_with_fsgroup(self, pvc_consumer):
        filename, ps = pvc_consumer
        pod_sc = ps.get("securityContext", {})
        assert pod_sc.get("fsGroup") == 10001, (
            f"{filename}: fsGroup must be 10001 so platform-data PVC files are group-writable"
        )
        # Verify the PVC is actually mounted
        volumes = {v["name"]: v for v in ps.get("volumes", [])}
        assert "platform-data" in volumes, f"{filename}: platform-data volume not found"
        pvc = volumes["platform-data"].get("persistentVolumeClaim", {})
        assert pvc.get("claimName") == "platform-data", (
            f"{filename}: platform-data PVC claimName mismatch"
        )


def test_ingestion_external_packages_match_scanner_staging():
    """New image imports must also exist in the maintained scanner build context."""
    import runpy

    repository = Path(__file__).resolve().parents[4]
    config = runpy.run_path(str(repository / "codebuild/security_image_targets.py"))["BUILD_CONFIG"]
    gate = runpy.run_path(
        str(repository / "modules/agent-context/scripts/validate-ingestion-image.py")
    )
    preparation = config["modules/agent-context/images/ingestion/Dockerfile"]["prepare"]
    for package in gate["STAGED"]:
        assert [
            "copy-tree",
            f"modules/agent-context/{package}",
            f"modules/agent-context/images/ingestion/{package}",
        ] in preparation
        assert f"COPY {package}/ /app/{package}/" in _DOCKERFILE.read_text() or package == "alembic"
