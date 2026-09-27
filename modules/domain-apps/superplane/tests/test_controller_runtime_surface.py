"""The controller runtime image keeps the small package surface S01 established.

Issue #5600 (work package S01) of the 2026-09-21 security scan. The controller image
reported 4 critical and 4 high matches. Six of the eight came from packages the controller
cannot invoke, because it starts no subprocess: ``helm`` carried CVE-2025-53547 plus three
advisories against Go modules compiled into ``/usr/bin/helm``, and ``aws-cli`` pulled the
Python chain that supplied the sqlite-libs, cryptography, certifi and jmespath matches.
Removing them resolved those findings at the source rather than moving them to a newer
vulnerable version.

## Why this file exists

That reasoning is invisible in the resulting Dockerfile. A later reader sees only a short
``apk add`` list and has no signal that ``helm`` was removed deliberately rather than never
needed, so the natural response to "the controller should run a helm command" is to add the
package back — silently reopening four advisories that no scan will flag until the next
scheduled run. The same applies to the base image: floating back to an end-of-life Alpine
reintroduces the c-ares and sqlite findings with no diff that looks security-relevant.

So these tests assert the *properties the security work established*, not formatting:

  * the base image is a supported Alpine release (3.20 went EOL 2026-04-01),
  * the removed packages stay removed,
  * the container still runs as non-root,
  * and the no-subprocess invariant that justifies the removals still holds in the Go source.

The last one is the load-bearing check. The removals are only safe while the controller
genuinely never executes a subprocess; if someone adds ``os/exec`` to call a binary, the
premise is gone and that must fail loudly here rather than as a runtime "helm: not found"
in a reconcile loop. A test that only read the Dockerfile would miss exactly that case.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONTROLLER = Path(__file__).resolve().parents[1] / "src" / "superplane-controller"
DOCKERFILE = CONTROLLER / "Dockerfile"

# Removed by S01. Each entry is a package whose presence reintroduces named advisories.
REMOVED_PACKAGES = {
    "curl": (
        "retains zlib CVE-2026-85091; the static controller starts no subprocess "
        "and the existing e2e diagnostics use BusyBox wget"
    ),
    "helm": (
        "CVE-2025-53547, plus GHSA-v778-237x-gjrc / GHSA-hcg3-q754-cr77 "
        "(golang.org/x/crypto) and GHSA-v23v-6jw2-98fq (github.com/docker/docker) "
        "via Go modules compiled into /usr/bin/helm"
    ),
    "aws-cli": (
        "pulls python3 and the py3-* chain: sqlite-libs (CVE-2025-3277), "
        "py3-cryptography (GHSA-r6ph-v2qm-q3c2), py3-certifi (GHSA-248v-346w-9cwc), "
        "py3-jmespath"
    ),
    "openssh-client": "unused remote-access surface in a controller that starts no subprocess",
    "wireguard-tools": "tunnels are established cloud-side by SkyPilot, not in this container",
}

# Alpine releases that were already end-of-life when S01 ran (2026-09-21). 3.20's EOL
# (2026-04-01) is why its c-ares and sqlite-libs were stale in the first place.
EOL_ALPINE = ("3.17", "3.18", "3.19", "3.20", "3.21")

MIN_ALPINE = (3, 22)


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text()


def _effective_lines() -> list[str]:
    """Dockerfile lines with comments stripped.

    The Dockerfile *explains* which packages were removed and why, naming ``helm`` and
    ``aws-cli`` repeatedly. A substring check over the raw text would therefore fail on the
    documentation it is meant to protect, and the cheapest way to pass would be deleting the
    explanation. Only executable lines are considered.
    """
    lines = []
    for raw in _dockerfile_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def test_runtime_base_is_a_supported_alpine() -> None:
    """The runtime stage pins a supported Alpine, so OS packages still get fixes."""
    runtime_froms = [
        ln
        for ln in _effective_lines()
        if ln.startswith("FROM") and "alpine:" in ln and " AS " not in ln.upper()
    ]
    assert runtime_froms, "no runtime Alpine FROM found in the controller Dockerfile"

    for line in runtime_froms:
        match = re.search(r"alpine:(\d+)\.(\d+)", line)
        assert match, f"runtime base must pin an explicit alpine major.minor: {line!r}"
        version = f"{match.group(1)}.{match.group(2)}"
        assert version not in EOL_ALPINE, (
            f"alpine:{version} is end-of-life and stops receiving security updates "
            f"(3.20's EOL on 2026-04-01 is why c-ares CVE-2025-31498 and the sqlite "
            f"finding appeared in issue #5600). Pin a supported release."
        )
        assert (int(match.group(1)), int(match.group(2))) >= MIN_ALPINE, (
            f"alpine:{version} predates the minimum supported base "
            f"{MIN_ALPINE[0]}.{MIN_ALPINE[1]} established by S01 (#5600)."
        )


@pytest.mark.parametrize("package", sorted(REMOVED_PACKAGES))
def test_removed_runtime_packages_stay_removed(package: str) -> None:
    """Re-adding one of these reopens the advisories S01 closed by removing it."""
    apk_lines = [
        ln for ln in _effective_lines() if "apk" in ln or ln.startswith(package)
    ]
    haystack = " ".join(_effective_lines())
    # Word-boundary match so `aws-cli` is not matched by an unrelated `aws-cli-v2` and
    # `helm` is not matched inside a longer token.
    assert not re.search(rf"(?<![\w-]){re.escape(package)}(?![\w-])", haystack), (
        f"{package!r} is back in the controller runtime image. It was removed by S01 "
        f"(#5600) because the controller starts no subprocess and so cannot invoke it; "
        f"it carried {REMOVED_PACKAGES[package]}. If the controller now genuinely needs "
        f"it, that is a scope change: re-add it deliberately, update this test, and "
        f"re-run the image scan for the advisories listed above.\n"
        f"Offending lines: {apk_lines}"
    )


def test_controller_still_runs_as_non_root() -> None:
    """Non-root operation is an acceptance item of #5600 and must survive the rebuild."""
    lines = _effective_lines()
    user_lines = [ln for ln in lines if ln.startswith("USER ")]
    assert user_lines, "controller Dockerfile must set a non-root USER"
    last = user_lines[-1]
    uid = last.split()[1].split(":")[0]
    assert uid not in ("root", "0"), f"controller must not run as root: {last!r}"
    assert uid.isdigit(), (
        f"USER should be a numeric uid so Kubernetes runAsNonRoot can verify it "
        f"without resolving /etc/passwd: {last!r}"
    )


