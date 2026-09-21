"""Carry the engine's bounded continuation assignment into the actual model input."""
from __future__ import annotations

import json
import os
import re

ENV = "ADP_REVIEW_CYCLE_INPUT"


def prepare_cycle_input(envelope: dict) -> dict | None:
    os.environ.pop(ENV, None)
    value = envelope.get("review_cycle_input")
    if value is None:
        return None
    source = envelope.get("source_ref") or {}
    if (
        not isinstance(value, dict)
        or (envelope.get("intent") or {}).get("trigger") != "engine_review_cycle"
        or value.get("action") not in {"review", "repair"}
        or envelope.get("persona") not in {
            "agent-codex-reviewer", "reviewer" if value.get("action") == "review" else "developer"
        }
        or value.get("repo") != source.get("repo")
        or type(value.get("pr_number")) is not int
        or value["pr_number"] < 1
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get("head_sha", "")))
        or not isinstance(value.get("findings"), list)
        or not value.get("accepted_scope")
        or not value.get("operation_key")
    ):
        raise RuntimeError("Invalid protected review-cycle input")
    encoded = json.dumps(value, sort_keys=True)
    if len(encoded.encode()) > 32768:
        raise RuntimeError("Review-cycle input exceeds its bound")
    os.environ[ENV] = encoded
    return value


def checkout_cycle_input(value, *, run, cwd):
    """Use the existing PR, with no canonical-branch creation/reset or WIP push."""
    metadata = json.loads(run(
        ["gh", "pr", "view", str(value["pr_number"]), "--repo", value["repo"],
         "--json", "headRefName,isCrossRepository,state"], cwd=cwd, timeout=120,
    ).stdout)
    if not isinstance(metadata, dict) or type(metadata.get("isCrossRepository")) is not bool:
        raise RuntimeError("Review-cycle PR branch metadata is unavailable")
    if metadata.get("state") == "MERGED" and value["action"] == "review":
        # GitHub retains the PR head ref after deleting its source branch. gh's
        # same-repository checkout still fetches that deleted branch (including
        # with --detach). Review the exact retained commit on a local-only branch;
        # never recreate or push the deleted provider branch.
        run(["git", "fetch", "--no-tags", "origin", f"refs/pull/{value['pr_number']}/head"], cwd=cwd, timeout=120)
        sha = run(["git", "rev-parse", "FETCH_HEAD"], cwd=cwd, timeout=30).stdout.strip()
        if sha != value["head_sha"]:
            raise RuntimeError("Review-cycle PR head changed before worker startup")
        branch = f"adp-review/pr-{value['pr_number']}-{sha[:12]}"
        run(["git", "checkout", "-b", branch, sha], cwd=cwd, timeout=30)
        return branch, sha
    if metadata.get("state") != "OPEN":
        raise RuntimeError("Review-cycle repair or unmerged review requires an open PR")
    if not metadata["isCrossRepository"]:
        branch = metadata.get("headRefName")
        if not isinstance(branch, str) or not branch:
            raise RuntimeError("Review-cycle PR branch metadata is unavailable")
        run(["git", "check-ref-format", "--branch", branch], cwd=cwd, timeout=30)
        # Bootstrap's depth-limited clone only maps main. gh fetches the PR ref
        # but git cannot establish its upstream without this exact branch map.
        # Preserve gh's existing fork handling; origin belongs to the base repo.
        run(["git", "remote", "set-branches", "--add", "origin", branch], cwd=cwd, timeout=30)
    run(["gh", "pr", "checkout", str(value["pr_number"]), "--repo", value["repo"]], cwd=cwd, timeout=120)
    sha = run(["git", "rev-parse", "HEAD"], cwd=cwd, timeout=30).stdout.strip()
    if sha != value["head_sha"]:
        raise RuntimeError("Review-cycle PR head changed before worker startup")
    branch = run(["git", "branch", "--show-current"], cwd=cwd, timeout=30).stdout.strip()
    if not branch:
        raise RuntimeError("Review-cycle PR has no working branch")
    return branch, sha
