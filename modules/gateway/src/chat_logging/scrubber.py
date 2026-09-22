"""Sensitive data scrubbing for chat logs.

Issue #143: Implements header scrubbing and regex-based secret detection.

Scrubbing Pipeline:
1. Header scrubbing - removes Authorization, X-Api-Key, Cookie headers
2. Regex-based secret detection - redacts AWS keys, JWTs, passwords, etc.
3. (Optional) Comprehend PII detection - handled by comprehend_client.py
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ScrubResult:
    """Result of a scrubbing operation."""

    content: Any  # Scrubbed content
    redactions_count: int = 0
    patterns_matched: list[str] = field(default_factory=list)
    headers_scrubbed: list[str] = field(default_factory=list)


# =============================================================================
# Regex Patterns for Secret Detection
# =============================================================================

# Pattern definitions with names for tracking
REGEX_PATTERNS: dict[str, tuple[str, str]] = {
    # AWS Access Keys: AKIA followed by 16 alphanumeric characters
    "aws_access_key": (r"AKIA[0-9A-Z]{16}", "[REDACTED:AWS_ACCESS_KEY]"),
    # AWS Secret Keys: 40-character base64-like strings (near access key context)
    # This pattern is more specific to avoid false positives
    "aws_secret_key": (
        r"(?:aws_secret_access_key|secret_key|secretkey|aws_secret)\s*[=:]\s*['\"]?([A-Za-z0-9+/]{40})['\"]?",
        "[REDACTED:AWS_SECRET_KEY]",
    ),
    # Generic 40-char base64 strings that look like AWS secret keys
    "aws_secret_key_pattern": (r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/])", "[REDACTED:POSSIBLE_SECRET_KEY]"),
    # JWT tokens: eyJ... format (base64 encoded JSON with header.payload.signature)
    "jwt_token": (r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*", "[REDACTED:JWT_TOKEN]"),
    # Private keys: BEGIN ... PRIVATE KEY blocks
    "private_key": (
        r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"
        r"[\s\S]*?"
        r"-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
        "[REDACTED:PRIVATE_KEY]",
    ),
    # PostgreSQL connection strings
    "postgresql_uri": (r"postgresql(?:\+\w+)?://[^\s\"'<>]+", "[REDACTED:CONNECTION_STRING]"),
    # Redis connection strings
    "redis_uri": (r"redis(?:s)?://[^\s\"'<>]+", "[REDACTED:CONNECTION_STRING]"),
    # MongoDB connection strings
    "mongodb_uri": (r"mongodb(?:\+srv)?://[^\s\"'<>]+", "[REDACTED:CONNECTION_STRING]"),
    # MySQL connection strings
    "mysql_uri": (r"mysql(?:\+\w+)?://[^\s\"'<>]+", "[REDACTED:CONNECTION_STRING]"),
    # Generic password patterns
    "password_pattern": (r"(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{4,}['\"]?", "[REDACTED:PASSWORD]"),
    # Secret patterns
    "secret_pattern": (r"(?:secret|api_secret|client_secret)\s*[=:]\s*['\"]?[^\s'\"]{4,}['\"]?", "[REDACTED:SECRET]"),
    # Token patterns (generic)
    "token_pattern": (r"(?:token|auth_token|access_token|refresh_token)\s*[=:]\s*['\"]?[^\s'\"]{8,}['\"]?", "[REDACTED:TOKEN]"),
    # API key patterns: sk-, pk_, ghp_, ghs_, etc.
    "sk_api_key": (r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED:API_KEY]"),
    "pk_api_key": (r"pk_(?:live_|test_)?[A-Za-z0-9_-]{20,}", "[REDACTED:API_KEY]"),
    "github_pat": (r"ghp_[A-Za-z0-9]{36,}", "[REDACTED:GITHUB_PAT]"),
    "github_oauth": (r"gho_[A-Za-z0-9]{36,}", "[REDACTED:GITHUB_TOKEN]"),
    "github_server": (r"ghs_[A-Za-z0-9]{36,}", "[REDACTED:GITHUB_TOKEN]"),
    "github_refresh": (r"ghr_[A-Za-z0-9]{36,}", "[REDACTED:GITHUB_TOKEN]"),
    # Slack tokens
    "slack_token": (r"xox[baprs]-[A-Za-z0-9-]{10,}", "[REDACTED:SLACK_TOKEN]"),
    # Bearer tokens in Authorization-like patterns
    "bearer_token": (r"Bearer\s+[A-Za-z0-9_.-]+", "[REDACTED:BEARER_TOKEN]"),
}

# =============================================================================
# Personal-data patterns (Issue #5672)
# =============================================================================
# The regex layer is the ONLY layer that always runs: it needs no AWS API call,
# no entitlement and no quota. Comprehend PII detection (ScrubLevel.STANDARD) is
# strictly additive on top of it, and it can be unavailable — the service may not
# be enabled in an account, the workload role may lack comprehend:DetectPiiEntities,
# or the account may be out of quota. Before this change the regex layer covered
# credential shapes only, so any environment where Comprehend was unavailable
# stored personal data entirely in the clear while reporting itself protected.
#
# These patterns close that gap for the categories that have a reliable textual
# shape. They are defence in depth, NOT a replacement for Comprehend.
#
# DELIBERATELY NOT COVERED HERE: person names and free-form postal addresses.
# Neither has a shape a regex can recognise without either missing most real
# values or redacting ordinary prose. Those two categories depend on Comprehend,
# which is why ScrubLevel.STANDARD is the default and why a failed Comprehend
# pass is logged as an error (see service.py) rather than a warning.
#
# Ordering note: card-number patterns are registered BEFORE the phone pattern so
# a formatted card ("4111 1111 1111 1111") is consumed as a card rather than
# partially matched as a phone number.
PII_REGEX_PATTERNS: dict[str, tuple[str, str]] = {
    # Email addresses
    "email_address": (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", "[REDACTED:EMAIL]"),
    # National identifiers. The bare form is the US SSN's dashed/spaced shape,
    # which is distinctive enough to match on its own. Nine contiguous digits are
    # NOT matched bare — far too common in ordinary text (ids, timestamps,
    # quantities) — so that form is only redacted when labelled.
    "national_id": (r"\b\d{3}[- ]\d{2}[- ]\d{4}\b", "[REDACTED:NATIONAL_ID]"),
    "national_id_labelled": (
        r"(?:ssn|social[\s_-]?security(?:[\s_-]?(?:number|no\.?|#))?|national[\s_-]?id(?:entifier)?|nino|sin)"
        r"\s*[:=#]?\s*['\"]?\d{3}[- ]?\d{2}[- ]?\d{4}['\"]?",
        "[REDACTED:NATIONAL_ID]",
    ),
    # Payment card numbers, major issuers, contiguous form (Visa, Mastercard,
    # Amex, Discover, Diners, JCB). Prefix-anchored rather than "13-19 digits"
    # to keep ordinary long numbers out of the match.
    "payment_card": (
        r"\b(?:4[0-9]{12}(?:[0-9]{3})?"
        r"|5[1-5][0-9]{14}"
        r"|2(?:2[2-9][0-9]|[3-6][0-9]{2}|7[01][0-9]|720)[0-9]{12}"
        r"|3[47][0-9]{13}"
        r"|3(?:0[0-5]|[68][0-9])[0-9]{11}"
        r"|6(?:011|5[0-9]{2})[0-9]{12}"
        r"|35(?:2[89]|[3-8][0-9])[0-9]{12})\b",
        "[REDACTED:PAYMENT_CARD]",
    ),
    # Payment card numbers in the grouped form people actually paste
    # ("4111 1111 1111 1111", "4111-1111-1111-1111", and the 4-6-5 Amex form).
    "payment_card_grouped": (r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,4}\b|\b\d{4}[ -]\d{6}[ -]\d{5}\b", "[REDACTED:PAYMENT_CARD]"),
    # Bank account and routing numbers. Both are plain digit runs with no
    # distinctive shape, so these are label-anchored only — a bare 9-digit run is
    # not evidence of a routing number.
    "bank_routing": (
        r"(?:routing(?:[\s_-]?(?:number|no\.?|#))?|aba(?:[\s_-]?(?:number|no\.?|#))?|sort[\s_-]?code)"
        r"\s*[:=#]?\s*['\"]?\d{6,9}['\"]?",
        "[REDACTED:BANK_ROUTING]",
    ),
    "bank_account": (
        r"(?:bank[\s_-]?account|account[\s_-]?(?:number|no\.?|#))\s*[:=#]?\s*['\"]?[0-9]{6,26}['\"]?",
        "[REDACTED:BANK_ACCOUNT]",
    ),
    # IBANs carry their own distinctive shape (2-letter country + 2 check digits +
    # 11-30 alphanumerics), so unlike a bare account number they can be matched
    # without a label. Minimum length follows the shortest real IBAN (Norway, 15).
    "iban": (r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "[REDACTED:BANK_ACCOUNT]"),
    # Phone numbers, as three explicit shapes rather than one loose digit-group
    # rule. A generic "digits separator digits separator digits" pattern also
    # matches ISO dates (2026-09-22) and version-like triples, which would redact
    # ordinary prose and destroy the operational value of the transcript.
    #   1. international, leading "+" country code
    #   2. parenthesised area code
    #   3. bare 3-3-4 / 3-4-4 national form — deliberately NOT 4-2-2 or 2-2-4,
    #      the shapes ISO and US/EU dates take
    "phone_number": (
        r"(?<![\w.+-])(?:"
        r"\+\d{1,3}[ .-]?(?:\(?\d{2,4}\)?[ .-]?){1,4}\d{2,4}"
        r"|\(\d{2,5}\)[ .-]?\d{2,4}[ .-]?\d{2,4}"
        r"|\d{3}[ .-]\d{3,4}[ .-]\d{4}"
        r")(?![\w-])",
        "[REDACTED:PHONE]",
    ),
}

# The always-on layer is credential patterns PLUS personal-data patterns.
# Registration order is preserved (Python dicts are ordered) and matters: see the
# ordering note above.
REGEX_PATTERNS.update(PII_REGEX_PATTERNS)

_SENSITIVE_NUMERIC_FIELDS: dict[str, tuple[str, str, str]] = {
    "ssn": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "socialsecurity": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "socialsecuritynumber": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "nationalid": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "nationalidentifier": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "nino": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "sin": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "taxid": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "taxpayerid": ("[REDACTED:NATIONAL_ID]", "numeric_national_id_field", "SSN"),
    "card": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "cardnumber": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "creditcard": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "creditcardnumber": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "debitcard": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "debitcardnumber": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "paymentcard": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "paymentcardnumber": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "pan": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "cvv": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "cvc": ("[REDACTED:PAYMENT_CARD]", "numeric_payment_card_field", "CREDIT_CARD"),
    "bankaccount": ("[REDACTED:BANK_ACCOUNT]", "numeric_bank_account_field", "BANK_ACCOUNT"),
    "bankaccountnumber": ("[REDACTED:BANK_ACCOUNT]", "numeric_bank_account_field", "BANK_ACCOUNT"),
    "routingnumber": ("[REDACTED:BANK_ROUTING]", "numeric_bank_routing_field", "BANK_ROUTING"),
    "abanumber": ("[REDACTED:BANK_ROUTING]", "numeric_bank_routing_field", "BANK_ROUTING"),
    "sortcode": ("[REDACTED:BANK_ROUTING]", "numeric_bank_routing_field", "BANK_ROUTING"),
    "phone": ("[REDACTED:PHONE]", "numeric_phone_field", "PHONE"),
    "phonenumber": ("[REDACTED:PHONE]", "numeric_phone_field", "PHONE"),
    "telephone": ("[REDACTED:PHONE]", "numeric_phone_field", "PHONE"),
    "mobile": ("[REDACTED:PHONE]", "numeric_phone_field", "PHONE"),
    "mobilenumber": ("[REDACTED:PHONE]", "numeric_phone_field", "PHONE"),
}
_TRANSPARENT_NUMERIC_PATH_PARTS = {"content", "data", "raw", "value", "values"}

_NUMERIC_SHAPE_PATTERNS = {
    name: (re.compile(pattern, re.IGNORECASE), replacement) for name, (pattern, replacement) in PII_REGEX_PATTERNS.items() if name == "payment_card"
}


def redact_numeric_value(value: Any, path: tuple[str, ...]) -> tuple[str, str, str] | None:
    """Return a redaction for a sensitive numeric leaf, or None for operational data."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None

    for path_part in reversed(path):
        normalized_part = re.sub(r"[^a-z0-9]", "", str(path_part).lower())
        if redaction := _SENSITIVE_NUMERIC_FIELDS.get(normalized_part):
            return redaction
        if normalized_part not in _TRANSPARENT_NUMERIC_PATH_PARTS:
            break

    text = str(value)
    for pattern_name, (pattern, replacement) in _NUMERIC_SHAPE_PATTERNS.items():
        if pattern.fullmatch(text):
            return replacement, f"numeric_{pattern_name}", "CREDIT_CARD"

    return None


