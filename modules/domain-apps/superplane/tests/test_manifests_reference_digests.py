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

# The one non-digest `image:` value R2 permits IN THE REPOSITORY, added for U3's manifests.
#
# WHY THIS EXEMPTION IS NOT A HOLE.
#
# U2 wrote this suite before any manifest existed and reasonably assumed a manifest would
# carry its digest literally. U3's manifests cannot: a literal digest here would be a SECOND
# pin, able to disagree with `releases/superplane.lock.yaml` with nothing to detect it — the
# drift R2 exists to prevent. The digest instead reaches the manifest through
# `/adp/<env>/superplane/skypilot-image`, which `infra/control-plane/config.tf` derives FROM
# the lock, so there is exactly one pin.
#
# What replaces the check for that value is stricter than what it replaces, because the text
# that matters for R2 is the text AFTER substitution:
#
#   1. `tests/test_skypilot_manifests.py::test_the_only_deployed_image_is_a_placeholder_…`
#      requires every `image:` in the repository to be this placeholder — so this exemption
#      cannot be used to smuggle in a tag.
#   2. `infra/control-plane/tests/lock_pin.tftest.hcl` asserts the SSM parameter Terraform
#      publishes is digest-addressed and is the lock's digest.
#   3. The rollout lane re-checks the live SSM value, then
#      `infra/scripts/check_rendered_manifests.py` requires every rendered digest to be one
#      the LOCK PINS — not merely 64 hex characters, which is the gap PR #5283's review
#      found in the guard this replaced.
#
# So a floating tag cannot survive to the cluster by this route, and unlike a literal digest
# it cannot drift from the lock either. The exemption is one exact string: any other
# non-digest value still fails below.
RENDERED_FROM_LOCK_PLACEHOLDER = "REPLACE_WITH_SKYPILOT_IMAGE"


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
            if image == RENDERED_FROM_LOCK_PLACEHOLDER:
                # See RENDERED_FROM_LOCK_PLACEHOLDER: the digest arrives from the SSM
                # parameter config.tf derives from this lock, so there is one pin rather than
                # two that can disagree. The rendered text is checked against the lock's
                # digests by check_rendered_manifests.py before kubectl is invoked.
                continue
            assert DIGEST_REF_RE.search(image), (
                f"{manifest.relative_to(MODULE_ROOT)} image {image!r} is not @sha256: pinned"
            )

    def test_a_placeholder_image_is_resolved_from_the_lock_by_terraform(
        self, manifest: Path
    ) -> None:
        """The exemption above is only sound if the placeholder really is lock-derived.

        Asserted here rather than assumed: if `config.tf` stopped deriving
        `skypilot-image` from the lock, the exemption would become a way to deploy an
        arbitrary image with this suite still green.
        """
        if RENDERED_FROM_LOCK_PLACEHOLDER not in _image_fields(manifest):
            pytest.skip("this manifest carries no placeholder image reference")

        config_tf = (MODULE_ROOT / "infra" / "control-plane" / "config.tf").read_text(
            encoding="utf-8"
        )
        assert "skypilot-image" in config_tf, (
            "no SSM parameter publishes skypilot-image, so the placeholder in this manifest "
            "resolves from nothing"
        )
        lock_pin = (
            MODULE_ROOT / "infra" / "control-plane" / "tests" / "lock_pin.tftest.hcl"
        ).read_text(encoding="utf-8")
        assert "aws_ssm_parameter.skypilot_image.value" in lock_pin, (
            "tests/lock_pin.tftest.hcl no longer asserts the published image is the lock's "
            "digest, so nothing keeps the placeholder honest"
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
