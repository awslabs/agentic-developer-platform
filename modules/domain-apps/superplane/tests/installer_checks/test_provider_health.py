"""Only API and proxy receive the new health-only service credential."""

import json
from types import SimpleNamespace

from installation.database_preparation import observation_secret_value
from installation.manifests import render

from .test_dedicated_readers import configured


def test_health_projection_has_no_provider_credential_on_api_or_manager(
    environment, release
):
    environment.pop("execution")
    configured(environment)
    docs = render(environment, release, control_plane_only=True)
    pods = {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]
        for d in docs
        if d["kind"] == "Deployment"
    }
    api = {v["name"]: v for v in pods["superplane-api"]["containers"][0]["env"]}
    assert api["SUPERPLANE_PROVIDER_HEALTH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "superplane-skypilot-auth",
        "key": "health-token",
        "optional": False,
    }
    assert (
        api["SUPERPLANE_PROVIDER_HEALTH_URL"]["value"]
        == "http://skypilot-api.skypilot.svc.cluster.local:46580"
    )
    assert json.loads(api["SUPERPLANE_PROVIDER_HEALTH_WORKSPACE_IDS"]["value"]) == [
        environment["workspace_id"]
    ]
    assert "SKYPILOT_SERVICE_TOKEN" not in api
    assert "superplane-skypilot-auth" not in json.dumps(pods["superplane-controller"])
    proxy = next(
        c
        for c in pods["skypilot-api"]["containers"]
        if c["name"] == "authenticated-transport"
    )
    proxy_env = {v["name"]: v for v in proxy["env"]}
    assert (
        proxy_env["SKYPILOT_HEALTH_TOKEN"]["valueFrom"]["secretKeyRef"]["key"]
        == "health-token"
    )
    assert (
        proxy_env["SKYPILOT_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"]["key"]
        == "token"
    )


def test_new_source_generates_distinct_health_key_and_keeps_workspace_grants(
    environment,
):
    configured(environment)
    result = observation_secret_value(
        SimpleNamespace(env=environment, owner="test", control_plane_only=True)
    )
    assert len(result["skypilot-health-token"]) >= 48
    assert result["skypilot-health-token"] != result["skypilot-token"]
    assert all(
        g["workspaces"] == [environment["workspace_id"]]
        for g in json.loads(result["submitters"])
    )
