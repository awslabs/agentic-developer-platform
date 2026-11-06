"""Read-only Product, repository and native-link admission with offline transport."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.config import ConfigError, ResolvedConnection, load_config
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios.delivery import LiveSession
from tests.e2e.orchestration.scenarios.http import Client, Unsupported
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC


@pytest.fixture
def qualified(write_config):
    config = load_config(
        write_config(
            {
                "product_epic": PRODUCT_EPIC,
                "connection": {
                    "repository": "example/engine",
                    "expected_org": "example",
                },
                "bounds": {"max_resources": 30},
            }
        )
    )
    epic = config.product_epic
    targets = (epic.epic, *epic.stories)
    reads = {
        "/auth/me": {"org_id": config.org_ref, "user_id": config.identity_ref},
        "/v1/products?limit=100": {
            "items": [{"id": epic.product_id, "lifecycle_status": "active"}],
            "next_cursor": None,
        },
        "/admin/connections": {
            "platform_verification": {"app_permissions_match": True},
            "connections": [
                {
                    "installation_id": epic.epic.installation_id,
                    "tenant_id": config.org_ref,
                    "is_active_tenant": True,
                    "revocation_pending": False,
                    "verification": {"repositories_live": True},
                    "repositories": [target.repository for target in targets],
                }
            ],
        },
        **{
            "/repos/" + target.repository: {
                "id": target.repository_id,
                "full_name": target.repository,
                "has_issues": True,
                "permissions": {"push": True},
            }
            for target in targets
        },
        f"/repos/{epic.epic.repository}/issues/{epic.epic_issue_number}": {
            "number": epic.epic_issue_number,
            "id": 12345,
        },
    }
    return config, reads


@pytest.fixture
def transport(qualified, monkeypatch):
    config, reads = qualified
    client = Client(config, SimpleNamespace(api_origin="https://offline.invalid"))
    observed = []

    def get(path, **kwargs):
        observed.append(path)
        if path not in reads:
            raise Unsupported(f"read {path} returned HTTP 404")
        return deepcopy(reads[path])

    client.get = get
    client.resolve_connection = lambda ref: ResolvedConnection(
        ref, config.expected_account_id, config.expected_org
    )
    monkeypatch.setattr(runner, "load_connection_resolver", lambda _: client)
    monkeypatch.setattr(
        runner,
        "_caller_identity",
        lambda: ({"account": config.expected_account_id, "arn": "offline"}, None),
    )
    return client, observed


def test_preflight_checks_product_and_all_three_repositories(qualified, transport):
    config, _ = qualified
    _, observed = transport
    result = runner.preflight(config)
    assert result.status == runner.STATUS_READY
    assert "/v1/products?limit=100" in observed
    for target in (config.product_epic.epic, *config.product_epic.stories):
        assert "/repos/" + target.repository in observed
    assert list(config.artifact_directory.iterdir()) == []


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("product-missing", "Product is missing or inaccessible"),
        ("product-unreadable", "Product"),
        ("wrong-owner", "Product owner identity"),
        ("unknown-installation", "repository connection"),
        ("stale-repositories", "repository connection"),
        ("unselected-repository", "repository connection"),
        ("wrong-tenant", "repository connection"),
        ("unknown-target", "repository target identity"),
        ("link-permission", "issues-write permission"),
        ("link-permission-unknown", "issues-write permission"),
        ("link-repository", "issues-write access"),
        ("missing-epic", "planning epic issue"),
    ],
)
def test_refusal_precedes_issue_creation(qualified, transport, failure, expected):
    config, reads = qualified
    client, observed = transport
    epic = config.product_epic
    if failure == "wrong-owner":
        reads["/auth/me"]["org_id"] = "other-tenant"
    elif failure == "product-missing":
        reads["/v1/products?limit=100"]["items"] = []
    elif failure == "product-unreadable":
        del reads["/v1/products?limit=100"]
    elif failure == "unknown-installation":
        reads["/admin/connections"]["connections"] = []
    elif failure == "stale-repositories":
        reads["/admin/connections"]["connections"][0]["verification"][
            "repositories_live"
        ] = False
    elif failure == "unselected-repository":
        reads["/admin/connections"]["connections"][0]["repositories"].remove(
            epic.stories[1].repository
        )
    elif failure == "wrong-tenant":
        reads["/admin/connections"]["connections"][0]["tenant_id"] = "other-tenant"
    elif failure == "unknown-target":
        reads["/repos/" + epic.stories[1].repository]["id"] = 999
    elif failure == "link-permission":
        reads["/admin/connections"]["platform_verification"][
            "app_permissions_match"
        ] = False
    elif failure == "link-permission-unknown":
        reads["/admin/connections"]["platform_verification"] = None
    elif failure == "link-repository":
        reads["/repos/" + epic.stories[0].repository]["permissions"] = {"push": False}
    else:
        del reads[f"/repos/{epic.epic.repository}/issues/{epic.epic_issue_number}"]

    preview = runner.preflight(config)
    assert preview.exit_code == runner.EXIT_TARGET_UNVERIFIED
    assert expected in preview.report["detail"]
    assert preview.report["mutations"] == []
    result = runner.run(config)
    assert result.status == runner.STATUS_REFUSED
    assert expected in result.report["detail"]
    assert list(config.artifact_directory.iterdir()) == []

    inventory = Inventory.create(
        config.artifact_directory, "q-0123456789abcdef", config.environment
    )
    provider = Mock()
    session = LiveSession(
        config, SimpleNamespace(), inventory, {"qualification-issue": provider}
    )
    session.client = client
    with pytest.raises(Unsupported, match=expected):
        session.start()
    provider.create.assert_not_called()
    assert inventory.fixtures == []
    assert all(not path.endswith("/issues") for path in observed)


@pytest.mark.parametrize(
    "missing", ["bounds.max_usd", "bounds.max_duration_seconds", "bounds.max_runs"]
)
def test_missing_finite_limits_are_config_errors_before_preflight(
    write_config, missing
):
    path = write_config({"product_epic": PRODUCT_EPIC}, remove=(missing,))
    with pytest.raises(ConfigError, match=missing):
        load_config(path)
