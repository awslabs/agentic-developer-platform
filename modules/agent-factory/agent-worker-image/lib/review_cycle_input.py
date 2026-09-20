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
        or envelope.get("persona") != ("reviewer" if value.get("action") == "review" else "developer")
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
    run(["gh", "pr", "checkout", str(value["pr_number"]), "--repo", value["repo"]], cwd=cwd)
    sha = run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    if sha != value["head_sha"]:
        raise RuntimeError("Review-cycle PR head changed before worker startup")
    branch = run(["git", "branch", "--show-current"], cwd=cwd).stdout.strip()
    if not branch:
        raise RuntimeError("Review-cycle PR has no working branch")
    return branch, sha
