"""Offline fixtures for the qualification harness tests (#5156).

Everything in this package runs network-free: no AWS call, no browser, no
deployed environment. That is deliberate — these tests prove the harness's
safety rules (bounds, ownership, crash recovery) which is exactly the logic you
cannot exercise against a live account without provisioning real resources.

Unlike the live suites next door, nothing here is gated behind an opt-in
environment variable, because there is nothing unsafe to opt into.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.e2e.orchestration.config import load_config

PACKAGE = Path(__file__).resolve().parent

# A config that passes every validation rule. Individual tests copy this and
# break one field, so the assertion is always "this one change is what was
# rejected" rather than "something in here is wrong".
VALID_CONFIG: dict = {
    "config_version": 1,
    "environment": "dev",
    "connection": {
        "connection_ref": "adp-dev-embark1",
        "repository": "aws-e/adp",
    },
    "identity": {
        "org_ref": "qual-org",
        "team_ref": "qual-team",
        "identity_ref": "qual-identity",
    },
    "versions": {
        "engine": "1.4.2",
        "worker": "0.9.0",
        "harness": "9072528a52f5a047a198281d8e642d4842a22525",
    },
    "bounds": {
        "max_resources": 5,
        "max_runs": 2,
        "max_usd": 5.0,
        "max_duration_seconds": 900,
    },
    "artifacts": {"directory": "artifacts"},
    "secret_refs": {"github_app_key": "secretsmanager:adp/dev/github-app/private-key"},
    "scenarios": [],
}


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config, items):
    """Undo a sibling package's unfiltered skip of every collected item.

    ``tests/e2e/chat/conftest.py`` and ``tests/e2e/infra/conftest.py`` skip all
    collected items when ``E2E_CHAT_ENABLED`` is unset, without filtering to
    their own package. Collected in the same session — ``pytest tests/`` — they
    would mark these offline tests skipped too, and pytest would still exit 0: a
    vacuous green run.

    This is a hookwrapper so the cleanup happens *after* every other
    implementation has had its say. A plain hook is not enough: hook call order
    would let a sibling add its skip marker after this one removed it, which is
    exactly what happened before (199 passing under a package-scoped run, all
    silently skipped under ``pytest tests/``).

    Only this package's items are touched; the sibling live suites keep their
    gate, which is theirs to own.
    """
    yield
    for item in items:
        if not Path(item.path).resolve().is_relative_to(PACKAGE):
            continue
        inherited = [
            marker
            for marker in item.own_markers
            if marker.name == "skip" and "E2E_CHAT_ENABLED" in str(marker.kwargs.get("reason", ""))
        ]
        for marker in inherited:
            item.own_markers.remove(marker)


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    """An isolated artifact root per test; never a shared or real directory."""
    directory = tmp_path / "artifacts"
    directory.mkdir()
    return directory


@pytest.fixture
def write_config(tmp_path: Path, artifact_dir: Path):
    """Write a config file, defaulting to the valid one, and return its path."""

    def _write(
        overrides: dict | None = None,
        *,
        name: str = "qualification.json",
        remove: Sequence[str] = (),
    ) -> Path:
        """`overrides` merges one level deep; `remove` takes dotted key paths."""
        document = json.loads(json.dumps(VALID_CONFIG))
        document["artifacts"]["directory"] = str(artifact_dir)
        for key, value in (overrides or {}).items():
            if isinstance(value, dict) and isinstance(document.get(key), dict):
                document[key].update(value)
            else:
                document[key] = value
        for dotted in remove:
            node = document
            *parents, leaf = dotted.split(".")
            for part in parents:
                node = node[part]
            node.pop(leaf, None)
        path = tmp_path / name
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def valid_config(write_config):
    """A loaded, validated config pointing at an isolated artifact directory."""
    return load_config(write_config())
