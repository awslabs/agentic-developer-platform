"""Amazon Comprehend PII detection client.

Issue #143: Provides async PII detection using Amazon Comprehend.

Uses asyncio.to_thread for non-blocking Comprehend API calls.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# PII entity types that Comprehend can detect
# https://docs.aws.amazon.com/comprehend/latest/dg/how-pii.html
PII_ENTITY_TYPES = {
    "NAME": "NAME",  # Full names
    "EMAIL": "EMAIL",  # Email addresses
    "PHONE": "PHONE",  # Phone numbers
    "SSN": "SSN",  # Social Security Numbers
    "CREDIT_DEBIT_NUMBER": "CREDIT_CARD",  # Credit/debit card numbers
    "ADDRESS": "ADDRESS",  # Physical addresses
    "DATE_TIME": "DATE_TIME",  # Dates and times (not always PII)
    "BANK_ACCOUNT_NUMBER": "BANK_ACCOUNT",  # Bank account numbers
    "BANK_ROUTING": "BANK_ROUTING",  # Bank routing numbers
    "PASSPORT_NUMBER": "PASSPORT",  # Passport numbers
    "DRIVER_ID": "DRIVER_LICENSE",  # Driver's license numbers
    "IP_ADDRESS": "IP_ADDRESS",  # IP addresses
    "MAC_ADDRESS": "MAC_ADDRESS",  # MAC addresses
    "URL": "URL",  # URLs (not always PII)
    "AGE": "AGE",  # Age information
    "USERNAME": "USERNAME",  # Usernames
    "PASSWORD": "PASSWORD",  # Passwords
    "AWS_ACCESS_KEY": "AWS_ACCESS_KEY",  # AWS access keys
    "AWS_SECRET_KEY": "AWS_SECRET_KEY",  # AWS secret keys
}

# Default PII types to redact (exclude non-sensitive types)
DEFAULT_PII_TYPES_TO_REDACT = {
    "NAME",
    "EMAIL",
    "PHONE",
    "SSN",
    "CREDIT_DEBIT_NUMBER",
    "ADDRESS",
    "BANK_ACCOUNT_NUMBER",
    "BANK_ROUTING",
    "PASSPORT_NUMBER",
    "DRIVER_ID",
    "IP_ADDRESS",
    "USERNAME",
    "PASSWORD",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
}


@dataclass
class PiiDetectionResult:
    """Result of PII detection and redaction."""

    content: str
    redactions_count: int = 0
    pii_types_found: list[str] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class ComprehendPiiDetector:
    """Async wrapper for Amazon Comprehend PII detection.

    Uses asyncio.to_thread to make blocking boto3 calls non-blocking.
    """

    # Maximum text size for Comprehend (100KB)
    MAX_TEXT_SIZE = 100_000
    # Confidence threshold for PII detection
    DEFAULT_CONFIDENCE_THRESHOLD = 0.7

    def __init__(
        self,
        region_name: str = "us-east-1",
        pii_types_to_redact: set[str] | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        """Initialize the Comprehend PII detector.

        Args:
            region_name: AWS region for Comprehend
            pii_types_to_redact: Set of PII types to redact.
                               Defaults to DEFAULT_PII_TYPES_TO_REDACT.
            confidence_threshold: Minimum confidence score to redact (0.0-1.0)
        """
        self._region_name = region_name
        self._pii_types_to_redact = pii_types_to_redact or DEFAULT_PII_TYPES_TO_REDACT
        self._confidence_threshold = confidence_threshold
        self._client: Any = None

    def _get_client(self) -> Any:
        """Get or create the Comprehend client.

        Returns:
            boto3 Comprehend client
        """
        if self._client is None:
            self._client = boto3.client("comprehend", region_name=self._region_name)
        return self._client

    async def detect_and_redact(self, text: str, language_code: str = "en") -> PiiDetectionResult:
        """Detect and redact PII from text asynchronously.

        Args:
            text: Text to analyze for PII
            language_code: Language code (default: "en")

        Returns:
            PiiDetectionResult with redacted text and metadata
        """
        if not text:
            return PiiDetectionResult(content="")

        # Truncate if too long
        if len(text) > self.MAX_TEXT_SIZE:
            logger.warning(f"Text truncated from {len(text)} to {self.MAX_TEXT_SIZE} bytes for Comprehend")
            text = text[: self.MAX_TEXT_SIZE]

        try:
            # Run synchronous Comprehend call in thread pool
            result = await asyncio.to_thread(self._detect_pii_sync, text, language_code)
            return result
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = f"Comprehend API error: {error_code}"
            logger.error(error_msg, extra={"error": str(e)})
            return PiiDetectionResult(content=text, error=error_msg)
        except Exception as e:
            error_msg = f"PII detection error: {str(e)}"
            logger.error(error_msg)
            return PiiDetectionResult(content=text, error=error_msg)

    def _detect_pii_sync(self, text: str, language_code: str) -> PiiDetectionResult:
        """Synchronous PII detection (runs in thread pool).

        Args:
            text: Text to analyze
            language_code: Language code

        Returns:
            PiiDetectionResult with redacted text
        """
        client = self._get_client()

        # Call Comprehend DetectPiiEntities
        response = client.detect_pii_entities(
            Text=text,
            LanguageCode=language_code,
        )

        entities = response.get("Entities", [])

        # Filter entities by type and confidence
        filtered_entities = [
            entity for entity in entities if entity.get("Type") in self._pii_types_to_redact and entity.get("Score", 0) >= self._confidence_threshold
        ]

        if not filtered_entities:
            return PiiDetectionResult(content=text, entities=entities)

        # Sort by start offset (descending) to replace from end to start
        filtered_entities.sort(key=lambda x: x.get("BeginOffset", 0), reverse=True)

        # Redact PII entities
        redacted_text = text
        pii_types_found: set[str] = set()

        for entity in filtered_entities:
            entity_type = entity.get("Type", "UNKNOWN")
            begin = entity.get("BeginOffset", 0)
            end = entity.get("EndOffset", 0)

            # Map Comprehend type to display name
            display_type = PII_ENTITY_TYPES.get(entity_type, entity_type)
            replacement = f"[PII:{display_type}]"

            redacted_text = redacted_text[:begin] + replacement + redacted_text[end:]
            pii_types_found.add(display_type)

        return PiiDetectionResult(
            content=redacted_text,
            redactions_count=len(filtered_entities),
            pii_types_found=list(pii_types_found),
            entities=[
                {
                    "type": e.get("Type"),
                    "score": e.get("Score"),
                    "begin": e.get("BeginOffset"),
                    "end": e.get("EndOffset"),
                }
                for e in filtered_entities
            ],
        )

    async def detect_and_redact_dict(
        self,
        data: dict[str, Any],
        language_code: str = "en",
        depth: int = 0,
        max_depth: int = 10,
    ) -> tuple[dict[str, Any], PiiDetectionResult]:
        """Recursively detect and redact PII from a dictionary.

        Args:
            data: Dictionary to process
            language_code: Language code
            depth: Current recursion depth
            max_depth: Maximum recursion depth

        Returns:
            Tuple of (redacted_dict, combined_result)
        """
        if depth > max_depth:
            return data, PiiDetectionResult(content="")

        redacted = {}
        total_redactions = 0
        all_pii_types: set[str] = set()

        for key, value in data.items():
            if isinstance(value, str) and len(value) > 10:  # Skip very short strings
                result = await self.detect_and_redact(value, language_code)
                redacted[key] = result.content
                total_redactions += result.redactions_count
                all_pii_types.update(result.pii_types_found)
            elif isinstance(value, dict):
                nested_data, result = await self.detect_and_redact_dict(value, language_code, depth + 1, max_depth)
                redacted[key] = nested_data
                total_redactions += result.redactions_count
                all_pii_types.update(result.pii_types_found)
            elif isinstance(value, list):
                nested_list, result = await self._process_list(value, language_code, depth + 1, max_depth)
                redacted[key] = nested_list
                total_redactions += result.redactions_count
                all_pii_types.update(result.pii_types_found)
            else:
                redacted[key] = value

        combined_result = PiiDetectionResult(
            content="",
            redactions_count=total_redactions,
            pii_types_found=list(all_pii_types),
        )

        return redacted, combined_result

    async def _process_list(self, data: list[Any], language_code: str, depth: int, max_depth: int) -> tuple[list[Any], PiiDetectionResult]:
        """Process a list for PII detection.

        Args:
            data: List to process
            language_code: Language code
            depth: Current recursion depth
            max_depth: Maximum recursion depth

        Returns:
            Tuple of (redacted_list, combined_result)
        """
        if depth > max_depth:
            return data, PiiDetectionResult(content="")

        redacted = []
        total_redactions = 0
        all_pii_types: set[str] = set()

        for item in data:
            if isinstance(item, str) and len(item) > 10:
                result = await self.detect_and_redact(item, language_code)
                redacted.append(result.content)
                total_redactions += result.redactions_count
                all_pii_types.update(result.pii_types_found)
            elif isinstance(item, dict):
                nested_data, result = await self.detect_and_redact_dict(item, language_code, depth + 1, max_depth)
                redacted.append(nested_data)
                total_redactions += result.redactions_count
                all_pii_types.update(result.pii_types_found)
            elif isinstance(item, list):
                nested_list, result = await self._process_list(item, language_code, depth + 1, max_depth)
                redacted.append(nested_list)
                total_redactions += result.redactions_count
                all_pii_types.update(result.pii_types_found)
            else:
                redacted.append(item)

        combined_result = PiiDetectionResult(
            content="",
            redactions_count=total_redactions,
            pii_types_found=list(all_pii_types),
        )

        return redacted, combined_result
