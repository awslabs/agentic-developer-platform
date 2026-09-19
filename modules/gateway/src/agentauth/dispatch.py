"""Authenticated, bounded child dispatch with durable exact-intent recovery."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, _iso, _key, envelope_digest
from src.agentauth.grants import (
    AUTHORITY_GATE_DECISION,
    AUTHORITY_GITHUB_EVENT,
    AUTHORITY_SERVICE_POLICY,
    AgentAction,
    DelegatedGrant,
    TargetRelationship,
)
from src.agentauth.policy import AgentAuthorizationService, PolicyError

logger = logging.getLogger("bedrockgateway.agentauth.dispatch")

AgentPersona = Literal[
    "developer", "reviewer", "operations", "aidlc", "architect", "pm", "product", "codex", "malware-analysis-agent", "pt-superpower"
]

# Issue #5365: mirrors the trusted webhook writer
# (`webhook-ingress/lambda/common/agent_authority.py`). Kept as named constants on
# both sides so the reader and the writer cannot drift into silently disagreeing
# about the marker's spelling, which would fail *open* on the writer's side and
# closed here. A contract test asserts both modules use the same values.
FAN_OUT_CAPABILITY_FIELD = "dispatch_capability"
FAN_OUT_CAPABILITY = "root_coordinator_repository_fan_out"
FAN_OUT_REPOSITORY_FIELD = "dispatch_repository_scope"
# Personas the platform will record as a coordinator. Not a persona the caller
# names: this is compared against the persona on the server-written EXEC row.
COORDINATOR_PERSONAS = frozenset({"operations", "aidlc"})


def _root_coordinator_fan_out(*, grant, raw_grant: dict, parent: dict, target_repo: str, graph_cleared: bool) -> bool:
    """May this caller dispatch to another issue in its own repository? (#5365)

    Every term is read from server-written state: the grant row (written by the
    HMAC-verified webhook path) and the caller's own execution row (written by the
    platform when the run was created). Nothing here is request-supplied — the
    caller contributes only the target it is asking for, which is what the terms
    are checked *against*.

    The conjunction is deliberately narrow:

    * ``github_event`` authority only. A run rooted in an orchestration approval
      (``gate_decision``) must not reach this path; it goes through graph dispatch
      so the approval gate keeps governing flow advancement.
    * The capability marker must be present on the stored grant. A coordinator
      persona alone confers nothing.
    * The repository is matched twice — against the marker the verified event
      wrote, and against the caller's own execution row — so a grant cannot reach
      into a repository its launch did not name.
    * The caller must be a *root* coordinator: no parent principal. A child
      dispatched by a coordinator is not one, which is what stops the capability
      from propagating down a chain even if a child grant ever carried the field.

    ``graph_cleared`` is the SQL-resolved fact from
    :func:`src.agentauth.coordinator.resolve_repository_fan_out`: the target is not
    a graph-owned node and the coordinator's own launch is not an unapproved flow's
    intent. It defaults to False at every caller that cannot resolve it, so a
    synchronous path with no database session gets no fan-out rather than an
    unchecked one.

    This lifts the single-issue pin and nothing else. Budget, concurrency and
    depth ceilings are applied by the caller of this helper and are unaffected.
    """
    return (
        graph_cleared
        and grant.authority.kind == AUTHORITY_GITHUB_EVENT
        and raw_grant.get(FAN_OUT_CAPABILITY_FIELD) == {"S": FAN_OUT_CAPABILITY}
        and raw_grant.get(FAN_OUT_REPOSITORY_FIELD) == {"S": target_repo}
        and not parent.get("parent_principal")
        and parent.get("persona", {}).get("S") in COORDINATOR_PERSONAS
        and parent.get("repo") == {"S": target_repo}
    )


# Which authority kinds may spawn another run at all (#4529). Enumerated rather than
# derived by excluding one kind, so a kind added later cannot dispatch until someone
# adds it here on purpose and says why.
#
#   `gate_decision`  — engine graph dispatch, which must also present a verified graph
#                      assignment (checked immediately below).
#   `github_event`   — the webhook path: a developer run spawning its reviewer.
#   `service_policy` — an accepted coordination policy fanning out to its children.
#
# `replan_request` is excluded: see the note at the check site.
_DISPATCH_AUTHORITY_KINDS: frozenset[str] = frozenset({AUTHORITY_GATE_DECISION, AUTHORITY_GITHUB_EVENT, AUTHORITY_SERVICE_POLICY})


class DispatchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(min_length=3, max_length=256, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    issue: int = Field(gt=0, strict=True)


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: AgentPersona
    target: DispatchTarget
    reason: str = Field(default="", max_length=4096)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")


@dataclass(frozen=True)
class GraphAssignment:
    """Trusted SQL assignment; never parsed from an agent request."""

    node_id: str
    attempt: int
    receipt_id: str
    graph_address: str
    wave_key: str | None = None
    wave_coordinator: bool = False
    review_expect: dict[str, Any] | None = None


class DispatchService:
    def __init__(self, *, store: BootstrapStore, policy: AgentAuthorizationService, queue_url: str, events_table: str, sqs, now=None):
        self.store, self.policy = store, policy
        self.queue_url, self.events_table, self.sqs = queue_url, events_table, sqs
        self.now = now or (lambda: datetime.now(UTC))

    def dispatch(self, *, body: DispatchRequest, credential_token: str, workload_binding: str, fan_out_cleared: bool = False) -> dict:
        prior, grant, caller = self.prepare(
            body=body, credential_token=credential_token, workload_binding=workload_binding, fan_out_cleared=fan_out_cleared
        )
        return self._publish(prior, grant, caller)

    def prepare(
        self,
        *,
        body: DispatchRequest,
        credential_token: str,
        workload_binding: str,
        graph: GraphAssignment | None = None,
        fan_out_cleared: bool = False,
    ):
        """Reserve protected intent without publishing before SQL commit."""
        if not self.queue_url.endswith(".fifo") or not self.events_table:
            raise PolicyError(503, "agent dispatch is not configured")
        caller = self.policy.resolve_caller(credential_token)
        # Scope the idempotency key to the authenticated attempt. Caller-chosen
        # request IDs can neither collide with another run nor replace an intent.
        request_key = envelope_digest({"principal": caller.principal, "request_id": body.request_id})
        pk = f"TENANT#{caller.tenant_id}"
        command_key = f"DISPATCH#{request_key}"
        intent = envelope_digest(body.model_dump())
        prior = self.store._read(pk, command_key)
        if prior and prior.get("intent_digest") != {"S": intent}:
            raise PolicyError(409, "dispatch request ID has different content")
        authorized = self.policy.authorize(
            credential_token=credential_token,
            action=AgentAction.DISPATCH,
            target_run_id=caller.invocation_id,
            presented_workload_binding=workload_binding,
            dispatch_replay=prior is not None,
        )
        grant = self.store.live_grant(invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=self.now())
        if grant != authorized.grant:
            raise BootstrapRefusedError("dispatch authority changed")
        raw_grant = self.store._read(pk, f"GRANT#{caller.principal}")
        parent = self.store._read(pk, f"EXEC#{caller.invocation_id}")
        if not raw_grant or not parent:
            raise BootstrapRefusedError("dispatch authority unavailable")
        # Recognized-authority handling (#4529). The two checks below express
        # "workflow dispatch needs a graph assignment" as a test on `gate_decision`,
        # so a kind this method has never considered previously fell through both:
        # no graph assignment was demanded of it, and it proceeded to dispatch.
        # Enumerating the kinds that may dispatch AT ALL closes that, and does so
        # before any eligibility read, so an unrecognized authority cannot dispatch
        # even if a future grant were minted carrying `DISPATCH`.
        #
        # `replan_request` is absent deliberately and is the case that matters: an
        # AI-DLC authoring run exists to propose a plan change, and a proposal that
        # could spawn executing work would be an amendment applying itself. Its grant
        # already omits `DISPATCH` (see `EngineAuthorityWriter.provision_authoring`);
        # this is the second, independent fence, so neither one alone is load-bearing.
        if grant.authority.kind not in _DISPATCH_AUTHORITY_KINDS:
            raise BootstrapRefusedError("authority kind may not dispatch")
        if grant.authority.kind == AUTHORITY_GATE_DECISION and graph is None:
            raise BootstrapRefusedError("workflow dispatch requires a verified graph assignment")
        if graph is not None and (grant.authority.kind != AUTHORITY_GATE_DECISION or graph.attempt < (0 if graph.wave_coordinator else 1)):
            raise BootstrapRefusedError("invalid graph assignment")
        if graph and graph.wave_coordinator and (body.persona != "operations" or not graph.wave_key):
            raise BootstrapRefusedError("invalid wave coordinator assignment")
        try:
            depth = int(parent.get("chain_depth", {}).get("N", "0")) + 1
            installation = int(parent["installation_id"]["N"])
            allowed_issue = int(raw_grant["work_item_issue"]["N"])
            total_limit = int(raw_grant["max_total_dispatches"]["N"])
            eligible = body.persona in raw_grant.get("dispatch_personas", {}).get("SS", [])
        except (KeyError, ValueError, TypeError):
            raise BootstrapRefusedError("dispatch eligibility unavailable") from None
        if (
            not eligible
            or not grant.flow_id
            or total_limit <= 0
            or body.target.repo not in grant.repo_scope
            or body.target.repo != parent["repo"]["S"]
            or (
                body.target.issue != allowed_issue
                and not (graph and parent.get("coordinator_flow_id") == {"S": grant.flow_id})
                and not (grant.authority.kind == AUTHORITY_SERVICE_POLICY and raw_grant.get("dispatch_issue_scope") == {"S": "repository"})
                and not _root_coordinator_fan_out(
                    grant=grant, raw_grant=raw_grant, parent=parent, target_repo=body.target.repo, graph_cleared=fan_out_cleared
                )
            )
            or installation <= 0
            or not 0 < depth <= grant.max_chain_depth
        ):
            raise BootstrapRefusedError("dispatch is outside delegated work")
        if prior is None:
            now = self.now()
            invocation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp-dispatch:{caller.tenant_id}:{request_key}"))
            envelope = self._envelope(body, caller, grant, invocation, installation, depth, now)
            from src.orchestration.work_admission import enabled as work_claims_enabled

            if work_claims_enabled():
                repository_id = int(parent.get("provider_repository_id", {}).get("N", "0"))
                if repository_id <= 0:
                    raise BootstrapRefusedError("parent dispatch has no immutable repository identity")
                envelope["source_ref"]["provider_repository_id"] = repository_id
                envelope["work_claim_required"] = True
            if graph:
                envelope["orchestration"] = {
                    "node_id": graph.node_id,
                    "flow_id": grant.flow_id,
                    "graph_address": graph.graph_address,
                    "root_decision_id": grant.authority.reference_id,
                }
                if graph.review_expect is not None:
                    if body.persona != "reviewer":
                        raise BootstrapRefusedError("review expectation requires a reviewer assignment")
                    envelope["review_expect"] = graph.review_expect
            child_grant = self._child_grant(body, grant, invocation, graph=graph)
            prior = {
                **_key(pk, command_key),
                "intent_digest": {"S": intent},
                "invocation_id": {"S": invocation},
                "envelope_json": {"S": json.dumps(envelope, separators=(",", ":"))},
                "created_at": {"S": _iso(now)},
                "publish_state": {"S": "pending"},
                "grant_id": {"S": grant.grant_id},
                "reservation_id": {"S": request_key},
            }
            if graph:
                prior.update(
                    orchestration_node_id={"S": graph.node_id},
                    orchestration_node_attempt={"N": str(graph.attempt)},
                    orchestration_dispatch_receipt={"S": graph.receipt_id},
                )
                if graph.wave_key:
                    prior["wave_key"] = {"S": graph.wave_key}
                if graph.wave_coordinator:
                    prior["wave_coordinator"] = {"BOOL": True}
            self._reserve(prior, envelope, child_grant, grant, caller, workload_binding, depth, total_limit, parent, now, raw_grant)
            prior = self.store._read(pk, command_key)
            if not prior or prior.get("intent_digest") != {"S": intent}:
                raise PolicyError(503, "dispatch reservation unavailable")
        if graph and (
            prior.get("orchestration_node_id") != {"S": graph.node_id}
            or prior.get("orchestration_node_attempt") != {"N": str(graph.attempt)}
            or prior.get("orchestration_dispatch_receipt") != {"S": graph.receipt_id}
        ):
            raise PolicyError(409, "dispatch request belongs to a different workflow attempt")
        if graph and json.loads(prior["envelope_json"]["S"]).get("review_expect") != graph.review_expect:
            # Retries keep the originally reserved envelope and its bootstrap digest.
            # A changed head or authority needs a fresh admitted review, never an
            # in-place rewrite of the old child or a second child on the same key.
            raise PolicyError(409, "review dispatch expectation changed; existing request cannot be repointed")
        return prior, grant, caller

    def _child_grant(self, body, parent, invocation, *, graph=None):
        actions = {AgentAction.MONITOR}
        coordinates = (parent.authority.kind == AUTHORITY_SERVICE_POLICY and body.persona in {"operations", "aidlc", "codex"}) or bool(
            graph and graph.wave_coordinator
        )
        if body.persona == "developer" or coordinates:
            actions.add(AgentAction.DISPATCH)
        if not actions <= parent.delegable_actions:
            raise BootstrapRefusedError("child privileges exceed delegation")
        return DelegatedGrant(
            grant_id=f"grant:{invocation}:1",
            tenant_id=parent.tenant_id,
            principal=f"{invocation}#1",
            authority=parent.authority,
            allowed_actions=frozenset(actions),
            target_relationships=frozenset({TargetRelationship.SELF, TargetRelationship.DESCENDANT}),
            flow_id=parent.flow_id,
            repo_scope=frozenset({body.target.repo}),
            expires_at=parent.expires_at,
            max_dispatch_concurrency=parent.max_dispatch_concurrency if graph and graph.wave_coordinator else min(parent.max_dispatch_concurrency, 1),
            max_chain_depth=parent.max_chain_depth,
            delegable_actions=frozenset(actions if coordinates else {AgentAction.MONITOR}),
        )

    @staticmethod
    def _envelope(body, caller, grant, invocation, installation, depth, now):
        return {
            "version": "1.0",
            "channel": "agent",
            "message_id": invocation,
            "tenant_id": caller.tenant_id,
            "persona": body.persona,
            "arrived_at": _iso(now),
            "actor": {"user_id": f"agent:{caller.principal}", "org_id": caller.tenant_id, "kind": "service", "is_bot": True},
            "source_ref": {"installation_id": installation, "repo": body.target.repo, "issue": body.target.issue},
            "intent": {"trigger": "delegated_dispatch", "persona": body.persona},
            "correlation": {
                "correlation_id": grant.flow_id,
                "parent_invocation_id": caller.invocation_id,
                "parent_principal": caller.principal,
                "root_human_id": grant.authority.human_id,
                "is_human_rooted": True,
                "chain_depth": depth,
            },
            "payload": {"issue": {"number": body.target.issue}, "comment": {"body": body.reason}},
        }

    def _reserve(self, command, envelope, child_grant, grant, caller, binding, depth, total_limit, parent, now, raw_grant):
        pk = f"TENANT#{caller.tenant_id}"
        invocation = envelope["message_id"]
        request_key = command["reservation_id"]["S"]
        digest = envelope_digest(envelope)
        execution = {
            **_key(pk, f"EXEC#{invocation}"),
            "invocation_id": {"S": invocation},
            "tenant_id": {"S": caller.tenant_id},
            "current_attempt": {"N": "1"},
            "status": {"S": "pending"},
            "current_credential_epoch": {"N": "1"},
            "min_acceptable_credential_epoch": {"N": "1"},
            "repo": {"S": envelope["source_ref"]["repo"]},
            "flow_id": {"S": grant.flow_id},
            "parent_principal": {"S": caller.principal},
            "arrived_at": {"S": envelope["arrived_at"]},
            "persona": {"S": envelope["persona"]},
            "envelope_digest": {"S": digest},
            "issue_number": {"N": str(envelope["source_ref"]["issue"])},
            "installation_id": {"N": str(envelope["source_ref"]["installation_id"])},
            **(
                {"provider_repository_id": {"N": str(envelope["source_ref"]["provider_repository_id"])}}
                if "provider_repository_id" in envelope["source_ref"]
                else {}
            ),
            "chain_depth": {"N": str(depth)},
            "parent_grant_id": {"S": grant.grant_id},
            "parent_grant_epoch": {"N": str(grant.revocation_epoch)},
            "dispatch_reservation_id": {"S": request_key},
        }
        child_item = self.store._grant_item(child_grant)
        if grant.authority.kind == AUTHORITY_GATE_DECISION:
            try:
                for field in ("orchestration_node_id", "orchestration_node_attempt", "orchestration_dispatch_receipt"):
                    execution[field] = command[field]
            except KeyError:
                raise BootstrapRefusedError("engine assignment unavailable") from None
            if "wave_key" in command:
                execution["wave_key"] = command["wave_key"]
            if command.get("wave_coordinator") == {"BOOL": True}:
                execution["coordinator_flow_id"] = {"S": grant.flow_id}
                execution["wave_coordinator"] = {"BOOL": True}
        child_item["work_item_issue"] = execution["issue_number"]
        child_item["max_total_dispatches"] = {"N": "1"}
        if envelope["persona"] == "developer":
            child_item["dispatch_personas"] = {"SS": ["reviewer"]}
        if command.get("wave_coordinator") == {"BOOL": True}:
            ceiling = min(16, int(raw_grant.get("max_child_dispatches", raw_grant["max_total_dispatches"])["N"]))
            if ceiling < 1:
                raise BootstrapRefusedError("wave dispatch budget unavailable")
            child_item["max_total_dispatches"] = {"N": str(ceiling)}
            child_item["max_child_dispatches"] = {"N": str(ceiling)}
            child_item["dispatch_personas"] = {
                "SS": [p for p in raw_grant["dispatch_personas"]["SS"] if p in {"developer", "reviewer", "operations"}]
            }
        if grant.authority.kind == AUTHORITY_SERVICE_POLICY:
            permitted = raw_grant.get("dispatch_personas", {}).get("SS", [])
            if envelope["persona"] == "developer" and "reviewer" not in permitted:
                child_item.pop("dispatch_personas", None)
            if envelope["persona"] in {"operations", "aidlc", "codex"} and permitted:
                child_item["dispatch_personas"] = {"SS": permitted}
                child_item["dispatch_issue_scope"] = raw_grant.get("dispatch_issue_scope", {"S": "same_issue"})
        lookup = {
            **_key(f"INVOCATION#{invocation}", "DISPATCH"),
            "tenant_id": {"S": caller.tenant_id},
            "envelope_digest": {"S": digest},
            "arrived_at": execution["arrived_at"],
            "grant_digest": {"S": envelope_digest(child_item)},
        }
        reservation = {
            **_key(pk, f"RESV#{grant.grant_id}#{request_key}"),
            "grant_id": {"S": grant.grant_id},
            "reservation_id": {"S": request_key},
            "state": {"S": "held"},
            "created_at": {"S": _iso(now)},
            "updated_at": {"S": _iso(now)},
        }
        # The row shown in Activity is created by the same transaction, before
        # SQS. Workers cannot select or create a different registration row.
        event = {
            "event_id": {"S": invocation},
            "arrived_at": execution["arrived_at"],
            "tenant_id": {"S": caller.tenant_id},
            "status": {"S": "queued"},
            "persona": execution["persona"],
            "repo": execution["repo"],
            "issue_number": execution["issue_number"],
            "actor_kind": {"S": "service"},
            "actor_user_id": {"S": f"agent:{caller.principal}"},
            "root_human_id": {"S": grant.authority.human_id},
            "parent_invocation_id": {"S": caller.invocation_id},
            "correlation_id": {"S": grant.flow_id},
            # Issue #5365: Activity and live acceptance must observe the same
            # credential-bound depth used for authorization. Protected dispatch
            # has no caller-selectable ancestry, so both counters start equal.
            "chain_depth": {"N": str(depth)},
            "credential_chain_depth": {"N": str(depth)},
        }
        transaction = [self.store._put(item) for item in (command, execution, child_item, lookup, reservation)]
        transaction.extend(
            [
                {"Put": {"TableName": self.events_table, "Item": event, "ConditionExpression": "attribute_not_exists(event_id)"}},
                {
                    "Update": {
                        "TableName": self.store.table,
                        "Key": _key(pk, f"RESV#{grant.grant_id}"),
                        "UpdateExpression": "ADD in_flight :one, total_dispatched :one SET updated_at = :now",
                        "ConditionExpression": (
                            "(attribute_not_exists(in_flight) OR in_flight < :ceiling) "
                            "AND (attribute_not_exists(total_dispatched) OR total_dispatched < :total_limit)"
                        ),
                        "ExpressionAttributeValues": {
                            ":one": {"N": "1"},
                            ":now": {"S": _iso(now)},
                            ":ceiling": {"N": str(grant.max_dispatch_concurrency)},
                            ":total_limit": {"N": str(total_limit)},
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": self.store.table,
                        "Key": _key(pk, f"EXEC#{caller.invocation_id}"),
                        "ConditionExpression": (
                            "#st = :active AND current_attempt = :attempt AND current_credential_epoch = :epoch AND workload_binding = :binding"
                        ),
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {
                            ":active": {"S": "active"},
                            ":attempt": {"N": str(caller.attempt)},
                            ":epoch": {"N": str(caller.credential_epoch)},
                            ":binding": {"S": binding},
                        },
                    }
                },
                self.store._grant_check(grant, now),
                self.store._authority_check(grant),
            ]
        )
        try:
            self.store.client.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError):
            existing = self.store._read(pk, command["sk"]["S"])
            if not existing or existing.get("intent_digest") != command["intent_digest"]:
                raise PolicyError(409, "dispatch reservation refused; no work published") from None

    def _publish(self, command, grant, caller):
        invocation = command["invocation_id"]["S"]
        result = {"status": "accepted", "message_id": invocation, "invocation_id": invocation, "correlation_id": grant.flow_id}
        if command.get("publish_state") == {"S": "refused"}:
            self._release_refused(command, caller)
            raise PolicyError(409, "work ownership refused; this request will not publish")
        if command.get("publish_state") == {"S": "published"}:
            return result
        child = self.store.authority.load_execution(invocation_id=invocation, tenant_id=caller.tenant_id)
        if child and child.workload_binding:
            # Real bootstrap is evidence the prior send arrived, even if its
            # acknowledgement or the publish-state update was lost.
            return result
        created = datetime.strptime(command["created_at"]["S"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if (self.now() - created).total_seconds() >= 240:
            raise PolicyError(409, "dispatch outcome unknown; inspect the existing invocation before retrying")
        self.store.live_grant(invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=self.now())
        from src.orchestration.work_admission import admit_pending
        from src.orchestration.work_admission import enabled as work_claims_enabled
        from src.orchestration.work_claims import WorkClaimError

        if work_claims_enabled():
            from functools import partial

            from anyio import from_thread

            try:
                # Gateway dispatch runs in an AnyIO worker; keep SQL on the
                # request event loop rather than creating a second engine/pool.
                from_thread.run(partial(admit_pending, allow_defer=True), self.store, invocation)
            except WorkClaimError as exc:
                self._refuse_unpublished(command, caller)
                raise PolicyError(409, f"work ownership refused: {exc.code}") from None
        try:
            if work_claims_enabled():
                # Fence a concurrent definitive refusal before contacting SQS.
                # 'publishing' means the outcome may be unknown: it must never
                # be compensated as if nothing could have reached the queue.
                self.store.client.update_item(
                    TableName=self.store.table,
                    Key={k: command[k] for k in ("pk", "sk")},
                    UpdateExpression="SET publish_state = :publishing",
                    ConditionExpression="intent_digest = :intent AND publish_state IN (:pending, :publishing)",
                    ExpressionAttributeValues={":publishing": {"S": "publishing"}, ":pending": {"S": "pending"}, ":intent": command["intent_digest"]},
                )
            self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=command["envelope_json"]["S"],
                # A coordinator may await its child while holding its own SQS
                # message. Sharing their FIFO group would deadlock that flow.
                MessageGroupId=envelope_digest({"tenant": caller.tenant_id, "invocation": invocation}),
                MessageDeduplicationId=invocation,
            )
            self.store.client.update_item(
                TableName=self.store.table,
                Key={k: command[k] for k in ("pk", "sk")},
                UpdateExpression="SET publish_state = :published",
                ConditionExpression="intent_digest = :intent",
                ExpressionAttributeValues={":published": {"S": "published"}, ":intent": command["intent_digest"]},
            )
        except (ClientError, BotoCoreError):
            raise PolicyError(503, f"dispatch outcome unknown for invocation {invocation}; retry the same request ID") from None
        logger.info(
            "Agent dispatch published",
            extra={
                "principal": caller.principal,
                "authority_reference_id": grant.authority.reference_id,
                "target_run_id": invocation,
                "action": "dispatch",
                "outcome": "published",
            },
        )
        return result

    def _release_refused(self, command, caller):
        self.store.authority.release_dispatch(
            tenant_id=caller.tenant_id,
            grant_id=command["grant_id"]["S"],
            reservation_id=command["reservation_id"]["S"],
        )

    def _refuse_unpublished(self, command, caller):
        """Fence a never-published child before freeing its concurrency slot.

        A transport ambiguity, or any concurrent publisher that passed the
        publication fence, keeps its reservation. Retrying the same refusal is
        idempotent and cannot refund the historical dispatch-attempt allowance.
        """
        try:
            self.store.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.store.table,
                            "Key": {k: command[k] for k in ("pk", "sk")},
                            "UpdateExpression": "SET publish_state = :refused",
                            "ConditionExpression": "intent_digest = :intent AND publish_state = :pending",
                            "ExpressionAttributeValues": {
                                ":refused": {"S": "refused"},
                                ":pending": {"S": "pending"},
                                ":intent": command["intent_digest"],
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.store.table,
                            "Key": _key(f"TENANT#{caller.tenant_id}", f"EXEC#{command['invocation_id']['S']}"),
                            "UpdateExpression": "SET #st = :cancelled",
                            "ConditionExpression": "#st = :pending AND attribute_not_exists(workload_binding)",
                            "ExpressionAttributeNames": {"#st": "status"},
                            "ExpressionAttributeValues": {":cancelled": {"S": "cancelled"}, ":pending": {"S": "pending"}},
                        }
                    },
                ]
            )
        except (ClientError, BotoCoreError):
            current = self.store._read(command["pk"]["S"], command["sk"]["S"])
            if not current or current.get("publish_state") != {"S": "refused"}:
                return
        self._release_refused(command, caller)
