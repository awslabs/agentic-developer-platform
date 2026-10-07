"""Attach canonical tags to pytest items and JUnit, optionally filter modules."""

import json
import os
from pathlib import Path

import pytest

from . import catalog


def pytest_addoption(parser):
    parser.addoption("--adp-modules", default=os.environ.get("REGRESSION_MODULES", ""))
    parser.addoption(
        "--adp-kind",
        choices=("all", "E2E", "LOCAL"),
        default=os.environ.get("REGRESSION_KIND", "all"),
    )


def pytest_configure(config):
    for kind in ("test", "module", "feature", "scenario"):
        config.addinivalue_line(
            "markers", f"adp_{kind}(id): canonical coverage identity"
        )
    if config.getoption("--adp-modules"):
        try:
            catalog.modules(config.getoption("--adp-modules"))
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from exc


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    requested = config.getoption("--adp-modules")
    selected = set(catalog.modules(requested)) if requested else None
    kind = config.getoption("--adp-kind")
    retained, deselected = [], []
    for item in items:
        try:
            metadata = catalog.pytest_tags(item.path, item.nodeid.split("::", 1)[1])
        except (ValueError, IndexError):
            metadata = catalog.tags({})
        for field, identities in metadata.items():
            name = "adp_" + field.removesuffix("_ids")
            for identity in identities:
                item.add_marker(getattr(pytest.mark, name)(identity))
                item.user_properties.append((name + "_id", identity))
        item._adp_tags = metadata
        matches_kind = kind == "all" or any(
            catalog.load()["tests"][tid]["kind"] == kind for tid in metadata["test_ids"]
        )
        matches_module = selected is None or selected.intersection(
            metadata["module_ids"]
        )
        (retained if matches_module and matches_kind else deselected).append(item)
    items[:] = retained
    config.hook.pytest_deselected(items=deselected)


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    inventory = os.environ.get("REGRESSION_INVENTORY")
    if inventory:
        target = Path(inventory).with_suffix(".tags.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {item.nodeid: item._adp_tags for item in session.items}, indent=2
            )
            + "\n"
        )
