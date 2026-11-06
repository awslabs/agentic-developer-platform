"""Encrypted, conditional writes for the V01-shaped browser recovery store."""

from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4

from botocore.exceptions import ClientError

from tests.e2e.cli_uplift.statestore import ENCRYPTION, PREFIX


class BrowserRunState:
    def __init__(self, s3, bucket, kms_key_id, binding, *, namespace=None):
        if namespace is not None and (type(namespace) is not str
                                      or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", namespace)):
            raise ValueError("browser fixture namespace must be a bounded suite identifier")
        self.s3, self.bucket, self.kms_key_id = s3, bucket, kms_key_id
        self.binding = deepcopy(binding)
        self.namespace = namespace
        scope = hashlib.sha256((binding["tenant_id"] + ":" + binding["actor_id"]).encode()).hexdigest()
        self.lock_key = f"{PREFIX}/product-browser/{scope}/owner.json"
        suffix = f"-{namespace}" if namespace is not None else ""
        self.key = f"{PREFIX}/product-browser/{scope}/{binding['run_id']}{suffix}.json"
        self.document, self.version = self.read(self.key)
        if self.document is not None and self.document.get("binding") != binding:
            raise ValueError("durable browser binding differs from the selected run")
        self.lock_version = None
        self.lease = None

    def read(self, key):
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                return None, None
            if error.response["Error"]["Code"] == "AccessDenied":
                # Prefix-scoped ListBucket can deny GetObject's implicit
                # existence check. Establish absence with the permitted exact
                # prefix read; denial alone must never discard recovery state.
                try:
                    listing = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1)
                except ClientError:
                    listing = None
                if isinstance(listing, dict) and listing.get("IsTruncated") is False:
                    entries = listing.get("Contents", [])
                    if (isinstance(entries, list) and len(entries) <= 1
                            and all(isinstance(entry, dict) and isinstance(entry.get("Key"), str)
                                    and entry["Key"].startswith(key) for entry in entries)
                            and not any(entry["Key"] == key for entry in entries)):
                        return None, None
            raise RuntimeError("protected browser state could not be read") from None
        raw = response["Body"].read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("protected browser state exceeds its bound")
        return json.loads(raw), response["ETag"]

    def put(self, key, value, version):
        response = self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=json.dumps(value, sort_keys=True).encode(),
            ServerSideEncryption=ENCRYPTION, SSEKMSKeyId=self.kms_key_id,
            ACL="bucket-owner-full-control", ContentType="application/json",
            **({"IfMatch": version} if version else {"IfNoneMatch": "*"}),
        )
        return response["ETag"]

    def claim(self, *, recover=False):
        lock, version = self.read(self.lock_key)
        if self.document is not None and not recover:
            raise ValueError("an existing browser run must be recovered, not replayed as new")
        if recover and self.document is None and (not lock or lock.get("run_id") != self.binding["run_id"]):
            raise ValueError("browser recovery requires original durable ownership")
        if lock and lock.get("run_id") is not None:
            if not recover or lock.get("run_id") != self.binding["run_id"]:
                raise ValueError("recover the active browser run before claiming this fixture")
            if lock.get("namespace") != self.namespace:
                raise ValueError("browser recovery requires the original suite namespace")
        self.lease = {"run_id": self.binding["run_id"], "holder": uuid4().hex}
        if self.namespace is not None:
            self.lease["namespace"] = self.namespace
        self.lock_version = self.put(self.lock_key, self.lease, version)
        if self.document is None:
            self.save({"binding": self.binding, "cases": {}, "restore_required": False})

    def save(self, value):
        lock, version = self.read(self.lock_key)
        if lock != self.lease or version != self.lock_version:
            raise ValueError("browser fixture ownership changed")
        if value.get("binding") != self.binding:
            raise ValueError("browser state cannot change its ownership binding")
        new_version = self.put(self.key, value, self.version)
        observed, observed_version = self.read(self.key)
        if observed != value or observed_version != new_version:
            raise RuntimeError("browser state was not durably readable")
        self.document, self.version = deepcopy(value), new_version

    def release(self):
        if self.document.get("restore_required") or self.document.get("scope_editor_restore_required") or self.document.get("scope_owner_role_restore_required") or any(
            not case.get("cleanup") for case in self.document["cases"].values()
        ):
            raise ValueError("browser fixture cleanup must finish before release")
        if any(self.document.get(key, {}).get("cleanup_pending") for key in ("catalog", "catalog_history", "catalog_recovery")):
            raise ValueError("catalog fixture cleanup must finish before browser state release")
        if self.document.get("scope", {}).get("cleanup_pending"):
            raise ValueError("scope fixture cleanup must finish before browser state release")
        released_version = self.put(self.lock_key, {"run_id": None}, self.lock_version)
        observed, version = self.read(self.lock_key)
        if observed != {"run_id": None} or version != released_version:
            raise RuntimeError("browser fixture lock release was not independently observed")
        return {"released": True, "version": version}


class FixtureStore:
    def __init__(self, state, case):
        self.state, self.case = state, case

    def load(self, run_id):
        if run_id != self.state.binding["run_id"]:
            raise ValueError("fixture store run mismatch")
        value = self.state.document["cases"][self.case]
        return deepcopy(value.get("document")), deepcopy(value.get("manifest"))

    def save(self, run_id, document, manifest=None):
        self.load(run_id)
        value = deepcopy(self.state.document)
        value["cases"][self.case].update(document=document, manifest=manifest)
        self.state.save(value)
