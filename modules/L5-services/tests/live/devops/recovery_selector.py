"""Run isolated recovery through first-party actions and selected readback endpoints."""

import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ingress_policy import read_ingress_policy
from journey_driver import private_path
from manual_ui import ManualDevops, validate_manual
from installed_journeys import release_readback_budget, release_readback_tokens
from installed_recovery import InstalledRecoveryWork, validate_installed_recovery
from installed_routes import InstalledRecoveryRoutes, cleanup_reserve, validate_installed_routes
from selector_evidence import ReadbackRecorder
from qualification import ROOT, load_manifest, verify_release
from recovery import DENIALS, validate_recovery, verify_recovery
from selector import (
    HTTPReader,
    PolicyIngressReader,
    authorize,
    save_evidence,
    selected_ingress_policy,
)

ACTIONS = ("revoke", "inject_fault", "submit", "restart_worker", "cleanup_routes")
READBACKS = ("routes", "route_owners", "route_inventory", "spaces", "sessions",
             "provider_calls", "work")


def authorize_recovery(manifest, source_sha, evidence_dir, *, now=None):
    directory = authorize(manifest, source_sha, evidence_dir, scenario="recovery", now=now)
    recovery = validate_recovery(manifest)
    actions = recovery.get("action_channels", {})
    readbacks = recovery.get("readback_channels", {})
    installed = "installed_work" in recovery
    expected_actions = set(ACTIONS) - ({"submit"} if installed else set())
    expected_reads = set(READBACKS) - ({"work", "spaces"} if installed else set())
    if "installed_routes" in recovery:
        validate_installed_routes(manifest)
        expected_actions.remove("cleanup_routes")
        expected_reads -= {"routes", "route_owners", "route_inventory"}
    if (not isinstance(actions, dict) or set(actions) != expected_actions
            or not isinstance(readbacks, dict) or set(readbacks) != expected_reads):
        raise ValueError("recovery requires explicit action and independent readback channels")
    tokens = release_readback_tokens(manifest)
    release_token_count = len(tokens)
    for channels in (actions, readbacks):
        for name, channel in channels.items():
            if not isinstance(channel, dict) or channel.get("source") != name:
                raise ValueError("recovery channel does not name its authorized source")
            address = urlsplit(channel.get("url", ""))
            token_env = channel.get("token_env", "")
            if (address.scheme != "https" or not address.hostname or address.username or address.password
                    or address.query or address.fragment or not re.fullmatch(r"[A-Z][A-Z0-9_]+", token_env)
                    or not os.environ.get(token_env)):
                raise ValueError("recovery channels require private HTTPS endpoints and credentials")
            tokens.add(os.environ[token_env])
    if len(tokens) != len(expected_actions) + len(expected_reads) + release_token_count:
        raise ValueError("action, readback and release credentials must be mutually independent")
    if installed:
        selected = validate_installed_recovery(manifest)
        for field in ("read_token_env", "submit_token_env"):
            if os.environ[selected[field]] in tokens:
                raise ValueError("installed recovery needs independent action and readback credentials")
            tokens.add(os.environ[selected[field]])
    for user in recovery["users"]:
        if (not re.fullmatch(r"[A-Z][A-Z0-9_]+", user["credential_ref"])
                or not os.environ.get(user["credential_ref"])
                or os.environ[user["credential_ref"]] in tokens):
            raise ValueError("two real users need separate private session credentials")
    if os.environ[recovery["users"][0]["credential_ref"]] == os.environ[recovery["users"][1]["credential_ref"]]:
        raise ValueError("two users cannot share a session credential")
    manual = validate_manual(manifest)
    browser = recovery.get("browser", {})
    journey = manifest.get("journey_driver", {})
    if not isinstance(browser, dict) or set(browser) != {
            "gateway_url", "configuration_revision", "provider_hostname"} | ({"storage_states"} if manual is None else set()):
        raise ValueError("recovery requires two private first-party browser sessions")
    gateway = urlsplit(browser["gateway_url"])
    scope = recovery["scope"]
    launch_path = (f"/api/v1/products/{scope['product_id']}/projects/{scope['project_id']}"
                   f"/services/devops/bindings/{recovery['binding_id']}/browser-launch")
    states = browser.get("storage_states", {})
    if (gateway.scheme != "https" or gateway.hostname != manifest["ingress_policy"]["frontend_hostname"]
            or gateway.username or gateway.password or gateway.port not in (None, 443)
            or gateway.path not in ("", "/") or gateway.query or gateway.fragment
            or browser["gateway_url"].rstrip("/") != journey.get("gateway_url", "").rstrip("/")
            or browser["provider_hostname"] != journey.get("provider_hostname")
            or manifest["ingress_policy"]["launch_path"] != launch_path
            or type(browser["configuration_revision"]) is not int or browser["configuration_revision"] < 1
            or (manual is None and (not isinstance(states, dict) or set(states) != {user["id"] for user in recovery["users"]}))
            or any(not re.fullmatch(r"[\w-]{1,128}", scope.get(field, ""))
                   for field in ("product_id", "project_id"))
            or not re.fullmatch(r"[\w-]{1,128}", recovery["binding_id"])):
        raise ValueError("recovery browser does not match the selected first-party ingress")
    if manual is not None:
        return directory
    try:
        paths = [private_path(states[user["id"]]) for user in recovery["users"]]
    except (OSError, TypeError, ValueError):
        raise ValueError("recovery needs two protected browser session files") from None
    if len(set(paths)) != 2:
        raise ValueError("recovery browser sessions must be independent")
    return directory


