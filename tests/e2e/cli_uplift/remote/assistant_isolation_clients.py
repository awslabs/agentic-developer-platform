"""Ordinary-user fixture and identity checks for the E45 harness driver."""

if __package__:
    from . import assistant_client, common
else:
    import assistant_client
    import common


ACTORS = ("a1", "a2", "b1")


def baseline():
    if __package__:
        from . import dispatcher

        module, _ = dispatcher.load("assistant_baseline")
        return module
    import assistant_baseline

    return assistant_baseline


def fixture_sessions(config):
    fixtures = config.get("assistant_users") or {}
    for actor in ACTORS:
        fixture = fixtures.get(actor)
        common.require(
            isinstance(fixture, dict)
            and all(
                isinstance(fixture.get(key), str) and fixture[key]
                for key in (
                    "fixture_name",
                    "canonical_user_id",
                    "tenant_id",
                    "login_user_id",
                )
            ),
            f"Assistant isolation requires an {actor.upper()} ordinary-user fixture",
        )
    common.require(
        len({fixtures[actor]["canonical_user_id"] for actor in ACTORS}) == 3
        and fixtures["a1"]["tenant_id"] == fixtures["a2"]["tenant_id"]
        and fixtures["b1"]["tenant_id"] != fixtures["a1"]["tenant_id"]
        and len({fixtures[actor]["fixture_name"] for actor in ACTORS}) == 3,
        "Assistant isolation requires distinct users in two tenants",
    )
    return {actor: baseline().session_for(config, fixtures[actor]) for actor in ACTORS}


def verify_identities(config, sessions):
    gateway = config["gateway_url"].rstrip("/")
    fixtures = config["assistant_users"]
    for actor in ACTORS:
        fixture = fixtures[actor]
        session = sessions[actor]
        principal = assistant_client.http_json(gateway + "/auth/me", session)
        common.require(
            isinstance(principal, dict)
            and principal.get("account_type") == "human"
            and principal.get("is_admin") is False
            and principal.get("user_id")
            in {fixture["login_user_id"], fixture["canonical_user_id"]}
            and principal.get("org_id") == fixture["tenant_id"],
            f"Assistant isolation {actor.upper()} requires its own ordinary non-admin identity",
        )
        capabilities = assistant_client.http_json(
            gateway + "/chat/capabilities", session
        )
        baseline().owned(capabilities, fixture)
        common.require(
            capabilities.get("enabled") is True
            and capabilities.get("history_configured") is True,
            f"Assistant isolation {actor.upper()} owned chat is unavailable",
        )
