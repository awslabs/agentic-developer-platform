"""Competing initial seeders must publish the same, preserved signing identity."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

spec = importlib.util.spec_from_file_location("portal_keys", Path(__file__).parents[2] / "scripts/provision-jwt-keys.py")
keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keys)


class MissingError(ClientError):
    def __init__(self):
        super().__init__({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")


class ExistsError(ClientError):
    def __init__(self):
        super().__init__({"Error": {"Code": "ResourceExistsException"}}, "PutSecretValue")


@pytest.mark.parametrize("scenario", ["new", "empty", "existing", "racing"])
def test_seed_preserves_current_key_and_converges(monkeypatch, capsys, scenario):
    existing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    existing_pem = existing_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

    class Secrets:
        exceptions = SimpleNamespace(ResourceExistsException=ExistsError, ResourceNotFoundException=MissingError)

        def __init__(self):
            self.value = existing_pem if scenario == "existing" else None
            self.seed_version = None

        def get_secret_value(self, **kwargs):
            if self.value is None:
                raise MissingError()
            return {"SecretString": self.value}

        def create_secret(self, **kwargs):
            if scenario != "new":
                raise ExistsError()
            self.seed_version = kwargs["ClientRequestToken"]
            self.value = kwargs["SecretString"]

        def put_secret_value(self, **kwargs):
            self.seed_version = kwargs["ClientRequestToken"]
            if scenario == "racing":
                # Another process filled the same immutable version after our
                # last read. AWS rejects replacing it with a different value.
                self.value = existing_pem
                raise ExistsError()
            self.value = kwargs["SecretString"]

    secrets = Secrets()
    published = {}
    monkeypatch.setattr(
        keys.boto3,
        "client",
        lambda service, **kwargs: secrets if service == "secretsmanager" else SimpleNamespace(put_parameter=lambda **kw: published.update(kw)),
    )
    keys.provision("test", "us-east-1")
    stored = serialization.load_pem_private_key(secrets.value.encode(), password=None)
    assert serialization.load_pem_public_key(published["Value"].encode()).public_numbers() == stored.public_key().public_numbers()
    if scenario in {"existing", "racing"}:
        assert secrets.value == existing_pem
    if scenario != "existing":
        assert secrets.seed_version == str(keys.uuid.uuid5(keys.uuid.NAMESPACE_URL, "adp/test/gateway-oidc-signing-key/initial-key-v1"))
    assert "PRIVATE KEY" not in capsys.readouterr().out
