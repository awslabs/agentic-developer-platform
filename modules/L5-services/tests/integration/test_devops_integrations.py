"""Offline readback against independently selected Product and provider identities."""

from copy import deepcopy

import pytest

from l5_services.adapters.devops.integrations import IntegrationAccessDenied, IntegrationAdapter
from l5_services.devops_configuration import integration_readiness


def ref(project, connection, kind, identity, revision):
    return {"tenant_id": "tenant-example", "project_id": project, "source_connection_id": connection,
            "record_type": kind, "record_id": identity, "record_revision": revision}


def fixture(project, repository_id, workflow_id, workflow_revision, telemetry_id):
    github_id, aws_id = "github-" + project, "aws-" + project
    repository = ref(project, github_id, "repository", repository_id, "commit-" + project)
    workflow = ref(project, github_id, "pipeline", workflow_id, workflow_revision)
    telemetry = ref(project, aws_id, "telemetry", telemetry_id, "log-" + project)
    integrations = [
        {"kind": "github", "connection_ref": ref(project, github_id, "connection", github_id, "grant"),
         "repository_refs": [repository], "workflow_refs": [workflow]},
        {"kind": "cloudwatch", "connection_ref": ref(project, aws_id, "connection", aws_id, "grant"),
         "resource_refs": [telemetry]},
    ]
    sources = {"coverage": "complete", "next_cursor": None, "items": [
        {"provider": "github", "connection_ref": {"id": github_id},
         "resources": [repository, workflow]},
        {"provider": "aws", "connection_ref": {"id": aws_id},
         "resources": [ref(project, aws_id, "environment", "selected-region", "aws-version")]},
    ]}
    bindings = {"next_cursor": None, "items": [
        {"project_id": project, "status": "healthy", "source_revision": repository["record_revision"],
         "selection": {"kind": "repository", "native_id": repository_id, "connection_ref": {"id": github_id}}},
        {"project_id": project, "status": "healthy", "source_revision": workflow_revision,
         "selection": {"kind": "pipeline", "native_id": workflow_id, "repository_id": repository_id,
                       "connection_ref": {"id": github_id}}},
    ]}
    return integrations, sources, bindings


class Product:
    def __init__(self, fixtures):
        self.fixtures = fixtures

    async def list_sources(self, product_id, project_id):
        return deepcopy(self.fixtures[project_id][1])

    async def list_bindings(self, product_id, project_id):
        return deepcopy(self.fixtures[project_id][2])


class GitHub:
    async def read_repository(self, connection, repository_id, revision):
        return {"repository_id": repository_id, "revision": revision, "content_readable": True,
                "repository": {"id": int(repository_id), "name": "repository-" + repository_id,
                               "full_name": "example-owner/repository-" + repository_id,
                               "owner": {"login": "example-owner", "type": "Organization"},
                               "html_url": "https://github.com/example-owner/repository-" + repository_id}}

    async def read_workflow(self, connection, repository_id, workflow_id, revision):
        return {"repository_id": repository_id, "workflow_id": workflow_id, "source_revision": revision,
                "deployment_revision": "deployed-" + revision, "deployment_readable": True}


class CloudWatch:
    async def read_resource(self, connection, resource_id, revision):
        return {"resource_id": resource_id, "revision": revision, "events_readable": True}


@pytest.mark.asyncio
async def test_integration_readback():
    fixtures = {
        "alpha": fixture("alpha", "901", "181", "feature-ref",
                         "arn:aws:logs:region-a:111111111111:log-group:alpha"),
        "beta": fixture("beta", "902", "282", "release-ref",
                        "arn:aws:logs:region-b:222222222222:log-group:beta"),
    }
    adapter = IntegrationAdapter(Product(fixtures), GitHub(), CloudWatch())
    for project, (integrations, _, _) in fixtures.items():
        result = await adapter.read("tenant-example", "product", project, integrations)
        assert result[0]["repositories"][0]["source_ref"] == integrations[0]["repository_refs"][0]
        assert result[0]["workflows"][0]["deployment_revision"] == "deployed-" + (
            integrations[0]["workflow_refs"][0]["record_revision"])
        assert result[1]["resources"][0]["source_ref"] == integrations[1]["resource_refs"][0]
        assert result[0]["version"] == result[1]["version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign_project", "ungranted", "repository", "workflow",
                                     "telemetry", "binding"])
