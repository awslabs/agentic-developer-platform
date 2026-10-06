"""Archive-boundary regressions; synthetic bytes are never Java/runtime evidence."""

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

SOURCE = (
    Path(__file__).parents[1] / "images/skypilot/jackson-maintenance/replace_jar.py"
)
spec = importlib.util.spec_from_file_location("jackson_jar_merge", SOURCE)
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


def archive(path, values):
    with zipfile.ZipFile(path, "w") as output:
        for name, raw in values.items():
            output.writestr(name, raw)


@pytest.fixture
def jars(tmp_path, monkeypatch):
    root = tmp_path
    (root / "artifacts").mkdir()
    original = root / "original.jar"
    original_entries = {
        "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\nMulti-Release: true\r\n\r\n",
        "unrelated/Consumer.class": b"synthetic unchanged consumer",
        "module-info.class": b"synthetic unchanged JAXB descriptor",
        merge.NAMESPACE + "core/Version.class": b"old synthetic library",
        "META-INF/versions/17/com/fasterxml/jackson/core/Parser.class": b"old synthetic parser",
        "META-INF/services/com.fasterxml.jackson.core.JsonFactory": b"old unrelocated service",
    }
    archive(original, original_entries)
    lock = {
        "original_jar_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "jackson_version": "2.18.11",
        "artifacts": [],
    }
    replacement = {
        merge.NAMESPACE + "core/Version.class": b"new synthetic library",
        "META-INF/versions/17/com/fasterxml/jackson/core/Parser.class": b"synthetic io/ray/shaded/com/fasterxml/jackson/core/Parser",
        "META-INF/services/io.ray.shaded.com.fasterxml.jackson.core.JsonFactory": b"new relocated service",
    }
    for component in ("core", "databind", "annotations"):
        filename = f"jackson-{component}-2.18.11.jar"
        path = root / "artifacts" / filename
        archive(
            path, {"META-INF/LICENSE": b"synthetic license for " + component.encode()}
        )
        lock["artifacts"].append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        replacement[merge.MAVEN + f"jackson-{component}/pom.properties"] = (
            b"version=2.18.11\n"
        )
    (root / "artifact-lock.json").write_text(json.dumps(lock))
    shaded = root / "shaded.jar"
    archive(shaded, replacement)
    monkeypatch.setattr(merge, "ROOT", root)
    return root, original, shaded, original_entries, replacement, lock


def test_real_entry_replacement_preserves_other_consumers_and_corrects_version_paths(
    jars,
):
    root, original, shaded, before, _, _ = jars
    receipt = merge.merge(original, shaded, root / "result.jar")
    with zipfile.ZipFile(root / "result.jar") as result:
        after = merge.records(result)
    assert after["unrelated/Consumer.class"] == before["unrelated/Consumer.class"]
    assert after["module-info.class"] == before["module-info.class"]
    assert after["META-INF/MANIFEST.MF"] == before["META-INF/MANIFEST.MF"]
    assert after[merge.NAMESPACE + "core/Version.class"] == b"new synthetic library"
    assert (
        "META-INF/versions/17/io/ray/shaded/com/fasterxml/jackson/core/Parser.class"
        in after
    )
    assert "META-INF/versions/17/com/fasterxml/jackson/core/Parser.class" not in after
    assert "META-INF/services/com.fasterxml.jackson.core.JsonFactory" not in after
    assert after[merge.MAVEN + "jackson-core/LICENSE"] == b"synthetic license for core"
    assert receipt["unrelated_entries_preserved"] == 3


@pytest.mark.parametrize(
    "failure",
    [
        "original-bytes",
        "signed",
        "unexpected-member",
        "unrelocated-version",
        "metadata-only",
        "vendor-bytes",
    ],
)
def test_foreign_or_incomplete_artifacts_are_refused(jars, failure):
    root, original, shaded, before, replacement, lock = jars
    if failure == "original-bytes":
        before["unrelated/Consumer.class"] = b"foreign consumer"
        archive(original, before)
    elif failure == "signed":
        before["META-INF/VENDOR.SF"] = b"synthetic signature"
        archive(original, before)
        lock["original_jar_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
        (root / "artifact-lock.json").write_text(json.dumps(lock))
    elif failure == "unexpected-member":
        replacement["unrelated/Consumer.class"] = b"foreign replacement"
    elif failure == "unrelocated-version":
        replacement["META-INF/versions/17/com/fasterxml/jackson/core/Parser.class"] = (
            b"unrelocated synthetic parser"
        )
    elif failure == "metadata-only":
        replacement = {
            name: raw
            for name, raw in replacement.items()
            if not name.endswith(".class")
        }
    elif failure == "vendor-bytes":
        (root / "artifacts/jackson-core-2.18.11.jar").write_bytes(
            b"foreign vendor input"
        )
    archive(shaded, replacement)
    with pytest.raises(ValueError):
        merge.merge(original, shaded, root / "result.jar")
    assert not (root / "result.jar").exists()
