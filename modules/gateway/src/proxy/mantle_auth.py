"""Upstream authentication for the bedrock-mantle passthrough (Issue #2709).

The mantle passthrough (``POST /openai/v1/responses``) authenticates to the
upstream ``bedrock-mantle`` endpoint with **AWS SigV4 only**.

Spike #2703 verified mantle accepts SigV4 signed with service name ``bedrock``
(region ``us-east-1``) using the gateway pod's ambient IRSA credential chain —
no API keys, no Secrets Manager, no ``AWS_BEARER_TOKEN_BEDROCK``. See
``docs/agent-context/design-notes/2703-codex-bedrock-model-path-spike.md`` §4-5.
The operator decision (2026-07-03) is SigV4 only: an unused key-auth path is
standing risk, so the bearer mode was removed rather than left selectable.

The ``MantleAuth`` interface is retained; ``SigV4MantleAuth`` is its only
implementation. Because SigV4 signs the request body and URL, the interface
signs a whole request (method + url + body) rather than returning static
headers.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)

# SigV4 signing service name for the mantle/OpenAI path. Verified in spike #2703;
# both "bedrock" and "bedrock-mantle" returned HTTP 200 — "bedrock" is canonical.
MANTLE_SIGNING_NAME = "bedrock"


class MantleAuthError(Exception):
    """Raised when upstream mantle auth cannot be constructed (misconfiguration)."""


class MantleAuth(ABC):
    """Strategy for authenticating an outbound mantle request.

    Implementations return the headers to merge into the outbound request. The
    returned dict MUST be treated as sensitive — callers must never log it.
    """

    @abstractmethod
    def sign(self, method: str, url: str, body: bytes) -> dict[str, str]:
        """Return signed auth headers for the given request.

        Args:
            method: HTTP method (e.g. ``"POST"``).
            url: Full upstream URL being requested.
            body: Exact request body bytes that will be sent upstream. The
                signature is computed over these bytes, so the caller MUST send
                them unchanged (any re-serialization breaks the signature).

        Returns:
            Headers to merge into the outbound request (Authorization,
            X-Amz-Date, and — when using temporary credentials —
            X-Amz-Security-Token). Sensitive; never log.
        """
        raise NotImplementedError


class SigV4MantleAuth(MantleAuth):
    """SigV4 auth using the pod's ambient AWS credential chain (IRSA).

    Signs the exact request body bytes with botocore ``SigV4Auth`` (signing name
    ``bedrock``). Credentials come from the default boto3 session — in the
    gateway pod that resolves to the IRSA role; no secrets are held.

    Args:
        region_name: AWS region used for the credential scope (e.g. ``us-east-1``).
        session: Optional pre-built boto3 session (injected in tests).
    """

    def __init__(self, region_name: str = "us-east-1", *, session: boto3.Session | None = None) -> None:
        if not region_name:
            raise MantleAuthError("mantle_region must be set for SigV4 auth")
        self._region = region_name
        self._session = session or boto3.Session()

    def sign(self, method: str, url: str, body: bytes) -> dict[str, str]:
        credentials = self._session.get_credentials()
        if credentials is None:
            raise MantleAuthError("no AWS credentials available for mantle SigV4 signing (the gateway pod's IRSA role must be configured)")
        # Sign the EXACT bytes that will be sent upstream. add_auth computes the
        # payload hash from `data`, then sets Authorization / X-Amz-Date (and
        # X-Amz-Security-Token for temporary creds) on the request headers.
        request = AWSRequest(method=method, url=url, data=body)
        SigV4Auth(credentials, MANTLE_SIGNING_NAME, self._region).add_auth(request)
        # NOTE: never log these values.
        return dict(request.headers)


def make_mantle_auth(region_name: str, *, session: boto3.Session | None = None) -> MantleAuth:
    """Construct the mantle auth strategy (SigV4 — the only supported mode).

    Args:
        region_name: AWS region for the SigV4 credential scope.
        session: Optional injected boto3 session (tests).
    """
    return SigV4MantleAuth(region_name, session=session)
