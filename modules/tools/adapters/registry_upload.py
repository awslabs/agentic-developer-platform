"""Publish a qualified Docker-save archive through ECR, without a Docker daemon.

The caller authenticates CodeBuild/S3 provenance and supplies the exact archive
checksum and qualified image-config digest. ``ecr(operation, **parameters)`` uses
the AWS ECR operation names/parameters (for example ``upload_layer_part`` with a
bytes ``layerPartBlob``). A CLI adapter can put that bounded blob in a private
temporary file and use ``fileb://``; this module needs only the standard library.

``guard()`` must recheck native authority before each request. The caller retains
durable ownership until requests settle, including uncertain PutImage replies;
these local checks do not implement cross-process lease fencing or retry policy.
"""

import gzip
import hashlib
import json
import os
import re
import stat
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

MAX_ARCHIVE = 2 * 1024**3
MAX_LAYER = 1024**3
MAX_PART = 20 * 1024**2  # ECR UploadLayerPart service-model limit.
MAX_JSON = 1024**2
CHUNK = 1024**2
MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
CONFIG_TYPE = "application/vnd.docker.container.image.v1+json"
LAYER_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"


class RegistryUploadError(RuntimeError):
    def __init__(self, reason, *, publication_uncertain=False, expected_image_digest=None):
        super().__init__("adapter_registry_" + reason)
        self.publication_uncertain = publication_uncertain
        self.expected_image_digest = expected_image_digest


def require(condition, reason):
    if not condition:
        raise RegistryUploadError(reason)


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _json(data):
    require(len(data) <= MAX_JSON, "json_limit")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_json_field")
            value[key] = item
        return value

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _value: require(False, "json_invalid"))
    except (ValueError, UnicodeError):
        raise RegistryUploadError("json_invalid") from None


def _snapshot(archive, target, checksum):
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source, target.open("xb") as output:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), "archive_not_regular")
        hasher, size = hashlib.sha256(), 0
        while block := source.read(CHUNK):
            size += len(block)
            require(size <= MAX_ARCHIVE, "archive_limit")
            hasher.update(block)
            output.write(block)
    require(size > 0 and hasher.hexdigest() == checksum, "archive_checksum_mismatch")


def _prepare(archive, work, checksum, config_digest, source, image):
    snapshot = work / "source.tar"
    _snapshot(archive, snapshot, checksum)
    with tarfile.open(snapshot, "r:*") as saved:
        members, total = {}, 0
        for member in saved:
            path = PurePosixPath(member.name)
            require(
                bool(member.name)
                and not path.is_absolute()
                and ".." not in path.parts
                and member.name not in members
                and (member.isfile() or member.isdir()),
                "archive_member_invalid",
            )
            total += member.size
            require(len(members) < 4096 and total <= MAX_ARCHIVE and 0 <= member.size <= MAX_LAYER, "archive_limit")
            members[member.name] = member

        def content(name, limit):
            require(isinstance(name, str) and name in members and members[name].isfile(), "archive_member_missing")
            require(members[name].size <= limit, "archive_member_limit")
            return saved.extractfile(members[name])

        with content("manifest.json", MAX_JSON) as stream:
            entries = _json(stream.read(MAX_JSON + 1))
        require(
            isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict), "single_image_required"
        )
        entry = entries[0]
        require(entry.get("RepoTags") == [image], "image_tag_mismatch")
        with content(entry.get("Config"), MAX_JSON) as stream:
            config_bytes = stream.read(MAX_JSON + 1)
        require(digest(config_bytes) == config_digest, "config_digest_mismatch")
        config = _json(config_bytes)
        require(
            isinstance(config, dict)
            and config.get("os") == "linux"
            and config.get("architecture") == "amd64"
            and isinstance(config.get("config"), dict)
            and config["config"].get("User") == "65532:65532"
            and isinstance(config["config"].get("Labels"), dict)
            and config["config"]["Labels"].get("org.opencontainers.image.revision") == source,
            "image_config_mismatch",
        )
        layers = entry.get("Layers")
        rootfs = config.get("rootfs")
        require(
            isinstance(layers, list)
            and 1 <= len(layers) <= 100
            and all(isinstance(name, str) for name in layers)
            and isinstance(rootfs, dict)
            and rootfs.get("type") == "layers"
            and isinstance(rootfs.get("diff_ids"), list)
            and len(rootfs["diff_ids"]) == len(layers),
            "layers_invalid",
        )
        config_path = work / "config.json"
        config_path.write_bytes(config_bytes)
        blobs = [(config_path, {"mediaType": CONFIG_TYPE, "size": len(config_bytes), "digest": config_digest})]
        inflated_bytes = 0
        for index, (name, expected) in enumerate(zip(layers, rootfs["diff_ids"], strict=True)):
            require(
                isinstance(expected, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", expected), "layer_digest_invalid"
            )
            layer = work / f"layer-{index}.gz"
            hasher, size = hashlib.sha256(), 0
            with content(name, MAX_LAYER) as incoming:
                magic = incoming.read(2)
                incoming.seek(0)
                # Docker-save archives may contain OCI-store gzip blobs or
                # legacy uncompressed layer.tar files. Validate uncompressed
                # diff IDs and produce deterministic gzip in either case.
                stream = gzip.GzipFile(fileobj=incoming) if magic == b"\x1f\x8b" else incoming
                with layer.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as output:
                    while block := stream.read(CHUNK):
                        size += len(block)
                        inflated_bytes += len(block)
                        require(size <= MAX_LAYER and inflated_bytes <= MAX_ARCHIVE, "layer_limit")
                        hasher.update(block)
                        output.write(block)
                if stream is not incoming:
                    stream.close()
            require("sha256:" + hasher.hexdigest() == expected, "layer_digest_mismatch")
            compressed_hash = hashlib.sha256()
            with layer.open("rb") as stream:
                while block := stream.read(CHUNK):
                    compressed_hash.update(block)
            blobs.append(
                (
                    layer,
                    {
                        "mediaType": LAYER_TYPE,
                        "size": layer.stat().st_size,
                        "digest": "sha256:" + compressed_hash.hexdigest(),
                    },
                )
            )
    manifest = canonical(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "config": blobs[0][1],
            "layers": [metadata for _, metadata in blobs[1:]],
        }
    )
    return blobs, manifest


