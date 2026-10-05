"""Shared Browser transport error contract."""

class BrowserBrokerError(RuntimeError):
    """Raised when the trusted browser broker cannot complete an analysis."""

    def __init__(
        self,
        message,
        *,
        code="broker_unavailable",
        retry_after=None,
        cleanup=None,
        browser_start_unattempted=False,
    ):
        super().__init__(message)
        self.code, self.retry_after, self.cleanup = code, retry_after, cleanup
        self.browser_start_unattempted = browser_start_unattempted

