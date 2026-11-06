"""Owner-authorized, bounded live readback entry point; never creates a target."""

import fnmatch
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from qualification import MATRIX, ROOT, load_manifest, timestamp, verify_release, verify_release_artifacts
from ingress_policy import read_ingress_policy, validate_policy_scope
from journey_driver import drive_journeys, validate_driver, verify_driven_journeys
from installed_journeys import InstalledJourneyReader, validate_installed_journeys, validate_runs
from selector_evidence import ReadbackRecorder, save_selector_evidence


CHANNELS = ("release", "capabilities", "journeys")


def authorize(manifest, source_sha, evidence_dir, *, scenario="journeys", now=None):
    permission = manifest.get("authorization", {})
    now = now or datetime.now(UTC)
    expires_at = timestamp(permission.get("expires_at"))
    if (permission.get("source_sha") != source_sha or source_sha != manifest["release"]["source_sha"]
            or not isinstance(permission.get("scenarios"), list)
            or scenario not in permission["scenarios"]
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", permission.get("run_id", ""))
            or not now < expires_at <= now + timedelta(hours=24)):
        raise ValueError("current owner authorization must pin this run and reviewed source")
    directory = Path(evidence_dir)
    if (not directory.is_absolute() or directory.is_relative_to(ROOT) or not directory.is_dir()
            or directory.is_symlink() or directory.resolve().is_relative_to(ROOT)
            or directory.stat().st_mode & 0o077):
        raise ValueError("attempt evidence needs a pre-existing private directory outside the checkout")
    limits = manifest.get("limits", {})
    capabilities = {item["id"] for item in json.loads(MATRIX.read_text())["capabilities"]}
    if not isinstance(limits, dict) or set(limits) != capabilities:
        raise ValueError("all committed capabilities need per-capability bounds")
    for limit in limits.values():
        try:
            spend = Decimal(str(limit["max_spend_usd"]))
        except (KeyError, TypeError, InvalidOperation):
            raise ValueError("every native capability needs a positive spend allocation") from None
        if (not spend.is_finite() or spend <= 0
                or type(limit.get("max_requests")) is not int or not 1 <= limit["max_requests"] <= 50
                or type(limit.get("deadline_seconds")) is not int
                or not 30 <= limit["deadline_seconds"] <= 900):
            raise ValueError("every capability needs bounded spend, requests and time")
    channels = manifest.get("readback_channels", {})
    required = set(CHANNELS) - ({"journeys"} if "installed_journeys" in manifest else set())
    if not isinstance(channels, dict) or set(channels) not in (required, required | {"ingress"}):
        raise ValueError("scoped release, capability and journey readbacks are required")
    selected_tokens = []
    for name, channel in channels.items():
        if not isinstance(channel, dict) or channel.get("source") != name:
            raise ValueError("readback channel does not name its independent source")
        parsed = urlsplit(channel.get("url", ""))
        token_env = channel.get("token_env", "")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not re.fullmatch(r"[A-Z][A-Z0-9_]+", token_env)
                or not os.environ.get(token_env)):
            raise ValueError("readbacks require private HTTPS endpoints and independent credentials")
        selected_tokens.append(os.environ[token_env])
    if len(set(selected_tokens)) != len(channels):
        raise ValueError("independent readback channels must not share credentials")
    if "installed_journeys" in manifest:
        validate_installed_journeys(manifest)
    return directory


class HTTPReader:
    def __init__(self, manifest, *, deadline, max_requests):
        import httpx
        self.client = httpx.Client(timeout=15, follow_redirects=False)
        self.manifest = manifest
        self.channels = manifest["readback_channels"]
        self.deadline = deadline
        self.max_requests = max_requests
        self.calls = 0
        self.journey_runs = manifest.get("installed_journeys", {}).get("runs")

    def bind_journeys(self, driven):
        self.journey_runs = validate_runs({entry: result["run_id"] for entry, result in driven.items()})

    def close(self):
        self.client.close()

    def _fetch(self, channel, target):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls > self.max_requests:
            raise ValueError("readback exceeded its authorized request or time bound")
        selected = self.channels[channel]
        with self.client.stream("GET", selected["url"], params={"target_ref": target},
                                headers={"Authorization": "Bearer " + os.environ[selected["token_env"]]},
                                timeout=min(remaining, 15)) as response:
            if response.status_code != 200:
                raise ValueError("independent readback unavailable")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("independent readback oversized")
        body = json.loads(content)
        if not isinstance(body, dict) or body.get("target_ref") != target:
            raise ValueError("readback returned a foreign target")
        return body.get("observation")

    def read_release(self, target):
        return self._fetch("release", target)

    def read_ingress(self, target):
        return self._fetch("ingress", target)

    def read_capabilities(self, target):
        return self._fetch("capabilities", target)

    def read_journeys(self, target):
        if "installed_journeys" in self.manifest:
            if target != self.manifest["target_ref"]:
                raise ValueError("installed journey readback returned a foreign target")
            reader = InstalledJourneyReader(self.manifest, deadline=self.deadline,
                                            max_requests=self.max_requests - self.calls)
            try:
                return reader.read(self.journey_runs)
            finally:
                self.calls += reader.calls
                reader.close()
        return self._fetch("journeys", target)


