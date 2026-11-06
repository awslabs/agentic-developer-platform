"""Sign native requests only inside the explicitly selected broker session."""

import json
import os
import sys
import urllib.parse


def sign(document):
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.config import Config
    from botocore.credentials import Credentials

    parsed = urllib.parse.urlsplit(document["url"])
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or document["method"] not in {"GET", "POST"}):
        raise ValueError("invalid native signing request")
    selected = [os.environ[name] for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")]
    if not all(selected):
        raise ValueError("selected native session is incomplete")
    credentials = Credentials(*selected)
    client = boto3.client("sts", region_name=document["region"],
        aws_access_key_id=credentials.access_key, aws_secret_access_key=credentials.secret_key,
        aws_session_token=credentials.token,
        config=Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}, proxies={}))
    identity = client.get_caller_identity()
    if (identity.get("Account") != document["account"]
            or identity.get("Arn", "").rsplit("/", 1)[0] != document["role"]
            or ":assumed-role/" not in identity.get("Arn", "")):
        raise ValueError("selected native signing identity differs")
    request = AWSRequest(method=document["method"], url=document["url"],
                         data=document["body"].encode(), headers=document["headers"])
    SigV4Auth(credentials, "execute-api", document["region"]).add_auth(request)
    return dict(request.headers.items())


def main():
    try:
        document = json.loads(sys.stdin.read(1048577))
        print(json.dumps(sign(document)))
        return 0
    except Exception:
        print("native signing unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
