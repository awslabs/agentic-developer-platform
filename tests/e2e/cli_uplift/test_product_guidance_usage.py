"""The Product actor does not acquire usage-log access for guidance tests."""

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui import test_product_guidance as live


def test_effect_capture_uses_verified_control_actor_for_independent_usage(monkeypatch):
    calls = []

    def read(url, *, headers, timeout):
        calls.append((url, headers, timeout))
        return SimpleNamespace(status=200, json=lambda: {"items": [], "total": 0})

    def capture(_tenant, _actor, _model, _mode, read_usage, *_args):
        return read_usage()

    monkeypatch.setattr(live, "capture_guidance_effects", capture)
    provider = SimpleNamespace(
        request=SimpleNamespace(get=read),
        usage_control={"access_token": "control-fixture"},
    )
    fixture = {
        "tenant_id": "owned-tenant",
        "actor_id": "ordinary-owner",
        "owner": {"access_token": "owner-fixture"},
    }
    observed = live.capture_client_effect_turn(
        fixture,
        "https://example.invalid",
        provider,
        "session-1",
        "fixture-model",
        "model",
        lambda: {},
        lambda: {},
    )
    assert observed == {"items": [], "total": 0}
    assert calls == [
        (
            "https://example.invalid/api/usage/logs?org_id=owned-tenant&user_id=ordinary-owner&model=fixture-model&limit=100",
            {"Authorization": "Bearer control-fixture"},
            30_000,
        )
    ]


def test_effect_capture_rejects_denied_privileged_read(monkeypatch):
    monkeypatch.setattr(live, "capture_guidance_effects", lambda *_args: _args[4]())
    provider = SimpleNamespace(
        request=SimpleNamespace(
            get=lambda *_args, **_kwargs: SimpleNamespace(status=403)
        ),
        usage_control={"access_token": "control-fixture"},
    )
    with pytest.raises(ValueError, match="usage readback"):
        live.capture_client_effect_turn(
            {"tenant_id": "owned-tenant", "actor_id": "ordinary-owner"},
            "https://example.invalid",
            provider,
            "session-1",
            "fixture-model",
            "model",
            lambda: {},
            lambda: {},
        )


def test_effect_report_is_private_and_rejects_incomplete_turns(tmp_path):
    fixture = {
        "run_id": "product-42-1",
        "source_revision": "a" * 40,
        "evidence_context": {"identities": {"requirements_revision": "b" * 40}},
    }
    turns = {}
    usage_ids = []
    for mode in ("deterministic", "model", "direct"):
        after = usage_ids + (["usage-1"] if mode == "model" else [])
        turns[mode] = {
            "mode": mode,
            "tenant_id": "tenant",
            "actor_id": "owner",
            "model_id": "fixture-model",
            "metered_requests": int(mode == "model"),
            "request_ids": ["request-1"] if mode == "model" else [],
            "proposal_ids": ["proposal-1"] if mode == "model" else [],
            "usage_row_ids_before": usage_ids,
            "usage_row_ids_after": after,
            "provider_before": {"repository_ids": ["verified-1"]},
            "provider_after": {"repository_ids": ["verified-1"]},
            "profile_revision_before": 1,
            "profile_revision_after": 2 if mode == "direct" else 1,
        }
        usage_ids = after
    path = live.write_client_effect_report(fixture, turns, str(tmp_path))
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert (
        stat.S_IMODE(
            tmp_path.joinpath("product-42-1-guidance-effects.json").stat().st_mode
        )
        == 0o600
    )
    assert json.loads(Path(path).read_text(encoding="utf-8"))["turns"] == turns
    with pytest.raises(ValueError, match="all three"):
        live.write_client_effect_report(
            fixture, {"model": turns["model"]}, str(tmp_path)
        )
