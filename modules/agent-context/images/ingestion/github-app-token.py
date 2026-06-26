#!/usr/bin/env python3
"""
Generate a GitHub App installation access token from AWS Secrets Manager.

Supports two secret formats:
  1. Split secrets (default): separate secrets for app ID and private key
     --app-id-secret adp/gh-app-ops-id --app-key-secret adp/gh-app-ops-key
  2. Combined secret: single JSON secret with app_id, installation_id, private_key
     --secret-id adp/github-app

The installation_id is discovered automatically from the GitHub API
if not provided (matches the behavior of actions/create-github-app-token).

Usage:
  # Split secrets (matches existing workflow pattern):
  python3 github-app-token.py \
    --app-id-secret adp/gh-app-ops-id \
    --app-key-secret adp/gh-app-ops-key \
    --region us-east-1 \
    --output-file /shared/github-token

  # Update K8s secret directly:
  python3 github-app-token.py \
    --app-id-secret adp/gh-app-ops-id \
    --app-key-secret adp/gh-app-ops-key \
    --k8s-secret agent-context-secrets \
    --k8s-key github-token \
    --namespace agent-context
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time


EXIT_GENERAL_ERROR = 1
EXIT_INSTALLATION_REVOKED = 2  # Installation not found (404/410) — revoked or removed


def error_exit(msg, code=EXIT_GENERAL_ERROR):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _get_secret_string(secret_id, region):
    """Retrieve a plain string secret from AWS Secrets Manager."""
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        error_exit("boto3 not installed. Run: pip install boto3")

    try:
        sm = boto3.client("secretsmanager", region_name=region)
        response = sm.get_secret_value(SecretId=secret_id)
        return response["SecretString"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            error_exit(f"Secret '{secret_id}' not found in Secrets Manager ({region})")
        elif code == "AccessDeniedException":
            error_exit(f"Access denied to secret '{secret_id}'. Check IAM/IRSA permissions.")
        else:
            error_exit(f"Secrets Manager error: {e}")


def get_credentials_split(app_id_secret, app_key_secret, region):
    """Get credentials from two separate secrets (app ID + private key)."""
    app_id = _get_secret_string(app_id_secret, region).strip()
    private_key = _get_secret_string(app_key_secret, region).strip()
    return app_id, private_key


def get_credentials_combined(secret_id, region):
    """Get credentials from a single JSON secret (legacy format)."""
    raw = _get_secret_string(secret_id, region)
    try:
        secret = json.loads(raw)
    except json.JSONDecodeError:
        error_exit(f"Secret '{secret_id}' does not contain valid JSON")
    for key in ("app_id", "private_key"):
        if key not in secret:
            error_exit(f"Secret '{secret_id}' is missing required key: '{key}'")
    return str(secret["app_id"]), secret["private_key"], secret.get("installation_id")


def generate_jwt(app_id, private_key):
    """Generate a JWT for GitHub App authentication."""
    try:
        import jwt
    except ImportError:
        error_exit("PyJWT not installed. Run: pip install PyJWT cryptography")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": str(app_id)}
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as e:
        error_exit(f"Failed to generate JWT: {e}")


def discover_installation_id(encoded_jwt, owner=None):
    """Discover the installation ID from the GitHub API."""
    try:
        import requests
    except ImportError:
        error_exit("requests not installed. Run: pip install requests")
    headers = {
        "Authorization": f"Bearer {encoded_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.get("https://api.github.com/app/installations", headers=headers, timeout=30)
    except requests.exceptions.RequestException as e:
        error_exit(f"GitHub API request failed: {e}")
    if resp.status_code != 200:
        error_exit(f"Failed to list installations: {resp.status_code} {resp.text[:300]}")
    installations = resp.json()
    if not installations:
        error_exit("No installations found for this GitHub App")
    if owner:
        for inst in installations:
            if inst.get("account", {}).get("login", "").lower() == owner.lower():
                return inst["id"]
        error_exit(f"No installation found for owner '{owner}'")
    installation = installations[0]
    print(
        f"Using installation {installation['id']} (account: {installation.get('account', {}).get('login', 'unknown')})",
        file=sys.stderr,
    )
    return installation["id"]


def get_installation_token(encoded_jwt, installation_id):
    """Exchange JWT for an installation access token."""
    try:
        import requests
    except ImportError:
        error_exit("requests not installed. Run: pip install requests")
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {encoded_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.post(url, headers=headers, timeout=30)
    except requests.exceptions.RequestException as e:
        error_exit(f"GitHub API request failed: {e}")
    if resp.status_code == 401:
        error_exit("GitHub API returned 401. JWT may be expired or private key is incorrect.")
    elif resp.status_code in (404, 410):
        error_exit(
            f"Installation {installation_id} not found (HTTP {resp.status_code}). "
            "The installation may have been revoked or removed.",
            code=EXIT_INSTALLATION_REVOKED,
        )
    elif resp.status_code != 201:
        error_exit(f"GitHub API returned {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return data["token"], data.get("expires_at", "unknown")


def _validate_k8s_name(name, label="value"):
    if not re.match(r"^[a-z0-9][a-z0-9.\-]{0,252}$", name):
        error_exit(f"Invalid Kubernetes {label}: '{name}'.")


def update_k8s_secret(secret_name, key, value, namespace):
    _validate_k8s_name(secret_name, "secret name")
    _validate_k8s_name(namespace, "namespace")
    _validate_k8s_name(key, "secret key")
    patch_data = json.dumps({"stringData": {key: value}})
    try:
        subprocess.run(
            ["kubectl", "patch", "secret", secret_name, "-n", namespace, "-p", patch_data],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        error_exit(f"Failed to patch K8s secret: {e.stderr}")
    except FileNotFoundError:
        error_exit("kubectl not found in PATH")


def main():
    parser = argparse.ArgumentParser(description="Generate GitHub App installation access token")
    parser.add_argument(
        "--app-id-secret", help="Secrets Manager ID for App ID (e.g., adp/gh-app-ops-id)"
    )
    parser.add_argument(
        "--app-key-secret", help="Secrets Manager ID for App private key (e.g., adp/gh-app-ops-key)"
    )
    parser.add_argument("--secret-id", help="Single JSON secret ID (legacy mode)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--owner", help="GitHub org/user to find installation for")
    parser.add_argument("--installation-id", help="Installation ID (auto-discovers if omitted)")
    parser.add_argument("--output-file", help="Write token to file")
    parser.add_argument("--k8s-secret", help="K8s secret to update")
    parser.add_argument("--k8s-key", default="github-token")
    parser.add_argument("--namespace", default="agent-context")
    args = parser.parse_args()

    if args.app_id_secret and args.app_key_secret:
        mode = "split"
    elif args.secret_id:
        mode = "combined"
    else:
        error_exit("Provide --app-id-secret + --app-key-secret (preferred) or --secret-id (legacy)")

    installation_id = args.installation_id
    if mode == "split":
        print(
            f"Fetching credentials from '{args.app_id_secret}' + '{args.app_key_secret}'...",
            file=sys.stderr,
        )
        app_id, private_key = get_credentials_split(
            args.app_id_secret, args.app_key_secret, args.region
        )
    else:
        print(f"Fetching credentials from '{args.secret_id}'...", file=sys.stderr)
        app_id, private_key, stored_id = get_credentials_combined(args.secret_id, args.region)
        if not installation_id and stored_id:
            installation_id = stored_id

    print(f"App ID: {app_id}", file=sys.stderr)
    print("Generating JWT...", file=sys.stderr)
    encoded_jwt = generate_jwt(app_id, private_key)

    if not installation_id:
        print("Discovering installation ID from GitHub API...", file=sys.stderr)
        installation_id = discover_installation_id(encoded_jwt, owner=args.owner)
    print(f"Installation ID: {installation_id}", file=sys.stderr)

    print("Requesting installation access token...", file=sys.stderr)
    token, expires_at = get_installation_token(encoded_jwt, installation_id)
    print(f"Token obtained (expires: {expires_at})", file=sys.stderr)

    if args.output_file:
        os.makedirs(os.path.dirname(args.output_file) or ".", exist_ok=True)
        fd = os.open(args.output_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode())
        finally:
            os.close(fd)
        print(f"Token written to {args.output_file} (expires: {expires_at})", file=sys.stderr)

    if args.k8s_secret:
        print(f"Updating K8s secret {args.k8s_secret}/{args.k8s_key}...", file=sys.stderr)
        update_k8s_secret(args.k8s_secret, args.k8s_key, token, args.namespace)
        print(f"K8s secret updated (expires: {expires_at})", file=sys.stderr)

    if not args.output_file and not args.k8s_secret:
        print(token)


if __name__ == "__main__":
    main()
