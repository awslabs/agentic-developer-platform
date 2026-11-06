"""Read only the original, versioned native answer selected by a scoped result."""

import asyncio
import re
from hashlib import sha256


class FinOpsAnswerReader:
    def __init__(self, bucket, client):
        self.bucket = bucket
        self.client = client

    async def read(self, run_id, reference):
        if (not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f-]{36}", run_id) is None
                or not isinstance(reference, dict) or set(reference) != {"artifact_id", "version", "sha256"}
                or reference["artifact_id"] != f"l5/finops/work/{run_id}/native/answers/original.txt"
                or not isinstance(reference["version"], str) or not reference["version"]
                or not isinstance(reference["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is None):
            raise ValueError("original native answer reference is invalid")
        result = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket,
                                         Key=reference["artifact_id"], VersionId=reference["version"])
        if (result.get("VersionId") != reference["version"]
                or result.get("ContentLength", 1 << 30) > 1024 * 1024):
            raise ValueError("original native answer version or size changed")
        contents = await asyncio.to_thread(result["Body"].read, 1024 * 1024 + 1)
        if len(contents) > 1024 * 1024 or sha256(contents).hexdigest() != reference["sha256"]:
            raise ValueError("original native answer digest changed")
        return contents.decode("utf-8")
