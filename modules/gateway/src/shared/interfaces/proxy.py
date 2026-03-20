from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from src.shared.schemas.auth import TokenContext


class IProxyService(ABC):
    @abstractmethod
    async def invoke(self, request: dict[str, Any], context: TokenContext) -> dict[str, Any]: ...

    @abstractmethod
    async def invoke_stream(self, request: dict[str, Any], context: TokenContext) -> AsyncIterator[bytes]: ...