async def test_integration_rejects_out_of_scope_readback(failure):
    fixtures = {"alpha": fixture("alpha", "901", "181", "feature-ref",
                                  "arn:aws:logs:region-a:111111111111:log-group:alpha")}
    integrations, sources, bindings = fixtures["alpha"]
    github, cloudwatch = GitHub(), CloudWatch()
    if failure == "foreign_project":
        integrations[0]["repository_refs"][0]["project_id"] = "foreign"
    elif failure == "ungranted":
        sources["items"][0]["resources"] = [integrations[0]["workflow_refs"][0]]
    elif failure == "repository":
        async def wrong_repository(*args):
            return {"repository_id": "other", "revision": "commit-alpha", "content_readable": True}
        github.read_repository = wrong_repository
    elif failure == "workflow":
        async def wrong_workflow(*args):
            return {"repository_id": "other", "workflow_id": "181", "source_revision": "feature-ref",
                    "deployment_revision": "deployed", "deployment_readable": True}
        github.read_workflow = wrong_workflow
    elif failure == "telemetry":
        async def wrong_telemetry(*args):
            return {"resource_id": "other", "revision": "log-alpha", "events_readable": True}
        cloudwatch.read_resource = wrong_telemetry
    else:
        bindings["items"][0]["status"] = "degraded"
    adapter = IntegrationAdapter(Product(fixtures), github, cloudwatch)
    with pytest.raises(IntegrationAccessDenied):
        await adapter.read("tenant-example", "product", "alpha", integrations)


@pytest.mark.asyncio
async def test_integration_limits():
    fixtures = {
        "alpha": fixture("alpha", "901", "181", "feature-ref",
                         "arn:aws:logs:region-a:111111111111:log-group:alpha"),
        "beta": fixture("beta", "902", "282", "release-ref",
                        "arn:aws:logs:region-b:222222222222:log-group:beta"),
    }
    adapter = IntegrationAdapter(Product(fixtures), GitHub(), CloudWatch())
    for project, (integrations, _, _) in fixtures.items():
        selection = integrations[0]
        settings = {"devops": {"integrations": [selection]}}
        assert integration_readiness(settings) == [
            {"version": 1, "kind": "github", "status": "verifying", "reason": "integration_readback_missing",
             "events": {"status": "verifying", "reason": "event_support_unverified"}}]
        evidence = (await adapter.read("tenant-example", "product", project, [selection]))[0]
        native = {"registration": {"id": "native-registration", "status": "active",
                                   "connection_ref": selection["connection_ref"]},
                  "association": {"id": "native-association", "status": "active",
                                  "registration_id": "native-registration",
                                  "source_refs": [*selection["repository_refs"], *selection["workflow_refs"]]},
                  "indexing": {"status": "indexed",
                               "revisions": [evidence["repositories"][0]["content_revision"]]},
                  "automation": {"automatic_review": "DISABLED", "automatic_testing": "DISABLED"},
                  "connection_mode": "app", "event_support": True}
        observation = {"selection": selection, "configuration_revision": 3,
                       "evidence": evidence, "native": native}
        assert integration_readiness(settings, [observation], configuration_revision=3)[0]["status"] == "ready"
        assert integration_readiness(settings, [observation], configuration_revision=4)[0]["status"] == "verifying"
        native["indexing"] = {"status": "uploaded"}
        assert integration_readiness(settings, [observation], configuration_revision=3)[0]["status"] == "verifying"
        native["indexing"] = {"status": "indexed", "revisions": [evidence["repositories"][0]["content_revision"]]}
        native["automation"]["automatic_testing"] = "ENABLED"
        assert integration_readiness(settings, [observation], configuration_revision=3)[0]["status"] == "blocked"
        native["automation"]["automatic_testing"] = "DISABLED"
        native["connection_mode"] = "pat"
        selection["event_support"] = True
        assert integration_readiness(settings, [observation], configuration_revision=3)[0]["events"][
            "requested_mode_unavailable"] is True
    assert integration_readiness({"devops": {"integrations": [{"kind": "gitlab"}]}})[0]["reason"] == "unsupported_connector"

