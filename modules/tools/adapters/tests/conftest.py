import pytest

from tool_adapters.config import AdapterConfiguration


@pytest.fixture
def release():
    return {
        "contract_version": "1.0",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "group": "tool-connectors",
        "group_generation": 1,
        "backend_generation": 1,
    }


@pytest.fixture
def configuration(release):
    return AdapterConfiguration.model_validate({"schema_version": "1.0", "mode": "foundation", "release": release})


@pytest.fixture
def binding(release):
    return {"operation_id": "operation-1", "receipt_id": "receipt-1", "payload_sha256": "c" * 64, "release": release}
