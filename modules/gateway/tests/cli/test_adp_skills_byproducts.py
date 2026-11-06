"""Running a skill must not block its next update (found live 2026-11-03).

The adp-flow scripts create ``scripts/__pycache__`` when executed from the client
copy. The sync fingerprinted those files, saw a mismatch with the recorded
provenance, and refused to refresh the directory as "modified".
"""

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "cli" / "adp_skills.py"


@pytest.fixture
def skills():
    spec = importlib.util.spec_from_file_location("adp_skills", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(tmp_path, skill_text):
    source = tmp_path / "bundle"
    (source / "adp-flow" / "scripts").mkdir(parents=True)
    (source / "MANIFEST.json").write_text(json.dumps({"version": "1.0.0", "skills": [{"name": "adp-flow", "clients": ["claude-code"]}]}))
    (source / "adp-flow" / "SKILL.md").write_text(skill_text)
    (source / "adp-flow" / "scripts" / "plan_writer.py").write_text("print('x')\n")
    return source


def test_bytecode_cache_does_not_count_as_a_user_edit_and_is_never_copied(skills, tmp_path):
    home = tmp_path / "home"
    source = _source(tmp_path, "# v1\n")
    skills.sync("claude-code", source, home)
    target = home / ".claude" / "skills" / "adp-flow"
    before = json.loads((target / ".adp-provenance.json").read_text())["sha256"]

    # Running the skill from the client copy leaves bytecode behind.
    cache = target / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "plan_writer.cpython-312.pyc").write_bytes(b"\x00bytecode")
    assert skills.unchanged(target, before), "a bytecode cache must not read as a modification"

    # A new bundle revision must still replace the client copy.
    (source / "adp-flow" / "SKILL.md").write_text("# v2\n")
    skills.sync("claude-code", source, home)
    assert (target / "SKILL.md").read_text() == "# v2\n"
    assert not (target / "scripts" / "__pycache__").exists(), "byproducts are not carried into the refreshed copy"

    # A bundle that itself carries a cache does not ship it.
    (source / "adp-flow" / "scripts" / "__pycache__").mkdir()
    (source / "adp-flow" / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (source / "adp-flow" / "SKILL.md").write_text("# v3\n")
    skills.sync("claude-code", source, home)
    assert (target / "SKILL.md").read_text() == "# v3\n"
    assert not list(target.rglob("*.pyc"))


def test_a_real_user_edit_still_refuses_the_update(skills, tmp_path, capsys):
    home = tmp_path / "home"
    source = _source(tmp_path, "# v1\n")
    skills.sync("claude-code", source, home)
    target = home / ".claude" / "skills" / "adp-flow"
    (target / "SKILL.md").write_text("# mine\n")
    (source / "adp-flow" / "SKILL.md").write_text("# v2\n")
    skills.sync("claude-code", source, home)
    assert (target / "SKILL.md").read_text() == "# mine\n"
    assert "Refusing unmarked or modified skill directory" in capsys.readouterr().err