def test_builder_uses_supported_go_version() -> None:
    """The builder stage should use a supported Go version, not an EOL toolchain.

    Go 1.23 reached end-of-life and its stdlib carries unpatched CVEs that show up
    as scanner findings. Go supports the two most recent releases (N and N-1).
    """
    builder_froms = [
        ln for ln in _effective_lines() if ln.startswith("FROM") and "golang:" in ln
    ]
    assert builder_froms, "no Go builder FROM found in the controller Dockerfile"

    for line in builder_froms:
        match = re.search(r"golang:(\d+)\.(\d+)", line)
        assert match, f"builder must pin a Go major.minor version: {line!r}"
        major, minor = int(match.group(1)), int(match.group(2))
        assert minor >= 25, (
            f"golang:{major}.{minor} is end-of-life. Go supports the two most recent "
            f"releases. Update to a supported version to avoid stdlib CVE findings."
        )


def test_controller_starts_no_subprocess() -> None:
    """The premise behind removing helm/aws-cli/ssh: the controller never execs anything.

    If this fails, the runtime package removals are no longer justified and the Dockerfile
    must be revisited — not this test relaxed.
    """
    offenders: list[str] = []
    for path in CONTROLLER.rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        text = path.read_text()
        if re.search(r'"os/exec"', text) or re.search(r"\bexec\.Command\b", text):
            offenders.append(str(path.relative_to(CONTROLLER)))

    assert not offenders, (
        "controller source now starts a subprocess: "
        f"{offenders}. S01 (#5600) removed helm, aws-cli, openssh-client and "
        "wireguard-tools from the runtime image on the evidence that this never happens. "
        "Adding a subprocess call means that evidence is stale — decide which binary is "
        "needed, re-add exactly that package, and re-scan the image."
    )
