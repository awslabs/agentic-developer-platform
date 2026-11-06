"""Exercise archive validation and ECR requests without Docker or cloud writes."""

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("registry_upload", Path(__file__).parents[1] / "registry_upload.py")
uploader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uploader)
SOURCE = "a" * 40
ACCOUNT = "123456789012"
REPOSITORY = "adp-tools-dev-adapters"
TAG = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/{REPOSITORY}:{SOURCE}"
WRITES = {"initiate_layer_upload", "upload_layer_part", "complete_layer_upload", "put_image"}


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def publication(tmp_path):
    layer = b"qualified-image-layer\n" * 100
    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"User": "65532:65532", "Labels": {"org.opencontainers.image.revision": SOURCE}},
        "rootfs": {"type": "layers", "diff_ids": [sha(layer)]},
    }
    state = SimpleNamespace(
        layer=layer,
        config=config,
        path=tmp_path / "image.tar",
        compressed=False,
        calls=[],
        uploads={},
        layers={},
        image=None,
        authorized=True,
        fault=None,
        repository_mutable=False,
        archive_extra=None,
        closed_paths=[],
    )

    def save():
        config_bytes = json.dumps(state.config).encode()
        contents = {
            "manifest.json": json.dumps(
                [{"Config": "config.json", "RepoTags": [TAG], "Layers": ["layer.tar"]}]
            ).encode(),
            "config.json": config_bytes,
            "layer.tar": gzip.compress(state.layer, mtime=1234) if state.compressed else state.layer,
        }
        with tarfile.open(state.path, "w") as archive:
            for name, data in contents.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if state.archive_extra:
                archive.addfile(state.archive_extra)
        state.arguments = {
            "archive_sha256": hashlib.sha256(state.path.read_bytes()).hexdigest(),
            "config_digest": sha(config_bytes),
            "source_revision": SOURCE,
            "account_id": ACCOUNT,
            "region": "us-east-1",
            "environment": "dev",
        }

    def guard():
        if not state.authorized:
            raise RuntimeError("private authority failure")

    def ecr(operation, **params):
        state.calls.append((operation, params))
        assert params["registryId"] == ACCOUNT
        if operation == "describe_repositories":
            assert params["repositoryNames"] == [REPOSITORY]
            return {
                "repositories": [
                    {
                        "registryId": ACCOUNT,
                        "repositoryName": REPOSITORY,
                        "repositoryArn": f"arn:aws:ecr:us-east-1:{ACCOUNT}:repository/{REPOSITORY}",
                        "repositoryUri": TAG.rsplit(":", 1)[0],
                        "imageTagMutability": "MUTABLE" if state.repository_mutable else "IMMUTABLE",
                    }
                ]
            }
        assert params["repositoryName"] == REPOSITORY
        if operation == "batch_get_image":
            assert params["imageIds"] == [{"imageTag": SOURCE}]
            if state.image:
                return {"images": [state.image], "failures": []}
            return {"images": [], "failures": [{"imageId": {"imageTag": SOURCE}, "failureCode": "ImageNotFound"}]}
        if operation == "batch_check_layer_availability":
            value = params["layerDigests"][0]
            data = state.layers.get(value)
            return {
                "layers": [
                    {
                        "layerDigest": value,
                        "layerAvailability": "AVAILABLE" if data else "UNAVAILABLE",
                        **({"layerSize": len(data)} if data else {}),
                    }
                ],
                "failures": [],
            }
        if operation == "initiate_layer_upload":
            value = str(uuid.UUID(int=len(state.uploads) + 1))
            state.uploads[value] = bytearray()
            return {"uploadId": value, "partSize": 127}
        if operation == "upload_layer_part":
            data = state.uploads[params["uploadId"]]
            assert params["partFirstByte"] == len(data)
            data.extend(params["layerPartBlob"])
            assert params["partLastByte"] == len(data) - 1
            if state.fault == "owner_during_layer":
                state.authorized = False
            return {
                "registryId": ACCOUNT,
                "repositoryName": REPOSITORY,
                "uploadId": params["uploadId"],
                "lastByteReceived": -1 if state.fault == "wrong_part" else len(data) - 1,
            }
        if operation == "complete_layer_upload":
            data = bytes(state.uploads[params["uploadId"]])
            assert params["layerDigests"] == [sha(data)]
            state.layers[sha(data)] = data
            return {
                "registryId": ACCOUNT,
                "repositoryName": REPOSITORY,
                "uploadId": params["uploadId"],
                "layerDigest": "sha256:" + "f" * 64 if state.fault == "wrong_completion" else sha(data),
            }
        assert operation == "put_image"
        assert params["imageTag"] == SOURCE
        if state.fault == "lost_put_reply_no_write":
            raise TimeoutError("private transport detail")
        assert params["imageDigest"] == sha(params["imageManifest"].encode())
        state.image = {
            "registryId": ACCOUNT,
            "repositoryName": REPOSITORY,
            "imageId": {"imageTag": SOURCE, "imageDigest": params["imageDigest"]},
            "imageManifest": params["imageManifest"],
        }
        if state.fault == "lost_put_reply_conflict":
            state.image["imageId"]["imageDigest"] = "sha256:" + "f" * 64
        if state.fault == "lost_put_reply_mutable":
            state.repository_mutable = True
        if state.fault == "owner_during_put":
            state.authorized = False
        if state.fault in {"lost_put_reply", "lost_put_reply_conflict", "lost_put_reply_mutable"}:
            raise TimeoutError("private transport detail")
        return {"image": state.image}

    state.save, state.ecr, state.guard = save, ecr, guard
    save()
    return state


