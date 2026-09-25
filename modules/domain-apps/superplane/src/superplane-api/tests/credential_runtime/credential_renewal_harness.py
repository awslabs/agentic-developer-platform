"""Stateful Kubernetes SDK double around production renewal and real PostgreSQL."""

import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.membership import SharedMembership
from superplane_bootstrap.namespace_admission import policy_documents

from workspace_provisioning.credential_controller import registry
from workspace_provisioning.credential_controller.renewal import (
    Renewal,
    credential_rows,
)
from workspace_provisioning.shared_membership import reserve

from workspace_provisioning.tests.test_credential_authority import authority_document
from workspace_provisioning.tests.test_member_credentials import API, ApiError, Resource
from workspace_bootstrap.tests import conftest as identities


class RenewalResource(Resource):
    def create(self, body, namespace=None):
        value = deepcopy(body)
        key = (value["kind"], namespace, value["metadata"]["name"])
        if key in self.api.objects:
            raise ApiError(409)
        value["metadata"].update(uid=str(uuid4()), resourceVersion="1")
        self.api.effects.append(("create", key))
        self.api.objects[key] = value
        return deepcopy(value)

    def patch(self, **kwargs):
        super().patch(**kwargs)
        self.api.effects.append(("patch", kwargs["name"]))
        if self.api.lose_publish:
            self.api.lose_publish = False
            raise OSError("response lost after the server committed the Secret CAS")

    def delete(self, **kwargs):
        super().delete(**kwargs)
        self.api.effects.append(
            ("delete", (self.kind, kwargs["namespace"], kwargs["name"]))
        )


class RenewalAPI(API):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.effects, self.tokens = [], {}
        self.lose_publish, self.on_token = False, None

    def get(self, api_version, kind):
        return RenewalResource(self, kind)

    def call_api(self, path, method, **kwargs):
        assert method == "POST" and path.endswith("/token")
        namespace, name = path.split("/")[4], path.split("/")[6]
        sa = self.objects[("ServiceAccount", namespace, name)]
        token = "private-test-member-" + str(uuid4())
        self.tokens[token] = (namespace, name, sa["metadata"]["uid"])
        self.effects.append(("token", (namespace, name)))
        if self.on_token:
            callback, self.on_token = self.on_token, None
            callback()
        return {
            "status": {
                "token": token,
                "expirationTimestamp": (
                    datetime.now(UTC) + timedelta(minutes=10)
                ).isoformat(),
            }
        }


