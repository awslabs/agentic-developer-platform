"""Unit tests for Comprehend PII detection (Issue #143)."""

from unittest.mock import MagicMock, patch

import pytest

from src.chat_logging.comprehend_client import ComprehendPiiDetector


class TestComprehendPiiDetector:
    """Tests for Amazon Comprehend PII detection client."""

    @pytest.fixture
    def detector(self):
        """Create a PII detector with mocked client."""
        return ComprehendPiiDetector(region_name="us-east-1")

    @pytest.fixture
    def mock_comprehend_response_with_pii(self):
        """Mock Comprehend response with PII entities."""
        return {
            "Entities": [
                {
                    "Type": "EMAIL",
                    "Score": 0.99,
                    "BeginOffset": 20,
                    "EndOffset": 35,
                },
                {
                    "Type": "NAME",
                    "Score": 0.95,
                    "BeginOffset": 0,
                    "EndOffset": 8,
                },
            ]
        }

    @pytest.fixture
    def mock_comprehend_response_empty(self):
        """Mock Comprehend response with no PII."""
        return {"Entities": []}

    @pytest.mark.asyncio
    async def test_detect_and_redact_email(self, detector):
        """Test email PII detection and redaction."""
        text = "John Doe john.doe@example.com"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {
                        "Type": "EMAIL",
                        "Score": 0.99,
                        "BeginOffset": 9,
                        "EndOffset": 29,
                    },
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "john.doe@example.com" not in result.content
            assert "[PII:EMAIL]" in result.content
            assert "EMAIL" in result.pii_types_found
            assert result.redactions_count == 1

    @pytest.mark.asyncio
    async def test_detect_and_redact_name(self, detector):
        """Test name PII detection and redaction."""
        text = "Contact John Smith for details."

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {
                        "Type": "NAME",
                        "Score": 0.98,
                        "BeginOffset": 8,
                        "EndOffset": 18,
                    },
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "John Smith" not in result.content
            assert "[PII:NAME]" in result.content

    @pytest.mark.asyncio
    async def test_detect_and_redact_phone(self, detector):
        """Test phone number PII detection."""
        text = "Call me at 555-123-4567 please."

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {
                        "Type": "PHONE",
                        "Score": 0.95,
                        "BeginOffset": 11,
                        "EndOffset": 23,
                    },
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "555-123-4567" not in result.content
            assert "[PII:PHONE]" in result.content

    @pytest.mark.asyncio
    async def test_detect_and_redact_ssn(self, detector):
        """Test SSN PII detection."""
        text = "SSN: 123-45-6789"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {
                        "Type": "SSN",
                        "Score": 0.99,
                        "BeginOffset": 5,
                        "EndOffset": 16,
                    },
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "123-45-6789" not in result.content
            assert "[PII:SSN]" in result.content

    @pytest.mark.asyncio
    async def test_detect_and_redact_credit_card(self, detector):
        """Test credit card PII detection."""
        text = "Card: 4111-1111-1111-1111"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {
                        "Type": "CREDIT_DEBIT_NUMBER",
                        "Score": 0.99,
                        "BeginOffset": 6,
                        "EndOffset": 25,
                    },
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "4111-1111-1111-1111" not in result.content
            assert "[PII:CREDIT_CARD]" in result.content

    @pytest.mark.asyncio
    async def test_detect_and_redact_address(self, detector):
        """Test address PII detection."""
        text = "Ship to 123 Main St, New York, NY 10001"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {
                        "Type": "ADDRESS",
                        "Score": 0.92,
                        "BeginOffset": 8,
                        "EndOffset": 39,
                    },
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "123 Main St" not in result.content
            assert "[PII:ADDRESS]" in result.content

    @pytest.mark.asyncio
    async def test_detect_no_pii(self, detector):
        """Test text with no PII."""
        text = "The weather is nice today."

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {"Entities": []}
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert result.content == text
            assert result.redactions_count == 0
            assert result.pii_types_found == []

    @pytest.mark.asyncio
    async def test_detect_multiple_pii_types(self, detector):
        """Test detection of multiple PII types in one text."""
        text = "John Doe (john@example.com) lives at 123 Main St"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {"Type": "NAME", "Score": 0.95, "BeginOffset": 0, "EndOffset": 8},
                    {"Type": "EMAIL", "Score": 0.99, "BeginOffset": 10, "EndOffset": 26},
                    {"Type": "ADDRESS", "Score": 0.90, "BeginOffset": 37, "EndOffset": 48},
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            assert "[PII:NAME]" in result.content
            assert "[PII:EMAIL]" in result.content
            assert "[PII:ADDRESS]" in result.content
            assert result.redactions_count == 3

    @pytest.mark.asyncio
    async def test_confidence_threshold(self, detector):
        """Test that low-confidence PII is not redacted."""
        detector._confidence_threshold = 0.9
        text = "Maybe a name: John"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {
                "Entities": [
                    {"Type": "NAME", "Score": 0.6, "BeginOffset": 14, "EndOffset": 18},  # Below threshold
                ]
            }
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            # Low confidence should not be redacted
            assert "John" in result.content
            assert result.redactions_count == 0

    @pytest.mark.asyncio
    async def test_detect_empty_text(self, detector):
        """Test handling empty text."""
        result = await detector.detect_and_redact("")
        assert result.content == ""
        assert result.redactions_count == 0

    @pytest.mark.asyncio
    async def test_detect_and_redact_dict(self, detector):
        """Test PII detection in nested dictionary."""
        data = {
            "user": {
                "name": "John Smith is my name",  # Longer than 10 chars to pass length check
                "email": "john@example.com is my email",  # Longer than 10 chars
            },
            "message": "Hello world",
        }

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            # Set up response for each text field
            mock_client.detect_pii_entities.side_effect = [
                {"Entities": [{"Type": "NAME", "Score": 0.95, "BeginOffset": 0, "EndOffset": 10}]},
                {"Entities": [{"Type": "EMAIL", "Score": 0.99, "BeginOffset": 0, "EndOffset": 16}]},
                {"Entities": []},  # Hello world has no PII
            ]
            mock_client_getter.return_value = mock_client

            result_data, result = await detector.detect_and_redact_dict(data)

            assert "[PII:NAME]" in result_data["user"]["name"]
            assert "[PII:EMAIL]" in result_data["user"]["email"]
            assert "Hello world" in result_data["message"]

    @pytest.mark.asyncio
    async def test_error_handling(self, detector):
        """Test error handling when Comprehend fails."""
        text = "Some text to analyze"

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.side_effect = Exception("API Error")
            mock_client_getter.return_value = mock_client

            result = await detector.detect_and_redact(text)

            # On error, original text should be returned
            assert result.content == text
            assert result.error is not None
            assert "Error" in result.error

    @pytest.mark.asyncio
    async def test_text_truncation(self, detector):
        """Test that long text is truncated."""
        # Create text longer than MAX_TEXT_SIZE
        long_text = "x" * (detector.MAX_TEXT_SIZE + 1000)

        with patch.object(detector, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.detect_pii_entities.return_value = {"Entities": []}
            mock_client_getter.return_value = mock_client

            await detector.detect_and_redact(long_text)

            # Verify text was truncated before API call
            call_args = mock_client.detect_pii_entities.call_args
            text_sent = call_args[1]["Text"]
            assert len(text_sent) <= detector.MAX_TEXT_SIZE
