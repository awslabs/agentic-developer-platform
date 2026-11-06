#!/usr/bin/env python3
"""Verify the private FinOps observation against fresh Product readbacks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/live/finops"))
from release import emit


if __name__ == "__main__":
    emit()
