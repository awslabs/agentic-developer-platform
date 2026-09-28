#!/usr/bin/env python3
"""Print only addresses/actions of saved-plan deletions; reject invalid plans."""
import json
import sys


def deletions(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("resource_changes"), list):
        raise ValueError("missing resource_changes")
    allowed = (["no-op"], ["read"], ["create"], ["update"], ["delete"],
               ["create", "delete"], ["delete", "create"], ["forget"])
    result = []
    for resource in plan["resource_changes"]:
        address = resource["address"]
        actions = resource["change"]["actions"]
        if not isinstance(address, str) or actions not in allowed:
            raise ValueError("invalid resource change")
        if "delete" in actions or "forget" in actions:
            result.append(f"{address}: {','.join(actions)}")
    return result


if __name__ == "__main__":
    try:
        with open(sys.argv[1]) as source:
            result = deletions(json.load(source))
    except (ValueError, KeyError, TypeError, OSError, IndexError):
        print("Cannot validate Terraform plan actions", file=sys.stderr)
        sys.exit(1)
    if result:
        print("\n".join(result))
