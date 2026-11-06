"""Product story code and pinned tests run independently in their change repos."""

import base64
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.fixtures import FixtureRequest, provision
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios.definitions import (
    fixture_source_paths,
    fixture_test,
    fixture_test_path,
)
from tests.e2e.orchestration.scenarios.delivery import LiveSession, assert_fixture_prs
from tests.e2e.orchestration.scenarios.providers import IssueProvider

pytest_plugins = ("tests.e2e.orchestration.test_product_epic_wiring",)

QUALIFICATION_ID = "q-0123456789abcdef"

GOOD_PRICING = """def discounted_cents(cents, basis_points):
    if cents < 0 or not 0 <= basis_points <= 10000:
        raise ValueError('invalid discount')
    return (cents * (10000 - basis_points) + 5000) // 10000
"""
INITIAL_PRICING = GOOD_PRICING.replace(
    "return (cents * (10000 - basis_points) + 5000) // 10000",
    "return round(cents * (10000 - basis_points) / 10000)",
)
QUOTE = """from .pricing import discounted_cents

def quote_total(items, basis_points):
    return sum(discounted_cents(cents, basis_points) for cents in items)
"""


def published_stories(config, transport):
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    provider = IssueProvider(transport)
    for index in (1, 2):
        provision(
            inventory,
            config,
            provider,
            FixtureRequest(
                f"story-{index}", provider.kind, f"{QUALIFICATION_ID}/story-{index}"
            ),
        )
    return [
        transport.issues[f"/repos/{target.repository}/issues/10"]["body"]
        for target in config.product_epic.stories
    ]


def test_issue_provider_publishes_pinned_repo_local_fixtures(wired):
    config, transport = wired
    bodies = published_stories(config, transport)
    for index, body in enumerate(bodies):
        test_path = fixture_test_path(index, QUALIFICATION_ID, product=True)
        assert test_path in body
        assert "unittest discover -s tests -v" in body
        assert "modules/gateway" not in body
        assert fixture_test(index, QUALIFICATION_ID, product=True) in body
        assert all(
            path in body
            for path in fixture_source_paths(index, QUALIFICATION_ID, product=True)
        )
    assert "The API repository's module is not available here" in bodies[1]
    assert ".pricing" in bodies[1]
    assert "modules/gateway/" in fixture_test_path(0, QUALIFICATION_ID)
    assert "src.qualification." in fixture_test(0, QUALIFICATION_ID)


