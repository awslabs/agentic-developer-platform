"""contract_errors names the cause of every anonymous 503 in the gateway log (#7983)."""

import logging

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.agentauth.chat_capability import ChatAuthorizationUnavailableError
from src.agentauth.chat_data_routes import contract_errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_class", "expected_detail"),
    [
        (ChatAuthorizationUnavailableError("chat result unavailable"), "ChatAuthorizationUnavailableError", "chat result unavailable"),
        (
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized: dynamodb:ConditionCheckItem"}}, "TransactWriteItems"),
            "ClientError",
            "AccessDeniedException",
        ),
    ],
)
async def test_anonymous_503_logs_the_exception_class_and_message(caplog, error, expected_class, expected_detail):
    @contract_errors
    async def record_turn_result():
        raise error

    with caplog.at_level(logging.WARNING, logger="src.agentauth.chat_data_routes"):
        with pytest.raises(HTTPException) as raised:
            await record_turn_result()

    assert raised.value.status_code == 503
    assert raised.value.detail == {"error": "chat_authority_unavailable"}
    records = [r for r in caplog.records if r.getMessage() == "Chat data route unavailable"]
    assert len(records) == 1
    assert records[0].route == "record_turn_result"
    assert records[0].error_class == expected_class
    assert expected_detail in records[0].error_detail


@pytest.mark.asyncio
async def test_refusals_stay_404_but_name_the_check_in_the_log(caplog):
    """#7990: the body stays a non-enumerating 404; the operator log says which check refused."""
    from src.agentauth.chat_capability import ChatAuthorizationRefusedError

    @contract_errors
    async def admit():
        raise ChatAuthorizationRefusedError("chat crew run refused")

    with caplog.at_level(logging.WARNING, logger="src.agentauth.chat_data_routes"):
        with pytest.raises(HTTPException) as raised:
            await admit()
    assert raised.value.status_code == 404
    assert raised.value.detail == {"error": "chat_scope_refused"}
    records = [r for r in caplog.records if r.getMessage() == "Chat data route refused"]
    assert len(records) == 1
    assert records[0].route == "admit"
    assert records[0].error_class == "ChatAuthorizationRefusedError"
    assert records[0].error_detail == "chat crew run refused"
    assert not [r for r in caplog.records if r.getMessage() == "Chat data route unavailable"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_body", "expected_class"),
    [
        ("history", "chat_history_conflict", "ChatHistoryConflictError"),
        ("removal", "chat_sandbox_removal_unconfirmed", "ChatSandboxRemovalUnconfirmedError"),
    ],
)
async def test_conflicts_stay_409_but_name_the_reason_in_the_log(caplog, error, expected_body, expected_class):
    """#8061: the supervisor retries every 409 for its whole window; the gateway log says why it keeps conflicting."""
    from src.agentauth.chat_history_write import ChatHistoryConflictError
    from src.agentauth.chat_teardown import ChatSandboxRemovalUnconfirmedError

    raised_error = (
        ChatHistoryConflictError("chat reserved pod exit not confirmed")
        if error == "history"
        else ChatSandboxRemovalUnconfirmedError("chat sandbox removal not confirmed")
    )

    @contract_errors
    async def resume_chat():
        raise raised_error

    with caplog.at_level(logging.WARNING, logger="src.agentauth.chat_data_routes"):
        with pytest.raises(HTTPException) as raised:
            await resume_chat()
    assert raised.value.status_code == 409
    assert raised.value.detail == {"error": expected_body}
    records = [r for r in caplog.records if r.getMessage() == "Chat data route conflict"]
    assert len(records) == 1
    assert records[0].route == "resume_chat"
    assert records[0].error_class == expected_class
    assert records[0].error_detail == str(raised_error)
    assert not [r for r in caplog.records if r.getMessage() in {"Chat data route refused", "Chat data route unavailable"}]
