"""#9455: an attested host run is activated by the controller, not by a later owner-side call.

dev 2026-11-01 20:44–20:58 and 2026-11-02 06:19–06:31: after `start` the run stayed `provisioning`
(every host route 403) until the owner read `/crew/target`, which was the only path that called
`activate_for_host`. The readiness success path now activates the run itself.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.identity import activate_attested_run  # noqa: E402
from src.agentauth.chat_crew_run import CrewRunConflictError  # noqa: E402

BOX = SimpleNamespace(
    id="box-1",
    org_id="aws-e",
    user_id="owner-1",
    machine_generation=62,
    deployment_id="adp-dev-dev-box",
)
INSTANCE = SimpleNamespace(provider_instance_id="i-0123456789abcdef0")
RUN = "cdd8887a-ab27-4d6a-808d-ef3a28400575"


class FakeStore:
    def __init__(
        self,
        *,
        host_role_arn="arn:aws:iam::123456789012:role/adp-dev-box-guest-x",
        activation=None,
        current=None,
    ):
        self.host_role_arn = host_role_arn
        self.calls = []
        self._activation = activation
        self._current = current

    def activate_for_host(self, **kwargs):
        self.calls.append(kwargs)
        if self._activation is not None:
            raise self._activation

    def read_run(self, run_id):
        return self._current


@pytest.mark.asyncio
async def test_a_ready_host_run_is_activated_with_the_exact_binding():
    store = FakeStore()
    assert (
        await activate_attested_run(store, box=BOX, instance=INSTANCE, run_id=RUN)
        is True
    )
    assert len(store.calls) == 1
    call = store.calls[0]
    assert (
        call["run_id"] == RUN
        and call["tenant_id"] == "aws-e"
        and call["user_id"] == "owner-1"
    )
    assert call["session_id"] == "box-1" and call["lease_generation"] == 62
    assert (
        call["target_ref"] == "i-0123456789abcdef0"
        and call["deployment_id"] == "adp-dev-dev-box"
    )
    assert isinstance(call["now"], int) and call["now"] > 1_700_000_000


@pytest.mark.asyncio
async def test_a_run_another_path_already_activated_counts_as_success():
    store = FakeStore(
        activation=CrewRunConflictError("prepared Crew host run changed"),
        current={"status": "active"},
    )
    assert (
        await activate_attested_run(store, box=BOX, instance=INSTANCE, run_id=RUN)
        is True
    )


@pytest.mark.asyncio
async def test_a_refused_activation_is_reported_not_raised(caplog):
    store = FakeStore(
        activation=CrewRunConflictError("Crew host lease changed during activation"),
        current={"status": "provisioning"},
    )
    with caplog.at_level("WARNING"):
        assert (
            await activate_attested_run(store, box=BOX, instance=INSTANCE, run_id=RUN)
            is False
        )
    assert (
        "activation refused after readiness" in caplog.text and "box-1" in caplog.text
    )


@pytest.mark.asyncio
async def test_a_provider_failure_is_reported_not_raised(caplog):
    error = ClientError(
        {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "slow down",
            }
        },
        "TransactWriteItems",
    )
    store = FakeStore(activation=error)
    with caplog.at_level("WARNING"):
        assert (
            await activate_attested_run(store, box=BOX, instance=INSTANCE, run_id=RUN)
            is False
        )
    assert "activation failed after readiness" in caplog.text


@pytest.mark.asyncio
async def test_without_a_guest_role_or_run_nothing_is_attempted():
    assert (
        await activate_attested_run(None, box=BOX, instance=INSTANCE, run_id=RUN)
        is False
    )
    store = FakeStore(host_role_arn=None)
    assert (
        await activate_attested_run(store, box=BOX, instance=INSTANCE, run_id=RUN)
        is False
    )
    store = FakeStore()
    assert (
        await activate_attested_run(store, box=BOX, instance=INSTANCE, run_id=None)
        is False
    )
    assert store.calls == []
