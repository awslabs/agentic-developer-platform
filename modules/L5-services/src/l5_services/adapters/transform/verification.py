"""Detached test evidence from a trusted, independently pinned executor."""

import asyncio
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from l5_services.adapters.transform.provenance import compare_source_trees


class DetachedCheckPending(RuntimeError):
    """SQL work must revisit a launched check rather than recording a failed baseline."""


class DetachedCheckRunner(Protocol):
    async def run_check(self, *, stage: str, files: dict[str, bytes], source_tree: str,
                        file_modes: dict[str, str],
                        command: dict, image_digest: str, bundle_sha256: str,
                        oracle_version: str, authority) -> dict: ...

    async def read_log(self, *, reference: dict, authority) -> bytes: ...


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _reference(reference):
    return (isinstance(reference, dict) and set(reference) == {
        "artifact_id", "version", "sha256", "bytes"}
        and all(isinstance(reference[key], str) and reference[key] for key in ("artifact_id", "version"))
        and _digest(reference["sha256"])
        and type(reference["bytes"]) is int and reference["bytes"] >= 0)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _submission_order_valid(stage, observed, submitted_at):
    if (not isinstance(submitted_at, datetime) or submitted_at.tzinfo is None
            or not isinstance(observed, dict)):
        return False
    if stage == "before":
        ended = _timestamp(observed.get("ended_at"))
        return ended is not None and ended <= submitted_at
    started = _timestamp(observed.get("started_at"))
    return stage == "after" and started is not None and started >= submitted_at


def _check_policy(policy):
    if (not isinstance(policy, dict) or not _digest(policy.get("bundle_sha256"))
            or not _digest(policy.get("lock_sha256"))
            or not isinstance(policy.get("image_digest"), str)
            or not policy["image_digest"].startswith("sha256:")
            or not _digest(policy["image_digest"].removeprefix("sha256:"))
            or not isinstance(policy.get("oracle_version"), str) or not policy["oracle_version"]
            or not isinstance(policy.get("commands"), list) or not policy["commands"]):
        raise ValueError("detached test policy is not pinned")
    for command in policy["commands"]:
        if (not isinstance(command, dict) or set(command) != {
                "argv", "cwd", "environment", "timeout_seconds", "minimum_test_count"}
                or not isinstance(command["argv"], list) or not command["argv"]
                or any(not isinstance(item, str) or not item for item in command["argv"])
                or not isinstance(command["cwd"], str) or not command["cwd"]
                or not isinstance(command["environment"], dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in command["environment"].items())
                or type(command["timeout_seconds"]) is not int
                or not 1 <= command["timeout_seconds"] <= 3600
                or type(command["minimum_test_count"]) is not int
                or command["minimum_test_count"] < 1):
            raise ValueError("detached test command is not pinned")


async def _receipt(stage, source_tree, command, policy, observed, runner, authority):
    if not isinstance(observed, dict):
        return {"stage": stage, "status": "inconclusive", "receipt": None}
    expected = {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                "environment": command["environment"], "image_digest": policy["image_digest"],
                "source_tree": source_tree, "bundle_sha256": policy["bundle_sha256"],
                "lock_sha256": policy["lock_sha256"], "oracle_version": policy["oracle_version"]}
    if any(observed.get(key) != value for key, value in expected.items()):
        raise ValueError("detached executor receipt differs from pinned test policy or tree")
    start = _timestamp(observed.get("started_at"))
    end = _timestamp(observed.get("ended_at"))
    if (start is None or end is None or end < start
            or not _reference(observed.get("stdout")) or not _reference(observed.get("stderr"))):
        return {"stage": stage, "status": "inconclusive", "receipt": observed}
    timed_out = observed.get("timed_out")
    if type(timed_out) is not bool:
        return {"stage": stage, "status": "inconclusive", "receipt": observed}
    exit_code = observed.get("exit_code")
    test_count = observed.get("test_count")
    if timed_out:
        status = "failed" if observed.get("termination_receipt") else "inconclusive"
    elif type(exit_code) is not int or type(test_count) is not int or test_count < 0:
        status = "inconclusive"
    else:
        status = "passed" if exit_code == 0 and test_count >= command["minimum_test_count"] else "failed"
    if status == "passed":
        for key in ("stdout", "stderr"):
            try:
                contents = await asyncio.wait_for(runner.read_log(
                    reference=observed[key], authority=authority), timeout=5)
            except (TimeoutError, FileNotFoundError):
                status = "inconclusive"
                break
            if (not isinstance(contents, bytes) or len(contents) != observed[key]["bytes"]
                    or sha256(contents).hexdigest() != observed[key]["sha256"]):
                status = "inconclusive"
                break
    return {"stage": stage, "status": status, "receipt": observed}


async def verify_detached_checks(runner: DetachedCheckRunner, policy: dict,
                                 base_files: dict[str, bytes], candidate_files: dict[str, bytes] | None,
                                 authority, *, baseline_receipts=None, submitted_at=None,
                                 base_modes=None, candidate_modes=None):
    _check_policy(policy)
    if baseline_receipts is not None and (not isinstance(baseline_receipts, list)
                                          or len(baseline_receipts) != len(policy["commands"])):
        raise ValueError("baseline receipts differ from pinned checks")
    if base_modes is None:
        base_modes = {path: "100644" for path in base_files}
    if candidate_files is None:
        candidate_modes = base_modes
    elif candidate_modes is None:
        candidate_modes = {path: "100644" for path in candidate_files}
    trees = compare_source_trees(base_files, candidate_files if candidate_files is not None else base_files,
                                 base_modes=base_modes, candidate_modes=candidate_modes)
    output = {"tests_before": [], "tests_after": [], "source_trees": trees}
    for stage, files, key in (("before", base_files, "tests_before"),
                              ("after", candidate_files, "tests_after")):
        for index, command in enumerate(policy["commands"]):
            tree = trees["base_tree" if stage == "before" else "candidate_tree"]
            if stage == "before" and baseline_receipts is not None:
                observed = baseline_receipts[index]
                entry = await _receipt(stage, tree, command, policy, observed, runner, authority)
                if not _submission_order_valid(stage, observed, submitted_at):
                    entry["status"] = "inconclusive"
                output[key].append(entry)
                continue
            if files is None or stage == "after" and any(
                    item["status"] != "passed" for item in output["tests_before"]):
                output[key].append({"stage": stage, "status": "not_run", "receipt": None})
                continue
            try:
                observed = await asyncio.wait_for(runner.run_check(
                    stage=stage, files=files, source_tree=tree, command=command,
                    file_modes=base_modes if stage == "before" else candidate_modes,
                    image_digest=policy["image_digest"], bundle_sha256=policy["bundle_sha256"],
                    oracle_version=policy["oracle_version"], authority=authority),
                    timeout=command["timeout_seconds"] + 5)
            except TimeoutError:
                output[key].append({"stage": stage, "status": "inconclusive", "receipt": None})
                continue
            entry = await _receipt(stage, tree, command, policy, observed, runner, authority)
            if stage == "after" and submitted_at is not None:
                if not _submission_order_valid(stage, observed, submitted_at):
                    entry["status"] = "inconclusive"
            output[key].append(entry)
    return output
