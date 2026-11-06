"""Repository-owned gateway setup orchestration; protected inputs are JSON, not code."""

import json
import hashlib
import os
import re
import stat
import tempfile
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tests.e2e.tools.atlassian_integration import SetupActionLedger


FIELDS = {
    "version",
    "gateway_url",
    "run_id",
    "owner_id",
    "fixture_kind",
    "policy_required",
    "ledger_file",
    "admin_token_file",
    "owner_token_file",
    "consent_file",
    "authorization_file",
    "attempt_file",
    "site_id",
    "observer_token_file",
    "account",
    "region",
    "core_label",
    "core_role_arn",
    "adapter_label",
    "adapter_role_arn",
    "plan_secret_arn",
    "client_secret_arn",
    "provider_name",
    "workload_name",
    "metadata_sha256",
    "subject_schema_sha256",
    "sites_schema_sha256",
    "expected_subject",
}
CONNECTION = "/api/auth/connections/atlassian-mcp"


def private_file(path, *, max_bytes=65536):
    path = Path(path)
    if (
        path.parent.is_symlink()
        or path.parent.stat().st_mode & 0o077
        or path.parent.stat().st_uid != os.getuid()
    ):
        raise ValueError("A bounded, owned, private setup input is required")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o077
            or details.st_uid != os.getuid()
            or details.st_size > max_bytes
        ):
            raise ValueError("A bounded, owned, private setup input is required")
        result = stream.read(max_bytes + 1)
        if len(result) > max_bytes:
            raise ValueError("A bounded, owned, private setup input is required")
        return result


