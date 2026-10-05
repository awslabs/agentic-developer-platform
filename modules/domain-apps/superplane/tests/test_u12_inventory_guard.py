"""An inconsistent serving inventory is refused before any facts are derived."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "optimization",
    [[], ["-O"], ["-OO"]],
    ids=["normal", "optimized", "optimized-no-docstrings"],
)
def test_serving_inventory_without_listing_is_an_evidence_error(optimization):
    # Construct without transports: this regression cannot read a provider,
    # retained operator directory, credential store, or a live baseline.
    program = """
from types import SimpleNamespace
from superplane_acceptance.live_observer import LiveBaselineObserver
from superplane_acceptance.cli_delivery import EvidenceError

subject = LiveBaselineObserver.__new__(LiveBaselineObserver)
subject.serving_inventory = lambda: SimpleNamespace(
    services=('synthetic-service',), evidence_reference='fixture',
    evidence_sha256='0' * 64,
)
subject._ledger = lambda: SimpleNamespace(service_inventory=None)
def forbidden(*args, **kwargs):
    raise SystemExit('serving facts must not be derived')
subject._selected_cluster_identity = forbidden
subject._receipt_facts = forbidden
try:
    subject._serving_facts()
except EvidenceError as error:
    if str(error) != 'serving inventory requires its authoritative retained listing':
        raise SystemExit('unexpected evidence refusal')
else:
    raise SystemExit('inconsistent serving inventory was not refused')
print('inconsistent inventory refused before deriving facts')
"""
    result = subprocess.run(
        [sys.executable, *optimization, "-c", program],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout.strip() == "inconsistent inventory refused before deriving facts"
    )
