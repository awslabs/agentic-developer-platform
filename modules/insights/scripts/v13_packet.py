"""Assemble a private B11 packet from recorded V13 runs; never execute fixtures here."""

import hashlib
import json
import os
import pathlib
import re
import sys


SCENARIOS = {f"AC-{number:02}": f"SCN-{number:02}" for number in range(1, 5)}


def required(record, names, label):
    if not isinstance(record, dict) or any(name not in record for name in names):
        raise ValueError(f"{label}: missing required evidence")
    return record


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: missing required evidence")
    return value


def revision(value, label):
    if not re.fullmatch(r"[a-f0-9]{40}", text(value, label)):
        raise ValueError(f"{label}: immutable source revision required")
    return value


def digest(value, label):
    if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", text(value, label)):
        raise ValueError(f"{label}: immutable digest required")
    return value


def prepare_packet(run):
    required(run, ("plan_revision", "requirements_revision", "source_revision",
                   "schema_head", "deployments", "artifacts", "attempts", "cleanup"), "run")
    digest(run["plan_revision"], "plan_revision")
    revision(run["requirements_revision"], "requirements_revision")
    revision(run["source_revision"], "source_revision")
    text(run["schema_head"], "schema_head")
    deployments = required(run["deployments"],
                           ("emitter", "read_service", "profile", "catalog"), "deployments")
    for name, image_field in (("emitter", "image_digest"), ("read_service", "image_digest")):
        deployment = required(deployments[name], ("source_revision", "workflow_revision",
                                                   image_field, "receipt"), name)
        revision(deployment["source_revision"], name + " source_revision")
        revision(deployment["workflow_revision"], name + " workflow_revision")
        digest(deployment[image_field], name + " image_digest")
        text(deployment["receipt"], name + " receipt")
    for name in ("profile", "catalog"):
        deployment = required(deployments[name], ("source_revision", "workflow_revision",
                                                   "artifact_digest", "receipt"), name)
        revision(deployment["source_revision"], name + " source_revision")
        revision(deployment["workflow_revision"], name + " workflow_revision")
        digest(deployment["artifact_digest"], name + " artifact_digest")
        text(deployment["receipt"], name + " receipt")
    artifacts = required(run["artifacts"], ("cli_checksum", "cli_report",
                                             "api_report", "run_report"), "artifacts")
    digest(artifacts["cli_checksum"], "cli_checksum")
    for name in ("cli_report", "api_report", "run_report"):
        text(artifacts[name], name)
    cleanup = required(run["cleanup"], ("status", "receipt"), "cleanup")
    text(cleanup["receipt"], "cleanup receipt")
    if cleanup["status"] not in ("complete", "failed", "not_run"):
        raise ValueError("cleanup: invalid status")

    attempts = run["attempts"]
    if not isinstance(attempts, list):
        raise ValueError("attempts: missing run results")
    latest = {}
    seen = set()
    ordered = {}
    for item in attempts:
        required(item, ("ac", "scenario", "test", "run_id", "attempt", "action",
                        "expected", "actual", "outcome", "evidence"), "attempt")
        ac = item["ac"]
        if ac not in SCENARIOS or item["scenario"] != SCENARIOS[ac]:
            raise ValueError("attempt: unknown AC/scenario mapping")
        for name in ("test", "run_id", "action", "expected", "actual"):
            text(item[name], name)
        evidence = required(item["evidence"], ("readback", "run_report"), "attempt evidence")
        for name in ("readback", "run_report"):
            text(evidence[name], name)
        if ac == "AC-04":
            cli = required(evidence, ("cli",), "CLI evidence")["cli"]
            if (not isinstance(cli, list) or len(cli) != 2
                    or {row.get("case") for row in cli if isinstance(row, dict)}
                    != {"authorized", "revoked"}):
                raise ValueError("CLI evidence: authorized and revoked commands required")
            for command in cli:
                required(command, ("command", "stdout", "stderr", "exit_code"), "CLI command")
                text(command["command"], "CLI command")
                if (not isinstance(command["stdout"], str)
                        or not isinstance(command["stderr"], str)
                        or type(command["exit_code"]) is not int):
                    raise ValueError("CLI command: stdout, stderr and exit code required")
                if (item["outcome"] == "passed"
                        and ((command["case"] == "authorized" and command["exit_code"] != 0)
                             or (command["case"] == "revoked" and command["exit_code"] == 0))):
                    raise ValueError("CLI command: exit code contradicts result")
        if ac == "AC-03":
            text(required(evidence, ("quarantine",), "quarantine evidence")["quarantine"],
                 "quarantine evidence")
        if item["outcome"] not in ("passed", "failed", "skipped", "not_run"):
            raise ValueError("attempt: invalid outcome")
        if type(item["attempt"]) is not int or item["attempt"] < 1:
            raise ValueError("attempt: invalid number")
        key = (ac, item["run_id"], item["attempt"])
        if key in seen:
            raise ValueError("attempt: duplicate run identity")
        seen.add(key)
        sequence = (ac, item["run_id"])
        if item["attempt"] <= ordered.get(sequence, 0):
            raise ValueError("attempt: out of order")
        ordered[sequence] = item["attempt"]
        latest[ac] = item["outcome"]

    complete = (cleanup["status"] == "complete"
                and all(latest.get(ac) == "passed" for ac in SCENARIOS))
    supplied = {key: value for key, value in run.items()
                if key not in ("packet_revision", "packet_version", "story", "status", "human_decision")}
    packet = {**supplied, "packet_version": "B11-V13/1", "story": 8276,
              "status": "complete" if complete else "incomplete", "human_decision": "pending"}
    encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    packet["packet_revision"] = hashlib.sha256(encoded).hexdigest()
    return packet


def review_handoff(packet, private_artifact_ref):
    """Identify the exact private packet for a later human B11 decision."""
    required(packet, ("packet_revision", "plan_revision", "status", "human_decision",
                      "story", "packet_version"), "packet")
    digest(packet["packet_revision"], "packet_revision")
    digest(packet["plan_revision"], "plan_revision")
    text(private_artifact_ref, "private artifact reference")
    canonical = {key: value for key, value in packet.items() if key != "packet_revision"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if (hashlib.sha256(encoded).hexdigest() != packet["packet_revision"]
            or packet["story"] != 8276 or packet["packet_version"] != "B11-V13/1"
            or packet["status"] != "complete" or packet["human_decision"] != "pending"):
        raise ValueError("packet is not complete, pending review and revision-bound")
    return {"bolt": "B11", "story": 8276, "plan_revision": packet["plan_revision"],
            "packet_revision": packet["packet_revision"],
            "private_artifact_ref": private_artifact_ref, "human_decision": "pending"}


def main(source, destination):
    packet = prepare_packet(json.loads(pathlib.Path(source).read_text()))
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(packet, output, sort_keys=True, indent=2)
        output.write("\n")
    return 0 if packet["status"] == "complete" else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: v13_packet.py PRIVATE_RUN.json PRIVATE_PACKET.json")
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
