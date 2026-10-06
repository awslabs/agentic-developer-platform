#!/usr/bin/env python3
"""Compatibility entrypoint for the app-owned protected registration command."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[3] / "domain-apps/superplane/infra/shared-operation-authority/scripts/register-domain-operation.py"), run_name="__main__")
