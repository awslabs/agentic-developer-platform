"""
Response Headers Utilities.

This module provides utilities for generating and injecting response headers
for budget and rate limit information.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Response

from src.shared.schemas.budget import BudgetStatusResponse, EnforcementResult
from src.shared.schemas.common import RateLimitCheckResult


class ResponseHeadersService:
    """
    Service for generating and injecting response headers.

    Provides standardized headers for:
    - Budget information (X-Budget-*)
    - Rate limit information (X-RateLimit-*)
    - Warning messages
    - Retry-After for 429 responses
    """

    # Budget headers
    BUDGET_LIMIT = "X-Budget-Limit"
    BUDGET_REMAINING = "X-Budget-Remaining"
    BUDGET_RESET = "X-Budget-Reset"
    BUDGET_WARNING = "X-Budget-Warning"

    # Rate limit headers
    RATELIMIT_LIMIT = "X-RateLimit-Limit"
    RATELIMIT_REMAINING = "X-RateLimit-Remaining"
    RATELIMIT_RESET = "X-RateLimit-Reset"
    RETRY_AFTER = "Retry-After"

    @staticmethod
    def budget_headers(
        budget_limit: Decimal | float | None = None,
        budget_remaining: Decimal | float | None = None,
        budget_reset: date | datetime | str | None = None,
    ) -> dict[str, str]:
        """
        Generate X-Budget-* headers.

        Args:
            budget_limit: Budget limit in USD
            budget_remaining: Remaining budget in USD
            budget_reset: Period reset date

        Returns:
            Dict of header name -> value
        """
        headers = {}

        if budget_limit is not None:
            headers[ResponseHeadersService.BUDGET_LIMIT] = f"{float(budget_limit):.2f}"

        if budget_remaining is not None:
            headers[ResponseHeadersService.BUDGET_REMAINING] = f"{float(budget_remaining):.2f}"

        if budget_reset is not None:
            if isinstance(budget_reset, date | datetime):
                headers[ResponseHeadersService.BUDGET_RESET] = budget_reset.isoformat()
            else:
                headers[ResponseHeadersService.BUDGET_RESET] = str(budget_reset)

        return headers

    @staticmethod
    def budget_headers_from_status(status: BudgetStatusResponse) -> dict[str, str]:
        """
        Generate budget headers from a BudgetStatusResponse.

        Args:
            status: Budget status response object

        Returns:
            Dict of header name -> value
        """
        return ResponseHeadersService.budget_headers(
            budget_limit=status.budget_amount_usd,
            budget_remaining=status.remaining_budget_usd,
            budget_reset=status.period_end,
        )

    @staticmethod
    def budget_headers_from_enforcement(result: EnforcementResult) -> dict[str, str]:
        """
        Generate budget headers from an EnforcementResult.

        Args:
            result: Enforcement result object

        Returns:
            Dict of header name -> value
        """
        headers = {}

        if result.budget_amount_usd is not None:
            headers[ResponseHeadersService.BUDGET_LIMIT] = f"{float(result.budget_amount_usd):.2f}"

        if result.current_spend_usd is not None and result.budget_amount_usd is not None:
            remaining = result.budget_amount_usd - result.current_spend_usd
            headers[ResponseHeadersService.BUDGET_REMAINING] = f"{max(0, float(remaining)):.2f}"

        return headers

    @staticmethod
    def rate_limit_headers(
        limit: int | None = None,
        remaining: int | None = None,
        reset_seconds: int | None = None,
    ) -> dict[str, str]:
        """
        Generate X-RateLimit-* headers.

        Args:
            limit: Rate limit value
            remaining: Remaining requests/tokens
            reset_seconds: Seconds until reset (Unix timestamp or relative)

        Returns:
            Dict of header name -> value
        """
        headers = {}

        if limit is not None:
            headers[ResponseHeadersService.RATELIMIT_LIMIT] = str(limit)

        if remaining is not None:
            headers[ResponseHeadersService.RATELIMIT_REMAINING] = str(remaining)

        if reset_seconds is not None:
            headers[ResponseHeadersService.RATELIMIT_RESET] = str(reset_seconds)

        return headers

    @staticmethod
    def rate_limit_headers_from_result(result: RateLimitCheckResult) -> dict[str, str]:
        """
        Generate rate limit headers from a RateLimitCheckResult.

        Args:
            result: Rate limit check result object

        Returns:
            Dict of header name -> value
        """
        return ResponseHeadersService.rate_limit_headers(
            limit=result.limit,
            remaining=result.remaining,
            reset_seconds=result.retry_after_seconds,
        )

    @staticmethod
    def warning_headers(warnings: list[str]) -> dict[str, str]:
        """
        Generate warning headers.

        Multiple warnings are combined into a single header with semicolon separator.

        Args:
            warnings: List of warning messages

        Returns:
            Dict of header name -> value
        """
        if not warnings:
            return {}

        # Combine multiple warnings
        combined_warning = "; ".join(warnings)
        return {ResponseHeadersService.BUDGET_WARNING: combined_warning}

    @staticmethod
    def retry_after_header(seconds: int) -> dict[str, str]:
        """
        Generate Retry-After header.

        Args:
            seconds: Number of seconds until retry

        Returns:
            Dict with Retry-After header
        """
        return {ResponseHeadersService.RETRY_AFTER: str(seconds)}

    @staticmethod
    def inject_headers(response: Response, headers: dict[str, str]) -> Response:
        """
        Inject headers into a response object.

        Args:
            response: Response to modify
            headers: Headers to add

        Returns:
            Modified response
        """
        for name, value in headers.items():
            response.headers[name] = value
        return response

    @staticmethod
    def merge_headers(*header_dicts: dict[str, str]) -> dict[str, str]:
        """
        Merge multiple header dictionaries.

        Later dictionaries override earlier ones for duplicate keys.

        Args:
            *header_dicts: Header dictionaries to merge

        Returns:
            Merged dictionary
        """
        result = {}
        for headers in header_dicts:
            result.update(headers)
        return result


def format_budget_for_headers(budget_status: dict[str, Any]) -> dict[str, str]:
    """
    Format budget status dictionary for response headers.

    Args:
        budget_status: Budget status from enforcement service

    Returns:
        Dict of headers
    """
    return ResponseHeadersService.budget_headers(
        budget_limit=budget_status.get("budget_limit"),
        budget_remaining=budget_status.get("budget_remaining"),
        budget_reset=budget_status.get("budget_reset"),
    )


def format_rate_limit_for_headers(
    limit: int | None = None,
    remaining: int | None = None,
    reset_seconds: int | None = None,
) -> dict[str, str]:
    """
    Format rate limit info for response headers.

    Args:
        limit: Rate limit value
        remaining: Remaining allowance
        reset_seconds: Seconds until reset

    Returns:
        Dict of headers
    """
    return ResponseHeadersService.rate_limit_headers(
        limit=limit,
        remaining=remaining,
        reset_seconds=reset_seconds,
    )
