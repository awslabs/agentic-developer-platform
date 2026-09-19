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
        else:
            # A wired claim owes the same explicitness a blocked one does: the
            # chain that reaches gateway authority, named rather than implied.
            assert path.get("authority"), name


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

    # Only work admission attaches a snapshot on the queue paths, so one of those
    # that never reaches it cannot be reported as wired no matter how much of it
    # exists in source.
    assert "ensure_snapshot_report_only" in (
        ROOT / "modules/gateway/src/orchestration/work_admission.py"
    ).read_text()

    for path in ("gitlab", "chat", "arc_github_actions"):
        row = _inventory()["invocation_paths"][path]
        assert row["state"] == "wired_report_only", path
        assert (ROOT / row["verified_by"]).is_file(), path
    assert "register_model_root" in publisher


def test_all_nine_arc_workflows_preflight_and_enable_the_per_launch_guard():
    workflows = [path for path in _inventory()["literal_files"]["persona_execution_legacy"] if path.startswith(".github/workflows/")]
    assert len(workflows) == 9
    for path in workflows:
        text = (ROOT / path).read_text()
        assert "id-token: write" in text
        assert "ADP_ARC_MODEL_POLICY_ENABLED" in text
        assert "ADP_AGENT_CONTROL_ENDPOINT" in text
        assert text.index("npx ts-node src/arc-model-preflight.ts") < text.rindex("npx ts-node src/")



def test_the_orchestration_path_attaches_its_snapshot_before_it_publishes():
    """A "wired" claim must cite behavioural evidence, not an assumption.

    The engine reaches work admission for its *work claim* but not for its
    snapshot: the claim is reserved inside the tick transaction, before any
    protected execution record exists. Its snapshot is attached instead by
    `prepare_pending`, in the post-commit/pre-publish window -- the only moment
    the execution both exists and is still `pending`. That ordering is the whole
    reason this path can be reported as wired, so the named test must drive the
    real tick and pin it. If it disappears, the claim is unverifiable and this
    fails.
    """
    orchestration = _inventory()["invocation_paths"]["orchestration"]
    evidence = ROOT / orchestration["verified_by"]

    assert evidence.is_file(), orchestration["verified_by"]
    body = evidence.read_text()
    # The evidence must exercise the real tick composition, not a helper in
    # isolation: an uncalled helper is the failure this path is recovering from.
    assert "tick_handler_module._run()" in body
    assert 'ordered.index("commit") < ordered.index("provision")' in body
    assert 'ordered.index("snapshot") < ordered.index("publish")' in body

    engine = (ROOT / "modules/gateway/src/orchestration/dispatch_pass.py").read_text()
    assert "ensure_snapshot_report_only" in engine
    handler = (ROOT / "modules/gateway/src/orchestration/tick_handler.py").read_text()
    assert "await prepare_pending(session, dispatch_report)" in handler
    # Ordering, asserted on the source too: a preparation call that drifted after
    # the send would be refused at runtime with `dispatch_not_pending`.
    assert handler.index("await prepare_pending(") < handler.index("publish_pending(dispatch_report)")


def test_gateway_selector_contains_no_model_literal_or_network_client():
    selector = (ROOT / "modules/gateway/src/agentauth/model_policy.py").read_text()
    assert not MODEL_LITERAL.search(selector)
    assert "requests." not in selector
    assert "httpx." not in selector
