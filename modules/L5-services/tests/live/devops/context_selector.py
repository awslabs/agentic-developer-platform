"""Bounded later-Context action and independent final acceptance selectors."""

import base64
import binascii
import json
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from acceptance import verify_acceptance
from portability import validate_projects, verify_portability
from context import verify_context
from qualification import load_manifest, verify_release
from ingress_policy import read_ingress_policy
from selector import HTTPReader, PolicyIngressReader, authorize, save_evidence, selected_ingress_policy
from installed_journeys import release_readback_budget, release_readback_tokens
from installed_asset import read_installed_asset, validate_installed_asset
from installed_sources import read_installed_source, validate_installed_sources
from installed_portability import read_installed_access, validate_installed_access
from installed_operations import submit_installed_operation, validate_installed_operations
from installed_foreign_probe import probe_installed_foreign, validate_installed_foreign_probe
from installed_configuration import InstalledConfiguration, validate_installed_configuration
from installed_portability_assets import InstalledPortabilityAssets, validate_installed_assets
from installed_native import InstalledNativeReader, validate_installed_native
from selector_evidence import ReadbackRecorder

CONTEXT_READS = ("result", "native_job", "source", "publication", "asset", "receipt",
                 "visibility", "baseline", "later", "usage", "denial")
ACCEPTANCE_READS = ("report", "epic", "check", "case", "cleanup")
PORTABILITY_ACTIONS = ("configure", "run_operation", "run_context", "probe_foreign", "cleanup_owned")
PORTABILITY_READS = ("platform", "product", "assets", "configuration", "access", "native",
                     "context", "foreign_denial", "usage")


def authorize_channels(manifest, source_sha, evidence_dir, scenario, *, now=None):
    directory = authorize(manifest, source_sha, evidence_dir, scenario=scenario, now=now)
    selected = manifest.get(scenario, {})
    actions = selected.get("action_channels", {})
    readbacks = selected.get("readback_channels", {})
    expected_actions = ({"run_later"} if scenario == "context" else
                        set(PORTABILITY_ACTIONS) if scenario == "portability" else set())
    expected_reads = set(CONTEXT_READS if scenario == "context" else
                         PORTABILITY_READS if scenario == "portability" else ACCEPTANCE_READS)
    installed_asset = scenario == "context" and "installed_asset" in selected
    if installed_asset:
        expected_reads.remove("asset")
    installed_sources = scenario == "context" and "installed_sources" in selected
    if installed_sources:
        expected_reads.remove("source")
    installed_access = scenario == "portability" and "installed_access" in selected
    if installed_access:
        expected_reads.remove("access")
    installed_operations = scenario == "portability" and "installed_operations" in selected
    if installed_operations:
        expected_actions.remove("run_operation")
    installed_foreign_probe = scenario == "portability" and "installed_foreign_probe" in selected
    if installed_foreign_probe:
        expected_actions.remove("probe_foreign")
    installed_configuration = scenario == "portability" and "installed_configuration" in selected
    if installed_configuration:
        expected_actions.remove("configure")
        expected_reads.remove("configuration")
    installed_assets = scenario == "portability" and "installed_assets" in selected
    if installed_assets:
        expected_actions.remove("cleanup_owned")
        expected_reads.remove("assets")
    installed_native = scenario == "portability" and "installed_native" in selected
    if installed_native:
        expected_reads.remove("native")
    limits = selected.get("limits", {})
    minimum = 15 if scenario == "context" else 50 if scenario == "portability" else 75
    if (not isinstance(actions, dict) or set(actions) != expected_actions
            or not isinstance(readbacks, dict) or set(readbacks) != expected_reads
            or type(limits.get("max_requests")) is not int
            or not minimum <= limits["max_requests"] <= 100
            or type(limits.get("deadline_seconds")) is not int
            or not 30 <= limits["deadline_seconds"] <= 900
            or (scenario == "portability" and (
                type(limits.get("cleanup_grace_seconds")) is not int
                or not 10 <= limits["cleanup_grace_seconds"] <= 60))):
        raise ValueError("selected scenario needs bounded actions and independent readbacks")
    tokens = release_readback_tokens(manifest)
    release_token_count = len(tokens)
    for channels in (actions, readbacks):
        for name, channel in channels.items():
            if not isinstance(channel, dict) or channel.get("source") != name:
                raise ValueError("independent channel must name its source")
            address = urlsplit(channel.get("url", ""))
            token_env = channel.get("token_env", "")
            if (address.scheme != "https" or not address.hostname or address.username or address.password
                    or address.query or address.fragment or not re.fullmatch(r"[A-Z][A-Z0-9_]+", token_env)
                    or not os.environ.get(token_env)):
                raise ValueError("private HTTPS action and observation credentials required")
            tokens.add(os.environ[token_env])
    if len(tokens) != release_token_count + len(expected_actions) + len(expected_reads):
        raise ValueError("live action, release and readback channels must use distinct credentials")
    if installed_asset:
        validate_installed_asset(manifest)
    if installed_sources:
        validate_installed_sources(manifest)
    if installed_access:
        validate_installed_access(manifest)
    if installed_operations:
        validate_installed_operations(manifest)
    if installed_foreign_probe:
        validate_installed_foreign_probe(manifest)
    if installed_configuration:
        validate_installed_configuration(manifest)
    if installed_assets:
        validate_installed_assets(manifest)
    if installed_native:
        validate_installed_native(manifest)
    return directory


