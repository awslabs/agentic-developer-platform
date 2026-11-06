"""Native rollout probes must not perform database or provider work."""

from unittest.mock import Mock

import pytest

from l5_services.workers import devops_events, devops_readback


@pytest.mark.parametrize("worker", [devops_events, devops_readback])
def test_native_probe_returns_identity_before_loading_dependencies(worker, monkeypatch):
    loader = Mock(side_effect=AssertionError("probe loaded runtime dependencies"))
    monkeypatch.setattr(worker, "load_service", loader)
    assert worker.lambda_handler({"l5_runtime_probe": 1}, None) == {"service_id": "devops"}
    loader.assert_not_called()
