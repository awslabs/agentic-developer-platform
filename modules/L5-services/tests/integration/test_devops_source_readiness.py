"""Selected provider readbacks never imply native source readiness on their own."""

import pytest

from l5_services.adapters.devops.integrations import IntegrationAdapter
from l5_services.devops.source_readiness import qualify_source_readiness
from l5_services.devops.release_prerequisites import INDEXING_UNAVAILABLE
from test_devops_integrations import CloudWatch, GitHub, Product, fixture


async def readiness_fixture(kind="github"):
    integrations, sources, bindings = fixture("alpha", "901", "181", "feature-ref",
                                              "arn:aws:logs:region-a:111111111111:log-group:alpha")
    observed = await IntegrationAdapter(Product({"alpha": (integrations, sources, bindings)}),
                                        GitHub(), CloudWatch()).read("tenant-example", "product", "alpha", integrations)
    selection, evidence = (integrations[0], observed[0]) if kind == "github" else (integrations[1], observed[1])
    selected_refs = ([*selection["repository_refs"], *selection["workflow_refs"]]
                     if kind == "github" else selection["resource_refs"])
    native = {"registration": {"status": "active", "id": "native-registration",
                               "connection_ref": selection["connection_ref"]},
              "association": {"status": "active", "id": "native-association",
                              "registration_id": "native-registration", "source_refs": selected_refs},
              "indexing": {"status": "indexed", "revisions": [evidence["repositories"][0]["content_revision"]]}
              if kind == "github" else {},
              "automation": {"automatic_review": "DISABLED", "automatic_testing": "DISABLED"},
              "connection_mode": "app" if kind == "github" else "aws", "event_support": True}
    return selection, evidence, native


@pytest.mark.asyncio
async def test_readiness_distinguishes_registration_association_index_and_access():
    for kind in ("github", "cloudwatch"):
        selection, evidence, native = await readiness_fixture(kind)
        ready = qualify_source_readiness(selection, evidence, native)
        assert ready["status"] == "ready" and ready["reason"] is None
        assert ready["stages"]["registration"] == ready["stages"]["association"] == "ready"
        assert ready["stages"]["content"] == "ready"
        assert ready["stages"]["indexing"] == ("ready" if kind == "github" else "not_required")
        assert ready["checked_revisions"]
        native["association"] = {}
        checking = qualify_source_readiness(selection, evidence, native)
        assert checking["status"] == "verifying"
        assert checking["stages"]["registration"] == "ready"
        assert checking["stages"]["association"] == "verifying"


@pytest.mark.asyncio
async def test_native_indexing_unavailability_is_not_fabricated_or_an_admission_gate():
    selection, evidence, native = await readiness_fixture()
    native["indexing"] = dict(INDEXING_UNAVAILABLE)
    observed = qualify_source_readiness(selection, evidence, native)
    assert observed["status"] == "ready"
    assert observed["stages"]["indexing"] == "unavailable"
    assert observed["indexing"] == INDEXING_UNAVAILABLE
    native["association"]["source_refs"] = []
    assert qualify_source_readiness(selection, evidence, native)["status"] == "verifying"
    native["indexing"]["revisions"] = ["fabricated-indexed-revision"]
    assert qualify_source_readiness(selection, evidence, native)["stages"]["indexing"] == "verifying"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pat", "unknown_index", "uploaded_only", "automatic_review",
                                     "automatic_testing", "unknown_automation", "foreign_association"])
async def test_readiness_rejects_unsupported_or_unverified_modes(failure):
    selection, evidence, native = await readiness_fixture()
    if failure == "pat":
        selection["event_support"] = True
        native["connection_mode"] = "pat"
    elif failure == "unknown_index":
        native["indexing"] = {"status": "unknown"}
    elif failure == "uploaded_only":
        native["indexing"] = {"status": "uploaded", "revisions": ["commit-alpha"]}
    elif failure in ("automatic_review", "automatic_testing"):
        native["automation"][failure] = "ENABLED"
    elif failure == "unknown_automation":
        native["automation"] = {}
    else:
        native["association"]["registration_id"] = "other-registration"
    result = qualify_source_readiness(selection, evidence, native)
    if failure == "pat":
        assert result["events"] == {"status": "unsupported", "reason": "pat_events_unavailable",
                                    "requested_mode_unavailable": True}
        assert result["status"] == "ready"
    elif failure in ("automatic_review", "automatic_testing"):
        assert result["status"] == "blocked" and result["reason"] == "unrequested_native_automation_enabled"
    else:
        assert result["status"] == "verifying"
        stage = ("indexing" if failure in ("unknown_index", "uploaded_only") else
                 "automation" if failure == "unknown_automation" else "association")
        assert result["stages"][stage] == "verifying"
