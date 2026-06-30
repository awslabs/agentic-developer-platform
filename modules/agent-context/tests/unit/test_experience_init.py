"""Unit tests for experience tool initialization (door/server.py _init_experience_tool).

Validates that:
- LiteLLMEmbeddingClient is instantiated with proxy_url (not base_url)
- The constructor signature matches what door/server.py passes

Regression test for #2435: base_url= kwarg caused silent TypeError,
leaving state.experience_tool = None → remember/experience "not available".
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


class TestExperienceToolInit:
    """Verify _init_experience_tool passes correct kwargs to LiteLLMEmbeddingClient."""

    def test_litellm_client_uses_proxy_url_kwarg(self):
        """LiteLLMEmbeddingClient must be called with proxy_url=, not base_url=.

        This is the exact regression from #2435: the constructor's signature is
        `__init__(self, proxy_url=None, model=None, timeout=30.0)` — passing
        `base_url=` causes an unexpected-keyword TypeError that silently prevents
        the experience tool from initializing.
        """
        from personal_context.embeddings import LiteLLMEmbeddingClient

        # Verify the constructor accepts proxy_url
        client = LiteLLMEmbeddingClient(proxy_url="http://test:4000/v1")
        assert client.proxy_url == "http://test:4000/v1"

    def test_litellm_client_rejects_base_url_kwarg(self):
        """LiteLLMEmbeddingClient does NOT accept base_url= (the old broken kwarg)."""
        from personal_context.embeddings import LiteLLMEmbeddingClient

        with pytest.raises(TypeError):
            LiteLLMEmbeddingClient(base_url="http://test:4000/v1")  # type: ignore[call-arg]

    def test_constructor_signature_has_proxy_url(self):
        """LiteLLMEmbeddingClient.__init__ has 'proxy_url' as its first non-self param."""
        from personal_context.embeddings import LiteLLMEmbeddingClient

        sig = inspect.signature(LiteLLMEmbeddingClient.__init__)
        params = list(sig.parameters.keys())
        # params[0] is 'self'
        assert "proxy_url" in params, f"Expected 'proxy_url' in params, got: {params}"
        assert "base_url" not in params, f"'base_url' should NOT be in params: {params}"

    def test_server_passes_proxy_url_not_base_url(self):
        """door/server.py calls LiteLLMEmbeddingClient(proxy_url=...), not base_url=.

        AST-level check to verify the call site uses the correct kwarg without
        needing to import the full server module (which has heavy deps).
        """
        server_path = Path(__file__).parent.parent.parent / "door" / "server.py"
        source = server_path.read_text()
        tree = ast.parse(source)

        # Find all calls to LiteLLMEmbeddingClient
        found_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check if the function being called is LiteLLMEmbeddingClient
                func = node.func
                if isinstance(func, ast.Name) and func.id == "LiteLLMEmbeddingClient":
                    kwargs = {kw.arg for kw in node.keywords}
                    found_calls.append(kwargs)
                elif isinstance(func, ast.Attribute) and func.attr == "LiteLLMEmbeddingClient":
                    kwargs = {kw.arg for kw in node.keywords}
                    found_calls.append(kwargs)

        assert len(found_calls) > 0, "No calls to LiteLLMEmbeddingClient found in door/server.py"

        for kwargs in found_calls:
            assert "proxy_url" in kwargs, (
                f"LiteLLMEmbeddingClient called without 'proxy_url=' kwarg. Found kwargs: {kwargs}"
            )
            assert "base_url" not in kwargs, (
                f"LiteLLMEmbeddingClient called with wrong 'base_url=' kwarg. "
                f"Found kwargs: {kwargs}"
            )