def publish(state):
    return uploader.publish_archive(
        state.path,
        **state.arguments,
        ecr=state.ecr,
        guard=state.guard,
        intent=lambda digest: None,
        settle=lambda digest: None,
    )


@pytest.mark.parametrize("compressed", [False, True])
def test_archive_publishes_through_bounded_ecr_calls_and_independent_readback(publication, compressed):
    state = publication
    state.compressed = compressed
    state.save()
    report = publish(state)
    manifest = json.loads(state.image["imageManifest"])
    config = json.loads(state.layers[manifest["config"]["digest"]])
    layer = gzip.decompress(state.layers[manifest["layers"][0]["digest"]])
    assert layer == state.layer
    assert sha(layer) == config["rootfs"]["diff_ids"][0]
    assert config == state.config
    assert report["image_digest"] == state.image["imageId"]["imageDigest"]
    assert report["config_digest"] == state.arguments["config_digest"]
    assert report["archive_sha256"] == state.arguments["archive_sha256"]
    assert report["reused"] is False
    assert [name for name, _ in state.calls][-2:] == ["put_image", "batch_get_image"]
    chunks = [params["layerPartBlob"] for name, params in state.calls if name == "upload_layer_part"]
    assert len(chunks) > 2 and max(map(len, chunks)) <= 127
    assert state.path.is_file()  # Caller retains its immutable source.


def test_same_qualified_image_reuses_existing_tag_without_writes(publication):
    first = publish(publication)
    publication.calls.clear()
    second = publish(publication)
    assert first["image_digest"] == second["image_digest"]
    assert second["reused"] is True
    assert [name for name, _ in publication.calls] == ["describe_repositories", "batch_get_image"]


def test_compressed_and_uncompressed_save_layouts_produce_same_image(publication):
    first = publish(publication)
    publication.compressed = True
    publication.save()
    publication.calls.clear()
    second = publish(publication)
    assert first["image_digest"] == second["image_digest"] and second["reused"]
    assert not any(name in WRITES for name, _ in publication.calls)


