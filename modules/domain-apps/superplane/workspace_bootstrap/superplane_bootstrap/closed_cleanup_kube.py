"""Private, CA-pinned GET/DELETE transport for the sealed original object roster.

No discovery, list, secrets, token requests, exec, impersonation, redirects or
caller-selected URLs. Reads use an independent service-held observer; deletes
use a fresh registrar session and exact UID/resourceVersion preconditions.
"""

import base64
import http.client
import json
import re
import ssl
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from .closed_cleanup_permit import object_scope, require
from .kube_grants import KubeGrants

RESOURCES = {
    ("v1", "Namespace"): "namespaces",
    ("v1", "ServiceAccount"): "serviceaccounts",
    ("rbac.authorization.k8s.io/v1", "Role"): "roles",
    ("rbac.authorization.k8s.io/v1", "RoleBinding"): "rolebindings",
    ("rbac.authorization.k8s.io/v1", "ClusterRole"): "clusterroles",
    ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"): "clusterrolebindings",
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
    ): "validatingadmissionpolicies",
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
    ): "validatingadmissionpolicybindings",
}


def token(session, target):
    from botocore.signers import RequestSigner

    sts = session.client("sts", region_name=target.region)
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        target.region,
        "sts",
        "v4",
        session.get_credentials(),
        session._session.get_component("event_emitter"),
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": sts.meta.endpoint_url
            + "/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": target.cluster_name},
            "context": {},
        },
        region_name=target.region,
        expires_in=60,
        operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class ScopedKubernetes:
    def __init__(
        self,
        permit,
        target,
        reader,
        registrar,
        revalidate,
        *,
        connection_factory=http.client.HTTPSConnection,
    ):
        self.target, self.reader, self.registrar, self.revalidate = (
            target,
            reader,
            registrar,
            revalidate,
        )
        self.reads, self.deletes = object_scope(permit)
        self.retirement_policy = None
        if any(key[1] == "ValidatingAdmissionPolicyBinding" for key in self.deletes):
            qualified = permit["qualification"]
            spec = next(
                s
                for s in qualified["retained_plan"]["grants"]
                if s["key"] == "retirement-fence-policy"
            )
            self.retirement_policy = (
                spec,
                qualified["retained_progress"][spec["key"]]["identity"],
            )
        self.connection_factory = connection_factory
        self._read_batch = None
        endpoint = urlsplit(target.endpoint)
        require(
            endpoint.scheme == "https"
            and endpoint.hostname
            and not endpoint.username
            and not endpoint.password
        )
        require(
            endpoint.path in {"", "/"}
            and not endpoint.query
            and not endpoint.fragment
            and endpoint.port in {None, 443}
        )
        self.host = endpoint.hostname
        self.context = ssl.create_default_context(
            cadata=base64.b64decode(
                target.certificate_authority_data, validate=True
            ).decode()
        )

    @staticmethod
    def key(body):
        metadata = body["metadata"]
        return (
            body["apiVersion"],
            body["kind"],
            metadata.get("namespace", ""),
            metadata["name"],
        )

    def request(self, method, body, *, deletion=None):
        key = self.key(body)
        require(key in self.reads and self.reads[key] == body)
        require(method == "GET" or method == "DELETE" and key in self.deletes)
        version, kind, namespace, name = key
        require((version, kind) in RESOURCES)
        require(
            all(
                re.fullmatch(r"[a-z0-9][a-z0-9.-]*", value)
                for value in (name, namespace)
                if value
            )
        )
        path = ("/api/" if version == "v1" else "/apis/") + version
        if namespace:
            path += "/namespaces/" + namespace
        path += "/" + RESOURCES[(version, kind)] + "/" + name
        batch = self._read_batch
        if batch is not None:
            require(method == "GET" and deletion is None and key in batch["keys"])
            require(time.monotonic() < batch["deadline"])
            authorization = batch["authorization"]
        else:
            self.revalidate()
            session = self.reader() if method == "GET" else self.registrar()
            authorization = token(session, self.target)
        headers = {
            "Authorization": "Bearer " + authorization,
            "Accept": "application/json",
        }
        payload = None
        if method == "DELETE":
            require(
                isinstance(deletion, dict)
                and set(deletion) == {"uid", "resourceVersion"}
            )
            require(
                deletion["uid"] == self.deletes[key]["uid"]
                and deletion["resourceVersion"]
            )
            headers["Content-Type"] = "application/json"
            payload = json.dumps(
                {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": deletion}
            ).encode()
        if batch is None:
            self.revalidate()
        connection = self.connection_factory(
            self.host, 443, context=self.context, timeout=15
        )
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024)
            # Never include server bodies, URLs or authentication headers in an error.
            if response.status == 404 and method == "GET":
                result = None
            else:
                require(
                    response.status == 200
                    or method == "DELETE"
                    and response.status == 202
                )
                result = json.loads(raw)
                require(isinstance(result, dict))
        finally:
            connection.close()
        if batch is None:
            self.revalidate()
        else:
            require(self._read_batch is batch and time.monotonic() < batch["deadline"])
        return result

    @contextmanager
    def read_batch(self, bodies):
        """One finite named-read pass, with fresh authority around all its I/O.

        No deletion can use this context. Results cannot establish cleanup until
        the final current source/owner/operation/enrollment check succeeds. Each
        pass revalidates its service-held reader; none of its proof is reused by a
        later pass or mutation. Network transport still pins the original CA and
        refuses redirects, unlisted objects, oversized responses and bad status.
        """
        require(self._read_batch is None)
        require(
            isinstance(bodies, (list, tuple)) and 0 < len(bodies) <= len(self.reads)
        )
        keys = [self.key(body) for body in bodies]
        require(len(set(keys)) == len(keys))
        require(
            all(
                key in self.reads and self.reads[key] == body
                for key, body in zip(keys, bodies)
            )
        )
        self.revalidate()
        authorization = token(self.reader(), self.target)
        self.revalidate()
        deadline = time.monotonic() + 30
        self._read_batch = {
            "keys": frozenset(keys),
            "authorization": authorization,
            "deadline": deadline,
        }
        try:
            yield
        finally:
            self._read_batch = None
            self.revalidate()
            require(time.monotonic() < deadline)

    def wait_ready(self):
        # Propagation probes only the original namespace with the independent
        # reader. No native deletion starts until that reader can observe it.
        from .errors import BootstrapRefused

        namespace = next(
            body for key, body in self.reads.items() if key[1] == "Namespace"
        )
        for attempt in range(16):
            self.revalidate()
            try:
                require(self.request("GET", namespace) is not None)
                return
            except BootstrapRefused:
                if attempt == 15:
                    raise
                time.sleep(2)

    def read_component(self, body):
        return self.request("GET", body)

    def observe(self, spec):
        require(spec["cluster_arn"] == self.target.cluster_arn)
        body = self.request("GET", spec["body"])
        return None if body is None else KubeGrants._identity(self, spec, body)

    def verify(self, spec, identity):
        KubeGrants.verify(self, spec, identity)

    def delete(self, spec, identity):
        key = self.key(spec["body"])
        require(key in self.deletes and identity == self.deletes[key])
        if key[1] == "ValidatingAdmissionPolicyBinding":
            # Native revocation removes the binding first. Its unchanged body
            # alone cannot prove the policy has remained dormant under the lock.
            policy, original = self.retirement_policy
            current = self.request("GET", policy["body"])
            if current is not None:
                require(KubeGrants._identity(self, policy, current) == original)
                self.verify(policy, original)
                require(not current["metadata"].get("finalizers"))
        body = self.request("GET", spec["body"])
        require(body is not None and KubeGrants._identity(self, spec, body) == identity)
        self.verify(spec, identity)
        require(not body["metadata"].get("finalizers"))
        self.request(
            "DELETE",
            spec["body"],
            deletion={
                "uid": identity["uid"],
                "resourceVersion": body["metadata"].get("resourceVersion"),
            },
        )
