"""Read only this bootstrap operation's management-controller observation."""

from datetime import UTC, datetime, timedelta
import json
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import BootstrapRefused


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ManagementObservation:
    """Service-composed operation credential; no broad registry bearer is accepted.

    The supplied opaque credential has a durable hash bound to the exact
    organization/workspace/operation/claim and live execution lease. Neither the
    credential nor the response is written to component journals or artifacts.
    """

    def __init__(self, *, origin, credential, binding, target, namespace, claim):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (not callable(credential) and not _scoped_token(credential))
            or (binding.principal.org_id, binding.principal.workspace_id)
            != (target.org_id, target.workspace_id)
        ):
            raise BootstrapRefused(
                "management observation requires scoped trusted transport"
            )
        self._origin, self._credential = origin.rstrip("/"), credential
        self._binding = binding
        self._expected = {
            "workspace_id": target.workspace_id,
            "org_id": target.org_id,
            "operation_id": binding.operation_id,
            "cluster_arn": target.cluster_arn,
            "namespace": namespace,
            "registration_claim": claim,
        }

    def observe(self):
        from .target import _binding_identity

        _binding_identity(self._binding)
        try:
            # A trusted supplier bridges to the producer's owning event loop and
            # reissues only against the original still-live shared grant. Never
            # cache its answer or extend a token's expiry locally.
            credential = (
                self._credential() if callable(self._credential) else self._credential
            )
            if not _scoped_token(credential):
                raise BootstrapRefused(
                    "management observation requires a scoped read token"
                )
            request = Request(
                self._origin
                + "/api/v1/workspaces/"
                + quote(self._expected["workspace_id"], safe="")
                + "/bootstrap-observation",
                headers={"Authorization": credential},
            )
            with build_opener(ProxyHandler({}), _NoRedirect()).open(
                request, timeout=5
            ) as response:
                raw = response.read((1 << 16) + 1)
                if len(raw) > 1 << 16:
                    raise ValueError("oversized observation")
                document = json.loads(raw)
            self.verify(document)
            return document
        except BootstrapRefused:
            raise
        except Exception as exc:
            # HTTP errors may include URLs or headers; only a bounded explanation
            # crosses the bootstrap reporting boundary.
            raise BootstrapRefused("management observation is unavailable") from exc

    def verify(self, document):
        try:
            now = datetime.now(UTC)
            observed = datetime.fromisoformat(
                document["last_reconciled"].replace("Z", "+00:00")
            )
            expires = datetime.fromisoformat(
                document["lease_expires_at"].replace("Z", "+00:00")
            )
            if (
                any(document.get(key) != value for key, value in self._expected.items())
                or document.get("registry_ready") is not True
                or document.get("target_status") != "observed_execution_unavailable"
                or observed.tzinfo is None
                or expires.tzinfo is None
                or not now - timedelta(seconds=30) <= observed <= now < expires
            ):
                raise ValueError("observation differs")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise BootstrapRefused(
                "management observation is stale or names another bootstrap"
            ) from exc


def _scoped_token(value):
    return (
        isinstance(value, str)
        and value.startswith("sp-bootstrap-read-")
        and 32 <= len(value) <= 256
        and "\n" not in value
        and "\r" not in value
    )
