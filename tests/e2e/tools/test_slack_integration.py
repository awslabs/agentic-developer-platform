"""Synthetic read-only Slack fixture; no live provider or channel is contacted."""

import json
from uuid import uuid4

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.scenarios import FixtureUnavailable
from tests.e2e.tools.slack_integration import slack_ac01, slack_ac04
from tests.e2e.tools.test_runner import GATEWAY_IMAGE, IMAGE, SHA, FixtureClient


class LiveObserver:
    deployed_target = False

    def __init__(self, defect=None):
        self.defect = defect
        self.states = {}

    def state(self, attempt_id):
        return self.states.setdefault(attempt_id, {"calls": 0, "posts": 0, "messages": [],
                                                    "receipt": None, "deleted": False})

    def pricing(self, client_name, attempt_id):
        from tests.e2e.tools.test_client_matrix import matrix_fixture

        quote = matrix_fixture(client_name)["all_in_quote"]
        if client_name != "cli":
            quote["max_model_requests"] = 1 if self.defect == "underpriced_model" else 9
        return {"source": "independent-test-pricing", "max_sessions": 1,
                "ttl_seconds": 300, "cleanup_seconds": 600, "per_call_usd": "0.3",
                "max_cell_usd": "7" if self.defect == "overbudget" else "6",
                "all_in_quote": quote}

    def deployment(self, client_name, attempt_id):
        return {"source": "independent-release-observer", "gateway": SHA,
                "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE,
                "connector_revision": "connector-fixture-v1", "oauth_provider_revision": "oauth-fixture-v1"}

    def client_version(self, client_name, attempt_id):
        return {"source": "independent-client-install", "name": client_name,
                "version": "fixture-v1"}

    def client_invocation(self, client_name, attempt_id):
        return {"source": "independent-client-transcript", "name": client_name,
                "operations": ["slack___get_user", "slack___post_message"], "calls": 2}

    def expected(self, fixture_id, attempt_id):
        return {"source": "independent-oauth-provider", "owner_id": fixture_id,
                "connection_id": "owned-slack-connection", "team_id": "T00000001",
                "user_id": "U00000002", "channel_id": "C00000003",
                "message": "ADP isolated fixture message.", "run_marker": str(uuid4()),
                "scopes": ["users:read", "chat:write"]}

    def cleanup_authority(self, fixture_id, attempt_id):
        return {"source": "independent-owner-approval", "owner_id": fixture_id,
                "team_id": "T00000001", "user_id": "U00000002",
                "channel_id": "C00000003" if self.defect != "wrong_channel" else "C00000004",
                "method": "chat.delete", "isolated": True, "authorized": True}

    def provider_calls(self, fixture_id, attempt_id):
        return self.state(attempt_id)["calls"]

    def provider_identity(self, fixture_id, attempt_id):
        return {"source": "independent-oauth-provider", "team_id": "T00000001",
                "user_id": "U00000009" if self.defect == "wrong_identity" else "U00000002",
                "scopes": ["users:read", "chat:write"]}

    def provider_posts(self, fixture_id, attempt_id):
        return self.state(attempt_id)["posts"]

    def messages(self, fixture_id, attempt_id, timeout_seconds):
        assert timeout_seconds == 5
        return list(self.state(attempt_id)["messages"])

    def receipt(self, fixture_id, attempt_id):
        return self.state(attempt_id)["receipt"]

    def session(self, attempt_id):
        return {"source": "independent-oauth-provider", "count": 1,
                "ttl_seconds": 300, "cleanup_seconds": 5}

    def cleaned(self, attempt_id):
        return self.state(attempt_id)["deleted"]


