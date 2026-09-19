"""The ECR inventory survives digest promotion — Issue #5042 (U3), EPIC #4910.

## The defect this suite exists to prevent from returning

PR #5283's review (finding 2) reproduced a P1: the module built its ECR repository
inventory from `pending_images` alone. U2's merged release contract moves an entry OUT of
`pending_images` and into `images` + `image_sources` when its digest resolves. So the
inventory shrank at the exact moment a release succeeded, and Terraform planned to destroy
the repository holding the image that had just been pushed into it.

The failure is worse than a shrinking list because of WHEN it fires. It cannot be observed
today — all three Superplane images are pending, so the pre-promotion inventory is correct
and every existing test passes. It fires on the first successful build, and a nonempty ECR
repository refuses deletion, so the symptom is a broken apply during a release rather than
an obviously-missing repository.

## Why these tests shell out to `terraform console`

The lock path is fixed inside the module (`${path.module}/../../releases/`), so a
`.tftest.hcl` file cannot feed it a post-promotion lock — there is no variable to override.
Rewriting the real lock during a test run would be worse: it mutates a file other suites
read.

So each test copies the module's REAL locals expression into a scratch directory beside a
fixture lock and evaluates it with `terraform console`. That keeps the thing under test the
actual shipped expression (extracted from ecr.tf, not retyped) while letting the lock vary.
A test that retyped the expression would pass while ecr.tf was broken, which is precisely
the failure mode being guarded against.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent
ECR_TF = MODULE_DIR / "ecr.tf"

# The three images U2's lock records as pending, and the repository each one must keep for
# the whole of its lifecycle — pending, promoted, and after every other image promotes too.
EXPECTED_REPOSITORIES = {
    "superplane-api": "adp-superplane-api",
    "superplane-controller": "adp-superplane-controller",
    "superplane-platform-monitor": "adp-superplane-platform-monitor",
}

TERRAFORM = shutil.which("terraform")
requires_terraform = pytest.mark.skipif(
    TERRAFORM is None, reason="terraform binary not available on PATH"
)


def _extract_inventory_locals() -> str:
    """Pull the real `locals` block out of ecr.tf.

    Extracted rather than retyped so this suite fails if ecr.tf regresses. The `lock`
    assignment is replaced (it reads a path relative to the module) but every derived
    expression is used verbatim.
    """
    text = ECR_TF.read_text(encoding="utf-8")
    match = re.search(r"^locals \{\n(.*?)^\}", text, re.DOTALL | re.MULTILINE)
    assert match, "could not locate the locals block in ecr.tf"
    body = match.group(1)

    # Point `lock` at the fixture instead of the module-relative real lock.
    body = re.sub(
        r"lock\s*=\s*yamldecode\(file\([^)]*\)\)",
        'lock = yamldecode(file("${path.module}/lock.yaml"))',
        body,
    )
    assert "lock.yaml" in body, (
        "the lock assignment in ecr.tf did not match the expected shape"
    )
    return "locals {\n" + body + "}\n"


def _evaluate(lock_yaml: str, expression: str):
    """Evaluate `expression` against ecr.tf's real locals and a fixture lock."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "lock.yaml").write_text(lock_yaml, encoding="utf-8")
        (work / "main.tf").write_text(_extract_inventory_locals(), encoding="utf-8")

        init = subprocess.run(
            [TERRAFORM, "init", "-backend=false", "-input=false"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        assert init.returncode == 0, (
            f"terraform init failed:\n{init.stdout}\n{init.stderr}"
        )

        console = subprocess.run(
            [TERRAFORM, "console"],
            cwd=work,
            input=f"jsonencode({expression})\n",
            capture_output=True,
            text=True,
        )
        assert console.returncode == 0, (
            f"terraform console failed for {expression}:\n{console.stdout}\n{console.stderr}"
        )
        raw = console.stdout.strip().splitlines()[-1]
        return json.loads(json.loads(raw))


def _lock(pending: dict[str, str], promoted: dict[str, str]) -> str:
    """Build a lock fixture in U2's real shape.

    A promoted entry appears in BOTH `images` (carrying the digest) and `image_sources`
    (carrying the build metadata, including `ecr_repository`) and is absent from
    `pending_images` — which is what U2's merged contract does on promotion.
    """
    doc: dict = {
        "schema_version": 1,
        "images": {},
        "image_sources": {},
        "pending_images": {},
    }

    # skypilot-api is always present and always external: pulled from Docker Hub by digest,
    # so it carries no `ecr_repository` and must never acquire an ECR repository here.
    doc["images"]["skypilot-api"] = "sha256:" + "d" * 64
    doc["image_sources"]["skypilot-api"] = {
        "registry": "registry-1.docker.io",
        "repository": "berkeleyskypilot/skypilot",
        "tag": "0.12.0",
    }

    for name, repository in pending.items():
        doc["pending_images"][name] = {
            "upstream_path": f"src/{name}",
            "ecr_repository": repository,
            "blocked_by": "source_access",
        }
    for name, repository in promoted.items():
        doc["images"][name] = "sha256:" + "a" * 64
        doc["image_sources"][name] = {
            "upstream_path": f"src/{name}",
            "ecr_repository": repository,
            "resolved_by": "build lane",
        }

    import yaml

    return yaml.safe_dump(doc, sort_keys=False)


ALL_REPOSITORIES = sorted(EXPECTED_REPOSITORIES.values())


@requires_terraform
def test_inventory_complete_before_any_promotion():
    """The pre-promotion state — the only one the pre-fix code got right."""
    result = _evaluate(
        _lock(pending=dict(EXPECTED_REPOSITORIES), promoted={}),
        "local.superplane_ecr_repositories",
    )
    assert result == ALL_REPOSITORIES


@requires_terraform
@pytest.mark.parametrize("promoted_image", sorted(EXPECTED_REPOSITORIES))
def test_inventory_survives_each_single_promotion(promoted_image: str):
    """Promoting ONE image must not remove its repository.

    This is the reproduction from the review, parametrised over all three images. Against
    the pre-fix locals, promoting `superplane-api` returned only the controller and
    platform-monitor repositories.
    """
    promoted = {promoted_image: EXPECTED_REPOSITORIES[promoted_image]}
    pending = {k: v for k, v in EXPECTED_REPOSITORIES.items() if k != promoted_image}

    result = _evaluate(_lock(pending, promoted), "local.superplane_ecr_repositories")

    assert result == ALL_REPOSITORIES, (
        f"promoting {promoted_image} changed the repository inventory. "
        f"Terraform would plan to destroy the dropped repository — which is the one the "
        f"release just pushed into. Missing: {sorted(set(ALL_REPOSITORIES) - set(result))}"
    )


@requires_terraform
def test_inventory_stable_when_all_images_promoted():
    """The steady state after the whole EPIC's builds succeed."""
    result = _evaluate(
        _lock(pending={}, promoted=dict(EXPECTED_REPOSITORIES)),
        "local.superplane_ecr_repositories",
    )
    assert result == ALL_REPOSITORIES


@requires_terraform
def test_external_skypilot_registry_gets_no_ecr_repository():
    """skypilot-api is pulled from Docker Hub by digest; it must stay external.

    The union widened what the inventory reads, so this asserts the widening did not sweep
    in an entry that has no `ecr_repository`. Keeping the external registry external is
    called out explicitly in the review.
    """
    result = _evaluate(
        _lock(pending=dict(EXPECTED_REPOSITORIES), promoted={}),
        "local.superplane_ecr_repositories",
    )
    assert not any("skypilot" in repository for repository in result), (
        f"skypilot-api acquired an ECR repository: {result}. It is pulled from Docker Hub "
        f"by digest and needs none."
    )


@requires_terraform
def test_duplicate_repository_names_are_detected():
    """Two images naming one repository must be caught, not silently deduplicated.

    `for_each` over a set deduplicates, so without the pre-dedup count comparison the
    second image would push into the first's repository with no plan diff to show it.
    """
    colliding = {
        "superplane-api": "adp-superplane-api",
        "superplane-controller": "adp-superplane-api",  # same repository — a lock typo
    }
    lock = _lock(pending=colliding, promoted={})

    prededup = _evaluate(lock, "local.ecr_repository_names_prededup")
    deduped = _evaluate(lock, "local.superplane_ecr_repositories")

    assert len(prededup) != len(deduped), (
        "a duplicate repository name did not change the pre/post-dedup counts, so the "
        "precondition in ecr.tf cannot detect it"
    )


@requires_terraform
def test_foreign_repository_names_are_detected():
    """A lock naming a non-domain repository must be flagged as foreign.

    Without this, a lock edit naming `adp-gateway` would make this module create — and on
    destroy DELETE — a repository owned by the gateway. "A separate state key alone does
    not prove resource isolation" (platform-isolation requirement, 2026-09-16).
    """
    foreign = _evaluate(
        _lock(pending={"superplane-api": "adp-gateway"}, promoted={}),
        "local.ecr_repositories_foreign",
    )
    assert foreign == ["adp-gateway"], (
        f"a foreign repository name was not flagged: {foreign}. This module would create "
        f"and destroy a repository it does not own."
    )


@requires_terraform
def test_domain_owned_names_are_not_flagged_as_foreign():
    """The ownership check must not reject the module's own repositories."""
    foreign = _evaluate(
        _lock(pending=dict(EXPECTED_REPOSITORIES), promoted={}),
        "local.ecr_repositories_foreign",
    )
    assert foreign == []


def test_real_lock_declares_every_expected_repository():
    """The shipped lock still names the three repositories, under either key.

    Guards the fixtures above from drifting away from reality: if U2 renames a repository
    or adds a fourth image, this fails and the parametrised cases get updated with it.
    """
    import yaml

    lock = yaml.safe_load(
        (MODULE_DIR / ".." / ".." / "releases" / "superplane.lock.yaml").read_text(
            encoding="utf-8"
        )
    )
    declared = {
        name: entry.get("ecr_repository")
        for key in ("pending_images", "image_sources")
        for name, entry in (lock.get(key) or {}).items()
        if isinstance(entry, dict) and entry.get("ecr_repository")
    }
    assert declared == EXPECTED_REPOSITORIES
