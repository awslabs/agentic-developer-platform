"""Resolve independently installed L5 transports without a gateway redeployment."""

import json
import os
import re

import boto3
from l5_services.queue_transport import sqs_publisher
from starlette.concurrency import run_in_threadpool

SERVICES = {"security", "devops", "transform", "well_architected", "finops"}


def publisher(service_id):
    if service_id not in SERVICES:
        return None
    variable = "L5_WORK_QUEUE_URL" if service_id == "security" else f"L5_{service_id.upper()}_WORK_QUEUE_URL"
    explicit = os.environ.get(variable)
    if explicit:

        async def publish_explicit(envelope, delay):
            await sqs_publisher(explicit)(envelope, delay)

        return publish_explicit
    environment = os.environ.get("L5_DEPLOYMENT_ENV")
    if environment not in {"dev", "test", "prod"}:
        return None

    async def publish(envelope, delay):
        if type(delay) is not int or not 0 <= delay <= 900:
            raise ValueError("SQS delay must be between 0 and 900 seconds")

        def send():
            parameter = boto3.client("ssm").get_parameter(Name=f"/adp/{environment}/l5-services/{service_id}/work-queue-url")["Parameter"]
            match = re.fullmatch(r"arn:aws:ssm:([a-z0-9-]+):([0-9]{12}):parameter/.+", parameter["ARN"])
            suffix = "" if service_id == "security" else "-" + service_id.replace("_", "-")
            if match is None:
                raise ValueError("L5 queue registry identity unavailable")
            region, account = match.groups()
            expected = f"https://sqs.{region}.amazonaws.com/{account}/adp-{environment}-l5{suffix}-work"
            if parameter["Value"] != expected:
                raise ValueError("L5 queue registry differs from selected service")
            boto3.client("sqs", region_name=region).send_message(
                QueueUrl=expected, MessageBody=json.dumps(envelope, separators=(",", ":")), DelaySeconds=delay
            )

        await run_in_threadpool(send)

    return publish
