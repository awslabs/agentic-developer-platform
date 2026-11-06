import shutil

import pytest

from workspace_provisioning import pricing_plan_validation as pricing


@pytest.fixture
def sources(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "scripts").mkdir(parents=True)
    (old / "main.tf").write_text('resource "aws_vpc" "workspace" {}')
    (old / "scripts/check_workspace_plan.py").write_text("historical guard")
    (old / "scripts/workspace_ownership.py").write_text("original ownership checks")
    (old / ".terraform.lock.hcl").write_text("reviewed provider identities")
    monkeypatch.setattr(
        pricing,
        "OLD_GUARD_SHA256",
        pricing.sha(old / "scripts/check_workspace_plan.py"),
    )
    shutil.copytree(old, new)
    (new / "scripts/check_workspace_plan.py").write_text("corrected pricing guard")
    (new / "regional-pricing.json").write_text('{"reviewed":"prices"}')
    return old, new


def test_transition_changes_only_pricing_guard_and_catalog(sources):
    old, new = sources
    before = pricing.module_files(old)
    result = pricing.verify_transition(old, new)
    assert set(result) == pricing.REPLACEMENTS
    assert pricing.module_files(old) == before


@pytest.mark.parametrize(
    "change",
    [
        "terraform",
        "ownership",
        "provider",
        "extra-script",
        "missing-script",
        "unknown-original",
        "existing-catalog",
    ],
)
def test_transition_refuses_permission_source_and_recipe_changes(sources, change):
    old, new = sources
    names = {
        "terraform": "main.tf",
        "ownership": "scripts/workspace_ownership.py",
        "provider": ".terraform.lock.hcl",
        "extra-script": "scripts/bypass.py",
    }
    if change in names:
        (new / names[change]).write_text("changed")
    elif change == "missing-script":
        (new / "scripts/workspace_ownership.py").unlink()
    elif change == "unknown-original":
        (old / "scripts/check_workspace_plan.py").write_text("unrecognized old guard")
    else:
        (old / "regional-pricing.json").write_text("preexisting pricing")
    with pytest.raises(ValueError):
        pricing.verify_transition(old, new)


def test_transition_refuses_symlinked_source(sources):
    old, new = sources
    (new / "main.tf").unlink()
    (new / "main.tf").symlink_to(old / "main.tf")
    with pytest.raises(ValueError):
        pricing.verify_transition(old, new)


@pytest.mark.parametrize(
    "extra",
    [
        None,
        "command-failure-abcdefgh/other",
        "command-failure-ijklmnop",
        "command-failure-abcdefgh/nested/secret",
    ],
)
def test_diagnostics_are_exactly_one_bounded_original_pair(extra):
    entries = {
        "command-failure-abcdefgh": {"kind": "directory"},
        **{
            "command-failure-abcdefgh/" + name: {"kind": "file", "sha256": "a" * 64}
            for name in ["stdout", "stderr"]
        },
    }
    if extra:
        entries[extra] = {"kind": "directory"}
        with pytest.raises(ValueError):
            pricing.diagnostic_paths(entries)
    else:
        assert pricing.diagnostic_paths(entries) == set(entries)


def test_no_repricing_for_partial_or_extra_saved_files(tmp_path):
    review = tmp_path / "review"
    review.mkdir()
    for name in pricing.ORIGINAL_FILES:
        (review / name).write_text("retained")
    (review / "apply-result.json").write_text("cannot discard apply evidence")
    with pytest.raises(ValueError):
        pricing.reproduce_pricing(tmp_path)
