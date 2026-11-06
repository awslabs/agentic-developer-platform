"""Release selection authenticates the original attempt and retained bytes."""

import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from tests.e2e.product_onboarding.product_completion_manifest import download
from tests.e2e.product_onboarding.test_product_completion_regression import attempt  # noqa: F401


@pytest.mark.parametrize("failure", [None, "run", "attempt", "failed", "context", "source", "digest", "archive", "missing"])
def test_named_manifest_requires_the_original_signed_attempt(attempt, failure):  # noqa: F811
    environment, _, destination, record, _ = attempt
    workflow = ".github/workflows/product-profile-release.yml"
    environment.update(PRODUCT_RELEASE_ID="product-40-1", ACCOUNT_ID="123456789012",
                       ENVIRONMENT="dev", PRODUCT_TARGET_REGION="us-east-1")
    payload = json.dumps(record).encode()
    environment["PRODUCT_RELEASE_MANIFEST_SHA256"] = hashlib.sha256(payload).hexdigest()
    run = dict(id=40, run_attempt=1, status="completed", conclusion="success",
               repository={"id": 123}, head_branch="main", head_sha="b" * 40, path=workflow)
    context = dict(schema_version=1, repository_id=123, run_id=40, run_attempt=1,
                   workflow_path=workflow, workflow_revision="b" * 40, source_revision="e" * 40,
                   account_id="123456789012", region="us-east-1", resource_kind="service",
                   resource_id="test", inputs={"environment": "dev"}, correlation="owned-release")
    if failure == "run":
        run["id"] = 99
    elif failure == "attempt":
        run["run_attempt"] = 2
    elif failure == "failed":
        run["conclusion"] = "failure"
    elif failure == "context":
        context["run_id"] = 99
    elif failure == "source":
        context["source_revision"] = "f" * 40
    elif failure == "digest":
        environment["PRODUCT_RELEASE_MANIFEST_SHA256"] = "0" * 64
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../manifest.json" if failure == "archive" else "manifest.json", payload)
    store = SimpleNamespace(
        read=Mock(return_value=(json.dumps(context).encode(), "context-hash", "context-version")),
        read_archive=Mock(return_value=None if failure == "missing" else (buffer.getvalue(), "archive-hash", "archive-version")),
    )
    github = Mock(return_value=run)
    if failure:
        with pytest.raises(ValueError):
            download(environment, destination, store=store, github=github)
        assert not destination.exists()
    else:
        assert download(environment, destination, store=store, github=github) == record
        assert destination.stat().st_mode & 0o777 == 0o600
        github.assert_called_once_with("repos/aws-e/adp/actions/runs/40/attempts/1")
        assert store.read_archive.call_args.kwargs == dict(
            source_revision="e" * 40, kind="workflow-artifact", name="product-profile-manifest",
            account="123456789012", environment="dev", region="us-east-1", repository="aws-e/adp",
            repository_id=123, run_id=40, attempt=1, workflow_path=workflow, workflow_revision="b" * 40,
        )
