#!/usr/bin/env python3
"""Create/reuse the GitLab SSO signing key and publish only its public key."""

import argparse
import uuid

import boto3
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def provision(environment, region):
    secrets = boto3.client("secretsmanager", region_name=region)
    name = f"adp/{environment}/gateway-oidc-signing-key"
    # Secrets Manager versions are immutable. One stable initial version makes
    # competing seeders converge without either replacing the winning key.
    seed_version = str(uuid.uuid5(uuid.NAMESPACE_URL, name + "/initial-key-v1"))
    try:
        value = secrets.get_secret_value(SecretId=name)["SecretString"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        value = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        try:
            secrets.create_secret(Name=name, SecretString=value, ClientRequestToken=seed_version)
        except secrets.exceptions.ResourceExistsException:
            # Terraform can create the secret container first. Never overwrite a
            # concurrently seeded CURRENT version with a newly generated key.
            try:
                value = secrets.get_secret_value(SecretId=name)["SecretString"]
            except secrets.exceptions.ResourceNotFoundException:
                try:
                    secrets.put_secret_value(SecretId=name, SecretString=value, ClientRequestToken=seed_version)
                except secrets.exceptions.ResourceExistsException:
                    pass  # Another seeder won the immutable initial version.
                value = secrets.get_secret_value(SecretId=name)["SecretString"]
    key = serialization.load_pem_private_key(value.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("The existing signing key must be RSA with at least 2048 bits")
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    boto3.client("ssm", region_name=region).put_parameter(
        Name=f"/adp/{environment}/gitlab/jwt-public-key", Type="String", Value=public, Overwrite=True
    )
    print("GitLab public key published; existing signing identity preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["dev", "test", "prod"])
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    provision(args.environment, args.region)
