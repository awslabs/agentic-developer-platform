from abc import ABC, abstractmethod
from typing import Any

from src.shared.schemas.auth import TokenContext


class IUsageService(ABC):
    @abstractmethod
    async def log_request(
        self,
        context: TokenContext,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        status_code: int,
        request_id: str | None = None,
        bedrock_account_id: str | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
    ) -> None:
        """Persist one model call to the usage ledger.

        Issue #4898: the implementation also persists
        ``usage_logs.graph_address``, derived from ``context``'s own verified
        graph assignment rather than from any argument — so no caller can select
        it and this signature does not grow a parameter for it. See
        ``UsageService._graph_address_for``.
        """
        ...

    @abstractmethod
    async def query_logs(self, org_id: str, filters: dict[str, Any] | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_usage_summary(self, org_id: str, filters: dict[str, Any] | None = None) -> dict[str, Any]: ...