# Headers to scrub from logs
SENSITIVE_HEADERS = {
    "authorization",
    "x-adp-report-credential",
    "x-api-key",
    "cookie",
    "set-cookie",
    "x-auth-token",
    "x-access-token",
    "proxy-authorization",
    "www-authenticate",
}


class HeaderScrubber:
    """Scrubs sensitive headers from request/response dictionaries."""

    def __init__(self, sensitive_headers: set[str] | None = None) -> None:
        """Initialize the header scrubber.

        Args:
            sensitive_headers: Set of header names to scrub (case-insensitive).
                              Defaults to SENSITIVE_HEADERS.
        """
        self._sensitive_headers = sensitive_headers or SENSITIVE_HEADERS

    def scrub(self, headers: dict[str, str] | None) -> ScrubResult:
        """Scrub sensitive headers from a headers dictionary.

        Args:
            headers: Dictionary of headers to scrub

        Returns:
            ScrubResult with scrubbed headers and metadata
        """
        if not headers:
            return ScrubResult(content={})

        scrubbed = {}
        scrubbed_list = []

        for key, value in headers.items():
            if key.lower() in self._sensitive_headers:
                scrubbed[key] = "[REDACTED:HEADER]"
                scrubbed_list.append(key)
            else:
                scrubbed[key] = value

        return ScrubResult(
            content=scrubbed,
            redactions_count=len(scrubbed_list),
            headers_scrubbed=scrubbed_list,
        )


