"""Safety-critical language and invocation checks for the flow skill."""

import re
from pathlib import Path


SKILL = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")


def test_flow_hard_rules_and_invocation():
    assert "disable-model-invocation: true" in SKILL.split("---", 2)[1]
    normalized = " ".join(SKILL.split())
    rules = (
        "Never approve a gate.",
        "Never register without a `proposed_execution_policy` with `expires_at`.",
        "Never register a story node without `repository_identity`, `provider_repository_id` and `repository_connection`.",
        "Never create an issue that the ledger or a title search shows exists.",
        "Never print tokens. Never write outside the delivery home plan directory and the target repositories' issues.",
    )
    assert all(rule in normalized for rule in rules)
    assert "owner alone decides whether to accept" in SKILL
    assert all((Path(__file__).resolve().parents[1] / name).is_file()
               for name in ("checks.md", "intent.template.md", "plan-readme.template.md"))


def test_flow_contains_no_dispatch_mention_or_label_flag():
    assert not re.search(r"@agent-", SKILL)
    assert "--add-label" not in SKILL


def test_grilling_has_only_two_documented_changes_and_original_mit_license():
    from hashlib import sha256

    grilling = Path(__file__).resolve().parents[2] / "adp-grilling"
    document = (grilling / "SKILL.md").read_text(encoding="utf-8")
    upstream_description = "description: Grill the user relentlessly about a plan, decision, or idea. Use when the user wants to stress-test their thinking, or uses any 'grill' trigger phrases."
    vendored_description = "description: Grill the user relentlessly about a plan, decision, or idea. Use for ADP intent interviews and delivery-flow planning, or when the user wants to stress-test their thinking or uses any 'grill' trigger phrases."
    seed = "A caller may pass an initial question set as a frontier seed. Ask those questions as round one, with a recommended answer for each, then continue the normal rounds until the frontier is empty.\n\n"
    assert document.count(vendored_description) == 1
    assert document.count(seed) == 1
    original = document.replace(vendored_description, upstream_description).replace(seed, "")
    assert sha256(original.encode()).hexdigest() == "a146842ed93ca43d833550fa9adf02e58cb63c83fea9d4b4522995b82242d4e2"
    assert sha256((grilling / "LICENSE").read_bytes()).hexdigest() == "0e7ac423bf2c6e223b7c5b156f8cf72da49d748e56a1641402c31f22ad07dbb5"
    assert "7739eaed7f8f3915437a8def198a6d0d7206dbc7" in (grilling / "NOTICE").read_text()


def test_both_skills_have_parseable_frontmatter_and_codex_discovery_metadata():
    import json

    import yaml

    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "MANIFEST.json").read_text())
    assert {entry["name"] for entry in manifest["skills"]} == {"adp-flow", "adp-grilling"}
    assert all(set(entry["clients"]) == {"claude-code", "codex"} for entry in manifest["skills"])
    for name in ("adp-flow", "adp-grilling"):
        directory = root / name
        document = (directory / "SKILL.md").read_text(encoding="utf-8")
        assert document.startswith("---\n")
        frontmatter = yaml.safe_load(document.split("---", 2)[1])
        assert frontmatter["name"] and frontmatter["description"]
        agent = yaml.safe_load((directory / "agents/openai.yaml").read_text(encoding="utf-8"))
        assert agent["interface"]["display_name"]
        assert agent["interface"]["short_description"]


def test_combined_skill_contract_preserves_inert_handoff():
    import json

    import yaml

    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "MANIFEST.json").read_text())
    names = {item["name"]: item for item in manifest["skills"]}
    flow = yaml.safe_load(SKILL.split("---", 2)[1])
    grilling_text = (root / "adp-grilling/SKILL.md").read_text()
    grilling = yaml.safe_load(grilling_text.split("---", 2)[1])
    assert flow["name"] == "adp-flow"
    assert flow["disable-model-invocation"] is True
    assert grilling["name"] == "grilling"
    assert "disable-model-invocation" not in grilling
    assert "frontier seed" in grilling_text
    assert names["adp-flow"]["invocation"] == "adp flow"
    assert names["adp-grilling"]["invocation"] == "adp grilling"
    assert "registered_inert_not_accepted" in SKILL
    assert "Never approve a gate." in SKILL
    assert "without running it" in SKILL
    assert "@agent-" not in SKILL
    assert "--add-label" not in SKILL
