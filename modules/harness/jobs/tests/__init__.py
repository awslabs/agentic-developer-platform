"""Test package for `harness_jobs`.

A package rather than loose modules so the suites can share fixtures and helpers
through relative imports (`from .conftest import requires_postgres`) without depending
on pytest's `sys.path` insertion, which differs between invocation directories.
"""
