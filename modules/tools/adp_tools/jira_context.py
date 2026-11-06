"""The bounded public contract for on-demand, Product-scoped Jira context."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTEXT_OPERATION = "get_project_context"
CONTEXT_TOOL = "atlassian___" + CONTEXT_OPERATION
READ_SCOPE = "read:jira:agent-interface"
SEARCH_SCOPE = "search:jira:agent-interface"


class JiraContextQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    product_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=255)
    source_id: str | None = Field(default=None, min_length=1, max_length=36)
    view: Literal["sources", "issues", "backlog", "board", "sprints"] = "sources"
    text: str | None = Field(default=None, min_length=1, max_length=500)
    issue_key: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$", max_length=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def scoped_read(self):
        if self.view != "sources" and not self.source_id:
            raise ValueError("Select a source returned by the sources view")
        if self.view == "sources" and any((self.source_id, self.text, self.issue_key, self.cursor)):
            raise ValueError("The sources view takes only Product and Project scope")
        if self.view in {"board", "sprints"} and any((self.text, self.issue_key)):
            raise ValueError("Issue filters require an issues or backlog view")
        return self


CONTEXT_DESCRIPTION = (
    "Read live Jira context for an ADP Product and Project using its approved shared connection. "
    "Start with view=sources, then select source_id to read issues, a Scrum backlog, board configuration, "
    "or sprints. Results include source and retrieval time. This tool never modifies Jira."
)
CONTEXT_SCHEMA = JiraContextQuery.model_json_schema()


def context_tool():
    return {"name": CONTEXT_OPERATION, "description": CONTEXT_DESCRIPTION, "inputSchema": CONTEXT_SCHEMA}


def jira_board_in_project(board: dict, project_id: str, project_key: str) -> bool:
    """Check the provider's location, whose detail and config shapes differ.

    Board details use projectId/projectKey. Configuration uses a typed
    projectKeyOrId, or the expanded id/key shown in Jira's REST example.
    Every supplied identity must agree; a user location is never a project.
    """
    location = board.get("location") if isinstance(board, dict) else None
    if not isinstance(location, dict) or location.get("type", "project") != "project":
        return False
    matches = []
    if "projectId" in location or "projectKey" in location:
        matches.append(
            type(location.get("projectId")) in (int, str)
            and str(location["projectId"]) == project_id
            and location.get("projectKey") == project_key
        )
    if "id" in location or "key" in location:
        matches.append(
            location.get("type") == "project"
            and type(location.get("id")) in (int, str)
            and str(location["id"]) == project_id
            and location.get("key") == project_key
        )
    if "projectKeyOrId" in location:
        matches.append(
            location.get("type") == "project"
            and isinstance(location["projectKeyOrId"], str)
            and location["projectKeyOrId"] in (project_id, project_key)
        )
    return bool(matches) and all(matches)


def jira_jql(project_ids: list[str], query: JiraContextQuery) -> str:
    """Build the entire query ourselves; public input is never executable JQL."""
    if not project_ids or any(not value.isascii() or not value.isdigit() or int(value) <= 0 for value in project_ids):
        raise ValueError("Verified numeric Jira project IDs required")
    clauses = ["project IN (" + ",".join(sorted(set(project_ids))) + ")"]
    if query.issue_key:
        clauses.append('key = "' + query.issue_key + '"')
    if query.text:
        # Escape both Lucene query operators and JQL's enclosing string.
        escaped = "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in query.text)
        escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
        clauses.append('text ~ "' + escaped + '"')
    return " AND ".join(clauses) + " ORDER BY Rank ASC"
