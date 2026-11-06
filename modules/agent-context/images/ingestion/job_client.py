"""One-attempt ingestion client; deliberately holds no shared AWS/DB credential."""

from __future__ import annotations

import dataclasses
import functools
import inspect
import io
import json
import os
from pathlib import Path

import requests

MAX_OBJECT_BYTES = 512 * 1024 * 1024


def enabled() -> bool:
    return bool(os.environ.get("INGESTION_JOB_FILE"))


def job() -> dict:
    path = Path(os.environ["INGESTION_JOB_FILE"])
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError("Ingestion admission is too large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not value.get("callback_grant"):
        raise RuntimeError("Ingestion requires a registered attempt")
    return value


def control_url() -> str:
    from urllib.parse import urlsplit

    value = os.environ.get("INGESTION_CONTROL_URL", "")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Ingestion control endpoint is not configured")
    return value


def call(operation: str, arguments: dict | None = None):
    return invoke(control_url(), job()["callback_grant"], operation, arguments or {})


def invoke(url: str, grant: str, operation: str, arguments: dict):
    response = requests.post(
        url,
        json={"callback_grant": grant, "operation": operation, "arguments": arguments},
        timeout=(10, 120),
        allow_redirects=False,
    )
    response.close()  # requests eagerly read this bounded JSON response
    if response.status_code != 200:
        if response.status_code == 404:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Object not found"}}, operation
            )
        raise RuntimeError(f"Ingestion operation refused ({response.status_code})")
    return response.json()["result"]


def install_github_token() -> bool:
    value = job().get("github_token")
    if not value:
        return False
    path = Path("/tmp/github-token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    os.environ["GIT_ASKPASS"] = "/app/git-credential-helper.sh"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    return True


class JobConnection:
    """No SQL cursor: only the named db.py commands can cross the boundary."""

    def close(self):
        pass


def catalog_rpc(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    def bounded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        if isinstance(bound.arguments.get("conn"), JobConnection):
            params = {k: v for k, v in bound.arguments.items() if k != "conn"}
            params = json.loads(json.dumps(params, default=lambda value: dataclasses.asdict(value)))
            return call("catalog." + function.__name__, params)
        return function(*args, **kwargs)

    return bounded


class ObjectClient:
    def list_objects_v2(self, **kwargs):
        return call("object.list", kwargs)

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise RuntimeError("Unsupported ingestion operation")
        client = self

        class Pages:
            def paginate(self, **kwargs):
                while True:
                    page = client.list_objects_v2(**kwargs)
                    yield page
                    if not page.get("IsTruncated"):
                        break
                    kwargs["ContinuationToken"] = page["NextContinuationToken"]

        return Pages()

    def head_object(self, *, Bucket, Key, **kwargs):
        return call("object.head", {"Bucket": Bucket, "Key": Key})

    def get_object(self, *, Bucket, Key, **kwargs):
        cap = call("object.get", {"Bucket": Bucket, "Key": Key})
        response = requests.get(cap["url"], stream=True, timeout=(10, 120), allow_redirects=False)
        try:
            if response.status_code != 200:
                raise RuntimeError("Ingestion object read failed")
            out = io.BytesIO()
            for chunk in response.iter_content(1024 * 1024):
                if out.tell() + len(chunk) > MAX_OBJECT_BYTES:
                    raise RuntimeError("Ingestion object exceeds size limit")
                out.write(chunk)
            out.seek(0)
            return {"Body": out, "ContentLength": out.getbuffer().nbytes}
        finally:
            response.close()

    def put_object(self, *, Bucket, Key, Body, ContentType="application/octet-stream", **kwargs):
        payload = Body.read(MAX_OBJECT_BYTES + 1) if hasattr(Body, "read") else Body
        if isinstance(payload, str):
            payload = payload.encode()
        if not isinstance(payload, bytes) or len(payload) > MAX_OBJECT_BYTES:
            raise RuntimeError("Ingestion object exceeds size limit")
        cap = call(
            "object.put",
            {
                "Bucket": Bucket,
                "Key": Key,
                "ContentLength": len(payload),
                "ContentType": ContentType,
            },
        )
        response = requests.post(
            cap["url"],
            data=cap["fields"],
            files={"file": ("artifact", payload)},
            timeout=(10, 120),
            allow_redirects=False,
        )
        response.close()
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError("Ingestion object write failed")
        return {}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        with open(Filename, "rb") as stream:
            return self.put_object(Bucket=Bucket, Key=Key, Body=stream, **(ExtraArgs or {}))

    def download_file(self, Bucket, Key, Filename):
        body = self.get_object(Bucket=Bucket, Key=Key)["Body"]
        Path(Filename).write_bytes(body.read())


class VectorWriter:
    def __init__(self):
        self._prepared = set()

    def put_vectors(self, index_name, vectors):
        if index_name not in self._prepared:
            call("vectors.prepare", {"index_name": index_name})
            self._prepared.add(index_name)
        prefix = f"{job()['registry_asset_id']}:{job()['ingestion_attempt_id']}:"
        # Legacy wiki keys lack registration identity; always namespace them.
        scoped = [
            {**v, "key": v["key"] if v["key"].startswith(prefix) else prefix + v["key"]}
            for v in vectors
        ]
        for offset in range(0, len(scoped), 500):
            call(
                "vectors.put", {"index_name": index_name, "vectors": scoped[offset : offset + 500]}
            )

    def count_repo_vectors(self, index_name, query_vector, org_repo):
        return call("vectors.count", {"index_name": index_name, "query_vector": query_vector})

    def count_asset_vectors(self, index_name, query_vector, *args, **kwargs):
        return call("vectors.count", {"index_name": index_name, "query_vector": query_vector})

    def clear_asset_vectors(self, index_name, *args):
        return call("vectors.clear", {"index_name": index_name})
