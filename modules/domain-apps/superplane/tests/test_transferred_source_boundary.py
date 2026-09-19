"""The transferred source is one writable tree, and not a deploy path — Issue #5326 (U22).

U22 moved three Superplane components into ADP ownership. That creates two risks the transfer
itself cannot prevent, so they are pinned here.

**Risk 1 — a second writable tree.** The reference snapshot at
``modules/domain-apps/ai-super-plane/reference/`` holds the same files as read-only evidence.
Two trees containing the same code, one of which nobody maintains, is how a fix lands in the
copy that is not built. The design allocation for this EPIC is explicit that a historical
reference may remain read-only evidence but must not become a second writable runtime tree, so
these tests assert the maintained tree is the only one anything reads.

**Risk 2 — the components' own deploy manifests becoming a live deploy path.** Each
transferred component ships an upstream ``deploy/`` directory. Those files carry upstream's AWS
account id and ``:latest`` image tags, and they transferred verbatim because byte-fidelity is
what makes the transfer auditable. They are *inventory*, not configuration: U3 owns reconciling
what the accepted topology needs into ``infra/control-plane/`` and ``k8s/``. If a rollout lane
ever pointed at them, the transfer would have quietly imported upstream's account as ADP's.

## Why these assert on mechanism, not on prose

Every claim below is checked against a file a lane actually reads — a workflow's steps, the
renderer's source directory, the lock's values. A test that searched documentation for the
right sentence would pass on a repo that says the right thing and does the wrong one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = MODULE_ROOT / "src"
LOCK_PATH = MODULE_ROOT / "releases" / "superplane.lock.yaml"
MANIFEST = SRC_ROOT / "TRANSFER-MANIFEST.md"
SNAPSHOT_REL = "modules/domain-apps/ai-super-plane/reference"

COMPONENTS = (
    "superplane-api",
    "superplane-controller",
    "superplane-platform-monitor",
)

UPSTREAM_ACCOUNTS = ("605440105851", "938500344975")

# The components upstream ships that the accepted topology does not include. Named explicitly
# so that transferring one later has to be a deliberate edit here, rather than passing
# unnoticed because a directory listing grew.
NOT_TRANSFERRED = (
    "superplane-agent-gateway",
    "superplane-cli",
    "superplane-portal",
    "superplane-skill",
)

# Workflows that could plausibly reach for source or manifests.
DOMAIN_WORKFLOWS = sorted(
    (REPO_ROOT / ".github" / "workflows").glob("superplane-*.yml")
)


@pytest.fixture(scope="module")
def lock() -> dict:
    return yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))


def _executed_lines(path: Path) -> list[str]:
    """Non-comment lines. Comments are excluded because the workflows explain what they
    rule out, and a check that could not tell an explanation from the thing explained would
    force the next person to delete the explanation to get green."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]


class TestTheMaintainedTreeIsTheOnlyWritableOne:
    def test_all_three_components_are_present(self) -> None:
        for component in COMPONENTS:
            assert (SRC_ROOT / component / "Dockerfile").is_file(), (
                f"{component} is not present as maintained source"
            )

    def test_excluded_components_were_not_transferred(self) -> None:
        """Retaining four of seven components was a topology decision, not an oversight."""
        for component in NOT_TRANSFERRED:
            assert not (SRC_ROOT / component).exists(), (
                f"{component} is not in the accepted topology but appears under src/"
            )

    def test_no_domain_workflow_reads_the_reference_snapshot(self) -> None:
        """The snapshot is evidence. A lane reading it would build unmaintained code."""
        for workflow in DOMAIN_WORKFLOWS:
            for line in _executed_lines(workflow):
                assert SNAPSHOT_REL not in line, (
                    f"{workflow.name} reads the reference snapshot: {line.strip()!r}"
                )

    def test_the_lock_points_only_at_the_maintained_tree(self, lock: dict) -> None:
        assert lock["maintained_source"]["root"] == "modules/domain-apps/superplane/src"
        for component, entry in (lock["pending_images"] or {}).items():
            assert entry["source_path"].startswith("src/"), (
                f"{component} resolves outside the maintained tree: {entry!r}"
            )

    def test_the_snapshot_is_not_a_python_import_path(self) -> None:
        """Belt and braces on the *test* side, not only the build side.

        A test that imported from the snapshot would make the evidence tree load-bearing for
        CI, which is the same mistake as building from it wearing a different hat.
        """
        for path in MODULE_ROOT.rglob("*.py"):
            if SRC_ROOT in path.parents or "reference" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(
                r"^\s*(?:from|import)\s+\S*ai_super_plane\S*", text, re.M
            ):
                pytest.fail(
                    f"{path} imports from the snapshot: {match.group(0).strip()!r}"
                )


