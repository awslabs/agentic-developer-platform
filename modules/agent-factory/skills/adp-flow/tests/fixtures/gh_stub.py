#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["GH_STUB_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
def flag(name):
    return args[args.index(name) + 1]
def save():
    path.write_text(json.dumps(state))
if args[:2] == ["issue", "list"]:
    repo = flag("--repo")
    print(json.dumps([item for item in state["remote"] if item["repo"] == repo and item["title"] in flag("--search")
                      and not (state.get("hide_created_from_search") and item["number"] >= 200)]))
elif args[:2] == ["issue", "create"]:
    if state.get("fail_once") and state["creates"] == 1:
        state["fail_once"] = False
        save()
        sys.exit(1)
    repo = flag("--repo")
    number = state["next_number"]
    state["next_number"] += 1
    state["creates"] += 1
    url = f"https://github.com/{repo}/issues/{number}"
    state["remote"].append({"repo": repo, "title": flag("--title"), "number": number, "url": url})
    save()
    print(url)
elif args[:1] == ["api"] and args[1] != "graphql":
    repo, number = args[1].removeprefix("repos/").rsplit("/issues/", 1)
    if state.get("fail_node_once") and number == "200":
        state["fail_node_once"] = False
        save()
        sys.exit(1)
    assert any(item["repo"] == repo and item["number"] == int(number) for item in state["remote"])
    print(f"NODE-{repo}-{number}")
elif args[:2] == ["api", "graphql"]:
    query = flag("-f")
    parameters = {value.split("=", 1)[0]: value.split("=", 1)[1] for index, value in enumerate(args) if index and args[index-1] == "-f"}
    parent = parameters["parent"]
    if query.startswith("query=") and "mutation" in query:
        state["links"].setdefault(parent, [])
        if parameters["child"] not in state["links"][parent]:
            state["links"][parent].append(parameters["child"])
        save()
        print(json.dumps({"data": {"addSubIssue": {"issue": {"id": parent}}}}))
    else:
        print(json.dumps({"data": {"node": {"subIssues": {"nodes": [{"id": child} for child in state["links"].get(parent, [])]}}}}))
else:
    raise RuntimeError(args)