class LiveDriver:
    isolated = authorized = cleanup_authorized = True
    version = "fixture-v1"

    def __init__(self, observer, client_name):
        self.observer = observer
        self.client_name = client_name

    def connect(self, fixture_id, attempt_id):
        return {"owner_id": fixture_id, "connection_id": "owned-slack-connection"}

    def discover(self, fixture_id, attempt_id):
        return ["slack___get_user", "slack___post_message"]

    def call(self, fixture_id, operation, attempt_id, user_id):
        assert operation == "slack___get_user"
        self.observer.state(attempt_id)["calls"] += 1
        return {"id": "U00000009" if self.observer.defect == "wrong_read_user" else user_id}

    def post(self, fixture_id, attempt_id, channel_id, text, run_marker):
        state = self.observer.state(attempt_id)
        state["calls"] += 1
        state["posts"] += 1
        message = {"channel_id": channel_id, "user_id": "U00000002", "text": text,
                   "run_marker": run_marker, "ts": "1700000000.000001"}
        if self.observer.defect == "wrong_user":
            message["user_id"] = "U00000009"
        state["messages"].append(message)
        state["receipt"] = {"receipt_id": "receipt-one", "status": "committed"}
        return {**message, "connection_id": "owned-slack-connection", "receipt_id": "receipt-one"}

    def delete(self, attempt_id, channel_id, ts, method):
        assert method == "chat.delete" and channel_id == "C00000003"
        state = self.observer.state(attempt_id)
        if self.observer.defect != "uncleaned":
            state["messages"] = [message for message in state["messages"] if message["ts"] != ts]
            state["deleted"] = True


def test_scn_8077_04_three_clients_independent_post_and_cleanup(tmp_path, monkeypatch):
    from tests.e2e.tools.scenarios import CLIENTS

    observer = LiveObserver()
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    passed = runner.evaluate(
        "SCN-8077-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE,
        IMAGE, command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda _, selected, __: LiveDriver(observer, selected),
        integration_observer=observer,
    )
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert passed and report["live_attestation"] is False
    assert len(report["scenarios"]) == 3
    assert {row["client"] for row in report["scenarios"]} == set(CLIENTS)
    assert all(row["status"] == "pass" and row["cleanup"]["status"] == "confirmed"
               and row["observations"]["exact_message_count"] == 1
               and row["observations"]["provider_posts"] == 1
               for row in report["scenarios"])
    assert len(observer.states) == 3
    assert all(state["calls"] == 2 and state["posts"] == 1 and
               not state["messages"] and state["deleted"] for state in observer.states.values())


@pytest.mark.parametrize("defect", ("overbudget", "wrong_channel", "wrong_user",
                                     "wrong_identity", "wrong_read_user", "uncleaned"))
def test_scn_8077_04_blocks_unpriced_unauthorized_or_unverified_cells(defect, monkeypatch):
    observer = LiveObserver(defect)
    if defect == "uncleaned":
        clock = iter((0, 601))
        monkeypatch.setattr("tests.e2e.tools.slack_integration.monotonic", lambda: next(clock))
    revisions = {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE}
    if defect == "overbudget":
        with pytest.raises(FixtureUnavailable):
            slack_ac04(LiveDriver(observer, "cli"), observer, "cli", revisions)
    else:
        result = slack_ac04(LiveDriver(observer, "cli"), observer, "cli", revisions)
        assert result["status"] != "pass"
        if defect in {"wrong_channel", "wrong_identity", "wrong_read_user"}:
            assert observer.state(result["attempt_id"])["posts"] == 0


