"""Execute the installed driver against local Gateway routes, not live acceptance."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from src.products.evaluation_cleanup import FixtureOwnershipError, retire_run_product
from src.products.models import Product, ProductChange, ProductOperation
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.e2e.product_onboarding.product_profile_api import PROTECTED_SELECTIONS
from tests.e2e.cli_uplift import cleanup, ports
from tests.e2e.product_onboarding import product_execution

sys.path.insert(0, str(Path(__file__).parents[1] / "cli_uplift" / "remote"))
import product_v01

pytest_plugins = ("modules.gateway.tests.conftest",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_actor,stale_release", [(False, False), (True, False), (False, True)]
)
async def test_cancelled_before_create_reconciles_original_intent(
    tmp_path,
    monkeypatch,
    db_session,
    app,
    test_client,
    valid_token_context,
    wrong_actor,
    stale_release,
):
    from src.auth.dependencies import get_current_user

    evaluation_id = "adp-e2e-20261020-113000-abcdef"
    run_id = "product-123456-2"
    workspace = (
        evaluation_id
        + "-product-"
        + hashlib.sha256((evaluation_id + ":product-workspace").encode()).hexdigest()[
            :24
        ]
    )
    actor = valid_token_context.model_copy(update={"org_id": workspace})
    cfg = {
        "platform_account": "000000000000",
        "region": "us-east-1",
        "expected_revision": "a" * 40,
    }
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    db_session.add(Organization(id=workspace, name="eval-regression-" + evaluation_id))
    db_session.add(
        User(
            id=actor.user_id,
            org_id=workspace,
            team_id=actor.team_id,
            email="owned@example.test",
            user_kind="human",
        )
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    manifest = cleanup.Manifest(tmp_path / "manifest.json", evaluation_id)
    for kind, identifier in (
        ("ec2_instance", "i-00000001"),
        ("adp_user", workspace + "/" + actor.user_id),
    ):
        manifest.record(
            kind, identifier, account=cfg["platform_account"], region=cfg["region"]
        )
    intent = {
        "run_id": run_id,
        "tenant_id": workspace,
        "actor_id": actor.user_id,
        "source_revision": cfg["expected_revision"],
        "idempotency_key": "product-create-"
        + hashlib.sha256((run_id + ":" + evaluation_id).encode()).hexdigest(),
    }
    pending = manifest.record(
        "product_fixture",
        run_id,
        detail={**intent, "instance_id": "i-00000001"},
        account=cfg["platform_account"],
        region=cfg["region"],
    )
    ctx = {
        "evaluation_id": evaluation_id,
        "document": {"instance_id": "i-00000001"},
        "manifest": manifest,
    }
    loop = asyncio.get_running_loop()
    created = []
    recorded = []
    store = SimpleNamespace(
        save=lambda _run, document, _manifest: recorded.append(
            json.loads(json.dumps(document))
        )
    )
    monkeypatch.setattr(
        product_execution.product_inventory,
        "observe",
        lambda *_args: {
            "source_revision": "b" * 40 if stale_release else cfg["expected_revision"]
        },
    )

    async def retire(owned):
        await db_session.rollback()
        try:
            receipt = await retire_run_product(db_session, owned)
            await db_session.commit()
        except FixtureOwnershipError:
            await db_session.rollback()
            raise product_execution.RetirementConflict(
                "Original create is not present"
            ) from None
        return {"success": True, "stage": "complete", "detail": receipt}

    monkeypatch.setattr(
        product_execution,
        "retire_independently",
        lambda _cfg, _aws, owned: asyncio.run_coroutine_threadsafe(
            retire(owned), loop
        ).result(15),
    )

    def restored(_cfg, _ctx, _store, *, owner):
        assert owner is True
        return {
            "user_id": "foreign-user" if wrong_actor else actor.user_id,
            "org_id": workspace,
            "access_token": "private-owner",
        }

    monkeypatch.setattr(product_execution.product_sessions, "restore", restored)

    async def create(path, token, body, key, status):
        assert path == "/api/v1/products" and token == "private-owner" and status == 201
        assert key == intent["idempotency_key"]
        assert (
            recorded[-1]["product_recovery_attempts"][-1]["state"]
            == "reconcile_started"
        )
        created.append(key)
        response = await test_client.post(
            "/v1/products", json=body, headers={"Idempotency-Key": key}
        )
        return response.status_code, response.json()

    monkeypatch.setattr(
        product_execution.product_sessions,
        "request",
        lambda _cfg, path, **kwargs: asyncio.run_coroutine_threadsafe(
            create(
                path,
                kwargs["token"],
                kwargs["body"],
                kwargs["idempotency_key"],
                kwargs["expected_status"],
            ),
            loop,
        ).result(15),
    )

    def aws_call(service, _operation, **_kwargs):
        if service == "sts":
            return {
                "Account": cfg["platform_account"],
                "Arn": "arn:aws:sts::000000000000:assumed-role/adp-cli-uplift-eval-orchestrator/recovery",
            }
        return {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": "i-00000001", "State": {"Name": "terminated"}}
                    ]
                }
            ]
        }

    if wrong_actor or stale_release:
        with pytest.raises(ports.PortError, match="actor differs|release changed"):
            await asyncio.to_thread(
                product_execution.retire_after_termination,
                cfg,
                SimpleNamespace(call=aws_call),
                ctx,
                pending,
                intent,
                store=store,
            )
        assert created == []
        return
    for _attempt in range(2):
        result = await asyncio.to_thread(
            product_execution.retire_after_termination,
            cfg,
            SimpleNamespace(call=aws_call),
            ctx,
            pending,
            intent,
            store=store,
        )
        assert result["detail"]["fixture_removed"] is True
    assert created == [intent["idempotency_key"]]
    assert await db_session.scalar(select(func.count()).select_from(Product)) == 1
    assert (
        await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 1
    )
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    receipt = result["detail"]
    assert (
        await test_client.get(
            f"/v1/products/{receipt['product_id']}/projects/{receipt['project_id']}"
        )
    ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", sorted(PROTECTED_SELECTIONS))
async def test_protected_selectors_run_real_api_and_retirement(
    selection,
    tmp_path,
    monkeypatch,
    db_session,
    app,
    test_client,
    valid_token_context,
):
    from src.auth.dependencies import get_current_user
    from src.products.evaluation_cleanup_routes import OwnedProductManifest

    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    tmp_path.chmod(0o700)
    owner = valid_token_context.model_copy(
        update={"user_id": "owned-user", "org_id": "owned-tenant"}
    )
    foreign = valid_token_context.model_copy(
        update={"user_id": "foreign-user", "org_id": "foreign-tenant"}
    )
    revoked = valid_token_context.model_copy(
        update={"user_id": "ordinary-user", "org_id": "owned-tenant"}
    )
    actors = {"owner-token": owner, "foreign-token": foreign, "revoked-token": revoked}
    db_session.add_all(
        [
            Organization(id="owned-tenant", name="eval-regression-owned"),
            Organization(id="foreign-tenant", name="Shared workspace"),
        ]
    )
    await db_session.flush()
    for token, actor in actors.items():
        db_session.add(
            User(
                id=actor.user_id,
                org_id=actor.org_id,
                team_id=actor.team_id,
                email=token + "@example.test",
                user_kind="human",
            )
        )
        db_session.add(
            TenantMembership(
                user_id=actor.user_id,
                tenant_id=actor.org_id,
                role="member",
                is_active=True,
            )
        )
    await db_session.commit()
    loop = asyncio.get_running_loop()
    requests = []

    async def request(token, method, path, **kwargs):
        app.dependency_overrides[get_current_user] = lambda: actors[token]
        requests.append((token, method, path))
        return await test_client.request(method, path, **kwargs)

    class Client:
        def __init__(self, token):
            self.token = token

        def get(self, path):
            return asyncio.run_coroutine_threadsafe(
                request(self.token, "GET", path), loop
            ).result(15)

        def post(self, path, **kwargs):
            return asyncio.run_coroutine_threadsafe(
                request(self.token, "POST", path, **kwargs), loop
            ).result(15)

        def patch(self, path, **kwargs):
            return asyncio.run_coroutine_threadsafe(
                request(self.token, "PATCH", path, **kwargs), loop
            ).result(15)

    @contextmanager
    def clients(token):
        yield Client(token)

    session = {
        "org_id": owner.org_id,
        "user_id": owner.user_id,
        "access_token": "owner-token",
    }
    monkeypatch.setattr(
        product_v01, "local_owner", lambda *_args, **_kwargs: (session, clients)
    )
    monkeypatch.setattr(
        product_v01, "ProductClient", lambda _origin, token: Client(token)
    )
    monkeypatch.setattr(
        product_v01.common,
        "load_session",
        lambda _cfg: {"org_id": foreign.org_id, "access_token": "foreign-token"},
    )

    async def revoke():
        await db_session.execute(
            update(TenantMembership)
            .where(TenantMembership.user_id == revoked.user_id)
            .values(is_active=False, revoked_at=datetime.now(UTC))
        )
        await db_session.commit()
        return {"user_id": revoked.user_id, "membership_revoked": True}

    monkeypatch.setattr(
        product_v01.product_denial_actor,
        "prepare",
        lambda *_args: (
            "revoked-token",
            lambda: asyncio.run_coroutine_threadsafe(revoke(), loop).result(15),
        ),
    )

    async def retire(manifest):
        await db_session.rollback()
        receipt = await retire_run_product(
            db_session,
            OwnedProductManifest.model_validate(manifest).model_dump(exclude_none=True),
        )
        await db_session.commit()
        return receipt

    def cleanup(cfg, evidence):
        record = json.loads((tmp_path / "product-ownership.json").read_text())
        manifest = {key: value for key, value in record.items() if key != "document"}
        receipt = asyncio.run_coroutine_threadsafe(retire(manifest), loop).result(15)
        evidence.update(success=True, detail=receipt)

    monkeypatch.setattr(product_v01.product_cleanup, "execute", cleanup)
    cfg = {
        "work_dir": str(tmp_path),
        "gateway_url": "https://gateway.example.invalid",
        "evaluation_id": "adp-e2e-20261020-113000-abcdef",
        "product_run_id": "product-123456-2",
        "workflow_run_id": "product-123456-2",
        "product_cases": selection,
        "product_actor_id": owner.user_id,
        "product_workspace_id": owner.org_id,
        "other_tenant_id": foreign.org_id,
        "product_intent_key": "product-create-" + "b" * 64,
        "expected_revision": "a" * 40,
    }
    shared = await request(
        "foreign-token",
        "POST",
        "/v1/products",
        json={
            "name": "Shared Product",
            "purpose": "Must remain unchanged",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "shared-product-create"},
    )
    assert shared.status_code == 201, shared.text
    result = await asyncio.to_thread(
        product_v01.run_selected, cfg, lambda: cfg["expected_revision"]
    )
    assert set(result["results"]) == set(selection.split(","))
    assert result["cleanup"]["fixture_removed"] is True
    assert await db_session.scalar(select(func.count()).select_from(Product)) == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductOperation)
            .where(ProductOperation.operation == "createProduct")
        )
        == 2
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductChange)
            .where(ProductChange.tenant_id == owner.org_id)
        )
        == 3
    )
    if "api_recovery_cleanup" in selection:
        report = result["results"]["api_recovery_cleanup"]
        assert report["shared_unchanged"] is True
        assert [attempt["state"] for attempt in report["attempts"]].count(
            "response_unknown"
        ) == 1
        assert (
            sum(
                token == "owner-token" and method == "POST"
                for token, method, _path in requests
            )
            == 3
        )
    if "api_denial_oracle" in selection:
        assert result["results"]["api_denial_oracle"]["history_count"] == 2
        assert any(token == "revoked-token" for token, _method, _path in requests)
