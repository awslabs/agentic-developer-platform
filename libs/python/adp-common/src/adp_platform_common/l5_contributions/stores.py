"""Atomic adapters for the two existing consumer-owned storage systems."""

import asyncio
import json

from .contract import MAX_BYTES, canonical


class PostgresStore:
    def __init__(self, pool):
        self.pool = pool

    def _read(self, key):
        connection = self.pool.getconn()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT document, revision FROM l5_contribution_records WHERE record_key = %s", (key,))
                row = cursor.fetchone()
            connection.rollback()
            return (row[0], row[1]) if row else (None, None)
        except Exception:
            connection.rollback()
            raise
        finally:
            self.pool.putconn(connection)

    async def read(self, key):
        return await asyncio.to_thread(self._read, key)

    def _compare_and_set(self, key, revision, record):
        connection = self.pool.getconn()
        try:
            with connection.cursor() as cursor:
                if revision is None:
                    cursor.execute("INSERT INTO l5_contribution_records (record_key, document, revision) "
                                   "VALUES (%s, %s::jsonb, 1) ON CONFLICT DO NOTHING", (key, canonical(record).decode()))
                else:
                    cursor.execute("UPDATE l5_contribution_records SET document = %s::jsonb, revision = revision + 1 "
                                   "WHERE record_key = %s AND revision = %s", (canonical(record).decode(), key, revision))
                changed = cursor.rowcount == 1
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            self.pool.putconn(connection)

    async def compare_and_set(self, key, revision, record):
        return await asyncio.to_thread(self._compare_and_set, key, revision, record)


class S3Store:
    def __init__(self, client, *, bucket, kms_key_id):
        if not bucket or not kms_key_id:
            raise ValueError("Insights durable storage is not installed")
        self.client, self.bucket, self.kms_key_id = client, bucket, kms_key_id

    @staticmethod
    def _key(key):
        # No caller-controlled S3 path: only engine-derived hexadecimal keys.
        if not all(part and all(c in "0123456789abcdef" for c in part)
                   for part in key.replace("/tasks/", "/").split("/")):
            raise ValueError("invalid durable key")
        return "assurance/v1/l5/insights/contributions/" + key + ".json"

    def _read(self, key):
        from botocore.exceptions import ClientError

        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as error:
            if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None, None
            raise
        # Bounded persistent records; Security pagination is bounded at admission.
        body = result["Body"]
        try:
            data = body.read(MAX_BYTES * 8 + 1)
        finally:
            body.close()
        if len(data) > MAX_BYTES * 8:
            raise ValueError("durable contribution exceeds read bound")
        return json.loads(data), result["ETag"]

    async def read(self, key):
        return await asyncio.to_thread(self._read, key)

    def _compare_and_set(self, key, revision, record):
        from botocore.exceptions import ClientError

        data = canonical(record)
        if len(data) > MAX_BYTES * 8:
            raise ValueError("durable contribution exceeds storage bound")
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data,
                ContentType="application/json", ServerSideEncryption="aws:kms", SSEKMSKeyId=self.kms_key_id,
                **({"IfNoneMatch": "*"} if revision is None else {"IfMatch": revision}))
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                return False
            raise

    async def compare_and_set(self, key, revision, record):
        return await asyncio.to_thread(self._compare_and_set, key, revision, record)
