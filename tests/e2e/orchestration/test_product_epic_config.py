"""Offline configuration contract for Product-bound planning epics."""

import pytest

from tests.e2e.orchestration.config import ConfigError, load_config

PRODUCT_EPIC = {
    "product_id": "00000000-0000-4000-8000-000000000001",
    "epic": {
        "target": {
            "repository": "example/planning",
            "connection_ref": "planning-connection",
            "repository_id": 101,
            "installation_id": 301,
        },
        "issue_number": 5,
    },
    "stories": [
        {
            "repository": "example/backend",
            "connection_ref": "backend-connection",
            "repository_id": 102,
            "installation_id": 301,
        },
        {
            "repository": "example/frontend",
            "connection_ref": "frontend-connection",
            "repository_id": 103,
            "installation_id": 301,
        },
    ],
}


def test_product_epic_targets_are_kept_distinct(write_config):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    assert config.product_epic.product_id == PRODUCT_EPIC["product_id"]
    assert config.product_epic.epic.repository == "example/planning"
    assert config.product_epic.epic_issue_number == 5
    assert [target.repository for target in config.product_epic.stories] == [
        "example/backend",
        "example/frontend",
    ]


def test_legacy_config_keeps_product_absent(valid_config):
    assert valid_config.product_epic is None


@pytest.mark.parametrize("field", ["repository", "connection_ref"])
def test_planning_target_must_differ_from_story(write_config, field):
    target = {
        **PRODUCT_EPIC["epic"]["target"],
        field: PRODUCT_EPIC["stories"][0][field],
    }
    epic = {**PRODUCT_EPIC["epic"], "target": target}
    with pytest.raises(ConfigError, match="must be distinct"):
        load_config(write_config({"product_epic": {**PRODUCT_EPIC, "epic": epic}}))


@pytest.mark.parametrize("issue_number", [None, 0, -1, True, "5"])
def test_invalid_epic_number_is_refused(write_config, issue_number):
    epic = {**PRODUCT_EPIC["epic"], "issue_number": issue_number}
    with pytest.raises(ConfigError, match="issue_number must be a positive integer"):
        load_config(write_config({"product_epic": {**PRODUCT_EPIC, "epic": epic}}))


def test_required_product_epic_missing_for_selected_scenario(write_config):
    with pytest.raises(ConfigError, match="product_epic is required"):
        load_config(write_config({"scenarios": ["product-epic-delivery"]}))


def test_incomplete_story_targets_are_refused(write_config):
    with pytest.raises(ConfigError, match="exactly two targets"):
        load_config(write_config({"product_epic": {**PRODUCT_EPIC, "stories": []}}))
