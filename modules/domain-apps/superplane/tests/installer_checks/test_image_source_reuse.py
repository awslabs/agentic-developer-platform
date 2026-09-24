"""Reuse images only when Git proves the entire build context is unchanged."""

import subprocess

import pytest

from installation import runner
from installation.config import COMPONENTS, Refusal


def test_real_git_context_identity_blocks_changed_or_missing_source(
    tmp_path, environment, release, monkeypatch
):
    environment.pop("execution")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def commit():
        git("add", ".")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "test",
        )
        return git("rev-parse", "HEAD")

    git("init")
    module = repo / "modules/domain-apps/superplane"
    for name in COMPONENTS[:-1]:
        context = module / "src" / name
        context.mkdir(parents=True)
        (context / "Dockerfile").write_text("FROM scratch\n")
    built = commit()
    (module / "installer.txt").write_text("installer-only correction")
    release["source_revision"] = commit()
    for name in COMPONENTS[:-1]:
        release["image_sources"][name]["source_revision"] = built
    output = tmp_path / "output"
    output.mkdir()
    installer = runner.Installer(environment, release, output)
    monkeypatch.setattr(runner, "MODULE", module)
    installer.verify_image_sources()
    assert len(installer.receipt["reused_image_sources"]) == 3
    (module / "src/superplane-api/Dockerfile").write_text("FROM changed\n")
    release["source_revision"] = commit()
    with pytest.raises(Refusal, match="build context differs"):
        installer.verify_image_sources()
    release["image_sources"]["superplane-api"]["source_revision"] = "f" * 40
    with pytest.raises(Refusal, match="git failed"):
        installer.verify_image_sources()
