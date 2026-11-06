#!/usr/bin/env python3
"""Read an operator-registered group token and CA directly into a memory mount."""

import argparse
import json
import http.client
import os
import ssl
import re
import secrets as token_generator
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from cryptography import x509


# GitLab's routable-token generator appends version 01, a two-character
# base36 payload length and a seven-character CRC. The private API below
# still authenticates the opaque token and its exact runner identity.
RUNNER_TOKEN_PATTERN = re.compile(r"glrt-[A-Za-z0-9_-]+(?:\.01\.[0-9a-z]{9})?")


class BootstrapError(Exception):
    """Safe error code; AWS and HTTP response bodies must never be logged."""


def validate_ca(bundle):
    if not isinstance(bundle, str) or "PRIVATE KEY" in bundle or not bundle.strip():
        raise BootstrapError("ca_not_verified")
    try:
        certificates = x509.load_pem_x509_certificates(bundle.encode("ascii"))
        now = datetime.now(timezone.utc)
        if not certificates or any(
            not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or certificate.not_valid_before_utc > now
            or certificate.not_valid_after_utc <= now
            for certificate in certificates
        ):
            raise BootstrapError("ca_not_verified")
        return bundle
    except (ValueError, UnicodeError, x509.ExtensionNotFound):
        raise BootstrapError("ca_not_verified") from None


def validate_token(response, group_id, tenant_id, runner_url, tags, access_level):
    try:
        secret = json.loads(response["SecretString"])
        expires = secret.get("token_expires_at")
        if (
            type(secret.get("schema_version")) is not int or secret["schema_version"] != 1
            or type(secret.get("group_id")) is not int or secret["group_id"] != group_id
            or secret.get("tenant_id") != tenant_id
            or secret.get("runner_url") != runner_url
            or secret.get("tag_list") != tags
            or secret.get("access_level") != access_level
            or type(secret.get("runner_id")) is not int or secret["runner_id"] <= 0
            or type(secret.get("generation")) is not int or secret["generation"] <= 0
            or not isinstance(secret.get("token"), str)
            or not RUNNER_TOKEN_PATTERN.fullmatch(secret["token"])
            or not isinstance(secret.get("operation_id"), str)
            or response["VersionId"] != secret["operation_id"]
            or (expires is not None and (
                not isinstance(expires, str)
                or datetime.fromisoformat(expires.replace("Z", "+00:00")).tzinfo is None
                or datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            ))
        ):
            raise BootstrapError("runner_secret_not_verified")
        return secret
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BootstrapError("runner_secret_not_verified") from None


def load(secrets, group_id, tenant_id, runner_url, tags, access_level, token_arn, ca_arn,
         output_dir, certs_dir, system_id_dir, *, connection_factory=http.client.HTTPSConnection):
    try:
        parsed = urlsplit(runner_url)
        port = parsed.port
    except ValueError:
        raise BootstrapError("runner_url_not_verified") from None
    if (
        parsed.scheme != "https" or not parsed.hostname or port not in (None, 443)
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in ("", "/gitlab")
        or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)
    ):
        raise BootstrapError("runner_url_not_verified")
    try:
        token_response = secrets.get_secret_value(SecretId=token_arn, VersionStage="AWSCURRENT")
        ca_response = secrets.get_secret_value(SecretId=ca_arn, VersionStage="AWSCURRENT")
        secret = validate_token(token_response, group_id, tenant_id, runner_url, tags, access_level)
        token = secret["token"]
        ca = validate_ca(ca_response["SecretString"])
        destination = Path(output_dir)
        certs = Path(certs_dir)
        identity = Path(system_id_dir)
        if (not destination.is_dir() or any(destination.iterdir())
                or not certs.is_dir() or any(certs.iterdir())
                or not identity.is_dir() or any(identity.iterdir())):
            raise BootstrapError("secret_mount_not_empty")
        ca_path = destination / "ca.pem"
        receipt_path = identity / ".adp-verified-runner.json"
        write_once(ca_path, ca)
        system_id_path = identity / ".runner_system_id"
        system_id = "r_" + token_generator.token_hex(6)
        try:
            write_once(system_id_path, system_id + "\n")
            context = ssl.create_default_context(cafile=str(ca_path))
            connection = connection_factory(parsed.hostname, context=context, timeout=10)
            try:
                connection.request("POST", f"{parsed.path}/api/v4/runners/verify",
                                   body=urlencode({"token": token, "system_id": system_id}),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                if response.status != 200:
                    raise BootstrapError("private_runner_not_verified")
                verified = json.loads(response.read(4097))
                if (type(verified) is not dict or type(verified.get("id")) is not int
                        or verified["id"] != secret["runner_id"] or verified.get("token") != token):
                    raise BootstrapError("private_runner_not_verified")
            finally:
                connection.close()
            write_once(certs / f"{parsed.hostname}.crt", ca)
            write_once(destination / "runner-token", token)
            write_once(receipt_path, json.dumps({"runner_id": secret["runner_id"], "system_id": system_id}))
        except (BootstrapError, OSError, ValueError, ssl.SSLError, http.client.HTTPException):
            ca_path.unlink(missing_ok=True)
            system_id_path.unlink(missing_ok=True)
            (certs / f"{parsed.hostname}.crt").unlink(missing_ok=True)
            receipt_path.unlink(missing_ok=True)
            (destination / "runner-token").unlink(missing_ok=True)
            raise
    except BootstrapError:
        raise
    except (OSError, ValueError, KeyError, ssl.SSLError, http.client.HTTPException, BotoCoreError, ClientError):
        raise BootstrapError("runtime_secret_load_failed") from None


def write_once(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(value)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group-id", type=int, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--runner-url", required=True)
    parser.add_argument("--tags", required=True)
    parser.add_argument("--access-level", required=True)
    parser.add_argument("--token-arn", required=True)
    parser.add_argument("--ca-arn", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--certs-dir", required=True)
    parser.add_argument("--system-id-dir", required=True)
    args = parser.parse_args()
    try:
        load(boto3.client("secretsmanager"), args.group_id, args.tenant_id,
             args.runner_url, args.tags.split(","), args.access_level,
             args.token_arn, args.ca_arn, args.output_dir, args.certs_dir, args.system_id_dir)
    except BootstrapError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
