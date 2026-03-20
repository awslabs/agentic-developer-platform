from abc import ABC, abstractmethod

from src.shared.schemas.auth import AuthExchangeRequest, AuthExchangeResponse, TokenContext


class IAuthService(ABC):
    @abstractmethod
    async def exchange_credentials(self, request: AuthExchangeRequest) -> AuthExchangeResponse: ...

    @abstractmethod
    async def validate_token(self, token: str) -> TokenContext: ...

    @abstractmethod
    async def revoke_token(self, token: str) -> None: ...
