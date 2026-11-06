"""Issue short-lived credentials for one recorded Dev Box instance."""

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from src.shared.models.dev_box import DevBoxInstance

INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}\Z")
REGION = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d\Z")
ACCOUNT_ID = re.compile(r"[0-9]{12}\Z")
OS_USER = "ec2-user"


def ssh_policy(instance: DevBoxInstance) -> dict:
    account = instance.provider_account_ref
    region = instance.region
    instance_id = instance.provider_instance_id
    if (
        not account
        or not ACCOUNT_ID.fullmatch(account)
        or not region
        or not REGION.fullmatch(region)
        or not instance_id
        or not INSTANCE_ID.fullmatch(instance_id)
    ):
        raise HTTPException(409, "Current Dev Box instance is unavailable for SSH")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "ssm:StartSession",
                "Resource": [
                    f"arn:aws:ec2:{region}:{account}:instance/{instance_id}",
                    f"arn:aws:ssm:{region}::document/AWS-StartSSHSession",
                ],
            },
            {
                "Effect": "Allow",
                "Action": "ec2-instance-connect:SendSSHPublicKey",
                "Resource": f"arn:aws:ec2:{region}:{account}:instance/{instance_id}",
                "Condition": {"StringEquals": {"ec2:osuser": OS_USER}},
            },
        ],
    }


async def issue_ssh_credentials(instance: DevBoxInstance, box_id: str, generation: int) -> dict:
    policy = ssh_policy(instance)
    account = instance.provider_account_ref
    role = os.getenv("BG_DEV_BOX_SSH_GRANT_ROLE_ARN", "")
    if not role.startswith(f"arn:aws:iam::{account}:role/") or not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+", role):
        raise HTTPException(503, "Dev Box SSH access is not configured")

    try:
        result = await asyncio.to_thread(
            boto3.client("sts", region_name=instance.region).assume_role,
            RoleArn=role,
            RoleSessionName=f"devbox-ssh-{box_id[:12]}",
            DurationSeconds=900,
            Policy=json.dumps(policy, separators=(",", ":")),
        )
    except (BotoCoreError, ClientError) as error:
        raise HTTPException(503, "Dev Box SSH access is unavailable") from error

    credentials = result["Credentials"]
    expires = credentials["Expiration"]
    if expires.tzinfo is None or expires.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=15):
        raise HTTPException(503, "Dev Box SSH credentials exceed the maximum lifetime")
    return {
        "instance_id": instance.provider_instance_id,
        "region": instance.region,
        "os_user": OS_USER,
        "generation": generation,
        "expires_at": expires.isoformat(),
        "credentials": {
            "access_key_id": credentials["AccessKeyId"],
            "secret_access_key": credentials["SecretAccessKey"],
            "session_token": credentials["SessionToken"],
        },
    }
