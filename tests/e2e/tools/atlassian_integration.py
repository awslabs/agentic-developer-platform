"""Local release recovery and run-owned Jira cleanup for the shared Atlassian suite."""

import copy
import fcntl
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from modules.tools.scripts.release_window import verify_manifest


class SetupActionLedger:
    MACHINE_ACTIONS = frozenset({
        "dcr_registration", "identity_provider_setup", "connection_begin", "mcp_configuration",
    })
    HUMAN_ACTIONS = frozenset({"consent", "site_selection", "policy_approval"})
    POLICY_ACTIONS = frozenset({"policy_observation"})

    def __init__(self, path, *, run_id, owner_id, fixture_kind):
        self.path = Path(path)
        self.identity = {"run_id": run_id, "owner_id": owner_id, "fixture_kind": fixture_kind}
        if (any(type(value) is not str or not value or len(value) > 128
                for value in (run_id, owner_id))
                or fixture_kind not in {"fresh", "legacy"}
                or self.path.parent.is_symlink()
                or not self.path.parent.is_dir()
                or self.path.parent.stat().st_mode & 0o077
                or self.path.is_symlink()):
            raise ValueError("A private, run-owned setup ledger is required")

    @contextmanager
    def _locked(self):
        handle = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(handle).st_mode & 0o077:
                raise ValueError("The setup lock must be private")
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("The setup ledger cannot be a symlink")
        try:
            handle = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {**self.identity, "actions": [], "live_status": "unproven"}
        with os.fdopen(handle) as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077 or os.fstat(stream.fileno()).st_size > 65536:
                raise ValueError("The setup ledger must be bounded and private")
            record = json.load(stream)
        if (type(record) is not dict
                or any(record.get(key) != value for key, value in self.identity.items())
                or record.get("live_status") != "unproven"
                or type(record.get("actions")) is not list
                or len(record["actions"]) > 256
                or any(type(action) is not dict
                       or set(action) != {"action", "actor", "status", "evidence_sha256"}
                       or action["action"] not in self.MACHINE_ACTIONS | self.HUMAN_ACTIONS | self.POLICY_ACTIONS
                       or action["actor"] != ("machine" if action["action"] in self.MACHINE_ACTIONS | self.POLICY_ACTIONS else "human")
                       or action["status"] not in {"started", "observed", "blocked", "unknown"}
                       or (action["evidence_sha256"] is not None and
                           (type(action["evidence_sha256"]) is not str or
                            re.fullmatch(r"[0-9a-f]{64}", action["evidence_sha256"]) is None))
                       or (action["status"] == "observed" and action["evidence_sha256"] is None)
                       for action in record["actions"])):
            raise ValueError("A different run or invalid record owns this setup ledger")
        return record

    def state(self):
        with self._locked():
            return self._read()

    def record(self, action, *, actor, status, evidence_sha256=None):
        if (action not in self.MACHINE_ACTIONS | self.HUMAN_ACTIONS | self.POLICY_ACTIONS
                or actor != ("machine" if action in self.MACHINE_ACTIONS | self.POLICY_ACTIONS else "human")
                or status not in {"started", "observed", "blocked", "unknown"}
                or (evidence_sha256 is not None and
                    (type(evidence_sha256) is not str or
                     re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is None))
                or (status == "observed" and evidence_sha256 is None)):
            raise ValueError("Only attributed, redacted setup actions may be recorded")
        with self._locked():
            record = self._read()
            if len(record["actions"]) >= 256:
                raise ValueError("The setup action ledger is full")
            record["actions"].append({
                "action": action, "actor": actor, "status": status,
                "evidence_sha256": evidence_sha256,
            })
            descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".setup-")
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(record, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return record


def live_connect(ledger, *, policy_required):
    if not isinstance(ledger, SetupActionLedger) or type(policy_required) is not bool:
        raise ValueError("A private setup ledger and explicit policy role are required")
    state = ledger.state()
    latest = {action["action"]: action["status"] for action in state["actions"]}
    required = SetupActionLedger.MACHINE_ACTIONS | {"consent", "site_selection"}
    if policy_required:
        required = required | ({"policy_observation"} if "policy_observation" in latest else {"policy_approval"})
    missing = sorted(action for action in required if latest.get(action) != "observed")
    return {
        "scenario": "SCN-01",
        "status": "blocked" if missing else "pass",
        "fixture_kind": state["fixture_kind"],
        "policy_required": policy_required,
        "unverified_actions": missing,
        "live_status": "unproven",
    }


def run_live_connect(ledger, *, policy_required, driver, observer):
    if (not isinstance(ledger, SetupActionLedger) or type(policy_required) is not bool
            or not all(getattr(driver, name, False) is True for name in ("isolated", "authorized", "deployed_target"))
            or getattr(observer, "independent", False) is not True
            or not callable(getattr(driver, "setup", None)) or not callable(getattr(driver, "cleanup", None))
            or not callable(getattr(observer, "read_actions", None)) or not callable(getattr(observer, "cleaned", None))):
        raise ValueError("Protected setup driver and independent browser/provider observer required")
    identity = ledger.identity
    result = {"scenario": "SCN-01", "status": "blocked", "cleanup": "unverified"}
    awaiting_policy = False
    try:
        if not ledger.state()["actions"]:
            ledger.record("connection_begin", actor="machine", status="started")
            driver.setup(**identity, policy_required=policy_required, timeout_seconds=120)
        elif callable(getattr(driver, "resume", None)):
            driver.resume(**identity, policy_required=policy_required, timeout_seconds=120)
        awaiting_policy = policy_required and any(action["action"] == "policy_observation" and action["status"] in {"blocked", "unknown"}
                                                  for action in ledger.state()["actions"])
        observed = observer.read_actions(**identity)
        if not isinstance(observed, list) or not 0 < len(observed) <= 256:
            raise ValueError("Independent setup readback unavailable")
        existing = ledger.state()["actions"]
        for event in observed:
            if (not isinstance(event, dict)
                    or set(event) != {"action", "actor", "status", "evidence_sha256", "source", "run_id", "owner_id"}
                    or event["run_id"] != identity["run_id"] or event["owner_id"] != identity["owner_id"]
                    or event["source"] not in {
                        "dcr_registration": {"identity", "provider"},
                        "identity_provider_setup": {"identity"},
                        "connection_begin": {"gateway"},
                        "mcp_configuration": {"client", "gateway"},
                        "consent": {"browser", "identity"},
                        "site_selection": {"gateway", "provider"},
                        "policy_approval": {"gateway"},
                        "policy_observation": {"provider"},
                    }.get(event["action"], set())):
                raise ValueError("Invalid independent setup observation")
            action = {name: event[name] for name in ("action", "actor", "status", "evidence_sha256")}
            if not existing or action not in existing or action["action"] == "policy_observation":
                if any(previous["action"] == action["action"] and previous["status"] == "observed"
                       for previous in existing) and action["action"] != "policy_observation":
                    raise ValueError("Independent setup readback changed after restart")
                ledger.record(**action)
                existing.append(action)
        result.update(live_connect(ledger, policy_required=policy_required))
        result.pop("live_status")
        if policy_required and any(action["action"] == "policy_observation" for action in existing) and not any(
                event["action"] == "policy_observation" for event in observed):
            result.update(status="blocked", unverified_actions=["policy_observation"])
            awaiting_policy = True
        latest = {action["action"]: action["status"] for action in ledger.state()["actions"]}
        awaiting_policy = (result.get("unverified_actions") == ["policy_observation"]
                           or policy_required and latest.get("policy_observation") in {"blocked", "unknown"})
    except (ValueError, KeyError, TypeError, OSError, TimeoutError, ImportError):
        result["status"] = "blocked"
    finally:
        if awaiting_policy:
            result["cleanup"] = "pending-policy"
        else:
            try:
                driver.cleanup(**identity)
                result["cleanup"] = "confirmed" if observer.cleaned(**identity) is True else "unverified"
            except (ValueError, KeyError, TypeError, OSError, TimeoutError, ImportError):
                result["cleanup"] = "unverified"
    if result["cleanup"] != "confirmed":
        result["status"] = "blocked"
    return result


def client_schema_loading(client, definitions, named_tool, call_result):
    """Report fixture-level MCP evidence without claiming a native model transcript."""
    if client not in {"claude-code", "codex"}:
        raise ValueError("A separate supported client report is required")
    if not isinstance(definitions, list) or not definitions or any(
        not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or not isinstance(tool.get("inputSchema"), dict)
        for tool in definitions
    ):
        raise ValueError("A complete MCP descriptor list is required")
    names = [tool["name"] for tool in definitions]
    if len(names) != len(set(names)) or named_tool not in names:
        raise ValueError("The named tool must occur exactly once in the catalogue")
    descriptor_bytes = len(json.dumps(definitions, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    if descriptor_bytes > 128 * 1024:
        raise ValueError("Unsupported eager MCP descriptor capacity")
    if not isinstance(call_result, dict) or call_result.get("isError") is not False:
        raise ValueError("A successful named MCP tool invocation is required")
    return {
        "scenario": "SCN-03",
        "client": client,
        "status": "pending-installed-client-proof",
        "loading_mode": "bounded-eager-named-tools",
        "mcp_descriptors": {"count": len(definitions), "utf8_bytes": descriptor_bytes, "limit_bytes": 128 * 1024},
        "named_call": {"tool": named_tool, "status": "fixture-pass"},
        "installed_client": {
            "binary_presence": "present-unverified" if shutil.which(
                "claude" if client == "claude-code" else "codex"
            ) else "unavailable",
            "version": None,
            "native_schema_deferral": "unverified",
            "invocation": "not-run",
        },
        "model_facing": {"before": None, "after_selection": None, "after_call": None},
    }


class RecoveryFixture:
    def __init__(
        self,
        directory,
        previous,
        *,
        source,
        account,
        gateway_image,
        facade_image,
        create=False,
    ):
        self.directory = Path(directory)
        self.expected = verify_manifest(
            previous,
            source=source,
            account=account,
            gateway_image=gateway_image,
            facade_image=facade_image,
        )
        if self.expected.get("route_group", {}).get("status") != "legacy":
            raise ValueError(
                "Recovery fixture requires a pinned legacy connector group"
            )
        if create:
            self.directory.mkdir(mode=0o700)
            nonce = uuid.uuid4().hex
            baseline = {
                "owner": nonce,
                "runtime": copy.deepcopy(self.expected),
                "credentials": {"legacy": "legacy-" + nonce, "v2": "binding-" + nonce},
                "pending": [
                    {
                        "id": "operation-" + nonce,
                        "backend_generation": 1,
                        "status": "unknown",
                    }
                ],
                "registrations": 1,
                "effects": {name: 0 for name in self.expected["route_group"]["routes"]},
            }
            self._write("baseline", baseline)
            self._write("state", baseline)
        if (
            self.directory.is_symlink()
            or not self.directory.is_dir()
            or self.directory.stat().st_mode & 0o077
        ):
            raise ValueError("Recovery fixture directory must be private and owned")
        self.baseline = self._read("baseline")
        if self.baseline.get("runtime") != self.expected or self._read("state").get(
            "owner"
        ) != self.baseline.get("owner"):
            raise ValueError("Recovery fixture source or ownership changed")

    @classmethod
    def generate(cls, root, previous, *, source, account, gateway_image, facade_image):
        return cls(
            Path(root) / ("release-recovery-" + uuid.uuid4().hex),
            previous,
            source=source,
            account=account,
            gateway_image=gateway_image,
            facade_image=facade_image,
            create=True,
        )

    def _read(self, name):
        path = self.directory / (name + ".json")
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o077
            or path.stat().st_size > 65536
        ):
            raise ValueError("Recovery fixture state must be a bounded private file")
        return json.loads(path.read_text())

    def _write(self, name, state):
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".release-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / (name + ".json"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def state(self):
        state = self._read("state")
        if state.get("owner") != self.baseline["owner"]:
            raise ValueError("A different fixture owns the recovery state")
        return state

    def readback(self, phase):
        if phase not in {"before", "after"}:
            raise ValueError("Unknown release observation phase")
        return self.state()["runtime"]

    def fence(self):
        state = self.state()
        group = state["runtime"]["route_group"]
        if group["status"] not in {"legacy", "active"}:
            raise ValueError("A fenced generation cannot be fenced twice")
        group.update(status="fenced", generation=group["generation"] + 1)
        self._write("state", state)
        return group["generation"]

    def interrupt(self, connector, digest):
        if connector not in self.expected["route_group"]["routes"] or not isinstance(
            digest, str
        ):
            raise ValueError("Select a fixture connector and proposed digest")
        state = self.state()
        group = state["runtime"]["route_group"]
        if group["status"] == "legacy":
            group.update(status="fenced", generation=group["generation"] + 1)
        elif group["status"] != "fenced":
            raise ValueError("A released group cannot be changed without a new fence")
        group["routes"][connector]["backend"] = "adapter"
        group["routes"][connector]["digest"] = digest
        self._write("state", state)

    def activate(self, candidate, *, source, account, gateway_image, facade_image):
        expected = verify_manifest(
            candidate,
            source=source,
            account=account,
            gateway_image=gateway_image,
            facade_image=facade_image,
        )
        state = self.state()
        current = copy.deepcopy(state["runtime"])
        group = current.pop("route_group", None)
        selected = copy.deepcopy(expected)
        next_group = selected.pop("route_group", None)
        baseline = self.baseline["runtime"]["route_group"]
        if (
            current != selected
            or not isinstance(group, dict)
            or not isinstance(next_group, dict)
            or group.get("status") != "fenced"
            or group.get("group") != baseline["group"]
            or group.get("generation") != baseline["generation"] + 1
            or next_group.get("status") != "active"
            or next_group.get("generation") != group["generation"] + 1
            or next_group.get("routes") != group.get("routes")
            or any(
                state.get(key) != self.baseline[key]
                for key in ("credentials", "pending", "registrations", "effects")
            )
        ):
            raise ValueError(
                "All four prepared routes and retained work must match before activation"
            )
        state["runtime"]["route_group"] = next_group
        self._write("state", state)
        return next_group["generation"]

    def admits(self, generation):
        group = self.state()["runtime"]["route_group"]
        return (
            group["status"] in {"legacy", "active"}
            and type(generation) is int
            and group["generation"] == generation
        )

    def restore(self):
        state = self.state()
        group = state["runtime"]["route_group"]
        if group["status"] != "fenced":
            raise ValueError("Recovery fixture must stay fenced during restoration")
        generation = group["generation"]
        state["runtime"] = copy.deepcopy(self.baseline["runtime"])
        state["runtime"]["route_group"].update(status="fenced", generation=generation)
        self._write("state", state)

    def _previous_state(self, state):
        if any(
            state.get(key) != self.baseline[key]
            for key in ("credentials", "pending", "registrations", "effects")
        ):
            return False
        actual = copy.deepcopy(state["runtime"])
        previous = copy.deepcopy(self.baseline["runtime"])
        group = actual.pop("route_group", None)
        original = previous.pop("route_group")
        return (
            actual == previous
            and isinstance(group, dict)
            and group.get("group") == original["group"]
            and group.get("routes") == original["routes"]
            and type(group.get("generation")) is int
            and group["generation"] >= original["generation"]
        )

    def reopen(self):
        state = self.state()
        group = state["runtime"]["route_group"]
        if group["status"] != "fenced" or not self._previous_state(state):
            raise ValueError("Prior group must be restored and checked while fenced")
        group.update(status="legacy", generation=group["generation"] + 1)
        self._write("state", state)
        return group["generation"]

    def cleanup(self, observed_effects=None):
        state = self.state()
        if (
            not self._previous_state(state)
            or state["runtime"]["route_group"]["status"] != "legacy"
        ):
            return {
                "status": "blocked",
                "reason": "Prior routes or retained authority differ",
            }
        pending = {
            entry["id"] for entry in state["pending"] if entry["status"] == "unknown"
        }
        if (
            not isinstance(observed_effects, dict)
            or set(observed_effects) != pending
            or any(
                type(count) is not int or count not in {0, 1}
                for count in observed_effects.values()
            )
        ):
            return {
                "status": "blocked",
                "reason": "Unknown work needs explicit effect readback",
            }
        for name in ("state", "baseline"):
            path = self.directory / (name + ".json")
            if path.is_symlink():
                raise ValueError("Recovery fixture state cannot be a symlink")
            path.unlink()
        self.directory.rmdir()
        return {
            "status": "cleaned",
            "provider_writes": 0,
            "observed_effects": observed_effects,
        }


def cleanup_recovery(ledger, provider, *, pending_operations, unrelated_issue_id):
    """Resume the original fixture cleanup without accepting an unverified outcome."""
    record = ledger.state()
    if record is None or not isinstance(unrelated_issue_id, str) or not unrelated_issue_id:
        raise ValueError("Original run-owned Jira cleanup and unrelated fixture required")
    site_id = record["site_id"]
    unrelated = provider.read_issue(unrelated_issue_id, site_id=site_id)
    if (not isinstance(unrelated, dict) or unrelated.get("id") != unrelated_issue_id
            or unrelated.get("owner_id") == record["owner_id"]
            and unrelated.get("run_id") == record["run_id"]):
        raise ValueError("Independent unrelated Jira fixture required")
    try:
        result = ledger.cleanup(provider, pending_operations=pending_operations)
    except (OSError, ValueError, TimeoutError):
        result = {"status": "pending"}
    current = None
    try:
        observed = provider.read_issues(site_id=site_id, project_key=record["project_key"],
                                        marker=record["marker"])
        current = ledger.state()
        owned_absent = (type(observed) is dict and observed.get("complete") is True
                        and observed.get("issues") == []
                        and all(provider.read_issue(issue_id, site_id=site_id) is None
                                for issue_id in current["issue_ids"]))
        unrelated_unchanged = provider.read_issue(unrelated_issue_id, site_id=site_id) == unrelated
    except (OSError, ValueError, TimeoutError):
        owned_absent = False
        unrelated_unchanged = False
    confirmed = (result["status"] == "confirmed" and current is not None
                 and current["cleanup_status"] == "confirmed"
                 and owned_absent and unrelated_unchanged)
    cleanup_status = "unverified" if result["status"] == "confirmed" and not confirmed else result["status"]
    return {"scenario": "SCN-04", "status": "pass" if confirmed else "blocked",
            "cleanup": cleanup_status, "owned_absent": owned_absent,
            "unrelated_unchanged": unrelated_unchanged}
