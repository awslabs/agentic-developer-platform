"""Select only reviewed B03 API and served-browser checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tests.e2e.product_onboarding.product_v04_evidence import (
    DENIAL_SELECTORS,
    ISOLATION_SELECTORS,
)


def selectors():
    return (
        ISOLATION_SELECTORS["api"],
        ISOLATION_SELECTORS["browser"],
        DENIAL_SELECTORS["api"],
        DENIAL_SELECTORS["browser"],
    )


def require_v04_inventory(inventory):
    expected = selectors()
    if not isinstance(inventory, list) or inventory != list(expected):
        raise ValueError(
            "B03 browser/API inventory is missing, duplicated, reordered or unrelated"
        )
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Select the fixed B03 browser/API checks"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--selectors", action="store_true")
    group.add_argument("--inventory", type=Path)
    args = parser.parse_args(argv)
    if args.selectors:
        print("\n".join(selectors()))
    else:
        descriptor = os.open(args.inventory, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as source:
            if os.fstat(source.fileno()).st_size > 65536:
                raise ValueError("B03 browser inventory exceeds its bound")
            require_v04_inventory(json.load(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
