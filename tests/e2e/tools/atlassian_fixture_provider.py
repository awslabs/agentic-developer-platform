"""Separately consented MCP capability for run-owned Jira fixture cleanup."""

import re
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from tool_adapters.atlassian_observe import _observation_value, collect_observation
from tool_adapters.atlassian import WriteMCPTransport

from tests.e2e.tools.atlassian_client_matrix import HANDLE, SHA256, digest
from tests.e2e.tools.atlassian_oauth_probe import CONSENT_SCOPES, RESOURCE
from tests.e2e.tools.atlassian_readback_grant import (
    IdentityReadbackGrant,
    MCPReadSession,
    object_result,
    argument_bindings,
)
from tests.e2e.tools.atlassian_setup_observer import _aws


DELETE_SCOPE = "delete:jira:agent-interface"
FIELDS = {
    "enabled",
    "tenant_id",
    "site_id",
    "project_key",
    "delete_call",
    "projects_call",
}


class JiraFixtureProvider:
    def __init__(self, config, *, readback, owner_id, run_id):
        self.grant = IdentityReadbackGrant(readback, owner_id)
        if (
            type(config) is not dict
            or set(config) != FIELDS
            or config["enabled"] is not True
            or any(
                not isinstance(value, str) or not HANDLE.fullmatch(value)
                for value in (run_id, config["tenant_id"], config["site_id"])
            )
            or not isinstance(config["project_key"], str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", config["project_key"]) is None
            or config["site_id"] not in readback["site_ids"]
        ):
            raise ValueError(
                "Separately enabled, owner-bound Jira cleanup capability required"
            )
        for name in ("delete_call", "projects_call"):
            call = config[name]
            if (
                type(call) is not dict
                or set(call) != {"schema_sha256", "arguments"}
                or not isinstance(call["schema_sha256"], str)
                or not SHA256.fullmatch(call["schema_sha256"])
                or type(call["arguments"]) is not dict
            ):
                raise ValueError(
                    "Observed cleanup and project-read schemas are required"
                )
        self.config = {
            **config,
            "readback": readback,
            "provider_account_id": readback["provider_account_id"],
        }
        self.owner_id, self.run_id = owner_id, run_id
        self.identity = {
            "owner_id": owner_id,
            "run_id": run_id,
            **{key: config[key] for key in ("tenant_id", "site_id", "project_key")},
        }
        self.prefix = "/ex/jira/" + quote(config["site_id"], safe="") + "/rest/api/3"
        self.marker = None

    def _arguments(self, session, operation, provider_id=None):
        name = "deleteJiraIssue" if operation == "delete_call" else "listJiraProjects"
        call = self.config[operation]
        schema = session.catalogue.get(name, {}).get("inputSchema")
        if (
            type(schema) is not dict
            or digest(schema) != call["schema_sha256"]
            or schema.get("type") != "object"
            or type(schema.get("properties")) is not dict
            or not set(schema.get("required", []))
            <= set(call["arguments"])
            <= set(schema["properties"])
        ):
            raise ValueError("Cleanup capability differs from its observed schema")
        values = {
            "site_id": self.identity["site_id"],
            "project_key": self.identity["project_key"],
            "provider_id": provider_id,
        }
        arguments = {}
        for key, value in call["arguments"].items():
            if (
                type(value) is dict
                and set(value) == {"fixture"}
                and value["fixture"] in values
            ):
                value = values[value["fixture"]]
            if value is None or type(value) not in {str, int, bool}:
                raise ValueError("Bounded cleanup capability arguments are required")
            expected_type = {str: "string", int: "integer", bool: "boolean"}[
                type(value)
            ]
            if schema["properties"][key].get("type") != expected_type:
                raise ValueError(
                    "Cleanup argument type differs from its observed schema"
                )
            arguments[key] = value
        if call["arguments"].get("cloudId") != {"fixture": "site_id"}:
            raise ValueError("Cleanup must remain bound to the original site")
        if operation == "delete_call":
            ids = [key for key in ("issueIdOrKey", "issueId", "id") if key in arguments]
            if (
                len(ids) != 1
                or call["arguments"][ids[0]] != {"fixture": "provider_id"}
                or set(arguments) - {"cloudId", ids[0], "deleteSubtasks"}
                or arguments.get("deleteSubtasks", False) is not False
            ):
                raise ValueError(
                    "Only the exact owned issue may be deleted, without cascading"
                )
        return name, arguments

    def _call(self, session, operation, provider_id=None):
        name, arguments = self._arguments(session, operation, provider_id)
        transport = session.transport
        if operation == "delete_call":
            transport = WriteMCPTransport(deadline=session.transport.deadline)
            transport.session_id, transport.request_id = (
                session.transport.session_id,
                session.transport.request_id,
            )
        try:
            response = transport.send(
                "tools/call", {"name": name, "arguments": arguments}, session.token
            )
        finally:
            if operation == "delete_call":
                session.transport.session_id, session.transport.request_id = (
                    transport.session_id,
                    transport.request_id,
                )
        if type(response) is not dict or "error" in response:
            raise ValueError("Jira cleanup capability call failed")
        return _observation_value(response.get("result"), object_result)

    def _with_session(self, action):
        config = self.grant.config
        workload = _aws(
            config,
            "adapter",
            "bedrock-agentcore",
            "get-workload-access-token-for-user-id",
            input_value={
                "workloadName": config["workload_name"],
                "userId": self.owner_id,
            },
        )
        response = _aws(
            config,
            "adapter",
            "bedrock-agentcore",
            "get-resource-oauth2-token",
            input_value={
                "workloadIdentityToken": self.grant._token(
                    workload, "workloadAccessToken"
                ),
                "resourceCredentialProviderName": config["provider_name"],
                "scopes": [*CONSENT_SCOPES, DELETE_SCOPE],
                "resources": [RESOURCE],
                "oauth2Flow": "USER_FEDERATION",
            },
        )
        token = self.grant._token(response, "accessToken")
        subject, sites, _ = collect_observation(
            token,
            SimpleNamespace(
                **{
                    key: config[key]
                    for key in ("subject_schema_sha256", "sites_schema_sha256")
                }
            ),
        )
        if subject != config["provider_account_id"] or not any(
            site.site_id == self.identity["site_id"]
            and DELETE_SCOPE in site.scopes
            and "read:jira:agent-interface" in site.scopes
            for site in sites
        ):
            raise ValueError(
                "Actual separately consented delete scope for this owner and site is required"
            )
        session = MCPReadSession(config, token, subject)
        self._arguments(session, "delete_call", "owned-issue-schema-check")
        projects = self._call(session, "projects_call")
        if (
            projects.get("isLast") is not True
            or type(projects.get("values")) is not list
            or len(projects["values"]) > 100
            or not any(
                type(project) is dict
                and project.get("key") == self.identity["project_key"]
                for project in projects["values"]
            )
        ):
            raise ValueError(
                "The original Jira project must remain independently visible"
            )
        return action(session)

    def authorize(self):
        self._with_session(lambda session: None)

    def bind(self, record):
        if (
            any(record.get(key) != value for key, value in self.identity.items())
            or not isinstance(record.get("marker"), str)
            or re.fullmatch(r"adp-jira-[A-Za-z0-9_-]{43}", record["marker"]) is None
        ):
            raise ValueError("Original owned Jira fixture reservation required")
        self.marker = record["marker"]

    def _owned(self, issue):
        fields = issue.get("fields") if type(issue) is dict else None
        if (
            self.marker is None
            or type(fields) is not dict
            or not isinstance(issue.get("id"), str)
            or not HANDLE.fullmatch(issue["id"])
            or type(fields.get("project")) is not dict
            or fields["project"].get("key") != self.identity["project_key"]
            or type(fields.get("creator")) is not dict
            or fields["creator"].get("accountId") != self.config["provider_account_id"]
            or not isinstance(fields.get("summary"), str)
            or self.marker not in fields["summary"]
        ):
            raise ValueError(
                "Provider fixture ownership differs from the durable reservation"
            )
        return {**self.identity, "marker": self.marker, "id": issue["id"]}

    def _search(self, session, query):
        result = session.get(
            self.prefix + "/search/jql?" + urlencode({"jql": query, "maxResults": 2})
        )
        if (
            type(result) is not dict
            or result.get("isLast") is not True
            or type(result.get("issues")) is not list
            or len(result["issues"]) > 1
        ):
            raise ValueError(
                "Exactly zero or one owned Jira fixture must be independently observable"
            )
        return result["issues"]

    def read_issue(self, issue_id, *, site_id):
        if (
            site_id != self.identity["site_id"]
            or not isinstance(issue_id, str)
            or not HANDLE.fullmatch(issue_id)
        ):
            raise ValueError("Jira fixture site or ID differs")

        def observe(session):
            bindings = argument_bindings(
                "issue",
                session.config["calls"]["issue"]["arguments"],
                session.schemas["issue"],
            )
            values = {"site_id": site_id, "provider_id": issue_id}
            response = session.transport.send(
                "tools/call",
                {
                    "name": "getJiraIssue",
                    "arguments": {
                        name: values[fixture] for name, fixture in bindings.items()
                    },
                },
                session.token,
            )
            if type(response) is not dict:
                raise ValueError("Direct known-ID read is unavailable")
            error = response.get("error")
            if type(error) is dict:
                data = error.get("data")
                if (
                    type(data) is dict
                    and type(data.get("status")) is int
                    and data["status"] == 404
                    and data.get("issueIdOrKey") == issue_id
                ):
                    return None
                raise ValueError("Direct known-ID read is denied or unconfirmed")
            if "error" in response:
                raise ValueError("Direct known-ID read is unconfirmed")
            result = self._owned(
                _observation_value(response.get("result"), object_result)
            )
            if result["id"] != issue_id:
                raise ValueError("Direct fixture read returned another issue")
            return result

        return self._with_session(observe)

    def read_issues(self, *, site_id, project_key, marker):
        if (site_id, project_key, marker) != (
            self.identity["site_id"],
            self.identity["project_key"],
            self.marker,
        ) or self.marker is None:
            raise ValueError("Jira fixture search differs from its reservation")
        return self._with_session(
            lambda session: {
                "complete": True,
                "issues": [
                    self._owned(
                        session.get(
                            self.prefix + "/issue/" + quote(str(issue["id"]), safe="")
                        )
                    )
                    for issue in self._search(
                        session, f'project = "{project_key}" AND text ~ "{marker}"'
                    )
                ],
            }
        )

    def delete_owned_issue(self, issue_id, **ownership):
        if (
            ownership != {**self.identity, "marker": self.marker}
            or not isinstance(issue_id, str)
            or not HANDLE.fullmatch(issue_id)
        ):
            raise ValueError("Unowned Jira deletion refused")

        def remove(session):
            current = self._owned(
                session.get(self.prefix + "/issue/" + quote(issue_id, safe=""))
            )
            if current != {**ownership, "id": issue_id}:
                raise ValueError("Jira fixture changed before deletion")
            self._call(session, "delete_call", issue_id)

        self._with_session(remove)
