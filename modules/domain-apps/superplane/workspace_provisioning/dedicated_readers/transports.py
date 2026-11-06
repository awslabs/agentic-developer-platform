"""Installed IRSA issuer and narrowly scoped management ServiceAccount projector."""

import base64
import json
import os
import tempfile
from pathlib import Path

from .renewal import readonly, require


class Registry:
    def __init__(self, config):
        from urllib.parse import urlsplit

        import httpx

        url = urlsplit(config["api_url"])
        require(
            url.scheme == "https"
            or (
                url.scheme == "http"
                and url.hostname
                and url.hostname.endswith(".svc.cluster.local")
            ),
            "Reader API origin refused",
        )
        require(
            not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and url.path in {"", "/"},
            "Reader API origin differs",
        )
        self.config = config
        self.client = httpx.Client(timeout=8, trust_env=False, follow_redirects=False)

    def read(self, workspace_id):
        credential = Path(self.config["credential_file"]).read_text().strip()
        response = self.client.get(
            self.config["api_url"].rstrip("/") + "/internal/readers/" + workspace_id,
            headers={"Authorization": credential},
        )
        require(
            response.status_code == 200 and len(response.content) < 32768,
            "Reader registry unavailable",
        )
        return response.json()


class Projector:
    def __init__(self, config):
        from kubernetes import client

        self.config = config["projection"]
        selected = client.Configuration()
        selected.host = self.config["endpoint"]
        selected.ssl_ca_cert = "/var/run/secrets/reader-management/ca.crt"
        selected.verify_ssl, selected.proxy = True, None
        selected.api_key_prefix["authorization"] = "Bearer"

        def refresh(current):
            current.api_key["authorization"] = Path(
                "/var/run/secrets/reader-management/token"
            ).read_text()

        selected.refresh_api_key_hook = refresh
        # auth_settings() only invokes the refresh hook for registered keys.
        # Seed the entry before ApiClient copies this configuration; later
        # requests still reread the projected token through the hook.
        refresh(selected)
        self.client = client.ApiClient(selected)
        self.core = client.CoreV1Api(self.client)

    def read(self):
        response = self.core.read_namespaced_secret(
            self.config["secret_name"],
            self.config["namespace"],
            _request_timeout=8,
            _preload_content=False,
        )
        return json.loads(response.data)

    def replace(self, document):
        self.core.replace_namespaced_secret(
            self.config["secret_name"],
            self.config["namespace"],
            document,
            _request_timeout=8,
        )


