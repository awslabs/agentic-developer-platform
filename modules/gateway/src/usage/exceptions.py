"""Usage module custom exceptions."""

from src.shared.exceptions import BedrockGatewayError


class UsageQueryError(BedrockGatewayError):
    """Raised when there is an error querying usage data."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            error="usage_query_error",
            message=message,
            status_code=400,
            details=details,
        )


class UsageRecordError(BedrockGatewayError):
    """Raised when there is an error recording usage data."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            error="usage_record_error",
            message=message,
            status_code=500,
            details=details,
        )


class InvalidTimeRangeError(BedrockGatewayError):
    """Raised when an invalid time range is specified."""

    def __init__(self, message: str = "Invalid time range"):
        super().__init__(
            error="invalid_time_range",
            message=message,
            status_code=400,
        )


class UsageDataNotFoundError(BedrockGatewayError):
    """Raised when requested usage data is not found."""

    def __init__(self, entity_type: str, entity_id: str):
        super().__init__(
            error="usage_not_found",
            message=f"No usage data found for {entity_type} '{entity_id}'",
            status_code=404,
            details={"entity_type": entity_type, "entity_id": entity_id},
        )
