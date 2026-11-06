"""SCN-11: inventory readers and dispatch sites before they can bypass authority."""

import ast
import json
import re
import runpy
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[6]
AGENT = REPO / "modules/agent-factory/agent/src"
LAMBDA = REPO / "modules/agent-factory/webhook-ingress/lambda"
REGISTRY = json.loads(Path(__file__).with_name("comment-consumers-7845.json").read_text())
READERS = re.compile(
    r"fetchApprovalComments\(|fetchIssueComments\(|getIssueComments\(|"
    r"getApprovalComments\(|\.issues\.listComments\(|\.comments\.list\(|"
    r"\.comments\b|selectPmDecision\(|selectSuperpowerGuidance\(|"
    r"resolveVerifiedGateAnswer\(|checkPostedApproval\(|"
    r"await\s+authorizeComment\(|await\s+selectLegacyRetry\("
)
PUBLISHERS = {
    "spawn_persona",
    "_spawn_persona",
    "publish_envelope",
    "_capture_invocation_event",
    "extract_intent_with_reason",
    "handle_agent_trigger",
    "handle_eventbridge",
}
WORKFLOW_ENTRY = re.compile(r"npx\s+ts-node\s+src/(agent-[\w-]+|skill-agent)\.ts\b")
INLINE_WORKFLOW_READER = re.compile(
    r"github\.rest\.issues\.listComments\(|gh\s+issue\s+view[^\n]*--json[^\n]*comments"
)


def discover_readers(sources):
    return {name for name, source in sources.items() if READERS.search(source)}


def discover_publishers(sources):
    discovered = set()
    for name, source in sources.items():
        tree = ast.parse(source, filename=name)
        calls = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        if calls & PUBLISHERS:
            discovered.add(name)
    return discovered


def discover_workflows(sources):
    return {
        name: match.group(1) + ".ts"
        for name, source in sources.items()
        if (match := WORKFLOW_ENTRY.search(source))
    }


def assert_registry(ts_sources, python_sources, workflow_sources):
    assert discover_readers(ts_sources) == set(REGISTRY["typescript"])
    for name, (role, gate) in REGISTRY["typescript"].items():
        assert role in {
            "executable",
            "context-and-presence",
            "context-only",
            "reporting-only",
            "transport-only",
        }
        source = ts_sources[name]
        if role in {"executable", "context-and-presence"}:
            assert re.search(r"\bawait\s+" + re.escape(gate), source), name
        else:
            assert gate in source, name
    assert discover_publishers(python_sources) == set(REGISTRY["python"])
    assert discover_workflows(workflow_sources) == REGISTRY["workflows"]
    assert {
        name for name, source in workflow_sources.items() if INLINE_WORKFLOW_READER.search(source)
    } == set(REGISTRY["inline_workflow_readers"])
    assert (
        "agent-[name].ts"
        in (REPO / "modules/agent-factory/rules/workflows/agent-template.yml").read_text()
    )


def assert_dispatch_gate(parser_source, handler_source):
    parser = ast.parse(parser_source)
    events = [
        node
        for node in parser.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DISPATCH_EVENTS"
            for target in node.targets
        )
    ]
    assert len(events) == 1
    assert isinstance(events[0].value, ast.Call)
    actual = {tuple(pair) for pair in ast.literal_eval(events[0].value.args[0])}
    assert actual == {tuple(pair) for pair in REGISTRY["dispatch_events"]}
    assert "(event_type, action) not in DISPATCH_EVENTS" in parser_source
    assert "(event_type, action) not in DISPATCH_EVENTS" in handler_source

    handler = ast.parse(handler_source)
    admissions = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and {name.id for name in ast.walk(node.test) if isinstance(name, ast.Name)}
        >= {"intent", "engine_command"}
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_authorize_github_dispatch"
            for call in ast.walk(node)
        )
    ]
    assert len(admissions) == 1
    admission = admissions[0]
    assert any(
        isinstance(node, ast.If)
        and any(isinstance(child, ast.Return) for child in ast.walk(node))
        and "decision" in {name.id for name in ast.walk(node.test) if isinstance(name, ast.Name)}
        for node in ast.walk(admission)
        if isinstance(node, ast.If)
    )
    effects = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_spawn_persona", "_capture_invocation_event"}
    ]

    def early_rate_limit_capture(node):
        rate_guards = [
            branch
            for branch in ast.walk(handler)
            if isinstance(branch, ast.If)
            and ast.unparse(branch.test) == "not allowed"
            and branch.lineno < admission.lineno
            and any(isinstance(child, ast.Return) for child in ast.walk(branch))
        ]
        return (
            any(node in ast.walk(branch) for branch in rate_guards)
            and any(
                keyword.arg == "status"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "rate_limited"
                for keyword in node.keywords
            )
        )

    assert effects and all(
        node.lineno > admission.end_lineno or early_rate_limit_capture(node) for node in effects
    )


