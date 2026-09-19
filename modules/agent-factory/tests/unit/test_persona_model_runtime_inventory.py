"""Fail when a model-deciding path or literal escapes PMM-07's inventory."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[4]
INVENTORY_PATH = ROOT / "modules/agent-factory/persona-model-runtime-inventory.json"
MODEL_LITERAL = re.compile(
    r"(?:global|us|eu)\.anthropic\.claude-[A-Za-z0-9:._-]+"
    r"|[\"']claude-(?:opus|sonnet|haiku)-[A-Za-z0-9:._-]+"
)
SUFFIXES = {".py", ".ts", ".tf", ".yml", ".yaml", ".sh", ".json"}


def _inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _runtime_literal_files() -> set[str]:
    found: set[str] = set()
    roots = (ROOT / ".github/workflows", ROOT / "modules/agent-factory", ROOT / "platform/scripts")
    for base in roots:
        for path in base.rglob("*"):
            relative = path.relative_to(ROOT)
            if (
                not path.is_file()
                or path.suffix not in SUFFIXES
                or "tests" in relative.parts
                or path.name.endswith(".test.ts")
                or "node_modules" in relative.parts
                or ".venv" in relative.parts
                or path.name.startswith("README")
            ):
                continue
            if MODEL_LITERAL.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(relative.as_posix())
    return found


def test_every_runtime_model_literal_is_classified_in_the_inventory():
    inventory = _inventory()["literal_files"]
    classified = set(inventory["persona_execution_legacy"]) | set(
        inventory["non_persona_or_generated"]
    )
    assert _runtime_literal_files() == classified


def test_every_required_invocation_path_has_an_explicit_resolution_state():
    paths = _inventory()["invocation_paths"]
    assert set(paths) == {
        "github_webhook",
        "agent_to_agent",
        "eventbridge",
        "orchestration",
        "gitlab",
        "chat",
        "arc_github_actions",
    }
    for name, path in paths.items():
        assert (ROOT / path["entrypoint"]).is_file(), name
        assert path["state"] in {"wired_report_only", "blocked"}, name
        if path["state"] == "blocked":
            assert path.get("blocker"), name


def test_wired_queue_paths_reach_gateway_authority_and_blocked_paths_stay_visible():
    spawn = (
        ROOT / "modules/agent-factory/webhook-ingress/lambda/common/spawn_persona.py"
    ).read_text()
    publisher = (
        ROOT / "modules/agent-factory/webhook-ingress/lambda/common/sqs_publisher.py"
    ).read_text()
    engine = (ROOT / "modules/gateway/src/orchestration/dispatch_pass.py").read_text()
    bootstrap = (ROOT / "modules/gateway/src/agentauth/routes.py").read_text()
    assert "publish_envelope(envelope)" in spawn
    assert "admit_issue_work(envelope)" in publisher
    assert "get_engine_authority_writer().provision(pending)" in engine
    # The gateway signs a report-only decision inside the bootstrap boundary,
    # which is where live admission runs against the stored snapshot.
    assert "bootstrap_model_policy_live" in bootstrap

    # These assertions intentionally keep the incomplete paths visible.
    # Removing a bypass without wiring its authority is not completion.
    assert 'envelope.get("channel") == "gitlab"' in publisher
    assert _inventory()["invocation_paths"]["chat"]["state"] == "blocked"
    assert _inventory()["invocation_paths"]["arc_github_actions"]["state"] == "blocked"


def test_gateway_selector_contains_no_model_literal_or_network_client():
    selector = (ROOT / "modules/gateway/src/agentauth/model_policy.py").read_text()
    assert not MODEL_LITERAL.search(selector)
    assert "requests." not in selector
    assert "httpx." not in selector
