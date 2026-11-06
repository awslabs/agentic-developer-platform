"""Unmounted queue admission transport using current owner grants, never ambient AWS.

Observation and evidence policies are separate to retain exact resource bounds
within STS's inline-policy limit. Neither session can invoke or clean up a fixture.
Native execution/result qualification remains a separate installation gate.
"""

import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from l5_services.api.configuration import view
from sqlalchemy import select

from src.auth.aws_connection_authority import verified_connection_evidence
from src.auth.l5_execution_scope import current_execution_scope
from src.l5_backend_devops import DevopsService
from src.l5_devops_queue_fixture import SCENARIO_ARTIFACT, QueueFixture
from src.l5_devops_queue_observer import check_selection
from src.l5_devops_reads import unavailable
from src.l5_devops_verification import VerificationReader
from src.l5_service_identity import worker_id
from src.products.models import ProductSourceBinding
from src.products.source_routes import connection_access
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


def _aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


def admission_policies(selection, expires_at):
    """Generate only the APIs consumed by admission observation and signed readback."""
    check_selection(selection)
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}}

    def allow(actions, resources, *, regional=False):
        constraints = deepcopy(condition)
        if regional:
            constraints["StringEquals"] = {"aws:RequestedRegion": selection["region"]}
        return {"Effect": "Allow", "Action": actions, "Resource": resources, "Condition": constraints}

    function = selection["function_version_arn"]
    observation = [
        allow("lambda:GetFunction", function),
        allow("lambda:GetFunctionConcurrency", function.rsplit(":", 1)[0]),
        allow(
            "lambda:GetEventSourceMapping",
            f"arn:aws:lambda:{selection['region']}:{selection['account_id']}:event-source-mapping:{selection['mapping_id']}",
        ),
        allow("sqs:GetQueueAttributes", selection["queue_arn"]),
        allow(["iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"], selection["execution_role_arn"]),
        allow("ec2:DescribeSecurityGroups", "*", regional=True),
        allow("sts:GetCallerIdentity", "*"),
    ]
    prefix = f"arn:aws:s3:::{selection['bucket']}/{selection['prefix']}"
    evidence = [
        allow(["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], [prefix + "admission/*", prefix + "observations/*"]),
        allow(["kms:Sign", "kms:Verify"], selection["signing_key_arn"]),
        allow("sts:GetCallerIdentity", "*"),
    ]
    policies = [json.dumps({"Version": "2012-10-17", "Statement": rows}, separators=(",", ":")) for rows in (observation, evidence)]
    if any(len(policy) > 2048 for policy in policies):
        raise unavailable("fixture_session_policy_too_large", "Shorten the fixture resource names; the exact admission policy exceeds STS limits.")
    return policies


class AdmissionSessions:
    def __init__(self, observation, evidence):
        self.observation, self.evidence = observation, evidence

    def client(self, name, **kwargs):
        if name in {"s3", "kms"}:
            return self.evidence.client(name, **kwargs)
        if name in {"lambda", "sqs", "iam", "ec2", "sts"}:
            return self.observation.client(name, **kwargs)
        raise ValueError("provider API is outside the queue admission policy")


class QueueAdmissionAuthority:
    def __init__(self, backend, verified, run_input):
        self.backend, self.verified, self.run_input = backend, deepcopy(verified), deepcopy(run_input)
        self.operation, self.key = backend.admission_operation, backend.admission_key
        raw = os.environ.get("L5_DEVOPS_QUEUE_FIXTURE")
        if not raw:
            raise unavailable("queue_fixture_not_installed", "Install and verify the exact queue fixture selection before admission.", 503)
        self.selection = json.loads(raw)
        check_selection(self.selection)
        self.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        self.credential_evidence = None

    async def current(self):
        backend, verified, selection = self.backend, self.verified, self.selection
        denied = unavailable(
            "queue_admission_authority_changed",
            "Review the current Product source, AWS connection, DevOps binding and owner delegation before retrying verification.",
        )
        now = datetime.now(UTC)
        scope = verified["scope"]
        if (
            json.loads(os.environ.get("L5_DEVOPS_QUEUE_FIXTURE", "null")) != selection
            or self.operation not in {"submit", "retry"}
            or not self.key
            or (backend.admission_operation, backend.admission_key) != (self.operation, self.key)
            or backend.actor.account_type != "human"
            or _aware(backend.actor.expires_at) <= now
            or now >= self.expires_at
            or (verified["service_id"], verified["actor_type"], verified["actor_id"], scope["tenant_id"])
            != ("devops", "human", backend.actor.user_id, backend.actor.org_id)
        ):
            raise denied
        service = DevopsService(backend)
        configuration, version = await service.current(scope["product_id"], scope["project_id"])
        binding = await service.binding(configuration, version, verified["binding_id"])
        options = {key: selection[key] for key in ("source_ref", "deployment_ref", "target_ref", "credential_ref")}
        options.update(kind="release_verification", scenarios=[SCENARIO_ARTIFACT])
        if "test_profile_ref" in verified["trigger"]:
            # This is the owner's native selector, not a queue fixture URL or
            # application credential claim. The fixture remains an independent oracle.
            options["test_profile_ref"] = verified["trigger"]["test_profile_ref"]
        if (
            view(configuration, version)["scope"] != scope
            or (configuration.configuration_id, version.revision, version.hash)
            != (verified["configuration_id"], verified["configuration_revision"], verified["configuration_hash"])
            or version.settings.get("enabled") is not True
            or self.run_input
            != {
                "service_id": "devops",
                "configuration_revision": version.revision,
                "binding_id": binding.binding_id,
                "capability": "release_verification",
                "devops_input": options,
            }
            or verified["trigger"].get("capability") != "release_verification"
            or binding.generation != verified["binding_generation"]
            or binding.connection_ref != verified["connection_ref"]
            or binding.connection_ref != verified["binding_connection_ref"]
            or binding.provider_identity != verified["provider_identity"]
            or binding.native_resource_ref != verified["native_space_ref"]
            or selection["account_id"] != binding.provider_identity["account_id"]
            or selection["region"] != binding.provider_identity["region"]
            or selection["source_ref"] not in version.settings.get("source_refs", [])
            or any(
                (ref["tenant_id"], ref["project_id"]) != (scope["tenant_id"], scope["project_id"])
                for key, ref in options.items()
                if key.endswith("_ref")
            )
            or any(
                selection[key]["source_connection_id"] != binding.connection_ref["source_connection_id"]
                for key in ("deployment_ref", "target_ref", "credential_ref")
            )
        ):
            raise denied
        grant = await backend.db.get(CredentialServiceDelegation, verified["delegation_id"], populate_existing=True)
        if (
            grant is None
            or grant.parent_delegation_id is None
            or grant.revision != verified["delegation_version"]
            or grant.revoked_at is not None
            or _aware(grant.expires_at) <= now
            or (grant.tenant_id, grant.product_id, grant.project_id, grant.created_by, grant.service_id, grant.credential_id)
            != (
                scope["tenant_id"],
                scope["product_id"],
                scope["project_id"],
                backend.actor.user_id,
                worker_id("devops"),
                binding.connection_ref["record_id"],
            )
            or grant.binding_generation != binding.generation
            or grant.capabilities != ["release_verification"]
            or not {"product:read", "credential:session"}.issubset(grant.operations)
            or grant.sources != [{**binding.connection_ref, "record_type": "credential"}]
            or not isinstance(grant.execution_scope, dict)
            or not await current_execution_scope(
                backend.db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation
            )
            or not await current_l5_parent(backend.db, grant)
        ):
            raise denied
        credential = await backend.db.get(UserCredential, grant.credential_id, populate_existing=True)
        evidence = verified_connection_evidence(credential)
        if self.credential_evidence is not None and self.credential_evidence != evidence:
            raise denied
        self.credential_evidence = deepcopy(evidence)
        source = selection["source_ref"]
        rows = (
            await backend.db.scalars(
                select(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == scope["tenant_id"],
                    ProductSourceBinding.project_id == scope["project_id"],
                    ProductSourceBinding.kind == "repository",
                    ProductSourceBinding.provider == "github",
                    ProductSourceBinding.installation_id == source["source_connection_id"],
                    ProductSourceBinding.native_id == source["record_id"],
                    ProductSourceBinding.unlinked_at.is_(None),
                    ProductSourceBinding.status != "revoked",
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        if (
            len(rows) != 1
            or ((rows[0].observed or {}).get("source_revision") or (rows[0].desired or {}).get("source_revision")) != source["record_revision"]
            or await connection_access(backend.db, backend.actor, rows[0]) is None
        ):
            raise denied
        self.expires_at = min(self.expires_at, _aware(backend.actor.expires_at), _aware(grant.expires_at))
        return binding

    async def session(self):
        binding = await self.current()
        policies = admission_policies(self.selection, self.expires_at)
        service = DevopsService(self.backend)
        observation = await service.selected_provider(binding, session_policy=policies[0], action_id=self.key, expires_at=self.expires_at)
        binding = await self.current()
        evidence = await service.selected_provider(binding, session_policy=policies[1], action_id=self.key, expires_at=self.expires_at)
        await self.current()
        return AdmissionSessions(observation, evidence)


class QueueVerificationReader(VerificationReader):
    """Install only after the native execution/result contract is qualified."""

    def bind_admission(self, verified, run_input):
        reader = type(self)(self.backend)
        reader.authority = QueueAdmissionAuthority(self.backend, verified, run_input)
        return reader

    async def read_deployment(self, deployment_ref, target_ref):
        authority = getattr(self, "authority", None)
        if authority is None:
            raise unavailable("queue_admission_authority_required", "Resolve the owner's current admission authority before producing evidence.")
        if (deployment_ref, target_ref) != (authority.selection["deployment_ref"], authority.selection["target_ref"]):
            raise unavailable("verification_target_out_of_scope", "Select the installed queue deployment and target.")
        self.session = await authority.session()
        self.validate_authority = authority.current
        scope = authority.verified["scope"]
        producer = QueueFixture(self.backend, scope["product_id"], scope["project_id"], session=self.session, validate_authority=authority.current)
        self.reference = await producer.publish_admission()
        return await super().read_deployment(deployment_ref, target_ref)
