"""The task route must not be able to break the existing ingress paths.

T2-AC04. The task handler is reached through one branch that imports its module
only when that branch is taken. These tests hold that arrangement in place,
because the failure they prevent is severe and silent: a bad task deployment
taking down GitHub webhook ingestion for every tenant.

Three situations are covered — task admission enabled, disabled, and the task
module failing to import at all — and in all three the GitHub, EventBridge and
agent-trigger paths must behave exactly as they do today.
"""

import builtins
import contextlib
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/t.fifo")
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _task_event():
    return {
        "resource": "/v1/tasks",
        "httpMethod": "POST",
        "headers": {
            "Authorization": "Bearer caller-token",
            "Idempotency-Key": "incident-483",
        },
        "body": json.dumps(
            {
                "schema_version": "1.0",
                "persona": "agent-task-investigator",
                "instructions": "Investigate the checkout-api 503 spike.",
            }
        ),
        "isBase64Encoded": False,
        "requestContext": {"requestId": "apigw-request-id"},
    }


def _eventbridge_event():
    return {
        "source": "adp.service",
        "detail-type": "AgentTaskRequested",
        "detail": {"service_identity": "svc-1"},
    }


def _agent_trigger_event():
    return {
        "resource": "/agent/trigger",
        "httpMethod": "POST",
        "headers": {},
        "body": json.dumps({"persona": "developer", "issue": 1}),
        "requestContext": {"requestId": "r"},
    }


def _github_event(payload: dict, event_type: str = "issue_comment"):
    body = json.dumps(payload)
    signature = (
        "sha256="
        + hmac.new(b"test-secret-123", body.encode(), hashlib.sha256).hexdigest()
    )
    return {
        "resource": "/github",
        "httpMethod": "POST",
        "headers": {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": event_type,
        },
        "body": body,
        "isBase64Encoded": False,
        "requestContext": {"requestId": "r"},
    }


# --- the task branch routes only its own resource -----------------------


def test_task_resource_reaches_the_task_handler():
    from handler import handler

    with patch("task_api.handler.handle_task_submit") as task_handler:
        task_handler.return_value = {"statusCode": 202, "body": "{}"}
        response = handler(_task_event(), None)
    assert response["statusCode"] == 202
    task_handler.assert_called_once()


def test_eventbridge_events_still_route_to_the_eventbridge_handler():
    from handler import handler

    with patch("eventbridge.handler.handle_eventbridge") as eventbridge:
        eventbridge.return_value = {"ok": True}
        with patch("task_api.handler.handle_task_submit") as task_handler:
            assert handler(_eventbridge_event(), None) == {"ok": True}
    eventbridge.assert_called_once()
    task_handler.assert_not_called()


def test_agent_trigger_still_routes_to_the_agent_trigger_handler():
    from handler import handler

    with patch("agent_trigger.handle_agent_trigger") as trigger:
        trigger.return_value = {"statusCode": 200, "body": "{}"}
        with patch("task_api.handler.handle_task_submit") as task_handler:
            assert handler(_agent_trigger_event(), None)["statusCode"] == 200
    trigger.assert_called_once()
    task_handler.assert_not_called()


