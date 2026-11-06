"""An explicit owner route never relaxes ordinary bootstrap TLS checks."""

import base64
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest
import urllib3

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.owner_tls_transport import PinnedOwnerTlsProxy
from superplane_bootstrap.target import VerifiedTarget


@pytest.fixture
def setup(tmp_path):
    kubernetes = pytest.importorskip("kubernetes.client")
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"exact verified target CA")
    target = VerifiedTarget(
        org_id="org",
        workspace_id="workspace",
        account_id="123456789012",
        region="us-east-1",
        cluster_name="selected",
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/selected",
        endpoint="https://selected.eks.amazonaws.com",
        certificate_authority_data=base64.b64encode(ca.read_bytes()).decode(),
        principal_arn="arn:aws:iam::123456789012:role/owner",
        cluster_ownership="adp-created",
    )
    config = kubernetes.Configuration()
    config.host, config.ssl_ca_cert = target.endpoint, str(ca)
    config.verify_ssl = True
    config.proxy = "http://127.0.0.1:43210"
    manager = urllib3.ProxyManager(
        config.proxy,
        cert_reqs="CERT_REQUIRED",
        ca_certs=str(ca),
        retries=False,
        maxsize=4,
    )
    api = SimpleNamespace(
        configuration=config, rest_client=SimpleNamespace(pool_manager=manager)
    )
    reads = []
    body = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "selected", "uid": "uid"},
    }

    def get(**kwargs):
        reads.append(kwargs)
        return body

    dynamic = SimpleNamespace(
        client=api,
        resources=SimpleNamespace(get=lambda **kwargs: SimpleNamespace(get=get)),
    )
    spec = {"cluster_arn": target.cluster_arn, "body": body}
    route = PinnedOwnerTlsProxy(
        api, target, proxy_url=config.proxy, deadline=time.monotonic() + 60
    )
    try:
        yield SimpleNamespace(
            api=api,
            dynamic=dynamic,
            target=target,
            route=route,
            config=config,
            manager=manager,
            reads=reads,
            spec=spec,
            ca=ca,
        )
    finally:
        manager.clear()


def test_default_clients_still_refuse_proxy_and_arbitrary_verifier(setup):
    s = setup
    with pytest.raises(BootstrapRefused):
        KubeGrants(s.dynamic, s.target)
    with pytest.raises(BootstrapRefused):
        KubeGrants(
            s.dynamic,
            s.target,
            owner_tls_proxy=SimpleNamespace(verify=lambda *args: True),
        )
    assert not s.reads
    backend = KubeGrants(s.dynamic, s.target, owner_tls_proxy=s.route)
    assert backend.observe(s.spec)["uid"] == "uid"
    assert len(s.reads) == 1 and s.config.proxy == "http://127.0.0.1:43210"


@pytest.mark.parametrize(
    "fault",
    [
        "endpoint",
        "target",
        "client",
        "proxy",
        "pool",
        "ca-bytes",
        "ca-path",
        "verification",
        "hostname",
        "sni",
        "manager-ca",
        "manager-cert",
        "manager-proxy",
        "pool-cert",
        "pool-hostname",
        "pool-ca",
        "pool-ca-dir",
        "pool-ca-data",
        "pool-connection",
        "deadline",
    ],
)
def test_live_route_checked_again_before_every_resource_read(setup, monkeypatch, fault):
    s = setup
    backend = KubeGrants(s.dynamic, s.target, owner_tls_proxy=s.route)
    backend.observe(s.spec)
    assert len(s.reads) == 1
    if fault == "endpoint":
        s.config.host = "https://other.eks.amazonaws.com"
    elif fault == "target":
        backend.target = replace(s.target, cluster_name="other")
    elif fault == "client":
        s.dynamic.client = SimpleNamespace(
            configuration=s.config, rest_client=s.api.rest_client
        )
    elif fault == "proxy":
        s.config.proxy = "http://127.0.0.1:43211"
    elif fault == "pool":
        s.api.rest_client.pool_manager = urllib3.PoolManager()
    elif fault == "ca-bytes":
        s.ca.write_bytes(b"changed")
    elif fault == "ca-path":
        s.config.ssl_ca_cert = str(s.ca) + ".other"
    elif fault == "verification":
        s.config.verify_ssl = False
    elif fault == "hostname":
        s.config.assert_hostname = False
    elif fault == "sni":
        s.config.tls_server_name = "other"
    elif fault == "manager-ca":
        s.manager.connection_pool_kw["ca_certs"] = "/other"
    elif fault == "manager-cert":
        s.manager.connection_pool_kw["cert_reqs"] = "CERT_NONE"
    elif fault == "manager-proxy":
        s.manager.proxy = urllib3.util.parse_url("http://127.0.0.1:43211")
    elif fault == "pool-cert":
        s.route._pool.cert_reqs = "CERT_NONE"
    elif fault == "pool-hostname":
        s.route._pool.assert_hostname = False
    elif fault == "pool-ca":
        s.route._pool.ca_certs = "/other"
    elif fault == "pool-ca-dir":
        s.route._pool.ca_cert_dir = "/other"
    elif fault == "pool-ca-data":
        s.route._pool.conn_kw["ca_cert_data"] = "other"
    elif fault == "pool-connection":
        s.route._pool.ConnectionCls = object
    elif fault == "deadline":
        monkeypatch.setattr(
            "superplane_bootstrap.owner_tls_transport.time.monotonic",
            lambda: s.route._deadline + 1,
        )
    with pytest.raises(BootstrapRefused):
        backend.observe(s.spec)
    assert len(s.reads) == 1


@pytest.mark.parametrize(
    "proxy,seconds",
    [
        ("http://127.0.0.1:43210", -1),
        ("http://127.0.0.1:43210", 3000),
        ("http://other:43210", 60),
        ("https://127.0.0.1:43210", 60),
        ("http://user@127.0.0.1:43210", 60),
        ("http://127.0.0.1:43210/other", 60),
    ],
)
def test_route_creation_refuses_unbounded_or_nonlocal_route(setup, proxy, seconds):
    with pytest.raises(BootstrapRefused):
        PinnedOwnerTlsProxy(
            setup.api,
            setup.target,
            proxy_url=proxy,
            deadline=time.monotonic() + seconds,
        )