class NativeAssociations:
    def __init__(self, connection_id):
        self.connection_id = connection_id
        self.associations = []
        self.creates = 0
        self.lose_response = False
        self.fail_before_create = False

    def list_services(self, **kwargs):
        return {"services": [{"serviceType": "github", "serviceId": "registered-github",
                              "additionalServiceDetails": {"github": {"owner": "example-owner", "ownerType": "organization"}}}]}

    def get_service(self, **kwargs):
        return {"service": self.list_services()["services"][0]}

    def list_associations(self, **kwargs):
        return {"associations": deepcopy(self.associations)}

    def associate_service(self, **kwargs):
        self.creates += 1
        if self.fail_before_create:
            raise TimeoutError("outcome unknown")
        association = {"associationId": "association-example", "agentSpaceId": kwargs["agentSpaceId"],
                       "serviceId": kwargs["serviceId"], "configuration": kwargs["configuration"],
                       "status": "valid", "capabilities": kwargs["capabilities"]}
        self.associations.append(association)
        if self.lose_response:
            raise TimeoutError("response lost")
        return {"association": association}

    def get_association(self, **kwargs):
        return {"association": deepcopy(self.associations[0])}


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_after_create", [True, False])
async def test_integration_recovery(tmp_path, lost_after_create):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.adapters.devops.spaces import SpaceSelection
    from l5_services.devops.lifecycle import SpaceLifecycle
    from l5_services.models import DevopsSpaceIntent
    from l5_services.models.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'integration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixtures = {"alpha": fixture("alpha", "901", "181", "feature-ref",
                                  "arn:aws:logs:region-a:111111111111:log-group:alpha")}
    integrations = fixtures["alpha"][0][:1]
    connection_id = integrations[0]["connection_ref"]["record_id"]
    native = NativeAssociations(connection_id)
    adapter = IntegrationAdapter(Product(fixtures), GitHub(), CloudWatch())
    selection = SpaceSelection("tenant-example", "product", "alpha", "binding-example",
                               "123456789012", "example-region-1")
    lifecycle = SpaceLifecycle(sessions, None)
    try:
        await lifecycle.prepare_create(selection, "operation-example", 3)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            receipt.state = "succeeded"
            receipt.native_space_id = "space-example"
            await session.commit()
        native.lose_response = lost_after_create
        native.fail_before_create = not lost_after_create
        with pytest.raises(IntegrationAccessDenied, match="association_outcome_unknown"):
            await lifecycle.verify_integration_associations(
                selection, "operation-example", integrations, adapter, native)
        assert native.creates == 1
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.integration_observation["reason"] == "association_outcome_unknown"
        native.lose_response = native.fail_before_create = False
        restarted = SpaceLifecycle(sessions, None)
        if lost_after_create:
            observed = await restarted.verify_integration_associations(
                selection, "operation-example", integrations, adapter, native)
            assert observed["status"] == "ready"
            assert observed["associations"][0]["association_id"] == "association-example"
            assert (await restarted.require_current_integration_access(
                selection, "operation-example", integrations, adapter, native))[0]["repositories"][0][
                    "content_readable"] is True
            assert (await restarted.verify_integration_associations(
                selection, "operation-example", integrations, adapter, native))["associations"] == observed[
                    "associations"]
            fixtures["alpha"][1]["items"][0]["resources"] = []
            with pytest.raises(IntegrationAccessDenied, match="repository_not_authorized"):
                await restarted.require_current_integration_access(
                    selection, "operation-example", integrations, adapter, native)
            fixtures["alpha"][1]["items"][0]["resources"] = [
                *integrations[0]["repository_refs"], *integrations[0]["workflow_refs"]]
            native.associations[0]["status"] = "revoked"
            with pytest.raises(IntegrationAccessDenied, match="native_association_readback_mismatch"):
                await restarted.require_current_integration_access(
                    selection, "operation-example", integrations, adapter, native)
            native.associations[0]["status"] = "valid"
            with pytest.raises(ValueError, match="integration selection changed"):
                await restarted.verify_integration_associations(
                    selection, "operation-example", [{**integrations[0], "workflow_refs": []}], adapter, native)
        else:
            with pytest.raises(IntegrationAccessDenied, match="association_outcome_unknown"):
                await restarted.verify_integration_associations(
                    selection, "operation-example", integrations, adapter, native)
            with pytest.raises(IntegrationAccessDenied, match="integration_access_unverified"):
                await restarted.require_current_integration_access(
                    selection, "operation-example", integrations, adapter, native)
        assert native.creates == 1
    finally:
        await engine.dispose()
