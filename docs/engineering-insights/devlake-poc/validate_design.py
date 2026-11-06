"""Validate design links, source references, coverage and dependency contracts."""

import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


def anchors(text):
    values = set()
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.M):
        heading = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        values.add(heading)
    return values


def check_graph(graph):
    complete, active, ordered = set(), set(), []

    def visit(node):
        if node in active:
            raise ValueError(f"dependency cycle at {node}")
        if node in complete:
            return
        active.add(node)
        for dependency in graph.get(node, []):
            visit(str(dependency))
        active.remove(node)
        complete.add(node)
        ordered.append(node)

    for node in graph:
        visit(node)
    return ordered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--issue-dir", type=Path)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    root = directory.parents[2]
    contract = json.loads((directory / "story-contracts.json").read_text())
    dependencies = json.loads((directory / "dependencies.json").read_text())
    graph = dependencies["blocked_by"].copy()
    for node, parents in dependencies.get("declared_capability_dependencies", {}).items():
        graph[node] = sorted(set(graph.get(node, []) + parents))
    order = check_graph(graph)
    if 8889 in graph["8919"] or 8893 in graph["8920"]:
        raise ValueError("controlled acceptance boundary reversed")
    issues = [story["issue"] for story in contract["stories"]]
    if len(issues) != 13 or len(set(issues)) != 13:
        raise ValueError("expected exactly 13 existing stories")
    if {criterion for story in contract["stories"] for criterion in story["epic_criteria"]} != {f"EC-{number:02}" for number in range(1, 11)}:
        raise ValueError("epic coverage differs")
    total_ac, total_scenarios = 0, 0
    for story in contract["stories"]:
        text = (directory / story["design"]).read_text()
        for label, prefix in (("acceptance_ids", "AC"), ("scenario_ids", "SCN")):
            actual = set(re.findall(rf"^\| ({prefix}-\d+) \|", text, re.M))
            if actual != set(story[label]):
                raise ValueError(f"{story['alias']} changed {label}")
        if sorted(dependencies["blocked_by"][str(story["issue"])]) != story["dependencies"]:
            raise ValueError("native prerequisite mismatch")
        total_ac += len(story["acceptance_ids"])
        total_scenarios += len(story["scenario_ids"])
    link_count, source_count, unchecked = 0, 0, set()
    cache = {}
    for path in sorted(directory.rglob("*.md")):
        text = path.read_text()
        for link in re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", text):
            parsed = urlsplit(link)
            if not parsed.scheme:
                target = path.parent / unquote(parsed.path) if parsed.path else path
                if not target.exists():
                    raise ValueError(f"{path.name}: missing {link}")
                if parsed.fragment and target.suffix == ".md" and unquote(parsed.fragment) not in anchors(target.read_text()):
                    raise ValueError(f"{path.name}: missing anchor {link}")
                link_count += 1
            elif parsed.netloc == "github.com" and "/blob/" in parsed.path:
                repository, source = parsed.path.strip("/").split("/blob/", 1)
                revision, source_path = source.split("/", 1)
                source_root = root if repository == "aws-e/adp" else args.upstream_root if repository == "apache/devlake" else None
                if source_root is None:
                    unchecked.add(link)
                    continue
                key = (str(source_root), revision, source_path)
                if key not in cache:
                    cache[key] = subprocess.check_output(["git", "-C", str(source_root), "show", f"{revision}:{source_path}"]).decode()
                if parsed.fragment.startswith("L") and int(parsed.fragment[1:]) > len(cache[key].splitlines()):
                    raise ValueError(f"source line beyond file: {link}")
                source_count += 1
    if args.issue_dir:
        specification = importlib.util.spec_from_file_location("issue_lint", root / "modules/insights/authoring/validate_issue.py")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        for number in [8887, *issues]:
            body = (args.issue_dir / f"{number}.md").read_text()
            errors, advisories = module.lint(body, stage="draft")
            if errors:
                raise ValueError(f"issue {number}: {errors}")
            if len(body.encode()) > 8192:
                raise ValueError(f"oversized issue {number}")
            if number != 8887:
                story = next(story for story in contract["stories"] if story["issue"] == number)
                for label, prefix in (("acceptance_ids", "AC"), ("scenario_ids", "SCN")):
                    if set(re.findall(rf"^\| ({prefix}-\d+) \|", body, re.M)) != set(story[label]):
                        raise ValueError(f"issue {number}: changed {label}")
            elif set(re.findall(r"^\| (EC-\d+) \|", body, re.M)) != {f"EC-{number:02}" for number in range(1, 11)}:
                raise ValueError("epic criterion mismatch")
            print(f"issue {number}: {len(body.encode())} bytes; format valid; {len(advisories)} planning advisories")
    print(f"13 stories; 10 EC; {total_ac} AC; {total_scenarios} SCN; {link_count} local links; {source_count} pinned source links valid")
    print("acyclic native + declared-capability order: " + ", ".join(order))
    print(f"{len(unchecked)} external source links require separate HTTPS verification")


if __name__ == "__main__":
    main()
