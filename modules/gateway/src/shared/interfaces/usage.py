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
    ) -> None: ...

    @abstractmethod
    async def query_logs(self, org_id: str, filters: dict[str, Any] | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_usage_summary(self, org_id: str, filters: dict[str, Any] | None = None) -> dict[str, Any]: ...