class PolicyIngressReader:
    def __init__(self, reader, policy, target):
        self.reader = reader
        self.policy = policy
        self.target = target
        self.release = None

    def read_release(self, target):
        self.release = self.reader.read_release(target)
        return self.release

    def read_ingress(self, target):
        if target != self.target or not isinstance(self.release, dict):
            raise ValueError("independent release must precede selected ingress readback")
        return {"browser_launch_route": "gateway", "function_urls": self.policy["function_urls"],
                "public_launchers": self.policy["public_launchers"],
                "source_sha": self.release.get("source_sha"),
                "deployment_id": self.release.get("deployment_id")}

    def read_capabilities(self, target):
        return self.reader.read_capabilities(target)

    def read_journeys(self, target):
        return self.reader.read_journeys(target)

    def close(self):
        self.reader.close()


def selected_ingress_policy(manifest, *, deadline, max_requests, policy_reader=read_ingress_policy):
    scope = validate_policy_scope(manifest)
    policy = policy_reader(manifest, deadline=deadline, max_requests=max_requests)
    distribution = policy.get("distribution") if isinstance(policy, dict) else None
    frontend = urlsplit(manifest.get("journey_driver", {}).get("gateway_url", ""))
    if (frontend.scheme != "https" or frontend.hostname != scope["frontend_hostname"]
            or frontend.username or frontend.password or frontend.port not in (None, 443)
            or frontend.path not in ("", "/") or frontend.query or frontend.fragment
            or not isinstance(policy, dict) or policy.get("function_urls") != []
            or policy.get("public_launchers") != []
            or policy.get("aws_account") != scope["account_id"]
            or policy.get("verified_functions") != sorted(scope["allowed_invoke_statements"])
            or not set(scope["functions"]) <= set(policy["verified_functions"])
            or not isinstance(distribution, dict)
            or distribution.get("distribution_id") != scope["distribution_id"]
            or distribution.get("gateway_origin_id") != scope["gateway_origin_id"]
            or distribution.get("frontend_hostname") != scope["frontend_hostname"]
            or distribution.get("status") != "Deployed"
            or "launch_behavior" not in distribution
            or (distribution.get("launch_behavior") is not None
                and (not isinstance(distribution["launch_behavior"], str)
                     or not fnmatch.fnmatchcase(scope["launch_path"], distribution["launch_behavior"])))):
        raise ValueError("independent deployed ingress policy was not verified")
    return policy


def qualify_journeys(source_sha, manifest_path, evidence_dir, *, reader_factory=HTTPReader,
                     policy_reader=read_ingress_policy, journey_runner=drive_journeys, now=None):
    manifest = load_manifest(manifest_path)
    directory = authorize(manifest, source_sha, evidence_dir, now=now)
    validate_driver(manifest)
    run_id = manifest["authorization"]["run_id"]
    if ((directory / f"devops-{run_id}-journey-actions.jsonl").exists()
            or (directory / f"devops-{run_id}-journeys.json").exists()):
        raise FileExistsError("first-party attempt already exists; reconcile before a new run")
    budget = max(limit["deadline_seconds"] for limit in manifest["limits"].values())
    requests = sum(limit["max_requests"] for limit in manifest["limits"].values())
    deadline = time.monotonic() + budget
    policy = selected_ingress_policy(manifest, deadline=deadline, max_requests=requests,
                                     policy_reader=policy_reader)
    reader = reader_factory(manifest, deadline=deadline, max_requests=requests)
    recorded = ReadbackRecorder(PolicyIngressReader(reader, policy, manifest["target_ref"]))
    try:
        verify_release_artifacts(manifest, recorded, now=now)
        driven = journey_runner(manifest, directory, deadline=deadline)
        if "installed_journeys" in manifest:
            reader.bind_journeys(driven)
        result = verify_release(manifest, recorded,
                                now=now, qualification_run_id=run_id)
        result["native_usage"] = verify_driven_journeys(manifest, result["journeys"], driven)
        result["driven_journeys"] = driven
        result["ingress_policy"] = policy
        result["live_checks"]["sso-gateway-only-ingress"] = {**result["ingress"], "aws_policy": policy}
        result["authorization_run_id"] = manifest["authorization"]["run_id"]
        save_evidence(directory, result["authorization_run_id"], "journeys", result, recorded.records)
        return result["authorization_run_id"]
    finally:
        reader.close()


def save_evidence(directory, run_id, scenario, result, records):
    save_selector_evidence(directory, run_id, scenario, result, records)