def test_product_test_fixtures_execute_in_isolated_repositories(wired, tmp_path):
    config, transport = wired
    bodies = published_stories(config, transport)
    for index, body in enumerate(bodies):
        project = tmp_path / f"repo-{index}"
        project.mkdir()
        test_file = project / fixture_test_path(index, QUALIFICATION_ID, product=True)
        test_file.parent.mkdir(parents=True)
        test_file.write_text(body.split("```python\n", 1)[1].split("\n```", 1)[0])
        source = (
            project / fixture_source_paths(index, QUALIFICATION_ID, product=True)[0]
        )
        source.parent.mkdir(parents=True)
        source.write_text(INITIAL_PRICING if index == 0 else GOOD_PRICING)
        if index == 1:
            quote = (
                project / fixture_source_paths(index, QUALIFICATION_ID, product=True)[1]
            )
            quote.write_text(QUOTE)
        entrypoint = (
            project / fixture_source_paths(index, QUALIFICATION_ID, product=True)[-1]
        )
        entrypoint.write_text("from importlib import import_module\n")
        command = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ]
        environment = {**os.environ, "PYTHONPATH": str(project)}
        result = subprocess.run(
            command,
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if index == 0:
            assert result.returncode != 0
            assert "FAIL: test_half_up" in result.stderr
            source.write_text(GOOD_PRICING)
            result = subprocess.run(
                command,
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        assert result.returncode == 0, result.stdout + result.stderr
        assert ("Ran 3 tests" if index == 0 else "Ran 5 tests") in result.stderr
        if index == 1:
            source.unlink()
            without_local_pricing = subprocess.run(
                command,
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert without_local_pricing.returncode != 0
            source.write_text(GOOD_PRICING)


def test_live_session_reads_product_tests_from_each_pr_head(wired, tmp_path):
    config, _ = wired
    inventory = Inventory.create(tmp_path, QUALIFICATION_ID, config.environment)
    session = LiveSession(config, SimpleNamespace(), inventory, {})
    client = Mock()
    heads = ["a" * 40, "b" * 40]
    requested = []

    def get(path, *, github=False):
        requested.append(path)
        for index, target in enumerate(config.product_epic.stories):
            repository = target.repository
            if path == f"/repos/{repository}/pulls/10":
                return {"head": {"sha": heads[index]}}
            test_path = fixture_test_path(index, QUALIFICATION_ID, product=True)
            if path == f"/repos/{repository}/contents/{test_path}?ref={heads[index]}":
                return {
                    "content": base64.b64encode(
                        fixture_test(index, QUALIFICATION_ID, product=True).encode()
                    ).decode()
                }
        raise AssertionError(f"unexpected provider request: {path}")

    def pages(path, *, key=None):
        for index, target in enumerate(config.product_epic.stories):
            if path == f"/repos/{target.repository}/pulls/10/files":
                return [
                    {"filename": name}
                    for name in (
                        *fixture_source_paths(index, QUALIFICATION_ID, product=True),
                        fixture_test_path(index, QUALIFICATION_ID, product=True),
                    )
                ]
        return []

    client.get.side_effect = get
    client.pages.side_effect = pages
    session.client = client
    data = {
        "graph": {
            "nodes": [
                {
                    "node_ref": ref,
                    "bound_pull_request": {
                        "repo": target.repository,
                        "pr_number": 10,
                        "head_sha": heads[index],
                    },
                }
                for index, (ref, target) in enumerate(
                    zip(("first", "second"), config.product_epic.stories)
                )
            ]
        }
    }
    prs = session.pull_requests(data)
    assert_fixture_prs(config, QUALIFICATION_ID, prs)
    assert len([path for path in requested if "/contents/" in path]) == 2
    prs[1]["files"] = [
        file for file in prs[1]["files"] if not file["filename"].endswith("pricing.py")
    ]
    with pytest.raises(AssertionError):
        assert_fixture_prs(config, QUALIFICATION_ID, prs)
    prs[1]["files"].append(
        {"filename": fixture_source_paths(1, QUALIFICATION_ID, product=True)[0]}
    )
    prs[1]["test_definition"] += "\n# modified\n"
    with pytest.raises(AssertionError):
        assert_fixture_prs(config, QUALIFICATION_ID, prs)


@pytest.mark.parametrize("index,entrypoint", [(0, "app.py"), (1, "client.py")])
def test_packaged_fixture_runs_existing_root_command(
    wired, tmp_path, index, entrypoint
):
    config, transport = wired
    body = published_stories(config, transport)[index]
    component = "qualification-api" if index == 0 else "qualification-client"
    assert f"existing root {entrypoint}" in body
    assert f"Extend the existing {component}/Dockerfile" in body
    assert "root Dockerfile" not in body
    assert "This packaging change needs no workflow rewrite" in body
    assert f"COPY {entrypoint} into /app" in body
    assert f"COPY {component}/qualification /app/{component}/qualification" in body
    assert f"ENV PYTHONPATH=/app/{component}" in body
    assert f"-f {component}/Dockerfile" in body
    assert f".github/workflows/{component}-deploy.yml" in body
    assert f"{component}/{entrypoint}" not in fixture_source_paths(
        index, QUALIFICATION_ID, product=True
    )
    project = tmp_path / "repo"
    package = project / component / "qualification" / QUALIFICATION_ID
    package.mkdir(parents=True)
    (package.parent / "__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "pricing.py").write_text(GOOD_PRICING)
    if index == 1:
        (package / "quote.py").write_text(QUOTE)
    module = f"qualification.{QUALIFICATION_ID}.{'pricing' if index == 0 else 'quote'}"
    assertion = (
        "assert fixture.discounted_cents(5, 1000) == 5\n"
        if index == 0
        else "assert fixture.quote_total([5, 15], 1000) == 19\n"
    )
    (project / entrypoint).write_text(
        "from importlib import import_module\n"
        f"fixture = import_module({module!r})\n" + assertion + "print('fixture-ok')\n"
    )
    image = tmp_path / "image" / "app"
    image.mkdir(parents=True)
    shutil.copy2(project / entrypoint, image / entrypoint)
    shutil.copytree(project / component, image / component)
    test_path = image / fixture_test_path(index, QUALIFICATION_ID, product=True)
    test_path.parent.mkdir(parents=True)
    test_path.write_text(fixture_test(index, QUALIFICATION_ID, product=True))
    environment = {**os.environ, "PYTHONPATH": str(image / component)}
    command = [sys.executable, entrypoint, *(["--health"] if index == 1 else [])]
    result = subprocess.run(
        command, cwd=image, env=environment, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fixture-ok"
    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=image,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert tests.returncode == 0, tests.stderr
    assert f"Ran {3 if index == 0 else 5} tests" in tests.stderr
    shutil.rmtree(image / component)
    missing_package = subprocess.run(
        command, cwd=image, env=environment, capture_output=True, text=True
    )
    assert missing_package.returncode != 0
    assert "ModuleNotFoundError" in missing_package.stderr


def test_reviewed_path_rules_select_separate_source_components(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "modules/gateway")
    )
    from src.orchestration.deployment_manifest import load_packaged_manifest

    manifest = load_packaged_manifest()
    for index, component in enumerate(("qualification-api", "qualification-client")):
        paths = [
            *fixture_source_paths(index, QUALIFICATION_ID, product=True),
            fixture_test_path(index, QUALIFICATION_ID, product=True),
        ]
        selected = {manifest.path_rule(path).component for path in paths}
        assert selected == {component, "ignore"}
        assert all(
            manifest.path_rule(path).component == component
            for path in fixture_source_paths(index, QUALIFICATION_ID, product=True)
        )
        assert [
            entry.entry_id for entry in manifest.entries if entry.covers(component)
        ] == [f"{component}-8394"]
