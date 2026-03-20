from abc import ABC, abstractmethod
from typing import Any


class IPoolService(ABC):
    @abstractmethod
    async def get_client(self) -> Any:
        """Returns a Bedrock client wrapper for the next healthy account."""
        ...

    @abstractmethod
    async def report_error(self, account_id: str) -> None: ...

    @abstractmethod
    async def get_pool_status(self) -> list[dict[str, Any]]: ...
