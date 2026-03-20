"""Unit tests for regex and header scrubbing (Issue #143)."""


class TestHeaderScrubber:
    """Tests for header scrubbing functionality."""

    def test_scrub_authorization_header(self, header_scrubber):
        """Test that Authorization header is scrubbed."""
        headers = {
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "Content-Type": "application/json",
        }
        result = header_scrubber.scrub(headers)

        assert result.content["Authorization"] == "[REDACTED:HEADER]"
        assert result.content["Content-Type"] == "application/json"
        assert result.redactions_count == 1
        assert "Authorization" in result.headers_scrubbed

    def test_scrub_x_api_key_header(self, header_scrubber):
        """Test that X-Api-Key header is scrubbed."""
        headers = {
            "X-Api-Key": "sk-test-api-key-12345",
            "Accept": "application/json",
        }
        result = header_scrubber.scrub(headers)

        assert result.content["X-Api-Key"] == "[REDACTED:HEADER]"
        assert result.content["Accept"] == "application/json"

    def test_scrub_cookie_header(self, header_scrubber):
        """Test that Cookie header is scrubbed."""
        headers = {
            "Cookie": "session=abc123; token=xyz789",
            "Host": "api.example.com",
        }
        result = header_scrubber.scrub(headers)

        assert result.content["Cookie"] == "[REDACTED:HEADER]"
        assert result.content["Host"] == "api.example.com"

    def test_scrub_multiple_sensitive_headers(self, header_scrubber):
        """Test scrubbing multiple sensitive headers."""
        headers = {
            "Authorization": "Bearer token",
            "X-Api-Key": "api-key",
            "Cookie": "session=123",
            "Content-Type": "application/json",
        }
        result = header_scrubber.scrub(headers)

        assert result.redactions_count == 3
        assert len(result.headers_scrubbed) == 3

    def test_scrub_case_insensitive(self, header_scrubber):
        """Test that header matching is case-insensitive."""
        headers = {
            "authorization": "Bearer token",
            "x-API-KEY": "key",
        }
        result = header_scrubber.scrub(headers)

        # Note: original key case is preserved
        assert result.content["authorization"] == "[REDACTED:HEADER]"
        assert result.redactions_count == 2

    def test_scrub_empty_headers(self, header_scrubber):
        """Test scrubbing empty headers dict."""
        result = header_scrubber.scrub({})
        assert result.content == {}
        assert result.redactions_count == 0

    def test_scrub_none_headers(self, header_scrubber):
        """Test scrubbing None headers."""
        result = header_scrubber.scrub(None)
        assert result.content == {}
        assert result.redactions_count == 0


