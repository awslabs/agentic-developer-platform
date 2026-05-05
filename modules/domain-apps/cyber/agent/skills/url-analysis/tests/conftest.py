"""Configure test imports for url-analysis skill tests."""

import sys
from pathlib import Path

import pytest

# Add the skill directory to sys.path so we can import modules directly
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "live: requires real AgentCore Browser access (skip in CI)")