@pytest.mark.parametrize(
    "difference",
    ["checksum", "config_digest", "source", "root_user", "layer", "traversal", "symlink", "duplicate", "limit"],
)
def test_invalid_archive_refused_before_any_ecr_call(publication, monkeypatch, difference):
    state = publication
    if difference == "checksum":
        state.arguments["archive_sha256"] = "f" * 64
    elif difference == "config_digest":
        state.arguments["config_digest"] = "sha256:" + "f" * 64
    elif difference in {"source", "root_user"}:
        if difference == "source":
            state.config["config"]["Labels"]["org.opencontainers.image.revision"] = "b" * 40
        else:
            state.config["config"]["User"] = "root"
        state.save()
    elif difference == "layer":
        state.layer = b"changed after qualification"
        state.save()
    elif difference == "limit":
        monkeypatch.setattr(uploader, "MAX_ARCHIVE", 100)
    else:
        state.archive_extra = tarfile.TarInfo("../escape" if difference == "traversal" else "config.json")
        if difference == "symlink":
            state.archive_extra.name = "link"
            state.archive_extra.type = tarfile.SYMTYPE
            state.archive_extra.linkname = "/outside"
        state.save()
    with pytest.raises(uploader.RegistryUploadError) as failure:
        publish(state)
    assert failure.value.publication_uncertain is False
    assert not state.calls


def test_archive_symlink_cannot_select_another_file(publication):
    target = publication.path.with_suffix(".link")
    target.symlink_to(publication.path)
    publication.path = target
    with pytest.raises(uploader.RegistryUploadError):
        publish(publication)
    assert not publication.calls


def test_mutable_repository_and_conflicting_tag_refuse_mutation(publication):
    publication.repository_mutable = True
    with pytest.raises(uploader.RegistryUploadError, match="repository_mismatch"):
        publish(publication)
    assert not any(name in WRITES for name, _ in publication.calls)
    publication.repository_mutable = False
    publish(publication)
    publication.image["imageId"]["imageDigest"] = "sha256:" + "f" * 64
    publication.calls.clear()
    with pytest.raises(uploader.RegistryUploadError, match="immutable_image_conflict"):
        publish(publication)
    assert not any(name in WRITES for name, _ in publication.calls)


@pytest.mark.parametrize("fault", ["owner_during_layer", "wrong_part", "wrong_completion"])
def test_partial_layer_failure_never_puts_an_image(publication, fault):
    publication.fault = fault
    with pytest.raises(uploader.RegistryUploadError) as failure:
        publish(publication)
    assert failure.value.publication_uncertain is False
    assert not any(name == "put_image" for name, _ in publication.calls)


def test_fenced_put_is_retained_as_uncertain_without_replay(publication):
    publication.fault = "owner_during_put"
    with pytest.raises(uploader.RegistryUploadError) as failure:
        publish(publication)
    assert failure.value.publication_uncertain is True
    assert str(failure.value) == "adapter_registry_authority_refused"
    assert publication.image is not None
    assert failure.value.expected_image_digest == publication.image["imageId"]["imageDigest"]
    assert [name for name, _ in publication.calls].count("put_image") == 1
    assert publication.calls[-1][0] == "put_image"


def test_lost_put_reply_recovers_matching_image_without_replay(publication):
    publication.fault = "lost_put_reply"
    settled = []
    result = uploader.publish_archive(
        publication.path,
        **publication.arguments,
        ecr=publication.ecr,
        guard=publication.guard,
        intent=lambda digest: None,
        settle=settled.append,
    )
    assert settled == [result["image_digest"]]
    assert [name for name, _ in publication.calls].count("put_image") == 1
    assert [name for name, _ in publication.calls][-2:] == ["describe_repositories", "batch_get_image"]


@pytest.mark.parametrize("fault", ["lost_put_reply_no_write", "lost_put_reply_conflict", "lost_put_reply_mutable"])
def test_lost_reply_without_matching_immutable_readback_preserves_intent(publication, fault):
    publication.fault = fault
    settled = []
    with pytest.raises(uploader.RegistryUploadError) as failure:
        uploader.publish_archive(
            publication.path,
            **publication.arguments,
            ecr=publication.ecr,
            guard=publication.guard,
            intent=lambda digest: None,
            settle=settled.append,
        )
    assert failure.value.publication_uncertain is True
    assert failure.value.expected_image_digest is not None
    assert settled == []
    assert [name for name, _ in publication.calls].count("put_image") == 1


