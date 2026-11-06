"""Real Kubernetes discovery and botocore signing with synthetic scoped credentials."""

import base64
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import boto3
import botocore.session
from botocore.credentials import Credentials
from kubernetes.client.rest import RESTClientObject
from urllib3.response import HTTPResponse

from workspace_provisioning.cluster_clients import kubernetes_client

OUTPUTS = {
    "cluster_endpoint": "https://workspace.invalid",
    "cluster_certificate_authority_data": base64.b64encode(b"test-ca").decode(),
    "aws_region": "us-west-2",
    "cluster_name": "test-workspace",
}


def process(directory):
    session = botocore.session.get_session()
    session._credentials = Credentials(
        "offline-key", "offline-secret", "offline-session"
    )
    session._credentials._expiry_time = datetime.now(UTC) + timedelta(hours=1)
    return SimpleNamespace(
        directory=Path(directory),
        verify=Mock(),
        checked=Mock(side_effect=AssertionError("AWS CLI must not run")),
        session=boto3.Session(botocore_session=session),
    )


class KubernetesDiscoveryAuthenticationTests(unittest.TestCase):
    def test_first_discovery_request_is_authenticated_and_reuses_scoped_token(self):
        observed = []

        def get(rest, url, headers=None, **kwargs):
            token = headers.get("authorization", "")
            self.assertTrue(token.startswith("Bearer k8s-aws-v1."))
            encoded = token.split("k8s-aws-v1.")[1]
            signed = urlparse(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            )
            query = parse_qs(signed.query)
            self.assertEqual(signed.hostname, "sts.us-west-2.amazonaws.com")
            self.assertEqual(query["Action"], ["GetCallerIdentity"])
            self.assertIn("x-k8s-aws-id", query["X-Amz-SignedHeaders"][0])
            self.assertIn("/us-west-2/sts/", query["X-Amz-Credential"][0])
            self.assertEqual(query["X-Amz-Expires"], ["60"])
            observed.append((url, token))
            if url.endswith("/version"):
                body = {"major": "1", "minor": "35", "gitVersion": "v1.35.0"}
            elif url.endswith("/apis"):
                body = {"kind": "APIGroupList", "groups": []}
            elif url.endswith("/api/v1"):
                body = {
                    "kind": "APIResourceList",
                    "groupVersion": "v1",
                    "resources": [
                        {
                            "name": "namespaces",
                            "kind": "Namespace",
                            "namespaced": False,
                            "verbs": ["get", "list"],
                        }
                    ],
                }
            elif url.endswith("/api"):
                body = {"kind": "APIVersions", "versions": ["v1"]}
            else:
                self.fail("unexpected discovery request: " + url)
            return HTTPResponse(body=json.dumps(body).encode(), status=200)

        with TemporaryDirectory() as directory:
            runner = process(directory)
            dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")
            try:
                with patch.object(RESTClientObject, "GET", get):
                    namespace = dynamic.resources.get(
                        api_version="v1", kind="Namespace"
                    )
                    self.assertEqual(namespace.kind, "Namespace")
                self.assertTrue(observed[0][0].endswith("/version"))
                self.assertGreater(len(observed), 1)
                self.assertEqual(len({header for _, header in observed}), 1)
                self.assertGreaterEqual(runner.verify.call_count, len(observed))
                runner.checked.assert_not_called()
                runner.verify.side_effect = RuntimeError("authority revoked")
                with self.assertRaisesRegex(RuntimeError, "authority revoked"):
                    dynamic.client.configuration.get_api_key_with_prefix(
                        "authorization"
                    )
            finally:
                dynamic.client.close()

    def test_missing_credentials_stop_before_discovery(self):
        with TemporaryDirectory() as directory:
            runner = process(directory)
            runner.session.get_credentials = Mock(
                side_effect=RuntimeError("token unavailable")
            )
            dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")
            try:
                with patch.object(RESTClientObject, "GET") as get:
                    with self.assertRaisesRegex(RuntimeError, "token unavailable"):
                        dynamic.resources
                    get.assert_not_called()
            finally:
                dynamic.client.close()

    def test_token_refreshes_before_session_credentials_expire(self):
        with TemporaryDirectory() as directory:
            runner = process(directory)
            now = datetime.now(UTC)
            runner.session.get_credentials()._expiry_time = now + timedelta(minutes=3)
            dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")
            try:
                config = dynamic.client.configuration
                with patch.object(
                    runner.session, "client", wraps=runner.session.client
                ) as clients:
                    with patch(
                        "workspace_provisioning.cluster_clients.datetime"
                    ) as clock:
                        clock.now.return_value = now
                        config.refresh_api_key_hook(config)
                        config.refresh_api_key_hook(config)
                        self.assertEqual(clients.call_count, 1)
                        clock.now.return_value = now + timedelta(minutes=2, seconds=1)
                        config.refresh_api_key_hook(config)
                        self.assertEqual(clients.call_count, 2)
            finally:
                dynamic.client.close()


