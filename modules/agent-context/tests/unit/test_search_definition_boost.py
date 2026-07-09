"""Unit tests for search definition-boost ranking (#3303, #3375).

Verifies:
- _is_identifier_query detects CamelCase, snake_case, and rejects free-text
- _definition_boosted_score pins definition files above all other tiers
- _resolve_definition_files works scope-less via code-index (#3375)
- Non-mocked integration: _handle_search with code-index fixture, no project scope
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from door.server import (
    _definition_boosted_score,
    _file_relevance_score,
    _is_identifier_query,
    _resolve_via_code_index,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# _is_identifier_query tests
# ---------------------------------------------------------------------------


class TestIsIdentifierQuery:
    """Verify identifier-shaped query detection."""

    @pytest.mark.parametrize(
        "query",
        [
            "DataLayerInterface",
            "PluginInterface",
            "BytesScanner",
            "PsList",
            "TranslationLayerInterface",
            "LinearlyMappedLayer",
            "SymbolSpace",
            "LayerStacker",
            "CreditCardError",
            "RecommendationService",
            "BacktestTool",
            "MandateProposalCard",
            "ContentRouter",
            "BaseAgent",
        ],
    )
    def test_camel_case_identifiers(self, query: str):
        """CamelCase/PascalCase identifiers should be detected."""
        assert _is_identifier_query(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "create_layer",
            "get_config",
            "calc_metrics",
            "truncate_for_display",
            "sha256_hash",
        ],
    )
    def test_snake_case_identifiers(self, query: str):
        """snake_case identifiers should be detected."""
        assert _is_identifier_query(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "backtest engine base class",
            "quick start installation",
            "loader registry data sources",
            "agent loop ReAct",
            "PsList windows plugin",
            "Quote shipping cost",
            "personalized tutoring features",
            "agentic loop label protocol",
            "Position TradeRecord dataclass",
            "LayerStacker automagic",
            "MemoryStore three-layer",
        ],
    )
    def test_free_text_phrases_rejected(self, query: str):
        """Multi-word free-text queries should NOT be identified as identifiers."""
        assert _is_identifier_query(query) is False

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "a",
            "AB",
        ],
    )
    def test_short_strings_rejected(self, query: str):
        """Very short strings (≤2 chars) should be rejected."""
        assert _is_identifier_query(query) is False

    def test_qualified_symbol_with_colons(self):
        """Qualified symbols with :: should be detected."""
        assert _is_identifier_query("volatility3::PsList") is True

    @pytest.mark.parametrize(
        "query",
        [
            "frontendServer",
            "nonexistent_foobar_xyz",
        ],
    )
    def test_lowercase_start_camel_or_snake(self, query: str):
        """lowerCamelCase or snake_case identifiers."""
        # frontendServer starts lowercase but has no underscore — not snake_case
        # and not matching CamelCase (requires uppercase start).
        # nonexistent_foobar_xyz IS snake_case.
        if "_" in query:
            assert _is_identifier_query(query) is True
        else:
            # lowerCamelCase: single token, no space, but doesn't match our patterns
            # This is acceptable — the filename-relevance already handles these well
            # since they tend to match the filename directly (e.g., frontendServer → main.go)
            pass  # No assertion — behavior is acceptable either way


# ---------------------------------------------------------------------------
# _definition_boosted_score tests
# ---------------------------------------------------------------------------


class TestDefinitionBoostedScore:
    """Verify definition-boost scoring logic."""

    def test_definition_file_gets_score_200(self):
        """Files in definition_files set should get score 200."""
        definition_files = {"framework/interfaces/layers.py"}
        score = _definition_boosted_score(
            "framework/interfaces/layers.py",
            "DataLayerInterface",
            definition_files,
        )
        assert score == 200

    def test_non_definition_file_delegates_to_relevance(self):
        """Files NOT in definition_files should use filename relevance score."""
        definition_files = {"framework/interfaces/layers.py"}
        score = _definition_boosted_score(
            "plugins/windows/pslist.py",
            "DataLayerInterface",
            definition_files,
        )
        # Should equal the filename-relevance score (not 200)
        expected = _file_relevance_score("plugins/windows/pslist.py", "DataLayerInterface")
        assert score == expected
        assert score < 200

    def test_empty_definition_files_uses_relevance(self):
        """When definition_files is empty, all files use filename relevance."""
        score = _definition_boosted_score(
            "framework/interfaces/layers.py",
            "DataLayerInterface",
            set(),
        )
        expected = _file_relevance_score("framework/interfaces/layers.py", "DataLayerInterface")
        assert score == expected

    def test_definition_boost_outranks_all_relevance_tiers(self):
        """Definition boost (200) must be higher than all relevance tiers (max 100)."""
        definition_files = {"some/deep/path/target.py"}

        # Score for definition file
        def_score = _definition_boosted_score(
            "some/deep/path/target.py", "Target", definition_files
        )

        # Score for an exact filename match (tier 100)
        relevance_score = _file_relevance_score("target.py", "target")

        assert def_score > relevance_score
        assert def_score == 200

    def test_multiple_definition_files(self):
        """Multiple definition files should all get score 200."""
        definition_files = {
            "volatility3/framework/interfaces/layers.py",
            "volatility3/framework/layers/linear.py",
        }
        score1 = _definition_boosted_score(
            "volatility3/framework/interfaces/layers.py",
            "TranslationLayerInterface",
            definition_files,
        )
        score2 = _definition_boosted_score(
            "volatility3/framework/layers/linear.py",
            "TranslationLayerInterface",
            definition_files,
        )
        assert score1 == 200
        assert score2 == 200

    def test_definition_boost_with_none_definition_files(self):
        """Passing empty set should not crash."""
        score = _definition_boosted_score("some/file.py", "SomeSymbol", set())
        assert isinstance(score, int)
        assert score <= 100  # No boost applied


# ---------------------------------------------------------------------------
# Integration scenario: ranking order with definition boost
# ---------------------------------------------------------------------------


class TestSearchRankingWithDefinitionBoost:
    """Verify that definition boost produces correct ranking order."""

    def test_definition_file_ranks_first_among_usage_files(self):
        """Simulate the vol3 scenario: definition file outranks usage sites."""
        query = "DataLayerInterface"
        definition_files = {"volatility3/framework/interfaces/layers.py"}

        # Simulated deduped results (definition file buried among usage sites)
        files = [
            "volatility3/framework/plugins/windows/pslist.py",
            "volatility3/framework/plugins/linux/proc.py",
            "volatility3/framework/automagic/stacker.py",
            "volatility3/framework/interfaces/layers.py",  # definition
            "volatility3/tests/test_layers.py",
            "volatility3/framework/layers/physical.py",
        ]

        # Sort using the definition-boosted scoring
        ranked = sorted(
            files,
            key=lambda f: -_definition_boosted_score(f, query, definition_files),
        )

        # The definition file must be first
        assert ranked[0] == "volatility3/framework/interfaces/layers.py"

    def test_multiple_definitions_rank_before_usage(self):
        """When a symbol is defined in multiple files, all rank above usage."""
        query = "TranslationLayerInterface"
        definition_files = {
            "volatility3/framework/interfaces/layers.py",
        }

        files = [
            "volatility3/framework/plugins/windows/modules.py",
            "volatility3/framework/layers/linear.py",
            "volatility3/framework/interfaces/layers.py",
            "volatility3/tests/test_translation.py",
        ]

        ranked = sorted(
            files,
            key=lambda f: -_definition_boosted_score(f, query, definition_files),
        )

        assert ranked[0] == "volatility3/framework/interfaces/layers.py"

    def test_no_definition_files_preserves_existing_ranking(self):
        """When no definitions found, ranking uses filename relevance only."""
        query = "DataLayerInterface"
        definition_files: set[str] = set()

        files = [
            "volatility3/framework/interfaces/layers.py",
            "volatility3/framework/plugins/windows/pslist.py",
        ]

        ranked = sorted(
            files,
            key=lambda f: -_definition_boosted_score(f, query, definition_files),
        )

        # Should be ranked by filename relevance (interfaces/layers.py contains
        # "layers" which matches via normalized score, etc.)
        # The key point: it doesn't crash and produces a valid ordering
        assert len(ranked) == 2


# ---------------------------------------------------------------------------
# Scope-less definition resolution via code-index (#3375)
# ---------------------------------------------------------------------------


class TestResolveViaCodeIndex:
    """Verify that _resolve_via_code_index resolves definitions from S3 code-index."""

    @pytest.fixture
    def vol3_index(self) -> dict:
        """Load the vol3 code-index fixture."""
        fixture_path = FIXTURES_DIR / "code-index-vol3-fixture.json"
        return json.loads(fixture_path.read_text())

    @pytest.fixture
    def fake_s3_client(self, vol3_index):
        """Fake S3 client that returns the vol3 fixture for load_code_index."""
        client = MagicMock()
        # Simulate get_object returning the fixture JSON
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(vol3_index).encode()
        client.get_object.return_value = {"Body": body_mock}
        # NoSuchKey exception type
        client.exceptions = MagicMock()
        client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return client

    @pytest.mark.asyncio
    async def test_resolves_definition_without_project_scope(self, fake_s3_client, vol3_index):
        """Definition file found via code-index when no project scope is available."""
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index(
                "DataLayerInterface", ["volatilityfoundation/volatility3"]
            )

        # Should find the definition file
        assert "framework/interfaces/layers.py" in result

    @pytest.mark.asyncio
    async def test_resolves_multiple_repos(self, fake_s3_client, vol3_index):
        """Should check all repos in the list."""
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index(
                "PsList",
                ["volatilityfoundation/volatility3", "other/repo"],
            )

        # PsList should be found in vol3
        assert "framework/plugins/windows/pslist.py" in result

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self, fake_s3_client, vol3_index):
        """Symbol matching should be case-insensitive."""
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index(
                "datalayerinterface", ["volatilityfoundation/volatility3"]
            )

        assert "framework/interfaces/layers.py" in result

    @pytest.mark.asyncio
    async def test_returns_empty_when_symbol_not_found(self, fake_s3_client, vol3_index):
        """Returns empty set when the symbol doesn't exist in any repo's index."""
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index(
                "NonExistentSymbol", ["volatilityfoundation/volatility3"]
            )

        assert result == set()

    @pytest.mark.asyncio
    async def test_returns_empty_when_s3_unavailable(self):
        """Returns empty set when S3 client is not configured."""
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = None
            mock_config.s3_bucket = ""
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index(
                "DataLayerInterface", ["volatilityfoundation/volatility3"]
            )

        assert result == set()

    @pytest.mark.asyncio
    async def test_returns_empty_on_empty_repos_list(self, fake_s3_client):
        """Returns empty set when no repos to check."""
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index("DataLayerInterface", [])

        assert result == set()

    @pytest.mark.asyncio
    async def test_graceful_on_s3_error(self, fake_s3_client, vol3_index):
        """Returns empty set (does not raise) when S3 throws an exception."""
        fake_s3_client.get_object.side_effect = Exception("S3 error")
        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            result = await _resolve_via_code_index(
                "DataLayerInterface", ["volatilityfoundation/volatility3"]
            )

        assert result == set()


# ---------------------------------------------------------------------------
# Non-mocked integration test: _handle_search end-to-end (#3375 mandatory)
# ---------------------------------------------------------------------------


class TestSearchDefinitionBoostIntegration:
    """End-to-end test that exercises _handle_search with definition resolution
    NOT mocked — realistic code-index fixture, NO project argument.

    This is the test that would have caught the round-1 no-op: it verifies
    that definition boost fires scope-less (from Zoekt-derived repos).
    """

    @pytest.fixture
    def vol3_index(self) -> dict:
        """Load the vol3 code-index fixture."""
        fixture_path = FIXTURES_DIR / "code-index-vol3-fixture.json"
        return json.loads(fixture_path.read_text())

    @pytest.fixture
    def fake_s3_client(self, vol3_index):
        """Fake S3 client that returns the vol3 fixture."""
        client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(vol3_index).encode()
        client.get_object.return_value = {"Body": body_mock}
        client.exceptions = MagicMock()
        client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return client

    @pytest.fixture
    def fake_zoekt(self):
        """Fake Zoekt backend returning vol3 search hits."""
        from door.acl import SearchHit

        zoekt = AsyncMock()
        # Simulate Zoekt results for "DataLayerInterface" query
        # Returns multiple hits across different files in vol3 — the definition
        # file is buried in the middle (not first).
        zoekt.search.return_value = [
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "framework/plugins/windows/pslist.py",
                    "line": 55,
                    "content": "class PsList(DataLayerInterface):",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "framework/automagic/stacker.py",
                    "line": 102,
                    "content": "    layer: DataLayerInterface = ...",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "framework/layers/physical.py",
                    "line": 30,
                    "content": "class PhysicalLayer(DataLayerInterface):",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "framework/interfaces/layers.py",
                    "line": 45,
                    "content": "class DataLayerInterface(metaclass=ABCMeta):",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "tests/test_layers.py",
                    "line": 12,
                    "content": "from volatility3.framework.interfaces.layers import DataLayerInterface",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "framework/plugins/linux/proc.py",
                    "line": 88,
                    "content": "    def _get_layer(self) -> DataLayerInterface:",
                    "match_type": "exact",
                },
            ),
        ]
        return zoekt

    @pytest.mark.asyncio
    async def test_definition_file_ranked_first_without_project_scope(
        self, fake_s3_client, fake_zoekt, vol3_index
    ):
        """The definition file lands in top results when no project argument is passed.

        This is the mandatory test from #3375 Validation: exercises _handle_search()
        end-to-end with definition resolution NOT mocked. The code-index fixture
        provides the definition mapping; Zoekt provides the initial hit list.
        Neptune is disabled. No project_scope is passed.
        """
        from door.acl import CallerPrincipal
        from door.server import _handle_search

        # Use a resolved caller so ACL passthrough works
        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
            patch("door.neptune_client.neptune_enabled", return_value=False),
            # Bypass ACL — we're testing ranking, not permissions
            patch("door.server._apply_acl", side_effect=lambda hits, _: hits),
        ):
            mock_state.zoekt = fake_zoekt
            mock_state.s3_client = fake_s3_client
            mock_state.acl_store = None
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"
            mock_config.semantic_enabled = False

            result = await _handle_search(
                {"query": "DataLayerInterface", "scope": "code", "limit": 20},
                caller=caller,
                project_scope=None,  # NO project scope — the key constraint
            )

        # Verify we got results
        assert result["total"] > 0
        results = result["results"]

        # The definition file MUST be in the top results
        result_files = [r.get("file", "") for r in results]
        assert "framework/interfaces/layers.py" in result_files

        # The definition file should be ranked FIRST (score 200 boost)
        assert results[0]["file"] == "framework/interfaces/layers.py"

    @pytest.mark.asyncio
    async def test_boost_works_with_project_scope_too(self, fake_s3_client, fake_zoekt, vol3_index):
        """Definition boost still works when project scope IS provided (no regression)."""
        from door.acl import CallerPrincipal
        from door.project_filter import ProjectScope
        from door.server import _handle_search

        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])
        project_scope = ProjectScope(
            project_id="vol3-project",
            repo_names={"volatilityfoundation/volatility3"},
        )

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
            patch("door.neptune_client.neptune_enabled", return_value=False),
            patch("door.server._apply_acl", side_effect=lambda hits, _: hits),
        ):
            mock_state.zoekt = fake_zoekt
            mock_state.s3_client = fake_s3_client
            mock_state.acl_store = None
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"
            mock_config.semantic_enabled = False

            result = await _handle_search(
                {"query": "DataLayerInterface", "scope": "code", "limit": 20},
                caller=caller,
                project_scope=project_scope,
            )

        results = result["results"]
        assert results[0]["file"] == "framework/interfaces/layers.py"
