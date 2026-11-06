"""Read the assigned branch checklist as progress data, never execution authority."""

import json
import os
import re
from pathlib import Path

from lib import mediated_github as github
from lib.codex_workspace import CodexWorkspace, WorkspaceError
from lib.operations_workspace import source_file

MAX_CONTEXT_BYTES = 48 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[a-f0-9]{40}")


def validate_board(value, issue):
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] not in (1, 2)
        or type(value.get("issue")) is not int
        or value["issue"] != issue
    ):
        raise WorkspaceError("board identity or version differs")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 1000:
        raise WorkspaceError("board task count is outside its bound")
    ids, parents = set(), {}
    for task in tasks:
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not IDENTIFIER.fullmatch(task["id"])
            or task["id"] in ids
        ):
            raise WorkspaceError("board task identity is invalid")
        ids.add(task["id"])
        if task.get("status") not in {"open", "in_progress", "done", "blocked"} or task.get(
            "kind"
        ) not in {"code", "test", "infra"}:
            raise WorkspaceError("board task state is invalid")
        if (
            not isinstance(task.get("title"), str)
            or not task["title"].strip()
            or len(task["title"]) > 1000
            or any(c in task["title"] for c in "\r\n\x00")
        ):
            raise WorkspaceError("board task title is invalid")
        for field in ("covers", "files", "commits"):
            if field in task and (
                not isinstance(task[field], list)
                or any(not isinstance(item, str) for item in task[field])
            ):
                raise WorkspaceError("board task list is invalid")
        for field in (
            "criterion",
            "note",
            "checkpoint_note",
            "started_at",
            "finished_at",
            "run_id",
            "pattern_id",
        ):
            if task.get(field) is not None and not isinstance(task[field], str):
                raise WorkspaceError("board task metadata is invalid")
        if "attempt" in task and (type(task["attempt"]) is not int or task["attempt"] < 0):
            raise WorkspaceError("board task attempt is invalid")
        step = task.get("planStep")
        if step is not None:
            if (
                not isinstance(step, dict)
                or not isinstance(step.get("id"), str)
                or not IDENTIFIER.fullmatch(step["id"])
                or not isinstance(step.get("title"), str)
                or not 1 <= len(step["title"]) <= 300
                or any(c in step["title"] for c in "\r\n\x00")
            ):
                raise WorkspaceError("board parent is invalid")
            if step["id"] in parents and parents[step["id"]] != step["title"]:
                raise WorkspaceError("board parent identity is ambiguous")
            parents[step["id"]] = step["title"]
    if any(reference not in ids for task in tasks for reference in task.get("covers", [])):
        raise WorkspaceError("board task reference is missing")
    return value


def read_story_board():
    """Never discard the caller's independently read operations repair ledger."""
    result = {
        "state": "unavailable",
        "reason": "story_board_read_unavailable",
        "next_action": "Read repair_tasks again when repository access is restored. Preserve the existing checklist and repair history; do not invent a replacement.",
    }
    try:
        state = github.read_repository()["repository"]
        match = re.fullmatch(r"agent/issue-([1-9][0-9]*)", state["branch"])
        if (
            not match
            or os.environ.get("ISSUE_NUMBER") != match[1]
            or os.environ.get("TARGET_REPO", state["repository"]) != state["repository"]
        ):
            raise WorkspaceError("assigned story differs")
        issue = int(match[1])
        if type(state.get("repository_id")) is not int or state["repository_id"] < 1:
            raise WorkspaceError("repository identity is missing")
        path = f".adp/tasks/{issue}.json"
        root = Path(os.environ["WORK_DIR"])
        workspace = CodexWorkspace(
            root,
            provider="github",
            repository=state["repository"],
            source_revision=state["default_branch_head"],
        )
        result.update(
            path=path,
            issue=issue,
            repository=state["repository"],
            repository_id=state["repository_id"],
            branch=state["branch"],
        )
        with source_file(workspace, path) as (_, raw, digest):
            result["sha256"] = digest
            try:
                board = validate_board(json.loads(raw), issue)
            except (ValueError, TypeError, RecursionError):
                return {
                    **result,
                    "state": "invalid",
                    "reason": "story_board_invalid",
                    "next_action": "Read this file with repair_read and its expected_sha256. Resolve the board identity/schema problem through the story owner; preserve the saved tasks and do not replace them with a new checklist.",
                }
        # A mediated checkout has a synthetic local commit. Only the retained
        # publication receipt identifies the provider base; a newer remote head
        # must never be attributed to the bytes we just read.
        receipt_path = github._publication_state_path(str(root))
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("version") != 1 or not all(
            isinstance(receipt.get(key), str) and SHA.fullmatch(receipt[key])
            for key in ("head", "tree")
        ):
            raise WorkspaceError("source receipt is invalid")
        result.update(
            confirmed_source_revision=receipt["head"],
            current_branch_revision=state["branch_head"],
            source="authorized_workspace",
            progress_only=True,
        )
        # The digest identifies the local snapshot even after legitimate edits.
        # Do not claim those edits were in the confirmed provider commit.
        try:
            baseline = github._git(str(root), "show", f"{receipt['tree']}:{path}")
            result["modified_since_confirmed_source"] = baseline != raw
        except github.MediatedError:
            result["modified_since_confirmed_source"] = True
        compact = json.dumps(board, ensure_ascii=False, separators=(",", ":"))
        result.update(
            task_count=len(board["tasks"]),
            counts={
                status: sum(task["status"] == status for task in board["tasks"])
                for status in ("open", "in_progress", "done", "blocked")
            },
        )
        if len(compact.encode()) > MAX_CONTEXT_BYTES:
            return {
                **result,
                "state": "oversized",
                "reason": "story_board_requires_paged_read",
                "next_action": "Read every page of this file with repair_read, starting at offset 0 and passing expected_sha256. Import the complete board before replanning; this metadata is not a partial task list.",
            }
        return {
            **result,
            "state": "available",
            "reason": None,
            "board": board,
            "next_action": "Continue the saved tasks by stable ID. Preserve all statuses, parent links and evidence notes; reconcile claims against accepted criteria and current evidence. This board does not authorize work or prove release acceptance.",
        }
    except FileNotFoundError:
        # Only the board's absence permits a fresh checklist. A missing source
        # receipt after reading bytes is a provenance failure, not an empty board.
        if "sha256" not in result and "path" in result:
            return {
                **result,
                "state": "absent",
                "reason": "story_board_absent",
                "next_action": "No saved board exists for this assigned story. Build a checklist from accepted task_context and retained repair history; do not infer completed work.",
            }
        return result
    except (OSError, ValueError, KeyError, TypeError, RecursionError, github.MediatedError) as error:
        if (
            isinstance(error, WorkspaceError)
            and str(error) == "Repair source exceeds 256 KiB file bound"
        ):
            return {
                **result,
                "state": "oversized",
                "reason": "story_board_exceeds_file_bound",
                "next_action": "The saved file exceeds the protected reader's 256 KiB bound. Ask the story owner to reduce/archive excess board metadata through an authorized authoring path, then read repair_tasks again. Do not invent a replacement or write .adp through repair tools.",
            }
        return result