class TestTransferredDeployAssetsAreInventoryNotConfiguration:
    """Each component's upstream `deploy/` directory transferred as evidence, not as a lane."""

    def test_the_deploy_directories_transferred(self) -> None:
        """They must be present — excluding them would have hidden what upstream deploys."""
        for component in COMPONENTS:
            assert (SRC_ROOT / component / "deploy").is_dir(), (
                f"{component}/deploy is missing from the transfer"
            )

    def test_the_manifest_inventories_every_file_carrying_upstreams_account(
        self,
    ) -> None:
        """The inventory must be complete, because an incomplete one is worse than none.

        A reader trusts this manifest to say where upstream's identifiers are, and U3 works
        from it when reconciling deployment assets. Earlier drafts missed both a component
        carrying the first account id and a file carrying a second account id, so a
        reconciliation that trusted them would have carried upstream configuration while
        believing it had checked every carrier.

        Derived from the tree rather than restated, so the manifest cannot drift from it.
        """
        carriers = sorted(
            (account, path.relative_to(SRC_ROOT).as_posix())
            for component in COMPONENTS
            for path in (SRC_ROOT / component / "deploy").rglob("*")
            if path.is_file()
            for account in UPSTREAM_ACCOUNTS
            if account in path.read_text(encoding="utf-8")
        )
        assert carriers, (
            "no transferred file carries upstream's account id — either the files changed "
            "or this test is checking the wrong place; both need a human"
        )
        text = MANIFEST.read_text(encoding="utf-8")
        for account, path in carriers:
            assert f"| `{account}` | `{path}` |" in text, (
                f"{path} carries upstream's account {account} but that pair is not inventoried"
            )

    def test_no_workflow_applies_a_transferred_manifest(self) -> None:
        """The property that keeps upstream's account from becoming ADP's by accident.

        These manifests carry `605440105851` and `:latest`. A rollout lane pointed at them
        would deploy into upstream's registry references under ADP's credentials, and would do
        it while every doc still said the images were not deployable.
        """
        for workflow in DOMAIN_WORKFLOWS:
            for line in _executed_lines(workflow):
                for component in COMPONENTS:
                    assert f"src/{component}/deploy" not in line, (
                        f"{workflow.name} reaches into a transferred deploy directory: "
                        f"{line.strip()!r}"
                    )

    def test_the_renderer_is_not_pointed_at_transferred_manifests(self) -> None:
        """`render_manifests.py` substitutes ADP identities into ADP manifests only.

        Rendering upstream's manifests would produce something that looks reviewed — real ARNs,
        real secret names — out of files nobody adapted to this topology.
        """
        for workflow in DOMAIN_WORKFLOWS:
            text = workflow.read_text(encoding="utf-8")
            if "render_manifests.py" not in text:
                continue
            for line in _executed_lines(workflow):
                if "--source-dir" not in line:
                    continue
                assert "/src/" not in line, (
                    f"{workflow.name} renders from the transferred tree: {line.strip()!r}"
                )

    def test_upstreams_account_id_is_not_adopted_by_adp_manifests(self) -> None:
        """The transferred files may contain it; ADP's applied manifests may not.

        Scoped to the module's OWN `k8s/` manifests rather than to the transferred tree,
        because the transferred tree is supposed to still contain it — that is what makes it an
        honest record of what upstream deploys.

        Asserted against parsed **values**, not raw text. Both `k8s/20-skypilot-config.yaml`
        and `infra/control-plane/variables.tf` mention this account legitimately: one explains
        in a comment why it was not adopted, the other lists it as a value to *reject*. A raw
        substring scan cannot tell "we deny this account" from "we deploy into it", and the
        pressure it creates is to delete the explanation — leaving a repo that no longer says
        why the account is blocked. So the account id is looked for where it would actually do
        harm: in a value something applies. The Terraform side has its own value-level check in
        `infra/control-plane/tests/no_inherited_defaults.tftest.hcl`, and the lock in
        `tests/test_lock.py::test_no_aws_account_id_is_invented`.
        """

        def walk(node: object) -> list[str]:
            if isinstance(node, dict):
                return [s for value in node.values() for s in walk(value)]
            if isinstance(node, list):
                return [s for item in node for s in walk(item)]
            return [str(node)] if node is not None else []

        for path in sorted((MODULE_ROOT / "k8s").glob("*.yaml")):
            documents = yaml.safe_load_all(path.read_text(encoding="utf-8"))
            for scalar in [s for document in documents for s in walk(document)]:
                for account in UPSTREAM_ACCOUNTS:
                    assert account not in scalar, (
                        f"{path.relative_to(REPO_ROOT)} adopts upstream's AWS account "
                        f"{account} in an applied value: {scalar!r}"
                    )


