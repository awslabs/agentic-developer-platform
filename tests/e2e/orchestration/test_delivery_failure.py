"""Non-live full-report failures remain inspectable after early interruption."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios import delivery, audit
from tests.e2e.orchestration.scenarios.definitions import CRITERIA
from tests.e2e.orchestration.scenarios.http import Unsupported


@pytest.mark.parametrize(
    "failure",
    [
        Unsupported("credential expired"),
        KeyboardInterrupt(),
        ValueError("runtime changed"),
    ],
)
def test_early_failure_retains_complete_nonpassing_report(
    valid_config, monkeypatch, failure
):
    inventory = Inventory.create(
        valid_config.artifact_directory, "q-failure0123456789", valid_config.environment
    )
    manifest = NS(
        planned_gates=["release", "refusal"], model_dump=lambda **_: {"offline": True}
    )
    monkeypatch.setattr(delivery, "load_manifest", lambda _: (manifest, "a" * 64))
    monkeypatch.setattr(
        audit, "collect", Mock(side_effect=Unsupported("audit unavailable"))
    )
    session = NS(
        live=False,
        interventions=[],
        start=Mock(side_effect=failure),
        versions=Mock(side_effect=ValueError("unverified code")),
    )
    report = delivery.execute(
        valid_config, inventory, {}, session_factory=lambda *_: session
    )
    assert {r.id for r in report.results} == {c.id for c in CRITERIA}
    assert any(r.status != "PASS" for r in report.results)
    assert report.versions["harness"] == "unverified"
    assert report.spend_usd is None and not report.interventions_complete
    assert list((inventory.path.parent / "evidence").glob("*.json"))


def test_evidence_resumption_appends_without_overwriting(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory,
        "q-evidence0123456789",
        valid_config.environment,
    )
    first = delivery.Evidence(inventory).save("one", {"original": True}, "offline")
    second = delivery.Evidence(inventory).save("two", {"next": True}, "offline")
    assert first.path != second.path
    assert (inventory.path.parent / first.path).exists()
    assert (inventory.path.parent / second.path).exists()
