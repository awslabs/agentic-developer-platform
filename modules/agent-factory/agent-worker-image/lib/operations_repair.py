"""Operations repair tools over the existing workspace and mediated GitHub client."""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from lib import mediated_github as github
from lib.codex_workspace import CodexWorkspace, WorkspaceError
from lib.operations_workspace import read_window, replace_text
from lib.operations_source_refresh import refresh_source, stage_resolved_conflict, unmerged_paths


def validate_edit(repository, path, encoded):
    receipt = github._request({
        "operation": "validate_repair_edit", "repository": repository,
        "changes": [{"path": path, "content_base64": base64.b64encode(encoded).decode("ascii"), "mode": "100644"}],
    })
    if receipt.get("path") != path or receipt.get("sha256") != hashlib.sha256(encoded).hexdigest():
        raise github.MediatedRefused("repair validation receipt differs from edit")


def invoke(name, args):
    if os.getenv("ADP_ENGINE_OPERATIONS_STORY") != "true" or os.getenv("AGENT_TYPE") not in {"operations", "agent-codex-operations"}:
        raise github.MediatedRefused("not an operations story")
    # These gateway operations already reauthorize the current run. An extra
    # provider read is optional enrichment only; it must not discard repair history.
    if name == "repair_tasks":
        result = github._request({"operation": "read_repair_tasks"})
        if isinstance(result.get("operations_tasks"), dict):
            from lib.operations_task_board import read_story_board
            result = {**result, "story_board": read_story_board()}
        return result
    if name == "repair_progress":
        if args.get("diagnosis_code") is not None and args["stage"] != "diagnose":
            raise WorkspaceError("diagnosis_code is only accepted for stage diagnose; omit it for other stages")
        return github._request({"operation": "record_repair_task", "task_stage": args["stage"],
                                "task_state": args["state"], "task_cycle": args.get("cycle", 1), "diagnosis_code": args.get("diagnosis_code")})
    if name == "repair_status":
        return github._request({"operation": "read_repository", "pull_number": args["pull_number"]})
    if name == "repair_merge":
        return github._request({"operation": "merge_pull_request", **args})
    root = Path(os.environ["WORK_DIR"])
    # Local workspace access still requires a current, authorized repository read.
    state = github.read_repository()["repository"]
    workspace = CodexWorkspace(root, provider="github", repository=state["repository"],
                               source_revision=state["default_branch_head"])
    if name == "repair_read":
        return read_window(workspace, **args)
    if name == "repair_replace":
        result = replace_text(workspace, **args, validate=lambda path, content: validate_edit(state["repository"], path, content))
        stage_resolved_conflict(str(root), args["path"])
        return result
    if name == "repair_files":
        return workspace.list_files(**args)
    if name == "repair_write":
        content = args.get("content")
        if not isinstance(content, str):
            raise github.MediatedRefused("repair content must be UTF-8 text")
        encoded = content.encode("utf-8")
        validate_edit(state["repository"], args.get("path"), encoded)
        result = workspace.write_file(**args)
        stage_resolved_conflict(str(root), args["path"])
        return result
    if name == "repair_publish":
        # A repeated call after a lost response can reuse the confirmed local receipt.
        conflicts = unmerged_paths(str(root))
        if conflicts:
            return {"published": False, "conflicts": conflicts,
                    "next_action": "Resolve the source conflicts with repair_write or repair_replace, run the required checks, then publish again."}
        confirmed_head, base, _ = github._publication_base(str(root))
        refreshed = refresh_source(str(root), state, confirmed_head=confirmed_head, base=base)
        conflicts = unmerged_paths(str(root))
        if conflicts:
            return {"published": False, "workspace_refreshed": True, "conflicts": conflicts,
                    "next_action": "Resolve the source conflicts with repair_write or repair_replace, run the required checks, then publish again."}
        confirmed_head, base, _ = github._publication_base(str(root))
        changes = github.collect_changes(str(root), base=base)
        if changes:
            published_tree = github._git(str(root), "write-tree").strip()
            published = github._request({"operation": "publish_commit", "message": args["title"],
                "expected_head": confirmed_head if state["branch_head"] is not None else None,
                "changes": [change.payload() for change in changes]})
            github._record_publication(github._publication_state_path(str(root)), head=published["commit_sha"],
                                       tree=published_tree)
        pull = github.upsert_pull_request(title=args["title"], body=args["body"])
        return {"pull_request": pull, "workspace_refreshed": refreshed}
    raise github.MediatedRefused("unknown repair tool")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("one repair tool is required")
    body = sys.stdin.read(65537)
    if len(body.encode()) > 65536:
        raise SystemExit("repair input exceeds bound")
    try:
        result = invoke(sys.argv[1], json.loads(body))
    except (WorkspaceError, github.MediatedError, FileNotFoundError, FileExistsError) as exc:
        result = {"error": str(exc)}
    # Both operations stacks share a 256 KiB startup transport. Preserve UTF-8
    # instead of expanding Unicode into escapes; never truncate tasks/history.
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if sys.argv[1] == "repair_tasks" and len(encoded.encode("utf-8")) + 1 > 256 * 1024:
        next_action = "The complete story board, private checkpoint and repair history exceed the combined restore bound. Preserve the existing branch and run activity; ask the platform operator to reconcile or page the retained history before resuming. No partial tasks or evidence were returned."
        encoded = json.dumps({
            "error": "story_restore_response_oversized",
            "story_board": {"state": "unavailable", "reason": "story_restore_response_oversized", "next_action": next_action},
            "story_checkpoint": {"state": "unavailable", "reason": "story_restore_response_oversized"},
            "next_action": next_action,
        }, separators=(",", ":"))
    print(encoded)


if __name__ == "__main__":
    main()