class TestTheManifestRecordsWhatTheStoryRequires:
    """The manifest is a required deliverable, so its required content is asserted."""

    def test_the_manifest_exists_where_other_files_reference_it(self) -> None:
        """`.ruff.toml`, `conftest.py` and the lock all cite this path."""
        assert MANIFEST.is_file(), f"{MANIFEST} is referenced but does not exist"

    def test_it_maps_every_component_from_old_path_to_maintained_path(self) -> None:
        text = MANIFEST.read_text(encoding="utf-8")
        for component in COMPONENTS:
            assert f"src/{component}/" in text, f"{component} has no path mapping"
            assert f"modules/domain-apps/superplane/src/{component}/" in text

    def test_it_names_the_excluded_components_and_says_why(self) -> None:
        text = MANIFEST.read_text(encoding="utf-8")
        for component in NOT_TRANSFERRED:
            assert component in text, f"{component} is not listed as excluded"

    def test_it_records_the_supported_commands(self) -> None:
        """Point 4 of the story: how to test and build this tree, in one place."""
        text = MANIFEST.read_text(encoding="utf-8")
        assert "pytest tests/" in text
        assert "go test ./..." in text
        assert "transfer-constraints.txt" in text
        assert "resolve_lock.py" in text

    def test_it_records_ownership_and_the_inherited_findings(self) -> None:
        text = MANIFEST.read_text(encoding="utf-8")
        assert "Maintainer ownership" in text
        # The inherited defects must be named with their owning units, so that reading the
        # manifest cannot leave someone believing the transfer repaired them.
        for unit in ("U13", "U14", "U23"):
            assert unit in text, f"{unit}'s inherited finding is not recorded"

    def test_it_does_not_claim_a_license_that_does_not_exist(self) -> None:
        """Upstream ships no LICENSE/NOTICE at this revision, and none was fabricated."""
        for component in COMPONENTS:
            for name in ("LICENSE", "NOTICE", "COPYING"):
                assert not (SRC_ROOT / component / name).exists(), (
                    f"a {name} was added to {component} that upstream does not have"
                )
        assert "no `LICENSE` and no `NOTICE`" in MANIFEST.read_text(encoding="utf-8")
