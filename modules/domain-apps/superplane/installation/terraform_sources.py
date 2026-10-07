"""Private, self-contained snapshots of the maintained Terraform build inputs."""

import os
from pathlib import Path

import yaml

from .config import MODULE, require


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    os.chmod(path, 0o600)


def copy_root(source: Path, destination: Path, replacements: dict) -> None:
    seen = set()
    for original in source.glob("*.tf"):
        text = original.read_text()
        for old, new in replacements.items():
            if old in text:
                seen.add(old)
                text = text.replace(old, new)
        write(destination / original.name, text)
    require(seen == set(replacements), "Maintained Terraform input contract changed")


def stage_control_plane(destination: Path, lock: dict) -> None:
    copy_root(
        MODULE / "infra/control-plane",
        destination,
        {
            "${path.module}/../../releases/superplane.lock.yaml": "${path.module}/release-lock.yaml",
            'source                     = "../../../shared/infra/codebuild-projects"': 'source                     = "./dependencies/image-builds"',
            "${path.module}/../../codebuild/projects.json": "${path.module}/dependencies/projects.json",
        },
    )
    copy_root(
        MODULE.parent / "shared/infra/codebuild-projects",
        destination / "dependencies/image-builds",
        {},
    )
    write(
        destination / "dependencies/projects.json",
        (MODULE / "codebuild/projects.json").read_text(),
    )
    write(destination / "release-lock.yaml", yaml.safe_dump(lock, sort_keys=False))


def stage_paid_build(destination: Path) -> None:
    copy_root(
        MODULE / "infra/paid-worker-build",
        destination,
        {
            "${path.module}/../paid-worker-project.json": "${path.module}/paid-worker-project.json"
        },
    )
    write(
        destination / "paid-worker-project.json",
        (MODULE / "infra/paid-worker-project.json").read_text(),
    )
