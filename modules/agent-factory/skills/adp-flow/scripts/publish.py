"""Publish an approved backlog, recording each issue before attempting the next."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def save(path: Path, ledger: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ordered_issues(draft: dict) -> list[dict]:
    items = draft["issues"]
    by_alias = {item["alias"]: item for item in items}
    if len(by_alias) != len(items) or sum(item["kind"] == "epic" for item in items) != 1:
        raise ValueError("backlog must contain unique aliases and exactly one epic")
    result = [item for item in items if item["kind"] == "epic"]
    waiting = {item["alias"] for item in items if item["kind"] == "story"}
    while waiting:
        ready = [item for item in items if item["alias"] in waiting and
                 all(dependency in {entry["alias"] for entry in result} for dependency in item.get("depends_on", []))]
        if not ready:
            raise ValueError("story dependencies are missing or cyclic")
        result.extend(ready)
        waiting.difference_update(item["alias"] for item in ready)
    result.extend(item for item in items if item["kind"] == "eval")
    if len(result) != len(items):
        raise ValueError("backlog contains an unknown issue kind")
    home = result[0]["repository"]
    if any(item["repository"] != home for item in result if item["kind"] == "eval"):
        raise ValueError("evaluation issues must live in the delivery home")
    return result


def issue_node(repo: str, number: int) -> str:
    return gh("api", f"repos/{repo}/issues/{number}", "--jq", ".node_id")


def publish_issue(item: dict) -> dict:
    repo, title = item["repository"], item["title"]
    matches = json.loads(gh("issue", "list", "--repo", repo, "--state", "all", "--search",
                            f'in:title "{title}"', "--limit", "1000", "--json", "number,title,url"))
    exact = [issue for issue in matches if issue["title"] == title]
    if len(exact) > 1:
        raise ValueError(f"multiple issues named {title!r} in {repo}; choose one before publishing")
    if exact:
        number, url = exact[0]["number"], exact[0]["url"]
        created_at = None
    else:
        url = gh("issue", "create", "--repo", repo, "--title", title, "--body", item["body"])
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"repository": repo, "title": title, "number": number,
            "url": url, "created_at": created_at, "linked": False}


def link_child(parent: str, child: str) -> bool:
    query = "mutation($parent:ID!,$child:ID!){addSubIssue(input:{issueId:$parent,subIssueId:$child}){issue{id}}}"
    read = "query($parent:ID!){node(id:$parent){...on Issue{subIssues(first:100){nodes{id}}}}}"
    try:
        gh("api", "graphql", "-f", f"query={query}", "-f", f"parent={parent}", "-f", f"child={child}")
        response = json.loads(gh("api", "graphql", "-f", f"query={read}", "-f", f"parent={parent}"))
        return any(node["id"] == child for node in response["data"]["node"]["subIssues"]["nodes"])
    except (subprocess.CalledProcessError, KeyError, ValueError) as error:
        print(f"non-blocking sub-issue link failure for {child}: {error}", file=sys.stderr)
        return False


def publish(directory: Path) -> dict:
    draft = json.loads((directory / "backlog.json").read_text(encoding="utf-8"))
    items = ordered_issues(draft)
    ledger_path = directory / "story-mapping.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"issues": {}}
    for item in items:
        alias = item["alias"]
        existing = ledger["issues"].get(alias)
        if existing:
            if (existing["repository"], existing["title"]) != (item["repository"], item["title"]):
                raise ValueError(f"issue {alias} ledger repository or title conflicts with backlog")
        else:
            ledger["issues"][alias] = publish_issue(item)
            save(ledger_path, ledger)
        entry = ledger["issues"][alias]
        if not entry.get("node_id"):
            entry["node_id"] = issue_node(entry["repository"], entry["number"])
            save(ledger_path, ledger)
    parent = ledger["issues"][items[0]["alias"]]["node_id"]
    for item in items[1:]:
        entry = ledger["issues"][item["alias"]]
        if not entry["linked"]:
            entry["linked"] = link_child(parent, entry["node_id"])
            save(ledger_path, ledger)
    return ledger


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    try:
        publish(arguments.directory)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"publish: {error}\n")
