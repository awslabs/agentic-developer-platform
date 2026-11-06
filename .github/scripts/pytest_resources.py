"""Bound coverage workers by runner memory and retain cgroup peak/OOM evidence."""

import argparse
import json
import os
from pathlib import Path


def memory_limit(root=Path("/sys/fs/cgroup"), physical=None):
    if physical is None:
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    limits = [physical]
    # ARC Linux runners use cgroup v2; retain the v1 fallback for older images.
    for name in ("memory.max", "memory/memory.limit_in_bytes"):
        try:
            value = (root / name).read_text().strip()
        except FileNotFoundError:
            continue
        if value != "max":
            limits.append(int(value))
    return min(limits)


def workers(coverage, limit):
    # Four coverage processes previously exhausted an 8 GiB runner. Reserve
    # four workers for the 16 GiB allocation; leave smaller runners at two.
    return 2 if coverage and limit < 16 * 1024**3 else 4


def report(root=Path("/sys/fs/cgroup")):
    result = {"memory_limit_bytes": memory_limit(root)}
    for name in (
        "memory.peak",
        "memory.current",
        "memory.events",
        "memory/memory.max_usage_in_bytes",
        "memory/memory.failcnt",
    ):
        path = root / name
        if path.exists():
            result[name] = path.read_text().strip()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(report(), sort_keys=True)
        if args.report
        else workers(args.coverage, memory_limit())
    )
