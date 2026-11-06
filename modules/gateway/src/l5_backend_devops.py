"""Request-bound DevOps callbacks using current owner, source and native authority."""

import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from l5_services.api.configuration import view
from l5_services.devops.prevention import PreventionPolicy
from l5_services.devops.release_prerequisites import INDEXING_UNAVAILABLE, release_eligible
from l5_services.models.binding import Binding
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.auth.l5_delegation import issue_l5_work_delegation
from src.internal.l5_devops_policy import space_arn
from src.internal.sts_assume_service import assume_role
from src.l5_devops_reads import association_readback, github_source, unavailable
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.source_routes import connection_access
from src.shared.config import get_settings
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper


def read_policy(binding, expires_at, *, integration=False, permission=False, update=False):
    resource = space_arn(binding)
    identity = binding.provider_identity
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}, "StringEquals": {"aws:RequestedRegion": identity["region"]}}
    actions = ["aidevops:GetAgentSpace", "aidevops:ListGoals"]
    if integration:
        actions.extend(["aidevops:ListAssociations", "aidevops:GetAssociation"])
    if update:
        actions.append("aidevops:UpdateGoal")
    statements = [{"Effect": "Allow", "Action": actions, "Resource": resource, "Condition": condition}]
    if integration:
        statements.extend(
            [
                {"Effect": "Allow", "Action": "aidevops:ListServices", "Resource": "*", "Condition": condition},
                {
                    "Effect": "Allow",
                    "Action": "aidevops:GetService",
                    "Resource": f"arn:aws:aidevops:{identity['region']}:{identity['account_id']}:service/*",
                    "Condition": condition,
                },
            ]
        )
    if permission:
        statements.append(
            {
                "Effect": "Allow",
                "Action": "iam:SimulatePrincipalPolicy",
                "Resource": identity["role_arn"],
                "Condition": {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}},
            }
        )
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


class PreventionClient:
    """AWS scopes UpdateGoal to the space; enforce the exact goal at this boundary."""

    def __init__(self, client, space_id, goal_id=None):
        self.client, self.space_id, self.goal_id = client, space_id, goal_id

    def _check(self, args):
        if args.get("agentSpaceId") != self.space_id:
            raise ValueError("prevention target differs from the selected space")

    def get_agent_space(self, **args):
        self._check(args)
        return self.client.get_agent_space(**args)

    def list_goals(self, **args):
        self._check(args)
        return self.client.list_goals(**args)

    def update_goal(self, **args):
        self._check(args)
        if (
            not self.goal_id
            or args.get("goalId") != self.goal_id
            or set(args) != {"agentSpaceId", "goalId", "evaluationSchedule", "clientToken"}
            or args["evaluationSchedule"] not in ({"state": "ENABLED"}, {"state": "DISABLED"})
        ):
            raise ValueError("prevention update differs from the selected goal")
        return self.client.update_goal(**args)


