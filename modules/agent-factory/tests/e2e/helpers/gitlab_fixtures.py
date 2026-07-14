"""
GitLab webhook test fixtures — sample payloads and expected SQS messages.

Provides factory functions for constructing realistic GitLab webhook payloads
matching the schemas consumed by the gitlab Lambda handler + event_parser.
"""

from __future__ import annotations


def gitlab_note_payload(
    project_id: int = 100,
    project_path: str = "test-group/test-repo",
    issue_iid: int = 1,
    note_id: int = 999,
    note_body: str = "@agent hello from E2E test",
    username: str = "e2e-tester",
    user_name: str = "E2E Tester",
    gitlab_url: str = "http://gitlab.example.com",
) -> dict:
    """Build a GitLab note webhook payload (note on issue).

    This is the primary actionable event type — a comment on an issue
    containing an @agent mention.
    """
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {
            "username": username,
            "name": user_name,
            "avatar_url": f"{gitlab_url}/uploads/-/system/user/avatar/1/avatar.png",
        },
        "project": {
            "id": project_id,
            "name": "test-repo",
            "path_with_namespace": project_path,
            "web_url": f"{gitlab_url}/{project_path}",
        },
        "object_attributes": {
            "id": note_id,
            "note": note_body,
            "noteable_type": "Issue",
            "noteable_id": issue_iid,
            "url": f"{gitlab_url}/{project_path}/-/issues/{issue_iid}#note_{note_id}",
        },
        "issue": {
            "iid": issue_iid,
            "title": "E2E test issue",
            "state": "opened",
        },
    }


def gitlab_push_payload(
    project_id: int = 100,
    project_path: str = "test-group/test-repo",
    gitlab_url: str = "http://gitlab.example.com",
) -> dict:
    """Build a GitLab push webhook payload (non-actionable event)."""
    return {
        "object_kind": "push",
        "event_name": "push",
        "ref": "refs/heads/main",
        "project": {
            "id": project_id,
            "name": "test-repo",
            "path_with_namespace": project_path,
            "web_url": f"{gitlab_url}/{project_path}",
        },
        "commits": [
            {
                "id": "abc123def456",
                "message": "test commit",
                "author": {"name": "E2E Tester", "email": "test@example.com"},
            }
        ],
    }


def gitlab_mr_note_payload(
    project_id: int = 100,
    project_path: str = "test-group/test-repo",
    note_body: str = "@agent review this MR",
    gitlab_url: str = "http://gitlab.example.com",
) -> dict:
    """Build a GitLab note payload on a MergeRequest (should be ignored).

    The current handler only processes notes on Issues, not MergeRequests.
    """
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {
            "username": "e2e-tester",
            "name": "E2E Tester",
        },
        "project": {
            "id": project_id,
            "name": "test-repo",
            "path_with_namespace": project_path,
            "web_url": f"{gitlab_url}/{project_path}",
        },
        "object_attributes": {
            "id": 1001,
            "note": note_body,
            "noteable_type": "MergeRequest",
            "noteable_id": 5,
        },
        "merge_request": {
            "iid": 5,
            "title": "Test MR",
            "state": "opened",
        },
    }


def expected_sqs_envelope_shape(
    project_id: int = 100,
    project_path: str = "test-group/test-repo",
    issue_iid: int = 1,
    persona: str = "agent",
    username: str = "e2e-tester",
) -> dict:
    """Return the expected shape of an SQS message envelope for assertions.

    Only checks key fields — timestamps and UUIDs are dynamic.
    """
    return {
        "version": "1.0",
        "channel": "gitlab",
        "persona": persona,
        "payload": {
            "provider": "gitlab",
            "event_type": "mention",
            "source": {
                "project_id": project_id,
                "project_path": project_path,
                "issue_iid": issue_iid,
            },
            "actor": {
                "username": username,
            },
            "content": {
                "mention_target": persona,
            },
        },
    }
