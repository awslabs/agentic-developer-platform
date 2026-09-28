#!/usr/bin/env python3
"""Build the reproducible S03 SkyPilot image as an OCI image layout."""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

BASE_REPOSITORY = "berkeleyskypilot/skypilot"
BASE_INDEX_DIGEST = (
    "sha256:0c0c0db86ee31559f9c98f2331b7d91ee84b61e7ab97bb6cbcd89938c89d37e4"
)
BASE_AMD64_MANIFEST_DIGEST = (
    "sha256:46d3886317e1c38ac4d21c4b4c335d93d0320433dee86df985f396de8a9bee8f"
)
SETUPTOOLS_VERSION = "81.0.0"
SETUPTOOLS_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/e1/e3/"
    "c164c88b2e5ce7b24d667b9bd83589cf4f3520d97cad01534cd3c4f55fdb/"
    "setuptools-81.0.0-py3-none-any.whl"
)
SETUPTOOLS_WHEEL_SHA256 = (
    "fdd925d5c5d9f62e4b74b30d6dd7828ce236fd6ed998a08d81de62ce5a6310d6"
)
SETUPTOOLS_WHEEL_SIZE = 1_062_021
JARACO_CONTEXT_SHA256 = (
    "6ebd727581a8d57aff3eed5a9ee11d77e42b8d48b3fc46392ffe44e02d372f8c"
)
SOURCE_DATE_EPOCH = 1_790_035_200
CREATED = "2026-09-22T00:00:00Z"
SITE_PACKAGES = PurePosixPath("usr/local/lib/python3.10/site-packages")
OLD_DIST_INFO = "setuptools-78.1.1.dist-info"
EXPECTED_TOP_LEVEL = {
    "_distutils_hack",
    "distutils-precedence.pth",
    "pkg_resources",
    "setuptools",
    "setuptools-81.0.0.dist-info",
}
REPLACED_DIRECTORIES = {"_distutils_hack", "pkg_resources", "setuptools"}
EXPECTED_MANIFEST_DIGEST = (
    "sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec"
)


class BuildError(RuntimeError):
    pass


def digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def verify(content: bytes, expected_digest: str, description: str) -> None:
    actual = digest_bytes(content)
    if actual != expected_digest:
        raise BuildError(f"{description}: expected {expected_digest}, got {actual}")


class CrossHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, url
        )
        if redirected is not None and (
            urllib.parse.urlsplit(request.full_url).netloc
            != urllib.parse.urlsplit(url).netloc
        ):
            redirected.remove_header("Authorization")
        return redirected


