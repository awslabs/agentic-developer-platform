"""Current Product work and broker identity for the installed Transform worker."""

import re
from datetime import UTC, datetime, timedelta

from l5_services.workers.execution import AuthorityRevoked


def _aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class TransformWorkAuthority:
    def __init__(self, *, scope, source_request, capture_limits, credentials, identity, expires_at, clock,
                 provider_deadline_seconds=None):
        self.scope = scope
        self.source_request = source_request
        self.capture_limits = capture_limits
        self.provider_identity = identity
        self._credentials = credentials
        self.expires_at = expires_at
        self._clock = clock
        self.provider_deadline_seconds = provider_deadline_seconds

    def provider_credentials(self):
        self.assert_current()
        if self._credentials is None:
            raise AuthorityRevoked("Transform provider session expired or unavailable")
        return dict(self._credentials)

    def assert_current(self):
        if _aware(self._clock()) >= self.expires_at:
            raise AuthorityRevoked("Transform work authority expired")


class TransformWorkAuthorizer:
    def __init__(self, transport, *, clock=None):
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self, run, claim, operation):
        admission = run.admission
        if not isinstance(admission, dict):
            raise AuthorityRevoked("Transform admission unavailable")
        scope = admission.get("scope") or {}
        settings = admission.get("transform_settings") or {}
        if not isinstance(scope, dict) or not isinstance(settings, dict):
            raise AuthorityRevoked("Transform scope or settings unavailable")
        connection = settings.get("connection_ref")
        current = _aware(self.clock())
        deadline = _aware(run.deadline_at)
        grace = (admission.get("monitoring_policy") or {}).get("poll_grace_seconds", 0)
        kinds = {"materialize": {"execute"}, "verify_baseline": {"submit"}, "submit": {"submit"},
                 "reconcile": {"submit", "reconcile"}, "recover_reconcile": {"reconcile"},
                 "poll": {"poll"}, "observe": {"poll"}, "recover_observe": {"poll"},
                 "collect": {"collect"}, "recover_collect": {"collect"}}
        if (run.service_id != "transform" or claim.tenant_id != run.tenant_id
                or claim.run_id != run.run_id or scope.get("tenant_id") != run.tenant_id
                or scope.get("product_id") != run.product_id or scope.get("project_id") != run.project_id
                or claim.admission != admission or claim.run_input != run.input
                or claim.run_revision != run.revision or claim.kind not in kinds.get(operation, ())
                or type(claim.generation) is not int or claim.generation < 1
                or not isinstance(connection, dict) or connection != admission.get("connection_ref")
                or connection.get("record_type") != "connection"
                or connection.get("tenant_id") != run.tenant_id
                or connection.get("project_id") != run.project_id
                or connection.get("record_id") != connection.get("source_connection_id")
                or not admission.get("delegation_id")
                or type(admission.get("delegation_version")) is not int
                or admission["delegation_version"] < 1
                or admission.get("binding_generation") != run.binding_generation
                or type(claim.fence) is not int or claim.fence < 1 or not claim.owner
                or type(grace) is not int or grace < 0 or current >= deadline + timedelta(seconds=grace)
                or (operation == "materialize" and (current >= deadline or run.state != "admitted"))
                or (operation in {"verify_baseline", "submit"}
                    and (current >= deadline or run.state != "queued"))
                or (operation not in {"materialize", "verify_baseline", "submit"}
                    and run.state not in {"queued", "running", "collecting", "outcome_unknown", "cancelled", "expired"})):
            raise AuthorityRevoked("Transform work, connection or scope differs")
        request = {"delegation_id": admission["delegation_id"],
                   "delegation_revision": admission["delegation_version"],
                   "operation_id": run.run_id, "attempt_id": claim.work_id,
                   "lease_owner": claim.owner, "lease_fence": claim.fence,
                   "connection_ref": {**connection, "record_type": "credential"}}
        source_request = {key: value for key, value in request.items() if key != "connection_ref"}
        credentials, identity, expires_at = None, None, deadline
        if operation not in {"materialize", "verify_baseline"}:
            response = await self.transport.post("/internal/v1/l5/credential-session", json=request)
            response.raise_for_status()
            result = response.json()
            identity = result.get("provider_identity") if isinstance(result, dict) else None
            expected_identity = admission.get("provider_identity")
            if (not isinstance(result, dict) or result.get("operation_id") != run.run_id
                    or result.get("attempt_id") != claim.work_id
                    or result.get("delegation_revision") != admission["delegation_version"]
                    or not isinstance(identity, dict) or identity.get("region") != settings.get("region")
                    or not isinstance(expected_identity, dict)
                    or any(not expected_identity.get(field)
                           or identity.get(field) != expected_identity[field]
                           for field in ("account_id", "role_arn", "region"))
                    or not isinstance(identity.get("account_id"), str)
                    or not re.fullmatch(r"[0-9]{12}", identity["account_id"])
                    or not isinstance(identity.get("role_arn"), str)
                    or not identity["role_arn"].startswith(
                        f"arn:aws:iam::{identity['account_id']}:role/")):
                raise AuthorityRevoked("Transform broker returned another work or provider identity")
            try:
                expires_at = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                raise AuthorityRevoked("Transform broker expiry unavailable") from error
            if (expires_at.tzinfo is None or _aware(expires_at) <= current
                    or _aware(expires_at) > deadline + timedelta(seconds=grace)):
                raise AuthorityRevoked("Transform broker session expired or exceeds work")
            expires_at = _aware(expires_at)
            credentials = {key: result.get(key) for key in (
                "access_key_id", "secret_access_key", "session_token")}
            if any(not isinstance(value, str) or not value for value in credentials.values()):
                raise AuthorityRevoked("Transform broker session credentials unavailable")
        return TransformWorkAuthority(
            scope=scope, source_request=source_request, capture_limits=run.capture_limits,
            credentials=credentials, identity=identity, expires_at=expires_at, clock=self.clock,
            provider_deadline_seconds=(admission.get("limits") or {}).get("deadline_seconds"))
