"""Exercise the shipped skill after the real client sync, without contacting ADP."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[5]
SOURCE = ROOT / "modules/agent-factory/skills"
CLI = ROOT / "modules/gateway/cli"
SPEC = importlib.util.spec_from_file_location("adp_skills", CLI / "adp_skills.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


@pytest.mark.parametrize("client,client_directory", [("codex", ".codex"), ("claude-code", ".claude")])
@pytest.mark.parametrize("layout", ["default", "adp_home", "prefix_bin", "beside_cli"])
def test_installed_writer_resolves_bundle_policy(tmp_path, client, client_directory, layout):
    home = tmp_path / "home"
    private_root = home / ".adp" if layout != "adp_home" else tmp_path / "private"
    bundle = private_root / "skills"
    bin_dir = tmp_path / "bin"
    if layout in {"prefix_bin", "beside_cli"}:
        bin_dir = tmp_path / "prefix" / "bin" if layout == "prefix_bin" else tmp_path / "prefix"
        bundle = tmp_path / "prefix" / "skills"
    bin_dir.mkdir(parents=True)
    if layout in {"prefix_bin", "beside_cli"}:
        shutil.copyfile(CLI / "adp", bin_dir / "adp")
        (bin_dir / "adp").chmod(0o755)
    output = tmp_path / "archive"
    subprocess.run(["bash", str(ROOT / "modules/gateway/scripts/build-skills-archive.sh"),
                    "--build", str(SOURCE), str(output), str(CLI / "adp")], check=True)
    with tarfile.open(output / "adp-skills.tar.gz", "r:gz") as archive:
        for member in archive:
            relative = Path(member.name)
            if relative.parts[0] == "skills":
                relative = Path(*relative.parts[1:])
            target = bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    INSTALLER.sync(client, bundle, home)
    installed = home / client_directory / "skills"
    assert not (installed / "_shared").exists()
    directory = tmp_path / "plan"
    shutil.copytree(SOURCE / "adp-flow/tests/fixtures/recorded-interview", directory)
    environment = {**os.environ, "HOME": str(home), "ADP_HOME": str(private_root), "PATH": str(bin_dir)}
    if layout == "default":
        environment.pop("ADP_HOME")
    writer = installed / "adp-flow/scripts/plan_writer.py"
    result = subprocess.run([sys.executable, str(writer), str(directory), str(directory / "facts.json")],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    document = json.loads((directory / "proposal.authored.json").read_text())
    assert len(document["wave_metadata"]) == 3
    assert document["proposed_execution_policy"]["limits"]["max_spend_usd"] == "600"
    resolved = subprocess.run([sys.executable, str(writer), "--shared-dir"],
                              env=environment, capture_output=True, text=True, check=True)
    assert Path(resolved.stdout.strip()) == bundle / "_shared"
