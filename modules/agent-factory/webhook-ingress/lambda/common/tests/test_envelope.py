"""Tests for the envelope dataclass models."""

from common.envelope import Actor, Intent, SourceRef, WebhookEnvelope


class TestActor:
    def test_basic_actor(self) -> None:
        actor = Actor(github_id=123, github_login="octocat", is_bot=False)
        assert actor.github_id == 123
        assert actor.github_login == "octocat"
        assert actor.is_bot is False

    def test_bot_actor(self) -> None:
        actor = Actor(github_id=456, github_login="dependabot[bot]", is_bot=True)
        assert actor.is_bot is True

    def test_defaults(self) -> None:
        actor = Actor()
        assert actor.github_id == 0
        assert actor.github_login == ""
        assert actor.is_bot is False


class TestSourceRef:
    def test_minimal_source_ref(self) -> None:
        ref = SourceRef(installation_id=100, repo="org/repo")
        assert ref.installation_id == 100
        assert ref.repo == "org/repo"
        assert ref.issue is None
        assert ref.pr is None
        assert ref.sha is None

    def test_full_source_ref(self) -> None:
        ref = SourceRef(
            installation_id=100,
            repo="org/repo",
            issue=42,
            pr=99,
            sha="abc123",
        )
        assert ref.issue == 42
        assert ref.pr == 99
        assert ref.sha == "abc123"


class TestIntent:
    def test_intent_with_label(self) -> None:
        intent = Intent(trigger="issue_labeled", label="agent:dev", persona="dev")
        assert intent.trigger == "issue_labeled"
        assert intent.label == "agent:dev"
        assert intent.persona == "dev"

    def test_intent_without_label(self) -> None:
        intent = Intent(trigger="pr_opened", persona="ops")
        assert intent.label is None


class TestWebhookEnvelope:
    def test_full_envelope(self) -> None:
        envelope = WebhookEnvelope(
            channel="github",
            tenant_id="tenant-abc",
            persona="dev",
            actor=Actor(github_id=1, github_login="user"),
            source_ref=SourceRef(installation_id=100, repo="org/repo", issue=5),
            intent=Intent(trigger="mentioned", persona="dev"),
            payload={"action": "created", "comment": {"body": "@agent help"}},
        )
        assert envelope.version == "1.0"
        assert envelope.channel == "github"
        assert envelope.tenant_id == "tenant-abc"
        assert envelope.arrived_at is not None

    def test_envelope_to_dict(self) -> None:
        envelope = WebhookEnvelope(
            channel="slack",
            tenant_id="tenant-xyz",
            actor=Actor(github_id=2, github_login="bot", is_bot=True),
            source_ref=SourceRef(installation_id=200, repo="org/other"),
            intent=Intent(trigger="slash_command", persona="pm"),
            payload={"text": "/deploy"},
        )
        data = envelope.to_dict()
        assert data["version"] == "1.0"
        assert data["channel"] == "slack"
        assert data["tenant_id"] == "tenant-xyz"
        assert data["actor"]["is_bot"] is True
        assert data["source_ref"]["installation_id"] == 200
        assert data["intent"]["trigger"] == "slash_command"

    def test_envelope_defaults(self) -> None:
        envelope = WebhookEnvelope()
        assert envelope.version == "1.0"
        assert envelope.channel == "github"
        assert envelope.tenant_id == ""
        assert envelope.arrived_at != ""
