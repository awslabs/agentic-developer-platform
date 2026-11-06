"""Controlled immutable-tag readback for unsettled native image publication."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.orchestration.operations_publication_recovery import MANIFEST_TYPE, ImageAbsence, read_pending_image
from src.orchestration.review_cycle import CycleBlockedError

ACCOUNT = "000000000000"
REGION = "us-east-1"
SOURCE = "a" * 40
REPOSITORY = "adp-tools-dev-adapters"
MANIFEST = '{"schemaVersion":2}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST.encode()).hexdigest()


@pytest.fixture
def registry():
    client = SimpleNamespace(calls=[], mutable=False, image=True, conflict=False, unavailable=False, response=None)

    def describe_repositories(**kwargs):
        client.calls.append(("describe_repositories", kwargs))
        if client.unavailable:
            raise RuntimeError("private provider message")
        return {
            "repositories": [
                {
                    "registryId": ACCOUNT,
                    "repositoryName": REPOSITORY,
                    "repositoryArn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{REPOSITORY}",
                    "repositoryUri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPOSITORY}",
                    "imageTagMutability": "MUTABLE" if client.mutable else "IMMUTABLE",
                }
            ]
        }

    def batch_get_image(**kwargs):
        client.calls.append(("batch_get_image", kwargs))
        if client.response is not None:
            return client.response
        if not client.image:
            return {"images": [], "failures": [{"imageId": {"imageTag": SOURCE}, "failureCode": "ImageNotFound"}]}
        return {
            "images": [
                {
                    "registryId": ACCOUNT,
                    "repositoryName": REPOSITORY,
                    "imageId": {"imageTag": SOURCE, "imageDigest": "sha256:" + "b" * 64 if client.conflict else DIGEST},
                    "imageManifestMediaType": MANIFEST_TYPE,
                    "imageManifest": MANIFEST,
                }
            ],
            "failures": [],
        }

    client.describe_repositories = describe_repositories
    client.batch_get_image = batch_get_image

    class Scoped:
        def client(self, name, *, config):
            assert name == "ecr"
            if getattr(client, "scope_unavailable", False):
                raise RuntimeError("private provider message")
            assert config.retries["total_max_attempts"] == 1
            assert config.connect_timeout == 3 and config.read_timeout == 10
            return client

    return Scoped(), client


def observe(registry, **changes):
    scoped, _ = registry
    options = dict(account_id=ACCOUNT, region=REGION, environment="dev", source_revision=SOURCE, image_digest=DIGEST)
    return read_pending_image(scoped, **(options | changes))


def test_matching_immutable_tag_is_read_only_digest_proof(registry):
    before = datetime.now(UTC)
    proof = observe(registry)
    assert proof.account_id == ACCOUNT and proof.repository == REPOSITORY
    assert proof.source_revision == SOURCE and proof.image_digest == DIGEST
    assert proof.observed_at >= before
    assert [name for name, _ in registry[1].calls] == ["describe_repositories", "batch_get_image"]
    assert registry[1].calls[-1][1]["acceptedMediaTypes"] == [MANIFEST_TYPE]


def test_missing_tag_does_not_establish_terminal_publication(registry):
    registry[1].image = False
    assert observe(registry) is None
    assert all(name != "put_image" for name, _ in registry[1].calls)


def test_native_missing_tag_reason_is_not_a_success_proof(registry):
    registry[1].response = {
        "images": [],
        "failures": [{"imageId": {"imageTag": SOURCE}, "failureCode": "ImageNotFound", "failureReason": "Requested image not found"}],
    }
    assert observe(registry) is None
    assert [name for name, _ in registry[1].calls] == ["describe_repositories", "batch_get_image"]


def test_absent_image_proof_requires_exact_immutable_repository_and_missing_tag(registry):
    registry[1].image = False
    before = datetime.now(UTC)
    absence = observe(registry, report_absence=True)
    assert isinstance(absence, ImageAbsence)
    assert (absence.account_id, absence.region, absence.repository, absence.source_revision, absence.image_digest) == (
        ACCOUNT,
        REGION,
        REPOSITORY,
        SOURCE,
        DIGEST,
    )
    assert absence.observed_at >= before
    registry[1].mutable = True
    with pytest.raises(CycleBlockedError, match="operations_publication_repository_changed"):
        observe(registry, report_absence=True)


@pytest.mark.parametrize("attribute", ["mutable", "conflict", "unavailable"])
def test_untrusted_repository_digest_or_provider_response_refused(registry, attribute):
    setattr(registry[1], attribute, True)
    with pytest.raises(CycleBlockedError):
        observe(registry)
    assert all(name in {"describe_repositories", "batch_get_image"} for name, _ in registry[1].calls)


def test_invalid_target_or_digest_refused_before_any_provider_call(registry):
    for kwargs in (
        {"account_id": "invalid"},
        {"image_digest": "unknown"},
        {"source_revision": "latest"},
        {"region": ""},
        {"environment": "stage"},
        {"environment": []},
    ):
        with pytest.raises(CycleBlockedError, match="operations_publication_recovery_inputs_invalid"):
            observe(registry, **kwargs)
    assert registry[1].calls == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"images": [], "failures": []},
        {"images": [], "failures": [{"imageId": {"imageTag": "other"}, "failureCode": "ImageNotFound"}]},
        {"images": [], "failures": [{"imageId": {"imageTag": SOURCE}, "failureCode": "AccessDenied"}]},
        {"images": [], "failures": [], "nextToken": "more"},
        {"images": [], "failures": [{"imageId": {"imageTag": SOURCE}, "failureCode": "ImageNotFound", "unexpected": "unknown"}]},
        {"images": "untrusted", "failures": []},
    ],
)
def test_ambiguous_or_malformed_readback_never_proves_absence(registry, response):
    registry[1].response = response
    with pytest.raises(CycleBlockedError):
        observe(registry)
    assert all(name in {"describe_repositories", "batch_get_image"} for name, _ in registry[1].calls)


@pytest.mark.parametrize("manifest", ["\ud800", "A" * 1_048_577, 7])
def test_invalid_manifest_never_establishes_publication(registry, manifest):
    client = registry[1]
    client.response = client.batch_get_image()
    client.calls.clear()
    client.response["images"][0]["imageManifest"] = manifest
    with pytest.raises(CycleBlockedError, match="operations_publication_image_changed"):
        observe(registry)
    assert [name for name, _ in client.calls] == ["describe_repositories", "batch_get_image"]


def test_scoped_client_failure_does_not_expose_provider_details(registry):
    registry[1].scope_unavailable = True
    with pytest.raises(CycleBlockedError, match="^operations_publication_read_unavailable$") as error:
        observe(registry)
    assert "private provider message" not in str(error.value)
    assert registry[1].calls == []
