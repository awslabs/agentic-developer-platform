"""The legacy gateway applies the same policy to YAML, Markdown and defaults."""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("communication_gateway_loader", BASE / "gateway/app/personas/loader.py")
loader = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = loader
spec.loader.exec_module(loader)


@pytest.mark.parametrize("source", ["yaml", "markdown", "default"])
def test_policy_is_included_once_even_when_cached(tmp_path, monkeypatch, source):
    monkeypatch.setattr(loader, "PERSONAS_YAML_DIR", str(tmp_path))
    monkeypatch.setattr(loader, "PERSONAS_MD_DIR", str(tmp_path))
    loader._cache.clear()
    if source == "yaml":
        (tmp_path / "developer.yaml").write_text('name: developer\nsystem_prompt: "Custom developer"\n')
    elif source == "markdown":
        (tmp_path / "developer.md").write_text("# Custom developer\n")
    policy = (BASE / "rules/personas/shared/human-communication.md").read_text()
    first = loader.load_persona("developer")
    second = loader.load_persona("developer")
    assert first is second
    assert second.source == source
    assert second.system_prompt.count(policy) == 1
    assert second.system_prompt.endswith(policy)
