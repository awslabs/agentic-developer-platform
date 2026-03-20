class BedrockGatewayError(Exception):
    def __init__(self, error: str, message: str, status_code: int = 500, details: dict | None = None):
        self.error = error
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class InvalidCredentialsError(BedrockGatewayError):
    def __init__(self, message: str = "Invalid or expired AWS credentials"):
        super().__init__("invalid_credentials", message, 401)


class UnknownOrganizationError(BedrockGatewayError):
    def __init__(self, account_id: str):
        super().__init__(
            "unknown_organization", f"AWS account {account_id} is not registered with any organization. Contact your platform administrator.", 403
        )


class UnregisteredServiceAccountError(BedrockGatewayError):
    def __init__(self, role_arn: str | None = None):
        super().__init__("unregistered_service_account", "Agent not registered. Contact your org administrator.", 403)


class TokenExpiredError(BedrockGatewayError):
    def __init__(self):
        super().__init__("token_expired", "Token has expired. Please re-authenticate.", 401)


class BudgetExceededError(BedrockGatewayError):
    def __init__(self, level: str, entity: str, budget_usd: float, spent_usd: float, period: str, resets_at: str):
        super().__init__(
            "budget_exceeded",
            f"Budget exceeded at {level} level",
            429,
            {"level": level, "entity": entity, "budget_usd": budget_usd, "spent_usd": spent_usd, "period": period, "resets_at": resets_at},
        )


class RateLimitExceededError(BedrockGatewayError):
    def __init__(self, limit_type: str, limit: int, retry_after: int):
        super().__init__(
            "rate_limited", f"Rate limit exceeded: {limit_type}", 429, {"type": limit_type, "limit": limit, "retry_after_seconds": retry_after}
        )


class ModelNotAllowedError(BedrockGatewayError):
    def __init__(self, model: str, allowed_models: list[str]):
        super().__init__(
            "model_not_allowed", "Your team does not have access to this model.", 403, {"model": model, "allowed_models": allowed_models}
        )


class NoHealthyAccountsError(BedrockGatewayError):
    def __init__(self):
        super().__init__(
            "service_unavailable", "All Bedrock accounts are currently unavailable. Please try again later or contact platform support.", 503
        )


class ForbiddenError(BedrockGatewayError):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__("forbidden", message, 403)


class ValidationError(BedrockGatewayError):
    def __init__(self, message: str = "Validation error"):
        super().__init__("validation_error", message, 400)


class NotFoundError(BedrockGatewayError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__("not_found", message, 404)


class ConflictError(BedrockGatewayError):
    """Resource conflict (e.g., duplicate unique constraint)."""

    def __init__(self, message: str = "Resource conflict"):
        super().__init__("conflict", message, 409)
