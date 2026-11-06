"""Gateway startup selects only installed, complete L5 service exports."""

from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_backend_registry, l5_integration
from src.app import create_app
from src.l5_backend_registry import SERVICE_CALLBACKS, install_l5_backends


@pytest.fixture
def installed_modules(monkeypatch):
    modules = {}

    def import_installed(module_name):
        if module_name not in modules:
            raise ModuleNotFoundError("service is not installed", name=module_name)
        return modules[module_name]

    monkeypatch.setattr(l5_backend_registry, "import_module", import_installed)
    return modules


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", tuple(SERVICE_CALLBACKS))
async def test_normal_app_startup_registers_only_installed_service_exports(installed_modules, service_id):
    module = ModuleType(f"src.l5_backend_{service_id}")
    callbacks = {name: object() for name in SERVICE_CALLBACKS[service_id]}
    module.gateway_backends = lambda: callbacks
    installed_modules[module.__name__] = module

    app = create_app()
    for field, value in callbacks.items():
        assert getattr(app.state, field) is value
    assert all(not hasattr(app.state, field) for name, fields in SERVICE_CALLBACKS.items() if name != service_id for field in fields)
    request = Request({"type": "http", "app": app})
    actor = SimpleNamespace(org_id="tenant-one", user_id="owner-one", account_type="human")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    selected_fields = {
        "devops": {
            "l5_devops_integration_observer": "devops_integration_observer",
            "l5_devops_verification_reader": "verification_reader",
            "l5_devops_release_authority": "release_authority",
            "l5_devops_prevention_provider": "prevention_provider",
        },
        "finops": {"l5_finops_authority": "finops_authority"},
        "transform": {
            "l5_transform_work_authority": "transform_work_authority",
            "l5_transform_executor_qualification_reader": "transform_qualification_reader",
            "l5_transform_artifact_store": "transform_artifact_store",
        },
        "well_architected": {
            "l5_wa_authority": "wa_authority",
            "l5_wa_profile_client": "wa_profile_client",
            "l5_wa_consumer_resolver": "wa_consumer_resolver",
            "l5_wa_qualify": "wa_qualify",
        },
    }[service_id]
    async with sessions() as session:
        selected = l5_integration.backend(service_id, request, db=session, actor=actor, x_l5_delegation_id=None)
        security = l5_integration.backend("security", request, db=session, actor=actor, x_l5_delegation_id=None)
        for field, attribute in selected_fields.items():
            assert getattr(selected, attribute) is callbacks[field]
            assert getattr(security, attribute) is None
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_id", "authority_field", "backend_field"),
    (
        ("devops", "l5_devops_integration_observer", "devops_integration_observer"),
        ("devops", "l5_devops_verification_reader", "verification_reader"),
        ("devops", "l5_devops_release_authority", "release_authority"),
        ("devops", "l5_devops_prevention_provider", "prevention_provider"),
        ("transform", "l5_transform_work_authority", "transform_work_authority"),
        ("finops", "l5_finops_authority", "finops_authority"),
        ("well_architected", "l5_wa_authority", "wa_authority"),
    ),
)
async def test_registered_authority_binds_to_each_request_without_mutating_export(installed_modules, service_id, authority_field, backend_field):
    class Authority:
        def __init__(self):
            self.backends = []

        def bind_backend(self, selected):
            assert selected.admission_key is None
            assert selected.admission_operation is None
            self.backends.append(selected)

            async def authorize():
                return selected.db, selected.actor, selected.admission_key, selected.admission_operation

            return authorize

        async def __call__(self):
            pytest.fail("application-global authority must not be called")

    authority = Authority()
    callbacks = {name: object() for name in SERVICE_CALLBACKS[service_id]}
    callbacks[authority_field] = authority
    module = ModuleType(f"src.l5_backend_{service_id}")
    module.gateway_backends = lambda: callbacks
    installed_modules[module.__name__] = module
    app = create_app()
    assert not authority.backends
    assert getattr(app.state, authority_field) is authority

    request = Request({"type": "http", "app": app})
    first_actor = SimpleNamespace(org_id="tenant-one", user_id="owner-one", account_type="human")
    second_actor = SimpleNamespace(org_id="tenant-two", user_id="owner-two", account_type="human")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as first_db, sessions() as second_db:
        first = l5_integration.backend(service_id, request, db=first_db, actor=first_actor, x_l5_delegation_id=None)
        second = l5_integration.backend(service_id, request, db=second_db, actor=second_actor, x_l5_delegation_id=None)
        first.admission_key, first.admission_operation = "first-key", "first-operation"
        second.admission_key, second.admission_operation = "second-key", "second-operation"
        assert authority.backends == [first, second]
        assert getattr(first, backend_field) is not authority
        assert getattr(second, backend_field) is not getattr(first, backend_field)
        assert await getattr(first, backend_field)() == (first_db, first_actor, "first-key", "first-operation")
        assert await getattr(second, backend_field)() == (second_db, second_actor, "second-key", "second-operation")
        assert getattr(app.state, authority_field) is authority
    await engine.dispose()


@pytest.mark.parametrize("backend_field", ("transform_work_authority", "finops_authority"))
def test_plain_async_callback_is_not_mistaken_for_an_opt_in_binder(backend_field):
    authority = AsyncMock()
    backend = l5_integration.GatewayConfigurationBackend(
        SimpleNamespace(bind=None), SimpleNamespace(org_id="tenant-one", user_id="owner-one"), **{backend_field: authority}
    )
    assert getattr(backend, backend_field) is authority
    authority.bind_backend.assert_not_called()


def test_normal_app_startup_leaves_uninstalled_services_unavailable(installed_modules):
    app = create_app()
    for names in SERVICE_CALLBACKS.values():
        for name in names:
            assert not hasattr(app.state, name)


@pytest.mark.parametrize("case", ("missing_factory", "missing_callback", "foreign_callback", "none_callback"))
def test_incomplete_service_module_fails_closed(installed_modules, case):
    module = ModuleType("src.l5_backend_finops")
    if case != "missing_factory":
        callbacks = {
            "missing_callback": {},
            "foreign_callback": {"l5_wa_authority": object()},
            "none_callback": {"l5_finops_authority": None},
        }[case]
        module.gateway_backends = lambda: callbacks
    installed_modules[module.__name__] = module
    with pytest.raises(RuntimeError, match="finops Gateway backend"):
        install_l5_backends(SimpleNamespace(state=SimpleNamespace()))


def test_missing_module_does_not_hide_broken_service_import(monkeypatch):
    def import_broken(module_name):
        raise ModuleNotFoundError("missing module", name="service_dependency")

    monkeypatch.setattr(l5_backend_registry, "import_module", import_broken)
    with pytest.raises(ModuleNotFoundError, match="missing module"):
        install_l5_backends(SimpleNamespace(state=SimpleNamespace()))