class HTTPRecoveryTarget:
    def __init__(self, manifest, directory, *, deadline, max_requests):
        import httpx
        self.client = httpx.Client(timeout=15, follow_redirects=False)
        self.httpx = httpx
        self.actions = manifest["recovery"]["action_channels"]
        self.readbacks = manifest["recovery"]["readback_channels"]
        self.run_id = manifest["authorization"]["run_id"]
        self.target = manifest["target_ref"]
        self.deadline = deadline
        self.max_requests = max_requests
        self.calls = 0
        self.cleanup_grace = manifest["recovery"]["bounds"]["cleanup_grace_seconds"]
        self.route_cleanup_reserve = cleanup_reserve(manifest) if "installed_routes" in manifest["recovery"] else 4
        self.cleanup_reserve = self.route_cleanup_reserve + 1
        self.cleanup_phase = False
        self.binding_id = manifest["recovery"]["binding_id"]
        self.recovery = manifest["recovery"]
        self.manifest, self.directory = manifest, directory
        self.installed_work = InstalledRecoveryWork(self, manifest) if "installed_work" in self.recovery else None
        self.installed_routes = InstalledRecoveryRoutes(self, manifest) if "installed_routes" in self.recovery else None
        self.journal = os.open(directory / f"devops-{self.run_id}-recovery.jsonl",
                               os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def close(self):
        self.client.close()
        os.close(self.journal)

    def _journal(self, operation, phase, receipt, *, evidence=None):
        entry = {"operation": operation, "phase": phase, "receipt": receipt,
                 "run_id": self.run_id, "at": datetime.now(UTC).isoformat()}
        if evidence is not None:
            entry["evidence"] = evidence
        content = (json.dumps(entry, sort_keys=True) + "\n").encode()
        offset = 0
        while offset < len(content):
            written = os.write(self.journal, content[offset:])
            if written <= 0:
                raise OSError("recovery journal write did not advance")
            offset += written
        os.fsync(self.journal)

    def _request(self, channel, body, *, action=False, token_env=None, receipt=None):
        self.calls += 1
        if action and channel == "cleanup_routes":
            self.cleanup_phase = True
        remaining = (self.deadline + (self.cleanup_grace if self.cleanup_phase else 0)) - time.monotonic()
        if (remaining <= 0 or self.calls > self.max_requests
                or (not self.cleanup_phase and self.calls >= self.max_requests - self.cleanup_reserve)):
            raise ValueError("recovery request exceeded authorized deadline or reserved cleanup bound")
        selected = (self.actions if action else self.readbacks)[channel]
        token = (None if token_env == "NO_AUTH" else "invalid"
                 if token_env == "INVALID_AUTH" else os.environ[token_env or selected["token_env"]])
        payload = {"target_ref": self.target, "qualification_run_id": self.run_id, **body}
        if action:
            self._journal(channel, "prepared", receipt)
        try:
            with self.client.stream("POST" if action else "GET", selected["url"],
                                    json=payload if action else None,
                                    params=None if action else {"selection": json.dumps(payload, sort_keys=True)},
                                    headers={**({"Authorization": "Bearer " + token} if token else {}),
                                             **({"Idempotency-Key": receipt} if receipt else {})},
                                    timeout=min(remaining, 15)) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1024 * 1024:
                        raise ValueError("recovery readback is oversized")
                status = response.status_code
                if status == 204 and not content:
                    observation = {}
                else:
                    observation = json.loads(content)
        except self.httpx.TimeoutException as error:
            if action:
                self._journal(channel, "uncertain", receipt)
                raise TimeoutError("uncertain recovery action; reconcile private journal") from error
            raise
        if (not isinstance(observation, dict)
                or ((not action or status != 204) and
                    (observation.get("target_ref") != self.target
                     or observation.get("qualification_run_id") != self.run_id))
                or status >= 500
                or 300 <= status < 400):
            raise ValueError("recovery response is unavailable, redirected or foreign")
        if action:
            self._journal(channel, "acknowledged", receipt)
        return status, observation.get("observation", {})

    def _read(self, channel, **selection):
        status, data = self._request(channel, selection)
        if status != 200:
            raise ValueError("independent recovery readback unavailable")
        return data

    def _act(self, channel, selection, *, credential=None, receipt=None):
        status, data = self._request(channel, selection, action=True, token_env=credential, receipt=receipt)
        return status, data

    def launch(self, credential, scope, kind, attempt_ref):
        selected = self.recovery["scope"]
        if (credential not in {user["credential_ref"] for user in self.recovery["users"]}
                or kind not in {"allowed", *DENIALS}
                or scope != (self.recovery["foreign_scope"] if kind == "foreign" else selected)):
            raise ValueError("browser launch must use the selected recovery scope and user")
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls >= self.max_requests - self.cleanup_reserve:
            raise ValueError("recovery browser launch exceeded the authorized bound")
        user = next(user for user in self.recovery["users"] if user["credential_ref"] == credential)
        browser = self.recovery["browser"]
        if self.manifest.get("manual_ui") is not None:
            return self.manual_launch(user, scope, kind, attempt_ref, remaining)
        if os.environ.get("L5_BROWSER_AUTOMATION", "disabled") != "allowed":
            raise ValueError("DevOps recovery requires human SSO observations while browser automation is disabled")
        payload = {"gateway_url": browser["gateway_url"], "scope": scope,
                   "binding_id": self.binding_id, "configuration_revision": browser["configuration_revision"],
                   "provider_hostname": browser["provider_hostname"], "kind": kind,
                   "storage_state": (None if kind in {"missing", "invalid"} else
                                     browser["storage_states"][user["id"]]),
                   "token": ("invalid" if kind == "invalid" else
                             None if kind in {"missing", "allowed"} else os.environ[credential])}
        self._journal("launch", "prepared", attempt_ref)
        try:
            result = subprocess.run(["node", str(Path(__file__).with_name("browser_recovery.mjs"))],
                                    input=json.dumps(payload).encode(), capture_output=True, check=False,
                                    cwd=ROOT / "modules/gateway/frontend", timeout=min(remaining, 45))
            if result.returncode or len(result.stdout) > 64:
                raise ValueError("first-party browser launch unavailable")
            observed = json.loads(result.stdout)
            if (not isinstance(observed, dict) or set(observed) != {"status"}
                    or type(observed["status"]) is not int or observed["status"] not in {200, 401, 403, 404, 429}):
                raise ValueError("first-party browser launch did not return a bounded status")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self._journal("launch", "uncertain", attempt_ref)
            raise ValueError("first-party browser launch uncertain; reconcile privately") from None
        self._journal("launch", "acknowledged", attempt_ref)
        return {"status": observed["status"], **({"scope": scope} if observed["status"] == 200 else {})}

    def manual_launch(self, user, scope, kind, attempt_ref, remaining):
        from urllib.parse import quote
        browser = self.recovery["browser"]
        path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/devops")
        if kind == "allowed":
            # Human arrival already carries a fresh protected launch chain. Do
            # not federate again merely to reconstruct a test response.
            with self.client.stream("GET", browser["gateway_url"].rstrip("/") + path + "/configuration",
                                    headers={"Authorization": "Bearer " + os.environ[user["credential_ref"]]},
                                    timeout=min(remaining, 15)) as response:
                if response.status_code != 200:
                    raise ValueError("DevOps human scope readback unavailable")
                content = b""
                for chunk in response.iter_bytes():
                    content += chunk
                    if len(content) > 65536:
                        raise ValueError("DevOps human scope readback oversized")
            ManualDevops(self.manifest, self.directory, deadline=self.deadline).arrival(json.loads(content), user["id"])
            return {"status": 200, "scope": scope}
        # Negative ingress checks remain actual canonical requests with a
        # separate provider-call oracle before and after each attempt.
        token = None if kind == "missing" else "invalid" if kind == "invalid" else os.environ[user["credential_ref"]]
        self._journal("launch", "prepared", attempt_ref)
        try:
            with self.client.stream("POST", browser["gateway_url"].rstrip("/") + path + "/bindings/"
                                    + quote(self.binding_id, safe="") + "/browser-launch",
                                    headers={**({"Authorization": "Bearer " + token} if token else {}),
                                             "X-ADP-Qualification-Attempt": attempt_ref},
                                    json={"configuration_revision": browser["configuration_revision"]},
                                    timeout=min(remaining, 15)) as response:
                # Never expose or follow a returned federation URL. A success
                # on a denial probe is a failure, even if a provider call happened.
                if response.status_code != DENIALS[kind]:
                    raise ValueError("DevOps denial probe did not return the required status")
                status = response.status_code
        except Exception:
            self._journal("launch", "uncertain", attempt_ref)
            raise
        self._journal("launch", "acknowledged", attempt_ref)
        return {"status": status}

    def logout(self, credential):
        if credential not in {user["credential_ref"] for user in self.recovery["users"]}:
            raise ValueError("logout requires a selected recovery user")
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls >= self.max_requests - self.cleanup_reserve:
            raise ValueError("logout exceeded the authorized deadline or reserved cleanup bound")
        self._journal("logout", "prepared", None)
        try:
            address = self.recovery["browser"]["gateway_url"].rstrip("/") + "/api/auth/logout"
            with self.client.stream("POST", address,
                                    headers={"Authorization": "Bearer " + os.environ[credential]},
                                    timeout=min(remaining, 15)) as response:
                if response.status_code != 200:
                    raise ValueError("Gateway logout was not confirmed")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1024 * 1024:
                        raise ValueError("Gateway logout response is oversized")
                if json.loads(content) != {"message": "Logout successful"}:
                    raise ValueError("Gateway logout response does not match its contract")
        except (self.httpx.HTTPError, ValueError):
            self._journal("logout", "uncertain", None)
            raise ValueError("Gateway logout unconfirmed; reconcile private journal") from None
        self._journal("logout", "acknowledged", None)

    def revoke(self, credential):
        if self._act("revoke", {"binding_id": self.binding_id}, credential=credential)[0] != 204:
            raise ValueError("membership revocation was not confirmed")

    def inject_fault(self, fault, scope, receipt):
        if self._act("inject_fault", {"fault": fault, "scope": scope}, receipt=receipt)[0] != 204:
            raise ValueError("scoped fault was not armed")

    def submit(self, scope, fault, receipt):
        if self.installed_work is not None:
            return self.installed_work.submit(scope, fault, receipt)
        if self._act("submit", {"fault": fault, "scope": scope}, receipt=receipt)[0] not in {200, 202}:
            raise ValueError("native work was not submitted")

    def restart_worker(self, fault):
        if self._act("restart_worker", {"fault": fault})[0] != 204:
            raise ValueError("isolated worker restart was not confirmed")

    def cleanup_routes(self, scope, routes):
        if self.installed_routes is not None:
            return self.installed_routes.cleanup_routes(scope, routes)
        if self._act("cleanup_routes", {"scope": scope, "routes": routes})[0] != 204:
            raise ValueError("owned route cleanup was not confirmed")

    def read_routes(self, scope, routes):
        if self.installed_routes is not None:
            return self.installed_routes.read_routes(scope, routes)
        return self._read("routes", scope=scope, routes=routes)

    def read_route_owners(self, scope, routes):
        if self.installed_routes is not None:
            return self.installed_routes.read_route_owners(scope, routes)
        return self._read("route_owners", scope=scope, routes=routes)

    def read_route_inventory(self, scope):
        if self.installed_routes is not None:
            return self.installed_routes.read_route_inventory(scope)
        return self._read("route_inventory", scope=scope)

    def read_route_snapshot(self, scope, *, ownership=False):
        if self.installed_routes is not None:
            return self.installed_routes.read_route_snapshot(scope)
        return {"retained": self.read_routes(scope, self.recovery["retained_routes"]),
                "owned": self.read_routes(scope, self.recovery["owned_routes"]),
                "owners": self.read_route_owners(scope, self.recovery["owned_routes"]) if ownership else {},
                "inventory": self.read_route_inventory(scope)}

    def read_spaces(self, scope):
        if self.installed_work is not None:
            return self.installed_work.read_spaces(scope)
        return self._read("spaces", scope=scope)

    def read_sessions(self, scope):
        return self._read("sessions", scope=scope)

    def read_provider_calls(self, attempt_ref):
        return self._read("provider_calls", attempt_ref=attempt_ref)

    def read_work(self, scope, receipt):
        if self.installed_work is not None:
            return self.installed_work.read_work(scope, receipt)
        return self._read("work", scope=scope, receipt=receipt)


def qualify_recovery(source_sha, manifest_path, evidence_dir, *, target_factory=HTTPRecoveryTarget,
                     release_reader_factory=HTTPReader, policy_reader=read_ingress_policy, now=None):
    manifest = load_manifest(manifest_path)
    directory = authorize_recovery(manifest, source_sha, evidence_dir, now=now)
    recovery = manifest["recovery"]
    policy = selected_ingress_policy(manifest, deadline=time.monotonic() + recovery["bounds"]["deadline_seconds"],
                                     max_requests=recovery["bounds"]["max_requests"], policy_reader=policy_reader)
    reader = release_reader_factory(manifest, deadline=time.monotonic() + recovery["bounds"]["deadline_seconds"],
                                    max_requests=release_readback_budget(manifest))
    recorded = ReadbackRecorder(PolicyIngressReader(reader, policy, manifest["target_ref"]))
    try:
        verify_release(manifest, recorded, now=now,
                       qualification_run_id=manifest["authorization"]["run_id"])
    finally:
        reader.close()
    target = target_factory(manifest, directory,
                            deadline=time.monotonic() + recovery["bounds"]["deadline_seconds"],
                            max_requests=recovery["bounds"]["max_requests"])
    try:
        result = verify_recovery(manifest, target, ReadbackRecorder(target, recorded.records))
        result["authorization_run_id"] = manifest["authorization"]["run_id"]
        save_evidence(directory, result["authorization_run_id"], "recovery", result, recorded.records)
        return result["authorization_run_id"]
    finally:
        target.close()
