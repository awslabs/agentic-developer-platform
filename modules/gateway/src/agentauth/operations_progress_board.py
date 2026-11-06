"""Qualify an inherited story checklist for merge, never as execution authority.

This reader is deliberately separate from repair edit/publication validation.
Completed tasks remain progress claims; they do not satisfy checks, acceptance,
deployment evidence, or any other requirement for the repair.
"""

import base64
import binascii
import hashlib
import json
import re

from .github_operations import OperationRefusedError

MAX_BOARD_BYTES = 256 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[a-f0-9]{40}")


async def assigned_story_issue(provider, authorized):
    from src.orchestration.models import OrchestrationNode
    from src.shared.database import get_session_factory

    assignment = provider.assignment
    if getattr(authorized, "assignment", None) != assignment or provider.repo != assignment.repository:
        raise OperationRefusedError("inherited progress board assignment differs")
    async with get_session_factory()() as session:
        node = await session.get(OrchestrationNode, assignment.node_id)
        if (
            node is None
            or node.org_id != assignment.tenant_id
            or node.kind != "story"
            or node.repository_identity != assignment.repository
            or node.provider_repository_id is not None
            and node.provider_repository_id != assignment.repository_id
            or not isinstance(node.issue_ref, str)
            or not re.fullmatch(r"[1-9][0-9]{0,18}", node.issue_ref)
            or assignment.branch != f"agent/issue-{node.issue_ref}"
        ):
            raise OperationRefusedError("inherited progress board requires a matching server story, repository and branch")
        return int(node.issue_ref)


def validate_board(value, issue):
    """Validate v1/v2 progress shape without interpreting its claims as authority."""
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] not in (1, 2)
        or type(value.get("issue")) is not int
        or value["issue"] != issue
    ):
        raise ValueError("identity or version")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 1000:
        raise ValueError("task count")
    ids, parents = set(), {}
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not IDENTIFIER.fullmatch(task["id"]) or task["id"] in ids:
            raise ValueError("task identity")
        ids.add(task["id"])
        if task.get("status") not in ("open", "in_progress", "done", "blocked") or task.get("kind") not in ("code", "test", "infra"):
            raise ValueError("task state")
        if (
            not isinstance(task.get("title"), str)
            or not task["title"].strip()
            or len(task["title"]) > 1000
            or any(c in task["title"] for c in "\r\n\x00")
        ):
            raise ValueError("task title")
        for field in ("covers", "files", "commits"):
            if field in task and (not isinstance(task[field], list) or any(not isinstance(item, str) for item in task[field])):
                raise ValueError("task list")
        for field in ("criterion", "note", "checkpoint_note", "started_at", "finished_at", "run_id", "pattern_id"):
            if task.get(field) is not None and not isinstance(task[field], str):
                raise ValueError("task metadata")
        if "attempt" in task and (type(task["attempt"]) is not int or task["attempt"] < 0):
            raise ValueError("task attempt")
        step = task.get("planStep")
        if step is not None:
            if (
                not isinstance(step, dict)
                or not isinstance(step.get("id"), str)
                or not IDENTIFIER.fullmatch(step["id"])
                or not isinstance(step.get("title"), str)
                or not 1 <= len(step["title"]) <= 300
                or any(c in step["title"] for c in "\r\n\x00")
                or step["id"] in parents
                and parents[step["id"]] != step["title"]
            ):
                raise ValueError("task parent")
            parents[step["id"]] = step["title"]
    if any(reference not in ids for task in tasks for reference in task.get("covers", [])):
        raise ValueError("task reference")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("non-JSON constant")


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value)


async def verify_progress_board(provider, entry, *, head, issue):
    path = f".adp/tasks/{issue}.json"
    if entry.get("filename") != path or entry.get("status") not in ("added", "modified") or "previous_filename" in entry:
        raise OperationRefusedError("repair may only inherit the assigned story progress board; removal and rename are forbidden")
    if not valid_sha(head):
        raise OperationRefusedError("inherited progress board revision is invalid")
    commit = await provider._call("GET", f"/repos/{provider.repo}/git/commits/{head}")
    if not isinstance(commit, dict) or commit.get("sha") != head or not isinstance(commit.get("tree"), dict):
        raise OperationRefusedError("inherited progress board commit identity differs")
    tree_sha = commit["tree"].get("sha")
    for component in (".adp", "tasks", f"{issue}.json"):
        if not valid_sha(tree_sha):
            raise OperationRefusedError("inherited progress board tree identity is invalid")
        tree = await provider._call("GET", f"/repos/{provider.repo}/git/trees/{tree_sha}")
        if (
            not isinstance(tree, dict)
            or tree.get("sha") != tree_sha
            or tree.get("truncated") is not False
            or not isinstance(tree.get("tree"), list)
            or len(tree["tree"]) > 10000
            or any(not isinstance(item, dict) for item in tree["tree"])
        ):
            raise OperationRefusedError("inherited progress board tree is unavailable or incomplete")
        matches = [item for item in tree["tree"] if item.get("path") == component]
        if len(matches) != 1:
            raise OperationRefusedError("inherited progress board path is missing or ambiguous")
        item = matches[0]
        expected_type, expected_mode = ("blob", "100644") if component.endswith(".json") else ("tree", "040000")
        if item.get("type") != expected_type or item.get("mode") != expected_mode or not valid_sha(item.get("sha")):
            raise OperationRefusedError("inherited progress board must be a regular file in ordinary trees")
        tree_sha = item["sha"]
    if entry.get("sha") != tree_sha:
        raise OperationRefusedError("inherited progress board diff and tested revision differ")
    blob = await provider._call("GET", f"/repos/{provider.repo}/git/blobs/{tree_sha}")
    if (
        not isinstance(blob, dict)
        or blob.get("sha") != tree_sha
        or blob.get("encoding") != "base64"
        or type(blob.get("size")) is not int
        or not 0 < blob["size"] <= MAX_BOARD_BYTES
        or not isinstance(blob.get("content"), str)
        or len(blob["content"]) > 2 * MAX_BOARD_BYTES
    ):
        raise OperationRefusedError("inherited progress board blob is unavailable or exceeds 256 KiB")
    try:
        raw = base64.b64decode(blob["content"].encode("ascii").translate(None, b" \t\r\n"), validate=True)
        # Match the Git object ID returned by the authenticated repository API.
        digest = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw, usedforsecurity=False).hexdigest()
        if len(raw) != blob["size"] or digest != tree_sha:
            raise ValueError("blob integrity")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
        validate_board(value, issue)
    except (ValueError, UnicodeError, binascii.Error, TypeError, RecursionError):
        raise OperationRefusedError("inherited progress board content is invalid; restore the assigned v1/v2 JSON board through its owner") from None
