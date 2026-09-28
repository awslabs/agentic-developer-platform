"""Exercise atomic route publication across create, update and disable races."""

import copy

import pytest

from installation.config import Refusal
from installation.runner import Installer
from .test_complete_command import ExternalTools


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_route_write_preserves_concurrent_foreign_registration(
    tmp_path, environment, release, present, enabled
):
    installer = Installer(environment, release, tmp_path)
    tools = ExternalTools(environment, release)
    installer.commands = tools
    if present:
        tools.route = {
            "installation_id": installer.owner,
            "namespace": environment["namespace"],
        }
    original = tools.call
    foreign = {"installation_id": "foreign", "namespace": "foreign", "enabled": True}

    def race(args, **kwargs):
        if "put-object" in args:
            tools.route = copy.deepcopy(foreign)
            tools.route_etag = '"foreign-version"'
        return original(args, **kwargs)

    tools.call = race
    with pytest.raises(Refusal, match="another installation"):
        installer.route(enabled)
    assert tools.route == foreign
    assert installer.receipt["public_route"]["etag"] == (
        '"route-0"' if present else None
    )
    assert installer.receipt["public_route_enabled"] is None
    assert installer.receipt["pending_route"]["value"]["enabled"] is enabled


def test_route_versions_are_retained_and_stale_writer_refuses(
    tmp_path, environment, release
):
    tools = ExternalTools(environment, release)
    installer = Installer(environment, release, tmp_path, tools)
    installer.route(False)
    first = installer.receipt["public_route"]["etag"]
    installer.route(True)
    assert installer.receipt["public_route"]["etag"] != first
    installer.receipt["public_route"]["etag"] = first
    with pytest.raises(Refusal, match="changed since"):
        installer.route(False)
    assert tools.route["enabled"] is True


@pytest.mark.parametrize(
    "support",
    [
        {"version": 1},
        {
            "version": 2,
            "transport": "s3-conditional-domain-registration",
            "configured": False,
        },
    ],
)
def test_preflight_refuses_legacy_or_unconfigured_gateway(
    tmp_path, environment, release, monkeypatch, support
):
    import httpx

    tools = ExternalTools(environment, release)
    installer = Installer(environment, release, tmp_path, tools)
    monkeypatch.setattr(
        httpx, "get", lambda *args, **kwargs: httpx.Response(200, json=support)
    )
    with pytest.raises(Refusal, match="conditional U23"):
        installer.gateway()
    assert not any("put-object" in args for args, _ in tools.calls)


def test_route_race_cannot_complete_installation(
    tmp_path, environment, release, monkeypatch
):
    from .test_complete_command import setup

    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    original = tools.call
    foreign = {"installation_id": "foreign", "namespace": "foreign", "enabled": True}

    def race(args, **kwargs):
        if (
            "put-object" in args
            and args[args.index("--key") + 1] == installer.route_key
        ):
            tools.route = copy.deepcopy(foreign)
            tools.route_etag = '"foreign-version"'
        return original(args, **kwargs)

    tools.call = race
    with pytest.raises(Refusal):
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert tools.route == foreign
    assert installer.receipt["status"] == "recovery-required"


def test_old_cleanup_receipt_cannot_remove_a_later_deployment(
    tmp_path, environment, release, monkeypatch
):
    from .test_complete_command import setup

    first, tools = setup(tmp_path / "first", environment, release, monkeypatch)
    first.preflight()
    first.execute(first.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(first.receipt)
    later = Installer(environment, release, tmp_path / "later", tools)
    tools.installer = later
    later.plan()
    later.preflight()
    later.execute(later.receipt["plan_sha256"], "verified-user")
    assert later.receipt["public_route"] != previous["public_route"]
    for old in previous["objects"]:
        if old["kind"] == "Deployment":
            assert (
                tools.objects[old["kind"], old["name"], old["namespace"]]["metadata"][
                    "uid"
                ]
                == old["uid"]
            )
    objects, route = copy.deepcopy(tools.objects), copy.deepcopy(tools.route)
    start = len(tools.calls)
    cleanup = Installer(environment, release, tmp_path / "cleanup", tools)
    with pytest.raises(Refusal, match="changed since"):
        cleanup.cleanup(previous)
    assert tools.objects == objects and tools.route == route
    assert not any(
        "put-object" in args or "delete" in args for args, _ in tools.calls[start:]
    )


def test_cleanup_preserves_an_unexpected_route_after_recorded_absence(
    tmp_path, environment, release, monkeypatch
):
    from .test_complete_command import setup

    first, tools = setup(tmp_path / "first", environment, release, monkeypatch)
    assert first.check_route_owner() is None
    previous = copy.deepcopy(first.receipt)
    first.route(True)
    current = copy.deepcopy(tools.route)
    with pytest.raises(Refusal, match="changed since"):
        Installer(environment, release, tmp_path / "cleanup", tools).cleanup(previous)
    assert tools.route == current


def test_cleanup_requires_recorded_route_evidence(tmp_path, environment, release):
    first = Installer(environment, release, tmp_path)
    with pytest.raises(Refusal, match="exact route observation"):
        first.cleanup(copy.deepcopy(first.receipt))


def test_rollback_fences_current_route_before_preparation(
    tmp_path, environment, release, monkeypatch
):
    from .test_complete_command import setup

    first, tools = setup(tmp_path / "first", environment, release, monkeypatch)
    first.preflight()
    first.execute(first.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(first.receipt)
    rollback = Installer(environment, release, tmp_path / "rollback", tools)
    tools.installer = rollback
    objects = copy.deepcopy(tools.objects)

    def concurrent_update():
        tools.route_etag = '"newer-route"'
        tools.route["release_id"] = "e" * 64

    monkeypatch.setattr(rollback, "images", concurrent_update)
    monkeypatch.setattr(
        rollback,
        "database",
        lambda: rollback.receipt.update(
            database_observations={
                "runtime-url": {"revision": release["schema"]["observed"]["head"]}
            }
        ),
    )
    with pytest.raises(Refusal, match="changed since"):
        rollback.rollback(previous, "verified-user")
    assert tools.route["enabled"] is True and tools.route["release_id"] == "e" * 64
    assert tools.objects == objects
    assert rollback.receipt["rollback_current_route"] == previous["public_route"]


@pytest.mark.parametrize("action", ["cleanup", "rollback"])
def test_lifecycle_requires_conditional_gateway_transport(
    tmp_path, environment, release, monkeypatch, action
):
    import httpx
    from .test_complete_command import setup

    first, tools = setup(tmp_path / "first", environment, release, monkeypatch)
    first.preflight()
    first.execute(first.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(first.receipt)
    objects, route = copy.deepcopy(tools.objects), copy.deepcopy(tools.route)
    start = len(tools.calls)
    lifecycle = Installer(environment, release, tmp_path / action, tools)
    monkeypatch.setattr(
        httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"version": 1})
    )
    with pytest.raises(Refusal, match="conditional U23"):
        if action == "cleanup":
            lifecycle.cleanup(previous)
        else:
            lifecycle.rollback(previous, "verified-user")
    assert tools.objects == objects and tools.route == route
    assert not any(
        "put-object" in args or "delete" in args for args, _ in tools.calls[start:]
    )
