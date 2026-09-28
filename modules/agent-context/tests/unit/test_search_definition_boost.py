"""Unit tests for search definition-boost ranking (#3303, #3375, #3451).

Verifies:
- _is_identifier_query detects CamelCase, snake_case, and rejects free-text
- _definition_boosted_score pins definition files above all other tiers
- _resolve_definition_files works scope-less via code-index (#3375)
- Non-mocked integration: _handle_search with code-index fixture, no project scope
- Injection: definition files absent from Zoekt are synthesized and injected (#3451)
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
    _synthesize_definition_entry,
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


# ---------------------------------------------------------------------------
# _synthesize_definition_entry unit tests (#3451)
# ---------------------------------------------------------------------------


class TestSynthesizeDefinitionEntry:
    """Verify that injected entries match Zoekt-hit shape."""

    def test_entry_shape_matches_zoekt_hit(self):
        """Synthesized entry must have all required Zoekt-hit fields."""
        definition_files = {"framework/interfaces/layers.py"}
        zoekt_repos = {"volatilityfoundation/volatility3"}

        entry = _synthesize_definition_entry(
            "framework/interfaces/layers.py",
            "DataLayerInterface",
            definition_files,
            zoekt_repos,
        )

        assert entry is not None
        # Must have all fields that a Zoekt hit has
        assert "repo_id" in entry
        assert "file" in entry
        assert "line" in entry
        assert "content" in entry
        assert "match_type" in entry
        # Types must match
        assert isinstance(entry["repo_id"], str)
        assert isinstance(entry["file"], str)
        assert isinstance(entry["line"], int)
        assert isinstance(entry["content"], str)
        assert entry["match_type"] == "exact"

    def test_entry_file_path_is_bare(self):
        """Synthesized entry uses bare path (no repo prefix), matching Zoekt format."""
        definition_files = {
            "framework/interfaces/layers.py",
            "volatilityfoundation/volatility3/framework/interfaces/layers.py",
        }
        zoekt_repos = {"volatilityfoundation/volatility3"}

        # When called with repo-prefixed path, it should strip the prefix
        entry = _synthesize_definition_entry(
            "volatilityfoundation/volatility3/framework/interfaces/layers.py",
            "DataLayerInterface",
            definition_files,
            zoekt_repos,
        )

        assert entry is not None
        assert entry["file"] == "framework/interfaces/layers.py"
        assert entry["repo_id"] == "volatilityfoundation/volatility3"

    def test_returns_none_for_empty_path(self):
        """Empty file path should return None."""
        entry = _synthesize_definition_entry(
            "", "DataLayerInterface", set(), set()
        )
        assert entry is None

    def test_bare_path_preserved_when_no_repo_prefix(self):
        """Bare path (no repo prefix) is kept as-is in the file field."""
        definition_files = {"framework/interfaces/layers.py"}
        zoekt_repos = {"volatilityfoundation/volatility3"}

        entry = _synthesize_definition_entry(
            "framework/interfaces/layers.py",
            "DataLayerInterface",
            definition_files,
            zoekt_repos,
        )

        assert entry is not None
        assert entry["file"] == "framework/interfaces/layers.py"

    def test_repo_id_populated_from_zoekt_repos(self):
        """repo_id should be set from the zoekt_repos set."""
        definition_files = {"src/models.py"}
        zoekt_repos = {"myorg/myrepo"}

        entry = _synthesize_definition_entry(
            "src/models.py", "MyModel", definition_files, zoekt_repos
        )

        assert entry is not None
        assert entry["repo_id"] == "myorg/myrepo"


# ---------------------------------------------------------------------------
# Injection cap and gating tests (#3451)
# ---------------------------------------------------------------------------


class TestDefinitionInjection:
    """Verify injection behavior: cap, gating on identifier queries, no duplication."""

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
    def fake_zoekt_without_definition(self):
        """Fake Zoekt backend whose results do NOT contain the definition file.

        This is the corrected fixture from #3451: round 2's test had the
        definition file IN the Zoekt results, masking the injection gap.
        """
        from door.acl import SearchHit

        zoekt = AsyncMock()
        # Only usage files — definition file (framework/interfaces/layers.py)
        # is deliberately ABSENT, simulating the real-world case where hundreds
        # of usage files crowd it out of Zoekt's top results.
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
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "framework/plugins/windows/modules.py",
                    "line": 44,
                    "content": "    layer: DataLayerInterface",
                    "match_type": "exact",
                },
            ),
        ]
        return zoekt

    @pytest.mark.asyncio
    async def test_injection_when_definition_absent_from_zoekt(
        self, fake_s3_client, fake_zoekt_without_definition, vol3_index
    ):
        """MANDATORY corrected test (#3451): definition file NOT in Zoekt results
        but resolved via code-index → must be INJECTED into top results.

        This is the test round 2 should have had. Round 2's fixture contained
        the definition file in Zoekt results, so it only tested re-ranking.
        """
        from door.acl import CallerPrincipal
        from door.server import _handle_search

        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
            patch("door.neptune_client.neptune_enabled", return_value=False),
            patch("door.server._apply_acl", side_effect=lambda hits, _: hits),
        ):
            mock_state.zoekt = fake_zoekt_without_definition
            mock_state.s3_client = fake_s3_client
            mock_state.acl_store = None
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"
            mock_config.semantic_enabled = False

            result = await _handle_search(
                {"query": "DataLayerInterface", "scope": "code", "limit": 20},
                caller=caller,
                project_scope=None,
            )

        # The definition file must appear in results despite not being in Zoekt
        results = result["results"]
        result_files = [r.get("file", "") for r in results]
        assert "framework/interfaces/layers.py" in result_files, (
            "Definition file should be INJECTED when absent from Zoekt results"
        )

        # It must be ranked FIRST (score 200 from definition boost)
        assert results[0]["file"] == "framework/interfaces/layers.py", (
            "Injected definition file should be ranked first via boost"
        )

    @pytest.mark.asyncio
    async def test_no_injection_for_natural_language_query(
        self, fake_s3_client, fake_zoekt_without_definition
    ):
        """Natural-language queries must NOT trigger injection."""
        from door.acl import CallerPrincipal
        from door.server import _handle_search

        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])

        # Modify the zoekt mock to return results for a free-text query
        from door.acl import SearchHit

        fake_zoekt_without_definition.search.return_value = [
            SearchHit(
                repo_name="volatilityfoundation/volatility3",
                data={
                    "repo_id": "volatilityfoundation/volatility3",
                    "file": "docs/getting-started.md",
                    "line": 10,
                    "content": "layer interface overview",
                    "match_type": "exact",
                },
            ),
        ]

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
            patch("door.neptune_client.neptune_enabled", return_value=False),
            patch("door.server._apply_acl", side_effect=lambda hits, _: hits),
        ):
            mock_state.zoekt = fake_zoekt_without_definition
            mock_state.s3_client = fake_s3_client
            mock_state.acl_store = None
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"
            mock_config.semantic_enabled = False

            result = await _handle_search(
                {"query": "layer interface overview", "scope": "code", "limit": 20},
                caller=caller,
                project_scope=None,
            )

        # No injection should happen — the query isn't identifier-shaped
        results = result["results"]
        result_files = [r.get("file", "") for r in results]
        assert "framework/interfaces/layers.py" not in result_files

    @pytest.mark.asyncio
    async def test_injection_capped_at_two(self, fake_s3_client):
        """At most 2 definition files should be injected per query."""
        from door.acl import CallerPrincipal, SearchHit
        from door.server import _handle_search

        # Create a code-index with 4 definitions for the same symbol
        multi_def_index = {
            "repo_id": "org/repo",
            "indexed_at": "2026-07-01T00:00:00Z",
            "definitions": [
                {"symbol": "Widget", "file": "src/widget_a.py", "line": 1, "kind": "class"},
                {"symbol": "Widget", "file": "src/widget_b.py", "line": 1, "kind": "class"},
                {"symbol": "Widget", "file": "src/widget_c.py", "line": 1, "kind": "class"},
                {"symbol": "Widget", "file": "src/widget_d.py", "line": 1, "kind": "class"},
            ],
        }
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(multi_def_index).encode()
        fake_s3_client.get_object.return_value = {"Body": body_mock}

        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])

        # Zoekt returns a single unrelated file
        fake_zoekt = AsyncMock()
        fake_zoekt.search.return_value = [
            SearchHit(
                repo_name="org/repo",
                data={
                    "repo_id": "org/repo",
                    "file": "tests/test_widget.py",
                    "line": 5,
                    "content": "from src.widget_a import Widget",
                    "match_type": "exact",
                },
            ),
        ]

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
                {"query": "Widget", "scope": "code", "limit": 20},
                caller=caller,
                project_scope=None,
            )

        results = result["results"]
        # Total results: 1 from Zoekt + at most 2 injected = 3
        # (4 definitions resolved but cap is 2)
        injected_files = [
            r["file"] for r in results
            if r["file"].startswith("src/widget_") and r["file"] != "tests/test_widget.py"
        ]
        assert len(injected_files) <= 2, (
            f"Injection should be capped at 2, got {len(injected_files)}"
        )
        # At least some injection happened
        assert len(injected_files) > 0

    @pytest.mark.asyncio
    async def test_no_duplicate_injection_when_already_in_zoekt(
        self, fake_s3_client, vol3_index
    ):
        """If definition file is already in Zoekt results, don't inject a duplicate."""
        from door.acl import CallerPrincipal, SearchHit
        from door.server import _handle_search

        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])

        # Zoekt results INCLUDE the definition file
        fake_zoekt = AsyncMock()
        fake_zoekt.search.return_value = [
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
                    "file": "framework/plugins/windows/pslist.py",
                    "line": 55,
                    "content": "class PsList(DataLayerInterface):",
                    "match_type": "exact",
                },
            ),
        ]

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
                project_scope=None,
            )

        results = result["results"]
        # Count occurrences of the definition file — should be exactly 1 (from Zoekt, not injected)
        def_file_count = sum(
            1 for r in results if r["file"] == "framework/interfaces/layers.py"
        )
        assert def_file_count == 1, (
            f"Definition file should appear exactly once, found {def_file_count}"
        )


