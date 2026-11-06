import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/prepare-broker-state.py"
spec = importlib.util.spec_from_file_location("prepare_broker_state", SCRIPT)
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


@pytest.mark.parametrize("state_arn", ["", "oauth-secret"])
def test_unprepared_infrastructure_blocks_code_publication(state_arn):
    with pytest.raises(ValueError, match="Apply gateway infrastructure"):
        rollout.state_environment({"STATE_SIGNING_SECRET_ARN": state_arn, "GITHUB_CLIENT_SECRET_ARN": "oauth-secret"}, 100)


def test_migration_window_is_absolute_and_unrelated_environment_is_preserved():
    variables = {"STATE_SIGNING_SECRET_ARN": "dedicated-secret", "GITHUB_CLIENT_SECRET_ARN": "oauth-secret", "OTHER": "preserve"}
    first = rollout.state_environment(variables, 100)
    assert first["STATE_LEGACY_ACCEPT_UNTIL"] == "700"
    assert first["OTHER"] == "preserve"
    assert rollout.state_environment(first, 1000) == first
