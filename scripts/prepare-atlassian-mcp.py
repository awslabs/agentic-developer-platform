#!/usr/bin/env python3
"""Prepare a protected provider plan; Terraform and Identity retain their roles.

Run after Terraform creates the two secret containers. The default is read-only
apart from private output files. --publish fills an empty plan secret, or accepts
an identical plan. It never changes a registered client, consent, or readiness.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

RESOURCE = "https://mcp.atlassian.com/v2/mcp"
METADATA_URL = "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp"
REQUIRED_SCOPES = {
    "read:me",
    "read:account",
    "offline_access",
    "read:jira:agent-interface",
    "search:jira:agent-interface",
    "read:confluence:agent-interface",
    "search:confluence:agent-interface",
    "write:jira:agent-interface",
}
MAX_METADATA_BYTES = 128 * 1024


class PreparationError(ValueError):
    """A bounded, nonsecret operator diagnostic."""


def https_url(value, *, host=None, root=False):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise PreparationError("An HTTPS URL is required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (host is not None and parsed.netloc != host)
        or (root and parsed.path not in ("", "/"))
    ):
        raise PreparationError(
            "The provider or gateway URL does not match its required HTTPS origin"
        )
    if not root and (not parsed.path.strip("/") or ".." in parsed.path.split("/")):
        raise PreparationError("The provider URL requires an exact resource path")
    return parsed


def fetch_metadata(url):
    parsed = https_url(url)
    if parsed.netloc not in {"mcp.atlassian.com", "auth.atlassian.com"}:
        raise PreparationError(
            "Metadata must come from the advertised Atlassian origins"
        )
    deadline = time.monotonic() + 20
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=5)
    try:
        connection.request("GET", parsed.path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise PreparationError(
                "Atlassian metadata retrieval failed; redirects are not followed"
            )
        chunks = bytearray()
        while True:
            chunk = response.read(min(16384, MAX_METADATA_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_METADATA_BYTES or time.monotonic() > deadline:
                raise PreparationError(
                    "Atlassian metadata exceeded its size or time bound"
                )
        value = json.loads(chunks)
        if not isinstance(value, dict):
            raise PreparationError("Atlassian metadata must be a JSON object")
        return value
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        raise PreparationError("Atlassian metadata could not be read") from None
    finally:
        connection.close()


def authorization_metadata_url(protected):
    if not isinstance(protected, dict):
        raise PreparationError("The protected resource metadata must be a JSON object")
    issuers = protected.get("authorization_servers")
    if (
        protected.get("resource") != RESOURCE
        or not isinstance(issuers, list)
        or len(issuers) != 1
    ):
        raise PreparationError(
            "The protected resource must advertise exactly one v2 issuer"
        )
    issuer = https_url(issuers[0], host="auth.atlassian.com")
    return f"https://auth.atlassian.com/.well-known/oauth-authorization-server{issuer.path}"


def string_set(document, field):
    value = document.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PreparationError(
            "Provider metadata is missing a supported OAuth capability list"
        )
    return set(value)


def build_plan(
    protected,
    authorization,
    *,
    gateway_url,
    client_secret_arn,
    revision,
    account,
    region,
    environment,
):
    authorization_metadata_url(protected)
    if (
        not isinstance(authorization, dict)
        or authorization.get("issuer") != protected["authorization_servers"][0]
    ):
        raise PreparationError(
            "The authorization server does not match the protected resource issuer"
        )
    for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        https_url(authorization.get(field), host="auth.atlassian.com")
    if (
        not REQUIRED_SCOPES <= string_set(protected, "scopes_supported")
        or "S256" not in string_set(authorization, "code_challenge_methods_supported")
        or not {"authorization_code", "refresh_token"}
        <= string_set(authorization, "grant_types_supported")
        or "client_secret_basic"
        not in string_set(authorization, "token_endpoint_auth_methods_supported")
    ):
        raise PreparationError(
            "The live metadata does not support the required MCP OAuth contract"
        )
    gateway = https_url(gateway_url, root=True)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise PreparationError("Use the exact reviewed source revision for this plan")
    prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/connections/atlassian-mcp/client-"
    if not client_secret_arn.startswith(prefix) or not re.fullmatch(
        r"[A-Za-z0-9]{6}", client_secret_arn[len(prefix) :]
    ):
        raise PreparationError(
            "The client secret must be the existing target-owned Terraform resource"
        )
    metadata_sha256 = hashlib.sha256(
        json.dumps([protected, authorization], sort_keys=True).encode()
    ).hexdigest()
    return {
        "resource": RESOURCE,
        "issuer": authorization["issuer"],
        "authorization_endpoint": authorization["authorization_endpoint"],
        "token_endpoint": authorization["token_endpoint"],
        "client_id": "",
        "app_secret_ref": client_secret_arn,
        "registration_mode": "dynamic-client-registration",
        "return_url": f"https://{gateway.netloc}/api/auth/connections/atlassian-mcp/return",
        "contract_revision": revision,
        "protected_resource_metadata": protected,
        "authorization_server_metadata": authorization,
        "metadata_sha256": metadata_sha256,
    }


def existing_secret(store, *, account, region, environment, kind):
    name = f"adp/{environment}/connections/atlassian-mcp/{kind}"
    result = store.describe_secret(SecretId=name)
    prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:{name}-"
    arn = result.get("ARN", "")
    if (
        result.get("Name") != name
        or result.get("DeletedDate") is not None
        or not isinstance(arn, str)
        or not arn.startswith(prefix)
        or not re.fullmatch(r"[A-Za-z0-9]{6}", arn[len(prefix) :])
    ):
        raise PreparationError(
            "Terraform secret containers are missing or belong to a different target"
        )
    return arn


def current_plan(store, arn):
    try:
        result = store.get_secret_value(SecretId=arn)
    except Exception as error:
        if (
            getattr(error, "response", {}).get("Error", {}).get("Code")
            == "ResourceNotFoundException"
        ):
            return None
        raise
    try:
        value = json.loads(result["SecretString"])
    except (KeyError, TypeError, ValueError):
        raise PreparationError(
            "The existing provider plan is unreadable; it will not be replaced"
        ) from None
    if not isinstance(value, dict):
        raise PreparationError(
            "The existing provider plan is invalid; it will not be replaced"
        )
    return value


def publish_plan(store, arn, plan):
    # The protected release workflow serializes deployment writers. Never reset a
    # retained plan when metadata/source changed; rotation needs its own review.
    current = current_plan(store, arn)
    if current is not None:
        if current != plan:
            raise PreparationError(
                "A different retained plan exists; no configuration or client state was changed"
            )
        return "unchanged"
    encoded = json.dumps(plan, sort_keys=True)
    store.put_secret_value(
        SecretId=arn,
        SecretString=encoded,
        ClientRequestToken=hashlib.sha256(encoded.encode()).hexdigest(),
    )
    if current_plan(store, arn) != plan:
        raise PreparationError("Provider plan publication could not be verified")
    return "published"


def write_private(directory, name, value):
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path = directory / name
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        if (
            path.is_symlink()
            or path.stat().st_mode & 0o077
            or path.read_bytes() != content
        ):
            raise PreparationError(
                "Use a private output directory without conflicting files"
            ) from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(args, *, sts, store, fetch=fetch_metadata):
    if (
        not re.fullmatch(r"\d{12}", args.account_id)
        or not re.fullmatch(r"[a-z]{2}-[a-z0-9-]+-\d", args.region)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.environment)
    ):
        raise PreparationError(
            "A specific account, region and environment are required"
        )
    if sts.get_caller_identity().get("Account") != args.account_id:
        raise PreparationError(
            "The active AWS identity does not match the selected account"
        )
    directory = Path(args.output_dir).absolute()
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    if (
        directory.is_symlink()
        or directory.resolve() != directory
        or directory.stat().st_mode & 0o077
    ):
        raise PreparationError(
            "The output directory must be private and must not traverse symlinks"
        )
    target = {
        "account": args.account_id,
        "region": args.region,
        "environment": args.environment,
    }
    config_arn = existing_secret(store, kind="config", **target)
    client_arn = existing_secret(store, kind="client", **target)
    retained = current_plan(store, config_arn)
    if getattr(args, "use_retained", False):
        if args.publish:
            raise PreparationError("Retained plan readback cannot publish a provider plan")
        if retained is None:
            return {"status": "unconfigured", "output_dir": str(directory)}
        protected = retained.get("protected_resource_metadata")
        authorization = retained.get("authorization_server_metadata")
        revision = retained.get("contract_revision")
    else:
        protected = fetch(METADATA_URL)
        authorization = fetch(authorization_metadata_url(protected))
        revision = args.contract_revision
    plan = build_plan(
        protected,
        authorization,
        gateway_url=args.gateway_url,
        client_secret_arn=client_arn,
        revision=revision,
        **target,
    )
    if retained is not None and retained != plan:
        raise PreparationError(
            "A different retained plan exists; review rotation before preparing another plan"
        )
    pins = {
        "atlassian_mcp_contract_revision": plan["contract_revision"],
        "atlassian_mcp_metadata_sha256": plan["metadata_sha256"],
    }
    write_private(directory, "provider-plan.json", plan)
    write_private(directory, "atlassian-mcp.tfvars.json", pins)
    status = publish_plan(store, config_arn, plan) if args.publish else "prepared"
    return {"status": status, "output_dir": str(directory), **pins}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--contract-revision", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--use-retained",
        action="store_true",
        help="Rebuild private Terraform pins from the existing plan without fetching provider metadata",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Fill only an empty or identical Terraform-owned plan secret",
    )
    args = parser.parse_args()
    try:
        import boto3

        session = boto3.Session(region_name=args.region)
        result = prepare(
            args, sts=session.client("sts"), store=session.client("secretsmanager")
        )
    except PreparationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print(
            "Provider plan preparation failed; verify metadata, Terraform containers and deployment-role access",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