class RegexScrubber:
    """Scrubs sensitive data using regex patterns."""

    def __init__(self, patterns: dict[str, tuple[str, str]] | None = None) -> None:
        """Initialize the regex scrubber.

        Args:
            patterns: Dictionary of pattern_name -> (regex, replacement).
                     Defaults to REGEX_PATTERNS.
        """
        self._patterns = patterns or REGEX_PATTERNS
        # Compile patterns for efficiency
        self._compiled: dict[str, tuple[re.Pattern, str]] = {}
        for name, (pattern, replacement) in self._patterns.items():
            try:
                self._compiled[name] = (re.compile(pattern, re.IGNORECASE | re.MULTILINE), replacement)
            except re.error as e:
                logger.warning(f"Failed to compile pattern '{name}': {e}")

    def scrub_text(self, text: str) -> ScrubResult:
        """Scrub sensitive data from a text string.

        Args:
            text: Text to scrub

        Returns:
            ScrubResult with scrubbed text and metadata
        """
        if not text:
            return ScrubResult(content="")

        scrubbed = text
        redactions = 0
        patterns_matched: list[str] = []

        for name, (pattern, replacement) in self._compiled.items():
            matches = pattern.findall(scrubbed)
            if matches:
                count = len(matches)
                scrubbed = pattern.sub(replacement, scrubbed)
                redactions += count
                patterns_matched.append(name)

        return ScrubResult(
            content=scrubbed,
            redactions_count=redactions,
            patterns_matched=patterns_matched,
        )

    def scrub_dict(
        self,
        data: dict[str, Any],
        depth: int = 0,
        max_depth: int = 10,
        path: tuple[str, ...] = (),
    ) -> ScrubResult:
        """Recursively scrub sensitive data from a dictionary.

        Args:
            data: Dictionary to scrub
            depth: Current recursion depth
            max_depth: Maximum recursion depth to prevent infinite loops

        Returns:
            ScrubResult with scrubbed dictionary and metadata
        """
        if depth > max_depth:
            return ScrubResult(content=data)

        scrubbed = {}
        total_redactions = 0
        all_patterns: list[str] = []

        for key, value in data.items():
            value_path = (*path, str(key))
            if isinstance(value, str):
                result = self.scrub_text(value)
                scrubbed[key] = result.content
                total_redactions += result.redactions_count
                all_patterns.extend(result.patterns_matched)
            elif isinstance(value, dict):
                result = self.scrub_dict(value, depth + 1, max_depth, value_path)
                scrubbed[key] = result.content
                total_redactions += result.redactions_count
                all_patterns.extend(result.patterns_matched)
            elif isinstance(value, list):
                result = self.scrub_list(value, depth + 1, max_depth, value_path)
                scrubbed[key] = result.content
                total_redactions += result.redactions_count
                all_patterns.extend(result.patterns_matched)
            elif redaction := redact_numeric_value(value, value_path):
                scrubbed[key] = redaction[0]
                total_redactions += 1
                all_patterns.append(redaction[1])
            else:
                scrubbed[key] = value

        return ScrubResult(
            content=scrubbed,
            redactions_count=total_redactions,
            patterns_matched=list(set(all_patterns)),  # Deduplicate
        )

    def scrub_list(
        self,
        data: list[Any],
        depth: int = 0,
        max_depth: int = 10,
        path: tuple[str, ...] = (),
    ) -> ScrubResult:
        """Recursively scrub sensitive data from a list.

        Args:
            data: List to scrub
            depth: Current recursion depth
            max_depth: Maximum recursion depth

        Returns:
            ScrubResult with scrubbed list and metadata
        """
        if depth > max_depth:
            return ScrubResult(content=data)

        scrubbed = []
        total_redactions = 0
        all_patterns: list[str] = []

        for item in data:
            if isinstance(item, str):
                result = self.scrub_text(item)
                scrubbed.append(result.content)
                total_redactions += result.redactions_count
                all_patterns.extend(result.patterns_matched)
            elif isinstance(item, dict):
                result = self.scrub_dict(item, depth + 1, max_depth, path)
                scrubbed.append(result.content)
                total_redactions += result.redactions_count
                all_patterns.extend(result.patterns_matched)
            elif isinstance(item, list):
                result = self.scrub_list(item, depth + 1, max_depth, path)
                scrubbed.append(result.content)
                total_redactions += result.redactions_count
                all_patterns.extend(result.patterns_matched)
            elif redaction := redact_numeric_value(item, path):
                scrubbed.append(redaction[0])
                total_redactions += 1
                all_patterns.append(redaction[1])
            else:
                scrubbed.append(item)

        return ScrubResult(
            content=scrubbed,
            redactions_count=total_redactions,
            patterns_matched=list(set(all_patterns)),
        )


