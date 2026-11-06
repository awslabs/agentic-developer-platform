"""Audit this authored flow against canonical stories and the source revision."""

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main():
    folder = Path(__file__).resolve().parent
    root = folder.parents[3]
    design = root / "docs/engineering-insights/devlake-poc"
    plan = json.loads((folder / "proposal.authored.json").read_text())
    stories = json.loads((design / "story-contracts.json").read_text())["stories"]
    mapping = json.loads((folder / "story-mapping.json").read_text())
    acceptance = json.loads((folder / "acceptance-mapping.json").read_text())
    subprocess.run(
        [sys.executable, str(root / ".github/scripts/validate_loop_proposal.py"),
         "--authored", str(folder / "proposal.authored.json")], check=True, cwd=root,
    )
    nodes = {n["address"]: n for n in plan["nodes"]}
    parents = defaultdict(set)
    for edge in plan["edges"]:
        parents[edge["to_address"]].add(edge["from_address"])

    def upstream(a, b):
        pending, seen = list(parents[b]), set()
        while pending:
            item = pending.pop()
            if item == a:
                return True
            if item not in seen:
                seen.add(item)
                pending.extend(parents[item])
        return False

    assert "execution_policy" not in plan
    assert plan["org_id"] == ""
    assert plan["proposed_execution_policy"]["environment_connection_ids"] == []
    assert not any(n["address"].endswith("/accept") for n in nodes.values())
    waves = {"/".join(n["address"].split("/")[:3]) for n in nodes.values()}
    for wave in waves:
        members = [n for n in nodes.values() if n["address"].startswith(wave + "/")]
        evaluators = [n for n in members if n["kind"] == "eval"]
        assert len(evaluators) == 1, wave
        evaluator = evaluators[0]
        assert evaluator.get("issue_ref", "").isdigit(), "Missing real evaluation issue"
        assert evaluator["evaluation"]["acceptance_mode"] == "machine"
        assert evaluator["evaluation"]["evidence_schema"] == "repository-evaluation/v1"
        assert plan["proposed_execution_policy"]["evaluation_acceptance"][evaluator["address"]] == "machine"
        for node in members:
            if node["kind"] == "story":
                assert node["address"] in parents[evaluator["address"]], node["address"]

    assert {m["alias"] for m in mapping} == {s["alias"] for s in stories}
    mapped = {m["issue"]: m for m in mapping}
    expected = {(s["alias"], a, c) for s in stories for a, c in zip(s["acceptance_ids"], s["scenario_ids"])}
    actual = {(a["story"], a["acceptance_id"], a["scenario_id"]) for a in acceptance["criteria"]}
    assert len(acceptance["criteria"]) == len(actual) == 44
    assert expected == actual
    assert {e["id"] for e in acceptance["epic_criteria"]} == {f"EC-{i:02}" for i in range(1, 11)}
    for item in acceptance["criteria"]:
        assert item["status"] == "not-run"
        evaluator = nodes[item["acceptance_node"]]
        assert item["criterion_id"] in evaluator["criteria"]
        source = next(s for s in stories if s["alias"] == item["story"])
        text = (design / source["design"]).read_text()
        assert f"| {item['scenario_id']} | {item['acceptance_id']} |" in text
        assert item["scenario_reference"] in text
    for story in stories:
        item = mapped[story["issue"]]
        assert item["native_blocked_by"] == story["dependencies"]
        node = nodes[item["build_node"]]
        assert int(node["issue_ref"]) == story["issue"]
        assert node["delivery_mode"] == "code_only"
        assert upstream(node["address"], item["acceptance_node"])
        assert node["file_groups"]
        for dep in story["dependencies"]:
            if dep in mapped:
                assert upstream(mapped[dep]["build_node"], node["address"])
            else:
                assert any(
                    i["source"] == "repository:aws-e/adp"
                    and i["name"] == f"capability-{dep}"
                    and f"https://github.com/aws-e/adp/issues/{dep}" in i.get("description", "")
                    for i in node["required_inputs"]
                )

    # Existence means at the immutable authoring revision, not after future work.
    revision = plan["spec_revision"]
    existing = set(subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", revision], cwd=root, text=True
    ).splitlines())
    creators, writers = {}, defaultdict(set)
    after_edits = []
    counts = defaultdict(int)
    for node in nodes.values():
        for group in node.get("file_groups", []):
            for path in group.get("create", []):
                assert path not in existing, ("create exists", path)
                assert path not in creators, ("duplicate creator", path)
                creators[path] = node["address"]
                writers[path].add(node["address"])
                counts["create"] += 1
            for path in group.get("modify", []):
                assert path in existing, ("modify absent", path)
                writers[path].add(node["address"])
                counts["modify"] += 1
            for edit in group.get("modify_after", []):
                after_edits.append((node["address"], edit))
                writers[edit["path"]].add(node["address"])
                counts["modify_after"] += 1
    for address, edit in after_edits:
        assert creators[edit["path"]] == edit["created_by"]
        assert upstream(edit["created_by"], address)
    for path, addresses in writers.items():
        for a in addresses:
            for b in addresses:
                assert a == b or upstream(a, b) or upstream(b, a), ("unordered writers", path, a, b)
    identities = defaultdict(list)
    for n in nodes.values():
        if n["kind"] == "story":
            identities[n["issue_ref"]].append(n["address"])
    for addresses in identities.values():
        for a in addresses:
            for b in addresses:
                assert a == b or upstream(a, b) or upstream(b, a), ("duplicate work", a, b)
    for path in folder.glob("issues/*.md"):
        body = path.read_text()
        assert len(body.encode()) <= 8192, path
        assert "## Defect protocol" in body
        assert "## Evaluation report" in body
    for path in folder.rglob("*.md"):
        for link in re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", path.read_text()):
            parsed = urlsplit(link)
            if not parsed.scheme:
                target = path.parent / unquote(parsed.path) if parsed.path else path
                assert target.is_file(), (path.name, "missing local link", link)
    stages = json.loads((folder / "delivery-stages.json").read_text())
    assert stages["planned_campaigns"] == 1 and stages["per_story_deployments"] == 0
    assert stages["failure_finalization"]["outside_success_dag"] is True
    assert not any(n["kind"] == "gate" for n in nodes.values())
    policy = plan["proposed_execution_policy"]
    assert policy["schema_version"] == 3 and policy["human_gates"] == []
    assert policy["limits"]["max_concurrent_actions"] == 2
    assert json.loads((folder / "policy-intent.json").read_text())["policy"] == policy
    assert policy["repair_paths"] and policy["repair_required_checks"] == ["DevLake POC / verify"]
    assert set(policy["repair_paths"]) <= set(writers)
    for path in policy["repair_paths"]:
        assert not any(term in path.split("/")[-1].lower() for term in ("credential", "secret", "approval", "policy", "token"))
        assert not path.startswith("docs/")
    # Use the installed predicate implementation to check that a failing original
    # AC cannot satisfy the machine declaration, rather than trusting our JSON.
    sys.path.insert(0, str(root / "modules/gateway"))
    # Exercise the actual dispatcher resolver after launch materialization.
    # Source access is not a capability/PR receipt; local-node and workflow
    # resolution have their own native checks and are not mocked here.
    import asyncio
    import importlib.util
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from src.orchestration.accepted_node import AcceptedNodeContract, unresolved_contract_inputs
    from src.orchestration.proposal import LoopProposal, ProposedNode, validate_proposal
    from src.orchestration.registration import insert_acceptance_gate

    # Approval must be able to start repository-only work with no cloud grant.
    # The original S01 environment placeholder deadlocked the entire build wave.
    # Exercise the real resolver BEFORE materialization as well as after it.
    for raw in plan["nodes"]:
        scoped_inputs = [i for i in raw.get("required_inputs", [])
                         if i["source"].startswith(("repository:", "environment:", "environment_connection:"))]
        scoped = ProposedNode.model_validate({**raw, "required_inputs": scoped_inputs})
        unbound = AcceptedNodeContract(1, scoped, frozenset(), frozenset({"aws-e/adp"}))
        denied = asyncio.run(unresolved_contract_inputs(None, SimpleNamespace(), unbound))
        if raw.get("delivery_mode") == "code_only":
            assert not denied, ("code work blocked by runtime admission", raw["address"])
        elif raw.get("operational_release_binding"):
            assert denied, ("unbound operations must remain blocked", raw["address"])
    first = ProposedNode.model_validate(next(n for n in plan["nodes"] if n["address"].endswith("/s01")))
    assert not asyncio.run(unresolved_contract_inputs(
        None, SimpleNamespace(), AcceptedNodeContract(1, first, frozenset(), frozenset({"aws-e/adp"})),
    ))

    loader = importlib.util.spec_from_file_location("prepare_launch", folder / "prepare_launch.py")
    materializer = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(materializer)
    bound = materializer.bind(plan, {
        "org_id": "synthetic-org", "product_id": "11111111-1111-4111-8111-111111111111",
        "installation_id": 1, "connection_id": "synthetic-connection", "target": "synthetic-target",
        "resource_id": "synthetic-resource", "launch_manifest": "synthetic-manifest",
        "expires_at": "2030-01-04T00:00:00Z",
    }, now=datetime(2030, 1, 1, tzinfo=UTC))
    effective, gate = insert_acceptance_gate(LoopProposal.model_validate(bound))
    assert not validate_proposal(effective)
    assert len(effective.nodes) == len(nodes) + 1
    assert len(effective.edges) == len(plan["edges"]) + 1
    assert [n.address for n in effective.nodes if n.kind == "gate"] == [gate]
    for raw in bound["nodes"]:
        scoped_inputs = [i for i in raw.get("required_inputs", [])
                         if i["source"].startswith(("repository:", "environment:", "environment_connection:"))]
        scoped = ProposedNode.model_validate({**raw, "required_inputs": scoped_inputs})
        contract = AcceptedNodeContract(1, scoped, frozenset({"synthetic-connection"}), frozenset({"aws-e/adp"}))
        assert not asyncio.run(unresolved_contract_inputs(None, SimpleNamespace(), contract)), raw["address"]
        revoked = AcceptedNodeContract(1, scoped, frozenset(), frozenset({"aws-e/adp"}))
        denied = asyncio.run(unresolved_contract_inputs(None, SimpleNamespace(), revoked))
        assert {i.name for i in denied} == {i["name"] for i in scoped_inputs if i["source"].startswith("environment")}

    from src.orchestration.repository_evaluation_contract import Predicate, predicate_passes
    checked = set()
    for node in nodes.values():
        if node["kind"] == "eval":
            spec = node["evaluation"]
            assert {s["address"] for s in spec["predecessors"]} == {
                a for a in parents[node["address"]] if nodes[a]["kind"] == "story"
            }
            for workflow in spec.get("workflows", []):
                owner = nodes[workflow["source"]["predecessor"]]
                assert owner["executor"]["role"] == "operate"
                assert workflow["definition"] == workflow["source"]
                for artifact in workflow["artifacts"]:
                    assert artifact["bind_workflow_provenance"] is True
                    for predicate in artifact["predicates"]:
                        if predicate["criterion_id"] in node.get("criteria", []):
                            key = predicate["criterion_id"]
                            declared = Predicate.model_validate(predicate)
                            assert predicate_passes(declared, {"criteria": {key: {"outcome": "pass"}}})
                            for value in ["fail", "skipped", "not_run", None]:
                                assert not predicate_passes(declared, {"criteria": {key: {"outcome": value}}})
                            assert not predicate_passes(declared, {"criteria": {}})
                            checked.add(key)
        if node.get("executor", {}).get("role") == "operate":
            binding = node["operational_release_binding"]
            delivery = binding["delivery_contract"]
            assert delivery["schema_version"] == 2 and delivery["cleanup_required"]
            assert "all-required-criteria" in delivery["required_checks"]
            assert binding["definition_predecessor"]
    assert checked == {a["criterion_id"] for a in acceptance["criteria"]} | {e["id"] for e in acceptance["epic_criteria"]}
    print(json.dumps({"nodes": len(nodes), "edges": len(plan["edges"]), "waves": len(waves),
                      "feature_stories": len(stories), "ac_scenario_pairs": len(actual),
                      "epic_criteria": 10, "planned_unique_files": len(writers),
                      "file_operations": dict(counts), "unordered_writers": 0,
                      "planned_release_campaigns": 1, "per_story_deployments": 0,
                      "machine_evaluations": 3, "human_evaluations": 0,
                      "repair_paths": len(policy["repair_paths"]),
                      "mandatory_predicates_negative_checked": len(checked)}, indent=2))


if __name__ == "__main__":
    main()
