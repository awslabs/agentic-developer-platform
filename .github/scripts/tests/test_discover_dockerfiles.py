"""Tests for discover-dockerfiles.py."""

import json
import os
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from discover_dockerfiles import discover_dockerfiles


def test_finds_dockerfiles_in_subdirs():
    """Discovers Dockerfiles in nested directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create some Dockerfiles
        (Path(tmpdir) / "modules" / "gateway").mkdir(parents=True)
        (Path(tmpdir) / "modules" / "gateway" / "Dockerfile").write_text("FROM python:3.12")
        (Path(tmpdir) / "platform" / "runner").mkdir(parents=True)
        (Path(tmpdir) / "platform" / "runner" / "Dockerfile").write_text("FROM ubuntu:22.04")

        result = discover_dockerfiles(tmpdir)
        assert len(result) == 2
        assert "./modules/gateway/Dockerfile" in result
        assert "./platform/runner/Dockerfile" in result


def test_excludes_node_modules():
    """Excludes Dockerfiles inside node_modules."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "app").mkdir()
        (Path(tmpdir) / "app" / "Dockerfile").write_text("FROM node:20")
        (Path(tmpdir) / "node_modules" / "pkg").mkdir(parents=True)
        (Path(tmpdir) / "node_modules" / "pkg" / "Dockerfile").write_text("FROM node:18")

        result = discover_dockerfiles(tmpdir)
        assert len(result) == 1
        assert "./app/Dockerfile" in result


def test_excludes_hidden_dirs():
    """Excludes Dockerfiles inside hidden directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "app").mkdir()
        (Path(tmpdir) / "app" / "Dockerfile").write_text("FROM python:3.12")
        (Path(tmpdir) / ".hidden").mkdir()
        (Path(tmpdir) / ".hidden" / "Dockerfile").write_text("FROM ubuntu")

        result = discover_dockerfiles(tmpdir)
        assert len(result) == 1


def test_empty_repo():
    """Returns empty list for repo with no Dockerfiles."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "src").mkdir()
        (Path(tmpdir) / "src" / "main.py").write_text("print('hello')")

        result = discover_dockerfiles(tmpdir)
        assert result == []


def test_results_are_sorted():
    """Results are sorted alphabetically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for name in ["zebra", "alpha", "middle"]:
            (Path(tmpdir) / name).mkdir()
            (Path(tmpdir) / name / "Dockerfile").write_text("FROM scratch")

        result = discover_dockerfiles(tmpdir)
        assert result == sorted(result)
