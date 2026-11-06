"""Independent schema-pinned MCP observations through an existing owner Identity grant."""

import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

from modules.tools.adapters.observation_configuration import _catalogue
from tool_adapters.atlassian import ReadMCPTransport
from tool_adapters.atlassian_observe import _observation_value, collect_observation

from tests.e2e.tools.atlassian_client_matrix import (
    ACCOUNT_ID,
    HANDLE,
    SHA256,
    IndependentProviderReader,
    digest,
)
from tests.e2e.tools.atlassian_oauth_probe import CONSENT_SCOPES
from tests.e2e.tools.atlassian_setup_observer import _aws

TOOLS = {
    "issue": "getJiraIssue",
    "search": "searchJiraIssuesUsingJql",
    "comments": "listJiraIssueComments",
    "changelog": "listJiraIssueChangelogs",
    "page": "getConfluenceContent",
}
FIELDS = {
    "account",
    "region",
    "environment",
    "adapter_role_arn",
    "adapter_label",
    "workload_name",
    "provider_name",
    "provider_account_id",
    "site_ids",
    "subject_schema_sha256",
    "sites_schema_sha256",
    "calls",
}
SITE_BINDING = {"site_id": ("cloudId",)}
ISSUE_BINDING = {**SITE_BINDING, "provider_id": ("issueIdOrKey", "id")}
PAGINATION_BINDING = {"start_at": ("startAt",), "max_results": ("maxResults",)}
CALL_BINDINGS = {
    "issue": ISSUE_BINDING,
    "search": {**SITE_BINDING, "jql": ("jql",), "max_results": ("maxResults",)},
    "comments": {**ISSUE_BINDING, **PAGINATION_BINDING},
    "changelog": {**ISSUE_BINDING, **PAGINATION_BINDING},
    "page": {**SITE_BINDING, "provider_id": ("id", "contentId", "pageId")},
}


def argument_bindings(operation, template, schema=None):
    if type(template) is not dict:
        raise ValueError("Provider argument binding unavailable")
    bindings = {}
    for fixture, candidates in CALL_BINDINGS[operation].items():
        selected = [name for name in candidates if name in template]
        if len(selected) != 1 or template[selected[0]] != {"fixture": fixture}:
            raise ValueError("Provider argument binding must use the requested fixture")
        bindings[selected[0]] = fixture
    if set(template) != set(bindings):
        raise ValueError("Provider argument binding contains an unqualified parameter")
    if schema is not None:
        properties = schema["properties"]
        if not set(schema.get("required", [])) <= set(bindings) <= set(properties):
            raise ValueError(
                "Provider argument binding differs from the discovered schema"
            )
        for name, fixture in bindings.items():
            scalar = "integer" if fixture in {"start_at", "max_results"} else "string"
            if (
                type(properties[name]) is not dict
                or properties[name].get("type") != scalar
            ):
                raise ValueError(
                    "Provider argument binding has an unqualified schema type"
                )
    return bindings


def object_result(value):
    if type(value) is not dict or "error" in value:
        raise ValueError("Independent provider result unavailable")
    return value


class IdentityReadbackGrant:
    def __init__(self, config, owner_id):
        if (
            type(config) is not dict
            or set(config) != FIELDS
            or not isinstance(owner_id, str)
            or not HANDLE.fullmatch(owner_id)
            or not isinstance(config["account"], str)
            or not re.fullmatch(r"[0-9]{12}", config["account"])
            or not isinstance(config["region"], str)
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", config["region"])
            or config["environment"] not in {"dev", "test", "prod"}
            or config["adapter_role_arn"]
            != f"arn:aws:iam::{config['account']}:role/adp-{config['environment']}-atlassian-mcp-observer-adapter"
            or config["adapter_label"] != "oidc-adapter"
            or config["workload_name"]
            != f"adp-tools-{config['environment']}-atlassian-mcp-oauth-user"
            or config["provider_name"]
            != f"adp-tools-{config['environment']}-atlassian-mcp-user"
            or not isinstance(config["provider_account_id"], str)
            or not ACCOUNT_ID.fullmatch(config["provider_account_id"])
            or type(config["site_ids"]) is not list
            or not 0 < len(config["site_ids"]) <= 32
            or any(
                not isinstance(site, str) or not HANDLE.fullmatch(site)
                for site in config["site_ids"]
            )
            or len(set(config["site_ids"])) != len(config["site_ids"])
            or any(
                not isinstance(config[key], str) or not SHA256.fullmatch(config[key])
                for key in ("subject_schema_sha256", "sites_schema_sha256")
            )
            or type(config["calls"]) is not dict
            or set(config["calls"]) != set(TOOLS)
        ):
            raise ValueError("An isolated owner-bound MCP read grant is required")
        for operation, call in config["calls"].items():
            if (
                type(call) is not dict
                or set(call) != {"schema_sha256", "arguments"}
                or not isinstance(call["schema_sha256"], str)
                or not SHA256.fullmatch(call["schema_sha256"])
                or type(call["arguments"]) is not dict
            ):
                raise ValueError(
                    "Discovered provider schemas and explicit read arguments are required"
                )
            argument_bindings(operation, call["arguments"])
        self.config, self.owner_id = config, owner_id

    @staticmethod
    def _token(response, field):
        value = response.get(field) if isinstance(response, dict) else None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 16384
            or any(character.isspace() for character in value)
            or response.get("sessionStatus") in {"IN_PROGRESS", "FAILED"}
        ):
            raise ValueError("The owner's existing Identity MCP grant is unavailable")
        return value

    def with_access_token(self, owner_id, observe):
        if owner_id != self.owner_id or not callable(observe):
            raise ValueError("Independent readback owner differs")
        config = self.config
        workload = _aws(
            config,
            "adapter",
            "bedrock-agentcore",
            "get-workload-access-token-for-user-id",
            input_value={"workloadName": config["workload_name"], "userId": owner_id},
        )
        result = _aws(
            config,
            "adapter",
            "bedrock-agentcore",
            "get-resource-oauth2-token",
            input_value={
                "workloadIdentityToken": self._token(workload, "workloadAccessToken"),
                "resourceCredentialProviderName": config["provider_name"],
                "scopes": list(CONSENT_SCOPES),
                "resources": ["https://mcp.atlassian.com/v2/mcp"],
                "oauth2Flow": "USER_FEDERATION",
            },
        )
        token = self._token(result, "accessToken")
        settings = SimpleNamespace(
            **{
                key: config[key]
                for key in ("subject_schema_sha256", "sites_schema_sha256")
            }
        )
        subject, sites, _ = collect_observation(token, settings)
        if subject != config["provider_account_id"] or any(
            not any(
                site.site_id == expected
                and {
                    "read:jira:agent-interface",
                    "read:confluence:agent-interface",
                    "search:jira:agent-interface",
                }
                <= set(site.scopes)
                for site in sites
            )
            for expected in config["site_ids"]
        ):
            raise ValueError("Independent MCP grant subject, site or scopes differ")
        return observe(MCPReadSession(config, token, subject))

    def reader(self):
        self.with_access_token(self.owner_id, lambda session: None)
        return BoundProviderReader(self, self.owner_id)