class ScrubPipeline:
    """Orchestrates the full scrubbing pipeline.

    Pipeline order:
    1. Header scrubbing
    2. Regex-based secret detection
    3. (Optional) Comprehend PII detection - handled externally
    """

    def __init__(
        self,
        header_scrubber: HeaderScrubber | None = None,
        regex_scrubber: RegexScrubber | None = None,
    ) -> None:
        """Initialize the scrub pipeline.

        Args:
            header_scrubber: Custom header scrubber instance
            regex_scrubber: Custom regex scrubber instance
        """
        self._header_scrubber = header_scrubber or HeaderScrubber()
        self._regex_scrubber = regex_scrubber or RegexScrubber()

    def scrub_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], ScrubResult]:
        """Scrub a request body and headers.

        Args:
            request_body: Request body dictionary
            headers: Optional headers dictionary

        Returns:
            Tuple of (scrubbed_body, combined_result)
        """
        total_redactions = 0
        all_patterns: list[str] = []
        all_headers: list[str] = []

        # Scrub headers if provided
        if headers:
            header_result = self._header_scrubber.scrub(headers)
            total_redactions += header_result.redactions_count
            all_headers.extend(header_result.headers_scrubbed)

        # Scrub request body
        body_result = self._regex_scrubber.scrub_dict(request_body)
        scrubbed_body = body_result.content
        total_redactions += body_result.redactions_count
        all_patterns.extend(body_result.patterns_matched)

        combined_result = ScrubResult(
            content=scrubbed_body,
            redactions_count=total_redactions,
            patterns_matched=list(set(all_patterns)),
            headers_scrubbed=all_headers,
        )

        return scrubbed_body, combined_result

    def scrub_response(self, response_body: dict[str, Any]) -> tuple[dict[str, Any], ScrubResult]:
        """Scrub a response body.

        Args:
            response_body: Response body dictionary

        Returns:
            Tuple of (scrubbed_body, result)
        """
        result = self._regex_scrubber.scrub_dict(response_body)
        return result.content, result

    def scrub_text(self, text: str) -> tuple[str, ScrubResult]:
        """Scrub a text string.

        Args:
            text: Text to scrub

        Returns:
            Tuple of (scrubbed_text, result)
        """
        result = self._regex_scrubber.scrub_text(text)
        return result.content, result
