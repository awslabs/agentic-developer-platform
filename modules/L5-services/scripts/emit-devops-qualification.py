#!/usr/bin/env python3
"""Emit a completed DevOps attempt without rerunning native actions."""

import argparse
import sys

from devops_qualification_driver import emit_saved
from qualification_devops import main as emit_legacy


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0].startswith("--"):
        return emit_legacy(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    parser.add_argument("--observations")
    selected = parser.parse_args(arguments)
    return emit_saved(selected.source_sha, selected.fixture_path, selected.evidence_dir,
                      observations=selected.observations)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("DevOps evidence emission failed; inspect private evidence") from None
