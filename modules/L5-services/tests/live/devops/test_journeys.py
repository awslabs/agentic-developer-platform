"""Offline adversarial checks; these synthetic fixtures are not live evidence."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from qualification import MATRIX, verify_release, load_manifest


NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.fixture
def fixture():
    release = {"source_sha": "a" * 40, "deployment_id": "deployment-example",
               "deployed_at": "2026-10-27T00:00:00Z",
               "artifacts": {name: "b" * 64 for name in ("api", "ui", "cli", "mcp", "worker")}}
    manifest = {"authorized": True, "fixture_owner": "fixture-owner-example", "target_ref": "target-example",
                "requirements_revision": "R3-secure-native-ui-ingress", "release": release}
    observed = {**release, "observed_at": "2026-10-28T00:00:00Z"}
    ingress = {"browser_launch_route": "gateway", "function_urls": [], "public_launchers": [],
               "source_sha": release["source_sha"], "deployment_id": release["deployment_id"]}
    capabilities = [{"capability": item["id"], "status": "completed", "native_id": f"native-{item['id']}",
                     "readback_ref": f"readback-{item['id']}", "deployment_id": release["deployment_id"]}
                    for item in json.loads(MATRIX.read_text())["capabilities"]]
    journeys = [{"entry_point": entry, "status": "completed", "readback_ref": f"readback-{entry}",
                 "deployment_id": release["deployment_id"]} for entry in ("product", "ui", "cli", "mcp")]
    return manifest, SimpleNamespace(read_release=lambda target: observed, read_ingress=lambda target: ingress,
                                     read_capabilities=lambda target: capabilities, read_journeys=lambda target: journeys)


def test_release_uses_separate_readers_and_requires_all_native_paths(fixture):
    manifest, reader = fixture
    calls = []
    for name in ("read_release", "read_ingress", "read_capabilities", "read_journeys"):
        original = getattr(reader, name)
        def record(target, original=original, name=name):
            calls.append((name, target))
            return original(target)
        setattr(reader, name, record)
    result = verify_release(manifest, reader, now=NOW)
    assert [call[0] for call in calls] == ["read_release", "read_ingress", "read_capabilities", "read_journeys"]
    assert all(target == "target-example" for _, target in calls)
    assert result["requirements_revision"] == "R3-secure-native-ui-ingress"
    assert "sso-gateway-only-ingress" in result["live_checks"]
    assert len(result["capabilities"]) == len(json.loads(MATRIX.read_text())["capabilities"])


@pytest.mark.parametrize("corrupt", [
    lambda manifest, reader: manifest["release"]["artifacts"].update(worker="0" * 64),
    lambda manifest, reader: manifest["release"].update(source_sha="c" * 40),
    lambda manifest, reader: reader.read_release("target-example")["artifacts"].update(cli="0" * 64),
    lambda manifest, reader: reader.read_release("target-example").update(observed_at="2026-10-26T00:00:00Z"),
    lambda manifest, reader: reader.read_ingress("target-example")["function_urls"].append("public-endpoint"),
    lambda manifest, reader: reader.read_capabilities("target-example").pop(),
    lambda manifest, reader: reader.read_capabilities("target-example")[0].update(native_id=""),
    lambda manifest, reader: reader.read_journeys("target-example")[0].update(status="blocked"),
    lambda manifest, reader: manifest.update(requirements_revision="R2"),
])
def test_release_rejects_wrong_revision_partial_native_and_public_ingress(fixture, corrupt):
    manifest, reader = deepcopy(fixture)
    corrupt(manifest, reader)
    with pytest.raises(ValueError):
        verify_release(manifest, reader, now=NOW)


def test_manifest_requires_private_regular_file_outside_checkout(fixture, tmp_path):
    manifest, _ = fixture
    target = tmp_path / "fixture.json"
    target.write_text(json.dumps(manifest))
    target.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_manifest(target)
    target.chmod(0o600)
    assert load_manifest(target) == manifest
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        load_manifest(link)