class DevopsService:
    def __init__(self, backend):
        self.backend = backend

    async def current(self, product_id, project_id):
        from src.auth.l5_owner_delegation import _owner

        # This requires actual current JWT owner/team/relationship authority,
        # deliberately excluding the platform-admin visibility override.
        configuration, version = await _owner(self.backend, product_id, project_id, "devops")
        if configuration.handoff_state != "linked":
            raise HTTPException(404, detail={"code": "not_found"})
        return configuration, version

    async def binding(self, configuration, version, binding_id=None):
        rows = (
            await self.backend.db.scalars(
                select(Binding)
                .where(
                    Binding.tenant_id == self.backend.actor.org_id,
                    Binding.configuration_id == configuration.configuration_id,
                    Binding.product_id == configuration.product_id,
                    Binding.project_id == configuration.project_id,
                    Binding.service_id == "devops",
                    Binding.state == "ready",
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        rows = [
            row
            for row in rows
            if (binding_id is None or row.binding_id == binding_id)
            and not row.gaps
            and row.connection_ref == version.settings["devops"]["hosting_connection_ref"]
            and row.scope == view(configuration, version)["scope"]
            and (row.provider_identity or {}).get("region") == version.settings["devops"].get("region")
        ]
        if len(rows) != 1:
            raise unavailable("devops_binding_unverified", "Select one verified DevOps space binding before checking provider readiness.")
        space_arn(rows[0])
        return rows[0]

    async def selected_provider(
        self, binding, *, integration=False, permission=False, update=False, action_id=None, expires_at=None, session_policy=None
    ):
        backend = self.backend
        configuration, selected_version = await self.current(binding.product_id, binding.project_id)
        expected_config = (configuration.configuration_id, selected_version.revision, selected_version.hash)
        if configuration.configuration_id != binding.configuration_id:
            raise HTTPException(404, detail={"code": "not_found"})
        connection = binding.connection_ref
        credential = await backend.db.get(UserCredential, connection["record_id"], populate_existing=True)
        if (
            credential is None
            or credential.id != connection.get("source_connection_id")
            or credential.org_id != backend.actor.org_id
            or credential.user_id != backend.actor.user_id
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
            or credential.aws_verified_version_id != connection.get("record_revision")
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        evidence = verified_connection_evidence(credential)
        selected_binding = (binding.generation, binding.state, binding.connection_ref, binding.provider_identity, binding.native_resource_ref)
        helper = SecretsManagerHelper(region_name=get_settings().aws_region)
        try:
            version = await run_in_threadpool(helper.current_version_id, credential.secret_arn)
            if version != credential.aws_verified_version_id:
                raise ValueError("selected connection changed")
            secret, served = await run_in_threadpool(helper.get_secret_at_version, credential.secret_arn, version)
            role, external_id, account = connection_material(json.loads(secret), credential.scopes or {}, credential)
            identity = binding.provider_identity
            if served != version or identity["account_id"] != account or identity["role_arn"] != role:
                raise ValueError("selected connection changed")
            issued = await run_in_threadpool(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=identity["region"],
                user_id=backend.actor.user_id,
                agent_id="l5-devops-readiness",
                task_id=action_id or backend.admission_key or "provider-readback",
                label=credential.label,
                session_policy=session_policy
                if session_policy is not None
                else read_policy(
                    binding,
                    min(datetime.now(UTC) + timedelta(minutes=15), expires_at) if expires_at else datetime.now(UTC) + timedelta(minutes=15),
                    integration=integration,
                    permission=permission,
                    update=update,
                ),
            )
            require_assumed_identity(issued, role)
            provider = boto3.Session(
                aws_access_key_id=issued.access_key_id,
                aws_secret_access_key=issued.secret_access_key,
                aws_session_token=issued.session_token,
                region_name=identity["region"],
            )
            observed = await run_in_threadpool(
                provider.client("sts", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})).get_caller_identity
            )
            if observed.get("Account") != account or observed.get("Arn") != issued.assumed_role_arn:
                raise ValueError("provider identity changed")
            if await run_in_threadpool(helper.current_version_id, credential.secret_arn) != version:
                raise ValueError("selected connection changed")
            await backend.db.refresh(credential)
            if verified_connection_evidence(credential) != evidence:
                raise ValueError("selected connection changed")
            await backend.db.refresh(binding)
            if selected_binding != (
                binding.generation,
                binding.state,
                binding.connection_ref,
                binding.provider_identity,
                binding.native_resource_ref,
            ):
                raise ValueError("selected binding changed")
            latest_config, latest = await self.current(binding.product_id, binding.project_id)
            if expected_config != (latest_config.configuration_id, latest.revision, latest.hash):
                raise ValueError("selected configuration changed")
            return provider
        except Exception as error:
            raise unavailable(
                "selected_connection_unavailable",
                "Reverify the selected AWS connection; its identity or scoped read permission could not be confirmed.",
                503,
            ) from error

    @staticmethod
    def native(provider):
        return provider.client("devops-agent", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}))

    async def integrations(self, tenant_id, product_id, project_id, settings, revision):
        if tenant_id != self.backend.actor.org_id:
            raise HTTPException(404, detail={"code": "not_found"})
        configuration, version = await self.current(product_id, project_id)
        expected_hash = version.hash
        if version.settings != settings or version.revision != revision:
            raise unavailable("stale_revision", "Refresh the current service configuration before checking integrations.")
        selections = settings["devops"].get("integrations") or []
        if not selections:
            return []
        if len(selections) > 20:
            raise unavailable("devops_integration_limit", "Select at most twenty integrations per bounded readiness check.")
        binding = await self.binding(configuration, version)
        provider = await self.selected_provider(binding, integration=True)
        native = self.native(provider)
        observations = []
        for selection in selections:
            observed = {"selection": selection, "configuration_revision": revision}
            try:
                if selection.get("kind") != "github":
                    raise unavailable(
                        "devops_cloudwatch_readback_unavailable",
                        "CloudWatch needs a native resource-scope readback adapter; no GitHub association can qualify it.",
                    )
                refs = selection.get("repository_refs") or []
                if len(refs) != 1 or selection.get("workflow_refs"):
                    raise unavailable(
                        "devops_integration_selection_unsupported",
                        "Select one repository per integration; workflow deployment qualification is not installed.",
                    )
                reference = refs[0]
                connection = selection.get("connection_ref") or {}
                if (
                    connection.get("record_id") != reference.get("source_connection_id")
                    or connection.get("source_connection_id") != reference.get("source_connection_id")
                    or connection.get("tenant_id") != tenant_id
                    or connection.get("project_id") != project_id
                ):
                    raise unavailable("devops_source_access_denied", "The selected integration and repository must use the same Product connection.")
                async with github_source(self.backend, project_id, reference) as (repository, _, _binding):
                    service, association = await run_in_threadpool(
                        association_readback, native, binding.native_resource_ref["record_id"], reference, repository
                    )
                capabilities = association.get("capabilities") or {}
                automation = {}
                for field, key in (
                    ("automatic_review", "RELEASE_READINESS_REVIEW"),
                    ("automatic_testing", "RELEASE_READINESS_REVIEW_AUTOMATED_TESTING"),
                ):
                    enabled = (capabilities.get(key) or {}).get("enabled")
                    if type(enabled) is bool:
                        automation[field] = "ENABLED" if enabled else "DISABLED"
                observed.update(
                    evidence={
                        "kind": "github",
                        "connection_ref": connection,
                        "repositories": [{"source_ref": reference, "content_revision": reference["record_revision"], "content_readable": True}],
                        "workflows": [],
                    },
                    native={
                        "registration": {"status": "active", "id": service["serviceId"], "connection_ref": connection},
                        "association": {
                            "status": "active",
                            "id": association["associationId"],
                            "registration_id": service["serviceId"],
                            "source_refs": refs,
                        },
                        "indexing": dict(INDEXING_UNAVAILABLE),
                        "automation": automation,
                    },
                )
                # Native API does not expose authenticated webhook delivery state;
                # leave event support unverified instead of inferring it from App use.
            except HTTPException as error:
                observed["unavailable"] = error.detail
            observations.append(observed)
        _, latest = await self.current(product_id, project_id)
        if latest.revision != revision or latest.hash != expected_hash:
            raise unavailable("stale_revision", "The service configuration changed during validation. Check its current revision.")
        return observations

    async def prevention(self, credential, selection, action_id, *, update=False):
        configuration, version = await self.current(selection.product_id, selection.project_id)
        binding = await self.binding(configuration, version, selection.binding_id)
        if (
            binding.connection_ref["record_id"] != credential.id
            or binding.provider_identity["account_id"] != selection.account_id
            or binding.provider_identity["region"] != selection.region
            or selection.tenant_id != self.backend.actor.org_id
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        # Re-run consent checks even when invoked outside the normal route.
        await self.backend._prevention_target(selection.product_id, selection.project_id, selection.binding_id)
        policy = PreventionPolicy.from_configuration(version.settings)
        if update and not policy.goal_id:
            raise unavailable("prevention_policy_unavailable", "Select the exact native goal before changing prevention.")
        grants = (
            await self.backend.db.scalars(
                select(CredentialServiceDelegation)
                .where(
                    CredentialServiceDelegation.tenant_id == self.backend.actor.org_id,
                    CredentialServiceDelegation.product_id == selection.product_id,
                    CredentialServiceDelegation.project_id == selection.project_id,
                    CredentialServiceDelegation.credential_id == credential.id,
                    CredentialServiceDelegation.created_by == self.backend.actor.user_id,
                    CredentialServiceDelegation.parent_delegation_id.is_(None),
                    CredentialServiceDelegation.revoked_at.is_(None),
                    CredentialServiceDelegation.expires_at > datetime.now(UTC),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        from src.l5_service_identity import worker_id

        grants = [grant for grant in grants if grant.service_id == worker_id("devops") and "prevention" in grant.capabilities]
        if len(grants) != 1:
            raise unavailable("delegation_denied", "Authorize the current prevention configuration before changing its schedule.", 403)
        expiry = grants[0].expires_at.replace(tzinfo=grants[0].expires_at.tzinfo or UTC)
        provider = await self.selected_provider(binding, update=update, action_id=action_id, expires_at=expiry)
        await self.backend._prevention_target(selection.product_id, selection.project_id, selection.binding_id)
        return PreventionClient(self.native(provider), binding.native_resource_ref["record_id"], policy.goal_id if update else None)

    async def release(self, run_input, verified, version):
        backend = self.backend
        scope = verified["scope"]
        configuration, current = await self.current(scope["product_id"], scope["project_id"])
        if (
            current.revision != version.revision
            or current.hash != version.hash
            or verified["configuration_id"] != configuration.configuration_id
            or run_input.get("capability") != "release_readiness"
            or not backend.admission_key
        ):
            raise unavailable("stale_revision", "Refresh the current release configuration before retrying.")
        binding = await self.binding(configuration, current, verified["binding_id"])
        options = run_input["devops_input"]
        if any(not re.fullmatch(r"[a-f0-9]{40}", str(options.get(key, ""))) for key in ("base_revision", "head_revision")):
            raise unavailable("release_source_selection_invalid", "Select immutable base and head Git commit hashes before requesting review.")
        head = options["repository_ref"]
        sources = [{**head, "record_revision": options["base_revision"]}, head]
        parent = await backend.db.get(CredentialServiceDelegation, verified["delegation_id"], populate_existing=True)
        if (
            parent is None
            or not isinstance(parent.execution_scope, dict)
            or any(source not in parent.sources or source not in current.settings.get("source_refs", []) for source in sources)
        ):
            raise unavailable(
                "release_source_consent_required",
                "Select both exact base and head commits in source_refs, then review and authorize the current delegation scope.",
            )
        provider = await self.selected_provider(binding, integration=True, permission=True)
        native = self.native(provider)
        async with github_source(backend, scope["project_id"], head) as (repository, read, source_binding):
            base = await read("commits/" + options["base_revision"])
            if base.get("sha") != options["base_revision"]:
                raise unavailable("release_base_unverified", "GitHub did not return the selected base commit.")
            _, association = await run_in_threadpool(association_readback, native, binding.native_resource_ref["record_id"], head, repository)
            deployments = await read("deployments?sha=" + options["head_revision"] + "&per_page=100")
            if not isinstance(deployments, list) or len(deployments) >= 100:
                raise unavailable("release_deployment_unverified", "Select a commit with a bounded, unambiguous GitHub deployment record.")
            deployments = [item for item in deployments if item.get("sha") == options["head_revision"] and item.get("environment")]
            if len(deployments) != 1 or type(deployments[0].get("id")) is not int:
                raise unavailable(
                    "release_deployment_unverified", "Publish one deployment record for the selected head commit before requesting release review."
                )
            deployment = deployments[0]
            statuses = await read(f"deployments/{deployment['id']}/statuses?per_page=1")
            if not isinstance(statuses, list) or len(statuses) != 1 or statuses[0].get("state") != "success":
                raise unavailable("release_deployment_unverified", "The selected commit's latest GitHub deployment status must be successful.")
            if await connection_access(backend.db, backend.actor, source_binding) is None:
                raise HTTPException(404, detail={"code": "not_found"})
        resource = space_arn(binding)
        response = await run_in_threadpool(
            provider.client("iam", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})).simulate_principal_policy,
            PolicySourceArn=binding.provider_identity["role_arn"],
            ActionNames=["aidevops:CreateBacklogTask"],
            ResourceArns=[resource],
        )
        evaluations = response.get("EvaluationResults") or []
        if (
            response.get("IsTruncated")
            or len(evaluations) != 1
            or evaluations[0].get("EvalActionName") != "aidevops:CreateBacklogTask"
            or evaluations[0].get("EvalResourceName") != resource
            or evaluations[0].get("EvalDecision") != "allowed"
            or evaluations[0].get("MissingContextValues")
        ):
            raise unavailable(
                "release_task_permission_denied", "Grant CreateBacklogTask on the selected DevOps space to the connection role, then check again."
            )
        proof = {"association": association, "deployment": deployment, "status": statuses[0], "permission": evaluations[0]}
        proof_hash = sha256(json.dumps(proof, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
        eligibility = {
            "status": "ready",
            "task_permission": "allowed",
            "permission_action": "CreateBacklogTask",
            "permission_space_id": binding.native_resource_ref["record_id"],
            "repository_ref": head,
            "base_revision": options["base_revision"],
            "head_revision": options["head_revision"],
            "source_indexing": dict(INDEXING_UNAVAILABLE),
            "source_indexed": None,
            "indexed_revisions": [],
            "native_association": association,
            "association_id": association["associationId"],
            "eligibility_ref": {**head, "record_type": "native_eligibility", "record_id": str(deployment["id"]), "record_revision": proof_hash},
            "reference_id": str(deployment["id"]),
            "reference_url": repository["html_url"] + "/commit/" + options["head_revision"],
            "deployment_context": {"target": deployment["environment"], "revision": deployment["sha"]},
        }
        if not release_eligible(options, eligibility, scope, binding.connection_ref, binding.native_resource_ref):
            raise unavailable(
                "release_native_eligibility_unverified",
                "Enable the selected repository's native release-review capability and recheck its current association.",
            )
        credential = await backend.db.get(UserCredential, binding.connection_ref["record_id"], populate_existing=True)
        child = await issue_l5_work_delegation(
            backend.db,
            backend.actor,
            parent,
            credential,
            binding,
            binding.connection_ref,
            sources,
            "release_readiness",
            current.settings["limits"]["deadline_seconds"],
            backend.admission_operation,
            backend.admission_key,
            previous_id=backend.previous_delegation_id,
        )
        admitted = {
            **verified,
            "delegation_id": child.id,
            "delegation_version": child.revision,
            "trigger": {**verified["trigger"], "source_refs": sources},
            "release_eligibility": eligibility,
            "adapter_revision": "release-diff-v1",
            "max_bytes": 6 * 1024 * 1024,
            "max_files": 1000,
        }
        source_client = GatewayAdmissionSourceClient(
            backend.db, backend.actor, child.id, admitted, backend.admission_operation, backend.admission_key, service_id="devops"
        )
        return {
            "eligibility": eligibility,
            "source_client": source_client,
            "verified": admitted,
            "max_bytes": admitted["max_bytes"],
            "max_files": admitted["max_files"],
        }


class Callback:
    def __init__(self, method):
        self.method = method

    def bind_backend(self, backend):
        method = getattr(DevopsService(backend), self.method)

        async def invoke(*args, **kwargs):
            try:
                return await method(*args, **kwargs)
            except HTTPException:
                raise
            except (BotoCoreError, ClientError, httpx.HTTPError, ConnectionError, TimeoutError, ValueError, KeyError, TypeError) as error:
                raise unavailable(
                    "devops_provider_readback_unavailable",
                    "The selected provider could not complete this DevOps check. Verify the connection's read permissions, "
                    "the selected native space/repository and provider availability, then retry.",
                    503,
                ) from error

        return invoke


def gateway_backends():
    from src.l5_devops_verification import VerificationReader

    return {
        "l5_devops_integration_observer": Callback("integrations"),
        "l5_devops_verification_reader": VerificationReader(),
        "l5_devops_release_authority": Callback("release"),
        "l5_devops_prevention_provider": Callback("prevention"),
    }
