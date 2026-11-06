"""Read-only provider verification using the selected L5 broker session."""

import base64
import asyncio
import logging
import re
import ssl
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from l5_services.devops.resource_scope import ScopeError, evaluate_role_scope

LOGGER = logging.getLogger(__name__)


def _association_call(client, operation, **parameters):
    """Retain the failed API step without logging provider payloads or credentials."""
    try:
        return getattr(client, operation)(**parameters)
    except (ClientError, BotoCoreError, ConnectionError, TimeoutError) as exc:
        response = exc.response if isinstance(exc, ClientError) else {}
        code = (response.get("Error") or {}).get("Code", type(exc).__name__)
        request_id = (response.get("ResponseMetadata") or {}).get("RequestId", "unavailable")
        def safe(value):
            return str(value) if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(value)) else "unavailable"
        LOGGER.warning("DevOps association API failed: operation=%s code=%s request_id=%s",
                       operation, safe(code), safe(request_id))
        if operation == "associate_service":
            reason = "association_outcome_unknown"
        else:
            stage = "discovery" if operation == "list_associations" else "readback"
            failure = "denied" if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException"} else "unavailable"
            reason = f"association_{stage}_{failure}"
        raise AccessDenied(reason) from exc


class AccessDenied(ValueError):
    """A selected resource cannot be verified without widening its scope."""


class KubernetesReader:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def read(self, aws_session, cluster: dict, cluster_name: str, namespace: str) -> dict:
        from botocore.signers import RequestSigner

        endpoint = cluster.get("endpoint")
        certificate = (cluster.get("certificateAuthority") or {}).get("data")
        if not isinstance(endpoint, str) or urlparse(endpoint).scheme != "https" or not certificate:
            raise AccessDenied("cluster endpoint or certificate is unavailable")
        try:
            authority = base64.b64decode(certificate, validate=True)
            credentials = aws_session.get_credentials()
            signer = RequestSigner(aws_session.client("sts").meta.service_model.service_id,
                                   aws_session.region_name, "sts", "v4", credentials,
                                   aws_session._session.get_component("event_emitter"))
            url = signer.generate_presigned_url(
                {"method": "GET", "url": f"https://sts.{aws_session.region_name}.amazonaws.com/"
                 "?Action=GetCallerIdentity&Version=2011-06-15",
                 "body": {}, "headers": {"x-k8s-aws-id": cluster_name}, "context": {}},
                operation_name="", expires_in=60, region_name=aws_session.region_name,
            )
            token = "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
            headers = {"Authorization": f"Bearer {token}"}
            namespace_path = f"/api/v1/namespaces/{quote(namespace, safe='')}"

            context = ssl.create_default_context(cadata=authority.decode())
            async with httpx.AsyncClient(verify=context, transport=self.transport, timeout=10,
                                         trust_env=False) as client:
                async def get(path):
                    response = await client.get(endpoint.rstrip("/") + path, headers=headers)
                    response.raise_for_status()
                    return response

                pods = (await get(namespace_path + "/pods")).json().get("items")
                events = (await get(namespace_path + "/events")).json().get("items")
                if not isinstance(pods, list) or not isinstance(events, list):
                    raise AccessDenied("namespace pod or event readback is incomplete")
                if not pods or not (pods[0].get("metadata") or {}).get("name"):
                    raise AccessDenied("no selected pod is available for log readback")
                await get(namespace_path + "/pods/" + quote(pods[0]["metadata"]["name"], safe="") + "/log")
                return {"pods": len(pods), "events": len(events), "logs_read": 1}
        except (httpx.HTTPError, ValueError, KeyError, ssl.SSLError, UnicodeDecodeError) as exc:
            raise AccessDenied("selected namespace, network or log read unavailable") from exc


