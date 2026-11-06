"""The Product/epic case uses the existing runner modes without paid transport."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.config import ConfigError, TargetVerification, load_config
from tests.e2e.orchestration.scenarios import REGISTRY
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC


def qualified_config(write_config):
    return load_config(
        write_config(
            {
                "product_epic": PRODUCT_EPIC,
                "scenarios": ["product-epic-delivery"],
                "bounds": {"max_resources": 30},
            }
        )
    )


def test_product_selector_uses_existing_registry_and_legacy_stays_default(
    write_config, valid_config
):
    assert list(REGISTRY) == [
        "autonomous-delivery",
        "product-epic-delivery",
        "product-epic-recovery",
        "legacy-attribution",
    ]
    assert list(runner.load_scenario_adapters(valid_config)) == ["autonomous-delivery"]
    configured = qualified_config(write_config)
    assert list(runner.load_scenario_adapters(configured)) == ["product-epic-delivery"]
    assert list(runner.load_scenario_adapters(replace(configured, scenarios=()))) == [
        "product-epic-delivery"
    ]


@pytest.mark.parametrize(
    "scenarios",
    [["autonomous-delivery"], ["autonomous-delivery", "product-epic-delivery"]],
)
def test_product_case_refuses_legacy_or_duplicate_selection(write_config, scenarios):
    with pytest.raises(ConfigError, match="avoid duplicate fixtures"):
        load_config(
            write_config({"product_epic": PRODUCT_EPIC, "scenarios": scenarios})
        )


def test_missing_product_cannot_select_product_case(write_config):
    with pytest.raises(ConfigError, match="product_epic is required"):
        load_config(write_config({"scenarios": ["product-epic-delivery"]}))


def test_product_selection_preflights_runs_resumes_and_cleans_without_a_second_runner(
    write_config, monkeypatch
):
    from tests.e2e.orchestration.scenarios import delivery

    config = qualified_config(write_config)
    target = TargetVerification(
        verified=True,
        reason="verified offline transport",
        expected_account_id=config.expected_account_id,
        expected_org=config.expected_org,
    )
    monkeypatch.setattr(runner, "_caller_identity", lambda: (None, "offline stub"))
    monkeypatch.setattr(runner, "verify_target", lambda *args: target)
    monkeypatch.setattr(runner, "verified_target", lambda *args: target)
    provider_configs = []

    def transport_providers(self, config):
        provider_configs.append(config)
        return ()

    monkeypatch.setattr(
        type(REGISTRY["product-epic-delivery"]),
        "fixture_providers",
        transport_providers,
    )
    received = []

    def transport_stub(config, inventory, providers):
        received.append((config, inventory.qualification_id, providers))
        return None

    monkeypatch.setattr(delivery, "execute", transport_stub)
    monkeypatch.setattr(
        runner,
        "load_connection_resolver",
        lambda _: SimpleNamespace(verify_product_epic=lambda: None),
    )
    preview = runner.preflight(config)
    assert preview.report["scenarios"] == ["product-epic-delivery"]
    assert preview.report["product_epic"] == {
        "product_id": PRODUCT_EPIC["product_id"],
        "planning_repository": PRODUCT_EPIC["epic"]["target"]["repository"],
        "planning_issue_number": PRODUCT_EPIC["epic"]["issue_number"],
        "story_repositories": [
            target["repository"] for target in PRODUCT_EPIC["stories"]
        ],
    }
    assert preview.report["mutations"] == []
    assert preview.report["planned_resource_units"] <= config.max_resources
    assert list(config.artifact_directory.iterdir()) == []

    result = runner.run(config)
    assert result.status == runner.STATUS_INCOMPLETE, result.report
    assert result.report["scenarios_executed"] == 1
    assert result.report["attempts"] == 1
    assert received == [(config, result.report["qualification_id"], {})]
    assert (
        runner.resume_qualification(config, result.report["qualification_id"]).status
        == runner.STATUS_READY
    )
    assert (
        runner.cleanup_qualification(config, result.report["qualification_id"]).status
        == runner.STATUS_PASS
    )
    assert provider_configs == [config, config, config]
