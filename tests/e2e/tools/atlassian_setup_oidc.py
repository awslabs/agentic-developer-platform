"""Refresh scoped GitHub OIDC credentials for a protected setup observer role."""

import json
import os
import re
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def credential_process(role_arn, *, environment):
    expected_workflow = (
        "aws-e/adp/.github/workflows/tools-live-evaluation.yml@refs/heads/main"
    )
    if (
        os.environ.get("GITHUB_REPOSITORY") != "aws-e/adp"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_WORKFLOW_REF") != expected_workflow
        or environment not in {"dev", "test", "prod"}
        or not re.fullmatch(r"[0-9]+", os.environ.get("GITHUB_RUN_ID", ""))
        or not re.fullmatch(r"[0-9]+", os.environ.get("GITHUB_RUN_ATTEMPT", ""))
        or not re.fullmatch(
            r"arn:aws:iam::[0-9]{12}:role/adp-(dev|test|prod)-atlassian-mcp-observer-(core|adapter)",
            role_arn,
        )
        or not role_arn.split(":role/", 1)[1].startswith(f"adp-{environment}-")
    ):
        raise ValueError("Protected setup observer context or role unavailable")
    endpoint = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(
            r"pipelines[a-z0-9-]*\.actions\.githubusercontent\.com",
            parsed.hostname or "",
        )
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("GitHub OIDC endpoint unavailable")
    endpoint += ("&" if "?" in endpoint else "?") + urlencode(
        {"audience": "sts.amazonaws.com"}
    )
    token = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    if not token:
        raise ValueError("GitHub OIDC request unavailable")
    opener = build_opener(NoRedirect)
    with opener.open(
        Request(endpoint, headers={"Authorization": "Bearer " + token}), timeout=15
    ) as response:
        identity = response.read(16385)
    if len(identity) > 16384:
        raise ValueError("GitHub OIDC response exceeded bound")
    jwt = json.loads(identity)["value"]
    if (
        not isinstance(jwt, str)
        or not jwt
        or any(character.isspace() for character in jwt)
    ):
        raise ValueError("GitHub OIDC token unavailable")
    region = os.environ["AWS_REGION"]
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region):
        raise ValueError("Selected AWS region unavailable")
    fields = {
        "Action": "AssumeRoleWithWebIdentity",
        "Version": "2011-06-15",
        "RoleArn": role_arn,
        "RoleSessionName": f"setup-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
        "WebIdentityToken": jwt,
        "DurationSeconds": "900",
    }
    with opener.open(
        Request(
            f"https://sts.{region}.amazonaws.com/",
            data=urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        timeout=15,
    ) as response:
        credentials = response.read(16385)
    if len(credentials) > 16384:
        raise ValueError("AWS STS response exceeded bound")
    item = ElementTree.fromstring(credentials).find(".//{*}Credentials")
    if item is None:
        raise ValueError("Scoped AWS credentials unavailable")
    values = {
        tag: item.findtext("{*}" + tag)
        for tag in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError("Scoped AWS credentials incomplete")
    return {"Version": 1, **values}


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("Expected scoped observer role and environment")
        result = credential_process(sys.argv[1], environment=sys.argv[2])
        print(json.dumps(result))
    except Exception:
        print("Protected observer credential exchange unavailable", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
