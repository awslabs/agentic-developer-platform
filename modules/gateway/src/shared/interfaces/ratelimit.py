from abc import ABC, abstractmethod

from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import RateLimitCheckResult


class IRateLimitService(ABC):
    @abstractmethod
    async def check_rate_limit(self, context: TokenContext) -> RateLimitCheckResult: ...

    @abstractmethod
    async def release_concurrent(self, context: TokenContext) -> None: ...