def test_scn_8077_04_underpriced_native_client_never_posts():
    observer = LiveObserver("underpriced_model")
    with pytest.raises(FixtureUnavailable, match="pricing"):
        slack_ac04(LiveDriver(observer, "codex"), observer, "codex", {
            "gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE})
    assert all(state["posts"] == 0 for state in observer.states.values())


def test_scn_8077_04_live_blocks_without_private_deployed_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    passed = runner.evaluate(
        "SCN-8077-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE,
        IMAGE, command=["adp", "mcp"], client_factory=FixtureClient, live=True,
        integration_driver_factory=lambda *_: pytest.fail("untrusted driver must not start"),
        integration_observer=LiveObserver(),
    )
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not passed and len(rows) == 3
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target"
               for row in rows)


class Observer:
    def __init__(self):
        self.reads = 0
        self.posts = 0
        self.cleaned_up = False
        self.identity = {
            "source": "independent-oauth-provider",
            "team_id": "T00000001",
            "user_id": "U00000002",
            "scope": "users:read",
        }

    def expected(self, fixture_id, attempt_id):
        return {
            "source": "independent-oauth-provider",
            "owner_id": fixture_id,
            "connection_id": "owned-slack-connection",
            "team_id": "T00000001",
            "user_id": "U00000002",
            "scope": "users:read",
        }

    def provider_calls(self, fixture_id, attempt_id):
        return self.reads

    def provider_posts(self, fixture_id, attempt_id):
        return self.posts

    def provider_identity(self, fixture_id, attempt_id):
        return self.identity.copy()

    def receipt(self, fixture_id, attempt_id):
        return {"receipt_id": "receipt-one", "status": "committed"}

    def cleaned(self, attempt_id):
        return self.cleaned_up


class Driver:
    isolated = True
    authorized = True

    def __init__(self, observer, defect=None):
        self.observer, self.defect = observer, defect

    def connect(self, fixture_id, attempt_id):
        return {"owner_id": fixture_id, "connection_id": "owned-slack-connection"}

    def discover(self, fixture_id, attempt_id):
        return (
            ["slack___get_user", "slack___post_message"]
            if self.defect == "advertised_post"
            else ["slack___get_user"]
        )

    def call(self, fixture_id, attempt_id, user_id):
        self.observer.reads += 1 if self.defect == "missing_read" else 2
        return {
            "id": "U00000003" if self.defect == "wrong_user" else user_id,
            "connection_id": "owned-slack-connection",
            "receipt_id": "receipt-one",
        }

    def post_denied(self, fixture_id, attempt_id):
        if self.defect == "sent_post":
            self.observer.posts += 1
        return {"error": "Slack connection unqualified"}

    def cleanup(self, attempt_id):
        self.observer.cleaned_up = self.defect != "uncleaned"


def test_scn_8077_01_proves_owned_read_denial_and_cleanup(tmp_path, monkeypatch):
    observer = Observer()
    assert slack_ac01(Driver(observer), observer)["status"] == "pass"
    assert observer.reads == 2 and observer.posts == 0 and observer.cleaned_up
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    passed = runner.evaluate(
        "SCN-8077-01",
        tmp_path,
        "https://example.invalid",
        SHA,
        GATEWAY_IMAGE,
        IMAGE,
        command=["adp", "mcp"],
        client_factory=FixtureClient,
        integration_driver_factory=lambda *_: Driver(observer),
        integration_observer=observer,
    )
    (row,) = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert passed and row["status"] == "pass" and row["issue_number"] == 8077
    assert (
        row["local_scenario_id"] == "SCN-01"
        and row["evidence_scope"] == "repository-integration"
    )
    assert row["observations"] == {
        "source": "independent-oauth-provider",
        "provider_reads": 2,
        "provider_posts": 0,
        "receipt": "committed",
        "user_id_match": True,
    }


@pytest.mark.parametrize(
    "defect",
    ("advertised_post", "missing_read", "wrong_user", "sent_post", "uncleaned", "wrong_observer"),
)
def test_scn_8077_01_rejects_false_read_or_post_evidence(defect):
    observer = Observer()
    if defect == "wrong_observer":
        observer.identity["user_id"] = "U00000003"
    result = slack_ac01(Driver(observer, defect), observer)
    assert result["status"] != "pass"


def test_scn_8077_01_requires_isolated_fixture(tmp_path, monkeypatch):
    with pytest.raises(FixtureUnavailable):
        slack_ac01(None, Observer())
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    passed = runner.evaluate(
        "SCN-8077-01",
        tmp_path,
        "https://example.invalid",
        SHA,
        GATEWAY_IMAGE,
        IMAGE,
        command=["adp", "mcp"],
        client_factory=FixtureClient,
    )
    (row,) = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not passed and row["status"] == "blocked"


class PostProvider:
    def __init__(self):
        self.messages = []
        self.posts = 0

    def send(self, channel_id, user_id, text, marker):
        self.posts += 1
        record = {"channel_id": channel_id, "user_id": user_id, "text": text,
                  "run_marker": marker, "ts": f"1710000000.{self.posts:06d}"}
        self.messages.append(record)
        return record.copy()


class PostObserver:
    def __init__(self, provider):
        self.provider = provider
        self.clean = False
        self.markers = {}

    def expected(self, fixture_id, attempt_id):
        self.markers.setdefault(attempt_id, str(uuid4()))
        return {"source": "independent-oauth-provider", "owner_id": fixture_id,
                "connection_id": "owned-slack-connection", "team_id": "T00000001",
                "user_id": "U00000001", "channel_id": "C00000001",
                "message": "ADP isolated fixture message.", "run_marker": self.markers[attempt_id],
                "scopes": ["users:read", "chat:write"]}

    def messages(self, fixture_id, attempt_id):
        return [message.copy() for message in self.provider.messages]

    def provider_posts(self, fixture_id, attempt_id):
        return self.provider.posts

    def cleaned(self, attempt_id):
        return self.clean


class PostDriver:
    isolated = True
    authorized = True
    approved = True
    cleanup_authorized = True

    def __init__(self, provider, observer, defect=None):
        self.provider, self.observer, self.defect = provider, observer, defect
        self.marker = None

    def connect(self, fixture_id, attempt_id):
        return {"owner_id": fixture_id, "connection_id": "owned-slack-connection"}

    def discover(self, fixture_id, attempt_id):
        return ["slack___get_user", "slack___post_message"]

    def post(self, fixture_id, attempt_id, channel_id, text, run_marker):
        self.marker = run_marker
        actual_channel = "C00000002" if self.defect == "wrong_channel" else channel_id
        if self.defect == "missing_post":
            record = {"channel_id": channel_id, "user_id": "U00000001", "text": text,
                      "run_marker": run_marker, "ts": "1710000000.000001"}
        else:
            record = self.provider.send(actual_channel, "U00000001", text, run_marker)
        if self.defect == "extra_post":
            self.provider.send(actual_channel, "U00000001", text, run_marker)
        if self.defect == "wrong_response":
            record["user_id"] = "U00000002"
        return {**record, "receipt_id": "receipt-one", "connection_id": "owned-slack-connection"}

    def cleanup(self, attempt_id):
        if self.defect != "uncleaned":
            self.provider.messages = [message for message in self.provider.messages if message["run_marker"] != self.marker]
            self.observer.clean = True


def test_scn_8077_02_proves_one_exact_post_by_independent_channel_readback(tmp_path, monkeypatch):
    from tests.e2e.tools.slack_integration import slack_ac02

    provider = PostProvider()
    observer = PostObserver(provider)
    result = slack_ac02(PostDriver(provider, observer), observer)
    assert result["status"] == "pass" and provider.posts == 1 and provider.messages == []
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    completed = runner.evaluate(
        "SCN-8077-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: PostDriver(provider, observer), integration_observer=observer,
    )
    (row,) = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert completed and row["status"] == "pass" and row["issue_number"] == 8077
    assert row["local_scenario_id"] == "SCN-02" and row["evidence_scope"] == "repository-integration"
    assert row["observations"] == {"source": "independent-oauth-provider", "provider_posts": 1,
                                   "exact_message_count": 1, "channel_match": True, "user_match": True, "marker_match": True}
    assert provider.posts == 2 and provider.messages == []


@pytest.mark.parametrize("defect", ("missing_post", "extra_post", "wrong_channel", "wrong_response", "uncleaned"))
def test_scn_8077_02_rejects_false_acknowledgment_or_unapproved_destination(defect):
    from tests.e2e.tools.slack_integration import slack_ac02

    provider = PostProvider()
    observer = PostObserver(provider)
    assert slack_ac02(PostDriver(provider, observer, defect), observer)["status"] != "pass"


def test_scn_8077_02_requires_approved_cleanup_and_independent_observer():
    from tests.e2e.tools.slack_integration import slack_ac02

    provider = PostProvider()
    observer = PostObserver(provider)
    driver = PostDriver(provider, observer)
    driver.cleanup_authorized = False
    with pytest.raises(FixtureUnavailable):
        slack_ac02(driver, observer)
    assert provider.posts == 0


class RecoveryProvider(PostProvider):
    def __init__(self):
        super().__init__()
        self.requests = 0


class RecoveryObserver(PostObserver):
    def __init__(self, provider):
        super().__init__(provider)
        self.receipts = {}
        self.cleaned_attempts = set()

    def provider_calls(self, fixture_id, attempt_id):
        return self.provider.requests

    def receipt(self, fixture_id, attempt_id):
        return self.receipts.get(attempt_id)

    def cleaned(self, attempt_id):
        return attempt_id in self.cleaned_attempts


class RecoveryDriver(PostDriver):
    def __init__(self, provider, observer, defect=None):
        super().__init__(provider, observer, defect)
        self.active = {}

    def discover(self, fixture_id, attempt_id, fault):
        return ["slack___get_user"] if fault == "revoked_scope" else super().discover(fixture_id, attempt_id)

    def fault(self, fixture_id, attempt_id, fault, channel_id, text, run_marker):
        self.active[attempt_id] = (fault, channel_id, text, run_marker)
        if fault == "revoked_scope":
            if self.defect == "revoked_post":
                self.provider.requests += 1
                self.provider.send(channel_id, "U00000001", text, run_marker)
            return {"status": 403, "error": "slack_write_scope_missing", "receipt_id": None}
        receipt_id = "receipt-" + attempt_id
        if self.defect != "missing_receipt":
            self.observer.receipts[attempt_id] = {"receipt_id": receipt_id, "status": "committed"}
        self.provider.requests += 1
        if fault == "ok_false":
            return {"status": 403, "error": "slack_api_error", "provider_status": 200,
                    "provider_error": "missing_scope", "receipt_id": receipt_id}
        if self.defect != "missing_post":
            actual_channel = "C00000002" if self.defect == "wrong_channel" else channel_id
            self.provider.send(actual_channel, "U00000001", text, run_marker)
        return {"status": 503, "error": "slack_post_uncertain", "receipt_id": receipt_id}

    def retry(self, fixture_id, attempt_id, run_marker):
        fault, channel_id, text, marker = self.active[attempt_id]
        assert marker == run_marker
        if self.defect == "extra_post" and fault == "reply_loss":
            self.provider.requests += 1
            self.provider.send(channel_id, "U00000001", text, marker)
        return {"status": 200, "error": "false_acknowledgment"} if self.defect == "false_recovery" else {
            "status": 409, "error": "Slack receipt already used",
        }

    def cleanup(self, attempt_id):
        marker = self.active[attempt_id][3] if attempt_id in self.active else None
        if self.defect != "uncleaned":
            self.provider.messages = [message for message in self.provider.messages if message["run_marker"] != marker]
        self.observer.cleaned_attempts.add(attempt_id)


def test_scn_8077_03_independently_resolves_lost_reply_and_checks_receipts(tmp_path, monkeypatch):
    from tests.e2e.tools.slack_integration import slack_ac03

    provider = RecoveryProvider()
    observer = RecoveryObserver(provider)
    result = slack_ac03(RecoveryDriver(provider, observer), observer)
    assert result["status"] == "pass" and result["cleanup"] == {"status": "confirmed"}
    assert [entry["fault"] for entry in result["observations"]] == ["ok_false", "reply_loss", "revoked_scope"]
    assert [entry["provider_calls"] for entry in result["observations"]] == [1, 1, 0]
    assert [entry["provider_posts"] for entry in result["observations"]] == [0, 1, 0]
    assert [entry["outcome"] for entry in result["observations"]] == [
        "observed-no-post", "observed-posted", "observed-no-post",
    ]
    assert provider.requests == 2 and provider.posts == 1 and provider.messages == []
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    passed = runner.evaluate(
        "SCN-8077-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: RecoveryDriver(provider, observer), integration_observer=observer,
    )
    (row,) = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert passed and row["status"] == "pass" and row["issue_number"] == 8077
    assert row["local_scenario_id"] == "SCN-03" and row["evidence_scope"] == "repository-integration"
    assert row["cleanup"] == {"status": "confirmed"} and provider.requests == 4 and provider.posts == 2
    assert provider.messages == []


@pytest.mark.parametrize("defect", ("missing_receipt", "missing_post", "wrong_channel", "extra_post", "false_recovery", "revoked_post", "uncleaned"))
def test_scn_8077_03_refuses_missing_receipt_false_readback_or_retry(defect):
    from tests.e2e.tools.slack_integration import slack_ac03

    provider = RecoveryProvider()
    observer = RecoveryObserver(provider)
    result = slack_ac03(RecoveryDriver(provider, observer, defect), observer)
    assert result["status"] != "pass"
    assert result["cleanup"]["status"] == ("unknown" if defect == "uncleaned" else "confirmed")


def test_scn_8077_03_needs_isolated_approved_fixture_and_not_live(tmp_path, monkeypatch):
    from tests.e2e.tools.slack_integration import slack_ac03

    provider = RecoveryProvider()
    observer = RecoveryObserver(provider)
    driver = RecoveryDriver(provider, observer)
    driver.cleanup_authorized = False
    with pytest.raises(FixtureUnavailable):
        slack_ac03(driver, observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    passed = runner.evaluate(
        "SCN-8077-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], live=True, client_factory=FixtureClient,
        integration_driver_factory=lambda *_: pytest.fail("local fixture cannot qualify live acceptance"),
        integration_observer=observer,
    )
    (row,) = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not passed and row["status"] == "blocked" and provider.requests == 0
