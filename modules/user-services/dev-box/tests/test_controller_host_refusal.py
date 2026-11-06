"""A refused host readiness is named on the instance row, once per class, and cleared on success.

dev 2026-10-20 (first real same-generation stop/start): the host refused every resume for twelve
minutes with ``Crew host bootstrap refused: Error`` while the owner's page said only "lease
fenced". The controller now records ``host_refused:<Class>`` (the innermost class the host
named) on the generation's instance and one ``DevBoxEvent`` per class; the desktop read exposes it
as ``host_readiness_refusal`` and the readiness success path clears it.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from src.shared.models.dev_box import DevBox, DevBoxEvent, DevBoxInstance

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller.identity import host_refusal_class, record_host_refusal  # noqa: E402
from test_controller_dashboard_origin import origin_db  # noqa: E402,F401  (fixture: box + instance + setup op)


@pytest.mark.parametrize(
    "stderr, status, expected",
    [
        ("crew-profile-ok\nCrew host bootstrap refused: Error: Crew host resume failed (InvalidCheckpoint: untrusted Crew host directory)\n",
         "Failed", "host_refused:InvalidCheckpoint"),
        ("Crew host bootstrap refused: CrewHomeUnknownProvenanceError: Crew home has unknown provenance: Crew provisioning marker missing\n",
         "Failed", "host_refused:CrewHomeUnknownProvenanceError"),
        ("Crew host bootstrap refused: Error\n", "Failed", "host_refused:Error"),
        ("Crew host profile refused: CrewProfileMismatch: expected=crew-r2 got=crew-r1\n", "Failed",
         "host_refused:CrewProfileMismatch"),
        ("", "TimedOut", "host_refused:TimedOut"),
        ("", "Success", "host_refused:unexpected_output"),
        ("refused: " + "A" * 200, "Failed", ("host_refused:" + "A" * 200)[:64]),
    ],
)
def test_the_innermost_named_class_is_recorded_and_bounded(stderr, status, expected):
    assert host_refusal_class(stderr, status) == expected
    assert len(host_refusal_class(stderr, status)) <= 64


@pytest.mark.asyncio
async def test_a_refusal_is_recorded_once_per_class_and_cleared_by_readiness(origin_db):  # noqa: F811
    stderr = "Crew host bootstrap refused: Error: Crew host resume failed (InvalidCheckpoint: untrusted Crew host directory)"
    async with origin_db() as db:
        box = await db.get(DevBox, "box")
        instance = await db.get(DevBoxInstance, "inst")
        for _ in range(3):
            assert await record_host_refusal(db, box, instance, stderr, "Failed") == "host_refused:InvalidCheckpoint"
        assert await record_host_refusal(db, box, instance, "Crew host bootstrap refused: Error", "Failed") == "host_refused:Error"
        events = (await db.execute(select(DevBoxEvent.event_type, DevBoxEvent.operation_id))).all()
    assert [tuple(row) for row in events] == [("host_refused:InvalidCheckpoint", "setup"), ("host_refused:Error", "setup")]
    async with origin_db() as db:
        instance = await db.get(DevBoxInstance, "inst")
        assert instance.error_class == "host_refused:Error"
        # The success path (identity.reconcile_host_readiness) clears the class with crew_ready.
        instance.readiness_class = "crew_ready"
        instance.error_class = None
        await db.commit()
    async with origin_db() as db:
        instance = await db.get(DevBoxInstance, "inst")
        assert instance.error_class is None and instance.readiness_class == "crew_ready"
