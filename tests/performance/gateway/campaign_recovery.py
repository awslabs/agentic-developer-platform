"""Fenced, protected fixture ownership journal for independent cleanup."""

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class RecoveryError(RuntimeError):
    pass


KINDS = {"quota", "identity", "secret"}


class RecoveryJournal:
    def __init__(self, directory, campaign, owner, clock=time.time, lease_seconds=30, create=True):
        self.directory = Path(directory)
        if (not self.directory.is_absolute() or not self.directory.is_dir()
                or self.directory.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise RecoveryError("journal requires an existing protected directory")
        if (not campaign or not owner or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise RecoveryError("journal requires campaign, owner and finite lease")
        self.path = self.directory / (campaign + ".journal.json")
        self.lock_path = self.directory / (campaign + ".lock")
        if self.path.name != campaign + ".journal.json" or "/" in campaign or campaign in {".", ".."}:
            raise RecoveryError("invalid campaign name")
        self.campaign = campaign
        self.owner = owner
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.epoch = None
        with self._locked(create=create) as record:
            if record["campaign"] != campaign or record["owner"] != owner:
                raise RecoveryError("journal ownership mismatch")
            self.epoch = record["epoch"]

    @contextmanager
    def _locked(self, create=False):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise RecoveryError("journal permissions changed")
                record = json.loads(self.path.read_text())
                if record.get("version") not in (1, 2, 3):
                    raise RecoveryError("unsupported recovery journal version")
            elif create:
                record = {"version": 1, "campaign": self.campaign, "owner": self.owner,
                          "epoch": 0, "lease_until": self.clock() + self.lease_seconds,
                          "status": "active", "intents": [], "attempts": [], "holds": [], "receipt": None}
                self._save(record)
            else:
                raise RecoveryError("journal not found")
            yield record
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _save(self, record):
        temporary = self.directory / (self.path.name + "." + uuid.uuid4().hex)
        with temporary.open("x") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(record, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _require_owner(self, record):
        if record["epoch"] != self.epoch or record["status"] != "active":
            raise RecoveryError("campaign has lost its recovery lease")
        if self.clock() >= record["lease_until"]:
            raise RecoveryError("campaign recovery lease expired")

    def heartbeat(self):
        with self._locked() as record:
            self._require_owner(record)
            record["lease_until"] = self.clock() + self.lease_seconds
            self._save(record)

    def bind_recovery(self, config, initialize=False):
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        with self._locked() as record:
            expected = record.get("recovery_binding")
            if expected is not None and expected != fingerprint:
                raise RecoveryError("recovery configuration differs from the journaled target and fixtures")
            if initialize:
                self._require_owner(record)
                record["recovery_binding"] = fingerprint
                self._save(record)

    def mutate(self, backend, kind, binding, desired, ownership_receipt):
        if kind not in KINDS or not isinstance(binding, str) or not binding or not ownership_receipt:
            raise RecoveryError("exact owned fixture binding and receipt required")
        with self._locked() as record:
            self._require_owner(record)
            if any(item["kind"] == kind and item["binding"] == binding for item in record["intents"]):
                raise RecoveryError("duplicate fixture intent")
            if not backend.verify_baseline():
                raise RecoveryError("baseline health must be verified before fixture mutation")
            snapshot = backend.read(kind, binding)
            if kind == "secret" and (
                    not isinstance(desired, dict) or set(desired) != {"reference"}
                    or not isinstance(desired["reference"], str)
                    or not desired["reference"]
                    or (snapshot["value"] is not None and (
                        not isinstance(snapshot["value"], dict)
                        or set(snapshot["value"]) != {"reference"}))):
                raise RecoveryError("secret journal accepts references only, never credential bytes")
            if not backend.owns(kind, binding, self.owner, ownership_receipt):
                raise RecoveryError("fixture ownership not verified")
            intent = {"id": uuid.uuid4().hex, "kind": kind, "binding": binding,
                      "before": snapshot["value"],
                      "before_version": snapshot["version"], "after": desired,
                      "ownership_receipt": ownership_receipt, "state": "intent"}
            record["intents"].append(intent)
            self._save(record)
            backend.compare_write(kind, binding, snapshot["version"], desired, self.owner, intent["id"])
            current = backend.read(kind, binding)
            if current["value"] != desired or current["owner"] != self.owner:
                raise RecoveryError("fixture mutation reply cannot be verified")
            intent["state"] = "observed"
            intent["after_version"] = current["version"]
            self._save(record)
            return intent

    def record_attempt(self, attempt_id, input_tokens, output_tokens, price):
        with self._locked() as record:
            self._require_owner(record)
            if any(item["id"] == attempt_id for item in record.get("attempts", [])):
                return
            record.setdefault("attempts", []).append({"id": attempt_id, "status": "pending",
                "input_tokens": input_tokens, "output_tokens": output_tokens, "reserved_spend": price})
            self._save(record)

    def record_outcome(self, attempt_id, complete):
        with self._locked() as record:
            self._require_owner(record)
            attempt = next((item for item in record.get("attempts", []) if item["id"] == attempt_id), None)
            if attempt is None:
                raise RecoveryError("outcome has no journaled admission")
            if attempt["status"] == "pending":
                attempt["status"] = "complete" if complete else "failed"
                self._save(record)

    def recover(self, backend, cleanup_seconds, force=False):
        if (not isinstance(cleanup_seconds, (int, float)) or not math.isfinite(cleanup_seconds)
                or cleanup_seconds <= 0 or cleanup_seconds > 600):
            raise RecoveryError("cleanup must have a finite deadline")
        with self._locked() as record:
            if record["status"] == "clean":
                return record["receipt"]
            if record.get("native_intents") and not hasattr(backend, "recover_native"):
                raise RecoveryError("native journal requires its compatible recovery backend")
            if force and (record["epoch"] != self.epoch or record["status"] != "active"):
                raise RecoveryError("stale owner cannot force fixture cleanup")
            if not force and self.clock() < record["lease_until"]:
                raise RecoveryError("active owner lease has not expired")
            record["epoch"] += 1
            record["status"] = "recovering"
            if hasattr(backend, "resume_restore") and any(intent["kind"] == "secret" for intent in record["intents"]):
                record["version"] = max(2, record["version"])
            for attempt in record.get("attempts", []):
                if attempt["status"] == "pending":
                    attempt["status"] = "unknown"
            record["lease_until"] = self.clock() + cleanup_seconds
            self._save(record)
            if hasattr(backend, "begin_recovery"):
                backend.begin_recovery()
            deadline = self.clock() + cleanup_seconds
            holds = []
            if record.get("native_intents"):
                holds.extend(backend.recover_native(record, self._save))
            intents = backend.recovery_order(record["intents"]) if hasattr(backend, "recovery_order") else reversed(record["intents"])
            for intent in intents:
                for attempt in range(3):
                    if self.clock() >= deadline:
                        break
                    try:
                        if not backend.owns(intent["kind"], intent["binding"],
                                            self.owner, intent["ownership_receipt"]):
                            raise RecoveryError("fixture ownership changed")
                        if hasattr(backend, "resume_restore"):
                            backend.resume_restore(intent["kind"], intent["binding"], self.owner, intent["id"])
                        current = backend.read(intent["kind"], intent["binding"])
                        if current["value"] == intent["before"] and current["version"] == intent["before_version"]:
                            if not backend.verify_restored(intent["kind"], intent["binding"], intent["before"]):
                                raise RecoveryError("unchanged fixture could not be verified")
                            intent["state"] = "unchanged"
                            break
                        if (current["value"] == intent["before"]
                                and backend.verify_restore_receipt(
                                    intent["id"], intent["kind"], intent["binding"],
                                    current["version"], intent["before"], self.owner)
                                and backend.verify_restored(intent["kind"], intent["binding"], intent["before"])):
                            intent["state"] = "restored"
                            break
                        if current["value"] != intent["after"] or (
                                intent.get("after_version") is not None
                                and current["version"] != intent["after_version"]):
                            raise RecoveryError("fixture changed after campaign mutation")
                        backend.compare_restore(intent["kind"], intent["binding"],
                                                current["version"], intent["before"], self.owner,
                                                intent["id"])
                        if not backend.verify_restored(intent["kind"], intent["binding"], intent["before"]):
                            raise RecoveryError("fixture restoration not verified")
                        intent["state"] = "restored"
                        break
                    except RecoveryError as error:
                        holds.append({"kind": intent["kind"], "binding": intent["binding"],
                                      "reason": str(error)})
                        break
                    except (OSError, TimeoutError):
                        if attempt == 2:
                            holds.append({"kind": intent["kind"], "binding": intent["binding"],
                                          "reason": "retry_exhausted"})
                if intent["state"] not in {"unchanged", "restored"} and not any(
                        hold["binding"] == intent["binding"] for hold in holds):
                    holds.append({"kind": intent["kind"], "binding": intent["binding"],
                                  "reason": "cleanup_deadline"})
                self._save(record)
            try:
                if not backend.verify_baseline():
                    holds.append({"reason": "baseline_health_unverified"})
            except (OSError, TimeoutError, RecoveryError):
                holds.append({"reason": "baseline_health_unavailable"})
            record["holds"] = holds
            record["status"] = "hold" if holds else "clean"
            record["receipt"] = {"status": record["status"], "epoch": record["epoch"],
                                 "accounting": ("unknown" if any(
                                     item["status"] == "unknown" for item in record.get("attempts", []))
                                     else "observed"),
                                 "restored": sum(item["state"] in {"unchanged", "restored"}
                                                 for item in record["intents"]) + sum(
                                                     item["state"] == "restored" for item in record.get("native_intents", [])),
                                 "holds": holds, "baseline_healthy": not any(
                                     hold["reason"].startswith("baseline_") for hold in holds)}
            self._save(record)
            return record["receipt"]


class FixtureHTTPBackend:
    """Operator-owned, scoped fixture adapter; never accesses accounting controls."""

    def __init__(self, config):
        import urllib.parse

        parsed = urllib.parse.urlsplit(config["adapter_url"])
        if ((parsed.scheme != "https" and not (
                parsed.scheme == "http" and config.get("local_test") is True
                and parsed.hostname in {"127.0.0.1", "localhost"}
        )) or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise RecoveryError("fixture adapter requires a protected URL")
        token = Path(config["adapter_token_file"])
        if (not token.is_absolute() or token.is_symlink() or not token.is_file()
                or token.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise RecoveryError("fixture adapter requires an owner-only token file")
        self.secret = token.read_text().strip()
        if not self.secret:
            raise RecoveryError("fixture adapter token is empty")
        self.url = config["adapter_url"].rstrip("/")
        self.expected = {"target": config["target"], "revision": config["revision"],
                         "account": config["approved_account"], "role": config["approved_role"]}
        self.deadline = None

    def _send(self, action, payload):
        import urllib.error
        import urllib.request

        class NoRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, new_url):
                return None

        if self.deadline is not None and self.deadline <= time.time():
            raise TimeoutError("fixture cleanup deadline reached")
        timeout = min(2, max(0.01, self.deadline - time.time())) if self.deadline else 2
        request = urllib.request.Request(
            self.url + "/v1/fixture/" + action, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.secret,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirects).open(request, timeout=timeout) as response:
                if response.headers.get("Content-Length") and int(response.headers["Content-Length"]) > 65536:
                    raise RecoveryError("fixture adapter response exceeds bound")
                result = json.loads(response.read(65537))
        except (OSError, ValueError) as error:
            raise TimeoutError("fixture adapter unavailable or invalid") from error
        if not isinstance(result, dict) or any(result.get(field) != value for field, value in self.expected.items()):
            raise RecoveryError("fixture adapter target or revision binding changed")
        return result

    def read(self, kind, binding):
        result = self._send("read", {"kind": kind, "binding": binding})
        resource = result.get("resource")
        if (not isinstance(resource, dict) or resource.get("kind") != kind
                or resource.get("binding") != binding or not isinstance(resource.get("version"), int)):
            raise RecoveryError("fixture adapter returned a different resource")
        return resource

    def owns(self, kind, binding, owner, receipt):
        result = self._send("owns", {"kind": kind, "binding": binding,
                                     "owner": owner, "receipt": receipt})
        return result.get("owned") is True

    def compare_write(self, kind, binding, version, desired, owner, intent_id):
        self._send("write", {"kind": kind, "binding": binding, "version": version,
                             "desired": desired, "owner": owner, "intent_id": intent_id})

    def compare_restore(self, kind, binding, version, before, owner, intent_id):
        self._send("restore", {"kind": kind, "binding": binding, "version": version,
                               "before": before, "owner": owner, "intent_id": intent_id})

    def verify_restore_receipt(self, intent_id, kind, binding, version, before, owner):
        result = self._send("receipt", {"intent_id": intent_id, "kind": kind,
                                        "binding": binding, "version": version,
                                        "before": before, "owner": owner})
        return result.get("restored") is True

    def verify_restored(self, kind, binding, before):
        result = self._send("verify", {"kind": kind, "binding": binding, "before": before})
        return result.get("restored") is True

    def verify_baseline(self):
        result = self._send("baseline", {})
        return (result.get("healthy") is True
                and result.get("no_owned_active_clients") is True
                and result.get("normal_user_denials_ok") is True)


def load_recovery_config(path, allow_legacy=False):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
        raise RecoveryError("recovery config requires private permissions")
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RecoveryError("invalid recovery config") from error
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RecoveryError("unsupported recovery config version")
    for field in ("campaign", "owner", "journal_dir", "target", "revision", "approved_account", "approved_role"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise RecoveryError(f"recovery config missing {field}")
    cleanup_seconds = config.get("cleanup_seconds")
    if (isinstance(cleanup_seconds, bool) or not isinstance(cleanup_seconds, (int, float))
            or not math.isfinite(cleanup_seconds) or not 0 < cleanup_seconds <= 600):
        raise RecoveryError("recovery requires a finite cleanup deadline")
    if config.get("backend") == "adp":
        from campaign_adp import validate_adp_config
        validate_adp_config(config)
    elif config.get("backend") == "adp-native":
        from campaign_native import validate_native_config
        validate_native_config(config)
    elif config.get("local_test") is True or allow_legacy:
        for field in ("adapter_url", "adapter_token_file"):
            if not isinstance(config.get(field), str) or not config[field].strip():
                raise RecoveryError(f"recovery config missing {field}")
    else:
        raise RecoveryError("live recovery requires the supported ADP fixture backend")
    return config


def fixture_backend(config, cleanup=False):
    if config.get("backend") == "adp-native":
        from campaign_native import NativeFixtureBackend
        return NativeFixtureBackend(config)
    if config.get("backend") == "adp":
        from campaign_adp import ADPFixtureBackend
        return ADPFixtureBackend(config)
    if config.get("local_test") is True or cleanup:
        return FixtureHTTPBackend(config)
    raise RecoveryError("synthetic fixture adapter is restricted to local tests")


def resume_cleanup(path):
    config = load_recovery_config(path, allow_legacy=True)
    cleanup_seconds = config["cleanup_seconds"]
    backend = fixture_backend(config, cleanup=True)
    journal = RecoveryJournal(config["journal_dir"], config["campaign"], config["owner"], create=False)
    journal.bind_recovery(config)
    backend.deadline = time.time() + cleanup_seconds
    receipt = journal.recover(backend, cleanup_seconds)
    return 0 if receipt["status"] == "clean" and receipt["accounting"] == "observed" else 5
