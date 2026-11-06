"""Exercise the real Kubernetes client's auth dispatch and projected renewal."""

from pathlib import Path

from workspace_provisioning.dedicated_readers.transports import Projector


def test_projector_sends_first_token_and_reopens_rotated_projection(monkeypatch):
    tokens = iter(["initial-token", "first-request-token", "rotated-token"])
    reads = []

    def read_token(path):
        assert path == Path("/var/run/secrets/reader-management/token")
        reads.append(str(path))
        return next(tokens)

    monkeypatch.setattr(Path, "read_text", read_token)
    projector = Projector({"projection": {"endpoint": "https://management.example"}})
    try:
        first = {}
        projector.client.update_params_for_auth(first, [], ["BearerToken"])
        assert first == {"authorization": "Bearer first-request-token"}
        renewed = {}
        projector.client.update_params_for_auth(renewed, [], ["BearerToken"])
        assert renewed == {"authorization": "Bearer rotated-token"}
        assert len(reads) == 3
        assert projector.client.configuration.verify_ssl is True
        assert projector.client.configuration.ssl_ca_cert.endswith(
            "/reader-management/ca.crt"
        )
    finally:
        projector.client.close()
