"""Pool-specific exceptions for Bedrock account management."""


class PoolError(Exception):
    """Base exception for pool-related errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class AllAccountsUnhealthyError(PoolError):
    """Raised when all accounts in the pool are unhealthy (US-9.4)."""

    def __init__(self, message: str = "All Bedrock accounts are currently unavailable"):
        super().__init__(message)


class RoleAssumptionError(PoolError):
    """Raised when STS AssumeRole fails."""

    def __init__(self, role_arn: str, reason: str):
        self.role_arn = role_arn
        self.reason = reason
        super().__init__(f"Failed to assume role {role_arn}: {reason}")


class PoolExhaustedError(PoolError):
    """Raised when all accounts have been tried and failed for a request."""

    def __init__(self, attempts: int):
        self.attempts = attempts
        super().__init__(f"All {attempts} pool accounts failed to serve the request")


class ClientCreationError(PoolError):
    """Raised when unable to create a Bedrock client."""

    def __init__(self, account_id: str, reason: str):
        self.account_id = account_id
        self.reason = reason
        super().__init__(f"Failed to create Bedrock client for account {account_id}: {reason}")


class HealthCheckError(PoolError):
    """Raised when a health check fails."""

    def __init__(self, account_id: str, reason: str):
        self.account_id = account_id
        self.reason = reason
        super().__init__(f"Health check failed for account {account_id}: {reason}")


class CredentialExpiredError(PoolError):
    """Raised when cached credentials have expired and refresh fails."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        super().__init__(f"Credentials expired for account {account_id} and could not be refreshed")


class NoAccountsConfiguredError(PoolError):
    """Raised when no accounts are configured in the pool."""

    def __init__(self):
        super().__init__("No Bedrock accounts are configured in the pool")
