"""Workspace bootstrap — Issue #5533 (w6-10), EPIC #4910.

This directory is a package, and the underscore in its name is load-bearing. Both
facts exist to keep the module-wide CI lane collectable; see
`tests/test_layout.py`, which asserts the rule and explains what happens when it is
broken. `../spike/__init__.py` is the existing precedent.

The bootstrap code itself lives in `superplane_bootstrap/`. Nothing is exported here:
this file makes the DIRECTORY importable so that pytest's dotted module names below
it are prefixed (`workspace_bootstrap.tests.conftest`), which is the only thing that
distinguishes this suite's `conftest` from the sibling suites'.
"""