def load_setup(path, *, gateway_url):
    """Reject arbitrary Python bindings and targets different from the selected run."""
    config = json.loads(private_file(path))
    selected = urlsplit(gateway_url)
    if (
        type(config) is not dict
        or set(config) not in (FIELDS, FIELDS | {"policy_observation"})
        or config["version"] != 1
        or config["gateway_url"] != gateway_url
        or selected.scheme != "https"
        or not selected.netloc
        or selected.path not in {"", "/"}
        or selected.query
        or selected.fragment
        or selected.username
        or selected.password
        or config["fixture_kind"] not in {"fresh", "legacy"}
        or type(config["policy_required"]) is not bool
        or any(
            type(config[key]) is not str or not config[key] or len(config[key]) > 128
            for key in ("run_id", "owner_id", "site_id", "expected_subject")
        )
        or any(
            type(config[key]) is not str or not config[key]
            for key in (
                "ledger_file",
                "admin_token_file",
                "owner_token_file",
                "observer_token_file",
                "consent_file",
                "authorization_file",
                "attempt_file",
            )
        )
    ):
        raise ValueError(
            "Protected setup configuration does not match the selected target"
        )
    policy = config.get("policy_observation")
    if policy is not None:
        reads = {
            "get_jira_issue",
            "search_jira",
            "get_confluence_page",
            "search_confluence",
        }
        if (
            type(policy) is not dict
            or set(policy)
            != {"tenant_id", "grant_file", "probes", "expected_results", "checks"}
            or not isinstance(policy["tenant_id"], str)
            or not policy["tenant_id"]
            or type(policy["probes"]) is not dict
            or set(policy["probes"]) != reads
            or type(policy["expected_results"]) is not dict
            or set(policy["expected_results"]) != reads
            or any(
                type(arguments) is not dict
                or arguments.get("cloudId") != config["site_id"]
                for arguments in policy["probes"].values()
            )
            or any(
                type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in policy["expected_results"].values()
            )
            or type(policy["checks"]) is not dict
            or set(policy["checks"]) != {"read", "search"}
        ):
            raise ValueError(
                "Protected policy fixtures must be complete and site-bound"
            )
        private_file(policy["grant_file"])
        for check in policy["checks"].values():
            if (
                type(check) is not dict
                or set(check) != {"path", "expected_result_sha256", "ledger_file"}
                or type(check["expected_result_sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", check["expected_result_sha256"])
                or type(check["path"]) is not str
                or not check["path"].startswith("/ex/")
            ):
                raise ValueError("Protected policy read expectation unavailable")
    if (
        not re.fullmatch(r"[0-9]{12}", config.get("account", ""))
        or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", config.get("region", ""))
        or config.get("core_label") == config.get("adapter_label")
        or any(
            not isinstance(config.get(key), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", config[key])
            for key in ("core_label", "adapter_label", "provider_name", "workload_name")
        )
        or any(
            not isinstance(config.get(key), str)
            or not re.fullmatch(r"[0-9a-f]{64}", config[key])
            for key in (
                "metadata_sha256",
                "subject_schema_sha256",
                "sites_schema_sha256",
            )
        )
        or any(
            not isinstance(config.get(key), str)
            or not re.fullmatch(
                rf"arn:aws:iam::{config['account']}:role/[A-Za-z0-9+=,.@_-]+",
                config[key],
            )
            for key in ("core_role_arn", "adapter_role_arn")
        )
        or config["core_role_arn"] == config["adapter_role_arn"]
        or any(
            not isinstance(config.get(key), str)
            or not re.fullmatch(
                rf"arn:aws:secretsmanager:{config['region']}:{config['account']}:secret:adp/[a-z0-9-]+/connections/atlassian-mcp/"
                + ("config" if key == "plan_secret_arn" else "client")
                + r"-[A-Za-z0-9]{6}",
                config[key],
            )
            for key in ("plan_secret_arn", "client_secret_arn")
        )
    ):
        raise ValueError(
            "Selected AWS connections and pinned provider metadata are required"
        )
    for key in ("admin_token_file", "owner_token_file", "observer_token_file"):
        token = private_file(config[key], max_bytes=16384).strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("A private authenticated human session is required")
    for key in ("consent_file", "authorization_file", "attempt_file"):
        output = Path(config[key])
        if (
            output.is_symlink()
            or output.parent.is_symlink()
            or output.parent.stat().st_mode & 0o077
        ):
            raise ValueError("The browser consent handoff must be private")
    ledger = SetupActionLedger(
        config["ledger_file"],
        run_id=config["run_id"],
        owner_id=config["owner_id"],
        fixture_kind=config["fixture_kind"],
    )
    return {"ledger": ledger, "policy_required": config["policy_required"]}, config


class GatewaySetupDriver:
    """Use the production DCR and human consent APIs; never handle an OAuth client secret."""

    isolated = True
    authorized = True
    deployed_target = True

    def __init__(self, config):
        self.config = config
        self.connection_revision = None

    @contextmanager
    def _exclusive(self):
        handle = os.open(
            self.config["attempt_file"] + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if os.fstat(handle).st_mode & 0o077:
                raise ValueError("The attempt lock must be private")
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _attempt(self):
        path = Path(self.config["attempt_file"])
        if not path.exists() and not path.is_symlink():
            return None
        state = json.loads(private_file(path, max_bytes=8192))
        if (
            type(state) is not dict
            or set(state)
            != {
                "run_id",
                "owner_id",
                "fixture_kind",
                "phase",
                "state",
                "session_uri",
                "authorization_url",
                "connection_id",
                "revision",
            }
            or any(
                state[key] != self.config[key]
                for key in ("run_id", "owner_id", "fixture_kind")
            )
            or state["phase"]
            not in {"starting", "pending", "completed", "selected", "cleaned"}
        ):
            raise ValueError("An unrelated or invalid attempt cannot be reconciled")
        return state

    def _save_attempt(self, state):
        path = Path(self.config["attempt_file"])
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=".atlassian-attempt-"
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _publish_authorization(self, *, run_id, state, authorization_url):
        payload = json.dumps(
            {"run_id": run_id, "state": state, "authorization_url": authorization_url}
        )
        path = Path(self.config["authorization_file"])
        if path.exists() or path.is_symlink():
            if json.loads(private_file(path, max_bytes=8192)) != json.loads(payload):
                raise ValueError("A different browser authorization owns this run")
            return
        handle = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(handle, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def _api(self, method, route, *, admin=False, body=None):
        token_file = self.config["admin_token_file" if admin else "owner_token_file"]
        token = private_file(token_file, max_bytes=16384).strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("Authenticated human session unavailable")
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = client.request(
                    method,
                    self.config["gateway_url"].rstrip("/") + route,
                    headers={"Authorization": "Bearer " + token},
                    json=body,
                )
            if response.status_code not in {200, 201} or len(response.content) > 65536:
                raise ValueError("Gateway setup request refused or unavailable")
            result = response.json()
            if type(result) is not dict:
                raise ValueError("Gateway setup response unavailable")
            return result
        except (httpx.HTTPError, ValueError):
            raise ValueError("Gateway setup request refused or unavailable") from None

    def setup(
        self, *, run_id, owner_id, fixture_kind, policy_required, timeout_seconds
    ):
        with self._exclusive():
            self._setup(
                run_id=run_id,
                owner_id=owner_id,
                fixture_kind=fixture_kind,
                policy_required=policy_required,
                timeout_seconds=timeout_seconds,
            )

    def _setup(
        self, *, run_id, owner_id, fixture_kind, policy_required, timeout_seconds
    ):
        if (run_id, owner_id, fixture_kind, policy_required) != tuple(
            self.config[key]
            for key in ("run_id", "owner_id", "fixture_kind", "policy_required")
        ) or timeout_seconds != 120:
            raise ValueError("A different run or owner attempted setup")
        attempt = self._attempt()
        if attempt is None:
            status = self._api("GET", CONNECTION + "/status")
            if status.get("connection"):
                raise ValueError(
                    "Existing user grant cannot be used or disconnected by this run"
                )
            if status.get("status") == "setup_required":
                self._api(
                    "POST", "/api/admin/connections/atlassian-mcp/prepare", admin=True
                )
                status = self._api("GET", CONNECTION + "/status")
            if status.get("status") not in {"ready", "consent_required"}:
                raise ValueError(
                    "Provider compatibility or tenant policy has not been verified"
                )
            authorization_path = Path(self.config["authorization_file"])
            if authorization_path.exists() or authorization_path.is_symlink():
                raise ValueError(
                    "A previous browser authorization must be reconciled first"
                )
            attempt = {
                "run_id": run_id,
                "owner_id": owner_id,
                "fixture_kind": fixture_kind,
                "phase": "starting",
                "state": None,
                "session_uri": None,
                "authorization_url": None,
                "connection_id": None,
                "revision": None,
            }
            self._save_attempt(attempt)
            started = self._api(
                "POST", CONNECTION + "/start", body={"capability_set": "reads"}
            )
            if not all(
                type(started.get(key)) is str and started[key]
                for key in ("state", "session_uri", "authorization_url")
            ):
                raise ValueError("Identity did not begin owner consent")
            authorization = urlsplit(started["authorization_url"])
            if (
                authorization.scheme != "https"
                or authorization.username
                or authorization.password
                or authorization.fragment
            ):
                raise ValueError(
                    "Identity supplied an invalid browser authorization URL"
                )
            attempt.update(
                phase="pending",
                state=started["state"],
                session_uri=started["session_uri"],
                authorization_url=started["authorization_url"],
            )
            self._save_attempt(attempt)
        if attempt["phase"] == "starting":
            raise ValueError("Identity start is uncertain; do not replay it")
        if attempt["phase"] == "pending":
            self._publish_authorization(
                run_id=run_id,
                state=attempt["state"],
                authorization_url=attempt["authorization_url"],
            )
            self._complete(attempt, timeout_seconds)
        if attempt["phase"] == "completed":
            self._select_site(attempt)
        if attempt["phase"] != "selected":
            raise ValueError("Run-owned setup cannot be resumed after cleanup")
        status = self._api("GET", CONNECTION + "/status")
        binding = status.get("connection") or {}
        if (
            status.get("status")
            not in (
                {"ready", "consent_required", "policy_blocked"}
                if policy_required
                else {"ready", "consent_required"}
            )
            or binding.get("id") != attempt["connection_id"]
            or binding.get("revision") != attempt["revision"]
            or binding.get("selected_site_id") != self.config["site_id"]
        ):
            raise ValueError("Run-owned connection changed before readback")
        self.connection_revision = attempt["revision"]

    def resume(self, **identity):
        self.setup(**identity)

    def _complete(self, attempt, timeout_seconds):
        attempt_id = hashlib.sha256(attempt["state"].encode()).hexdigest()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            completed = self._api("GET", CONNECTION + "/attempts/" + attempt_id)
            if completed.get("status") == "pending":
                time.sleep(1)
                continue
            if completed.get("status") == "observing":
                try:
                    self._api(
                        "POST",
                        CONNECTION + "/complete",
                        body={
                            "state": attempt["state"],
                            "session_uri": attempt["session_uri"],
                        },
                    )
                except ValueError:
                    pass
                time.sleep(1)
                continue
            if completed.get("status") != "completed":
                raise ValueError("Browser consent attempt expired or changed")
            if not isinstance(completed.get("revision"), str) or not isinstance(
                completed.get("id"), str
            ):
                raise ValueError("Gateway did not confirm a new connection revision")
            attempt.update(
                phase="completed",
                connection_id=completed["id"],
                revision=completed["revision"],
            )
            self._save_attempt(attempt)
            return
        raise TimeoutError("Human browser consent was not returned")

    def _select_site(self, attempt):
        binding = self._api("GET", CONNECTION + "/status").get("connection") or {}
        if binding.get("id") != attempt["connection_id"]:
            raise ValueError("A different connection cannot be selected")
        if binding.get("revision") == attempt["revision"]:
            selected = self._api(
                "POST",
                CONNECTION + "/select-site",
                body={
                    "connection_revision": attempt["revision"],
                    "site_id": self.config["site_id"],
                },
            )
            if (
                not isinstance(selected.get("revision"), str)
                or not selected["revision"]
                or selected.get("selected_site_id") != self.config["site_id"]
            ):
                raise ValueError("Site selection was not confirmed")
            attempt.update(phase="selected", revision=selected["revision"])
            self._save_attempt(attempt)
        elif binding.get("selected_site_id") == self.config["site_id"] and binding.get(
            "revision"
        ):
            attempt.update(phase="selected", revision=binding["revision"])
            self._save_attempt(attempt)
        else:
            raise ValueError("Site selection is uncertain or changed")

    def cleanup(self, *, run_id, owner_id, fixture_kind):
        with self._exclusive():
            self._cleanup(run_id=run_id, owner_id=owner_id, fixture_kind=fixture_kind)

    def _cleanup(self, *, run_id, owner_id, fixture_kind):
        if (run_id, owner_id, fixture_kind) != tuple(
            self.config[key] for key in ("run_id", "owner_id", "fixture_kind")
        ):
            raise ValueError("A different run cannot disconnect this owner")
        attempt = self._attempt()
        if attempt is None or attempt["phase"] == "cleaned":
            return
        if attempt["phase"] not in {"selected", "completed"} or not attempt["revision"]:
            raise ValueError(
                "Pending or uncertain consent cannot be blanket-disconnected"
            )
        status = self._api("GET", CONNECTION + "/status")
        binding = status.get("connection") or {}
        if (
            binding.get("id") != attempt["connection_id"]
            or binding.get("revision") != attempt["revision"]
        ):
            raise ValueError("The connection changed before cleanup")
        result = self._api(
            "DELETE", CONNECTION, body={"connection_revision": attempt["revision"]}
        )
        if result.get("status") != "disconnected":
            raise ValueError("Gateway did not disconnect the run-owned connection")
        attempt["phase"] = "cleaned"
        self._save_attempt(attempt)
