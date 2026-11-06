"""Rows that predate the recorded release pins learn them from the host once (#8223 cause 34).

Runs in the controller state-machine process group (dev-box-ci.yml); reuses ``origin_db``.
"""

import sys
from pathlib import Path

import pytest
from src.shared.models.dev_box import DevBoxInstance

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.identity import generation_pins, resolve_generation_pins  # noqa: E402
from controller.launch import LaunchTemplate  # noqa: E402
from test_controller_dashboard_origin import origin_db  # noqa: E402,F401  (fixture: box + instance + setup op)

RECEIPT_PINS = ("crew-old", "b" * 40, "c" * 64)


class RecordingSsm:
    def __init__(self, output="crew-profile-pins " + " ".join(RECEIPT_PINS) + "\n", status="Success"):
        self.output, self.status, self.sent = output, status, []

    def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **kwargs):
        return {"Status": self.status, "StandardOutputContent": self.output, "StandardErrorContent": ""}


def _current_release():
    return LaunchTemplate(template_id="lt-1", version="12", region="us-east-1", image_id="ami", revision="r1",
                          profile_revision="crew-new", source_revision="s" * 40, crew_bootstrap="user-data",
                          crew_bundle_sha256="d" * 64, crew_source_revision="t" * 40)


@pytest.mark.asyncio
async def test_a_row_without_pins_learns_them_from_the_host_receipt_once(origin_db):  # noqa: F811
    ssm = RecordingSsm()
    async with origin_db() as db:
        instance = await db.get(DevBoxInstance, "inst")
        pins = await resolve_generation_pins(db, ssm, instance, _current_release())
    assert (pins.profile_revision, pins.source_revision, pins.crew_bundle_sha256) == RECEIPT_PINS
    assert pins.crew_bootstrap == "user-data" and pins.image_id is None
    assert len(ssm.sent) == 1 and "crew-bootstrap.json" in ssm.sent[0]["Parameters"]["commands"][0]
    async with origin_db() as db:
        instance = await db.get(DevBoxInstance, "inst")
        assert (instance.profile_revision, instance.source_revision, instance.crew_bundle_sha256) == RECEIPT_PINS
        # Pinned now: no second host round-trip, and the current release no longer matters.
        again = await resolve_generation_pins(db, ssm, instance, _current_release())
    assert again == generation_pins(instance, _current_release()) and len(ssm.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output, status", [("garbage\n", "Success"), ("", "Failed"), ("crew-profile-pins a b\n", "Success")])
async def test_a_refused_or_malformed_discovery_leaves_the_row_alone_and_uses_the_release(origin_db, output, status):  # noqa: F811
    ssm = RecordingSsm(output=output, status=status)
    async with origin_db() as db:
        instance = await db.get(DevBoxInstance, "inst")
        pins = await resolve_generation_pins(db, ssm, instance, _current_release())
        assert pins.profile_revision == "crew-new" and pins.crew_bundle_sha256 == "d" * 64
        await db.refresh(instance)
        assert instance.profile_revision is None


@pytest.mark.asyncio
async def test_an_image_mode_release_never_asks_the_host(origin_db):  # noqa: F811
    ssm = RecordingSsm()
    release = LaunchTemplate(template_id="lt-1", version="12", region="us-east-1", image_id="ami", revision="r1",
                             profile_revision="crew-img", source_revision="s" * 40)
    async with origin_db() as db:
        instance = await db.get(DevBoxInstance, "inst")
        pins = await resolve_generation_pins(db, ssm, instance, release)
    assert pins.profile_revision == "crew-img" and pins.crew_bootstrap == "image" and ssm.sent == []
