"""Thin client over ADP's vault **HTTP API**. The only place a secret value goes.

Issue #5047 (U7), EPIC #4910. R7, ADP client half.

## What this is for

Onboarding a provider account has exactly one step that touches secret material:
handing the value to ADP's vault. Everything afterwards moves a **reference**. This
client is that one step, plus the reads needed to turn a vault credential into the
`CredentialReference` the domain contract accepts.

The vault endpoint is `modules/gateway/src/auth/vault_routes.py`
(`APIRouter(prefix="/auth")`), consumed over HTTP:

| Method | Path | Used for |
|---|---|---|
| `POST` | `/auth/credentials` | register a provider credential — the one value-carrying call |
| `GET` | `/auth/credentials` | list credential **metadata** (never values, never ARNs) |
| `DELETE` | `/auth/credentials/{id}` | revoke, as the *separate later step* after a rotation |

## Why HTTP and not an import

The vault's logic lives in `vault_service.py` and `credential_resolver.py`, and
importing either into this process would be the wrong kind of reuse. Those modules
talk to a database session and to Secrets Manager directly, so importing them would
put a second process on the vault's storage — with its own connection, its own
migration assumptions and its own ability to read a secret value. The vault's
boundary is the HTTP API precisely so that the number of processes holding
`secretsmanager:GetSecretValue` stays at one.

That is also why `tests/test_vault_client.py` asserts the absence structurally, with
an import-graph check rather than a comment: "we chose not to import it" is a
sentence that stays true in a docstring while a later edit quietly makes it false.

## What is mocked, and recorded as mocked

**B's exact-credential binding does not exist.** This is verified, not assumed:
`credential_resolver.py` exposes `resolve(service, ...)` with `scope_hint` and
`strict`, and nothing else — no `resolve_by_id`, no `resolve_exact`. Those two
parameters are not an exact-binding mechanism and it matters why:

* `resolve()` matches by **service**, then ranks candidates and returns the first.
  Its own docstring says "the first matching credential wins". Ask for a service and
  you get *a* credential for it, not the one you named.
* `scope_hint` bounds how *wide* a scope may be returned (refusing an org-scoped
  credential when the caller asked for user scope). Narrowing a scope is not
  identifying a credential — several credentials share a scope.
* `strict=True` restricts a credential to exact-scope matches. Again a property of
  the scope, not of the identity.

So there is no server-side call meaning "use credential X and nothing else". The
vault also exposes no `GET /auth/credentials/{id}` — only the list endpoint — so
even by id, exact resolution is client-side filtering over a list.

`EXACT_BINDING_IS_MOCKED` is therefore True and `resolve_exact()` returns results
tagged `"exact_binding": "mock"`. Client-side filtering is an honest approximation
of a read but is **not enforcement**: a server that hands back a list and a client
that picks from it cannot stop a different caller picking differently. When B ships
real exact binding, `resolve_exact()` is the one seam to replace.

This follows `acceptance-split.md` rule 5 and the precedent in this same package —
`contract.py` records its capacity mock the same way, and for the same reason: a
mock that returns plausible values with no marker is indistinguishable from a live
reading to whoever consumes it.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .redaction import value_contains_secret

logger = logging.getLogger(__name__)

# True while B's exact-credential-binding enforcement does not exist. Asserted by
# the tests, so flipping it without replacing `resolve_exact()` fails CI rather
# than silently upgrading a filtered list into a claimed guarantee.
EXACT_BINDING_IS_MOCKED = True

# Vault paths. Relative, so a deployment supplies the base URL and no environment
# is baked in.
CREDENTIALS_PATH = "/auth/credentials"

# Fields the vault's `CredentialResponse` returns. Listed so `_reference_from()`
# reads named fields rather than copying the response wholesale — a vault response
# that gains a field is not automatically republished by this client.
#
# `tests/test_vault_client.py` parses these names out of the gateway's own
# `vault_schemas.py` and fails if this tuple drifts from the real model, which is
# what keeps the fixtures backend-derived rather than written from this client's
# expectations.
CREDENTIAL_METADATA_FIELDS: tuple[str, ...] = (
    "id",
    "service",
    "label",
    "credential_type",
    "scope",
    "expires_at",
    "last_used_at",
    "strict",
    "created_at",
    "updated_at",
)


class VaultClientError(RuntimeError):
    """A vault call failed.

    The message is composed here from the operation and the HTTP status — never from
    the response body. An upstream error body could contain the value that was just
    submitted (a validation error echoing its input is the classic case), and an
    exception string ends up in logs and in agent transcripts.
    """


@dataclass(frozen=True)
class VaultCredential:
    """Non-secret metadata for one vault credential.

    Deliberately has no `value` field and no `arn` field. There is no attribute on
    this object through which a secret could be carried, so no caller — including a
    future one — can log a value by logging this object.
    """

    credential_id: str
    service: str
    label: str
    credential_type: str
    scope: str

    def __post_init__(self) -> None:
        for value in (
            self.credential_id,
            self.service,
            self.label,
            self.credential_type,
            self.scope,
        ):
            if (
                not isinstance(value, str)
                or value_contains_secret(value)
                or re.search(r"\barn:[a-z0-9-]*:[a-z0-9-]+:", value, re.IGNORECASE)
            ):
                raise VaultClientError("vault returned unsafe credential metadata")

    @property
    def owner_scope(self) -> str:
        """The vault's ownership scope: user | team | org | domain_app."""
        return self.scope


