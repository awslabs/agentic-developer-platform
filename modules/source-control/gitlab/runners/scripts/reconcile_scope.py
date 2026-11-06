"""Validate the complete operator-approved group set for orphan reconciliation."""

import re
import sys


def group_ids(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", value):
        raise ValueError("an exact comma-separated list of numeric groups is required")
    groups = value.split(",")
    if len(groups) > 32 or len(groups) != len(set(groups)):
        raise ValueError("group list is duplicated or exceeds the approved limit")
    return value


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("one protected group-list input is required")
        print(group_ids(sys.argv[1]))
    except ValueError as error:
        sys.exit(f"Runner reconciliation refused: {error}")
