"""Installed Engineering Insights receiver backed by its versioned evidence bucket."""

import json
import os
from functools import lru_cache

import boto3
from adp_platform_common.l5_contributions.http import receiver_router
from adp_platform_common.l5_contributions.stores import S3Store
from fastapi import FastAPI

app = FastAPI(title="Engineering Insights contributions")


@lru_cache(maxsize=1)
def store():
    return S3Store(boto3.client("s3", region_name=os.environ["AWS_REGION"]),
                   bucket=os.environ["L5_INSIGHTS_EVIDENCE_BUCKET"],
                   kms_key_id=os.environ["L5_INSIGHTS_EVIDENCE_KMS_KEY"])


app.include_router(receiver_router(consumer="insights", store_factory=store,
    keys_factory=lambda: json.loads(os.environ.get("L5_CONSUMER_VERIFICATION_KEYS", "{}"))))


@app.get("/health")
def health():
    return {"status": "ok"}
