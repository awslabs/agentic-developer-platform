"""Exercise durable publication, provider schema approval and original rollback."""

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from test_prepare_handoff import cloud as modeled_cloud

cloud = modeled_cloud


@pytest.mark.parametrize("system_enabled", [False, True])
def test_installs_real_target_ids_and_matches_adapter_catalogue(cloud, monkeypatch, system_enabled):
    import asyncio

    import catalogue_binding
    from tool_adapters import atlassian_route
    from tool_adapters.config import AdapterConfiguration

    cloud.runtime_settings["observation"]["system_context_enabled"] = system_enabled
    prepared = cloud.preparer().prepare()
    backend = cloud.backend(prepared, cloud.env)
    release = prepared["plan"]["after"]["release"]
    configuration = AdapterConfiguration.model_validate(cloud.runtime_settings)
    catalogue_binding.install(backend, release, configuration)
    packets = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, content, headers):
            packets.append(json.loads(content))
            return type("Response", (), {"status_code": 200, "content": b"{}"})()

    monkeypatch.setattr(atlassian_route, "_sign_dispatch", lambda *_: {})
    monkeypatch.setattr(atlassian_route.httpx, "AsyncClient", lambda **_: Client())
    asyncio.run(atlassian_route.publish_reads(configuration))
    assert len(packets) == 1
    published = json.dumps(packets[0]["tools"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    for values in cloud.catalogue_maps.values():
        assert values["ADP_TOOLS_ATLASSIAN_READS_SHA256"] == hashlib.sha256(published).hexdigest()
        assert values["ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED"] == str(system_enabled).lower()
        assert values["ADP_TOOLS_ATLASSIAN_WRITES_ENABLED"] == "false"
        assert values["ADP_TOOLS_ATLASSIAN_WRITES_SHA256"] == ""
        for name, route in prepared["plan"]["after"]["routes"].items():
            prefix = "ADP_TOOLS_" + name.upper()
            assert values[prefix + "_TARGET_ID"] == route["target_id"]
            assert values[prefix + "_SOURCE_REVISION"] == release["source_revision"]
            assert values[prefix + "_MEDIATOR_URL"] == (
                f"https://api123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/{name}/mcp"
            )
            assert values[prefix + "_MEDIATOR_ENABLED"] == "true"


def test_partial_install_restores_original_references_without_adapter(cloud):
    import catalogue_binding
    import handoff_release
    from tool_adapters.config import AdapterConfiguration

    original = {
        "name": "ADP_TOOLS_GITHUB_SOURCE_REVISION",
        "valueFrom": {"secretKeyRef": {"name": "previous-connector", "key": "source"}},
    }
    unrelated = {"name": "GITHUB_APP_ID", "value": "unchanged"}
    for document in cloud.catalogue_deployments.values():
        current = document["spec"]["template"]["spec"]["containers"][0]
        current["env"] = [original, unrelated]
        current["envFrom"] = [{"configMapRef": {"name": "original-config"}}]
    cloud.catalogue_maps["gateway"] = {"ADP_TOOLS_GITHUB_MEDIATOR_ENABLED": "false"}
    prepared = cloud.preparer().prepare()
    before = copy.deepcopy(prepared["selectors"]["runtime_before"])
    backend = cloud.backend(prepared, cloud.env)
    native = backend.kubectl

    def unavailable(*args, **kwargs):
        # Simulate losing the acknowledgement after the first workload patch.
        result = native(*args, **kwargs)
        if args[1] == "gateway" and args[2] == "patch" and args[3].startswith("deployment/"):
            raise OSError("lost acknowledgement")
        return result

    backend.kubectl = unavailable
    with pytest.raises(OSError):
        catalogue_binding.install(
            backend, prepared["plan"]["after"]["release"], AdapterConfiguration.model_validate(cloud.runtime_settings)
        )
    backend.kubectl = native
    restored = handoff_release.restored(prepared["plan"])["release"]
    catalogue_binding.install(backend, restored)
    for name, document in cloud.catalogue_deployments.items():
        current = document["spec"]["template"]["spec"]["containers"][0]
        assert current["env"] == [unrelated, original]
        assert current["envFrom"] == before["workloads"][name]["envFrom"]
        expected = {
            **before["maps"][name],
            "ADP_TOOL_ADAPTER_RELEASE": catalogue_binding.canonical(restored),
            "ADP_TOOL_ADAPTER_LEGACY_ENABLED": "false",
        }
        assert cloud.catalogue_maps[name] == expected
    # Preparation replay must never turn its own partial mutation into a baseline.
    assert cloud.preparer().prepare()["selectors"]["runtime_before"] == before


def test_regular_deployments_preserve_handoff_configuration():
    root = Path(__file__).parents[4]
    for path in ("modules/gateway/k8s/deployment.yaml", "modules/tools/facade/k8s/deployment.yaml"):
        document = (root / path).read_text()
        sources = document.split("envFrom:", 1)[1].split("\n          env:", 1)[0]
        assert sources.rstrip().endswith("name: adp-tool-adapter-runtime\n                optional: true")
        blocks = re.findall(r"- name: (ADP_TOOLS_(?:GITHUB|JIRA|SLACK)_[A-Z_]+)\n(.*?)(?=\n\s*- name:)", document, re.S)
        for name, block in blocks:
            if "_POST_" not in name:
                assert "configMapKeyRef:" in block and "name: adp-tool-adapter-runtime" in block
    infrastructure = (root / "platform/infra/tools-handoff-rbac.tf").read_text()
    assert 'resource_names = ["adp-tool-adapter-runtime"]' in infrastructure
    assert "ignore_changes = [data]" in infrastructure


def test_legacy_fallback_survives_handoff_redeploy_and_rollback(cloud):
    import catalogue_binding
    import handoff_release
    from tool_adapters.config import AdapterConfiguration

    current = cloud.catalogue_deployments["gateway"]["spec"]["template"]["spec"]["containers"][0]
    fallback = {"secretRef": {"name": "adp-tools-oauth-mediators", "optional": True}}
    current["envFrom"] = [fallback, {"configMapRef": {"name": catalogue_binding.NAME, "optional": True}}]
    current["env"] = [
        {
            "name": name,
            "valueFrom": {"configMapKeyRef": {"name": catalogue_binding.NAME, "key": name, "optional": True}},
        }
        for name in sorted(catalogue_binding.CATALOGUE_FIELDS)
        if not name.startswith("ADP_TOOLS_ATLASSIAN_")
    ]
    original = copy.deepcopy(current)
    prepared = cloud.preparer().prepare()
    backend = cloud.backend(prepared, cloud.env)
    release = prepared["plan"]["after"]["release"]
    configuration = AdapterConfiguration.model_validate(cloud.runtime_settings)
    assert prepared["selectors"]["runtime_before"]["workloads"]["gateway"]["envFrom"] == original["envFrom"]

    catalogue_binding.install(backend, release, configuration)
    assert current["envFrom"] == original["envFrom"]
    current.update(copy.deepcopy(original))
    catalogue_binding.verify(backend, release, configuration)

    restored = handoff_release.restored(prepared["plan"])["release"]
    catalogue_binding.install(backend, restored)
    assert current == original
    assert cloud.catalogue_maps["gateway"] == {
        "ADP_TOOL_ADAPTER_RELEASE": catalogue_binding.canonical(restored),
        "ADP_TOOL_ADAPTER_LEGACY_ENABLED": "false",
    }
    current.update(copy.deepcopy(original))
    catalogue_binding.verify(backend, restored)


@pytest.mark.parametrize("drift", ["target", "inline_release", "inline_source", "configmap_reference"])
def test_repeated_readback_refuses_configuration_drift(cloud, drift):
    import catalogue_binding
    from tool_adapters.config import AdapterConfiguration

    prepared = cloud.preparer().prepare()
    backend = cloud.backend(prepared, cloud.env)
    release = prepared["plan"]["after"]["release"]
    configuration = AdapterConfiguration.model_validate(cloud.runtime_settings)
    catalogue_binding.install(backend, release, configuration)
    catalogue_binding.verify(backend, release, configuration)
    current = cloud.catalogue_deployments["gateway"]["spec"]["template"]["spec"]["containers"][0]
    if drift == "target":
        cloud.catalogue_maps["gateway"]["ADP_TOOLS_ATLASSIAN_TARGET_ID"] = "unapproved"
    elif drift == "inline_release":
        current["env"].append({"name": "ADP_TOOL_ADAPTER_RELEASE", "value": "{}"})
    elif drift == "inline_source":
        current["env"].append({"name": "ADP_TOOLS_ATLASSIAN_SOURCE_REVISION", "value": "f" * 40})
    else:
        current["envFrom"][-1]["configMapRef"]["name"] = "unapproved"
    with pytest.raises(ValueError, match="adapter_catalogue_binding_refused"):
        catalogue_binding.verify(backend, release, configuration)


def test_handoff_and_rollback_preserve_concurrent_unrelated_configuration(cloud):
    import catalogue_binding
    import handoff_release
    from tool_adapters.config import AdapterConfiguration

    prepared = cloud.preparer().prepare()
    backend = cloud.backend(prepared, cloud.env)
    current = cloud.catalogue_deployments["gateway"]["spec"]["template"]["spec"]["containers"][0]
    other = {"configMapRef": {"name": "other-component", "optional": True}}
    current["envFrom"] = [other]
    catalogue_binding.install(
        backend, prepared["plan"]["after"]["release"], AdapterConfiguration.model_validate(cloud.runtime_settings)
    )
    assert current["envFrom"][0] == other
    restored = handoff_release.restored(prepared["plan"])["release"]
    catalogue_binding.install(backend, restored)
    assert current["envFrom"] == [other]
    catalogue_binding.verify(backend, restored)


def test_old_plan_without_original_configuration_is_refused_before_fencing(cloud):
    from test_handoff_release import Transitions

    import handoff_release

    prepared = cloud.preparer().prepare()
    del prepared["selectors"]["runtime_before"]
    core = Transitions(handoff_release)
    cloud.calls.clear()
    with pytest.raises(ValueError, match="adapter_handoff_refused"):
        handoff_release.execute(prepared, cloud.backend(prepared, cloud.env), core, clock=lambda: 150)
    assert core.calls == ["read"]
    assert cloud.calls == []