class AWSAccessAdapter:
    def __init__(self, broker_transport: httpx.AsyncClient, *, aws_session_factory=None,
                 kubernetes_reader=None, clock=None):
        self.broker = broker_transport
        self.aws_session_factory = aws_session_factory or self._aws_session
        self.kubernetes_reader = kubernetes_reader or KubernetesReader()
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _aws_session(credentials, region):
        import boto3

        return boto3.Session(aws_access_key_id=credentials["access_key_id"],
                             aws_secret_access_key=credentials["secret_access_key"],
                             aws_session_token=credentials["session_token"], region_name=region)

    @staticmethod
    def _association(host_client, space_id, selection, host_account, *, reconcile_only):
        configuration = ({"aws": {"accountId": selection.account_id, "accountType": "monitor",
                                  "assumableRoleArn": selection.role_arn}}
                         if selection.account_id == host_account else
                         {"sourceAws": {"accountId": selection.account_id, "accountType": "source",
                                        "assumableRoleArn": selection.role_arn}})
        matches = []
        listed = 0
        token = None
        visited = set()
        while True:
            page = _association_call(host_client, "list_associations", agentSpaceId=space_id,
                                     **({"nextToken": token} if token else {}))
            if not isinstance(page.get("associations"), list):
                raise AccessDenied("association_discovery_incomplete")
            for item in page["associations"]:
                listed += 1
                current = item.get("configuration") or {}
                selected = current.get("aws") or current.get("sourceAws") or {}
                if selected.get("accountId") == selection.account_id:
                    if item.get("agentSpaceId") != space_id or current != configuration:
                        raise AccessDenied("association_scope_conflict")
                    matches.append(item)
            token = page.get("nextToken")
            if not token:
                break
            if token in visited or len(visited) >= 20:
                raise AccessDenied("association_discovery_incomplete")
            visited.add(token)
        if len(matches) > 1:
            raise AccessDenied("duplicate_resource_associations")
        if matches:
            association_id = matches[0].get("associationId")
        else:
            if reconcile_only:
                LOGGER.warning("DevOps association reconciliation found no matching account: listed=%d", listed)
                raise AccessDenied("association_outcome_unknown")
            created = _association_call(host_client, "associate_service", agentSpaceId=space_id,
                                        serviceId="aws", configuration=configuration)
            association_id = (created.get("association") or {}).get("associationId")
        if not association_id:
            raise AccessDenied("association_outcome_unknown")
        observed = _association_call(host_client, "get_association", agentSpaceId=space_id,
                                     associationId=association_id)
        association = observed.get("association") or {}
        if (association.get("associationId") != association_id
                or association.get("agentSpaceId") != space_id
                or association.get("serviceId") != "aws"
                or association.get("configuration") != configuration):
            raise AccessDenied("association_readback_mismatch")
        if association.get("status") != "valid":
            raise AccessDenied("association_not_active")
        return {"association_id": association_id, "association_status": "valid",
                "association_readback": {key: association[key] for key in ("associationId", "agentSpaceId", "serviceId", "configuration", "status")},
                "account_type": "monitor" if selection.account_id == host_account else "source"}

    @staticmethod
    def _eks_access(eks, selection, cluster):
        mode = (cluster.get("accessConfig") or {}).get("authenticationMode")
        if mode not in {"API", "API_AND_CONFIG_MAP"}:
            raise AccessDenied("eks_api_authentication_unavailable")
        network = cluster.get("resourcesVpcConfig") or {}
        public = network.get("endpointPublicAccess") is True
        private = network.get("endpointPrivateAccess") is True
        if not public and not private:
            raise AccessDenied("eks_network_access_unverified")
        try:
            entry = eks.describe_access_entry(clusterName=selection.cluster_name,
                                              principalArn=selection.role_arn).get("accessEntry")
        except (ClientError, BotoCoreError) as exc:
            raise AccessDenied("eks_access_entry_unavailable") from exc
        if (not isinstance(entry, dict) or entry.get("clusterName") != selection.cluster_name
                or entry.get("principalArn") != selection.role_arn or entry.get("type") != "STANDARD"
                or entry.get("kubernetesGroups")):
            raise AccessDenied("eks_resource_role_access_entry_unverified")
        policies = []
        token = None
        visited = set()
        while True:
            try:
                page = eks.list_associated_access_policies(
                    clusterName=selection.cluster_name, principalArn=selection.role_arn,
                    **({"nextToken": token} if token else {}))
            except (ClientError, BotoCoreError) as exc:
                raise AccessDenied("eks_access_policy_readback_unavailable") from exc
            if (not isinstance(page, dict) or page.get("clusterName") != selection.cluster_name
                    or page.get("principalArn") != selection.role_arn):
                raise AccessDenied("eks_access_policy_readback_unverified")
            if not isinstance(page.get("associatedAccessPolicies"), list):
                raise AccessDenied("eks_access_policy_readback_unverified")
            policies.extend(page["associatedAccessPolicies"])
            token = page.get("nextToken")
            if not token:
                break
            if token in visited or len(visited) >= 20:
                raise AccessDenied("eks_access_policy_readback_incomplete")
            visited.add(token)
        expected_policy = "arn:aws:eks::aws:cluster-access-policy/AmazonAIOpsAssistantPolicy"
        if (len(policies) != 1 or not isinstance(policies[0], dict)
                or policies[0].get("policyArn") != expected_policy
                or policies[0].get("accessScope") != {"type": "namespace",
                                                      "namespaces": [selection.namespace]}):
            raise AccessDenied("eks_access_policy_outside_namespace")
        return {"principal_arn": selection.role_arn, "policy_arn": expected_policy,
                "namespace": selection.namespace, "authentication_mode": mode,
                "network_mode": "both" if public and private else "public" if public else "private"}

    async def verify(self, selection, *, delegation_id, delegation_revision, operation_id, attempt_id,
                     host_account, space_arn, host_client, space_id, reconcile_only=False):
        selection.fingerprint()
        request = {"delegation_id": delegation_id, "delegation_revision": delegation_revision,
                   "operation_id": operation_id, "attempt_id": attempt_id,
                   "connection_ref": selection.connection_ref}
        try:
            response = await self.broker.post("/internal/v1/l5/credential-session", json=request)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise AccessDenied("selected_connection_revoked") from exc
            if exc.response.status_code in {409, 410}:
                raise AccessDenied("selected_connection_changed") from exc
            raise AccessDenied("selected_connection_unavailable") from exc
        except httpx.TransportError as exc:
            raise AccessDenied("selected_connection_unavailable") from exc
        credentials = response.json()
        identity = credentials.get("provider_identity") or {}
        if (credentials.get("operation_id"), credentials.get("attempt_id"),
            credentials.get("delegation_revision")) != (operation_id, attempt_id, delegation_revision):
            raise AccessDenied("broker session belongs to another operation")
        if (identity.get("account_id"), identity.get("role_arn"), identity.get("region")) != (
            selection.account_id, selection.setup_role_arn, selection.region
        ):
            raise AccessDenied("selected setup role, account or Region does not match broker readback")
        try:
            expiry = datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError) as exc:
            raise AccessDenied("selected_resource_session_expiry_unknown") from exc
        if expiry.tzinfo is None or expiry <= self.clock():
            raise AccessDenied("selected resource session has expired")
        session = self.aws_session_factory(credentials, selection.region)
        return await self.verify_session(selection, session=session, expiry=expiry, host_account=host_account,
                                         space_arn=space_arn, host_client=host_client, space_id=space_id,
                                         reconcile_only=reconcile_only)

    async def verify_session(self, selection, *, session, expiry, host_account, space_arn,
                             host_client, space_id, reconcile_only=False, inline_only=False):
        """Use a lease-guarded installed session, never ambient provider credentials."""
        fingerprint = selection.fingerprint()
        if expiry.tzinfo is None or expiry <= self.clock():
            raise AccessDenied("selected resource session has expired")
        if selection.workload_kind == "eks":
            expected_cluster_arn = (f"arn:aws:eks:{selection.region}:{selection.account_id}:"
                                    f"cluster/{selection.cluster_name}")
            allowed_actions = {"eks:DescribeCluster": {
                expected_cluster_arn}}
        else:
            allowed_actions = {"lambda:GetFunction": {selection.resource_arn},
                               "logs:FilterLogEvents": {selection.telemetry_arn}}
        try:
            policy = await asyncio.to_thread(evaluate_role_scope, session.client("iam"),
                                             selection.role_arn, space_arn, host_account, allowed_actions, inline_only=inline_only)
        except ScopeError as exc:
            raise AccessDenied(str(exc)) from exc
        except (ClientError, BotoCoreError, KeyError, TypeError) as exc:
            raise AccessDenied("effective_policy_readback_unavailable") from exc
        if selection.workload_kind == "eks":
            try:
                eks = session.client("eks")
                cluster = (await asyncio.to_thread(eks.describe_cluster,
                                                    name=selection.cluster_name)).get("cluster") or {}
            except (ClientError, BotoCoreError) as exc:
                raise AccessDenied("selected_cluster_unavailable") from exc
            if cluster.get("name") != selection.cluster_name or cluster.get("arn") != expected_cluster_arn:
                raise AccessDenied("cluster readback is outside selected account or Region")
            access_entry = await asyncio.to_thread(self._eks_access, eks, selection, cluster)
            reads = await self.kubernetes_reader.read(session, cluster, selection.cluster_name, selection.namespace)
            if not all(reads.get(key, 0) >= 1 for key in ("pods", "logs_read")) or "events" not in reads:
                raise AccessDenied("selected namespace readback is incomplete")
            resource = {"cluster_arn": cluster["arn"], "namespace": selection.namespace,
                        "eks_access": access_entry, "observations": reads}
        else:
            try:
                function = await asyncio.to_thread(session.client("lambda").get_function,
                                                   FunctionName=selection.resource_arn)
                if (function.get("Configuration") or {}).get("FunctionArn") != selection.resource_arn:
                    raise AccessDenied("resource_readback_outside_selection")
                log_group_name = selection.telemetry_arn.split(":log-group:", 1)[1][:-2]
                logs = await asyncio.to_thread(session.client("logs").filter_log_events,
                                               logGroupName=log_group_name, limit=1)
            except (ClientError, BotoCoreError) as exc:
                raise AccessDenied("selected_resource_or_telemetry_unavailable") from exc
            if not isinstance(logs.get("events"), list):
                raise AccessDenied("telemetry_readback_incomplete")
            resource = {"resource_arn": selection.resource_arn, "telemetry_arn": selection.telemetry_arn,
                        "observations": {"functions": 1, "log_events_sampled": len(logs["events"])}}
        try:
            association = await asyncio.to_thread(self._association, host_client, space_id, selection,
                                                   host_account, reconcile_only=reconcile_only)
        except (ClientError, BotoCoreError, ConnectionError, TimeoutError) as exc:
            raise AccessDenied("association_outcome_unknown") from exc
        return {"status": "verifying", "account_id": selection.account_id, "region": selection.region,
                "role_arn": selection.role_arn, **resource, **association, "effective_scope": policy,
                "scope_fingerprint": fingerprint, "checked_at": self.clock().isoformat(),
                "expires_at": expiry.isoformat()}
