"""Tests for the envelope token_source field (Issue #3385, C3)."""

from common.envelope import WebhookEnvelope


class TestTokenSource:
    """Issue #3385: token_source field for PAT execution path."""

    def test_token_source_default_is_none(self) -> None:
        """token_source defaults to None (backward compat)."""
        envelope = WebhookEnvelope()
        assert envelope.token_source is None

    def test_envelope_to_dict_omits_token_source_when_none(self) -> None:
        """to_dict() does NOT include token_source key when None (backward compat).

        Existing consumers that don't know about the field must not see it.
        """
        envelope = WebhookEnvelope(channel="github", tenant_id="t1")
        data = envelope.to_dict()
        assert "token_source" not in data

    def test_envelope_to_dict_includes_token_source_when_pat(self) -> None:
        """to_dict() includes token_source="pat" when explicitly set."""
        envelope = WebhookEnvelope(
            channel="github",
            tenant_id="tenant-abc",
            token_source="pat",
        )
        data = envelope.to_dict()
        assert data["token_source"] == "pat"

    def test_envelope_to_dict_includes_token_source_when_app(self) -> None:
        """to_dict() includes token_source="app" when explicitly set."""
        envelope = WebhookEnvelope(
            channel="github",
            tenant_id="tenant-abc",
            token_source="app",
        )
        data = envelope.to_dict()
        assert data["token_source"] == "app"

    def test_worker_parse_tolerates_absent_token_source(self) -> None:
        """Envelope from dict without token_source works (legacy)."""
        # Simulate receiving a legacy envelope dict without the field
        legacy_dict = {
            "version": "1.0",
            "channel": "github",
            "tenant_id": "t1",
            "persona": "dev",
        }
        # WebhookEnvelope uses dataclass defaults; field not in dict = None
        envelope = WebhookEnvelope(
            version=legacy_dict.get("version", "1.0"),
            channel=legacy_dict.get("channel", "github"),
            tenant_id=legacy_dict.get("tenant_id", ""),
            persona=legacy_dict.get("persona", ""),
        )
        assert envelope.token_source is None

    def test_other_fields_unaffected_by_token_source(self) -> None:
        """Adding token_source does not affect existing field serialization."""
        envelope = WebhookEnvelope(
            channel="github",
            tenant_id="tenant-xyz",
            model_requested="claude-opus",
            model_resolved="us.anthropic.claude-opus-4-6-v1",
            aws_label="prod-account",
            token_source="pat",
        )
        data = envelope.to_dict()
        assert data["model_requested"] == "claude-opus"
        assert data["model_resolved"] == "us.anthropic.claude-opus-4-6-v1"
        assert data["aws_label"] == "prod-account"
        assert data["token_source"] == "pat"
        assert data["channel"] == "github"