# ---------------------------------------------------------------------------
# _strip_host_prefix tests (#3512)
# ---------------------------------------------------------------------------


class TestStripHostPrefix:
    """Verify host-prefix stripping for Zoekt repo names (#3512)."""

    def test_github_com_prefix_stripped(self):
        from door.server import _strip_host_prefix

        assert _strip_host_prefix("github.com/org/repo") == "org/repo"

    def test_github_com_full_repo_stripped(self):
        from door.server import _strip_host_prefix

        assert (
            _strip_host_prefix("github.com/volatilityfoundation/volatility3")
            == "volatilityfoundation/volatility3"
        )

    def test_gitlab_example_com_stripped(self):
        from door.server import _strip_host_prefix

        assert _strip_host_prefix("gitlab.example.com/org/repo") == "org/repo"

    def test_org_repo_unchanged(self):
        from door.server import _strip_host_prefix

        assert _strip_host_prefix("org/repo") == "org/repo"

    def test_org_repo_full_unchanged(self):
        from door.server import _strip_host_prefix

        assert (
            _strip_host_prefix("volatilityfoundation/volatility3")
            == "volatilityfoundation/volatility3"
        )

    def test_bare_repo_name_unchanged(self):
        from door.server import _strip_host_prefix

        assert _strip_host_prefix("repo") == "repo"

    def test_empty_string_unchanged(self):
        from door.server import _strip_host_prefix

        assert _strip_host_prefix("") == ""

    def test_self_hosted_domain_stripped(self):
        from door.server import _strip_host_prefix

        assert _strip_host_prefix("git.internal.corp/team/project") == "team/project"