class HTTPSScopedTarget:
    def __init__(self, manifest, directory, scenario, *, deadline, max_requests, release_reader=None):
        import httpx
        self.httpx = httpx
        self.manifest = manifest
        self.scenario = scenario
        self.run_id = manifest["authorization"]["run_id"]
        self.release_reader = release_reader
        self.deadline, self.max_requests, self.calls = deadline, max_requests, 0
        self.cleanup_phase = False
        self.general_cleanup_reserve = 4
        self.cleanup_grace = manifest[scenario]["limits"].get("cleanup_grace_seconds", 0)
        self.asset_readers = ([InstalledPortabilityAssets(self, entry) for entry in validate_installed_assets(manifest)]
                             if scenario == "portability" and "installed_assets" in manifest[scenario] else [])
        self.journal = (os.open(directory / f"devops-{self.run_id}-{scenario}.jsonl",
                                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        if scenario in {"context", "portability"} else None)
        self.client = httpx.Client(timeout=15, follow_redirects=False)

    @property
    def cleanup_reserve(self):
        return self.general_cleanup_reserve + sum(reader.reserved for reader in self.asset_readers)

    def _asset_reader(self, scope):
        reader = next((reader for reader in self.asset_readers if reader.scope == scope), None)
        if reader is None:
            raise ValueError("installed assets requested foreign Product scope")
        return reader

    def close(self):
        self.client.close()
        if self.journal is not None:
            os.close(self.journal)
        if self.release_reader is not None:
            self.release_reader.close()

    def _journal(self, operation, phase, receipt, **details):
        entry = {"operation": operation, "run_id": self.run_id, "phase": phase,
                 "receipt": receipt, "at": datetime.now(UTC).isoformat(), **details}
        os.write(self.journal, (json.dumps(entry, sort_keys=True) + "\n").encode())
        os.fsync(self.journal)

    def _request(self, channel, selection, *, action=False, receipt=None, statuses=None,
                 include_status=False):
        self.calls += 1
        if self.scenario == "portability":
            if action and channel == "configure":
                self.cleanup_phase = False
            elif action and channel == "cleanup_owned":
                self.cleanup_phase = True
        remaining = self.deadline + (self.cleanup_grace if self.cleanup_phase else 0) - time.monotonic()
        limit = self.max_requests - (self.cleanup_reserve - self.general_cleanup_reserve
                                    if self.cleanup_phase else self.cleanup_reserve)
        reserve_exceeded = self.calls > limit if self.cleanup_phase else self.calls >= limit
        if (remaining <= 0 or self.calls > self.max_requests
                or (self.scenario == "portability" and reserve_exceeded)):
            raise ValueError("selected Context/acceptance request exceeded authorized bound")
        config = self.manifest[self.scenario]["action_channels" if action else "readback_channels"][channel]
        selection = {"target_ref": self.manifest["target_ref"], "qualification_run_id": self.run_id, **selection}
        if action:
            self._journal(channel, "prepared", receipt)
        if self.asset_readers and self.cleanup_phase:
            self.general_cleanup_reserve = max(0, self.general_cleanup_reserve - 1)
        try:
            with self.client.stream("POST" if action else "GET", config["url"],
                                    json=selection if action else None,
                                    params=None if action else {"selection": json.dumps(selection, sort_keys=True)},
                                    headers={"Authorization": "Bearer " + os.environ[config["token_env"]],
                                             **({"Idempotency-Key": receipt} if receipt else {})},
                                    timeout=min(remaining, 15)) as response:
                if response.status_code not in (statuses or ({200, 202, 204} if action else {200})):
                    raise ValueError("selected independent channel unavailable or redirected")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1024 * 1024:
                        raise ValueError("selected readback is oversized")
                body = (json.loads(content) if content else
                        {"target_ref": self.manifest["target_ref"],
                         "qualification_run_id": self.run_id, "observation": {}}
                        if action and response.status_code == 204 else None)
        except self.httpx.TimeoutException as error:
            if action:
                self._journal(channel, "uncertain", receipt)
                raise TimeoutError("later Context submission uncertain; reconcile journal") from error
            raise
        if (not isinstance(body, dict) or body.get("target_ref") != self.manifest["target_ref"]
                or body.get("qualification_run_id") != self.run_id):
            raise ValueError("selected channel returned foreign or stale observation")
        if action:
            self._journal(channel, "acknowledged", receipt)
        observed = body.get("observation")
        return {**observed, "status": response.status_code} if include_status and isinstance(observed, dict) else observed

    def run_later(self, scope, intent, publication_ref, limits):
        receipt = self.manifest["context"]["receipt"]
        return self._request("run_later", {"scope": scope, "intent": intent,
                         "publication_ref": publication_ref, "limits": limits}, action=True, receipt=receipt)

    def _project(self, scope):
        return next(project for project in self.manifest["projects"] if project["scope"] == scope)

    def configure(self, project):
        if self.scenario == "portability" and "installed_configuration" in self.manifest["portability"]:
            return InstalledConfiguration(self, project["scope"]).confirm(project)
        result = self._request("configure", {"project": project}, action=True,
                               receipt=project["configuration_receipt"])
        if not isinstance(result, dict) or result.get("scope") != project["scope"]:
            raise ValueError("scoped project configuration was not acknowledged")

    def run_operation(self, scope, capability, limits):
        if self.scenario == "portability" and "installed_operations" in self.manifest["portability"]:
            return submit_installed_operation(self, scope, capability, limits)
        return self._request("run_operation", {"scope": scope, "capability": capability,
                              "limits": limits}, action=True, receipt=self._project(scope)["operation_receipt"])

    def run_context(self, scope, result_ref, limits):
        return self._request("run_context", {"scope": scope, "result_ref": result_ref,
                              "limits": limits}, action=True, receipt=self._project(scope)["context_receipt"])

    def probe_foreign(self, scope, foreign):
        if self.scenario == "portability" and "installed_foreign_probe" in self.manifest["portability"]:
            return probe_installed_foreign(self, scope, foreign)
        result = self._request("probe_foreign", {"scope": scope, "foreign_scope": foreign},
                               action=True, statuses={403, 404}, include_status=True)
        if not isinstance(result, dict):
            raise ValueError("foreign probe lacks a scoped denial")
        return result

    def cleanup_owned(self, scope, assets):
        if self.asset_readers:
            return self._asset_reader(scope).cleanup(scope, assets)
        self._request("cleanup_owned", {"scope": scope, "assets": assets}, action=True,
                      receipt=self._project(scope)["cleanup_receipt"], statuses={200, 204})

    def read_platform(self, target):
        return self._request("platform", {"target_ref": target})

    def read_product(self, scope):
        return self._request("product", {"scope": scope})

    def read_assets(self, scope, assets):
        if self.asset_readers:
            return self._asset_reader(scope).read(scope, assets)
        return self._request("assets", {"scope": scope, "assets": assets})

    def read_configuration(self, scope):
        if self.scenario == "portability" and "installed_configuration" in self.manifest["portability"]:
            return InstalledConfiguration(self, scope).read()
        return self._request("configuration", {"scope": scope})

    def read_access(self, scope):
        if self.scenario == "portability" and "installed_access" in self.manifest["portability"]:
            return read_installed_access(self, scope)
        return self._request("access", {"scope": scope})

    def read_native(self, scope, run_id):
        if self.scenario == "portability" and "installed_native" in self.manifest["portability"]:
            return InstalledNativeReader(self, scope).read(run_id)
        return self._request("native", {"scope": scope, "run_id": run_id})

    def read_context(self, scope, run_id):
        return self._request("context", {"scope": scope, "run_id": run_id})

    def read_foreign_denial(self, scope, foreign):
        return self._request("foreign_denial", {"scope": scope, "foreign_scope": foreign})

    def read_result(self, scope, result_ref):
        return self._request("result", {"scope": scope, "result_ref": result_ref})

    def read_native_job(self, scope, job_id):
        return self._request("native_job", {"scope": scope, "job_id": job_id})

    def read_source(self, scope, source):
        if self.scenario == "context" and "installed_sources" in self.manifest["context"]:
            return read_installed_source(self, scope, source)
        observed = self._request("source", {"scope": scope, "source": source})
        if not isinstance(observed, dict) or not isinstance(observed.get("content_b64"), str):
            raise ValueError("source content needs an independent bounded readback")
        try:
            content = base64.b64decode(observed.pop("content_b64"), validate=True)
        except binascii.Error:
            raise ValueError("invalid source content readback") from None
        return {**observed, "content": content}

    def read_publication(self, scope, publication_ref):
        return self._request("publication", {"scope": scope, "publication_ref": publication_ref})

    def read_asset(self, scope, asset_ref):
        if self.scenario == "context" and "installed_asset" in self.manifest["context"]:
            return read_installed_asset(self, scope, asset_ref)
        return self._request("asset", {"scope": scope, "asset_ref": asset_ref})

    def read_receipt(self, scope, publication_ref):
        return self._request("receipt", {"scope": scope, "publication_ref": publication_ref})

    def read_visibility(self, scope, publication_ref):
        return self._request("visibility", {"scope": scope, "publication_ref": publication_ref})

    def read_baseline(self, scope, intent):
        return self._request("baseline", {"scope": scope, "intent": intent})

    def read_later(self, scope, run_id):
        return self._request("later", {"scope": scope, "run_id": run_id})

    def read_usage(self, scope, run_id=None):
        selection = {"scope": scope}
        if run_id is not None:
            selection["run_id"] = run_id
        return self._request("usage", selection)

    def read_denial(self, scope, run_id, name):
        return self._request("denial", {"scope": scope, "run_id": run_id, "case": name})

    def read_report(self):
        return self._request("report", {})

    def read_epic_criteria(self, target):
        return self._request("epic", {"target_ref": target})

    def read_check(self, target, ref):
        return self._request("check", {"target_ref": target, "readback_ref": ref})

    def read_case(self, target, ref):
        return self._request("case", {"target_ref": target, "readback_ref": ref})

    def read_cleanup(self, target):
        return self._request("cleanup", {"target_ref": target})

    def read_release(self, target):
        return self.release_reader.read_release(target)

    def read_ingress(self, target):
        return self.release_reader.read_ingress(target)

    def read_capabilities(self, target):
        return self.release_reader.read_capabilities(target)

    def read_journeys(self, target):
        return self.release_reader.read_journeys(target)


def qualify_context(source_sha, manifest_path, evidence_dir, *, target_factory=HTTPSScopedTarget,
                    release_reader_factory=HTTPReader, policy_reader=read_ingress_policy, now=None):
    manifest = load_manifest(manifest_path)
    directory = authorize_channels(manifest, source_sha, evidence_dir, "context", now=now)
    context = manifest["context"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", context.get("receipt", "")):
        raise ValueError("later operation needs a fresh owner-selected idempotency receipt")
    policy = selected_ingress_policy(manifest, deadline=time.monotonic() + context["limits"]["deadline_seconds"],
                                     max_requests=context["limits"]["max_requests"], policy_reader=policy_reader)
    reader = release_reader_factory(manifest, deadline=time.monotonic() + context["limits"]["deadline_seconds"],
                                    max_requests=release_readback_budget(manifest))
    recorded = ReadbackRecorder(PolicyIngressReader(reader, policy, manifest["target_ref"]))
    try:
        verify_release(manifest, recorded, now=now,
                       qualification_run_id=manifest["authorization"]["run_id"])
    finally:
        reader.close()
    target = target_factory(manifest, directory, "context",
                            deadline=time.monotonic() + context["limits"]["deadline_seconds"],
                            max_requests=context["limits"]["max_requests"])
    try:
        result = verify_context(manifest, target, ReadbackRecorder(target, recorded.records), now=now or datetime.now(UTC))
        result["authorization_run_id"] = manifest["authorization"]["run_id"]
        save_evidence(directory, result["authorization_run_id"], "context", result, recorded.records)
        return result["authorization_run_id"]
    finally:
        target.close()


def qualify_acceptance(source_sha, manifest_path, evidence_dir, *, target_factory=HTTPSScopedTarget,
                       release_reader_factory=HTTPReader, policy_reader=read_ingress_policy, now=None):
    manifest = load_manifest(manifest_path)
    directory = authorize_channels(manifest, source_sha, evidence_dir, "acceptance", now=now)
    limits = manifest["acceptance"]["limits"]
    deadline = time.monotonic() + limits["deadline_seconds"]
    policy = selected_ingress_policy(manifest, deadline=deadline, max_requests=limits["max_requests"],
                                     policy_reader=policy_reader)
    release_reader = release_reader_factory(manifest, deadline=deadline, max_requests=release_readback_budget(manifest))
    try:
        target = target_factory(manifest, directory, "acceptance", release_reader=PolicyIngressReader(
                                    release_reader, policy, manifest["target_ref"]),
                                deadline=time.monotonic() + limits["deadline_seconds"],
                                max_requests=limits["max_requests"])
    except Exception:
        release_reader.close()
        raise
    try:
        recorded = ReadbackRecorder(target)
        evidence = recorded.read_report()
        result = verify_acceptance(manifest, evidence, recorded, now=now or datetime.now(UTC))
        result["ingress_policy"] = policy
        result["authorization_run_id"] = manifest["authorization"]["run_id"]
        save_evidence(directory, result["authorization_run_id"], "acceptance", result, recorded.records)
        return result["authorization_run_id"]
    finally:
        target.close()


def qualify_portability(source_sha, manifest_path, evidence_dir, *, target_factory=HTTPSScopedTarget,
                        release_reader_factory=HTTPReader, policy_reader=read_ingress_policy, now=None):
    manifest = load_manifest(manifest_path)
    directory = authorize_channels(manifest, source_sha, evidence_dir, "portability", now=now)
    validate_projects(manifest)
    receipts = [project[name] for project in manifest["projects"]
                for name in ("configuration_receipt", "operation_receipt", "context_receipt", "cleanup_receipt")]
    if (len(set(receipts)) != 8 or any(not isinstance(receipt, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt) for receipt in receipts)):
        raise ValueError("all project actions require unique private idempotency receipts")
    limits = manifest["portability"]["limits"]
    policy = selected_ingress_policy(manifest, deadline=time.monotonic() + limits["deadline_seconds"],
                                     max_requests=limits["max_requests"], policy_reader=policy_reader)
    reader = release_reader_factory(manifest, deadline=time.monotonic() + limits["deadline_seconds"],
                                    max_requests=release_readback_budget(manifest))
    recorded = ReadbackRecorder(PolicyIngressReader(reader, policy, manifest["target_ref"]))
    try:
        verify_release(manifest, recorded, now=now,
                       qualification_run_id=manifest["authorization"]["run_id"])
    finally:
        reader.close()
    target = target_factory(manifest, directory, "portability",
                            deadline=time.monotonic() + limits["deadline_seconds"],
                            max_requests=limits["max_requests"])
    try:
        result = verify_portability(manifest, target, ReadbackRecorder(target, recorded.records))
        saved = {"authorization_run_id": manifest["authorization"]["run_id"],
                 "requirements_revision": manifest["requirements_revision"], "projects": result}
        save_evidence(directory, saved["authorization_run_id"], "portability", saved, recorded.records)
        return saved["authorization_run_id"]
    finally:
        target.close()
