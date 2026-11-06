"""Product release acceptance requires real case selection and runtime readback."""

import pytest

from tests.e2e.cli_uplift import cases, preflight

SOURCE = "a" * 40
DIGEST = "sha256:" + "c" * 64
MANIFEST = "d" * 64


@pytest.fixture
def release():
    receipt = {
        "source_revision": SOURCE,
        "workflow_revision": "b" * 40,
        "run_attempt": 2,
        "image_digest": DIGEST,
        "migration_head": "112_product_profile_foundation",
        "cli_manifest_sha256": MANIFEST,
        "product_profile_enabled": True,
        "product_onboarding_enabled": False,
        "product_profile_schema_version": "1.0",
    }
    runtime = {
        "source_revision": SOURCE,
        "image_digest": DIGEST,
        "migration_head": receipt["migration_head"],
        "api": {
            "authenticated": True,
            "product_profile_schema_version": "1.0",
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
        },
    }
    record = {"served_cli_hashes": {"command-manifest.json": MANIFEST}}
    return [{"expected_revision": SOURCE}, sorted(preflight.PRODUCT_RELEASE_CASES), receipt, runtime, record]


def test_valid_selected_release_produces_bounded_check(release):
    preflight.check_product_release(*release)
    assert release[4]["product_release_check"]["migration_head"] == release[2]["migration_head"]
    matrix = cases.new_matrix(("product-profile",))
    assert matrix["PR01"]["status"] == cases.NOT_RUN
    assert cases.accept(matrix, ("product-profile",))[0] != cases.PASSED


@pytest.mark.parametrize("selection", [None, [], ["deploy_core_receipt"], ["unknown"],
                                       sorted(preflight.PRODUCT_RELEASE_CASES) + ["unknown"]])
def test_empty_unknown_or_incomplete_selection_never_passes(release, selection):
    release[1] = selection
    with pytest.raises(preflight.PreflightError, match="case selection"):
        preflight.check_product_release(*release)
    assert "product_release_check" not in release[4]


@pytest.mark.parametrize("document,path,value", [
    (2, "source_revision", "f" * 40),
    (3, "source_revision", "f" * 40),
    (2, "migration_head", "111_oauth_reconnect_resources"),
    (3, "migration_head", "111_oauth_reconnect_resources"),
    (2, "product_profile_enabled", False),
    (2, "product_onboarding_enabled", True),
])
def test_stale_source_schema_or_capability_fails(release, document, path, value):
    release[document][path] = value
    with pytest.raises(preflight.PreflightError):
        preflight.check_product_release(*release)
    assert "product_release_check" not in release[4]


@pytest.mark.parametrize("document,path,value", [
    (2, "image_digest", "sha256:" + "e" * 64),
    (3, "image_digest", "sha256:" + "e" * 64),
    (4, "served_cli_hashes", {}),
    (3, "api", {"status": "healthy"}),
    (3, "api", {"product_profile_schema_version": "1.0"}),
])
def test_served_artifact_or_health_only_cannot_pass(release, document, path, value):
    release[document][path] = value
    with pytest.raises(preflight.PreflightError):
        preflight.check_product_release(*release)
    assert "product_release_check" not in release[4]
