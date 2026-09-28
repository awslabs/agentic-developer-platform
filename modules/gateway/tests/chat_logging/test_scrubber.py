"""Unit tests for regex and header scrubbing (Issue #143)."""

import pytest


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


def test_shared_run_report_capability_is_never_logged():
    from src.chat_logging.scrubber import HeaderScrubber

    result = HeaderScrubber().scrub({"X-Adp-Report-Credential": "adprpt1.sensitive", "Content-Type": "application/json"})
    assert "adprpt1.sensitive" not in str(result)


class TestPersonalDataRegexPatterns:
    """Always-on personal-data coverage in the regex layer (Issue #5672).

    The regex layer is the only layer that runs unconditionally — no AWS API call,
    no entitlement, no quota. Comprehend PII detection is additive and can be
    unavailable in an account. Before #5672 this layer covered credential shapes
    only, so a Comprehend-less environment stored personal data entirely in the
    clear. These tests pin the categories that have a reliable textual shape.
    """

    @pytest.mark.parametrize(
        "label,text,secret",
        [
            ("email", "Please email jane.doe@example.com about it", "jane.doe@example.com"),
            ("email_plus", "reply to a.b+tag@sub.example.co.uk today", "a.b+tag@sub.example.co.uk"),
            ("ssn_dashed", "her SSN is 123-45-6789 on file", "123-45-6789"),
            ("ssn_spaced", "SSN 123 45 6789 recorded", "123 45 6789"),
            ("ssn_labelled", "ssn: 123456789", "123456789"),
            ("national_id_labelled", "national id = 987654321", "987654321"),
            ("card_visa", "charge card 4111111111111111 now", "4111111111111111"),
            ("card_mastercard", "card 5500005555555559 declined", "5500005555555559"),
            ("card_amex", "amex 378282246310005 on file", "378282246310005"),
            ("card_grouped", "card 4111 1111 1111 1111 exp 12/29", "4111 1111 1111 1111"),
            ("card_hyphenated", "card 4111-1111-1111-1111 exp", "4111-1111-1111-1111"),
            ("card_amex_grouped", "amex 3782 822463 10005 cvv", "3782 822463 10005"),
            ("routing", "routing number: 021000021", "021000021"),
            ("aba", "ABA# 021000021", "021000021"),
            ("sort_code", "sort code: 123456", "123456"),
            ("iban_labelled", "iban: GB29NWBK60161331926819", "GB29NWBK60161331926819"),
            ("iban_bare", "wire it to DE89370400440532013000 please", "DE89370400440532013000"),
            ("bank_account", "bank account: 12345678901", "12345678901"),
            ("phone_paren", "call (555) 123-4567 after noon", "(555) 123-4567"),
            ("phone_dashed", "ring 555-123-4567 tomorrow", "555-123-4567"),
            ("phone_dotted", "ring 555.123.4567 tomorrow", "555.123.4567"),
            ("phone_intl", "dial +44 20 7946 0958 now", "+44 20 7946 0958"),
        ],
    )
    def test_personal_data_is_redacted(self, regex_scrubber, label, text, secret):
        result = regex_scrubber.scrub_text(text)

        assert secret not in result.content, f"{label}: {secret!r} survived in {result.content!r}"
        assert "[REDACTED:" in result.content
        assert result.redactions_count >= 1

    @pytest.mark.parametrize(
        "label,text",
        [
            # A loose "digits separator digits separator digits" phone rule also
            # matches these. Redacting them would destroy the operational value of
            # the transcript, which is the whole reason it is kept.
            ("iso_date", "the incident started on 2026-09-22 at midnight"),
            ("us_date", "renewal due 09-22-2026 sharp"),
            ("eu_date", "renewal due 22/09/2026 sharp"),
            ("timestamp", "elapsed 12:34:56 total"),
            ("semver", "gateway version 1.2.3 released"),
            ("long_integer", "we processed 1234567890 records in total"),
            ("plain_prose", "Summarise this document in three bullet points"),
            ("token_counts", "input_tokens 1024 output_tokens 2048"),
        ],
    )
    def test_ordinary_content_is_not_redacted(self, regex_scrubber, label, text):
        """False positives here are a real cost, not a safe default."""
        result = regex_scrubber.scrub_text(text)

        assert result.content == text, f"{label}: false positive {result.patterns_matched} on {text!r}"
        assert result.redactions_count == 0

    def test_representative_transcript_redacts_every_shaped_category(self, regex_scrubber):
        """The acceptance case: one prompt carrying every category at once."""
        prompt = (
            "Customer jane.doe@example.com, phone (555) 123-4567, "
            "SSN 123-45-6789, card 4111 1111 1111 1111, "
            "routing number: 021000021, iban: GB29NWBK60161331926819"
        )

        result = regex_scrubber.scrub_text(prompt)

        for secret in [
            "jane.doe@example.com",
            "(555) 123-4567",
            "123-45-6789",
            "4111 1111 1111 1111",
            "021000021",
            "GB29NWBK60161331926819",
        ]:
            assert secret not in result.content, f"{secret!r} survived in {result.content!r}"

    def test_personal_data_redacted_in_nested_request_body(self, scrub_pipeline):
        """Real prompts arrive nested inside a messages array, not as flat text."""
        request = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "My email is jane.doe@example.com and my card is 4111111111111111"},
                {"role": "assistant", "content": [{"type": "text", "text": "Noted, I also see SSN 123-45-6789"}]},
            ],
        }

        scrubbed, result = scrub_pipeline.scrub_request(request, headers=None)

        flattened = str(scrubbed)
        assert "jane.doe@example.com" not in flattened
        assert "4111111111111111" not in flattened
        assert "123-45-6789" not in flattened
        assert result.redactions_count >= 3

    def test_numeric_personal_data_redacted_in_tool_request_and_response(self, scrub_pipeline):
        request = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": {"customer": {"ssn": 123456789}, "attempt": 2},
                        }
                    ],
                }
            ]
        }
        response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-2",
                    "name": "charge_card",
                    "input": {"card_number": 4111111111111111, "status_code": 200},
                }
            ],
            "usage": {"input_tokens": 123456789, "output_tokens": 16},
        }

        scrubbed_request, request_result = scrub_pipeline.scrub_request(request)
        scrubbed_response, response_result = scrub_pipeline.scrub_response(response)

        stored = str({"request": scrubbed_request, "response": scrubbed_response})
        assert scrubbed_request["messages"][0]["content"][0]["content"]["customer"]["ssn"] == "[REDACTED:NATIONAL_ID]"
        assert "4111111111111111" not in stored
        assert scrubbed_request["messages"][0]["content"][0]["content"]["attempt"] == 2
        assert scrubbed_response["content"][0]["input"]["status_code"] == 200
        assert scrubbed_response["usage"]["input_tokens"] == 123456789
        assert request_result.redactions_count == 1
        assert response_result.redactions_count == 1
        assert "numeric_national_id_field" in request_result.patterns_matched
        assert "numeric_payment_card_field" in response_result.patterns_matched

    def test_credential_patterns_still_redacted(self, regex_scrubber):
        """Regression guard: adding PII patterns must not displace credential ones."""
        text = "key AKIAIOSFODNN7EXAMPLE and token ghp_" + "a" * 36

        result = regex_scrubber.scrub_text(text)

        assert "AKIAIOSFODNN7EXAMPLE" not in result.content
        assert "ghp_" + "a" * 36 not in result.content
