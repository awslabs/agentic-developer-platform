"""Read authenticated, immutable deployment evidence; never infer fixture safety."""

import base64
import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from botocore.config import Config
from starlette.concurrency import run_in_threadpool

from src.l5_devops_reads import unavailable


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class VerificationReader:
    def __init__(self, backend=None, *, session=None, reference=None, validate_authority=None):
        self.backend, self.selected = backend, None
        self.session, self.reference = session, deepcopy(reference)
        self.validate_authority = validate_authority

    def bind_backend(self, backend):
        # Application-level factories must never carry a previous owner's session.
        return type(self)(backend)

    async def read_deployment(self, deployment_ref, target_ref):
        from src.l5_backend_devops import DevopsService

        self.selected = None
        backend = self.backend
        if (
            backend is None
            or backend.actor.account_type != "human"
            or not backend.admission_key
            or backend.admission_operation not in {"submit", "retry"}
            or deployment_ref.get("tenant_id") != backend.actor.org_id
            or target_ref.get("tenant_id") != backend.actor.org_id
            or deployment_ref.get("project_id") != target_ref.get("project_id")
        ):
            raise unavailable("verification_target_out_of_scope", "The evidence must match this owner's selected deployment and target.")
        if self.reference is None or self.session is None:
            raise unavailable(
                "deployment_evidence_producer_required",
                "Install the independent deployment evidence producer with an owner-scoped session and an immutable evidence reference "
                "for this admission. No fixture safety evidence has been supplied.",
                503,
            )
        try:
            if self.validate_authority is not None:
                await self.validate_authority()
            reference = self.reference
            if (
                set(reference) != {"bucket", "key", "version", "sha256", "owner_account_id", "signing_key_arn", "producer"}
                or any(not isinstance(v, str) or not v for v in reference.values())
                or reference["version"] == "null"
                or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
                or not re.fullmatch(r"[0-9]{12}", reference["owner_account_id"])
                or not re.fullmatch(r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[a-zA-Z0-9-]+", reference["signing_key_arn"])
            ):
                raise ValueError("invalid evidence reference")
            config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})
            s3 = self.session.client("s3", config=config)
            response = await run_in_threadpool(
                s3.get_object,
                Bucket=reference["bucket"],
                Key=reference["key"],
                VersionId=reference["version"],
                ExpectedBucketOwner=reference["owner_account_id"],
            )
            try:
                contents = await run_in_threadpool(response["Body"].read, 1024 * 1024 + 1)
            finally:
                response["Body"].close()
            if (
                len(contents) > 1024 * 1024
                or response.get("VersionId") != reference["version"]
                or sha256(contents).hexdigest() != reference["sha256"]
            ):
                raise ValueError("evidence integrity mismatch")
            envelope = json.loads(contents)
            if set(envelope) != {"payload", "signature"}:
                raise ValueError("invalid signed envelope")
            payload = envelope["payload"]
            if (
                set(payload) != {"schema_version", "producer", "scope", "configuration", "admission", "issued_at", "expires_at", "observation"}
                or payload["schema_version"] != 1
                or payload["producer"] != reference["producer"]
            ):
                raise ValueError("invalid producer schema")
            kms = self.session.client("kms", region_name=reference["signing_key_arn"].split(":")[3], config=config)
            signature = await run_in_threadpool(
                kms.verify,
                KeyId=reference["signing_key_arn"],
                Message=sha256(canonical(payload)).digest(),
                MessageType="DIGEST",
                SigningAlgorithm="RSASSA_PSS_SHA_256",
                Signature=base64.b64decode(envelope["signature"], validate=True),
            )
            if signature.get("SignatureValid") is not True or signature.get("KeyId") != reference["signing_key_arn"]:
                raise ValueError("producer signature invalid")
            now = datetime.now(UTC)
            issued = datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
            if (
                issued.tzinfo is None
                or expiry.tzinfo is None
                or not now - timedelta(minutes=5) <= issued <= now < expiry <= issued + timedelta(days=1)
            ):
                raise ValueError("producer evidence expired")
            scope = payload["scope"]
            if (
                set(scope) != {"tenant_id", "product_id", "project_id"}
                or scope["tenant_id"] != backend.actor.org_id
                or scope["project_id"] != deployment_ref["project_id"]
                or payload["admission"] != {"operation": backend.admission_operation, "idempotency_key": backend.admission_key}
            ):
                raise ValueError("evidence admission mismatch")
            configuration, version = await DevopsService(backend).current(scope["product_id"], scope["project_id"])
            if payload["configuration"] != {"id": configuration.configuration_id, "revision": version.revision, "hash": version.hash}:
                raise ValueError("evidence configuration mismatch")
            observation = payload["observation"]
            if (
                not isinstance(observation, dict)
                or observation.get("deployment_ref") != deployment_ref
                or observation.get("target_ref") != target_ref
                or not isinstance(observation.get("scenario_specs"), list)
            ):
                raise ValueError("evidence target mismatch")
            # The common admission validator checks source/deployed SHA, isolation,
            # privilege, network, scenario limits, cleanup identity and deadline.
            if self.validate_authority is not None:
                await self.validate_authority()
            self.selected = payload
            return {key: value for key, value in observation.items() if key != "scenario_specs"}
        except Exception as error:
            raise unavailable(
                "deployment_evidence_unverified",
                "Refresh the independent producer's signed evidence for this admission, current configuration, deployment and target; "
                "verify its pinned S3 version, hash and KMS key.",
                409,
            ) from error

    async def read_scenarios(self, artifacts):
        from src.l5_backend_devops import DevopsService

        if self.selected is None:
            raise unavailable("verification_target_unverified", "Read the signed deployment evidence before selecting scenarios.")
        if self.validate_authority is not None:
            await self.validate_authority()
        scope = self.selected["scope"]
        configuration, version = await DevopsService(self.backend).current(scope["product_id"], scope["project_id"])
        specs = self.selected["observation"]["scenario_specs"]
        if (
            self.selected["configuration"] != {"id": configuration.configuration_id, "revision": version.revision, "hash": version.hash}
            or [item.get("artifact") for item in specs] != artifacts
        ):
            raise unavailable("verification_target_unverified", "Scenario artifacts or configuration changed after deployment verification.")
        return specs
