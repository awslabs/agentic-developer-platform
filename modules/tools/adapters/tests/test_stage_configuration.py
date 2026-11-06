"""The protected stage binds the published image to reviewed generations."""

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "stage_configuration.py"
spec = importlib.util.spec_from_file_location("stage_configuration", SCRIPT)
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def release():
    return {"schema_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64}


def test_stage_configuration_retains_published_digest_and_reviewed_generations():
    result = stage.configuration(release(), source="a" * 40, group_generation="2", backend_generation="3")
    assert result.mode == "foundation"
    assert result.release.image_digest == release()["image_digest"]
    assert result.release.group == "atlassian-foundation"
    assert (result.release.group_generation, result.release.backend_generation) == (2, 3)


@pytest.mark.parametrize(
    "change",
    ["source", "digest", "schema", "group_generation", "backend_generation"],
)
def test_stage_configuration_refuses_unbound_image_or_generations(change):
    publication = release()
    source, group, backend = "a" * 40, "2", "3"
    if change == "source":
        source = "c" * 40
    elif change == "digest":
        publication["image_digest"] = "latest"
    elif change == "schema":
        publication["schema_version"] = "unknown"
    elif change == "group_generation":
        group = "0"
    else:
        backend = "bad"
    with pytest.raises(ValueError):
        stage.configuration(publication, source=source, group_generation=group, backend_generation=backend)


def test_stage_configuration_main_writes_private_config_once(tmp_path, monkeypatch):
    publication = tmp_path / "publication.json"
    publication.write_text(json.dumps(release()))
    output = tmp_path / "configuration.json"
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setenv("ADP_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_GROUP_GENERATION", "2")
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_BACKEND_GENERATION", "3")
    monkeypatch.setattr(
        sys, "argv", ["stage_configuration.py", "--publication", str(publication), "--output", str(output)]
    )
    stage.main()
    assert json.loads(output.read_text())["release"]["image_digest"] == release()["image_digest"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        stage.main()


@pytest.mark.parametrize(
    "change", [None, "source", "image", "generation", "mode", "profile", "profile-source", "mixed-profile"]
)
def test_existing_configure_command_uses_only_matching_observation_release(tmp_path, monkeypatch, change):
    publication = tmp_path / "publication.json"
    publication.write_text(json.dumps(release()))
    observed = stage.configuration(
        release(), source="a" * 40, group_generation="2", backend_generation="3"
    ).model_dump()
    observed["mode"] = "observation"
    observed["observation"] = {
        "core_authorize_url": "https://core.example/internal/v1/tool-adapters/authorize",
        "core_role_arn": "arn:aws:iam::123456789012:role/core",
        "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012",
        "edge_provenance_sha256": "c" * 64,
        "identity_workload": "adp-dev-atlassian-mcp-oauth-user",
        "identity_provider": "adp-dev-atlassian-mcp-user",
        "config_revision": "d" * 64,
        "subject_schema_sha256": "e" * 64,
        "sites_schema_sha256": "f" * 64,
    }
    if change == "source":
        observed["release"]["source_revision"] = "c" * 40
    elif change == "image":
        observed["release"]["image_digest"] = "sha256:" + "c" * 64
    elif change == "generation":
        observed["release"]["group_generation"] = 99
    elif change == "mode":
        observed["mode"], observed["observation"] = "foundation", None
    elif change in {"profile", "profile-source", "mixed-profile"}:
        observed["source_revision"] = "c" * 40 if change == "profile-source" else "a" * 40
        if change != "mixed-profile":
            observed.pop("release")
    supplied = tmp_path / "observation.json"
    supplied.write_text(json.dumps(observed))
    output = tmp_path / "configuration.json"
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setenv("ADP_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_GROUP_GENERATION", "2")
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_BACKEND_GENERATION", "3")
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_OBSERVATION_FILE", str(supplied))
    monkeypatch.setattr(
        sys, "argv", ["stage_configuration.py", "--publication", str(publication), "--output", str(output)]
    )
    if change not in {None, "profile"}:
        with pytest.raises(ValueError):
            stage.main()
        assert not output.exists()
    else:
        stage.main()
        actual = json.loads(output.read_text())
        assert actual["mode"] == "observation"
        assert actual["observation"]["subject_schema_sha256"] == "e" * 64
        assert actual["release"]["image_digest"] == release()["image_digest"]
        assert actual["release"]["source_revision"] == "a" * 40
