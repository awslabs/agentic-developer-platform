"""The platform-monitor runtime image keeps the small package surface #6124 established.

Issue #6124 (parent #5599). The platform monitor image reported 41 post-filter
vulnerability occurrences (1 critical, 18 high). The runtime base was Alpine 3.19, which
reached end-of-life on 2025-11-01 and no longer receives security updates. Upgrading to
Alpine 3.24, switching to the ECR public registry path, and pinning a numeric non-root UID
resolves OS-level findings and matches the controller's security posture established by S01.

## Why this file exists

The monitor Dockerfile is simple — a Go binary on a minimal Alpine — but that simplicity
can be undone by well-meaning additions. A later reader with a new requirement might add
packages that reintroduce the findings this work resolved. These tests assert the
*properties the security work established*, not formatting:

  * the base image is a supported Alpine release (3.19 went EOL 2025-11-01),
  * the container runs as a non-root numeric UID,
  * the no-subprocess invariant holds in the Go source (same justification as the
    controller: the monitor reaches everything over HTTP through the observation API,
    and starts no subprocess),
  * the builder uses a supported Go version.

The no-subprocess check is the load-bearing test. If someone adds ``os/exec`` to call a
binary, the minimal package set is no longer safe to maintain and must be revisited.
"""

from __future__ import annotations

import re
from pathlib import Path


MONITOR = Path(__file__).resolve().parents[1] / "src" / "superplane-platform-monitor"
DOCKERFILE = MONITOR / "Dockerfile"

# Alpine releases that have reached end-of-life. Keep in sync with the controller test.
EOL_ALPINE = ("3.17", "3.18", "3.19", "3.20", "3.21")

MIN_ALPINE = (3, 22)


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text()


def _effective_lines() -> list[str]:
    """Dockerfile lines with comments stripped."""
    lines = []
    for raw in _dockerfile_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def test_runtime_base_is_a_supported_alpine() -> None:
    """The runtime stage pins a supported Alpine, so OS packages still get fixes."""
    # The runtime FROM is the one that does NOT have "AS builder" and contains "alpine:".
    runtime_froms = [
        ln
        for ln in _effective_lines()
        if ln.startswith("FROM") and "alpine:" in ln and " AS " not in ln.upper()
    ]
    assert runtime_froms, "no runtime Alpine FROM found in the monitor Dockerfile"

    for line in runtime_froms:
        match = re.search(r"alpine:(\d+)\.(\d+)", line)
        assert match, f"runtime base must pin an explicit alpine major.minor: {line!r}"
        version = f"{match.group(1)}.{match.group(2)}"
        assert version not in EOL_ALPINE, (
            f"alpine:{version} is end-of-life and stops receiving security updates. "
            f"3.19's EOL on 2025-11-01 is why OS-level findings appeared in the S21 scan "
            f"(#6124). Pin a supported release."
        )
        assert (int(match.group(1)), int(match.group(2))) >= MIN_ALPINE, (
            f"alpine:{version} predates the minimum supported base "
            f"{MIN_ALPINE[0]}.{MIN_ALPINE[1]}."
        )


def test_monitor_runs_as_non_root() -> None:
    """Non-root operation with a numeric UID for Kubernetes runAsNonRoot compatibility."""
    lines = _effective_lines()
    user_lines = [ln for ln in lines if ln.startswith("USER ")]
    assert user_lines, "monitor Dockerfile must set a non-root USER"
    last = user_lines[-1]
    uid = last.split()[1].split(":")[0]
    assert uid not in ("root", "0"), f"monitor must not run as root: {last!r}"
    assert uid.isdigit(), (
        f"USER should be a numeric uid so Kubernetes runAsNonRoot can verify it "
        f"without resolving /etc/passwd: {last!r}"
    )


def test_monitor_starts_no_subprocess() -> None:
    """The monitor reaches everything over HTTP and never execs a subprocess.

    If this fails, the minimal package set is no longer safe to maintain — revisit the
    Dockerfile rather than relaxing this test.
    """
    offenders: list[str] = []
    for path in MONITOR.rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        text = path.read_text()
        if re.search(r'"os/exec"', text) or re.search(r"\bexec\.Command\b", text):
            offenders.append(str(path.relative_to(MONITOR)))

    assert not offenders, (
        "platform-monitor source now starts a subprocess: "
        f"{offenders}. #6124 established a minimal runtime package set on the evidence "
        "that this never happens. Adding a subprocess call means that evidence is stale — "
        "decide which binary is needed, add it to the Dockerfile, and re-scan the image."
    )


def test_builder_uses_supported_go_version() -> None:
    """The builder stage should use a supported Go version, not an EOL toolchain."""
    builder_froms = [
        ln for ln in _effective_lines() if ln.startswith("FROM") and "golang:" in ln
    ]
    assert builder_froms, "no Go builder FROM found in the monitor Dockerfile"

    for line in builder_froms:
        match = re.search(r"golang:(\d+)\.(\d+)", line)
        assert match, f"builder must pin a Go major.minor version: {line!r}"
        major, minor = int(match.group(1)), int(match.group(2))
        # Go supports current and previous release (N and N-1). Go 1.25 made 1.23 EOL.
        assert minor >= 25, (
            f"golang:{major}.{minor} is end-of-life. Go supports the two most recent "
            f"releases. Update to a supported version to avoid stdlib CVE findings."
        )