class KubernetesPermissionReviewTests(unittest.TestCase):
    def target(self):
        from superplane_bootstrap.target import VerifiedTarget

        return VerifiedTarget(
            org_id="synthetic-org",
            workspace_id="synthetic-workspace",
            account_id="000000000000",
            region=OUTPUTS["aws_region"],
            cluster_name=OUTPUTS["cluster_name"],
            cluster_arn="arn:aws:eks:us-west-2:000000000000:cluster/test-workspace",
            endpoint=OUTPUTS["cluster_endpoint"],
            certificate_authority_data=OUTPUTS["cluster_certificate_authority_data"],
            principal_arn="arn:aws:iam::000000000000:role/installer",
            cluster_ownership="managed",
        )

    def test_reviews_use_authenticated_bounded_sdk_without_discovery_or_cli(self):
        body = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "verb": "create",
                    "resource": "pods",
                    "subresource": "exec",
                    "namespace": "synthetic",
                }
            },
        }
        observed = []

        def post(rest, url, headers=None, **kwargs):
            self.assertEqual(
                url,
                OUTPUTS["cluster_endpoint"]
                + "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
            )
            self.assertTrue(headers["authorization"].startswith("Bearer k8s-aws-v1."))
            self.assertEqual(kwargs["body"], body)
            self.assertEqual(kwargs["_request_timeout"], (5, 30))
            observed.append((headers["authorization"], runner.verify.call_count))
            return HTTPResponse(
                body=b'{"status":{"allowed":false}}',
                status=201,
                headers={"content-type": "application/json"},
            )

        with TemporaryDirectory() as directory:
            runner = process(directory)
            dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")
            try:
                with (
                    patch.object(
                        RESTClientObject, "GET", side_effect=AssertionError("discovery")
                    ),
                    patch.object(RESTClientObject, "POST", post),
                ):
                    for _ in range(2):
                        result = dynamic.self_subject_access_review(
                            body, target=self.target()
                        )
                        self.assertIs(result["status"]["allowed"], False)
                        self.assertGreater(runner.verify.call_count, observed[-1][1])
                self.assertEqual(observed[0][0], observed[1][0])
                self.assertGreater(observed[1][1], observed[0][1])
                self.assertIsNone(dynamic._dynamic)
                runner.checked.assert_not_called()
            finally:
                dynamic.client.close()

    def test_revocation_during_request_refuses_answer(self):
        with TemporaryDirectory() as directory:
            runner = process(directory)
            dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")

            def post(*args, **kwargs):
                runner.verify.side_effect = RuntimeError("authority revoked")
                return HTTPResponse(body=b'{"status":{"allowed":true}}', status=201)

            try:
                with patch.object(RESTClientObject, "POST", post):
                    with self.assertRaisesRegex(RuntimeError, "authority revoked"):
                        dynamic.self_subject_access_review({}, target=self.target())
            finally:
                dynamic.client.close()

    def test_changed_transport_or_revoked_authority_prevents_request(self):
        from superplane_bootstrap.errors import BootstrapRefused

        for changed in ("host", "verify_ssl", "ca", "authority"):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                runner = process(directory)
                dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")
                config = dynamic.client.configuration
                if changed == "host":
                    config.host = "https://other.invalid"
                elif changed == "verify_ssl":
                    config.verify_ssl = False
                elif changed == "ca":
                    Path(config.ssl_ca_cert).write_bytes(b"other-ca")
                else:
                    runner.verify.side_effect = RuntimeError("authority revoked")
                try:
                    with patch.object(RESTClientObject, "POST") as post:
                        with self.assertRaises((BootstrapRefused, RuntimeError)):
                            dynamic.self_subject_access_review({}, target=self.target())
                        post.assert_not_called()
                finally:
                    dynamic.client.close()

    def test_concurrent_discovery_initializes_once_and_serializes_cache_lookups(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Lock
        from time import sleep

        start, lock = Barrier(4), Lock()
        active = peak = 0

        def lookup(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            sleep(0.01)
            with lock:
                active -= 1
            return kwargs["kind"]

        discovered = SimpleNamespace(resources=SimpleNamespace(get=lookup))
        with (
            TemporaryDirectory() as directory,
            patch(
                "kubernetes.dynamic.DynamicClient", return_value=discovered
            ) as factory,
        ):
            runner = process(directory)
            dynamic = kubernetes_client(runner, OUTPUTS, name="workspace")

            def read(index):
                start.wait(timeout=5)
                return dynamic.resources.get(api_version="v1", kind="Kind" + str(index))

            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    self.assertEqual(
                        list(executor.map(read, range(4))),
                        ["Kind" + str(i) for i in range(4)],
                    )
                factory.assert_called_once()
                self.assertEqual(
                    factory.call_args.kwargs["cache_file"],
                    str(Path(directory, "workspace.discovery.json")),
                )
                self.assertEqual(peak, 1)
                self.assertEqual(active, 0)
            finally:
                dynamic.client.close()
