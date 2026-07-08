"""GitLab event type detection and payload extraction.

Parses GitLab webhook payloads to detect note events on issues and extract
@-mention patterns for agent dispatch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Pattern to detect @agent mentions in note bodies.
# Matches @agent or @agent-<persona> (e.g. @agent-developer, @agent-reviewer).
MENTION_PATTERN = re.compile(r"@agent(?:-([a-zA-Z0-9_-]+))?")


@dataclass
class ParsedGitLabEvent:
    """Parsed result from a GitLab webhook event."""

    is_actionable: bool
    event_type: str  # "mention" or ""
    project_id: int
    project_path: str
    issue_iid: int | None
    note_id: int | None
    author_username: str
    author_name: str
    body: str
    mention_target: str  # The persona after @agent- (or "agent" if bare @agent)
    gitlab_url: str
    reason: str  # Why the event was skipped (empty if actionable)


def parse_event(payload: dict[str, Any]) -> ParsedGitLabEvent:
    """Parse a GitLab webhook payload and determine if it's actionable.

    An event is actionable if:
    1. object_kind == "note"
    2. The note is on an issue (not MR, snippet, or commit)
    3. The note body contains an @agent mention

    Args:
        payload: Parsed JSON body from the GitLab webhook.

    Returns:
        ParsedGitLabEvent with is_actionable=True if the event should be
        forwarded to SQS, False otherwise.
    """
    object_kind = payload.get("object_kind", "")

    # Extract project info (present in all webhook events)
    project = payload.get("project", {})
    project_id = project.get("id", 0)
    project_path = project.get("path_with_namespace", "")
    gitlab_url = project.get("web_url", "")

    # Only handle note events
    if object_kind != "note":
        return ParsedGitLabEvent(
            is_actionable=False,
            event_type="",
            project_id=project_id,
            project_path=project_path,
            issue_iid=None,
            note_id=None,
            author_username="",
            author_name="",
            body="",
            mention_target="",
            gitlab_url=gitlab_url,
            reason=f"unsupported object_kind: {object_kind}",
        )

    # Check that the note is on an issue (not MR, snippet, or commit)
    note_attrs = payload.get("object_attributes", {})
    noteable_type = note_attrs.get("noteable_type", "")

    if noteable_type != "Issue":
        return ParsedGitLabEvent(
            is_actionable=False,
            event_type="",
            project_id=project_id,
            project_path=project_path,
            issue_iid=None,
            note_id=note_attrs.get("id"),
            author_username="",
            author_name="",
            body="",
            mention_target="",
            gitlab_url=gitlab_url,
            reason=f"note on {noteable_type}, not Issue",
        )

    # Extract note details
    note_body = note_attrs.get("note", "")
    note_id = note_attrs.get("id", 0)

    # Extract issue IID
    issue = payload.get("issue", {})
    issue_iid = issue.get("iid")

    # Extract author
    user = payload.get("user", {})
    author_username = user.get("username", "")
    author_name = user.get("name", "")

    # Check for @agent mention
    match = MENTION_PATTERN.search(note_body)
    if not match:
        return ParsedGitLabEvent(
            is_actionable=False,
            event_type="",
            project_id=project_id,
            project_path=project_path,
            issue_iid=issue_iid,
            note_id=note_id,
            author_username=author_username,
            author_name=author_name,
            body=note_body,
            mention_target="",
            gitlab_url=gitlab_url,
            reason="no @agent mention found in note body",
        )

    # Determine the mention target (persona)
    persona_suffix = match.group(1)
    mention_target = persona_suffix if persona_suffix else "agent"

    return ParsedGitLabEvent(
        is_actionable=True,
        event_type="mention",
        project_id=project_id,
        project_path=project_path,
        issue_iid=issue_iid,
        note_id=note_id,
        author_username=author_username,
        author_name=author_name,
        body=note_body,
        mention_target=mention_target,
        gitlab_url=gitlab_url,
        reason="",
    )
