from copy import deepcopy

import pytest

from modules.tools.platform.scripts.target_metadata import target_revision_values


def test_dynamic_revision_tracks_configuration_not_stale_cached_schema():
    target = {"targetId": "fixture", "updatedAt": "2026-01-02T00:00:00Z",
              "lastSynchronizedAt": "2026-01-01T00:00:00Z",
              "targetConfiguration": {"mcp": {"mcpServer": {"listingMode": "DYNAMIC"}}}}
    original = target_revision_values([target])
    without_sync = deepcopy(target)
    without_sync.pop("lastSynchronizedAt")
    assert target_revision_values([without_sync]) == original
    target["updatedAt"] = "2026-01-03T00:00:00Z"
    assert target_revision_values([target]) != original


@pytest.mark.parametrize("mode", [None, "DEFAULT"])
def test_cached_target_still_requires_synchronization(mode):
    target = {"targetId": "fixture", "updatedAt": "2026-01-02T00:00:00Z",
              "targetConfiguration": {"mcp": {"mcpServer": {"listingMode": mode}}}}
    with pytest.raises(ValueError):
        target_revision_values([target])


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-01-02T00:00:00"])
def test_dynamic_target_requires_valid_configuration_timestamp(timestamp):
    with pytest.raises(ValueError):
        target_revision_values([{"targetId": "fixture", "updatedAt": timestamp,
                                 "targetConfiguration": {"mcp": {"mcpServer": {"listingMode": "DYNAMIC"}}}}])
