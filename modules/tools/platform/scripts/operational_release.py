"""Protected ordinary-user release smoke, without financial qualification.

This opt-in dev maintenance policy never creates native approval or cost proof.
The protected environment pins the fixture, query, source and workflow revision.
Ordinary Gateway consent, entitlement, admission and ownership checks still run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from .native_smoke import TOOL, discovery, search_receipt, retain
from .native_smoke_inputs import job, private_write
from .native_test_session import (
    AdmissionUnavailable, JWT, NoRedirects, canonical, identifier, origin,
    private_file, request_json, unique_object,
)
from .target_metadata import target_revision

MODE = "operational"
SCOPE = "ordinary_user_behavior_only"


def refuse():
    raise AdmissionUnavailable("Protected operational release inputs or evidence are invalid")


def qualification(env):
    mode = env.get("ADP_TOOLS_RELEASE_QUALIFICATION", "native")
    if mode not in {"native", MODE}:
        refuse()
    return mode


def validate(config, env, *, now):
    job(env)
    fields = {
        "version", "scope", "source_revision", "definition_revision", "gateway_origin",
        "subject_id", "user_id", "tenant_id", "team_id", "query", "authorized_at", "expires_at",
    }
    if (
        qualification(env) != MODE
        or env.get("ADP_TOOLS_ENVIRONMENT") != "dev"
        or env.get("ADP_CORRELATION")
        or type(config) is not dict or set(config) != fields
        or type(config["version"]) is not int or config["version"] != 1
        or config["scope"] != SCOPE
        or config["source_revision"] != env["ADP_SOURCE_REVISION"]
        or config["definition_revision"] != env["ADP_DEFINITION_REVISION"]
        or config["gateway_origin"] != env.get("ADP_TOOLS_SMOKE_GATEWAY_URL")
        or type(config["authorized_at"]) is not int or type(config["expires_at"]) is not int
        or not 0 < config["authorized_at"] <= now < config["expires_at"] <= config["authorized_at"] + 3600
        or not isinstance(config["query"], str) or not 1 <= len(config["query"]) <= 200 or not config["query"].strip()
        or any(not isinstance(config[name], str) or not 1 <= len(config[name]) <= 255
               for name in ("subject_id", "user_id", "tenant_id", "team_id"))
    ):
        refuse()
    origin(config["gateway_origin"])
    identifier(config["subject_id"])
    return config


def preflight(environ=None, *, now=None):
    env = dict(os.environ if environ is None else environ)
    now = time.time() if now is None else now
    config = validate(json.loads(private_file(env["ADP_TOOLS_OPERATIONAL_CONFIG_FILE"], 16384),
                                 object_pairs_hook=unique_object), env, now=now)
    token = private_file(env["ADP_TOOLS_OPERATIONAL_LOGIN_FILE"], 16384).decode().strip()
    if not JWT.fullmatch(token):
        refuse()
    # A local mismatch rejects early. Only the live authenticated /auth/me
    # response below establishes identity; decoding is not signature validation.
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="), object_pairs_hook=unique_object)
    if (claims.get("token_use") != "access" or claims.get("sub") != config["subject_id"]
            or type(claims.get("exp")) is not int or claims["exp"] <= now):
        refuse()
    if not env.get("ADP_TOOLS_SMOKE_EVIDENCE_DIR"):
        refuse()
    return config, token


class Session:
    def __init__(self, config, token, *, opener=None):
        self.config, self.token = config, token
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirects())

    def request(self, method, path, *, payload=None, gateway_url=None, operation_id=None):
        if (method not in {"GET", "POST"}
                or not (path == "/api/auth/me" or path.startswith("/api/agent/tools"))
                or ".." in path or "#" in path or time.time() >= self.config["expires_at"]):
            refuse()
        headers = {"Authorization": "Bearer " + self.token}
        if gateway_url:
            headers["X-Adp-Expected-Gateway-Sha256"] = hashlib.sha256(gateway_url.encode()).hexdigest()
        return request_json(self.opener, self.config["gateway_origin"] + path, headers,
                            method=method, payload=payload)

    def identity(self):
        me = self.request("GET", "/api/auth/me")
        if (me.get("user_id") != self.config["subject_id"]
                or me.get("org_id") != self.config["tenant_id"]
                or me.get("team_id") != self.config["team_id"]
                or me.get("is_admin") is not False):
            refuse()


def prepare(environ=None, *, session_type=Session):
    env = dict(os.environ if environ is None else environ)
    config = validate(json.loads(env["ADP_TOOLS_OPERATIONAL_SMOKE_JSON"], object_pairs_hook=unique_object),
                      env, now=time.time())
    directory = Path(env["RUNNER_TEMP"]) / "adp-operational-tools-inputs"
    directory.mkdir(mode=0o700)  # Never replace the inputs of an existing attempt.
    private_write(directory / "config.json", canonical(config))
    private_write(directory / "login.jwt", env["ADP_TOOLS_SMOKE_OPERATIONAL_ACCESS_TOKEN"].encode())
    paths = {"ADP_TOOLS_OPERATIONAL_CONFIG_FILE": str(directory / "config.json"),
             "ADP_TOOLS_OPERATIONAL_LOGIN_FILE": str(directory / "login.jwt")}
    config, token = preflight({**env, **paths})
    session_type(config, token).identity()
    evidence = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(evidence / "input-delivery.json", canonical({
        "version": 1, "qualification_mode": MODE, "scope": SCOPE,
        "configuration_sha256": hashlib.sha256(canonical(config)).hexdigest(),
        "github_run_id": env["GITHUB_RUN_ID"], "github_run_attempt": "1",
        "source_revision": config["source_revision"], "definition_revision": config["definition_revision"],
        "authorized_at": config["authorized_at"], "expires_at": config["expires_at"],
    }))
    with open(env["GITHUB_ENV"], "a") as output:
        for key, value in paths.items():
            if "\n" in value or "\r" in value:
                refuse()
            output.write(key + "=" + value + "\n")
    return paths


def verify(session, config, gateway_url, *, request_id=None):
    request_id = request_id or str(uuid4())
    payload = {"tool": TOOL, "arguments": {"query": config["query"]},
               "request_id": request_id, "execution_id": None, "ttl_seconds": None}
    report = {"request_id": request_id, "behavioral_status": "failed", "cleanup_status": "unknown",
              "governance_complete": False, "cost_qualification": "not_requested",
              "scope": SCOPE, "failures": [], "query_sha256": hashlib.sha256(config["query"].encode()).hexdigest()}
    execution_id = None
    attempted = False
    try:
        session.identity()
        report["tools_list_sha256"] = discovery(session, gateway_url, None)
        # Listing's root_human_id is independently checked against protected
        # fixture configuration, not inferred from the supplied JWT.
        listing = session.request("GET", "/api/agent/tools")
        if listing.get("root_human_id") != config["user_id"]:
            refuse()
        attempted = True
        result = session.request("POST", "/api/agent/tools/call", payload=payload, gateway_url=gateway_url)
        execution_id, receipt = search_receipt(result, request_id, gateway_url)
        report.update(execution_id=execution_id, receipt_id=receipt)
    except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
        report["failures"].append("search_unconfirmed" if attempted else "identity_or_discovery_unconfirmed")
    if attempted and execution_id is None:
        # Read/recover the original request once, never submit a second query.
        try:
            recovered = session.request("POST", "/api/agent/tools/operations/recover", payload=payload)
            if recovered.get("request_id") != request_id:
                refuse()
            execution_id = identifier(recovered.get("execution_id"))
            report["execution_id"] = execution_id
        except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
            report["failures"].append("owned_execution_unconfirmed")
    if execution_id is not None:
        try:
            closed = session.request("POST", f"/api/agent/tools/executions/{execution_id}/close?tool={TOOL}")
            if (closed.get("execution_id") != execution_id or closed.get("state") != "closed"
                    or closed.get("cleanup_status") != "confirmed"):
                refuse()
            report.update(cleanup_status="confirmed", cleanup_evidence_sha256=hashlib.sha256(canonical(closed)).hexdigest())
        except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
            report["failures"].append("close_unconfirmed")
    if not report["failures"]:
        try:
            operation = session.request("GET", f"/api/agent/tools/executions/{execution_id}/operations/{request_id}?tool={TOOL}")
            if (operation.get("request_id") != request_id or operation.get("execution_id") != execution_id
                    or operation.get("receipt_id") != report["receipt_id"]
                    or operation.get("status") != "confirmed" or operation.get("result_status") != "success"
                    or discovery(session, gateway_url, None) != report["tools_list_sha256"]):
                refuse()
            session.identity()
            report["behavioral_status"] = "passed"
        except (AdmissionUnavailable, ValueError, TypeError, KeyError, AttributeError):
            report["failures"].append("final_readback_unconfirmed")
    return report


def run(outputs, targets, environ=None, *, session_type=Session):
    env = dict(os.environ if environ is None else environ)
    config, token = preflight(env)
    directory = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "operational-smoke.json"
    if path.exists() or path.is_symlink():
        refuse()
    values = json.loads(Path(outputs).read_text())
    revision = target_revision(Path(targets))
    report = verify(session_type(config, token), config, values["gateway_url"]["value"])
    report.update(schema_version="1.0", qualification_mode=MODE,
                  github_run_id=env["GITHUB_RUN_ID"], github_run_attempt="1",
                  source_revision=env["ADP_SOURCE_REVISION"], definition_revision=env["ADP_DEFINITION_REVISION"],
                  target_sync_revision=revision,
                  configuration_sha256=hashlib.sha256(canonical(config)).hexdigest())
    retain(path, report)
    if report["behavioral_status"] != "passed":
        raise AdmissionUnavailable("Operational authenticated smoke failed; evidence retained; release publication refused")
    return {**{key: report[key] for key in (
        "qualification_mode", "scope", "cost_qualification", "behavioral_status", "cleanup_status",
        "governance_complete", "tools_list_sha256", "target_sync_revision", "github_run_id")},
        "operational_evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def qualify(release_path, environ):
    env = dict(environ)
    job(env)
    directory = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    evidence = private_file(directory / "operational-smoke.json", 16384)
    report = json.loads(evidence, object_pairs_hook=unique_object)
    retention = json.loads(private_file(directory / "retention.json", 8192), object_pairs_hook=unique_object)
    release_path = Path(release_path)
    release = json.loads(private_file(release_path, 65536), object_pairs_hook=unique_object)
    digest = hashlib.sha256(evidence).hexdigest()
    manifest = retention.get("manifest", {})
    prefix = f"artifacts/14/aws-e/adp/{env['GITHUB_RUN_ID']}/1/release/operational-tools-smoke/"
    import re
    if (
        qualification(env) != MODE or env.get("ADP_TOOLS_ENVIRONMENT") != "dev"
        or report.get("qualification_mode") != MODE or report.get("scope") != SCOPE
        or report.get("behavioral_status") != "passed" or report.get("cleanup_status") != "confirmed"
        or report.get("cost_qualification") != "not_requested" or report.get("governance_complete") is not False
        or report.get("github_run_id") != env["GITHUB_RUN_ID"] or report.get("github_run_attempt") != "1"
        or release.get("component") != "tools-platform" or "native_smoke" in release
        or release.get("qualification_status") != "smoke_passed_evidence_pending"
        or any(report.get(key) != env[var] or release.get(key) != env[var]
               for key, var in (("source_revision", "ADP_SOURCE_REVISION"), ("definition_revision", "ADP_DEFINITION_REVISION")))
        or release.get("operational_smoke") != {**{key: report[key] for key in (
            "qualification_mode", "scope", "cost_qualification", "behavioral_status", "cleanup_status",
            "governance_complete", "tools_list_sha256", "target_sync_revision", "github_run_id")},
            "operational_evidence_sha256": digest}
        or retention.get("version") != 1 or retention.get("operational_smoke_sha256") != digest
        or not isinstance(manifest, dict)
        or not re.fullmatch(r"adp-dev-ci-evidence-[0-9]{12}", str(manifest.get("bucket", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", "")))
        or manifest.get("key") != prefix + manifest.get("sha256", "") + ".manifest.json"
        or not isinstance(manifest.get("version_id"), str) or manifest["version_id"] in {"", "null"}
    ):
        refuse()
    release["qualification_status"] = "operational_smoke_verified_evidence_retained"
    release["operational_evidence_manifest"] = manifest
    replacement = release_path.with_suffix(".qualified.json")
    private_write(replacement, canonical(release) + b"\n")
    os.replace(replacement, release_path)


if __name__ == "__main__":
    try:
        prepare()
    except (AdmissionUnavailable, ValueError, TypeError, KeyError, OSError):
        raise SystemExit("Operational inputs unavailable; release refused") from None