def publish_archive(
    archive,
    *,
    archive_sha256,
    config_digest,
    source_revision,
    account_id,
    region,
    environment,
    ecr,
    guard,
    intent,
    settle,
):
    """Validate locally, then publish once; never retry an uncertain mutation.

    A matching existing immutable tag is read-only reuse. ``publication_uncertain``
    on an error means PutImage may have succeeded; preserve the native operation
    and reconcile the exact expected digest rather than blindly replaying it.
    """
    require(
        isinstance(archive_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", archive_sha256)
        and isinstance(config_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest)
        and isinstance(source_revision, str)
        and re.fullmatch(r"[0-9a-f]{40}", source_revision)
        and isinstance(account_id, str)
        and re.fullmatch(r"[0-9]{12}", account_id)
        and isinstance(region, str)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        and isinstance(environment, str)
        and environment in {"dev", "prod"}
        and callable(ecr)
        and callable(guard)
        and callable(intent)
        and callable(settle),
        "inputs_invalid",
    )
    repository = f"adp-tools-{environment}-adapters"
    image = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{source_revision}"
    publication_started = False
    expected_digest = None

    def authority():
        try:
            guard()
        except Exception:
            raise RegistryUploadError("authority_refused", publication_uncertain=publication_started) from None

    def call(operation, **parameters):
        nonlocal publication_started
        authority()
        if operation == "put_image":
            publication_started = True
        try:
            result = ecr(operation, registryId=account_id, **parameters)
        except Exception:
            raise RegistryUploadError("request_failed", publication_uncertain=publication_started) from None
        require(isinstance(result, dict), "response_invalid")
        return result

    try:
        authority()
        with tempfile.TemporaryDirectory(prefix="adp-registry-upload-") as directory:
            blobs, manifest = _prepare(
                Path(archive), Path(directory), archive_sha256, config_digest, source_revision, image
            )
            expected_digest = digest(manifest)

            def verify_repository():
                repositories = call("describe_repositories", repositoryNames=[repository]).get("repositories", [])
                require(
                    len(repositories) == 1
                    and repositories[0].get("repositoryName") == repository
                    and repositories[0].get("registryId") == account_id
                    and repositories[0].get("repositoryArn")
                    == f"arn:aws:ecr:{region}:{account_id}:repository/{repository}"
                    and repositories[0].get("repositoryUri") == image.rsplit(":", 1)[0]
                    and repositories[0].get("imageTagMutability") == "IMMUTABLE",
                    "repository_mismatch",
                )

            verify_repository()

            def read_image():
                result = call(
                    "batch_get_image",
                    repositoryName=repository,
                    imageIds=[{"imageTag": source_revision}],
                    acceptedMediaTypes=[MANIFEST_TYPE],
                )
                images, failures = result.get("images", []), result.get("failures", [])
                if not images:
                    require(
                        len(failures) == 1
                        and failures[0].get("failureCode") == "ImageNotFound"
                        and failures[0].get("imageId") == {"imageTag": source_revision},
                        "image_read_failed",
                    )
                    return False
                require(len(images) == 1 and not failures, "image_read_failed")
                observed = images[0]
                require(
                    observed.get("registryId") == account_id
                    and observed.get("repositoryName") == repository
                    and observed.get("imageId", {}).get("imageTag") == source_revision
                    and observed["imageId"].get("imageDigest") == expected_digest
                    and observed.get("imageManifest") == manifest.decode(),
                    "immutable_image_conflict",
                )
                return True

            reused = read_image()
            if not reused:
                for path, metadata in blobs:
                    checked = call(
                        "batch_check_layer_availability", repositoryName=repository, layerDigests=[metadata["digest"]]
                    )
                    rows = checked.get("layers", [])
                    require(
                        len(rows) == 1
                        and not checked.get("failures")
                        and rows[0].get("layerDigest") == metadata["digest"]
                        and rows[0].get("layerAvailability") in {"AVAILABLE", "UNAVAILABLE"},
                        "layer_read_failed",
                    )
                    if rows[0]["layerAvailability"] == "AVAILABLE":
                        require(rows[0].get("layerSize") == metadata["size"], "layer_size_mismatch")
                        continue
                    started = call("initiate_layer_upload", repositoryName=repository)
                    upload_id, part_size = started.get("uploadId"), started.get("partSize")
                    require(
                        isinstance(upload_id, str)
                        and str(uuid.UUID(upload_id)) == upload_id
                        and type(part_size) is int
                        and 0 < part_size <= MAX_PART,
                        "upload_parameters_invalid",
                    )
                    position = 0
                    with path.open("rb") as stream:
                        while block := stream.read(part_size):
                            end = position + len(block) - 1
                            uploaded = call(
                                "upload_layer_part",
                                repositoryName=repository,
                                uploadId=upload_id,
                                partFirstByte=position,
                                partLastByte=end,
                                layerPartBlob=block,
                            )
                            require(
                                uploaded.get("registryId") == account_id
                                and uploaded.get("repositoryName") == repository
                                and uploaded.get("uploadId") == upload_id
                                and uploaded.get("lastByteReceived") == end,
                                "upload_part_mismatch",
                            )
                            position = end + 1
                    require(position == metadata["size"], "upload_size_mismatch")
                    completed = call(
                        "complete_layer_upload",
                        repositoryName=repository,
                        uploadId=upload_id,
                        layerDigests=[metadata["digest"]],
                    )
                    require(
                        completed.get("registryId") == account_id
                        and completed.get("repositoryName") == repository
                        and completed.get("uploadId") == upload_id
                        and completed.get("layerDigest") == metadata["digest"],
                        "upload_completion_mismatch",
                    )
                try:
                    intent(expected_digest)
                except Exception:
                    raise RegistryUploadError("publication_intent_refused") from None
                recovered = False
                try:
                    result = call(
                        "put_image",
                        repositoryName=repository,
                        imageTag=source_revision,
                        imageDigest=expected_digest,
                        imageManifest=manifest.decode(),
                        imageManifestMediaType=MANIFEST_TYPE,
                    )
                except RegistryUploadError as failure:
                    if str(failure) != "adapter_registry_request_failed":
                        raise
                    verify_repository()
                    recovered = read_image()
                    require(recovered, "publication_not_observed")
                else:
                    require(
                        result.get("image", {}).get("imageId", {}).get("imageDigest") == expected_digest,
                        "publication_digest_mismatch",
                    )
                require(recovered or read_image(), "publication_not_observed")
                try:
                    settle(expected_digest)
                except Exception:
                    raise RegistryUploadError("publication_settlement_refused", publication_uncertain=True) from None
            authority()
            return {
                "schema_version": "1.0",
                "repository": repository,
                "source_revision": source_revision,
                "archive_sha256": archive_sha256,
                "config_digest": config_digest,
                "image_digest": expected_digest,
                "reused": reused,
            }
    except RegistryUploadError as error:
        error.publication_uncertain = error.publication_uncertain or publication_started
        error.expected_image_digest = expected_digest
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError, EOFError):
        raise RegistryUploadError(
            "archive_or_response_invalid",
            publication_uncertain=publication_started,
            expected_image_digest=expected_digest,
        ) from None
