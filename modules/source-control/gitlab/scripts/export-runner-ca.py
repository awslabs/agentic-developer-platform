#!/usr/bin/env python3
"""Validate the instance ACM chain against an operator-approved root before export."""

import argparse
import hashlib
import re
import ssl
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization


class CaExportError(Exception):
    """A safe error code; never expose provider responses or private names."""


def validate_chain(certificate, chain, approved_roots, fingerprint, hostname):
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise CaExportError("approved_root_fingerprint_required")
    try:
        root_bytes = Path(approved_roots).read_bytes()
        if hashlib.sha256(root_bytes).hexdigest() != fingerprint:
            raise CaExportError("approved_root_mismatch")
        roots = x509.load_pem_x509_certificates(root_bytes)
        leaf = x509.load_pem_x509_certificate(certificate.encode())
        intermediates = x509.load_pem_x509_certificates(chain.encode()) if chain else []
        if not roots or not isinstance(hostname, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
            raise CaExportError("certificate_not_verified")
        if not intermediates and all(leaf.fingerprint(hashes.SHA256()) != root.fingerprint(hashes.SHA256()) for root in roots):
            raise CaExportError("certificate_chain_missing")
        for intermediate in intermediates:
            if not intermediate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise CaExportError("certificate_not_verified")
        with tempfile.TemporaryDirectory(prefix="runner-ca-") as temporary:
            leaf_path = Path(temporary) / "leaf.pem"
            chain_path = Path(temporary) / "chain.pem"
            root_path = Path(temporary) / "approved.pem"
            leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
            chain_path.write_bytes(b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in intermediates))
            root_path.write_bytes(b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in roots))
            command = ["openssl", "verify", "-trusted", str(root_path), "-purpose", "sslserver",
                       "-verify_hostname", hostname]
            if intermediates:
                command.extend(["-untrusted", str(chain_path)])
            command.append(str(leaf_path))
            result = subprocess.run(command, capture_output=True, timeout=15, check=False)
            if result.returncode:
                raise CaExportError("certificate_not_verified")
            for index in range(len(intermediates)):
                intermediate_path = Path(temporary) / f"intermediate-{index}.pem"
                intermediate_path.write_bytes(intermediates[index].public_bytes(serialization.Encoding.PEM))
                result = subprocess.run(["openssl", "verify", "-trusted", str(root_path),
                                         "-untrusted", str(chain_path), str(intermediate_path)],
                                        capture_output=True, timeout=15, check=False)
                if result.returncode:
                    raise CaExportError("certificate_not_verified")
        trust = []
        seen = set()
        for cert in (*intermediates, *roots):
            digest = cert.fingerprint(hashes.SHA256())
            if digest not in seen:
                trust.append(cert.public_bytes(serialization.Encoding.PEM))
                seen.add(digest)
        return b"".join(trust).decode()
    except CaExportError:
        raise
    except (OSError, ValueError, TypeError, x509.ExtensionNotFound, subprocess.TimeoutExpired):
        raise CaExportError("certificate_not_verified") from None


def export_ca(acm, secrets, environment, certificate_arn, runner_url, approved_root_file, fingerprint, *, transport=None):
    if not isinstance(environment, str) or not re.fullmatch(r"[a-z0-9-]+", environment):
        raise CaExportError("invalid_environment")
    if not isinstance(certificate_arn, str) or not re.fullmatch(r"arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]{36}", certificate_arn):
        raise CaExportError("invalid_certificate_reference")
    if not isinstance(runner_url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*/?", runner_url):
        raise CaExportError("invalid_runner_url")
    try:
        parsed = urlsplit(runner_url)
        if parsed.port == 0 or any(segment in {".", ".."} for segment in parsed.path.split("/")):
            raise CaExportError("invalid_runner_url")
        document = acm.get_certificate(CertificateArn=certificate_arn)
        bundle = validate_chain(document["Certificate"], document.get("CertificateChain"),
                                approved_root_file, fingerprint, parsed.hostname)
        context = ssl.create_default_context(cadata=bundle)
        with httpx.Client(verify=context, follow_redirects=False, trust_env=False,
                          timeout=5, transport=transport) as client:
            response = client.get(runner_url.rstrip("/") + "/-/liveness")
        if response.status_code != 200:
            raise CaExportError("private_route_not_verified")
        try:
            details = response.json()
        except ValueError:
            raise CaExportError("private_route_not_verified") from None
        if not isinstance(details, dict) or details.get("status") != "ok":
            raise CaExportError("private_route_not_verified")
        result = secrets.put_secret_value(SecretId=f"adp/{environment}/gitlab/ca-chain", SecretString=bundle)
        if not isinstance(result.get("VersionId"), str):
            raise CaExportError("ca_storage_not_verified")
        saved = secrets.get_secret_value(SecretId=f"adp/{environment}/gitlab/ca-chain", VersionId=result["VersionId"])
        if saved.get("SecretString") != bundle:
            raise CaExportError("ca_storage_not_verified")
        return {"status": "exported", "version_id": result["VersionId"]}
    except CaExportError:
        raise
    except Exception:
        raise CaExportError("ca_provider_unavailable") from None


def main(argv=None, *, sdk=None, transport=None):
    import json
    import logging
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--certificate-arn", required=True)
    parser.add_argument("--runner-url", required=True)
    parser.add_argument("--approved-root-file", required=True)
    parser.add_argument("--approved-root-sha256", required=True)
    args = parser.parse_args(argv)
    previous = logging.root.manager.disable
    try:
        logging.disable(max(previous, logging.CRITICAL))
        if sdk is None:
            import boto3
            sdk = boto3.session.Session(region_name=args.region)
        result = export_ca(sdk.client("acm"), sdk.client("secretsmanager"), args.environment,
                           args.certificate_arn, args.runner_url, args.approved_root_file,
                           args.approved_root_sha256, transport=transport)
    except CaExportError as error:
        print(str(error), file=sys.stderr)
        return 3 if str(error) == "ca_provider_unavailable" else 2
    except Exception:
        print("ca_provider_unavailable", file=sys.stderr)
        return 3
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
