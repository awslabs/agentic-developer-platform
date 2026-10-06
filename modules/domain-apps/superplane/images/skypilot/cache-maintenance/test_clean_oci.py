"""Finite overlay tests, with harmless synthetic bytes and OCI archives."""

import importlib.util
import io
import os
import tarfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("clean_oci", HERE / "clean_oci.py")
CLEAN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CLEAN)
ROOT = next(
    p for p in HERE.parents if (p / "codebuild/exact_image_disposition.py").is_file()
)
D = CLEAN.load_verifier(
    Path(
        os.environ.get(
            "ADP_COMPONENT_VERIFIER", ROOT / "codebuild/exact_image_disposition.py"
        )
    )
)
SOURCE = "/usr/local/lib/python3.10/email/_parseaddr.py"
CACHE = "/usr/local/lib/python3.10/email/__pycache__/_parseaddr.cpython-310.pyc"
RECORD = "/usr/local/lib/python3.10/site-packages/fixture.dist-info/RECORD"
UNRELATED = "/usr/local/lib/python3.10/site-packages/unrelated/cached.pyc"


def fixture(tmp_path, orphan=False):
    layer = io.BytesIO()
    payload = {
        SOURCE: b"harmless source\n",
        CACHE: b"old cache",
        UNRELATED: b"preserve cache",
    }
    payload[RECORD] = (
        b"../email/__pycache__/_parseaddr.cpython-310.pyc,,\r\nfixture.dist-info/RECORD,,\r\n"
    )
    if orphan:
        del payload[SOURCE]
    with tarfile.open(fileobj=layer, mode="w") as out:
        for name, data in payload.items():
            entry = CLEAN.tar_entry(name.lstrip("/"), data)
            entry.uid, entry.gid, entry.mtime = 123, 456, 789
            out.addfile(entry, io.BytesIO(data))
    layer_bytes = layer.getvalue()
    layer_digest = "sha256:" + D.sha(layer_bytes)
    config = D.canonical(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"User": "1000", "WorkingDir": "/work"},
            "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
        }
    )
    manifest = D.canonical(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + D.sha(config), "size": len(config)},
            "layers": [
                {
                    "digest": layer_digest,
                    "size": len(layer_bytes),
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                }
            ],
        }
    )
    archive = tmp_path / "producer.tar"
    with tarfile.open(archive, "w") as out:
        for data in (layer_bytes, config, manifest):
            out.addfile(
                CLEAN.tar_entry("blobs/sha256/" + D.sha(data), data), io.BytesIO(data)
            )
        layout = b'{"imageLayoutVersion":"1.0.0"}'
        out.addfile(CLEAN.tar_entry("oci-layout", layout), io.BytesIO(layout))
    return archive, "sha256:" + D.sha(manifest)


def test_complete_overlay_preserves_unrelated_bytes_and_record_ownership(tmp_path):
    archive, platform = fixture(tmp_path)
    first = CLEAN.build(D, archive, platform, tmp_path / "first")
    second = CLEAN.build(D, archive, platform, tmp_path / "second")
    assert first == second
    assert len(first["transition"]["removed_caches"]) == 1
    result = tmp_path / "first/image.tar"
    _, _, files, retained, _ = D.component_verifier().read_oci(
        D, result, first["output"]["platform_digest"]
    )
    assert CACHE not in files
    assert files[UNRELATED]["sha256"] == D.sha(b"preserve cache")
    assert retained[RECORD] == b"fixture.dist-info/RECORD,,\r\n"
    with tarfile.open(result) as archive:
        raw = archive.extractfile("blobs/sha256/" + first["layer_digest"].split(":")[1])
        with tarfile.open(fileobj=raw) as layer:
            header = layer.getmember(RECORD.lstrip("/"))
            assert (header.uid, header.gid, header.mode, header.mtime) == (
                123,
                456,
                0o644,
                789,
            )
            assert len(layer.getmembers()) == 2


def test_orphan_cache_rejected_before_output(tmp_path):
    archive, platform = fixture(tmp_path, orphan=True)
    with pytest.raises(D.Invalid, match="orphan or linked cache"):
        CLEAN.build(D, archive, platform, tmp_path / "result")
    assert not (tmp_path / "result").exists()


def test_existing_output_cannot_be_replaced(tmp_path):
    archive, platform = fixture(tmp_path)
    target = tmp_path / "protected"
    target.mkdir()
    with pytest.raises(D.Invalid, match="output directory already exists"):
        CLEAN.build(D, archive, platform, target)
