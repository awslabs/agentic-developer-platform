"""Explicit owner-only CONNECT route with unchanged target TLS verification.

Ordinary bootstrap clients continue to forbid proxies. Native owner recovery may
bind an already-created loopback CONNECT pool to one target and short deadline;
this object cannot create a route, issue credentials or select grant operations.
"""

from urllib.parse import urlsplit
import time

from .errors import BootstrapRefused


def _require(value):
    if not value:
        raise BootstrapRefused("owner Kubernetes TLS route changed or expired")


class PinnedOwnerTlsProxy:
    def __init__(self, client, target, *, proxy_url, deadline):
        import urllib3

        from .target import VerifiedTarget

        selected = urlsplit(proxy_url)
        _require(
            isinstance(target, VerifiedTarget)
            and selected.scheme == "http"
            and selected.hostname == "127.0.0.1"
            and selected.port is not None
            and 1 <= selected.port <= 65535
            and not selected.username
            and not selected.password
            and not selected.path
            and not selected.query
            and not selected.fragment
            and type(deadline) in {int, float}
            and 0 < deadline - time.monotonic() <= 2700
        )
        self._client, self._target = client, target
        self._configuration = client.configuration
        self._rest = client.rest_client
        self._manager = self._rest.pool_manager
        _require(type(self._manager) is urllib3.ProxyManager)
        self._proxy = urllib3.util.parse_url(proxy_url)
        self._proxy_url, self._deadline = proxy_url, deadline
        self._ca_path = self._configuration.ssl_ca_cert
        self._pool = self._manager.connection_from_url(target.endpoint)
        self.verify(client, target)

    def verify(self, client, target):
        """Check the live pool before every native Kubernetes resource access."""
        import urllib3

        manager, config = self._manager, self._configuration
        expected_proxy_config = manager.proxy_config
        retry = manager.connection_pool_kw.get("retries")
        _require(
            time.monotonic() < self._deadline
            and client is self._client
            and target == self._target
            and client.configuration is config
            and client.rest_client is self._rest
            and self._rest.pool_manager is manager
            and type(manager) is urllib3.ProxyManager
            and config.host == target.endpoint
            and config.verify_ssl is True
            and config.assert_hostname is None
            and config.tls_server_name is None
            and config.proxy == self._proxy_url
            and config.ssl_ca_cert == self._ca_path
            and manager.proxy == self._proxy
            and manager.proxy_headers == {}
            and tuple(expected_proxy_config) == (None, False, None, None)
            and type(retry) is urllib3.util.Retry
            and retry.total is False
            and retry.redirect == 0
            and retry.connect is None
            and retry.read is None
            and retry.status is None
            and retry.other is None
            and manager.connection_pool_kw
            == {
                "cert_reqs": "CERT_REQUIRED",
                "ca_certs": self._ca_path,
                "retries": retry,
                "maxsize": 4,
                "_proxy": self._proxy,
                "_proxy_headers": {},
                "_proxy_config": expected_proxy_config,
            }
        )
        pool = manager.connection_from_url(target.endpoint)
        destination = urlsplit(target.endpoint)
        _require(
            pool is self._pool
            and type(pool) is urllib3.HTTPSConnectionPool
            and pool.ConnectionCls is urllib3.connection.HTTPSConnection
            and pool.host == destination.hostname
            and pool.port == (destination.port or 443)
            and pool.scheme == destination.scheme == "https"
            and pool.cert_reqs == "CERT_REQUIRED"
            and pool.ca_certs == self._ca_path
            and pool.ca_cert_dir is None
            and getattr(pool, "ca_cert_data", None) is None
            and pool.conn_kw.get("ca_cert_data") is None
            and pool.cert_file is None
            and pool.key_file is None
            and pool.retries is retry
            and pool.assert_hostname is None
            and pool.assert_fingerprint is None
            and pool.proxy == self._proxy
            and pool.proxy_config == expected_proxy_config
            and not pool.conn_kw.get("ssl_context")
            and not pool.conn_kw.get("server_hostname")
        )
        return True
