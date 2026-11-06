"""Select and verify only the reviewed served-browser V11 Product cases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tests.e2e.product_onboarding.product_v11_evidence import CASES


def selectors():
    return tuple(spec[2] for spec in CASES.values())


def require_v11_inventory(inventory):
    expected = selectors()
    if not isinstance(inventory, list) or inventory != list(expected):
        raise ValueError("V11 browser inventory is missing, duplicated, reordered or includes unrelated cases")
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description="Select and verify the pinned V11 browser suite")
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
                raise ValueError("V11 browser inventory exceeds its bound")
            require_v11_inventory(json.load(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