# A transport is any callable taking (method, url, body, headers) and returning
# (status, decoded JSON or None). Injected so the tests exercise the real request
# construction and response handling without a network or a live vault.
Transport = Callable[[str, str, bytes | None, Mapping[str, str]], tuple[int, Any]]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urllib_transport(
    method: str, url: str, body: bytes | None, headers: Mapping[str, str]
) -> tuple[int, Any]:
    """Default transport: stdlib `urllib`.

    stdlib rather than `requests`/`httpx` because this module ships inside a domain
    tool surface that declares no dependencies of its own, and adding an HTTP
    library to reach one endpoint would put a transitive dependency set into every
    consumer of the tool surface.
    """
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - https URL supplied by deployment config
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RefuseRedirects()
        )
        with opener.open(request, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise VaultClientError("vault response exceeds size limit")
            status = response.status
    except urllib.error.HTTPError as exc:
        # Body deliberately not read: see VaultClientError.
        return exc.code, None
    except urllib.error.URLError:
        raise VaultClientError("vault unreachable") from None
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        raise VaultClientError(
            f"vault returned a non-JSON body (HTTP {status})"
        ) from None


class VaultClient:
    """Thin HTTP client for ADP's vault credential endpoints.

    `token_provider` is a callable rather than a token string so a long-lived client
    picks up a refreshed token, and so no caller is encouraged to keep a bearer
    token in an attribute where a repr would print it.
    """

    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], str],
        *,
        transport: Transport | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError(
                "base_url must be an HTTPS vault endpoint without credentials, query or fragment"
            )
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._transport = transport or _urllib_transport

    # -- internals ---------------------------------------------------------

    def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        """Make one vault call. Never logs the payload."""
        url = f"{self._base_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "authorization": f"Bearer {self._token_provider()}",
            "content-type": "application/json",
        }
        status, decoded = self._transport(method, url, body, headers)
        if not 200 <= status < 300:
            # The method and path are safe to name; the body is not included. Note
            # the payload is not logged either — for POST it contains the value.
            raise VaultClientError(f"vault {method} {path} failed with HTTP {status}")
        return decoded

    @staticmethod
    def _reference_from(response: Mapping[str, Any]) -> VaultCredential:
        """Build metadata from a vault `CredentialResponse` body.

        Reads named fields instead of copying the mapping, so a field the vault adds
        later is not silently republished by this client.
        """
        missing = [f for f in ("id", "service", "label") if not response.get(f)]
        if missing:
            raise VaultClientError(
                f"vault response is missing required field(s): {', '.join(sorted(missing))}"
            )
        return VaultCredential(
            credential_id=str(response["id"]),
            service=str(response["service"]),
            label=str(response["label"]),
            credential_type=str(response.get("credential_type", "")),
            scope=str(response.get("scope", "")),
        )

    # -- the one value-carrying call ---------------------------------------

    def register_provider_credential(
        self,
        *,
        service: str,
        label: str,
        credential_type: str,
        value: str,
        scope_hint: str = "user",
    ) -> VaultCredential:
        """Register a provider secret with the vault and return its **reference**.

        This is the only method that accepts a secret value, and the value goes to
        the vault endpoint and nowhere else: it is not returned, not stored on this
        object, and not logged — including in the failure path, where `_call` raises
        a message composed from the status alone.

        The return value is metadata only. A caller wanting a domain-side
        credential reference passes `VaultCredential.credential_id` to
        `CredentialReference`, which refuses an ARN — so the reference that reaches
        the domain contract cannot be a pointer into the AWS account.
        """
        if not value:
            raise ValueError("value is required")
        response = self._call(
            "POST",
            CREDENTIALS_PATH,
            {
                "service": service,
                "label": label,
                "credential_type": credential_type,
                "value": value,
                "scope_hint": scope_hint,
            },
        )
        if not isinstance(response, Mapping):
            raise VaultClientError("vault returned no credential body on register")
        logger.info("registered vault credential")
        return self._reference_from(response)

    # -- metadata reads ----------------------------------------------------

    def list_credentials(
        self, *, scope: str | None = None
    ) -> tuple[VaultCredential, ...]:
        """List credential metadata visible to the caller.

        The vault's own response excludes secret values and ARNs, which is why this
        is the read the client is built on rather than a value-fetching call: there
        is no code path here that could obtain a value to leak.
        """
        path = CREDENTIALS_PATH
        if scope:
            path = f"{path}?{urlencode({'scope': scope})}"
        response = self._call("GET", path)
        if response is None:
            return ()
        if not isinstance(response, list):
            raise VaultClientError("vault credential list response was not a list")
        return tuple(self._reference_from(item) for item in response)

    def resolve_exact(
        self, credential_id: str
    ) -> tuple[VaultCredential | None, dict[str, str]]:
        """Resolve one credential **by id**, and report that this is a mock.

        Returns `(credential_or_None, provenance)` where provenance always contains
        `{"exact_binding": "mock"}` while `EXACT_BINDING_IS_MOCKED` holds. The tuple
        shape is deliberate: a caller cannot take the credential without also
        receiving the marker saying how it was resolved, whereas an attribute on the
        result would be easy to ignore.

        Why it is a mock is in the module docstring: the vault exposes no
        `GET /auth/credentials/{id}` and B has no `resolve_by_id`/`resolve_exact`, so
        this filters the list endpoint client-side. That is a correct *read* and is
        not *enforcement* — nothing server-side refuses a caller who resolves
        differently.
        """
        if not credential_id or not credential_id.strip():
            raise ValueError("credential_id is required")
        provenance = {
            "exact_binding": "mock" if EXACT_BINDING_IS_MOCKED else "enforced",
            "mechanism": "client-side filter over GET /auth/credentials",
        }
        for candidate in self.list_credentials():
            if candidate.credential_id == credential_id:
                return candidate, provenance
        return None, provenance

    # -- revocation, the separate step after a rotation --------------------

    def revoke_credential(self, credential_id: str) -> None:
        """Delete a credential from the vault.

        Exposed as its own method, called by nobody during a rotation. The domain
        contract's `rotate()` returns the old reference as *superseded* and this is
        what an operator calls afterwards, once traffic is confirmed on the
        replacement. Keeping them separate is what makes the ordering acceptance 5
        requires expressible: there is no combined "rotate and delete" call whose
        failure halfway through leaves the connection pointing at nothing.
        """
        if not credential_id or not credential_id.strip():
            raise ValueError("credential_id is required")
        if credential_id in {".", ".."}:
            raise ValueError("credential_id cannot be a path segment")
        self._call("DELETE", f"{CREDENTIALS_PATH}/{quote(credential_id, safe='')}")
        logger.info("revoked vault credential")