def test_publication_intent_is_recorded_before_put_and_refusal_prevents_put(publication):
    intents = []

    def intent(digest):
        assert not any(operation == "put_image" for operation, _ in publication.calls)
        intents.append(digest)
        raise RuntimeError("controlled intent refusal")

    with pytest.raises(uploader.RegistryUploadError, match="publication_intent_refused") as failure:
        uploader.publish_archive(
            publication.path,
            **publication.arguments,
            ecr=publication.ecr,
            guard=publication.guard,
            intent=intent,
            settle=lambda digest: None,
        )
    assert not failure.value.publication_uncertain
    assert intents == [failure.value.expected_image_digest]
    assert not any(operation == "put_image" for operation, _ in publication.calls)


def test_owner_loss_before_start_performs_no_requests(publication):
    publication.authorized = False
    with pytest.raises(uploader.RegistryUploadError, match="authority_refused") as failure:
        publish(publication)
    assert not failure.value.publication_uncertain and not publication.calls


def test_wrong_region_repository_is_rejected_before_upload(publication):
    original = publication.ecr

    def ecr(operation, **params):
        response = original(operation, **params)
        if operation == "describe_repositories":
            response["repositories"][0]["repositoryArn"] = f"arn:aws:ecr:eu-west-1:{ACCOUNT}:repository/{REPOSITORY}"
        return response

    publication.ecr = ecr
    with pytest.raises(uploader.RegistryUploadError, match="repository_mismatch"):
        publish(publication)
    assert not any(name in WRITES for name, _ in publication.calls)


def test_malformed_put_response_preserves_publication_uncertainty(publication):
    original = publication.ecr

    def ecr(operation, **params):
        response = original(operation, **params)
        return {"image": None} if operation == "put_image" else response

    publication.ecr = ecr
    with pytest.raises(uploader.RegistryUploadError) as failure:
        publish(publication)
    assert failure.value.publication_uncertain
    assert publication.image is not None
    assert failure.value.expected_image_digest == publication.image["imageId"]["imageDigest"]
    assert publication.calls[-1][0] == "put_image"


def test_bounded_api_upload_id_and_chunk_size_are_required(publication):
    original = publication.ecr

    def ecr(operation, **params):
        response = original(operation, **params)
        if operation == "initiate_layer_upload":
            response["partSize"] = uploader.MAX_PART + 1
        return response

    publication.ecr = ecr
    with pytest.raises(uploader.RegistryUploadError, match="upload_parameters_invalid"):
        publish(publication)
    assert not any(name == "upload_layer_part" for name, _ in publication.calls)


def test_settlement_requires_matching_readback_after_successful_put(publication):
    markers = []

    def intent(digest):
        assert not any(operation == "put_image" for operation, _ in publication.calls)
        markers.append(("intent", digest))

    def settle(digest):
        assert publication.calls[-1][0] == "batch_get_image"
        assert publication.image["imageId"]["imageDigest"] == digest
        markers.append(("settled", digest))

    report = uploader.publish_archive(
        publication.path,
        **publication.arguments,
        ecr=publication.ecr,
        guard=publication.guard,
        intent=intent,
        settle=settle,
    )
    assert markers == [("intent", report["image_digest"]), ("settled", report["image_digest"])]
    markers.clear()
    reused = uploader.publish_archive(
        publication.path,
        **publication.arguments,
        ecr=publication.ecr,
        guard=publication.guard,
        intent=intent,
        settle=settle,
    )
    assert reused["reused"] is True and not markers


def test_settlement_refusal_preserves_uncertain_publication(publication):
    def refuse(digest):
        raise RuntimeError("controlled settlement loss")

    with pytest.raises(uploader.RegistryUploadError, match="settlement_refused") as failure:
        uploader.publish_archive(
            publication.path,
            **publication.arguments,
            ecr=publication.ecr,
            guard=publication.guard,
            intent=lambda digest: None,
            settle=refuse,
        )
    assert failure.value.publication_uncertain
    assert failure.value.expected_image_digest == publication.image["imageId"]["imageDigest"]
    assert [operation for operation, _ in publication.calls].count("put_image") == 1
