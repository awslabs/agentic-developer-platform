"""Load existing gateway signing material for the tick, independently of IAM mode."""

import os


def signing_key() -> str:
    value = os.environ.get("AGENT_RUN_CREDENTIAL_KEY", "")
    if value:
        return value
    parameter = os.environ.get("AGENT_RUN_CREDENTIAL_KEY_PARAMETER", "")
    if not parameter:
        return ""
    try:
        import boto3

        value = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")).get_parameter(
            Name=parameter,
            WithDecryption=True,
        )["Parameter"]["Value"]
    except Exception:
        # Neither SDK exceptions nor secret values belong in dispatch logs.
        return ""
    if not isinstance(value, str) or len(value) < 32:
        return ""
    os.environ["AGENT_RUN_CREDENTIAL_KEY"] = value
    return value
