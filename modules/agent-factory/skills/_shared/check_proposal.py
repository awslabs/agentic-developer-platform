"""Check authoring requirements the engine intentionally treats as optional."""

import json
import sys
from pathlib import Path


def check(document: dict, preview: dict | None = None) -> None:
    if not document.get("product_id"):
        raise ValueError("product_id is required for a new Product-bound proposal")
    policy = document.get("proposed_execution_policy")
    if not isinstance(policy, dict):
        raise ValueError("proposed_execution_policy is required; unbounded previews must not be emitted")
    eval_addresses = {node["address"] for node in document["nodes"] if node["kind"] == "eval"}
    if policy.get("evaluation_acceptance") != dict.fromkeys(eval_addresses, "human"):
        raise ValueError("evaluation_acceptance must explicitly map every eval address to human")
    if preview is not None and (
        preview.get("execution_is_unbounded") is not False
        or preview.get("proposed_execution_policy") is None
        or preview.get("would_register") is not True
    ):
        raise ValueError("preview is unbounded or would not register; do not emit the proposal")
    for node in document["nodes"]:
        if node["kind"] != "story":
            continue
        connection = node.get("repository_connection")
        if (
            node.get("repository_identity") not in policy.get("repository_ids", [])
            or type(node.get("provider_repository_id")) is not int
            or node["provider_repository_id"] <= 0
            or not isinstance(connection, dict)
            or connection.get("provider") != "github"
            or type(connection.get("installation_id")) is not int
            or connection["installation_id"] <= 0
        ):
            raise ValueError(f"story {node['address']} is missing verified repository binding")


if __name__ == "__main__":
    try:
        if len(sys.argv) not in (2, 4) or len(sys.argv) == 4 and sys.argv[2] != "--preview":
            raise ValueError("usage: check_proposal.py proposal.json [--preview preview.json]")
        document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        preview = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")) if len(sys.argv) == 4 else None
        check(document, preview)
    except (ValueError, KeyError, IndexError, OSError) as exc:
        print(f"proposal authoring check failed: {exc}", file=sys.stderr)
        sys.exit(1)
