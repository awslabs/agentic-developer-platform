"""Guidance fixtures and CLI saves against the local gateway and real retirement."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select

from src.products.evaluation_cleanup import FixtureOwnershipError, retire_run_product
from src.products.models import (
    Product,
    ProductChange,
    ProductOnboardingSession,
    ProductOperation,
    ProductProjectBinding,
    Project,
)
from src.shared.models.organization import Organization, User
from tests.e2e.new_ui.product_fixture_provider import BrowserFixtureProvider
from tests.e2e.new_ui import test_product_guidance as guidance
from tests.e2e.product_onboarding.product_v02_record import CaseEvidence

pytest_plugins = ("modules.gateway.tests.conftest",)
STARTING_POINTS = ("idea", "existing_assets", "running_product")


class LocalState:
    def __init__(self, binding):
        self.binding = binding
        self.document = {"cases": {}}

    def save(self, value):
        self.document = deepcopy(value)


@pytest.fixture
async def guidance_gateway(
    test_client, app, db_session, valid_token_context, monkeypatch, tmp_path
):
    from src.auth.dependencies import get_current_user

    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="eval-regression-guidance"))
    db_session.add(
        User(
            id=actor.user_id,
            org_id=actor.org_id,
            team_id=actor.team_id,
            email="guidance@example.test",
            user_kind="human",
        )
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    monkeypatch.setattr(
        "boto3.client",
        lambda *args, **kwargs: pytest.fail("Guided fixtures must not call providers"),
    )
    loop = asyncio.get_running_loop()

    def wait(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=30)

    class Request:
        def __getattr__(self, method):
            def send(url, **kwargs):
                parsed = urlsplit(url)
                path = parsed.path.removeprefix("/api") + (
                    "?" + parsed.query if parsed.query else ""
                )
                response = wait(
                    test_client.request(
                        method.upper(),
                        path,
                        json=kwargs.get("data"),
                        headers=kwargs.get("headers"),
                    )
                )
                return SimpleNamespace(
                    status=response.status_code,
                    json=response.json,
                    body=lambda: response.content,
                )

            return send

    async def retire(manifest):
        originals = {
            change.id: deepcopy(change.snapshot)
            for change in await db_session.scalars(
                select(ProductChange).where(
                    ProductChange.product_id == manifest["product_id"]
                )
            )
        }
        operations = await db_session.scalar(
            select(func.count()).select_from(ProductOperation)
        )
        try:
            receipt = await retire_run_product(db_session, manifest)
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise
        assert (
            await db_session.scalar(select(func.count()).select_from(ProductOperation))
            == operations
        )
        for change_id, snapshot in originals.items():
            assert (
                await db_session.get(ProductChange, (actor.org_id, change_id))
            ).snapshot == snapshot
        return receipt

    provider = object.__new__(BrowserFixtureProvider)
    provider.request = Request()
    provider.base_url = "https://example.test"
    provider.run_id = "product-42-1"
    provider.binding = {
        "run_id": provider.run_id,
        "tenant_id": actor.org_id,
        "actor_id": actor.user_id,
        "source_revision": "a" * 40,
    }
    provider.state = LocalState(provider.binding)
    provider.owner = {"access_token": "local-test-token", "tenant_id": actor.org_id}
    provider.observer = provider.nonmember = provider.owner
    provider.frontend = provider.evidence_context = {}
    provider.membership = SimpleNamespace(restore=lambda state: None)
    provider.retire = lambda manifest: wait(retire(manifest))

    script = Path(__file__).resolve().parents[3] / "modules/gateway/cli/adp-onboard.py"
    spec = importlib.util.spec_from_file_location("guidance_test_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    class Client:
        def request(self, method, path, body=None, **kwargs):
            response = getattr(provider.request, method.lower())(
                path, data=body, headers=kwargs.get("extra_headers")
            )
            assert 200 <= response.status < 300, response.json()
            return response.json()

    def installed_cli(
        binary, checksums, owner, base_url, arguments, *, input_body, record
    ):
        payload = tmp_path / "direct-save.json"
        payload.write_text(json.dumps(input_body), encoding="utf-8")
        arguments = [*arguments[:-1], str(payload), "--json"]
        return cli.execute(cli.parser().parse_args(arguments), Client())["detail"]

    monkeypatch.setattr(guidance, "installed_cli", installed_cli)
    unrelated = await test_client.post(
        "/v1/products",
        json={
            "name": "Unrelated Product",
            "purpose": "Preserve me",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "unrelated_guidance_product"},
    )
    assert unrelated.status_code == 201, unrelated.text
    return provider


def start_guidance(provider, fixture):
    path = (
        provider.base_url
        + f"/api/v1/products/{fixture['product_id']}"
        + f"/projects/{fixture['project_id']}/onboarding-sessions"
    )
    profile = provider.request.get(path.removesuffix("/onboarding-sessions")).json()
    started = provider.request.post(
        path,
        data={},
        headers={
            "Idempotency-Key": "guidance_start_" + fixture["product_id"],
            "If-Profile-Revision": str(profile["revision"]),
        },
    )
    assert started.status == 201, started.json()
    session = started.json()
    guided = provider.request.post(
        path + f"/{session['id']}/guidance",
        data={"message": "What is missing?"},
        headers={
            "Idempotency-Key": "guidance_turn_" + session["id"],
            "If-Profile-Revision": str(session["revision"]),
        },
    )
    assert guided.status == 200 and guided.json()["proposals"] == []
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("starting_point", STARTING_POINTS)
@pytest.mark.parametrize(
    "stage", ("created", "started", "saved", "complete", "lost_cleanup_response")
)
async def test_guidance_fixture_retires_after_success_or_interruption(
    guidance_gateway, db_session, starting_point, stage
):
    provider = guidance_gateway

    def exercise():
        case = "guidance_" + starting_point
        retire = provider.retire
        if stage == "lost_cleanup_response":

            def lose_response(manifest):
                provider.retire = retire
                retire(manifest)
                raise TimeoutError("lost cleanup response")

            provider.retire = lose_response
        try:
            with provider.fixture(case, CaseEvidence()) as fixture:
                if stage == "created":
                    raise InterruptedError("interrupted after create")
                session = start_guidance(provider, fixture)
                if stage == "started":
                    raise InterruptedError("interrupted after start")
                saved = guidance.save_direct_with_installed_cli(
                    None, {}, fixture, provider.base_url, session, None
                )
                assert saved["revision"] == session["revision"] + 1
                if stage == "saved":
                    raise InterruptedError("interrupted after save")
        except InterruptedError:
            assert stage in ("created", "started", "saved")
        except TimeoutError:
            assert stage == "lost_cleanup_response"
            provider.cleanup(case, provider.request)
        cleanup = provider.state.document["cases"][case]["cleanup"]
        assert (
            cleanup["fixture_removed"]
            and cleanup["owned_absent"]
            and cleanup["shared_unchanged"]
        )
        assert provider.cleanup(case, provider.request) == cleanup
        return cleanup

    cleanup = await asyncio.to_thread(exercise)
    changes = (
        await db_session.scalars(
            select(ProductChange).where(
                ProductChange.product_id == cleanup["product_id"]
            )
        )
    ).all()
    assert sum(change.changed_paths == ["/fixture_cleanup"] for change in changes) == 1


@pytest.mark.asyncio
async def test_direct_save_keys_remain_distinct_after_retirement(guidance_gateway):
    provider = guidance_gateway

    def exercise():
        for starting_point in STARTING_POINTS:
            with provider.fixture(
                "guidance_" + starting_point, CaseEvidence()
            ) as fixture:
                session = start_guidance(provider, fixture)
                first = guidance.save_direct_with_installed_cli(
                    None, {}, fixture, provider.base_url, session, None
                )
                replay = guidance.save_direct_with_installed_cli(
                    None, {}, fixture, provider.base_url, session, None
                )
                assert replay == first and first["revision"] == session["revision"] + 1

    await asyncio.to_thread(exercise)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    (
        "wrong_starting_point",
        "extra_patch",
        "wrong_step",
        "different_purpose",
        "foreign_actor",
        "pending_operation",
        "provider_operation",
        "tampered_hash",
        "proposals",
        "waiting_session",
        "configuration",
        "shared_project",
        "extra_history_path",
    ),
)
async def test_guidance_retirement_refuses_unreviewed_mutations(
    guidance_gateway, db_session, fault
):
    provider = guidance_gateway
    context = provider.fixture("guidance_running_product", CaseEvidence())
    fixture = await asyncio.to_thread(context.__enter__)
    session = await asyncio.to_thread(start_guidance, provider, fixture)
    await asyncio.to_thread(
        guidance.save_direct_with_installed_cli,
        None,
        {},
        fixture,
        provider.base_url,
        session,
        None,
    )
    operation_name = (
        "updateProduct"
        if fault in ("wrong_starting_point", "extra_patch")
        else "updateStep"
    )
    operation = await db_session.scalar(
        select(ProductOperation).where(
            ProductOperation.product_id == fixture["product_id"],
            ProductOperation.operation == operation_name,
        )
    )
    saved_session = await db_session.get(
        ProductOnboardingSession, (fixture["tenant_id"], session["id"])
    )
    if fault in (
        "wrong_starting_point",
        "extra_patch",
        "wrong_step",
        "different_purpose",
    ):
        request = deepcopy(operation.request)
        if fault == "wrong_starting_point":
            request["patch"]["starting_point"] = "existing_assets"
        elif fault == "extra_patch":
            request["patch"]["purpose"] = "Unreviewed change"
        elif fault == "wrong_step":
            request["step_id"] = "basic"
        else:
            request["request"]["product"]["purpose"] = "Unreviewed change"
        operation.request = request
        operation.request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    elif fault == "foreign_actor":
        operation.actor_id = "another-actor"
    elif fault == "pending_operation":
        operation.state = "pending"
    elif fault == "provider_operation":
        operation.provider_operation_id = "external-operation"
    elif fault == "tampered_hash":
        operation.request_hash = "0" * 64
    elif fault == "proposals":
        saved_session.proposals = [{"id": "pending-proposal"}]
    elif fault == "waiting_session":
        saved_session.state = "waiting_external"
    elif fault == "configuration":
        project = await db_session.get(
            Project, (fixture["tenant_id"], fixture["project_id"])
        )
        project.configuration = {
            "financials": {"budget_policy_ref": {"record_id": "shared-budget"}}
        }
    elif fault == "shared_project":
        unrelated = await db_session.scalar(
            select(Product).where(Product.id != fixture["product_id"])
        )
        binding = await db_session.scalar(
            select(ProductProjectBinding).where(
                ProductProjectBinding.product_id == fixture["product_id"]
            )
        )
        db_session.add(
            ProductProjectBinding(
                tenant_id=fixture["tenant_id"],
                product_id=unrelated.id,
                project_id=fixture["project_id"],
                valid_from=binding.valid_from - timedelta(days=2),
                valid_to=binding.valid_from - timedelta(days=1),
                provenance={"actor_id": fixture["actor_id"], "source": "direct_input"},
            )
        )
    else:
        change = await db_session.scalar(
            select(ProductChange).where(ProductChange.operation_id == operation.id)
        )
        change.changed_paths = [*change.changed_paths, "/product/domain"]
    await db_session.commit()
    with pytest.raises(FixtureOwnershipError):
        await asyncio.to_thread(context.__exit__, None, None, None)
    product = await db_session.get(
        Product, (fixture["tenant_id"], fixture["product_id"])
    )
    project = await db_session.get(
        Project, (fixture["tenant_id"], fixture["project_id"])
    )
    assert product.archived_at is None and project.archived_at is None
    create = await db_session.scalar(
        select(ProductOperation).where(
            ProductOperation.product_id == fixture["product_id"],
            ProductOperation.operation == "createProduct",
        )
    )
    assert not create.receipt.get("fixture_removed")


@pytest.mark.asyncio
@pytest.mark.parametrize("starting_point", ("existing_assets", "running_product"))
async def test_guidance_setup_interruption_retires_original_create(
    guidance_gateway, starting_point, monkeypatch
):
    provider = guidance_gateway

    def interrupt(*args, **kwargs):
        raise InterruptedError("interrupted before starting-point update")

    monkeypatch.setattr(provider.request, "patch", interrupt)

    def exercise():
        case = "guidance_" + starting_point
        with pytest.raises(InterruptedError):
            with provider.fixture(case, CaseEvidence()):
                pytest.fail("Setup must stop before yielding a fixture")
        assert provider.state.document["cases"][case]["cleanup"]["owned_absent"] is True

    await asyncio.to_thread(exercise)
