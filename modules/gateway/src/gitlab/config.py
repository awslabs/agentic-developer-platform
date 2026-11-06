"""GitLab configuration shared with workers that have no gateway auth authority."""

import os
import re
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException


def environment():
    return os.environ.get("BG_ENVIRONMENT", "dev")


def region():
    return os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def discover_url():
    try:
        value = boto3.client("ssm", region_name=region()).get_parameter(Name=f"/adp/{environment()}/gitlab/url")["Parameter"]["Value"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise
    return host(value)


def host(value):
    """Validate an operator-approved HTTPS base URL, including a rooted install."""
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        # Accessing port also rejects malformed/out-of-range authorities.
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port == 0
            or any(char in value for char in ("\\", "%", "?", "#"))
            or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", parsed.path)
            or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        ):
            raise ValueError
    except ValueError:
        raise HTTPException(503, "Approved GitLab provider must be an HTTPS base URL with a canonical path") from None
    return value.rstrip("/")
