"""Persona parity test — Issue #5420 (PMM-03).

Asserts the staged copy of personas.py matches the authoritative source.
This is the AC-01 structural evidence: a divergence is a red test rather
than a silent drift (design §2.1 option a).

Mirrors the anti-vacuity discipline from test_persona_catalogue_parity.py.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _repo_root() -> Path:
    """Resolve the repo root from this test file's location."""
    # modules/gateway/tests/admin/persona_models/test_persona_parity.py
    # parents: [0]=persona_models [1]=admin [2]=tests [3]=gateway [4]=modules [5]=repo
    return Path(__file__).resolve().parents[5]


class TestPersonaParity:
    """The staged personas copy must match the authoritative source."""

    def test_valid_personas_match(self):
        """VALID_PERSONAS in the staged copy equals the authoritative source."""
        # Import the staged copy (gateway runtime)
        from src.admin.persona_models._personas import LABEL_TO_PERSONA as STAGED_LABELS
        from src.admin.persona_models._personas import MENTION_TO_PERSONA as STAGED_MENTIONS
        from src.admin.persona_models._personas import PERSONA_COMPATIBILITY_CLASS as STAGED_CLASSES
        from src.admin.persona_models._personas import TASK_PERSONA_COMPATIBILITY_CLASS as STAGED_TASK_CLASSES
        from src.admin.persona_models._personas import VALID_PERSONAS as STAGED_VALID

        source_path = _repo_root() / "modules" / "agent-factory" / "webhook-ingress" / "lambda" / "common"

        # Add the source directory to sys.path temporarily
        source_str = str(source_path)
        sys.path.insert(0, source_str)
        try:
            # Import the authoritative module under a distinct name
            spec = importlib.util.spec_from_file_location(
                "authoritative_personas",
                source_path / "personas.py",
            )
            auth_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(auth_module)
        finally:
            sys.path.remove(source_str)

        # Compare
        assert STAGED_VALID == auth_module.VALID_PERSONAS, (
            f"Staged VALID_PERSONAS differs from authoritative source.\n"
            f"Staged only: {STAGED_VALID - auth_module.VALID_PERSONAS}\n"
            f"Source only: {auth_module.VALID_PERSONAS - STAGED_VALID}"
        )

        assert STAGED_LABELS == auth_module.LABEL_TO_PERSONA, "LABEL_TO_PERSONA mismatch"
        assert STAGED_MENTIONS == auth_module.MENTION_TO_PERSONA, "MENTION_TO_PERSONA mismatch"
        assert STAGED_CLASSES == auth_module.PERSONA_COMPATIBILITY_CLASS, "PERSONA_COMPATIBILITY_CLASS mismatch"
        assert STAGED_TASK_CLASSES == auth_module.TASK_PERSONA_COMPATIBILITY_CLASS
        assert not set(STAGED_TASK_CLASSES) & STAGED_VALID

    def test_harness_revisions_match_exact_runtime_pins(self):
        """Generated server metadata follows each owning SDK package pin."""
        from src.admin.persona_models._personas import (
            COMPATIBILITY_CLASS_HARNESS_CONTRACT_REVISION,
        )

        root = _repo_root()
        claude = json.loads((root / "modules" / "agent-factory" / "agent" / "package.json").read_text())
        codex = json.loads((root / "modules" / "agent-factory" / "codex-reviewer" / "package.json").read_text())

        assert COMPATIBILITY_CLASS_HARNESS_CONTRACT_REVISION == {
            "claude-agent-sdk": claude["dependencies"]["@anthropic-ai/claude-agent-sdk"],
            "codex-sdk": codex["dependencies"]["@openai/codex-sdk"],
        }

    def test_sync_rejects_a_new_unclassified_persona(self):
        """Adding a persona never silently assigns the Claude harness."""
        script = _repo_root() / "modules" / "gateway" / "scripts" / "sync_personas.py"
        spec = importlib.util.spec_from_file_location("sync_personas_under_test", script)
        sync_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync_module)
        unclassified = SimpleNamespace(
            LABEL_TO_PERSONA={},
            MENTION_TO_PERSONA={},
            AUTOMATIC_PERSONAS={"future-persona"},
            VALID_PERSONAS={"future-persona"},
            PERSONA_COMPATIBILITY_CLASS={},
        )

        with pytest.raises(ValueError, match="keys must exactly match VALID_PERSONAS"):
            sync_module._generate_output(unclassified)

    def test_sync_rejects_a_persona_category_it_cannot_emit(self):
        """A new persona category fails the generator instead of silently dropping it.

        Regression for the failure this PR inherited: AUTOMATIC_PERSONAS was added to
        personas.py while the generator still derived VALID_PERSONAS from labels and
        mentions only.  The generator printed a warning, exited 0, and wrote a staged
        copy missing a persona -- surfacing later as an unexplained red parity test.
        The drift must be reported where it is introduced.
        """
        script = _repo_root() / "modules" / "gateway" / "scripts" / "sync_personas.py"
        spec = importlib.util.spec_from_file_location("sync_personas_drift_test", script)
        sync_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync_module)

        # A persona reachable only through a category the generator does not emit.
        undeclared_category = SimpleNamespace(
            LABEL_TO_PERSONA={"developer": "developer"},
            MENTION_TO_PERSONA={"@agent-developer": "developer"},
            AUTOMATIC_PERSONAS=set(),
            VALID_PERSONAS={"developer", "persona-from-a-future-category"},
            PERSONA_COMPATIBILITY_CLASS={
                "developer": "claude-agent-sdk",
                "persona-from-a-future-category": "claude-agent-sdk",
            },
        )

        with pytest.raises(ValueError, match="union of LABEL_TO_PERSONA"):
            sync_module._generate_output(undeclared_category)

    def test_gateway_ci_watches_both_harness_package_pins(self):
        """A harness-only version bump must run generated-copy parity checks."""
        workflow = (_repo_root() / ".github" / "workflows" / "gateway-ci.yml").read_text()
        for manifest in (
            "modules/agent-factory/agent/package.json",
            "modules/agent-factory/codex-reviewer/package.json",
        ):
            assert workflow.count(manifest) == 2, f"{manifest} must trigger Gateway CI for both pull_request and push"

    def test_non_vacuous_match(self):
        """Anti-vacuity: at least 10 personas in the staged copy."""
        from src.admin.persona_models._personas import VALID_PERSONAS

        assert len(VALID_PERSONAS) >= 10, (
            f"Staged VALID_PERSONAS has only {len(VALID_PERSONAS)} entries — suspiciously small, check if the copy is correct."
        )

    def test_authoritative_source_exists(self):
        """Guard against vacuous pass if the source file moves."""
        source = _repo_root() / "modules" / "agent-factory" / "webhook-ingress" / "lambda" / "common" / "personas.py"
        assert source.exists(), f"Authoritative personas.py not found at {source}"

    def test_staged_copy_is_generated(self):
        """The staged copy has the generation marker from sync_personas.py."""
        staged = Path(__file__).resolve().parents[3] / "src" / "admin" / "persona_models" / "_personas.py"
        content = staged.read_text()
        assert "GENERATED" in content or "sync_personas.py" in content, (
            "_personas.py should be generated by scripts/sync_personas.py, not hand-edited"
        )

    def test_sync_personas_check_mode_passes(self):
        """sync_personas.py --check exits 0 when the staged copy is current."""
        import subprocess

        scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
        result = subprocess.run(
            ["python3", str(scripts_dir / "sync_personas.py"), "--check"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"sync_personas.py --check failed (staged copy is stale):\n{result.stderr}"
