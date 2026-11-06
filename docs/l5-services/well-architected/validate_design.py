"""Offline source/link/contract/story checks, not provider or authorization proof."""

import csv
import hashlib
import json
import re
import subprocess
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[2]


def headings(text):
    return {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
            for heading in re.findall(r"^#{1,6} (.+)$", text, re.M)}


def check_links():
    checked = 0
    for document in HOME.rglob("*.md"):
        text = document.read_text()
        links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
        links += re.findall(r"^\[[^\]]+\]:\s+(\S+)", text, re.M)
        defined = set(re.findall(r"^\[([^\]]+)\]:", text, re.M))
        assert set(re.findall(r"\[[^\]]+\]\[([^\]]+)\]", text)) <= defined, document
        for target in links:
            parsed = urlsplit(target)
            if parsed.scheme:
                matched = re.fullmatch(r"/aws-e/adp/blob/([a-f0-9]{40})/(.+)", parsed.path) if parsed.netloc == "github.com" else None
                if matched:
                    body = subprocess.check_output(["git", "show", matched[1] + ":" + unquote(matched[2])], cwd=ROOT).decode()
                    if parsed.fragment.startswith("L"):
                        assert int(parsed.fragment[1:].split("-")[0]) <= len(body.splitlines()), target
                    elif parsed.fragment:
                        assert parsed.fragment in headings(body), target
                    checked += 1
                continue
            resolved = (document.parent / unquote(parsed.path)).resolve() if parsed.path else document
            assert resolved.exists(), (document, target)
            if parsed.fragment and resolved.suffix == ".md":
                assert parsed.fragment in headings(resolved.read_text()), (document, target)
            checked += 1
    return checked


def check_contract():
    schema = json.loads((HOME / "contract-proposal.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/RunInput"})
    example = json.loads((HOME / "request-example.json").read_text())
    assert validator.is_valid(example)
    cases = []
    for field, value in (("service_id", "security"), ("capability", "resource_inspection"),
                         ("configuration_revision", 0), ("delegation_id", "forged")):
        changed = deepcopy(example)
        changed[field] = value
        cases.append(changed)
    for field, value in (("record_type", "environment"), ("record_revision", "HEAD")):
        changed = deepcopy(example)
        changed["sources"][0][field] = value
        cases.append(changed)
    changed = deepcopy(example)
    changed["sources"] = []
    cases.append(changed)
    assert all(not validator.is_valid(case) for case in cases)
    settings = Draft202012Validator({**schema, "$ref": "#/$defs/Settings"})
    valid = {"review_mode": "source_only", "pillars": ["SECURITY"],
             "execution_role_ref": {**example["sources"][0], "record_type": "role"},
             "source_store_ref": {**example["sources"][0], "record_type": "artifact_store"}}
    assert settings.is_valid(valid)
    assert not settings.is_valid({**valid, "pillars": ["SUSTAINABILITY"]})
    assert not settings.is_valid({**valid, "review_mode": "resource_inspection"})
    shared = json.loads((HOME.parent / "contracts/v1/schemas.json").read_text())["$defs"]["RecordRef"]
    assert set(schema["$defs"]["RecordRef"]["properties"]) == set(shared["properties"])
    return len(cases) + 4


def check_stories():
    specs = json.loads((HOME / "story-specs.json").read_text())
    alignment = json.loads((HOME / "issue-alignment.json").read_text())
    aliases = {story["alias"] for story in specs["stories"]}
    assert aliases == {f"WA{number:02}" for number in range(1, 8)}
    covered = set()
    architecture = headings((HOME / "architecture.md").read_text())
    backlog = (HOME / "backlog.md").read_text()
    for story in specs["stories"]:
        assert set(story["depends_on"]) <= aliases - {story["alias"]}
        assert set(story["sections"]) <= architecture
        ids = [check["id"] for check in story["checks"]]
        assert len(ids) == len(set(ids))
        for check in story["checks"]:
            covered.update(check["ec"])
            assert story["alias"] + "/" + check["id"] in backlog
            assert check["assertion"] in backlog
    assert covered == {f"EC-{number:02}" for number in range(1, 9)}
    pending = {story["alias"]: set(story["depends_on"]) for story in specs["stories"]}
    seen = set()
    while pending:
        ready = [alias for alias, dependencies in pending.items() if dependencies <= seen]
        assert ready, "cyclic story dependencies"
        for alias in ready:
            seen.add(alias)
            del pending[alias]
    if alignment["stories"]:
        assert {story["alias"] for story in alignment["stories"]} == aliases
        assert re.fullmatch(r"[a-f0-9]{40}", alignment["canonical_revision"])
        for story in alignment["stories"]:
            body = (HOME / "stories" / (story["alias"].lower() + ".md")).read_text()
            assert len(body.encode()) <= 8192
            assert re.findall(r"^## (.+)$", body, re.M) == ["The problem in plain terms", "Description", "Impact analysis", "Design", "Deployment", "Validation"]
            assert alignment["canonical_revision"] in body
            assert "Approved design" in body and "Accepted" in body
            assert alignment["approval"]["status"] == "approved"
            assert alignment["approval"]["accepted_revision"] == alignment["canonical_revision"]
            assert alignment["approval"]["evidence_url"] in body
            assert "@agent-" not in body
            assert hashlib.sha256(body.encode()).hexdigest() == story["body_sha256"]
            specification = next(item for item in specs["stories"] if item["alias"] == story["alias"])
            for check in specification["checks"]:
                assert check["id"] in body and "SCN-" + check["id"].split("-")[1] in body
                assert check["assertion"] in body
    return sum(len(story["checks"]) for story in specs["stories"])


def check_inventory():
    summary = json.loads((HOME / "inventory-summary.json").read_text())
    with (HOME / "tracked-artifacts.tsv").open() as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    actual = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", summary["revision"]], cwd=ROOT).decode().splitlines()
    assert {row["path"] for row in rows} == set(actual)
    assert len(rows) == summary["tracked_artifacts"]
    assert all(row["component"] and row["disposition"] for row in rows)
    assert summary["unmapped_paths"] == []
    current = set(actual)
    with (HOME / "main-delta.tsv").open() as source:
        delta = list(csv.DictReader(source, delimiter="\t"))
    for row in delta:
        assert row["component"] and row["disposition"]
        if row["status"] == "D":
            current.remove(row["path"])
        else:
            current.add(row["path"])
    followup = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", summary["followup_main"]], cwd=ROOT).decode().splitlines()
    assert current == set(followup)
    assert len(delta) == summary["followup_changed_paths"]
    assert len(current) == summary["followup_tracked_artifacts"]
    return len(rows)


def main():
    print(f"Verified {check_links()} local/immutable source links; {check_contract()} input cases; "
          f"{check_stories()} acceptance rows; {check_inventory()} tracked paths")
    print("Schema shape and source consistency only; no live authority/provider/consumer/approval claim")


if __name__ == "__main__":
    main()
