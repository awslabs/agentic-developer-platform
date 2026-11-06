"""Retain verified readbacks and project their digest-bound emitter summaries."""

import base64
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
SCENARIOS = {"journeys", "recovery", "context", "portability", "acceptance"}


def encode(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "content": base64.b64encode(value).decode()}
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    raise TypeError("unsupported selector observation")


def evidence_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode).encode()


class ReadbackRecorder:
    def __init__(self, reader, records=None):
        self.reader = reader
        self.records = records if records is not None else []

    def __getattr__(self, name):
        method = getattr(self.reader, name)
        if not name.startswith("read_") or not callable(method):
            return method

        def read(*args, **kwargs):
            value = method(*args, **kwargs)
            record = {"method": name, "args": args, "kwargs": kwargs, "value": value}
            self.records.append(json.loads(evidence_bytes(record)))
            return value

        return read


def summary_for(document, filename, content):
    scenario = document.get("selector")
    run_id = document.get("authorization_run_id")
    records = document.get("observations")
    if (document.get("selector_evidence_version") != 1 or scenario not in SCENARIOS
            or document.get("status") != "passed" or not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", run_id)
            or filename != f"devops-{run_id}-{scenario}.json"
            or not isinstance(records, list) or not records or len(content) > MAX_BYTES):
        raise ValueError("selector evidence is incomplete or belongs to another run")

    def observed(method):
        return [(f"{filename}#/observations/{index}/value", record["value"], record["args"])
                for index, record in enumerate(records) if record["method"] == method]

    def one(method):
        values = observed(method)
        if len(values) != 1:
            raise ValueError("selector evidence lacks a unique required readback")
        return values[0]

    releases = observed("read_release")
    if not releases:
        raise ValueError("selector evidence lacks deployed release readback")
    release = releases[-1][1]
    summary = {"selector_evidence_version": 1, "selector": scenario, "status": "passed", "run_id": run_id,
               "evidence_ref": filename, "evidence_sha256": hashlib.sha256(content).hexdigest(),
               "observed_at": document["observed_at"], "artifacts": release["artifacts"],
               "deployment_id": release["deployment_id"], "readback_refs": [
                   f"{filename}#/observations/{index}/value" for index in range(len(records))]}
    if scenario == "journeys":
        capabilities = one("read_capabilities")
        summary["native_results"] = [{**value, "readback_ref": f"{capabilities[0]}/{index}"}
                                     for index, value in enumerate(capabilities[1])]
    elif scenario == "context":
        native = one("read_native_job")
        summary["native_results"] = [{"native_id": native[1]["job_id"], "status": native[1]["status"],
                                      "readback_ref": native[0]}]
        summary["reuse"] = {field: observed(method)[0][0] for field, method in (
            ("result_ref", "read_result"), ("publication_ref", "read_publication"),
            ("source_ref", "read_source"), ("later_decision_ref", "read_later"), ("denial_ref", "read_denial"))}
    elif scenario == "recovery":
        summary["users"] = [session["user_id"] for session in one("read_sessions")[1]]
        summary["denied_provider_calls"] = len({args[0] for _, _, args in observed("read_provider_calls")})
        summary["cleanup"] = {"status": "confirmed", **document["cleanup"]}
    elif scenario == "portability":
        native = observed("read_native")
        configurations = observed("read_configuration")
        accesses = observed("read_access")
        if len(native) != 2 or len(configurations) != 2 or len(accesses) != 2:
            raise ValueError("portability evidence needs both verified projects")
        summary["native_results"] = [{"capability": value["capability"], "native_id": value["native_job_id"],
                                      "status": value["status"], "readback_ref": ref} for ref, value, _ in native]
        summary["projects"] = []
        for (_, configuration, _), (_, native_result, _) in zip(configurations, native, strict=True):
            if configuration["scope"] != native_result["scope"]:
                raise ValueError("portability readbacks belong to different projects")
            summary["projects"].append({
                "product": configuration["scope"]["product_id"], "project": configuration["scope"]["project_id"],
                "repository": configuration["repository_ref"], "team": configuration["team_ref"],
                "policy": configuration["policy_ref"], "environment": configuration["environment_ref"],
                "source_sha": native_result["source_sha"], "workload": configuration["workload_kind"],
                "account": configuration["account_ref"]})
        hosting_account = observed("read_platform")[0][1]["hosting_account_ref"]
        cross_account = [value["role_ref"] for _, value, _ in accesses if value["account_ref"] != hosting_account]
        if not cross_account:
            raise ValueError("portability evidence lacks verified cross-account access")
        summary["cross_account_role"] = cross_account[0]
        summary["retained_unchanged"] = True
    else:
        report = one("read_report")[1]
        summary["cases"] = report["cases"]
        summary["epic_criteria"] = list(one("read_epic_criteria")[1])
        summary["cleanup"] = one("read_cleanup")[1]
        summary["live_checks"] = {value["name"]: {**value, "readback_ref": args[1]}
                                  for _, value, args in observed("read_check")}
    return summary


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
            raise ValueError("selector evidence needs a private regular file")
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def save_selector_evidence(directory, run_id, scenario, result, records):
    filename = f"devops-{run_id}-{scenario}.json"
    document = {**result, "selector_evidence_version": 1, "selector": scenario, "status": "passed",
                "authorization_run_id": run_id, "observed_at": datetime.now(UTC).isoformat(),
                "observations": records}
    content = evidence_bytes(document)
    summary = summary_for(document, filename, content)
    write_private(Path(directory) / filename, content)
    write_private(Path(directory) / f"{scenario}.json", evidence_bytes(summary))


def verified_summary(directory, scenario, summary):
    run_id = summary.get("run_id")
    if (summary.get("selector_evidence_version") != 1 or summary.get("selector") != scenario
            or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", run_id)):
        raise ValueError("invalid selector evidence summary")
    filename = f"devops-{run_id}-{scenario}.json"
    if summary.get("evidence_ref") != filename:
        raise ValueError("selector evidence reference belongs to another run")
    descriptor = os.open(Path(directory) / filename, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077 or details.st_size > MAX_BYTES:
            raise ValueError("selector evidence must be bounded and private")
        content = source.read(MAX_BYTES + 1)
    expected = summary_for(json.loads(content), filename, content)
    if summary != expected:
        raise ValueError("selector summary differs from its verified evidence")
    return summary
