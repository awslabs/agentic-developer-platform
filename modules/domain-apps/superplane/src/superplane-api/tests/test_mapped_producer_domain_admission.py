import uuid

from app.models.organization import Organization
from tests.conftest import async_session_test


async def test_mapped_producer_refuses_stale_or_unavailable_membership_before_domain_grants(
    client, monkeypatch
):
    from app import auth, domain_guard
    from app.adapters.operation_dispatch import ProducerRefusedError
    from app.current_identity import MappedProducerIdentityReader
    from app.main import app
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from superplane_auth.policy import DomainTokenPolicy

    first_org, second_org = uuid.uuid4(), uuid.uuid4()
    first_workspace, second_workspace = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add_all(
            [
                Organization(id=first_org, name="producer-first", adp_org_id="O1"),
                Organization(id=second_org, name="producer-second", adp_org_id="O2"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Workspace(
                    id=first_workspace,
                    org_id=first_org,
                    name="producer-first",
                    isolation_mode="shared",
                    status="Ready",
                ),
                Workspace(
                    id=second_workspace,
                    org_id=second_org,
                    name="producer-second",
                    isolation_mode="shared",
                    status="Ready",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                WorkspaceGrantRecord(
                    workspace_id=first_workspace,
                    org_id=first_org,
                    principal="alice",
                    principal_type="human",
                    permissions="workspace:read",
                ),
                WorkspaceGrantRecord(
                    workspace_id=second_workspace,
                    org_id=second_org,
                    principal="alice",
                    principal_type="human",
                    permissions="workspace:read",
                ),
            ]
        )
        await db.commit()

    claims = {
        "sub": "alice",
        "custom:org_id": "O1",
        "custom:account_type": "human",
        "token_use": "access",
        "iss": "https://issuer.example",
        "client_id": "client",
    }
    monkeypatch.setattr(auth, "verify_access_token", lambda token: claims)
    monkeypatch.setattr(
        app.state,
        "domain_policy",
        DomainTokenPolicy(
            allowed_client_ids=["client"],
            expected_issuer="https://issuer.example",
        ),
        raising=False,
    )
    state = {"selected": "O1", "active": True, "enabled": True, "failure": None}

    class Producer:
        def __init__(self):
            self.calls = []

        async def post(self, route, payload, *, distinguish_denial=False):
            self.calls.append((route, payload, distinguish_denial))
            if state["failure"] == "outage":
                raise RuntimeError("identity provider unavailable")
            if (
                state["failure"] == "denied"
                or payload["org_id"]
                != {
                    "O1": str(first_org),
                    "O2": str(second_org),
                }[state["selected"]]
            ):
                raise ProducerRefusedError()
            result = {
                "version": 1,
                "subject": payload["subject"],
                "principal_type": "human",
                "adp_org_id": state["selected"],
                "membership_id": f"member-{state['selected']}",
                "active": state["active"],
                "enabled": state["enabled"],
            }
            if state["failure"] == "wrong-subject":
                result["subject"] = "bob"
            if state["failure"] == "wrong-organization":
                result["adp_org_id"] = "O1"
            if state["failure"] == "missing-membership":
                result["membership_id"] = ""
            return result

    producer = Producer()
    monkeypatch.setattr(
        app.state,
        "current_identity_reader",
        MappedProducerIdentityReader(producer, async_session_test),
        raising=False,
    )
    headers = {"Authorization": "Bearer signed"}

    assert (
        await client.get(f"/workspaces/{first_workspace}", headers=headers)
    ).status_code == 200
    state["selected"] = "O2"
    claims["custom:org_id"] = "O2"
    assert (
        await client.get(f"/workspaces/{second_workspace}", headers=headers)
    ).status_code == 200
    assert (
        await client.get(f"/workspaces/{first_workspace}", headers=headers)
    ).status_code == 403

    def reject_principal(*args, **kwargs):
        raise AssertionError("refused identity must not construct a domain principal")

    async def reject_grant(*args, **kwargs):
        raise AssertionError("refused identity must not read domain grants")

    with monkeypatch.context() as refusal:
        refusal.setattr(domain_guard, "DomainPrincipal", reject_principal)
        refusal.setattr(
            domain_guard.domain_auth, "authorize_workspace_operation", reject_grant
        )

        async def denied(expected, *, workspace=second_workspace):
            before = len(producer.calls)
            response = await client.get(f"/workspaces/{workspace}", headers=headers)
            assert response.status_code == expected
            return len(producer.calls) - before

        claims["custom:org_id"] = "O1"
        assert await denied(403, workspace=first_workspace) == 1
        claims["custom:org_id"] = "O2"
        state["active"] = False
        assert await denied(403) == 1
        state["active"] = True
        state["enabled"] = False
        assert await denied(403) == 1
        state["enabled"] = True
        for failure, status_code in (
            ("wrong-subject", 403),
            ("wrong-organization", 403),
            ("missing-membership", 403),
            ("denied", 403),
            ("outage", 503),
        ):
            state["failure"] = failure
            assert await denied(status_code) == 1
        state["failure"] = None
        claims["custom:org_id"] = "unknown"
        assert await denied(403) == 0

    claims["custom:org_id"] = "O2"
    assert (
        await client.get(f"/workspaces/{second_workspace}", headers=headers)
    ).status_code == 200
    assert all(
        call[0] == "/current-identity" and call[2] is True for call in producer.calls
    )
    assert all(call[1]["subject"] == "alice" for call in producer.calls)
