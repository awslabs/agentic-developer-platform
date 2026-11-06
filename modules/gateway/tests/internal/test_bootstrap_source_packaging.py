"""The exact app-owned qualification is available in an isolated image layout."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


def test_staged_qualification_works_without_repository_paths(tmp_path):
    gateway = Path(__file__).resolve().parents[2]
    subprocess.run([str(gateway / "scripts/stage-contracts.sh")], check=True, capture_output=True)
    target = tmp_path / "image/src/shared/domain_bootstrap_boundary.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(gateway / "src/shared/domain_bootstrap_boundary.py", target)
    contract = tmp_path / "image/contracts/superplane-bootstrap-qualification.json"
    contract.parent.mkdir()
    shutil.copyfile(gateway / "contracts/superplane-bootstrap-qualification.json", contract)
    spec = importlib.util.spec_from_file_location("isolated_bootstrap_boundary", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image = "qualified@sha256:9fd2a13be7c5e147fe00cf5433fcd792ab751d27cd5ce86206c1d9bc326212bb"
    source = module.qualified_source(image)
    assert source["revision"] == "61fcfc098e6e110411402342fcfc3a3531130d21"
    assert len(source["hashes"]) == 24
    assert source["cleanup_acquisition_prefix"] is True
    assert source["precomponent_permission_refusal"] is True
    assert module.qualified_source("qualified@sha256:" + "a" * 64) is None
    contract.write_text(contract.read_text() + " ")
    with pytest.raises(ValueError, match="manifest changed"):
        module.qualified_source(image)


def test_management_qualification_is_staged_and_does_not_qualify_acquisition(tmp_path):
    gateway = Path(__file__).resolve().parents[2]
    subprocess.run([str(gateway / "scripts/stage-contracts.sh")], check=True, capture_output=True)
    target = tmp_path / "image/src/shared/domain_bootstrap_boundary.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(gateway / "src/shared/domain_bootstrap_boundary.py", target)
    contract = tmp_path / "image/contracts/superplane-bootstrap-management-qualification.json"
    contract.parent.mkdir()
    shutil.copyfile(gateway / "contracts/superplane-bootstrap-management-qualification.json", contract)
    spec = importlib.util.spec_from_file_location("isolated_management_boundary", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image = "qualified@sha256:845ce1dc5c4d3a1379c548e48d8fd269b1977118793738fc02a6883413fd6117"
    source = module.qualified_source(image)
    assert source["revision"] == "0025522e4a21c5373aa24aa3f0473ccbef4d5f66"
    assert source["management_components_revoked"] is True
    assert "acquisition_prefix" not in source
    contract.write_text(contract.read_text() + " ")
    with pytest.raises(ValueError, match="qualification changed"):
        module.qualified_source(image)
