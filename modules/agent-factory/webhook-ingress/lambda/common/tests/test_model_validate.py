"""Tests for common/model_validate.py — Issue #2279.

Tests alias resolution and fnmatch validation against allowed patterns.
"""

import sys
import time
from pathlib import Path

import pytest

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.model_validate import (
    resolve_and_validate,
    resolve_canonical_override,
    resolve_legacy_assignment,
)


class TestAliasResolution:
    """Tests that known aliases resolve to the correct Bedrock model IDs."""

    # Version-pinned aliases (<family><major><minor>) → invocable inference
    # profiles (global. prefix). Bare opus/sonnet/haiku were removed.
    def test_opus48_resolves(self):
        assert resolve_and_validate("opus48") == "global.anthropic.claude-opus-4-8"

    def test_opus46_resolves(self):
        assert resolve_and_validate("opus46") == "global.anthropic.claude-opus-4-6-v1"

    def test_sonnet46_resolves(self):
        assert resolve_and_validate("sonnet46") == "global.anthropic.claude-sonnet-4-6"

    def test_sonnet45_resolves(self):
        assert (
            resolve_and_validate("sonnet45")
            == "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )

    def test_haiku45_resolves(self):
        assert (
            resolve_and_validate("haiku45")
            == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    def test_case_insensitive(self):
        """Aliases are case-insensitive (users may type OPUS48 or opus48)."""
        assert resolve_and_validate("OPUS48") == "global.anthropic.claude-opus-4-8"

    def test_bare_alias_no_longer_resolves(self):
        """Bare 'opus'/'sonnet'/'haiku' are removed — they no longer match an
        alias and (not matching any allowed pattern as a raw ID) are rejected."""
        assert resolve_and_validate("opus") is None
        assert resolve_and_validate("sonnet") is None
        assert resolve_and_validate("haiku") is None


class TestPassThroughModelIds:
    """Tests that full Bedrock model IDs pass through and are validated."""

    def test_published_full_bedrock_id_allowed(self):
        """A published canonical ID passes through."""
        model_id = "global.anthropic.claude-sonnet-4-6"
        result = resolve_and_validate(model_id)
        assert result == model_id

    def test_pattern_shaped_unpublished_id_still_executes_on_the_legacy_path(self):
        """Legacy (executed) resolution keeps its historic pass-through.

        ``resolve_and_validate`` answers "what does this run execute?", and in
        ``report_only`` that answer must not change. Making it strict is what
        regressed a requested regional model into the worker's own default --
        the strict judgement belongs to the *proposed* resolution instead, see
        ``TestCanonicalOverrideResolution``.
        """
        model_id = "us.anthropic.claude-opus-4-20250514-v1:0"
        assert resolve_and_validate(model_id) == model_id

    def test_non_anthropic_model_rejected_by_default(self):
        """A non-Claude model not in patterns is rejected (returns None)."""
        result = resolve_and_validate("meta.llama3-70b-instruct-v1:0")
        # Default patterns only allow anthropic.claude-* and regional variants
        assert result is None


class TestValidationAgainstPatterns:
    """Tests fnmatch validation against persona/tenant allowed_models."""

    def test_persona_allowed_models_restricts(self):
        """Persona's allowed_models restricts which models are valid."""
        # Persona only allows Sonnet
        persona_allowed = ["global.anthropic.claude-sonnet-*"]
        result = resolve_and_validate("opus48", persona_allowed_models=persona_allowed)
        assert result is None  # Opus not in persona's allowed list

    def test_persona_allowed_models_permits(self):
        """Model that matches persona's pattern is allowed.

        Note (#2300): the resolved alias is a global.-prefixed inference
        profile, so the persona pattern must match that prefix.
        """
        persona_allowed = ["global.anthropic.claude-opus-*"]
        result = resolve_and_validate("opus46", persona_allowed_models=persona_allowed)
        assert result == "global.anthropic.claude-opus-4-6-v1"

    def test_tenant_patterns_used_when_persona_empty(self):
        """Tenant patterns are used when persona allowed_models is empty."""
        tenant_patterns = ["global.anthropic.claude-haiku-*"]
        result = resolve_and_validate(
            "haiku45", persona_allowed_models=None, tenant_patterns=tenant_patterns
        )
        assert result == "global.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_tenant_patterns_reject(self):
        """Tenant patterns can reject a model."""
        tenant_patterns = ["global.anthropic.claude-sonnet-*"]
        result = resolve_and_validate(
            "opus48", persona_allowed_models=None, tenant_patterns=tenant_patterns
        )
        assert result is None

    def test_default_patterns_allow_all_claude(self):
        """Default patterns allow all the version-pinned Claude aliases."""
        for alias in ["opus48", "opus46", "sonnet46", "sonnet45", "haiku45"]:
            result = resolve_and_validate(alias)
            assert result is not None, f"Alias '{alias}' should be allowed by defaults"


class TestUnknownAliases:
    """Tests behavior with unknown/invalid aliases."""

    def test_unknown_alias_that_looks_like_model_id(self):
        """An unknown alias that doesn't match any pattern returns None."""
        result = resolve_and_validate("gpt-4o")
        assert result is None

    def test_unknown_alias_gibberish(self):
        """Complete gibberish returns None."""
        result = resolve_and_validate("xyzzy-turbo-9000")
        assert result is None

    def test_empty_string_rejected(self):
        """Empty string alias is rejected."""
        result = resolve_and_validate("")
        assert result is None


def test_edge_validation_adds_no_network_call_and_stays_inside_webhook_budget():
    """One thousand local resolutions leave overwhelming headroom under 10s."""
    started = time.perf_counter()
    for _ in range(1000):
        assert resolve_and_validate("sonnet46") == (
            "global.anthropic.claude-sonnet-4-6"
        )
    assert time.perf_counter() - started < 0.25


class TestCanonicalOverrideResolution:
    """The strict *proposed* resolution: only what the authority published."""

    def test_published_alias_and_id_resolve(self):
        assert (
            resolve_canonical_override("sonnet46")
            == "global.anthropic.claude-sonnet-4-6"
        )
        assert (
            resolve_canonical_override("us.anthropic.claude-sonnet-4-6")
            == "us.anthropic.claude-sonnet-4-6"
        )

    def test_pattern_shaped_but_unpublished_id_is_refused(self):
        """An allowlist pattern is not catalogue membership (design §3.9).

        The edge may refuse invalid input from generated catalogue data, but it
        must never select a model the authority never published.
        """
        unpublished = "us.anthropic.claude-opus-4-20250514-v1:0"
        assert resolve_canonical_override(unpublished) is None
        assert resolve_canonical_override("us.anthropic.claude-opus-4-6-v1") is None
        assert resolve_canonical_override("eu.anthropic.claude-sonnet-4-6") is None

    def test_surrounding_whitespace_does_not_smuggle_a_value(self):
        assert resolve_canonical_override("  sonnet46  ") == (
            "global.anthropic.claude-sonnet-4-6"
        )

    def test_refusal_is_never_a_substitution(self):
        """A refused canonical override yields None, not another model."""
        for value in ["", "gibberish", "gpt-4o", "meta.llama3-70b-instruct-v1:0"]:
            assert resolve_canonical_override(value) is None


class TestLegacyAndProposedStayIndependent:
    """PMM-07's core invariant: the proposal cannot move the executed model."""

    def test_regional_request_executes_unchanged_while_proposal_refuses(self):
        """The reproduced live regression, pinned as a test.

        Before this split, ``us.anthropic.claude-opus-4-6-v1`` resolved to
        nothing at the edge and the worker substituted its own default, so a
        user silently got a different model. Legacy execution must keep the
        requested value; only the proposal may refuse.
        """
        for requested in [
            "us.anthropic.claude-opus-4-6-v1",
            "eu.anthropic.claude-sonnet-4-6",
        ]:
            assert resolve_legacy_assignment(requested) == requested
            assert resolve_canonical_override(requested) is None

    def test_published_controls_agree_on_both_paths(self):
        """Controls that were never affected must stay identical on both."""
        for requested, expected in [
            ("sonnet46", "global.anthropic.claude-sonnet-4-6"),
            ("us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"),
        ]:
            assert resolve_legacy_assignment(requested) == expected
            assert resolve_canonical_override(requested) == expected

    def test_back_compatible_name_is_the_legacy_executed_answer(self):
        """Existing callers asking "what runs?" keep their historic answer."""
        for value in [
            "sonnet46",
            "us.anthropic.claude-opus-4-6-v1",
            "us.anthropic.claude-opus-4-20250514-v1:0",
            "gibberish",
            "",
        ]:
            assert resolve_and_validate(value) == resolve_legacy_assignment(value)


class TestMalformedGeneratedCatalogueIsContained:
    """A bad generated artefact must refuse safely, never crash on import.

    ``frozenset()`` over unhashable elements, and ``.get`` on a non-object root,
    both used to raise while module globals were still initialising -- taking
    the whole Lambda down rather than refusing one directive.
    """

    @staticmethod
    def _load(tmp_path, payload: str | None):
        """Import model_validate.py standalone beside a chosen catalogue file."""
        import importlib.util

        source = Path(__file__).parents[1] / "model_validate.py"
        (tmp_path / "model_validate.py").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        if payload is not None:
            (tmp_path / "persona_model_catalogue.json").write_text(
                payload, encoding="utf-8"
            )
        spec = importlib.util.spec_from_file_location(
            f"mv_probe_{abs(hash(payload))}", tmp_path / "model_validate.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # must not raise
        return module

    MALFORMED = {
        "missing_file": None,
        "array_root": "[]",
        "null_root": "null",
        "string_root": '"nope"',
        "invalid_json": "{not json",
        "empty_file": "",
        "unhashable_element": (
            '{"schema_version":1,"compatibility_class":"claude-agent-sdk",'
            '"aliases":{},"allowed_patterns":[],"canonical_model_ids":[{}]}'
        ),
        "non_string_element": (
            '{"schema_version":1,"compatibility_class":"claude-agent-sdk",'
            '"aliases":{},"allowed_patterns":[],"canonical_model_ids":[5,null]}'
        ),
        "non_string_alias_value": (
            '{"schema_version":1,"compatibility_class":"claude-agent-sdk",'
            '"aliases":{"a":5},"allowed_patterns":[],"canonical_model_ids":[]}'
        ),
        "wrong_schema_version": (
            '{"schema_version":2,"compatibility_class":"claude-agent-sdk",'
            '"aliases":{},"allowed_patterns":[],"canonical_model_ids":[]}'
        ),
        "wrong_compatibility_class": (
            '{"schema_version":1,"compatibility_class":"codex-sdk",'
            '"aliases":{},"allowed_patterns":[],"canonical_model_ids":[]}'
        ),
    }

    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_import_survives_and_the_proposal_refuses(self, tmp_path, name):
        module = self._load(tmp_path, self.MALFORMED[name])
        assert module.CANONICAL_MODEL_IDS == frozenset()
        assert module.resolve_canonical_override("sonnet46") is None

    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_legacy_execution_is_unaffected_by_a_bad_artefact(self, tmp_path, name):
        """The executed model must not depend on a *generated* file at all.

        This is why the legacy alias table and pattern list are inlined: a
        malformed artefact must not be able to change, or break, a live run.
        """
        module = self._load(tmp_path, self.MALFORMED[name])
        assert module.resolve_legacy_assignment("sonnet46") == (
            "global.anthropic.claude-sonnet-4-6"
        )
        assert module.resolve_legacy_assignment("us.anthropic.claude-opus-4-6-v1") == (
            "us.anthropic.claude-opus-4-6-v1"
        )

    def test_a_valid_artefact_still_publishes(self, tmp_path):
        """Containment must not mask a genuinely working catalogue."""
        real = (Path(__file__).parents[1] / "persona_model_catalogue.json").read_text(
            encoding="utf-8"
        )
        module = self._load(tmp_path, real)
        assert module.resolve_canonical_override("sonnet46") == (
            "global.anthropic.claude-sonnet-4-6"
        )
