"""Execute only legacy verbs against a core-verified connection snapshot."""

from fastapi import HTTPException

from tool_adapters.legacy_catalogue import LEGACY_SCOPES
from tool_adapters.legacy_github import observe_github_user
from tool_adapters.legacy_jira import observe_jira_sites, observe_jira_user, read_jira_issue, select_jira_site
from tool_adapters.legacy_slack import SlackIdentityObservation, read_slack_user
from tool_adapters.legacy_slack_post import approved_post_fixture, fixture_text, post_slack_fixture


def _deny():
    raise HTTPException(403, detail={"error": "connection_changed"})


async def execute_legacy(token: str, connector: str, operation: str, arguments: dict, protected: dict) -> dict:
    if (
        type(connector) is not str
        or type(operation) is not str
        or connector not in LEGACY_SCOPES
        or operation not in LEGACY_SCOPES[connector]
        or type(arguments) is not dict
        or type(protected) is not dict
        or type(protected.get("account_id")) is not str
        or type(protected.get("scopes")) is not list
    ):
        _deny()
    scopes = protected["scopes"]
    if connector == "github":
        if arguments:
            raise HTTPException(400, detail={"error": "unsupported_operation"})
        app_id = protected.get("app_id")
        if app_id is not None:
            raise HTTPException(403, detail={"error": "github_app_grant_unverified"})
        else:
            if "read:user" not in scopes:
                _deny()
            observation = await observe_github_user(token)
            if list(observation.scopes) != scopes:
                _deny()
        if (
            observation.account_id != protected["account_id"]
            or str(observation.profile.get("id")) != observation.account_id
        ):
            _deny()
        return observation.profile
    if LEGACY_SCOPES[connector][operation] not in scopes:
        _deny()
    if connector == "jira":
        if set(arguments) != {"cloudId", "issueIdOrKey"}:
            raise HTTPException(400, detail={"error": "unsupported_operation"})
        cloud_id, issue = arguments["cloudId"], arguments["issueIdOrKey"]
        sites = protected.get("sites")
        if type(sites) is not tuple:
            _deny()
        expected = select_jira_site(sites, cloud_id)
        observation = await observe_jira_user(token)
        if observation.account_id != protected["account_id"]:
            _deny()
        current = select_jira_site(await observe_jira_sites(token), cloud_id)
        if current != expected:
            _deny()
        return await read_jira_issue(token, protected_site=current, cloud_id=cloud_id, issue_id_or_key=issue)
    user_id, team_id = protected.get("user_id"), protected.get("team_id")
    if type(user_id) is not str or type(team_id) is not str or protected["account_id"] != team_id + ":" + user_id:
        _deny()
    identity = SlackIdentityObservation(user_id, team_id, tuple(scopes))
    if operation == "get_user":
        if set(arguments) != {"user"}:
            raise HTTPException(400, detail={"error": "unsupported_operation"})
        return await read_slack_user(token, protected_identity=identity, user_id=arguments["user"])
    if set(arguments) != {"channel", "text", "run_marker"} or type(protected.get("owner_id")) is not str:
        raise HTTPException(400, detail={"error": "unsupported_operation"})
    fixture = approved_post_fixture(owner_id=protected["owner_id"], team_id=team_id)
    if arguments != {
        "channel": fixture.channel_id,
        "text": fixture_text(fixture, fixture.run_marker),
        "run_marker": fixture.run_marker,
    }:
        _deny()
    return await post_slack_fixture(
        token,
        protected_identity=identity,
        fixture=fixture,
        owner_id=protected["owner_id"],
        channel_id=arguments["channel"],
        run_marker=arguments["run_marker"],
        text=arguments["text"],
    )