class RenewalHarness:
    def __init__(self, postgres, directory, monkeypatch):
        self.postgres, self.directory = postgres, directory
        self.holder, self.fence = "installed-renewal-controller", None
        self.document = authority_document(org_id=identities.ORG_ID)
        self.authority = registry.Authority.read(
            str(uuid4()), json.dumps(self.document)
        )
        self.members = [
            SharedMembership.create(
                org_id=identities.ORG_ID,
                workspace_id=str(uuid4()),
                cluster_id=self.document["cluster_id"],
                request_id=str(uuid4()),
                cluster_arn=self.document["target"]["cluster_arn"],
                endpoint=self.document["target"]["endpoint"],
            )
            for _ in range(2)
        ]
        self.namespace_uids = {
            member.workspace_id: "ns-" + str(uuid4()) for member in self.members
        }
        self.issuer_api = self.api(self.document["target"], "issuer")
        self.projector_api = self.api(self.document["management_target"], "projector")
        for body, uid in zip(
            policy_documents(self.document["issuer"]["group"]),
            ("policy-uid", "binding-uid"),
            strict=True,
        ):
            body["metadata"].update(uid=uid, generation=1)
            if body["kind"] == "ValidatingAdmissionPolicy":
                body["status"] = {
                    "observedGeneration": 1,
                    "typeChecking": {"expressionWarnings": []},
                }
            self.issuer_api.objects[(body["kind"], None, body["metadata"]["name"])] = (
                body
            )
        for member in self.members:
            self.issuer_api.objects[("Namespace", None, member.namespace)] = {
                "metadata": {
                    "name": member.namespace,
                    "uid": self.namespace_uids[member.workspace_id],
                },
                "status": {"phase": "Active"},
            }
        projection = self.document["projection"]
        self.projector_api.objects[("Namespace", None, projection["namespace"])] = {
            "metadata": {
                "name": projection["namespace"],
                "uid": projection["namespace_uid"],
            },
            "status": {"phase": "Active"},
        }
        for scope in ("reader", "mutator"):
            name = projection[scope + "_secret"]
            self.projector_api.objects[("Secret", projection["namespace"], name)] = {
                "metadata": {
                    "name": name,
                    "uid": projection[scope + "_secret_uid"],
                    "resourceVersion": "1",
                    "annotations": {"unrelated": "preserve"},
                },
                "data": {
                    "unrelated.kubeconfig": base64.b64encode(b"unrelated").decode()
                },
                "type": "Opaque",
            }
        self.proofs = []
        harness = self

        class ConsumerAPI:
            def __init__(self, configuration):
                self.configuration = configuration
                assert configuration.host == harness.document["target"]["endpoint"]
                assert Path(configuration.ssl_ca_cert).read_bytes() == base64.b64decode(
                    harness.document["target"]["certificate_authority_data"]
                )
                assert configuration.verify_ssl is True and not configuration.proxy

            def call_api(self, path, method, **kwargs):
                token = self.configuration.api_key["authorization"]
                namespace, name, uid = harness.issuer_api.tokens[token]
                current = harness.issuer_api.objects.get(
                    ("ServiceAccount", namespace, name)
                )
                if current is None or current["metadata"]["uid"] != uid:
                    raise ApiError(401)
                harness.proofs.append((namespace, name, path, method))
                if path.endswith("/selfsubjectreviews"):
                    return {
                        "status": {
                            "userInfo": {
                                "uid": uid,
                                "username": f"system:serviceaccount:{namespace}:{name}",
                            }
                        }
                    }
                if path == f"/api/v1/namespaces/{namespace}/pods" and method == "GET":
                    return {"kind": "PodList", "items": []}
                if path.endswith("/selfsubjectaccessreviews"):
                    attrs = kwargs["body"]["spec"]["resourceAttributes"]
                    allowed = (
                        attrs.get("namespace") == namespace
                        and name.startswith("sp-mutator-")
                        and attrs["resource"] in {"pods", "jobs", "superplanenodes"}
                        and attrs["verb"] in {"create", "patch", "delete"}
                    )
                    return {"status": {"allowed": allowed}}
                raise AssertionError("unscoped projected consumer request")

            def close(self):
                pass

        monkeypatch.setattr("kubernetes.client.ApiClient", ConsumerAPI)
        self.postgres.run(self.seed())

    def api(self, target, name):
        path = self.directory / (name + ".ca")
        path.write_bytes(base64.b64decode(target["certificate_authority_data"]))
        return RenewalAPI(
            SimpleNamespace(
                host=target["endpoint"],
                ssl_ca_cert=str(path),
                verify_ssl=True,
                assert_hostname=None,
                tls_server_name=None,
                proxy=None,
            )
        )

    async def seed(self):
        async with self.postgres.connect() as c:
            await c.execute(
                "INSERT INTO clusters(id,org_id,name,status,sharing_enabled,eks_cluster_arn,endpoint) VALUES($1,$2,'shared','Ready',true,$3,$4)",
                UUID(self.document["cluster_id"]),
                UUID(identities.ORG_ID),
                self.members[0].cluster_arn,
                self.members[0].endpoint,
            )
            for member in self.members:
                await c.execute(
                    "INSERT INTO workspaces(id,org_id,name,status,isolation_mode,is_default) VALUES($1,$2,$3,'Provisioning','namespace',false)",
                    UUID(member.workspace_id),
                    UUID(member.org_id),
                    member.namespace,
                )
                async with c.transaction():
                    await reserve(c, member)
                await c.execute(
                    "UPDATE cluster_memberships SET state='active',namespace_uid=$2 WHERE workspace_id=$1",
                    UUID(member.workspace_id),
                    self.namespace_uids[member.workspace_id],
                )
                await c.execute(
                    "UPDATE workspaces SET status='Ready' WHERE id=$1",
                    UUID(member.workspace_id),
                )
            await c.execute(
                "INSERT INTO cluster_credential_authorities(authority_id,org_id,cluster_id,document_json,enabled) VALUES($1,$2,$3,$4,true)",
                UUID(self.authority.authority_id),
                UUID(identities.ORG_ID),
                UUID(self.document["cluster_id"]),
                self.authority.document_json,
            )
            self.fence = await registry.acquire(c, self.authority, self.holder)

    def store(self, function, *args, **kwargs):
        async def run():
            async with self.postgres.connect() as c:
                async with c.transaction():
                    await registry.verify(c, self.authority, self.holder, self.fence)
                    return await function(c, *args, **kwargs)

        return self.postgres.run(run())

    def authorize(self, binding, action):
        async def run():
            async with self.postgres.connect() as c:
                await registry.verify(
                    c, self.authority, self.holder, self.fence, binding, action
                )
                await registry.renew(c, self.authority, self.holder, self.fence)

        self.postgres.run(run())

    def sql(self, query, *args):
        async def run():
            async with self.postgres.connect() as c:
                return await c.execute(query, *args)

        return self.postgres.run(run())

    def engine(self, member):
        return Renewal(
            authority=self.authority,
            store=self.store,
            authorize=self.authorize,
            issuer_grants=KubeGrants(
                self.issuer_api, self.authority.target(member.workspace_id)
            ),
            projector_grants=KubeGrants(
                self.projector_api,
                self.authority.target(member.workspace_id, management=True),
            ),
            directory=self.directory,
        )

    def reconcile(self, member, **kwargs):
        self.engine(member).reconcile(
            member, self.namespace_uids[member.workspace_id], **kwargs
        )

    def rows(self, member):
        return self.store(credential_rows, member)

    def secret(self, scope):
        return self.projector_api.objects[
            (
                "Secret",
                self.document["projection"]["namespace"],
                self.document["projection"][scope + "_secret"],
            )
        ]