def request_bytes(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(CrossHostRedirectHandler())
    with opener.open(request, timeout=120) as response:
        return response.read()


def docker_hub_token() -> str:
    query = urllib.parse.urlencode(
        {
            "service": "registry.docker.io",
            "scope": f"repository:{BASE_REPOSITORY}:pull",
        }
    )
    response = json.loads(request_bytes(f"https://auth.docker.io/token?{query}"))
    return str(response["token"])


def registry_blob(digest: str, token: str, accept: str | None = None) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = accept
    resource = "manifests" if accept else "blobs"
    return request_bytes(
        f"https://registry-1.docker.io/v2/{BASE_REPOSITORY}/{resource}/{digest}",
        headers=headers,
    )


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={encoded.decode()}"


def installed_wheel_files(wheel: zipfile.ZipFile) -> dict[str, tuple[bytes, int]]:
    files: dict[str, tuple[bytes, int]] = {}
    top_level: set[str] = set()
    for item in wheel.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts:
            raise BuildError(f"unsafe path in setuptools wheel: {item.filename}")
        if not path.parts:
            continue
        top_level.add(path.parts[0])
        if item.is_dir():
            continue
        mode = (item.external_attr >> 16) & 0o777
        files[path.as_posix()] = (wheel.read(item), mode or 0o644)

    if top_level != EXPECTED_TOP_LEVEL:
        raise BuildError(
            f"unexpected setuptools wheel top-level paths: {sorted(top_level)}"
        )

    dist_info = f"setuptools-{SETUPTOOLS_VERSION}.dist-info"
    installer = b"pip\n"
    requested = b""
    files[f"{dist_info}/INSTALLER"] = (installer, 0o644)
    files[f"{dist_info}/REQUESTED"] = (requested, 0o644)

    record_path = f"{dist_info}/RECORD"
    rows = list(csv.reader(io.StringIO(files[record_path][0].decode())))
    rows = [row for row in rows if row[0] != record_path]
    rows.extend(
        [
            [f"{dist_info}/INSTALLER", record_hash(installer), str(len(installer))],
            [f"{dist_info}/REQUESTED", record_hash(requested), "0"],
            [record_path, "", ""],
        ]
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(sorted(rows, key=lambda row: row[0]))
    files[record_path] = (output.getvalue().encode(), 0o644)

    jaraco_path = "setuptools/_vendor/jaraco/context/__init__.py"
    verify(files[jaraco_path][0], f"sha256:{JARACO_CONTEXT_SHA256}", jaraco_path)
    wheel_metadata = files["setuptools/_vendor/wheel-0.46.3.dist-info/METADATA"][0]
    if b"\nVersion: 0.46.3\n" not in wheel_metadata:
        raise BuildError("setuptools wheel does not vendor wheel 0.46.3")
    return files


def tar_info(name: str, *, mode: int, size: int = 0, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = SOURCE_DATE_EPOCH
    info.size = size
    info.type = kind
    return info


def build_layer(wheel_content: bytes) -> tuple[bytes, str]:
    with zipfile.ZipFile(io.BytesIO(wheel_content)) as wheel:
        files = installed_wheel_files(wheel)

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        old_dist_whiteout = SITE_PACKAGES / f".wh.{OLD_DIST_INFO}"
        archive.addfile(
            tar_info(old_dist_whiteout.as_posix(), mode=0, kind=tarfile.REGTYPE),
            io.BytesIO(b""),
        )

        directories: set[PurePosixPath] = set()
        for relative in files:
            parent = PurePosixPath(relative).parent
            while parent != PurePosixPath("."):
                directories.add(parent)
                parent = parent.parent

        for directory in sorted(
            directories, key=lambda path: (len(path.parts), str(path))
        ):
            target = SITE_PACKAGES / directory
            archive.addfile(
                tar_info(f"{target.as_posix()}/", mode=0o755, kind=tarfile.DIRTYPE)
            )
            if len(directory.parts) == 1 and directory.name in REPLACED_DIRECTORIES:
                opaque = target / ".wh..wh..opq"
                archive.addfile(
                    tar_info(opaque.as_posix(), mode=0, kind=tarfile.REGTYPE),
                    io.BytesIO(b""),
                )

        for relative, (content, mode) in sorted(files.items()):
            target = SITE_PACKAGES / relative
            archive.addfile(
                tar_info(
                    target.as_posix(),
                    mode=mode,
                    size=len(content),
                    kind=tarfile.REGTYPE,
                ),
                io.BytesIO(content),
            )

    uncompressed = stream.getvalue()
    compressed_stream = io.BytesIO()
    with gzip.GzipFile(
        fileobj=compressed_stream, mode="wb", filename="", mtime=SOURCE_DATE_EPOCH
    ) as compressed:
        compressed.write(uncompressed)
    return compressed_stream.getvalue(), digest_bytes(uncompressed)


def write_blob(layout: Path, content: bytes) -> tuple[str, int]:
    digest = digest_bytes(content)
    destination = layout / "blobs" / "sha256" / digest.removeprefix("sha256:")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return digest, len(content)


def download_base(layout: Path) -> tuple[dict, dict, bytes]:
    token = docker_hub_token()
    index_content = registry_blob(
        BASE_INDEX_DIGEST,
        token,
        "application/vnd.oci.image.index.v1+json,"
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
    verify(index_content, BASE_INDEX_DIGEST, "base image index")
    index = json.loads(index_content)
    amd64 = next(
        descriptor
        for descriptor in index["manifests"]
        if descriptor.get("platform", {}).get("os") == "linux"
        and descriptor.get("platform", {}).get("architecture") == "amd64"
    )
    if amd64["digest"] != BASE_AMD64_MANIFEST_DIGEST:
        raise BuildError(
            f"base amd64 manifest changed: expected {BASE_AMD64_MANIFEST_DIGEST}, "
            f"got {amd64['digest']}"
        )

    manifest_content = registry_blob(
        BASE_AMD64_MANIFEST_DIGEST,
        token,
        "application/vnd.oci.image.manifest.v1+json,"
        "application/vnd.docker.distribution.manifest.v2+json",
    )
    verify(manifest_content, BASE_AMD64_MANIFEST_DIGEST, "base amd64 manifest")
    manifest = json.loads(manifest_content)

    config_content = b""
    for descriptor in [manifest["config"], *manifest["layers"]]:
        digest = descriptor["digest"]
        blob_path = layout / "blobs" / "sha256" / digest.removeprefix("sha256:")
        content = registry_blob(digest, token)
        verify(content, digest, f"base blob {digest}")
        if len(content) != descriptor["size"]:
            raise BuildError(f"base blob {digest} has unexpected size")
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(content)
        if digest == manifest["config"]["digest"]:
            config_content = content
    return manifest, json.loads(config_content), index_content


def build(output: Path) -> str:
    if output.exists() and any(output.iterdir()):
        raise BuildError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    manifest, config, base_index_content = download_base(output)
    wheel_content = request_bytes(SETUPTOOLS_WHEEL_URL)
    verify(
        wheel_content,
        f"sha256:{SETUPTOOLS_WHEEL_SHA256}",
        "setuptools wheel",
    )
    if len(wheel_content) != SETUPTOOLS_WHEEL_SIZE:
        raise BuildError("setuptools wheel has unexpected size")

    layer, layer_diff_id = build_layer(wheel_content)
    layer_digest, layer_size = write_blob(output, layer)

    config["rootfs"]["diff_ids"].append(layer_diff_id)
    config.setdefault("history", []).append(
        {
            "created": CREATED,
            "created_by": (
                "ADP S03: replace setuptools 78.1.1 with 81.0.0 from pinned wheel "
                f"sha256:{SETUPTOOLS_WHEEL_SHA256}"
            ),
            "comment": "Reproducible security rebuild for issue 5602",
        }
    )
    config_content = canonical_json(config)
    config_digest, config_size = write_blob(output, config_content)

    manifest["config"] = {
        **manifest["config"],
        "digest": config_digest,
        "size": config_size,
    }
    manifest["layers"].append(
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": layer_digest,
            "size": layer_size,
        }
    )
    manifest_content = canonical_json(manifest)
    manifest_digest, manifest_size = write_blob(output, manifest_content)
    if EXPECTED_MANIFEST_DIGEST and manifest_digest != EXPECTED_MANIFEST_DIGEST:
        raise BuildError(
            f"reproducibility check failed: expected {EXPECTED_MANIFEST_DIGEST}, "
            f"got {manifest_digest}"
        )

    (output / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n')
    output_index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": manifest["mediaType"],
                "digest": manifest_digest,
                "size": manifest_size,
                "platform": {"architecture": "amd64", "os": "linux"},
                "annotations": {
                    "org.opencontainers.image.ref.name": (
                        f"skypilot:0.12.3-setuptools-{SETUPTOOLS_VERSION}"
                    )
                },
            }
        ],
    }
    (output / "index.json").write_bytes(canonical_json(output_index) + b"\n")
    provenance = {
        "manifest_digest": manifest_digest,
        "base_index_digest": BASE_INDEX_DIGEST,
        "base_amd64_manifest_digest": BASE_AMD64_MANIFEST_DIGEST,
        "base_index_content_sha256": digest_bytes(base_index_content),
        "setuptools_version": SETUPTOOLS_VERSION,
        "setuptools_wheel_url": SETUPTOOLS_WHEEL_URL,
        "setuptools_wheel_sha256": f"sha256:{SETUPTOOLS_WHEEL_SHA256}",
        "layer_digest": layer_digest,
        "layer_diff_id": layer_diff_id,
        "config_digest": config_digest,
        "source_date_epoch": SOURCE_DATE_EPOCH,
    }
    (output / "provenance.json").write_bytes(canonical_json(provenance) + b"\n")
    return manifest_digest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output", type=Path, help="empty output directory for the OCI layout"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        print(build(args.output))
    except (BuildError, OSError, ValueError, KeyError, StopIteration) as error:
        print(f"build failed: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
