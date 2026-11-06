"""Signed immutable queue-fixture receipts and exact owned version cleanup."""

import base64
import json
from hashlib import sha256

from botocore.exceptions import ClientError

from src.l5_devops_verification import canonical


class QueueEvidence:
    def __init__(self, session, selection):
        from botocore.config import Config

        self.selection = selection
        config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})
        self.s3 = session.client("s3", region_name=selection["region"], config=config)
        self.kms = session.client("kms", region_name=selection["region"], config=config)

    def _args(self, key):
        if not key.startswith(self.selection["prefix"]) or ".." in key:
            raise ValueError("evidence key is outside the installed fixture")
        return {"Bucket": self.selection["bucket"], "Key": key, "ExpectedBucketOwner": self.selection["account_id"]}

    def _sign(self, payload):
        result = self.kms.sign(
            KeyId=self.selection["signing_key_arn"],
            Message=sha256(canonical(payload)).digest(),
            MessageType="DIGEST",
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if result.get("KeyId") != self.selection["signing_key_arn"] or not result.get("Signature"):
            raise ValueError("fixture signing key differs from installation")
        return canonical({"payload": payload, "signature": base64.b64encode(result["Signature"]).decode()})

    def _read(self, key, *, reference=None):
        args = self._args(key)
        if reference is not None:
            if (
                reference.get("bucket") != args["Bucket"]
                or reference.get("key") != key
                or not reference.get("version")
                or reference["version"] == "null"
            ):
                raise ValueError("fixture evidence reference changed")
            args["VersionId"] = reference["version"]
        try:
            response = self.s3.get_object(**args)
        except ClientError as error:
            # AccessDenied and ambiguous failures never prove absence.
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "NoSuchVersion"}:
                return None
            raise
        try:
            content = response["Body"].read(1024 * 1024 + 1)
        finally:
            response["Body"].close()
        digest = sha256(content).hexdigest()
        version = response.get("VersionId")
        if (
            len(content) > 1024 * 1024
            or not version
            or version == "null"
            or response.get("Metadata", {}).get("sha256") != digest
            or reference is not None
            and (version != reference["version"] or digest != reference["sha256"])
        ):
            raise ValueError("fixture evidence integrity changed")
        envelope = json.loads(content)
        if set(envelope) != {"payload", "signature"}:
            raise ValueError("fixture signed envelope invalid")
        result = self.kms.verify(
            KeyId=self.selection["signing_key_arn"],
            Message=sha256(canonical(envelope["payload"])).digest(),
            MessageType="DIGEST",
            SigningAlgorithm="RSASSA_PSS_SHA_256",
            Signature=base64.b64decode(envelope["signature"], validate=True),
        )
        if result.get("SignatureValid") is not True or result.get("KeyId") != self.selection["signing_key_arn"]:
            raise ValueError("fixture evidence signature invalid")
        return envelope["payload"], {"bucket": args["Bucket"], "key": key, "version": version, "sha256": digest}

    def read(self, name):
        key = self.selection["prefix"] + name
        result = self._read(key)
        if result is None:
            return None
        payload, reference = result
        if set(payload) != {"key", "data"} or payload["key"] != key:
            raise ValueError("fixture receipt was moved from its signed key")
        return payload["data"], reference

    def create(self, name, data):
        """First writer wins. Return false on replay, without overwriting evidence."""
        key = self.selection["prefix"] + name
        content = self._sign({"key": key, "data": data})
        try:
            result = self.s3.put_object(
                **self._args(key),
                Body=content,
                ContentType="application/json",
                IfNoneMatch="*",
                Metadata={"sha256": sha256(content).hexdigest()},
                ServerSideEncryption="AES256",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            previous = self.read(name)
            if previous is None or previous[0] != data:
                raise ValueError("fixture receipt conflicts with an earlier selection") from None
            return False, previous[1]
        if not result.get("VersionId") or result["VersionId"] == "null":
            raise ValueError("fixture evidence bucket must have versioning enabled")
        saved = self.read(name)
        if saved is None or saved[0] != data or saved[1]["version"] != result["VersionId"]:
            raise ValueError("fixture write could not be independently read back")
        return True, saved[1]

    def publish(self, payload):
        """Emit exactly the signed envelope consumed by VerificationReader."""
        content = self._sign(payload)
        key = self.selection["prefix"] + "admission/" + sha256(content).hexdigest()
        result = self.s3.put_object(
            **self._args(key),
            Body=content,
            ContentType="application/json",
            IfNoneMatch="*",
            Metadata={"sha256": sha256(content).hexdigest()},
            ServerSideEncryption="AES256",
        )
        if not result.get("VersionId") or result["VersionId"] == "null":
            raise ValueError("fixture evidence bucket must have versioning enabled")
        reference = {"bucket": self.selection["bucket"], "key": key, "version": result["VersionId"], "sha256": sha256(content).hexdigest()}
        if self._read(key, reference=reference)[0] != payload:
            raise ValueError("published fixture observation failed readback")
        return {
            **reference,
            "owner_account_id": self.selection["account_id"],
            "signing_key_arn": self.selection["signing_key_arn"],
            "producer": self.selection["producer"],
        }

    def delete_owned(self, name, manifest, token):
        """Delete only the exact immutable payload version in its signed receipt."""
        key = self.selection["prefix"] + name + "/payload"
        reference = manifest["payload_ref"]
        if reference["key"] != key or manifest["deletion_token"] != token:
            raise ValueError("fixture cleanup selection differs from its owned receipt")
        before = self._read(key, reference=reference)
        if before is not None:
            if before[0] != {"key": key, "data": manifest["payload"]}:
                raise ValueError("fixture cleanup payload ownership changed")
            self.s3.delete_object(**self._args(key), VersionId=reference["version"])
        if self._read(key, reference=reference) is not None:
            raise ValueError("exact owned fixture version is still present")
        self.create(name + "/cleanup", {"payload_ref": reference, "deletion_token": token, "absent": True})

    def payload_present(self, name, manifest):
        return self.read_payload(name, manifest) is not None

    def read_payload(self, name, manifest):
        key = self.selection["prefix"] + name + "/payload"
        if manifest["payload_ref"]["key"] != key:
            raise ValueError("fixture payload reference differs from the owned key")
        result = self._read(key, reference=manifest["payload_ref"])
        if result is not None and result[0] != {"key": key, "data": manifest["payload"]}:
            raise ValueError("fixture data differs from its owned receipt")
        return None if result is None else result[0]["data"]
