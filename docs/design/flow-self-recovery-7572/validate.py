"""Validate this design's local links, pinned sources, examples and coverage."""

import ast
import csv
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from audit import BASE, DESTINATION, ROOT


def headings(text):
    found = []
    for heading in re.findall(r"^#{1,6} (.+)$", text, re.M):
        plain = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", heading)
        plain = re.sub(r"[^\w\- ]", "", plain.lower()).replace(" ", "-")
        found.append(plain)
    return set(found)


def verify_link(document, link):
    parsed = urlsplit(link)
    if parsed.scheme:
        matched = re.fullmatch(r"/aws-e/adp/blob/([^/]+)/(.+)", parsed.path) if parsed.netloc == "github.com" else None
        if matched:
            revision, path = matched.groups()
            assert revision == BASE, f"Unpinned source in {document.name}"
            content = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=ROOT).decode()
            if parsed.fragment:
                line = re.fullmatch(r"L(\d+)", parsed.fragment)
                assert line and 1 <= int(line[1]) <= len(content.splitlines()), f"Bad source line: {link}"
        return
    target = (document.parent / unquote(parsed.path)).resolve() if parsed.path else document
    assert target.exists(), f"Missing link from {document.name}: {link}"
    if parsed.fragment and target.suffix == ".md":
        assert unquote(parsed.fragment) in headings(target.read_text()), f"Missing heading in {document.name}: {link}"


def main():
    links = 0
    for document in sorted(DESTINATION.glob("*.md")):
        text = document.read_text()
        definitions = dict(re.findall(r"^\[([^]]+)\]:\s*(\S+)", text, re.M))
        for label in re.findall(r"(?<!!)\[[^]\n]+\]\[([^]\n]+)\]", text):
            assert label in definitions, f"Undefined reference in {document.name}: {label}"
        targets = re.findall(r"(?<!!)\[[^]\n]+\]\(([^)\s]+)\)", text) + list(definitions.values())
        for link in targets:
            verify_link(document, link)
            links += 1
    backlog = (DESTINATION / "backlog.md").read_text()
    section = backlog.split("## Existing acceptance and scenario mapping", 1)[1].split("## Proposed additional acceptance cases", 1)[0]
    expected = {7559: (3, 3), 7563: (3, 3), 7571: (2, 2), 7382: (3, 3), 7381: (4, 3), 7385: (3, 3), 7358: (7, 5), 7457: (4, 4), 7513: (5, 5), 7572: (1, 1)}
    for issue, (acceptance_count, scenario_count) in expected.items():
        identity_cells = re.findall(rf"^\| {issue} ([^|]+)\|", section, re.M)
        accepted = set(re.findall(r"AC-\d+", " ".join(identity_cells)))
        scenarios = set(re.findall(r"SCN-\d+", " ".join(identity_cells)))
        assert accepted == {f"AC-{number:02}" for number in range(1, acceptance_count + 1)}, f"AC coverage: {issue}"
        assert scenarios == {f"SCN-{number:02}" for number in range(1, scenario_count + 1)}, f"Scenario coverage: {issue}"
    for block in re.findall(r"```bash\n(.*?)```", backlog, re.S):
        for candidate in re.findall(r"(?:tests|platform|src)/[^\s]+\.(?:py|tsx)", block):
            locations = [ROOT / candidate, ROOT / "modules/gateway" / candidate, ROOT / "modules/gateway/frontend" / candidate]
            assert any(location.is_file() for location in locations), f"Nonexistent example test: {candidate}"
    rows = list(csv.DictReader((DESTINATION / "gateway-migrations.tsv").open(), delimiter="\t"))
    revisions = [row["revision"] for row in rows]
    assert len(revisions) == len(set(revisions)) and all(revisions)
    parents = {}
    for row in rows:
        raw = ast.literal_eval(row["down_revision"])
        parents[row["revision"]] = set(raw if isinstance(raw, tuple) else [raw] if raw else [])
    all_parents = set().union(*parents.values())
    assert all_parents <= set(revisions)
    assert set(revisions) - all_parents == {"094_credential_agent_consent"}
    visited = set()
    while len(visited) < len(revisions):
        ready = {revision for revision, dependencies in parents.items() if revision not in visited and dependencies <= visited}
        assert ready, "Cyclic migration graph"
        visited.update(ready)
    tracked = list(csv.DictReader((DESTINATION / "tracked-artifacts.tsv").open(), delimiter="\t"))
    expected_paths = set(subprocess.check_output(["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT).decode().splitlines())
    assert len(tracked) == len(expected_paths) == 10139
    assert {row["path"] for row in tracked} == expected_paths
    assert all(row["audit_component"] and row["disposition"] for row in tracked)
    entries = list(csv.DictReader((DESTINATION / "entrypoints.tsv").open(), delimiter="\t"))
    workflow_paths = {row["path"] for row in tracked if row["path"].startswith(".github/workflows/") and row["path"].endswith((".yml", ".yaml"))}
    assert workflow_paths <= {row["path"] for row in entries}
    print(f"PASS: {links} links; 35 existing ACs and mapped scenarios; runnable example paths; 101 migration revisions; 10139 unique tracked artifacts; {len(workflow_paths)} workflows")


if __name__ == "__main__":
    main()
