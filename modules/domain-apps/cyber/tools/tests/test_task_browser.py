import base64
import hashlib
import uuid
from types import SimpleNamespace

from cyber_tools.task_browser import TaskBrowser
from test_service_boundary import authorization, attempt


class Authority:
    def __init__(self):
        self.verified = authorization()
        self.artifacts = []
        self.calls = []

    def authorize(self, **kwargs):
        self.calls.append(kwargs)
        return self.verified

    def put_run_artifact(self, **kwargs):
        self.artifacts.append(kwargs)
        return SimpleNamespace(
            artifact_id="art_" + str(uuid.uuid4()),
            content_type=kwargs["content_type"],
            content_sha256=hashlib.sha256(kwargs["content"]).hexdigest(),
        )


def fixture():
    calls = []

    def request(operation, payload):
        calls.append((operation, payload))
        if operation == "start":
            return {
                "session_token": "native-private-token",
                "session_open": True,
                "view_id": "view1",
                "observations": [
                    {
                        "id": "obs-001",
                        "visible_text": "A real observation",
                        "forms": [{"action": "/login"}],
                    }
                ],
            }
        if operation == "step":
            return {"session_open": True, "view_id": "view2", "observations": []}
        return {"session_open": False, "cleanup_status": "stopped"}

    tool = TaskBrowser(None, request=request)
    tool.authority = Authority()

    def invoke(operation, payload):
        return tool.invoke(
            {
                "schema_version": "1.0",
                "attempt": attempt(tool.authority.verified),
                "operation_id": str(uuid.uuid4()),
                "operation": operation,
                "payload": payload,
            }
        )

    return tool, calls, invoke


def test_native_browser_scope_profile_navigation_and_cleanup():
    tool, calls, invoke = fixture()
    start = invoke(
        "browser_start",
        {
            "url": "https://example.com",
            "profile": "mobile",
            "scope": "observed_external",
        },
    )
    assert start["operation_status"] == "confirmed", start
    assert calls[0] == (
        "start",
        {
            "url": "https://example.com",
            "profile": "mobile",
            "scope": "observed_external",
        },
    )
    assert "native-private-token" not in str(start)
    sid = start["result"]["session_id"]
    inspected = invoke("browser_inspect", {"session_id": sid, "section": "forms"})
    assert "/login" in inspected["result"]["text"]
    result = invoke(
        "browser_step",
        {
            "session_id": sid,
            "view_id": "view1",
            "action": "navigate",
            "url": "https://reference.example/",
        },
    )
    assert result["result"]["view_id"] == "view2"
    assert calls[-1][1]["session_token"] == "native-private-token"
    assert invoke("cancel_jobs", {})["result"]["pending_jobs"] == []
    assert calls[-1][0] == "close"
    assert (
        invoke("browser_start", {"url": "https://example.com"})["operation_status"]
        == "rejected"
    )


def test_owned_session_and_uncertain_action_are_never_replayed():
    tool, calls, invoke = fixture()
    start = invoke("browser_start", {"url": "https://example.com"})
    sid = start["result"]["session_id"]
    assert (
        invoke("browser_start", {"url": "https://example.com"})["result"]
        == start["result"]
    )
    assert len(calls) == 1
    bad = invoke(
        "browser_step", {"session_id": "f" * 64, "view_id": "view1", "action": "scroll"}
    )
    assert bad["operation_status"] == "rejected"

    def failure(*args):
        calls.append(args)
        raise TimeoutError()

    tool.request = failure
    payload = {"session_id": sid, "view_id": "view1", "action": "scroll"}
    assert invoke("browser_step", payload)["operation_status"] == "unknown"
    assert invoke("browser_step", payload)["operation_status"] == "unknown"
    assert len(calls) == 2
    original = tool.authority.verified
    tool.authority.verified = authorization()
    assert (
        invoke("browser_close", {"session_id": sid})["operation_status"] == "rejected"
    )
    tool.authority.verified = original


def test_screenshot_preview_is_bounded_and_inert():
    from PIL import Image
    import io

    tool, calls, invoke = fixture()
    sid = invoke("browser_start", {"url": "https://example.com"})["result"][
        "session_id"
    ]
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")
    tool.sessions[sid]["packet"]["observations"][0]["screenshot_base64"] = (
        base64.b64encode(buf.getvalue()).decode()
    )
    result = invoke("browser_inspect", {"session_id": sid, "section": "screenshot"})
    image = result["result"]["image"]
    assert image["media_type"] == "image/jpeg"
    assert len(base64.b64decode(image["data"])) <= 12000


def test_inspection_uses_native_network_and_top_level_choices():
    import json

    tool, _, invoke = fixture()
    sid = invoke("browser_start", {"url": "https://example.com"})["result"][
        "session_id"
    ]
    packet = tool.sessions[sid]["packet"]
    packet["observations"][0]["network_requests"] = [
        {"url": "https://example.com/", "status": 200}
    ]
    packet["choices"] = [{"candidate_id": "link-1", "url": "https://iana.org"}]
    network = invoke("browser_inspect", {"session_id": sid, "section": "network"})[
        "result"
    ]
    assert json.loads(network["text"])[0][0]["status"] == 200
    choices = invoke("browser_inspect", {"session_id": sid, "section": "choices"})[
        "result"
    ]
    assert json.loads(choices["text"])[0]["candidate_id"] == "link-1"
    missing = invoke("browser_inspect", {"session_id": sid, "section": "frames"})[
        "result"
    ]
    assert missing["coverage"] == "unavailable"
