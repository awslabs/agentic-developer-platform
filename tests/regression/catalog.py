"""Shared test identities, selection and coverage gaps. No cloud access."""

from functools import lru_cache
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load():
    return json.loads(Path(__file__).with_name("catalog.json").read_text())


def modules(value, catalog=None):
    """IDs separated by commas/semicolons, or names separated by semicolons.

    Semicolons allow names containing commas to remain unambiguous.
    """
    catalog = catalog or load()
    aliases = {}
    for key, row in catalog["modules"].items():
        aliases[key.casefold()] = key
        aliases[row["name"].casefold()] = key
    value = value.strip()
    if value.casefold() == "all":
        return list(catalog["modules"])
    if value.casefold() in aliases:
        return [aliases[value.casefold()]]
    tokens = (
        re.split(r"[;\n]", value) if re.search(r"[;\n]", value) else value.split(",")
    )
    selected = set()
    for token in tokens:
        key = aliases.get(token.strip().casefold())
        if key is None:
            raise ValueError(f"Unknown or empty module: {token!r}")
        selected.add(key)
    return sorted(selected)


def selected_tests(value, catalog=None):
    catalog = catalog or load()
    selected = set(modules(value, catalog))
    return {
        key: row
        for key, row in catalog["tests"].items()
        if selected.intersection(row["module_ids"])
    }


def tags(rows):
    return {
        "test_ids": sorted(rows),
        **{
            field: sorted({tag for row in rows.values() for tag in row[field]})
            for field in ("module_ids", "feature_ids", "scenario_ids")
        },
    }


def cli_tags(case_id):
    return tags(
        {
            key: row
            for key, row in load()["tests"].items()
            if row["selector"] == f"CLI {case_id}"
        }
    )


def pytest_tags(path, selector):
    """Match canonical source definitions; parameter variants share identity."""
    relative = Path(path).resolve().relative_to(ROOT).as_posix()
    selector = selector.split("[", 1)[0]
    return tags(
        {
            key: row
            for key, row in load()["tests"].items()
            if row["path"] == relative and row["selector"] == selector
        }
    )


def lane(row):
    if row["kind"] != "E2E":
        return None
    if re.fullmatch(r"CLI [CDE]\d{2}", row["selector"]):
        return "module-cli"
    if row["path"].startswith("tests/e2e/chat/"):
        return "chat"
    if row["path"].startswith("tests/e2e/new_ui/"):
        return "browser"
    if row["path"] == "platform/evals/budget-ratelimit/run-eval.sh":
        return "module-budgets"
    if row["path"] == "platform/evals/cli-onboarding/run-eval.sh":
        return "module-onboarding"
    return None


def plan(value):
    catalog = load()
    selected = modules(value)
    tests = selected_tests(value)
    scenarios = {
        key: row for key, row in catalog["scenarios"].items() if key[:7] in selected
    }
    lanes = sorted({lane(row) for row in tests.values()} - {None})
    cases = sorted(
        {
            row["selector"].split()[1]
            for row in tests.values()
            if lane(row) == "module-cli"
        }
    )
    return {
        "modules": selected,
        "scenarios": scenarios,
        "tests": tests,
        "lanes": lanes,
        "cli_scope": ",".join(f"case:{case}" for case in cases),
        "unmapped_scenarios": [
            key for key, row in scenarios.items() if not row["test_ids"]
        ],
        "without_automated_e2e": [
            key
            for key, row in scenarios.items()
            if not any(lane(catalog["tests"][tid]) for tid in row["test_ids"])
        ],
        "outside_coordinator": [key for key, row in tests.items() if lane(row) is None],
        "coverage_claim": "Mapped minimums only; passing selected tests does not establish complete scenario coverage.",
    }
