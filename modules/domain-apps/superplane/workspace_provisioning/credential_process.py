"""Bounded SDK credential-process client for the worker's private memory bridge."""

from datetime import datetime, timezone
import json
import os
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

# Two live authority checks each have a 35-second worker bridge bound.
TIMEOUT_SECONDS = 75
MAX_RESPONSE_BYTES = 16384


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("credential bridge redirect refused")


def read_credentials(environment):
    url = environment["SUPERPLANE_CREDENTIAL_BRIDGE_URL"]
    token = environment["SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN"]
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path != "/credentials"
        or parsed.query
        or parsed.fragment
        or not 32 <= len(token) <= 256
        or not token.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in token)
    ):
        raise ValueError("private credential bridge required")
    request = Request(url, headers={"Authorization": token})
    with build_opener(ProxyHandler({}), NoRedirect()).open(
        request, timeout=TIMEOUT_SECONDS
    ) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("credential response exceeded bound")
    value = json.loads(body)
    if not isinstance(value, dict) or set(value) != {
        "AccessKeyId",
        "SecretAccessKey",
        "Token",
        "Expiration",
    }:
        raise ValueError("credential response shape differs")
    if any(not isinstance(v, str) or not v for v in value.values()):
        raise ValueError("credential response fields differ")
    expiry = datetime.fromisoformat(value["Expiration"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("credential response expired")
    return {
        "Version": 1,
        "AccessKeyId": value["AccessKeyId"],
        "SecretAccessKey": value["SecretAccessKey"],
        "SessionToken": value["Token"],
        "Expiration": value["Expiration"],
    }


def main():
    try:
        value = read_credentials(os.environ)
    except Exception:
        # SDKs may include this process's stderr in errors. Never echo a URL,
        # token, response body or credential when retrieval fails.
        print("Operation credential authority unavailable", file=sys.stderr)
        return 1
    json.dump(value, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
