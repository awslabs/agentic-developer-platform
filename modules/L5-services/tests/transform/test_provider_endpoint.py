"""Private endpoint routing is installed authority and reaches only provider Pods."""

import json
import pytest

from l5_services.adapters.transform.provider_endpoint import TransformProviderEndpoint
from l5_services.adapters.transform.provider_runner import provider_environment
from l5_services.adapters.transform.pod_control import TransformPodControl
from l5_services.adapters.transform.check_control import TransformCheckControl
from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.adapters.transform.verification import DetachedCheckPending
from l5_services.workers.execution import AuthorityRevoked
from test_isolated_executor import prepared, NODE_UID


IMAGE = "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-example-l5-transform-executor@sha256:" + "b" * 64
REFERENCE = {"schema_version": 1, "account_id": "000000000000", "region": "us-east-1",
    "service_name": "com.amazonaws.us-east-1.transform-custom", "vpc_id": "vpc-" + "a" * 17,
    "endpoint_id": "vpce-" + "b" * 17,
    "dns_name": "vpce-" + "b" * 17 + "-example1.transform-custom.us-east-1.vpce.amazonaws.com"}


def endpoint(reference=None):
    return TransformProviderEndpoint.from_installed(json.dumps(reference or REFERENCE),
                                                    region="us-east-1", provider_image=IMAGE)


@pytest.mark.parametrize("change", [
    {"dns_name": "transform-custom.us-east-1.api.aws"},
    {"dns_name": "attacker.example"},
    {"dns_name": REFERENCE["dns_name"] + "/path"},
    {"dns_name": REFERENCE["dns_name"] + ":443"},
    {"dns_name": "user:password@" + REFERENCE["dns_name"]},
    {"dns_name": REFERENCE["dns_name"] + "?next=attacker"},
    {"dns_name": REFERENCE["dns_name"] + "#fragment"},
    {"dns_name": REFERENCE["dns_name"] + ".attacker.example"},
    {"dns_name": REFERENCE["dns_name"].replace("us-east-1", "us-west-2")},
    {"endpoint_id": "vpce-" + "c" * 17}, {"account_id": "111111111111"},
    {"region": "us-west-2"}, {"service_name": "com.amazonaws.us-east-1.sts"},
    {"url": "https://attacker.example"}, {"schema_version": True},
])
def test_endpoint_rejects_uninstalled_or_ambiguous_routing(change):
    with pytest.raises(ValueError, match="L5_TRANSFORM_PROVIDER_ENDPOINT_REF"):
        endpoint({**REFERENCE, **change})


def test_only_selected_endpoint_is_forwarded_to_vendor(monkeypatch, tmp_path):
    for name in ("ATXCT_FES_ENDPOINT", "ATX_CUSTOM_ENDPOINT", "SEG_ENDPOINT", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "https://attacker.example")
    monkeypatch.setenv("L5_TRANSFORM_PROVIDER_ENDPOINT_REF", endpoint().as_json())
    monkeypatch.setenv("L5_TRANSFORM_PROVIDER_IMAGE", IMAGE)
    credentials = {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "fixture-access",
                   "AWS_SECRET_ACCESS_KEY": "fixture-secret", "AWS_SESSION_TOKEN": "fixture-token"}
    result = provider_environment(tmp_path, credentials)
    assert result["ATXCT_FES_ENDPOINT"] == "https://" + REFERENCE["dns_name"]
    assert all(name not in result for name in ("ATX_CUSTOM_ENDPOINT", "SEG_ENDPOINT", "HTTPS_PROXY",
                                             "L5_TRANSFORM_PROVIDER_ENDPOINT_REF"))
    assert result["AWS_REGION"] == "us-east-1"


@pytest.mark.asyncio
async def test_only_provider_pod_receives_installed_endpoint(monkeypatch):
    old, api, artifacts, intent, manifest, authority, contents = prepared()
    control = TransformPodControl(api, namespace=old.namespace, node_name=old.node_name, node_uid=old.node_uid,
        source=old.source, artifacts=artifacts, provider_image=IMAGE, provider_endpoint=endpoint())
    # Even extra caller-controlled intent settings are never used as routing authority.
    intent["provider_endpoint"] = "https://attacker.example"
    await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    provider = next(pod for pod in api.objects.values() if pod["kind"] == "Pod")
    settings = {x["name"]: x["value"] for x in provider["spec"]["containers"][0]["env"]}
    assert settings == {"L5_TRANSFORM_PROVIDER_ENDPOINT_REF": endpoint().as_json(), "L5_TRANSFORM_PROVIDER_IMAGE": IMAGE}
    assert "attacker.example" not in json.dumps(provider)
    provider["spec"]["containers"][0]["env"].append({"name": "ATXCT_FES_ENDPOINT", "value": "https://attacker.example"})
    with pytest.raises(AuthorityRevoked, match=r"isolation changed at spec\.containers\[0\]\.env"):
        await control.read_provider(intent=intent, authority=authority)
    provider["spec"]["containers"][0]["env"].pop()
    monkeypatch.setenv("L5_TRANSFORM_PROVIDER_ENDPOINT_REF", endpoint().as_json())
    checks = TransformCheckControl(api, artifacts, namespace="transform-checks", image=IMAGE,
                                   node_name="isolated-example", node_uid=NODE_UID)
    files, modes = {"src/example.py": contents}, {"src/example.py": "100644"}
    command = {"argv": ["pytest", "-q"], "cwd": ".", "environment": {}, "timeout_seconds": 120, "minimum_test_count": 1}
    policy = {"commands": [command], "image_digest": IMAGE.split("@")[1], "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "pytest-v1"}
    with pytest.raises(DetachedCheckPending):
        await checks.run_check(stage="before", files=files, file_modes=modes,
            source_tree=compare_source_trees(files, files)["base_tree"], command=command,
            image_digest=policy["image_digest"], bundle_sha256=policy["bundle_sha256"],
            oracle_version=policy["oracle_version"], policy=policy, authority=authority)
    check = next(pod for pod in api.objects.values() if pod["kind"] == "Pod" and pod["metadata"]["namespace"] == "transform-checks")
    assert "env" not in check["spec"]["containers"][0] and "envFrom" not in check["spec"]["containers"][0]
    assert REFERENCE["dns_name"] not in json.dumps(check)


def test_raw_endpoint_cannot_be_used_as_protected_reference():
    with pytest.raises(ValueError, match="protected installation"):
        TransformPodControl(None, namespace="transform-provider", node_name="isolated-example", node_uid=NODE_UID,
                            provider_image=IMAGE, provider_endpoint="https://attacker.example")