def sources():
    ts_sources = {
        str(path.relative_to(AGENT)): path.read_text()
        for path in AGENT.rglob("*.ts")
        if not path.name.endswith(".test.ts")
    }
    python_sources = {
        str(path.relative_to(LAMBDA)): path.read_text()
        for path in LAMBDA.rglob("*.py")
        if "tests" not in path.parts
    }
    paths = list((REPO / ".github/workflows").glob("*.yml"))
    paths += list((REPO / "modules/agent-factory/client-workflows").rglob("*.yml"))
    workflow_sources = {str(path.relative_to(REPO)): path.read_text() for path in paths}
    return ts_sources, python_sources, workflow_sources


def test_tracked_comment_consumers_and_dispatch_are_classified():
    ts_sources, python_sources, workflow_sources = sources()
    assert_registry(ts_sources, python_sources, workflow_sources)
    assert_dispatch_gate(
        python_sources["github/intent_parser.py"], python_sources["github/handler.py"]
    )


def test_packaging_contract_rejects_missing_jwt(tmp_path):
    script = (
        REPO / "modules/agent-factory/webhook-ingress/scripts/check-comment-authority-package.py"
    )
    archive = tmp_path / "github.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("handler.py", "def handler(event, context): pass")
        package.writestr("common/github_actor_authority.py", "")
    with pytest.raises(ValueError, match="missing jwt/__init__.py"):
        runpy.run_path(str(script))["verify_archive"](archive)
    requirements = (REPO / "modules/agent-factory/webhook-ingress/requirements.txt").read_text()
    assert "PyJWT==" in requirements and "cryptography==" in requirements
    workflow = (REPO / ".github/workflows/webhook-ingress-ci.yml").read_text()
    assert "pytest lambda/" in workflow
    assert "python3 scripts/check-comment-authority-package.py dist/github.zip" in workflow


@pytest.mark.parametrize(
    "mutation",
    [
        "new_reader",
        "missing_gate",
        "new_publisher",
        "copied_prompt",
        "copied_workflow",
        "inline_workflow",
        "new_event",
        "early_effect",
    ],
)
def test_guard_catches_unreviewed_mutation(mutation):
    ts_sources, python_sources, workflow_sources = sources()
    if mutation == "new_reader":
        ts_sources["new-reader.ts"] = "const comments = await fetchApprovalComments();"
    elif mutation == "missing_gate":
        ts_sources["monitoring.ts"] = ts_sources["monitoring.ts"].replace(
            "await authorizeComment(comment, context)", "true"
        )
    elif mutation == "new_publisher":
        python_sources["github/unguarded.py"] = "def trigger():\n    spawn_persona()\n"
    elif mutation == "copied_prompt":
        ts_sources["agent-superpower-copy.ts"] = ts_sources["agent-superpower.ts"]
    elif mutation == "copied_workflow":
        workflow_sources[".github/workflows/agent-copy.yml"] = workflow_sources[
            ".github/workflows/agent-pm.yml"
        ]
    elif mutation == "inline_workflow":
        workflow_sources[".github/workflows/ungated.yml"] = (
            "const comments = await github.rest.issues.listComments({});"
        )
    elif mutation == "new_event":
        python_sources["github/intent_parser.py"] = python_sources[
            "github/intent_parser.py"
        ].replace('("issues", "opened"),', '("issues", "opened"),\n    ("issues", "edited"),')
    else:
        python_sources["github/handler.py"] = python_sources["github/handler.py"].replace(
            "    # 10. Parse intent", "    _capture_invocation_event()\n    # 10. Parse intent"
        )
    with pytest.raises(AssertionError):
        assert_registry(ts_sources, python_sources, workflow_sources)
        assert_dispatch_gate(
            python_sources["github/intent_parser.py"], python_sources["github/handler.py"]
        )
