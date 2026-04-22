"""
Latency recording harness for E2E tests.

Captures timestamped events per test (send, first_frame, first_heartbeat,
first_chunk, terminal_frame, ...) and exposes deltas. On test completion,
writes JSON to /tmp/e2e-latency-<test>.json. A session-scoped pytest plugin
aggregates results and prints a summary table.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

OUTPUT_DIR = Path(os.environ.get("E2E_LATENCY_DIR", "/tmp"))


@dataclass
class LatencyRecorder:
    """Captures timestamped events and computes deltas for a single test."""

    test_name: str
    _marks: dict[str, float] = field(default_factory=dict)
    _notes: dict[str, str] = field(default_factory=dict)

    def mark(self, event: str) -> None:
        """Record a named timestamp. First call wins (no overwrite)."""
        if event not in self._marks:
            self._marks[event] = time.monotonic()

    def note(self, key: str, value: str) -> None:
        """Attach a note (e.g., chunk_total=2)."""
        self._notes[key] = value

    def delta(self, start: str, end: str) -> float | None:
        """Return seconds between two marks, or None if either is missing."""
        s = self._marks.get(start)
        e = self._marks.get(end)
        if s is None or e is None:
            return None
        return round(e - s, 3)

    @property
    def deltas(self) -> dict[str, float | None]:
        """Common deltas for the summary table."""
        return {
            "send_to_first_frame": self.delta("send", "first_frame"),
            "send_to_first_heartbeat": self.delta("send", "first_heartbeat"),
            "send_to_first_chunk": self.delta("send", "first_chunk"),
            "send_to_terminal": self.delta("send", "terminal_frame"),
            "first_frame_to_terminal": self.delta("first_frame", "terminal_frame"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "test": self.test_name,
            "marks": {k: round(v, 6) for k, v in self._marks.items()},
            "deltas": {k: v for k, v in self.deltas.items() if v is not None},
            "notes": self._notes,
        }

    def write(self) -> Path:
        """Write latency JSON for this test to /tmp."""
        safe = self.test_name.replace("/", "_").replace("::", "__")
        path = OUTPUT_DIR / f"e2e-latency-{safe}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


# ---------------------------------------------------------------------------
# Pytest plugin — session-scoped aggregation + summary printing
# ---------------------------------------------------------------------------

_ALL_RECORDERS: list[LatencyRecorder] = []


def get_recorder(test_name: str) -> LatencyRecorder:
    """Factory used by the fixture; registers the recorder for aggregation."""
    rec = LatencyRecorder(test_name=test_name)
    _ALL_RECORDERS.append(rec)
    return rec


def _format_delta(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:>8.1f} s"


def print_summary() -> None:
    """Pretty-print a summary table of all latency recordings."""
    if not _ALL_RECORDERS:
        return

    header = (
        f"{'test':<40} | {'send->first_frame':>17} | {'send->terminal':>14} | notes"
    )
    sep = "-" * len(header)
    lines = ["\n", "=" * 60, "E2E Latency Summary", "=" * 60, header, sep]

    for rec in _ALL_RECORDERS:
        d = rec.deltas
        notes_parts = []
        if d.get("send_to_first_heartbeat") is not None:
            notes_parts.append(f"first_hb={d['send_to_first_heartbeat']:.1f}s")
        for k, v in rec._notes.items():
            notes_parts.append(f"{k}={v}")
        notes_str = "; ".join(notes_parts) if notes_parts else ""

        short_name = rec.test_name.split("::")[-1] if "::" in rec.test_name else rec.test_name
        lines.append(
            f"{short_name:<40} | "
            f"{_format_delta(d.get('send_to_first_frame')):>17} | "
            f"{_format_delta(d.get('send_to_terminal')):>14} | "
            f"{notes_str}"
        )

    lines.append(sep)
    print("\n".join(lines))


def write_aggregate() -> Path | None:
    """Write all recorder data to a single aggregate JSON file."""
    if not _ALL_RECORDERS:
        return None
    path = OUTPUT_DIR / "e2e-latency-aggregate.json"
    data = [rec.to_dict() for rec in _ALL_RECORDERS]
    path.write_text(json.dumps(data, indent=2))
    return path


# ---------------------------------------------------------------------------
# Pytest hooks — auto-register via conftest
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):
    """Called at the end of the test session."""
    for rec in _ALL_RECORDERS:
        try:
            rec.write()
        except Exception:
            pass
    write_aggregate()
    print_summary()
