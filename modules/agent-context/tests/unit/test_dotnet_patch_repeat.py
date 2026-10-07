"""The maintained base already has Xml 8.0.4; reapplying must verify its bytes."""
import importlib.util
import json
from pathlib import Path
import zipfile
import pytest

spec = importlib.util.spec_from_file_location("dotnet_patch", Path(__file__).resolve().parents[2] / "images/ingestion/high-security/patch-dotnet.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


def fixture(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    archive = tmp_path / "xml.nupkg"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("lib/net8.0/System.Security.Cryptography.Xml.dll", b"reviewed dll")
        z.writestr("LICENSE.txt", "license")
    old = "System.Security.Cryptography.Xml/8.0.3"
    deps = {"libraries": {old: {}}, "targets": {"net8": {old: {"runtime": {"lib/net8.0/System.Security.Cryptography.Xml.dll": {}}}, "consumer": {"dependencies": {"System.Security.Cryptography.Xml": "8.0.3"}}}}}
    (root / "dotnet-format.deps.json").write_text(json.dumps(deps))
    return root, archive, tmp_path / "licenses"


def test_install_then_repeat_preserves_exact_metadata(tmp_path):
    args = fixture(tmp_path)
    patch.install(*args)
    metadata = (args[0] / "dotnet-format.deps.json").read_bytes()
    patch.install(*args)
    assert (args[0] / "dotnet-format.deps.json").read_bytes() == metadata
    data = json.loads(metadata)
    assert data["targets"]["net8"]["consumer"]["dependencies"]["System.Security.Cryptography.Xml"] == "8.0.4"
    assert (args[2] / "LICENSE.txt").read_text() == "license"


def test_rejects_changed_installed_payload(tmp_path):
    args = fixture(tmp_path)
    patch.install(*args)
    (args[0] / "System.Security.Cryptography.Xml.dll").write_bytes(b"altered")
    with pytest.raises(AssertionError, match="payload differs"):
        patch.install(*args)


def test_rejects_unexpected_dependency_version(tmp_path):
    args = fixture(tmp_path)
    path = args[0] / "dotnet-format.deps.json"
    path.write_text(path.read_text().replace("8.0.3", "8.0.5"))
    with pytest.raises(AssertionError, match="Unexpected Xml"):
        patch.install(*args)
