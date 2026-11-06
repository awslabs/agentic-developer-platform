"""Scoped ADP operations for fixtures; no ambient credentials or fixture service."""

import hashlib
import json
import os
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from campaign_recovery import RecoveryError


def private_file(value):
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o077):
        raise RecoveryError("ADP fixture input must be an owner-only regular file")
    return path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_selected(command, *, input, timeout):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True,
                               env={**os.environ, "AWS_MAX_ATTEMPTS": "1"})
    try:
        stdout, _ = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)


def validate_adp_config(config):
    for field in ("gateway_admin_token_file", "aws_connection", "aws_region", "user_pool_id", "health_file", "health_owner"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise RecoveryError(f"ADP fixture configuration missing {field}")
    private_file(config["gateway_admin_token_file"])
    private_file(config["health_file"])
    parsed = urllib.parse.urlsplit(config["target"])
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RecoveryError("ADP fixture target must use HTTPS without credentials")
    fixtures = config.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise RecoveryError("ADP fixture inventory is required")
    seen = set()
    identities = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict) or fixture.get("kind") not in {"identity", "quota", "secret"}:
            raise RecoveryError("unsupported ADP fixture kind")
        for field in ("binding", "ownership_receipt"):
            if not isinstance(fixture.get(field), str) or not fixture[field].strip():
                raise RecoveryError("exact fixture binding and ownership receipt required")
        key = fixture["kind"], fixture["binding"]
        if key in seen:
            raise RecoveryError("duplicate ADP fixture")
        seen.add(key)
        if fixture["kind"] == "identity":
            if (fixture.get("desired") != {"enabled": True}
                    or not all(isinstance(fixture.get(field), str) and fixture[field]
                               for field in ("username", "tenant", "root_human"))):
                raise RecoveryError("identity must name an owned Cognito subject and membership")
            identities[fixture["binding"]] = fixture
    for fixture in fixtures:
        kind = fixture["kind"]
        subject = fixture["binding"] if kind == "quota" else fixture.get("identity")
        if kind != "identity" and subject not in identities:
            raise RecoveryError("quota and credential fixtures must bind an owned identity")
        if kind == "quota":
            desired = fixture.get("desired")
            if (not isinstance(desired, dict) or set(desired) != {"rpm", "tpm", "concurrent_requests"}
                    or any(value is not None and (type(value) is not int or not 1 <= value <= 2147483647)
                           for value in desired.values())):
                raise RecoveryError("quota must contain exact finite ADP dimensions")
        if kind == "secret":
            path = Path(fixture["binding"])
            if (not path.is_absolute() or path.is_symlink() or not path.parent.is_dir()
                    or path.parent.is_symlink() or path.parent.stat().st_mode & 0o077
                    or fixture.get("desired") != {"reference": str(path)}
                    or not all(isinstance(fixture.get(field), str) and fixture[field]
                               for field in ("password_secret_id", "client_id"))):
                raise RecoveryError("credential fixture requires a private cache and explicit Cognito inputs")
    return config


