"""Compile a confirmed interview and published issue ledger into an inert plan."""

import argparse
import json
import os
import re
import shutil
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

OPERATIONAL_ACTIONS = {"deploy", "operate"}


def shared_directory() -> Path:
    checkout = Path(__file__).resolve().parents[2] / "_shared"
    if checkout.is_dir():
        return checkout
    bundles = []
    executable = shutil.which("adp")
    if executable:
        cli = Path(executable).resolve().parent
        bundles.append(cli / "skills")
        if cli.name == "bin":
            bundles.append(cli.parent / "skills")
    bundles.append(Path(os.environ.get("ADP_HOME") or Path.home() / ".adp") / "skills")
    for bundle in bundles:
        if (bundle / "MANIFEST.json").is_file():
            return bundle / "_shared"
    raise ValueError("shared skills bundle not found; run ADP CLI setup before planning")


def load_intent(path: Path) -> dict:
    match = re.search(r"```json\s*\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL)
    if not match:
        raise ValueError("intent.md must contain a fenced JSON interview record")
    return json.loads(match.group(1))


def build_plan(intent: dict, ledger: dict, facts: dict) -> tuple[dict, dict, str]:
    template = json.loads((shared_directory() / "policy.default.json").read_text(encoding="utf-8"))
    policy = deepcopy(template)
    policy.update(intent.get("policy", {}))
    policy["limits"] = {**template["limits"], **intent.get("policy", {}).get("limits", {})}
    operational_authority = OPERATIONAL_ACTIONS.intersection(policy["allowed_actions"])
    declared_operations = set()
    issues = ledger["issues"]
    home = facts["home_repository"]
    repositories = facts["repositories"]
    epic = issues[intent["epic"]]
    if epic["repository"] != home or not epic.get("number"):
        raise ValueError("epic must be published in the delivery home")
    flow = f"adp-{epic['number']}"
    epic_ref = f"epic-{epic['number']}"
    nodes, edges, metadata, addresses = [], [], [], {}
    previous_eval = None
    evaluation_aliases = set()
    for wave in intent["waves"]:
        wave_ref = wave["ref"]
        actions = wave.get("actions", [action for action in policy["allowed_actions"] if action not in OPERATIONAL_ACTIONS])
        effects = wave.get("effects", [])
        if not isinstance(actions, list) or any(action not in policy["allowed_actions"] for action in actions):
            raise ValueError(f"wave {wave_ref} actions must be a list drawn from policy.allowed_actions")
        if not isinstance(effects, list) or any(effect not in ("external_spend", "irreversible") for effect in effects):
            raise ValueError(f"wave {wave_ref} effects must list external_spend or irreversible")
        operations = OPERATIONAL_ACTIONS.intersection(actions)
        declared_operations.update(operations)
        gate = wave.get("readiness_gate")
        if operations or effects or gate is not None:
            if not isinstance(gate, dict) or not isinstance(gate.get("title"), str) or not gate["title"].strip():
                raise ValueError(f"wave {wave_ref} requires a readiness_gate with a nonblank title")
        predecessor = previous_eval
        if gate:
            gate_address = f"{flow}/{epic_ref}/{wave_ref}/readiness"
            if "readiness" in wave["stories"]:
                raise ValueError(f"wave {wave_ref} story alias readiness conflicts with its readiness gate")
            nodes.append({"address": gate_address, "kind": "gate", "title": gate["title"]})
            if previous_eval:
                edges.append({"from_address": previous_eval, "to_address": gate_address})
            predecessor = gate_address
        evaluation_alias = wave.get("evaluation")
        if not isinstance(evaluation_alias, str) or not evaluation_alias or evaluation_alias in evaluation_aliases:
            raise ValueError(f"wave {wave_ref} must have exactly one distinct evaluation")
        evaluation_aliases.add(evaluation_alias)
        metadata.append({"epic_ref": epic_ref, "wave_ref": wave_ref, "title": wave["title"], "description": wave["description"]})
        if not wave["stories"]:
            raise ValueError(f"wave {wave_ref} has no stories")
        for alias in wave["stories"]:
            issue = issues[alias]
            repo = issue["repository"]
            expected_repo = intent["story_repositories"].get(alias)
            if not expected_repo or repo != expected_repo:
                raise ValueError(f"story {alias} repository mismatch: interview {expected_repo}, ledger {repo}")
            binding = repositories.get(repo, {})
            for field, value in (("number", issue.get("number")),
                                 ("provider_repository_id", binding.get("provider_repository_id")),
                                 ("installation_id", binding.get("installation_id"))):
                if type(value) is not int or value <= 0:
                    raise ValueError(f"story {alias} is missing valid {field}")
            address = f"{flow}/{epic_ref}/{wave_ref}/{alias}"
            addresses[alias] = address
            nodes.append({"address": address, "kind": "story", "title": issue["title"], "issue_ref": str(issue["number"]),
                          "executor": {"schema_version": 1, "kind": "agent", "role": "develop", "persona": "agent-codex-developer"},
                          "repository_identity": repo, "provider_repository_id": binding["provider_repository_id"],
                          "repository_connection": {"provider": "github", "installation_id": binding["installation_id"]}})
            if operational_authority and "deploy" not in actions:
                nodes[-1]["delivery_mode"] = "code_only"
                nodes[-1]["evidence_expectations"] = ["verified-merge"]
            if predecessor:
                edges.append({"from_address": predecessor, "to_address": address})
        evaluation = issues[evaluation_alias]
        if evaluation["repository"] != home or not evaluation.get("number"):
            raise ValueError(f"wave {wave_ref} evaluation must be published in the delivery home")
        home_binding = repositories[home]
        eval_address = f"{flow}/{epic_ref}/{wave_ref}/evidence"
        addresses[evaluation_alias] = eval_address
        nodes.append({"address": eval_address, "kind": "eval", "title": evaluation["title"],
                      "issue_ref": str(evaluation["number"]), "repository_identity": home,
                      "provider_repository_id": home_binding["provider_repository_id"]})
        edges.extend({"from_address": addresses[alias], "to_address": eval_address} for alias in wave["stories"])
        previous_eval = eval_address
    if operational_authority - declared_operations:
        missing = ", ".join(sorted(operational_authority - declared_operations))
        raise ValueError(f"policy operational authority {missing} requires a declared wave action and readiness_gate")
    edges.extend({"from_address": addresses[source], "to_address": addresses[target]} for source, target in intent.get("edges", []))
    if not facts.get("product_id"):
        raise ValueError("product_id is required")
    spec_revision = facts.get("spec_revision")
    if not isinstance(spec_revision, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", spec_revision):
        # The gateway's proposal schema takes a non-empty string here; the Product's
        # integer revision is not it (a Dev Box run sent `5` and was refused).
        raise ValueError("spec_revision must be a non-empty string naming the specification revision (for example the delivery home commit SHA), not a number")
    try:
        available = facts["project_budget"]["available_usd"]
        limit = Decimal(str(policy["limits"]["max_spend_usd"]))
        headroom = Decimal(str(available)) if available is not None else None
    except (KeyError, InvalidOperation, TypeError) as error:
        raise ValueError("Project available_usd and limits.max_spend_usd must be known amounts") from error
    if not limit.is_finite() or limit < 0 or (headroom is not None and (not headroom.is_finite() or headroom < 0)):
        raise ValueError("Project headroom and proposed limit must be finite nonnegative amounts")
    if headroom is not None and limit > headroom:
        raise ValueError(f"proposed limits.max_spend_usd {limit} exceeds Project available_usd {headroom}")
    policy["org_id"] = facts["org_id"]
    policy["repository_ids"] = sorted({home, *(node["repository_identity"] for node in nodes if node["kind"] == "story")})
    policy["evaluation_acceptance"] = {node["address"]: "human" for node in nodes if node["kind"] == "eval"}
    emitted_at = datetime.fromisoformat(facts["emitted_at"].replace("Z", "+00:00"))
    if emitted_at.utcoffset() is None:
        raise ValueError("emitted_at must include a timezone")
    if "expires_at" in intent.get("policy", {}):
        try:
            expiry = datetime.fromisoformat(policy["expires_at"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("policy.expires_at must be a timestamp with a timezone after emitted_at") from error
        if expiry.utcoffset() is None or expiry <= emitted_at:
            raise ValueError("policy.expires_at must be a timestamp with a timezone after emitted_at")
    else:
        policy["expires_at"] = (emitted_at + timedelta(days=14)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    proposal = {"flow_slug": flow, "title": intent["title"], "description": intent["description"],
                "org_id": facts["org_id"], "product_id": facts["product_id"], "intent_ref": str(epic["number"]),
                "spec_revision": facts["spec_revision"],
                "epic_metadata": [{"epic_ref": epic_ref, "title": epic["title"], "description": intent["description"]}],
                "wave_metadata": metadata, "nodes": nodes, "edges": edges, "proposed_execution_policy": policy}
    policy_intent = {"status": "proposed_not_authorized", "proposed_execution_policy": policy,
                     "evaluation_issues": [issues[wave["evaluation"]]["url"] for wave in intent["waves"]],
                     "unresolved_bindings": []}
    readme = f"# {intent['title']}\n\n{intent['description']}\n\n"
    readme += "\n".join(f"- {wave['title']}: {', '.join(wave['stories'])}; evaluation {wave['evaluation']}" for wave in intent["waves"])
    for wave in intent["waves"]:
        if wave.get("readiness_gate"):
            readme += f"\n- Human readiness before {wave['title']}: {wave['readiness_gate']['title']}"
    budget_note = ("Project uncapped, metered" if headroom is None else f"Project available: ${headroom}")
    readme += f"\n\n{budget_note}; proposed flow limit: ${limit}.\n"
    readme += "\nThis plan is proposed, not authorized. Registration creates an inert draft; only the owner can accept its gate.\n"
    return proposal, policy_intent, readme


def write_plan(directory: Path, facts_path: Path) -> dict:
    proposal, policy, readme = build_plan(
        load_intent(directory / "intent.md"),
        json.loads((directory / "story-mapping.json").read_text(encoding="utf-8")),
        json.loads(facts_path.read_text(encoding="utf-8")),
    )
    for name, content in (("proposal.authored.json", proposal), ("policy-intent.json", policy)):
        (directory / name).write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    (directory / "README.md").write_text(readme, encoding="utf-8")
    return proposal


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?")
    parser.add_argument("facts", type=Path, nargs="?")
    parser.add_argument("--shared-dir", action="store_true", help="print the shared authoring resource directory")
    args = parser.parse_args()
    try:
        if args.shared_dir:
            print(shared_directory())
        elif args.directory is None or args.facts is None:
            parser.error("directory and facts are required when writing a plan")
        else:
            write_plan(args.directory, args.facts)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"plan writer: {error}\n")
