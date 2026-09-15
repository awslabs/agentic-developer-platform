from abc import ABC, abstractmethod
from typing import Any


class IPoolService(ABC):
    @abstractmethod
    async def get_client(self, credentials: Any | None = None) -> Any:
        """Returns a Bedrock client wrapper for the next healthy account.

        Args:
            credentials: Issue #4744 (#4692 · R3) — explicit credentials for a
                cross-account routed call, or ``None`` for the ambient (platform
                account) client. Duck-typed rather than imported so this interface
                keeps no dependency on the proxy package: implementations need
                ``.region`` and ``.as_boto3_kwargs()``, which
                ``src.proxy.bedrock_signing.DestinationCredentials`` provides.

        The parameter is **credentials, not a routing target**, and it is an explicit
        argument rather than ambient state. Both choices come from design note
        §2.1/§2.3: the pool receives the *result* of a routing decision and can never
        see its inputs, and the contextvar alternative was rejected because a
        credential selection read from ambient state is exactly how one principal's
        request gets signed with another principal's credentials.

        Defaulting to ``None`` keeps every existing caller correct and unchanged: no
        credentials means the ambient client, which is today's behaviour and remains
        the behaviour of every unmapped call.
        """
        ...

    @abstractmethod
    async def report_error(self, account_id: str) -> None: ...

    @abstractmethod
    async def get_pool_status(self) -> list[dict[str, Any]]: ...