class ADPFixtureBackend:
    retryable_http_statuses = frozenset()

    def __init__(self, config):
        self.config = validate_adp_config(config)
        self.fixtures = {(item["kind"], item["binding"]): item for item in config["fixtures"]}
        self.deadline = None
        self.recovery_started = None
        self.path = Path(config["journal_dir"]) / (config["campaign"] + ".operations.json")
        self.state = json.loads(private_file(str(self.path)).read_text()) if self.path.exists() else {}

    def _timeout(self):
        remaining = self.deadline - time.time() if self.deadline else 10
        if remaining <= 0:
            raise TimeoutError("fixture deadline reached")
        return min(10, remaining)

    def _save(self):
        temporary = self.path.with_suffix("." + uuid.uuid4().hex)
        with temporary.open("x") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(self.state, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        sync_directory(self.path.parent)

    def _aws(self, service, operation, payload):
        command = ["adp-cred", "assume", "--service", "aws", "--label", self.config["aws_connection"],
                   "--exec", "aws", service, operation, "--region", self.config["aws_region"],
                   "--output", "json", "--no-cli-pager", "--cli-input-json", "file:///dev/stdin"]
        try:
            result = run_selected(command, input=json.dumps(payload), timeout=self._timeout())
            if result.returncode:
                raise RecoveryError("selected AWS operation failed; inspect protected operator evidence")
            return json.loads(result.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise TimeoutError("selected AWS operation has an unknown outcome") from None

    def _verify_aws(self):
        identity = self._aws("sts", "get-caller-identity", {})
        if (identity.get("Account") != self.config["approved_account"]
                or identity.get("Arn", "").rsplit("/", 1)[0] != self.config["approved_role"]
                or ":assumed-role/" not in identity.get("Arn", "")):
            raise RecoveryError("selected AWS account or exact assumed role differs")

    def _cognito(self, operation, fixture):
        self._verify_aws()
        return self._aws("cognito-idp", operation, {"UserPoolId": self.config["user_pool_id"],
                                                  "Username": fixture["username"]})

    def _http(self, method, route, payload=None, *, token=None):
        from campaign_guard import NoRedirects

        token = token if token is not None else private_file(self.config["gateway_admin_token_file"]).read_text().strip()
        if not token:
            raise RecoveryError("gateway recovery credential is empty")
        request = urllib.request.Request(self.config["target"].rstrip("/") + route,
            data=json.dumps(payload).encode() if payload is not None else None, method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(NoRedirects, urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self._timeout()) as response:
                content = bytearray()
                while not response.isclosed():
                    response.fp.raw._sock.settimeout(self._timeout())
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > 1048576:
                        raise RecoveryError("ADP fixture response exceeds bound")
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise RecoveryError("ADP fixture response is not an object")
                return result
        except urllib.error.HTTPError as error:
            if error.code in self.retryable_http_statuses:
                raise TimeoutError("fixture operation outcome unavailable; reconcile its operation ID") from None
            if error.code == 404 and method == "GET" and ("/campaign/" in route or "/operations/" in route):
                return None
            if route == "/admin/campaign-identity" and error.code in (401, 403):
                try:
                    result = json.loads(error.read(65537))
                    detail = result.get("detail", {}) if isinstance(result, dict) else {}
                except (ValueError, OSError):
                    detail = {}
                if isinstance(detail, dict) and (error.code, detail.get("error")) in {
                        (403, "tenant_membership_revoked"), (401, "token_expired")}:
                    return {"unusable": True}
            raise RecoveryError("ADP fixture request refused; no unsafe retry") from None
        except (OSError, ValueError):
            raise TimeoutError("ADP fixture response unavailable") from None

    def _fixture(self, kind, binding):
        try:
            return self.fixtures[kind, binding]
        except KeyError:
            raise RecoveryError("resource is outside the exact fixture inventory") from None

    def _quota_route(self, binding):
        identity = self._fixture("identity", binding)
        return ("/admin/organizations/" + urllib.parse.quote(identity["tenant"], safe="")
                + "/ratelimit-cli/user/" + urllib.parse.quote(binding, safe=""))

    def _identity_route(self, binding):
        fixture = self._fixture("identity", binding)
        return ("/admin/organizations/" + urllib.parse.quote(fixture["tenant"], safe="")
                + "/campaign-identities/" + urllib.parse.quote(binding, safe=""))

    def recovery_order(self, intents):
        return sorted(reversed(intents), key=lambda intent: {"quota": 0, "identity": 1, "secret": 2}[intent["kind"]])

    def _user(self, binding):
        fixture = self._fixture("identity", binding)
        user = self._cognito("admin-get-user", fixture)
        attributes = {item["Name"]: item["Value"] for item in user["UserAttributes"]}
        if user.get("Username") != fixture["username"] or attributes.get("sub") != binding:
            raise RecoveryError("owned Cognito subject was replaced")
        return user

    def _secret_snapshot(self, binding, name=None, directory=None):
        try:
            descriptor = os.open(name if name is not None else binding,
                                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return {"value": None, "version": None, "owner": self.config["owner"]}, None
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
                raise RecoveryError("credential cache must be an owner-only regular file")
            content = source.read()
            after = os.fstat(source.fileno())
            if (details.st_size, details.st_mtime_ns, details.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RecoveryError("credential cache changed while reading")
        return {"value": {"reference": binding}, "owner": self.config["owner"],
                "version": digest([details.st_dev, details.st_ino, details.st_mtime_ns,
                                   hashlib.sha256(content).hexdigest()])}, content

    def read(self, kind, binding):
        self._fixture(kind, binding)
        if kind == "quota":
            result = self._http("GET", self._quota_route(binding))
            if (result.get("entity_id") != binding or result.get("entity_type") != "user"
                    or result.get("org_id") != self._fixture("identity", binding)["tenant"]):
                raise RecoveryError("gateway resolved a different quota scope")
            if self._fixture("identity", binding)["root_human"] not in {binding, result.get("canonical_user_id")}:
                raise RecoveryError("gateway resolved a different human root")
            saved = result["saved"]
            value = {key: saved[key] for key in ("rpm", "tpm", "concurrent_requests")} if saved else None
            version = saved["updated_at"] if saved else None
        elif kind == "identity":
            user = self._user(binding)
            if not user.get("Enabled"):
                raise RecoveryError("campaign login must be pre-provisioned enabled; Cognito mutations are not fenced")
            result = self._http("GET", self._identity_route(binding))
            if result.get("subject") != binding or result.get("tenant") != self._fixture(kind, binding)["tenant"]:
                raise RecoveryError("gateway resolved a different membership")
            value, version = result["value"], result["version"]
        else:
            return self._secret_snapshot(binding)[0]
        return {"value": value, "version": version, "owner": self.config["owner"]}

    def owns(self, kind, binding, owner, receipt):
        fixture = self._fixture(kind, binding)
        return owner == self.config["owner"] and receipt == fixture["ownership_receipt"]

    def _quota_operation(self, binding, operation_id, version, desired):
        return self._http("PUT", self._quota_route(binding) + "/campaign/" + operation_id,
                          {"expected_revision": version, "desired": desired})

    def compare_write(self, kind, binding, version, desired, owner, intent_id):
        fixture = self._fixture(kind, binding)
        if owner != self.config["owner"] or desired != fixture["desired"]:
            raise RecoveryError("fixture mutation is outside the approved inventory")
        current = self.read(kind, binding)
        if current["version"] != version:
            raise RecoveryError("fixture changed before setup")
        if kind == "quota" and current["value"] is not None and any(
                value is not None and (type(value) is not int or not 1 <= value <= 2147483647)
                for value in current["value"].values()):
            raise RecoveryError("existing quota cannot be restored through the supported API")
        if kind == "identity" and current["value"] != {"enabled": False}:
            raise RecoveryError("only pre-provisioned revoked dedicated memberships may be enabled")
        if kind == "secret" and current["value"] is not None:
            raise RecoveryError("credential cache must not already exist")
        self.state[intent_id] = {"kind": kind, "binding": binding, "before": current}
        self._save()
        if kind == "quota":
            self._quota_operation(binding, str(uuid.UUID(intent_id)), version, desired)
        elif kind == "identity":
            self._http("PUT", self._identity_route(binding) + "/operations/" + str(uuid.UUID(intent_id)),
                       {"expected_version": version, "enabled": True, "restore_operation_id": None})
        else:
            identity = self._fixture("identity", fixture["identity"])
            self._user(fixture["identity"])
            self._verify_aws()
            secret = self._aws("secretsmanager", "get-secret-value", {"SecretId": fixture["password_secret_id"]})
            credentials = json.loads(secret["SecretString"])
            if credentials.get("username") != identity["username"]:
                raise RecoveryError("password reference names a different owned user")
            self._verify_aws()
            auth = self._aws("cognito-idp", "admin-initiate-auth", {
                "UserPoolId": self.config["user_pool_id"], "ClientId": fixture["client_id"],
                "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
                "AuthParameters": {"USERNAME": identity["username"], "PASSWORD": credentials["password"]}})
            token = auth.get("AuthenticationResult", {}).get("AccessToken")
            if not isinstance(token, str) or not token:
                raise RecoveryError("Cognito did not issue an access token")
            probe = self._http("GET", "/admin/campaign-identity", token=token)
            if probe.get("subject") != fixture["identity"] or probe.get("tenant") != identity["tenant"]:
                raise RecoveryError("issued credential does not authenticate as the exact nonprivileged fixture")
            descriptor = os.open(binding, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(token)
                output.flush()
                os.fsync(output.fileno())
            sync_directory(Path(binding).parent)
        self.state[intent_id]["after"] = self.read(kind, binding)
        self._save()

    def compare_restore(self, kind, binding, version, before, owner, intent_id):
        if owner != self.config["owner"]:
            raise RecoveryError("fixture owner changed")
        current = self.read(kind, binding)
        if current["version"] != version:
            raise RecoveryError("fixture changed before restoration")
        operation = self.state.get(intent_id, {})
        if kind == "quota":
            receipt = self._http("GET", self._quota_route(binding) + "/campaign/" + str(uuid.UUID(intent_id)))
            if not receipt or not receipt["after"] or receipt["after"]["updated_at"] != version:
                raise RecoveryError("quota mutation has no matching authoritative receipt")
            restore_id = str(uuid.uuid5(uuid.UUID(intent_id), "restore"))
            self._quota_operation(binding, restore_id, version, before)
        elif kind == "identity":
            receipt = self._http("GET", self._identity_route(binding) + "/operations/" + str(uuid.UUID(intent_id)))
            if not receipt or receipt["after"]["version"] != version or receipt["before"]["value"] != before:
                raise RecoveryError("membership mutation has no matching authoritative receipt; legacy identity requires operator recovery")
            self._http("PUT", self._identity_route(binding) + "/operations/" + str(uuid.uuid5(uuid.UUID(intent_id), "restore")),
                       {"expected_version": version, "enabled": False, "restore_operation_id": str(uuid.UUID(intent_id))})
        else:
            if operation.get("after") != current:
                raise RecoveryError("fixture mutation outcome is unknown or was changed")
            snapshot, content = self._secret_snapshot(binding)
            if snapshot != current:
                raise RecoveryError("credential cache changed before its probe")
            if not self.credential_unusable(binding, content):
                raise RecoveryError("issued credential remains usable; retain cache and cleanup hold")
            directory = Path(binding).parent / (".campaign-cleanup-" + uuid.uuid4().hex)
            directory.mkdir(mode=0o700)
            details = directory.stat()
            sync_directory(directory.parent)
            operation["cleanup"] = {"directory": str(directory), "device": details.st_dev,
                                    "inode": details.st_ino, "version": version, "phase": "prepared"}
            self._save()
            self._finish_secret_restore(intent_id, credential_verified=True)
            return
        operation["restored"] = self.read(kind, binding)
        self.state[intent_id] = operation
        self._save()

    def resume_restore(self, kind, binding, owner, intent_id):
        operation = self.state.get(intent_id, {})
        if kind == "secret" and "cleanup" in operation:
            if owner != self.config["owner"] or operation.get("kind") != kind or operation.get("binding") != binding:
                raise RecoveryError("credential cleanup claim ownership changed")
            self._fixture(kind, binding)
            self._finish_secret_restore(intent_id)

    def _hold_secret_claim(self, operation, descriptor):
        try:
            os.link("token", operation["binding"], src_dir_fd=descriptor, follow_symlinks=False)
            sync_directory(Path(operation["binding"]).parent)
        except OSError:
            pass
        operation["cleanup"]["phase"] = "hold"
        self._save()
        raise RecoveryError("credential cache changed during cleanup; claim preserved for operator recovery")

    def _finish_secret_restore(self, intent_id, credential_verified=False):
        operation = self.state[intent_id]
        cleanup = operation["cleanup"]
        binding = operation["binding"]
        if cleanup["phase"] == "hold":
            raise RecoveryError("credential cleanup claim requires operator recovery")
        descriptor = os.open(cleanup["directory"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            details = os.fstat(descriptor)
            if ((details.st_dev, details.st_ino) != (cleanup["device"], cleanup["inode"])
                    or details.st_uid != os.geteuid() or details.st_mode & 0o077):
                raise RecoveryError("credential cleanup directory changed")
            if cleanup["phase"] == "prepared":
                try:
                    os.stat("token", dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(binding, "token", dst_dir_fd=descriptor)
                os.fsync(descriptor)
                sync_directory(Path(binding).parent)
            try:
                claimed, content = self._secret_snapshot(binding, "token", descriptor)
            except (OSError, RecoveryError):
                self._hold_secret_claim(operation, descriptor)
            if claimed["value"] is not None:
                if claimed["version"] != cleanup["version"] or cleanup["phase"] == "deleted":
                    self._hold_secret_claim(operation, descriptor)
                if not credential_verified:
                    if not self.credential_unusable(binding, content):
                        raise RecoveryError("claimed credential remains usable; retain claim and cleanup hold")
                cleanup["phase"] = "owned"
                self._save()
                os.unlink("token", dir_fd=descriptor)
            elif cleanup["phase"] == "prepared":
                raise RecoveryError("credential cleanup claim disappeared before ownership verification")
            os.fsync(descriptor)
            cleanup["phase"] = "deleted"
            self._save()
            restored = self.read("secret", binding)
            if restored["value"] is not None:
                raise RecoveryError("credential cache replaced during cleanup; replacement preserved")
            operation["restored"] = restored
            self._save()
        finally:
            os.close(descriptor)

    def verify_restore_receipt(self, intent_id, kind, binding, version, before, owner):
        if owner != self.config["owner"]:
            return False
        if kind == "quota":
            operation_id = str(uuid.uuid5(uuid.UUID(intent_id), "restore"))
            result = self._http("GET", self._quota_route(binding) + "/campaign/" + operation_id)
            return bool(result and result["request"]["desired"] == before
                        and (result["after"]["updated_at"] if result["after"] else None) == version)
        if kind == "identity":
            operation_id = str(uuid.uuid5(uuid.UUID(intent_id), "restore"))
            result = self._http("GET", self._identity_route(binding) + "/operations/" + operation_id)
            return bool(result and result["after"]["value"] == before and result["after"]["version"] == version
                        and result["request"]["restore_operation_id"] == str(uuid.UUID(intent_id)))
        operation = self.state.get(intent_id, {})
        restored = operation.get("restored")
        return restored == {"value": before, "version": version, "owner": owner}

    def credential_unusable(self, binding, content):
        return self._http("GET", "/admin/campaign-identity", token=content.decode().strip()).get("unusable") is True

    def verify_restored(self, kind, binding, before):
        return self.read(kind, binding)["value"] == before

    def begin_recovery(self):
        self.recovery_started = time.time()

    def verify_baseline(self):
        self._verify_aws()
        health = json.loads(private_file(self.config["health_file"]).read_text())
        expected = {key: self.config[key] for key in ("campaign", "target", "revision", "approved_account", "approved_role")}
        expected["owner"] = self.config["health_owner"]
        observed = health.get("observed_at")
        if (any(health.get(key) != value for key, value in expected.items())
                or type(observed) not in (int, float) or not 0 <= time.time() - observed <= 30
                or (self.recovery_started is not None and observed < self.recovery_started)
                or any(health.get(key) is not True for key in (
                    "healthy", "no_owned_active_clients", "normal_user_denials_ok"))):
            return False
        return self._http("GET", "/health/detailed").get("all_healthy") is True