class TestRegexScrubber:
    """Tests for regex-based secret detection."""

    def test_scrub_aws_access_key(self, regex_scrubber):
        """Test AWS access key detection."""
        text = "My AWS key is AKIAIOSFODNN7EXAMPLE"
        result = regex_scrubber.scrub_text(text)

        assert "AKIAIOSFODNN7EXAMPLE" not in result.content
        assert "[REDACTED:AWS_ACCESS_KEY]" in result.content
        assert result.redactions_count >= 1

    def test_scrub_aws_secret_key_pattern(self, regex_scrubber):
        """Test AWS secret key pattern detection."""
        text = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result = regex_scrubber.scrub_text(text)

        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result.content
        assert "[REDACTED:" in result.content

    def test_scrub_jwt_token(self, regex_scrubber):
        """Test JWT token detection."""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        text = f"Authorization token: {jwt}"
        result = regex_scrubber.scrub_text(text)

        assert jwt not in result.content
        assert "[REDACTED:" in result.content  # May be JWT_TOKEN or TOKEN pattern

    def test_scrub_private_key(self, regex_scrubber):
        """Test private key detection."""
        text = """Here is a key:
-----BEGIN RSA PRIVATE KEY-----
MIIEpQIBAAKCAQEAzR...
-----END RSA PRIVATE KEY-----
"""
        result = regex_scrubber.scrub_text(text)

        assert "-----BEGIN RSA PRIVATE KEY-----" not in result.content
        assert "[REDACTED:PRIVATE_KEY]" in result.content

    def test_scrub_postgresql_uri(self, regex_scrubber):
        """Test PostgreSQL connection string detection."""
        text = "Connect to postgresql://user:password@host:5432/db"
        result = regex_scrubber.scrub_text(text)

        assert "postgresql://user:password@host:5432/db" not in result.content
        assert "[REDACTED:CONNECTION_STRING]" in result.content

    def test_scrub_redis_uri(self, regex_scrubber):
        """Test Redis connection string detection."""
        text = "Redis at redis://default:secret@redis.example.com:6379/0"
        result = regex_scrubber.scrub_text(text)

        assert "redis://" not in result.content.lower() or "[REDACTED:" in result.content
        assert "[REDACTED:CONNECTION_STRING]" in result.content

    def test_scrub_mongodb_uri(self, regex_scrubber):
        """Test MongoDB connection string detection."""
        text = "MongoDB: mongodb+srv://user:pass@cluster.mongodb.net/mydb"
        result = regex_scrubber.scrub_text(text)

        assert "mongodb+srv://" not in result.content
        assert "[REDACTED:CONNECTION_STRING]" in result.content

    def test_scrub_password_pattern(self, regex_scrubber):
        """Test password pattern detection."""
        # Use format that matches the regex: password=value (no spaces/quotes)
        text = "config password=secretpass123"
        result = regex_scrubber.scrub_text(text)

        assert "secretpass123" not in result.content
        assert "[REDACTED:" in result.content

    def test_scrub_sk_api_key(self, regex_scrubber):
        """Test sk- API key pattern (OpenAI style)."""
        text = "API key: sk-proj-abc123def456xyz789abcdef012"
        result = regex_scrubber.scrub_text(text)

        assert "sk-proj-abc123def456xyz789abcdef012" not in result.content
        assert "[REDACTED:API_KEY]" in result.content

    def test_scrub_github_pat(self, regex_scrubber):
        """Test GitHub personal access token detection."""
        text = "Token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"
        result = regex_scrubber.scrub_text(text)

        assert "ghp_" not in result.content
        # May be caught by GITHUB_PAT or generic TOKEN pattern
        assert "[REDACTED:" in result.content

    def test_scrub_github_server_token(self, regex_scrubber):
        """Test GitHub server token detection."""
        text = "Server token: ghs_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"
        result = regex_scrubber.scrub_text(text)

        assert "ghs_" not in result.content
        # May be caught by GITHUB_TOKEN or generic TOKEN pattern
        assert "[REDACTED:" in result.content

    def test_scrub_slack_token(self, regex_scrubber):
        """Test Slack token detection."""
        text = "Slack: xoxb-123456789-abcdefghij"
        result = regex_scrubber.scrub_text(text)

        assert "xoxb-" not in result.content
        assert "[REDACTED:SLACK_TOKEN]" in result.content

    def test_scrub_bearer_token(self, regex_scrubber):
        """Test Bearer token detection."""
        text = "Header: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
        result = regex_scrubber.scrub_text(text)

        # Should be caught by either bearer or jwt pattern
        assert "[REDACTED:" in result.content

    def test_scrub_nested_dict(self, regex_scrubber):
        """Test scrubbing nested dictionary."""
        data = {
            "config": {
                "database": "postgresql://user:pass@localhost/db",
                "api_key": "sk-secret-key-12345678901234567890",
            },
            "message": "Hello world",
        }
        result = regex_scrubber.scrub_dict(data)

        assert "postgresql://" not in str(result.content)
        assert "[REDACTED:" in str(result.content)

    def test_scrub_list(self, regex_scrubber):
        """Test scrubbing a list."""
        data = [
            "Regular text",
            "API key: sk-secret-api-key-12345678901234",
            {"nested": "postgresql://user:pass@host/db"},
        ]
        result = regex_scrubber.scrub_list(data)

        assert "[REDACTED:" in str(result.content)

    def test_scrub_empty_text(self, regex_scrubber):
        """Test scrubbing empty text."""
        result = regex_scrubber.scrub_text("")
        assert result.content == ""
        assert result.redactions_count == 0

    def test_no_false_positives_normal_text(self, regex_scrubber):
        """Test that normal text isn't falsely detected."""
        text = "Hello, how are you? The weather is nice today."
        result = regex_scrubber.scrub_text(text)

        assert result.content == text
        assert result.redactions_count == 0


class TestScrubPipeline:
    """Tests for the complete scrub pipeline."""

    def test_scrub_request_with_headers(self, scrub_pipeline):
        """Test scrubbing request with sensitive headers."""
        request_body = {
            "messages": [{"role": "user", "content": "My password=secret123"}],
            "model": "claude-3",
        }
        headers = {
            "Authorization": "Bearer token123",
            "X-Api-Key": "api-key-456",
        }

        scrubbed, result = scrub_pipeline.scrub_request(request_body, headers)

        assert "Authorization" in result.headers_scrubbed
        assert "X-Api-Key" in result.headers_scrubbed
        assert "secret123" not in str(scrubbed)
        assert result.redactions_count >= 3  # 2 headers + password

    def test_scrub_response(self, scrub_pipeline):
        """Test scrubbing response body."""
        response = {
            "content": [{"type": "text", "text": "Your API key is sk-test-key-123456789012345678901234"}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

        scrubbed, result = scrub_pipeline.scrub_response(response)

        assert "sk-test-key" not in str(scrubbed)
        assert result.redactions_count >= 1

    def test_scrub_text(self, scrub_pipeline):
        """Test scrubbing plain text."""
        text = "Connect to postgresql://user:pass@localhost/db"
        scrubbed, result = scrub_pipeline.scrub_text(text)

        assert "postgresql://" not in scrubbed
        assert "[REDACTED:" in scrubbed