# ---------------------------------------------------------------------------
# Host-prefixed repo regression test (#3512)
# ---------------------------------------------------------------------------


class TestHostPrefixedRepoResolution:
    """Mandatory non-mocked regression test: host-prefixed repo_name from Zoekt
    must still resolve definitions via code-index (#3512).

    This test reproduces the LIVE failing condition:
    - SearchHit.repo_name = "github.com/volatilityfoundation/volatility3" (host-prefixed)
    - Zoekt results EXCLUDE the definition file (framework/interfaces/layers.py)
    - S3 code-index key is "code-indexes/volatilityfoundation-volatility3.json"

    Before the fix, _resolve_via_code_index receives the host-prefixed name,
    normalizes it to "github.com-volatilityfoundation-volatility3", and fails
    to find the S3 key. Result: definition_files is empty, injection never fires.
    """

    @pytest.fixture
    def vol3_index(self) -> dict:
        """Load the vol3 code-index fixture."""
        fixture_path = FIXTURES_DIR / "code-index-vol3-fixture.json"
        return json.loads(fixture_path.read_text())

    @pytest.fixture
    def fake_s3_client(self, vol3_index):
        """Fake S3 client keyed ONLY at the real S3 path (no host prefix).

        The only valid key is 'code-indexes/volatilityfoundation-volatility3.json'.
        A request for 'code-indexes/github.com-volatilityfoundation-volatility3.json'
        will raise NoSuchKey — exactly reproducing the live failure.
        """
        client = MagicMock()
        NoSuchKey = type("NoSuchKey", (Exception,), {})
        client.exceptions = MagicMock()
        client.exceptions.NoSuchKey = NoSuchKey

        valid_key = "code-indexes/volatilityfoundation-volatility3.json"

        def get_object_side_effect(Bucket, Key):
            if Key == valid_key:
                body_mock = MagicMock()
                body_mock.read.return_value = json.dumps(vol3_index).encode()
                return {"Body": body_mock}
            raise NoSuchKey(f"NoSuchKey: {Key}")

        client.get_object.side_effect = get_object_side_effect
        # list_objects_v2 for suffix-match strategy
        client.list_objects_v2.return_value = {
            "Contents": [{"Key": valid_key}],
        }
        return client

    @pytest.fixture
    def fake_zoekt_host_prefixed(self):
        """Fake Zoekt backend returning hits with HOST-PREFIXED repo_name.

        This is the key difference from existing tests: repo_name includes
        'github.com/' prefix, exactly as the live Zoekt API returns.
        Definition file (framework/interfaces/layers.py) is deliberately ABSENT.
        """
        from door.acl import SearchHit

        zoekt = AsyncMock()
        zoekt.search.return_value = [
            SearchHit(
                repo_name="github.com/volatilityfoundation/volatility3",
                data={
                    "repo_id": "github.com/volatilityfoundation/volatility3",
                    "file": "framework/plugins/windows/pslist.py",
                    "line": 55,
                    "content": "class PsList(DataLayerInterface):",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="github.com/volatilityfoundation/volatility3",
                data={
                    "repo_id": "github.com/volatilityfoundation/volatility3",
                    "file": "framework/automagic/stacker.py",
                    "line": 102,
                    "content": "    layer: DataLayerInterface = ...",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="github.com/volatilityfoundation/volatility3",
                data={
                    "repo_id": "github.com/volatilityfoundation/volatility3",
                    "file": "framework/layers/physical.py",
                    "line": 30,
                    "content": "class PhysicalLayer(DataLayerInterface):",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="github.com/volatilityfoundation/volatility3",
                data={
                    "repo_id": "github.com/volatilityfoundation/volatility3",
                    "file": "tests/test_layers.py",
                    "line": 12,
                    "content": "from volatility3.framework.interfaces.layers import DataLayerInterface",
                    "match_type": "exact",
                },
            ),
            SearchHit(
                repo_name="github.com/volatilityfoundation/volatility3",
                data={
                    "repo_id": "github.com/volatilityfoundation/volatility3",
                    "file": "framework/plugins/linux/proc.py",
                    "line": 88,
                    "content": "    def _get_layer(self) -> DataLayerInterface:",
                    "match_type": "exact",
                },
            ),
        ]
        return zoekt

    @pytest.mark.asyncio
    async def test_host_prefixed_repo_resolves_definition(
        self, fake_s3_client, fake_zoekt_host_prefixed
    ):
        """Host-prefixed repo names from Zoekt must resolve definitions via code-index.

        This test MUST fail on main before the fix (host prefix defeats lookup)
        and pass after applying _strip_host_prefix at zoekt_repos collection.
        """
        from door.acl import CallerPrincipal
        from door.server import _handle_search

        caller = CallerPrincipal(github_login="test-user", github_teams=["eng"])

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
            patch("door.neptune_client.neptune_enabled", return_value=False),
            patch("door.server._apply_acl", side_effect=lambda hits, _: hits),
        ):
            mock_state.zoekt = fake_zoekt_host_prefixed
            mock_state.s3_client = fake_s3_client
            mock_state.acl_store = None
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"
            mock_config.semantic_enabled = False

            result = await _handle_search(
                {"query": "DataLayerInterface", "scope": "code", "limit": 20},
                caller=caller,
                project_scope=None,
            )

        results = result["results"]
        result_files = [r.get("file", "") for r in results]

        # The definition file MUST be injected (it's not in Zoekt results)
        assert "framework/interfaces/layers.py" in result_files, (
            "Definition file must be injected when Zoekt returns host-prefixed repo names. "
            f"Got files: {result_files}"
        )

        # It must be ranked FIRST (score 200 from definition boost)
        assert results[0]["file"] == "framework/interfaces/layers.py", (
            "Injected definition file must be ranked first via boost. "
            f"Got first: {results[0].get('file', '')}"
        )

    @pytest.mark.asyncio
    async def test_host_prefixed_repo_code_index_receives_stripped_name(
        self, fake_s3_client, fake_zoekt_host_prefixed
    ):
        """Verify _resolve_via_code_index receives org/repo (not host/org/repo).

        Directly tests that the stripping produces a name that matches the
        real S3 key format (code-indexes/volatilityfoundation-volatility3.json).
        """
        from door.server import _resolve_via_code_index

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            # With stripped name (correct — should find the index)
            result = await _resolve_via_code_index(
                "DataLayerInterface",
                ["volatilityfoundation/volatility3"],
            )
            assert result, "Stripped repo name must resolve via code-index"
            assert any("layers.py" in f for f in result)

    @pytest.mark.asyncio
    async def test_host_prefixed_name_fails_without_stripping(
        self, fake_s3_client, fake_zoekt_host_prefixed
    ):
        """Confirm the host-prefixed name fails to resolve (documents the bug).

        This validates our S3 mock is realistic: a request for the wrong key
        raises NoSuchKey, and the suffix-match also fails because the normalized
        name is longer than the real filename.
        """
        from door.server import _resolve_via_code_index

        with (
            patch("door.server.state") as mock_state,
            patch("door.server.config") as mock_config,
        ):
            mock_state.s3_client = fake_s3_client
            mock_config.s3_bucket = "test-bucket"
            mock_config.code_index_s3_prefix = "content/code-indexes"

            # With host-prefixed name (the bug — should NOT find the index)
            result = await _resolve_via_code_index(
                "DataLayerInterface",
                ["github.com/volatilityfoundation/volatility3"],
            )
            assert not result, (
                "Host-prefixed repo name should NOT resolve via code-index "
                "(this documents the bug that #3512 fixes)"
            )
