"""Uniquely named fixture plugin for the hosting suites.

Issue #5050 (U5), EPIC #4910.

The fakes themselves live in `_hosting_fakes.py` so the test modules can import them by
name — this directory is not a package, so a relative import is unavailable. Keeping this
out of ``conftest.py`` also prevents pytest's global ``conftest`` module name from shadowing
the established observation-suite fixtures during whole-domain collection. This module
records that the fakes
stand in for B and close no live criterion.

Importing `_hosting_path` (transitively, via `_hosting_fakes`) is what makes
`superplane_hosting` importable; see `_hosting_path.py` for why the package sits one level
inside `hosting/`.
"""

from __future__ import annotations

import pytest
from _hosting_fakes import (
    RAISED_AT,
    RECEIPT_HANDLE,
    SESSION_ID,
    CallLog,
    RecordingHandoff,
    RecordingInbox,
    RecordingStore,
)
from superplane_hosting import AlertKind, AlertRecord, SessionOutcome


@pytest.fixture
def log() -> CallLog:
    return CallLog()


@pytest.fixture
def store(log: CallLog) -> RecordingStore:
    return RecordingStore(log=log)


@pytest.fixture
def handoff(log: CallLog) -> RecordingHandoff:
    return RecordingHandoff(log=log)


@pytest.fixture
def inbox(log: CallLog) -> RecordingInbox:
    return RecordingInbox(log=log)


@pytest.fixture
def budget_alert() -> AlertRecord:
    """A budget alert — one of the two kinds acceptance 3 names."""
    return AlertRecord(
        kind=AlertKind.BUDGET,
        session_id=SESSION_ID,
        summary="Workspace budget threshold reached.",
        raised_at=RAISED_AT,
    )


@pytest.fixture
def outcome() -> SessionOutcome:
    """A finished session with no alerts."""
    return SessionOutcome(
        session_id=SESSION_ID,
        receipt_handle=RECEIPT_HANDLE,
        result={"status": "ok"},
    )


@pytest.fixture
def outcome_with_alert(budget_alert: AlertRecord) -> SessionOutcome:
    """A finished session that raised an operator alert."""
    return SessionOutcome(
        session_id=SESSION_ID,
        receipt_handle=RECEIPT_HANDLE,
        result={"status": "ok"},
        alerts=(budget_alert,),
    )