class Issuer:
    def __init__(self, config):
        import boto3

        self.config = config
        self.source = boto3.Session(region_name=config["region"])
        credentials = self.source.get_credentials()
        require(
            credentials is not None
            and credentials.method == "assume-role-with-web-identity"
            and os.environ.get("AWS_ROLE_ARN") == config["role_arn"],
            "Reader requires its installed IRSA identity",
        )

    def aws_identity(self, selected):
        identity = self.source.client("sts").get_caller_identity()
        require(
            identity["Account"] == self.config["account_id"]
            and identity["UserId"].split(":", 1)[0] == self.config["role_id"],
            "Reader role was replaced",
        )
        eks = self.source.client("eks")
        cluster = eks.describe_cluster(name=selected["cluster_name"])["cluster"]
        require(
            cluster["status"] == "ACTIVE"
            and cluster["arn"] == selected["cluster_arn"]
            and cluster["endpoint"] == selected["endpoint"]
            and cluster["certificateAuthority"]["data"]
            == selected["certificate_authority_data"],
            "Reader cluster TLS identity changed",
        )
        entry = eks.describe_access_entry(
            clusterName=selected["cluster_name"], principalArn=self.config["role_arn"]
        )["accessEntry"]
        require(
            entry["accessEntryArn"] == selected["access_entry_arn"]
            and entry["principalArn"] == self.config["role_arn"]
            and entry["type"] == "STANDARD"
            and entry.get("username")
            == "superplane:reader:" + selected["workspace_id"] + ":{{SessionName}}"
            and entry.get("kubernetesGroups")
            == ["superplane:reader:" + selected["workspace_id"]],
            "Reader EKS access entry changed",
        )
        policies = eks.list_associated_access_policies(
            clusterName=selected["cluster_name"], principalArn=self.config["role_arn"]
        )
        require(
            policies.get("associatedAccessPolicies") == []
            and not policies.get("nextToken"),
            "Reader EKS entry has unexpected additive policies",
        )

    def client(self, selected, directory, token=None):
        from botocore.signers import RequestSigner
        from kubernetes import client

        ca = Path(directory) / "ca.pem"
        ca.write_bytes(
            base64.b64decode(selected["certificate_authority_data"], validate=True)
        )
        selected_config = client.Configuration()
        selected_config.host, selected_config.ssl_ca_cert = (
            selected["endpoint"],
            str(ca),
        )
        selected_config.verify_ssl, selected_config.proxy = True, None
        selected_config.api_key_prefix["authorization"] = "Bearer"
        if token is None:
            self.aws_identity(selected)
            sts = self.source.client("sts")
            signer = RequestSigner(
                sts.meta.service_model.service_id,
                self.config["region"],
                "sts",
                "v4",
                self.source.get_credentials(),
                self.source._session.get_component("event_emitter"),
            )
            url = signer.generate_presigned_url(
                {
                    "method": "GET",
                    "url": sts.meta.endpoint_url
                    + "/?Action=GetCallerIdentity&Version=2011-06-15",
                    "body": {},
                    "headers": {"x-k8s-aws-id": selected["cluster_name"]},
                    "context": {},
                },
                region_name=self.config["region"],
                expires_in=60,
                operation_name="",
            )
            token = "k8s-aws-v1." + base64.urlsafe_b64encode(
                url.encode()
            ).decode().rstrip("=")
        selected_config.api_key["authorization"] = token
        return client.ApiClient(selected_config)

    @staticmethod
    def identities(core, target):
        namespace = core.read_namespace(target["namespace"], _request_timeout=8)
        account = core.read_namespaced_service_account(
            target["service_account"], target["namespace"], _request_timeout=8
        )
        require(
            namespace.metadata.uid == target["namespace_uid"]
            and account.metadata.uid == target["service_account_uid"]
            and not namespace.metadata.deletion_timestamp
            and not account.metadata.deletion_timestamp,
            "Reader namespace or ServiceAccount was replaced",
        )

    def issue(self, selected, target, authorize):
        from kubernetes import client

        with (
            tempfile.TemporaryDirectory(prefix="sp-reader-ca-") as directory,
            self.client(selected, directory) as api,
        ):
            core = client.CoreV1Api(api)
            self.identities(core, target)
            authorize()
            result = core.create_namespaced_service_account_token(
                target["service_account"],
                target["namespace"],
                {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenRequest",
                    "spec": {
                        "audiences": [selected["audience"]],
                        "expirationSeconds": 900,
                    },
                },
                _request_timeout=8,
            )
            self.identities(core, target)
            authorize()
            return (
                result.status.token,
                result.status.expiration_timestamp.isoformat(),
            )

    def verify_reader(self, selected, target, token):
        from kubernetes import client

        with (
            tempfile.TemporaryDirectory(prefix="sp-reader-ca-") as directory,
            self.client(selected, directory) as issuer_api,
            self.client(selected, directory, token) as api,
        ):
            inventory = client.CoreV1Api(issuer_api)
            namespaces = self.namespace_inventory(inventory)
            authenticated = client.AuthenticationV1Api(api).create_self_subject_review(
                {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "SelfSubjectReview",
                },
                _request_timeout=8,
            )
            require(
                authenticated.status.user_info.username
                == "system:serviceaccount:"
                + target["namespace"]
                + ":"
                + target["service_account"]
                and authenticated.status.user_info.uid == target["service_account_uid"],
                "Issued reader authenticated as another identity",
            )
            self.verify_permissions(
                client.AuthorizationV1Api(api), api, namespaces, target=target
            )
            require(
                self.namespace_inventory(inventory) == namespaces,
                "Namespace inventory changed during reader proof",
            )
            core = client.CoreV1Api(api)
            require(
                core.read_namespace(
                    target["namespace"], _request_timeout=8
                ).metadata.uid
                == target["namespace_uid"],
                "Issued reader namespace changed",
            )
            core.list_node(
                label_selector="superplane.ai/workspace=" + target["workspace_id"],
                limit=1,
                _request_timeout=8,
            )

    @staticmethod
    def namespace_inventory(core):
        listing = core.list_namespace(limit=257, _request_timeout=8)
        require(
            not listing.metadata._continue and 0 < len(listing.items) <= 256,
            "Reader namespace inventory is incomplete",
        )
        return sorted((item.metadata.name, item.metadata.uid) for item in listing.items)

    @staticmethod
    def verify_permissions(auth, api, namespaces, *, target=None):
        from kubernetes import client

        from .eks_rules import known_eks_rules_gap, native_eks_reader_rules

        user = None
        for namespace, _uid in namespaces:
            response = auth.create_self_subject_rules_review(
                {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SelfSubjectRulesReview",
                    "spec": {"namespace": namespace},
                },
                _request_timeout=8,
            )
            status = api.sanitize_for_serialization(response)["status"]
            allowed = readonly(status)
            if not allowed and target is not None and known_eks_rules_gap(status):
                if user is None:
                    user = (
                        client.AuthenticationV1Api(api)
                        .create_self_subject_review(
                            {
                                "apiVersion": "authentication.k8s.io/v1",
                                "kind": "SelfSubjectReview",
                            },
                            _request_timeout=8,
                        )
                        .status.user_info
                    )
                allowed = native_eks_reader_rules(status, target, user)
            require(
                allowed,
                "Issued reader has mutation or secret authority, or unverifiable rules",
            )