def test_github_webhook_signature_rejection_is_unchanged():
    """The GitHub path must still refuse a bad signature, not reach tasks."""
    from handler import handler

    event = _github_event({"action": "created"})
    event["headers"]["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    with patch("task_api.handler.handle_task_submit") as task_handler:
        response = handler(event, None)
    assert response["statusCode"] == 401
    task_handler.assert_not_called()


def test_github_webhook_requires_its_event_header_as_before():
    from handler import handler

    event = _github_event({"action": "created"})
    del event["headers"]["X-GitHub-Event"]
    with patch("task_api.handler.handle_task_submit") as task_handler:
        response = handler(event, None)
    assert response["statusCode"] == 400
    task_handler.assert_not_called()


def test_github_installation_noop_path_is_unchanged():
    from handler import handler

    event = _github_event(
        {"action": "created", "installation": {"id": 42}}, event_type="installation"
    )
    with patch("task_api.handler.handle_task_submit") as task_handler:
        response = handler(event, None)
    assert response["statusCode"] == 200
    task_handler.assert_not_called()


@pytest.mark.parametrize(
    "resource",
    ["/github", "/agent/trigger", "", "/v1/tasks/extra", "/V1/TASKS", "/v1/task"],
)
def test_only_the_exact_task_resource_takes_the_task_branch(resource):
    """A near-miss path must not be routed to task submission."""
    from handler import handler

    event = _task_event()
    event["resource"] = resource
    # These resources fall through to the GitHub path, which may refuse the
    # task-shaped body however it likes. The only claim under test is that the
    # task handler is never reached.
    with patch("task_api.handler.handle_task_submit") as task_handler, (
        contextlib.suppress(Exception)
    ):
        handler(event, None)
    task_handler.assert_not_called()


# --- the existing paths survive a broken task module --------------------


@contextlib.contextmanager
def _task_modules_unloaded():
    """Unload the task modules, then restore the *same* module objects.

    Restoring the originals rather than letting them be re-imported matters:
    other test modules hold direct references to these objects, and a second
    copy in ``sys.modules`` would make ``patch()`` target one object while the
    caller uses the other.
    """
    saved = {n: m for n, m in sys.modules.items() if n.startswith("task_api")}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n.startswith("task_api")]:
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.fixture
def task_import_fails():
    """Make importing the task module raise, as a bad deployment would."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("task_api"):
            raise ImportError("simulated task module failure")
        return real_import(name, *args, **kwargs)

    with _task_modules_unloaded(), patch.object(
        builtins, "__import__", side_effect=fake_import
    ):
        yield


def test_eventbridge_path_survives_a_task_import_failure(task_import_fails):
    from handler import handler

    with patch("eventbridge.handler.handle_eventbridge", return_value={"ok": True}):
        assert handler(_eventbridge_event(), None) == {"ok": True}


def test_agent_trigger_path_survives_a_task_import_failure(task_import_fails):
    from handler import handler

    with patch(
        "agent_trigger.handle_agent_trigger",
        return_value={"statusCode": 200, "body": "{}"},
    ):
        assert handler(_agent_trigger_event(), None)["statusCode"] == 200


def test_github_path_survives_a_task_import_failure(task_import_fails):
    from handler import handler

    event = _github_event({"action": "created"})
    event["headers"]["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert handler(event, None)["statusCode"] == 401


def test_a_task_import_failure_surfaces_only_on_the_task_route(task_import_fails):
    """The failure is not swallowed — it just cannot reach other routes."""
    from handler import handler

    with pytest.raises(ImportError):
        handler(_task_event(), None)


# --- the existing paths are unaffected by the rollout flag --------------


@pytest.mark.parametrize("flag", [None, "false", "true"])
def test_existing_paths_behave_identically_with_admission_off_or_on(monkeypatch, flag):
    from handler import handler

    if flag is None:
        monkeypatch.delenv("ADP_TASK_API_ADMISSION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ADP_TASK_API_ADMISSION_ENABLED", flag)

    with patch("eventbridge.handler.handle_eventbridge", return_value={"ok": True}):
        assert handler(_eventbridge_event(), None) == {"ok": True}
    with patch(
        "agent_trigger.handle_agent_trigger",
        return_value={"statusCode": 200, "body": "{}"},
    ):
        assert handler(_agent_trigger_event(), None)["statusCode"] == 200
    event = _github_event({"action": "created"})
    event["headers"]["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert handler(event, None)["statusCode"] == 401


def test_task_submission_is_refused_while_admission_is_disabled(monkeypatch):
    """With the flag off no task is accepted and nothing is queued."""
    from handler import handler

    monkeypatch.delenv("ADP_TASK_API_ADMISSION_ENABLED", raising=False)
    with patch("task_api.admit_client.admit") as admit:
        response = handler(_task_event(), None)
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["code"] == "prerequisite_unavailable"
    admit.assert_not_called()


def test_the_task_module_is_not_imported_unless_the_task_route_is_used():
    """Cold-start cost and blast radius both depend on this staying true."""
    from handler import handler

    with _task_modules_unloaded():
        with patch("eventbridge.handler.handle_eventbridge", return_value={"ok": True}):
            handler(_eventbridge_event(), None)
        assert not [m for m in sys.modules if m.startswith("task_api")]
