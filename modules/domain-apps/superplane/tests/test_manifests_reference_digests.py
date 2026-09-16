"""No Superplane manifest may reference a floating tag — Issue #5041 (U2), EPIC #4910.

Every `image:` field in a Superplane manifest must be digest-addressed (`@sha256:`) and
none may reference `:latest`. That is the deploy-side half of R2: pinning in the lock file
achieves nothing if the manifest that actually reaches the cluster names a tag.

## Why this suite is written to be loud about finding nothing

U3 owns the rollout, so at the time of writing there are **no** manifests under this module
— `infra/control-plane/` and `infra/workspaces/` are empty. A conventional "loop over the
manifests and assert" suite would therefore pass by iterating zero times, and would keep
passing after U3 adds a manifest full of `:latest`, because nothing would notice the glob
had started matching. "No manifests found" and "all manifests are pinned" are the same
green tick, which is the failure mode U1's lane header calls out by name.

So the discovery is asserted separately from the content:

  * ``test_manifest_discovery_is_reported`` records how many manifests exist and never
    fails — it exists so the count is visible in `-v` output rather than implied;
  * the content tests are parametrized over discovered manifests and **skip with a reason**
    when there are none, so the report distinguishes "not yet applicable" from "verified";
  * ``test_upstream_floating_tag_is_not_copied_in`` is the one that has teeth today. It
    fails if a manifest appears carrying the specific floating tag upstream ships
    (`berkeleyskypilot/skypilot:latest`), which is the most likely way this regresses:
    somebody copies upstream's `infra/skypilot-api/03-*.yaml` in unchanged.

The upstream reference snapshot is deliberately out of scope here: it is read-only evidence
and is not part of what ADP deploys.
"""

from __future__ import annotations

import _release_path  # noqa: F401

import re
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]

# Where a Superplane k8s manifest would live. `releases/` is excluded: the lock file is a
# build input, not a manifest, and it legitimately records a tag under `image_sources` for
# auditability.
MANIFEST_DIRS = (
    MODULE_ROOT / "infra",
    MODULE_ROOT / "k8s",
)

DIGEST_REF_RE = re.compile(r"@sha256:[0-9a-f]{64}$")

# The exact floating reference upstream ships in all three of its SkyPilot manifests.
UPSTREAM_FLOATING_REF = "berkeleyskypilot/skypilot:latest"


def _discover_manifests() -> list[Path]:
    found: list[Path] = []
    for directory in MANIFEST_DIRS:
        if not directory.is_dir():
            continue
        for pattern in ("**/*.yaml", "**/*.yml"):
            found.extend(p for p in directory.glob(pattern) if p.is_file())
    return sorted(set(found))


def _image_fields(path: Path) -> list[str]:
    """Every `image:` value in a manifest, across multi-document YAML.

    Walks the parsed structure rather than grepping text, so an `image` nested in a
    Deployment's pod template or an init container is found the same way as a top-level one.
    """
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        pytest.fail(f"{path} is not valid YAML: {exc}")

    images: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image" and isinstance(value, str):
                    images.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for doc in docs:
        walk(doc)
    return images


MANIFESTS = _discover_manifests()


def test_manifest_discovery_is_reported() -> None:
    """Make the manifest count visible instead of implied. Never fails by design."""
    if not MANIFESTS:
        print(
            f"\nNo Superplane manifests under {[str(d.relative_to(MODULE_ROOT)) for d in MANIFEST_DIRS]} yet (U3 owns the rollout)."
        )
    else:
        print(
            f"\nChecking {len(MANIFESTS)} manifest(s): {[str(p.relative_to(MODULE_ROOT)) for p in MANIFESTS]}"
        )


@pytest.mark.skipif(
    not MANIFESTS, reason="no Superplane manifests exist yet (U3 owns the rollout)"
)
@pytest.mark.parametrize(
    "manifest", MANIFESTS, ids=lambda p: str(p.relative_to(MODULE_ROOT))
)
class TestDiscoveredManifestsArePinned:
    def test_no_image_references_latest(self, manifest: Path) -> None:
        for image in _image_fields(manifest):
            assert not image.endswith(":latest"), (
                f"{manifest.relative_to(MODULE_ROOT)} references {image!r}"
            )

    def test_every_image_is_digest_addressed(self, manifest: Path) -> None:
        for image in _image_fields(manifest):
            assert DIGEST_REF_RE.search(image), (
                f"{manifest.relative_to(MODULE_ROOT)} image {image!r} is not @sha256: pinned"
            )

    def test_every_digest_appears_in_the_lock(self, manifest: Path) -> None:
        """A manifest may only deploy a digest this unit actually pinned.

        Catches a manifest pinned to some other digest — reproducible in form, but not
        the release the lock records.
        """
        lock = yaml.safe_load(
            (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        pinned = {str(v) for v in (lock.get("images") or {}).values()}
        for image in _image_fields(manifest):
            match = DIGEST_REF_RE.search(image)
            if match:
                digest = match.group(0).lstrip("@")
                assert digest in pinned, (
                    f"{manifest.relative_to(MODULE_ROOT)} deploys {digest}, which the lock does not pin"
                )


class TestUpstreamFloatingTagIsNotCopiedIn:
    """Has teeth today: fails the moment upstream's floating ref appears anywhere here.

    The likely regression is copying upstream's `infra/skypilot-api/03-*.yaml` in
    unchanged, since all three of its manifests carry this exact reference.
    """

    def test_upstream_floating_ref_is_absent_from_the_module(self) -> None:
        offenders = []
        for path in MODULE_ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            # The lock's header quotes this ref to explain what it replaced, so the
            # comparison is against non-comment lines only.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if UPSTREAM_FLOATING_REF in stripped:
                    offenders.append(f"{path.relative_to(MODULE_ROOT)}: {stripped}")
        assert not offenders, (
            "upstream's floating SkyPilot tag was copied in:\n" + "\n".join(offenders)
        )

    def test_the_lock_pins_skypilot_by_digest_instead(self) -> None:
        """The positive counterpart: the replacement for that tag exists and is a digest."""
        lock = yaml.safe_load(
            (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(lock["images"]["skypilot-api"]))