class MCPReadSession:
    def __init__(self, config, token, subject):
        self.config, self.token, self.subject = config, token, subject
        self.deadline = time.monotonic() + 30
        self.transport = ReadMCPTransport(deadline=self.deadline)
        tools = _catalogue(token, transport_factory=lambda **kwargs: self.transport)
        self.catalogue = tools
        self.schemas = {}
        for operation, name in TOOLS.items():
            schema = tools.get(name, {}).get("inputSchema")
            if (
                type(schema) is not dict
                or schema.get("type") != "object"
                or type(schema.get("properties")) is not dict
                or digest(schema) != config["calls"][operation]["schema_sha256"]
            ):
                raise ValueError("Independent provider schema differs")
            argument_bindings(
                operation, config["calls"][operation]["arguments"], schema
            )
            self.schemas[operation] = schema

    def get(self, path):
        parsed = urlsplit(path)
        match = re.fullmatch(r"/ex/(jira|confluence)/([^/]+)/(.*)", parsed.path)
        if (
            match is None
            or unquote(match[2]) not in self.config["site_ids"]
            or time.monotonic() >= self.deadline
        ):
            raise ValueError(
                "Independent read is outside the selected site or deadline"
            )
        values = {"site_id": unquote(match[2])}
        query = parse_qs(parsed.query, strict_parsing=True)
        if any(len(value) != 1 for value in query.values()):
            raise ValueError("Independent query is ambiguous")
        if match[1] == "jira" and match[3] == "rest/api/3/search/jql":
            operation = "search"
            values.update(jql=query["jql"][0], max_results=int(query["maxResults"][0]))
        elif match[1] == "jira":
            issue = re.fullmatch(
                r"rest/api/3/issue/([^/]+)(?:/(comment|changelog))?", match[3]
            )
            if issue is None:
                raise ValueError("Unsupported independent Jira read")
            operation = {
                None: "issue",
                "comment": "comments",
                "changelog": "changelog",
            }[issue[2]]
            values["provider_id"] = unquote(issue[1])
            if issue[2]:
                values.update(
                    start_at=int(query["startAt"][0]),
                    max_results=int(query["maxResults"][0]),
                )
        else:
            page = re.fullmatch(r"wiki/api/v2/pages/([^/]+)", match[3])
            if page is None:
                raise ValueError("Unsupported independent Confluence read")
            operation = "page"
            values["provider_id"] = unquote(page[1])
        schema = self.schemas[operation]
        bindings = argument_bindings(
            operation, self.config["calls"][operation]["arguments"], schema
        )
        payload = {name: values[fixture] for name, fixture in bindings.items()}
        response = self.transport.send(
            "tools/call", {"name": TOOLS[operation], "arguments": payload}, self.token
        )
        if type(response) is not dict or "error" in response:
            raise ValueError("Independent provider read failed")
        return _observation_value(response.get("result"), object_result)


class BoundProviderReader(IndependentProviderReader):
    def _authorize(self, session, site_id, product):
        if site_id not in self.lifecycle.config["site_ids"]:
            raise ValueError("Readback site is outside the protected fixture")
        return session.subject

    @staticmethod
    def _get(path, session):
        return session.get(path)
