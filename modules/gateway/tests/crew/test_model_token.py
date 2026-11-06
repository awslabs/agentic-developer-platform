"""Guest model tokens are short-lived and recheck their live Crew run."""

import pytest
from fastapi import HTTPException

from src.crew import model_token
from src.crew.targets import CrewLease, CrewOwner, ResolvedCrewTarget


@pytest.fixture
def current(monkeypatch):
    owner = CrewOwner("tenant-a", "owner-a")
    endpoint = "https://10.4.0.3:8443"
    target = ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            target_kind="ec2-instance",
            target_ref="box-a",
            generation=3,
            expires_at=500,
            endpoint=endpoint,
        ),
        "ready",
        host_run_id="run-a",
        host_instance_ref="i-current",
        host_fingerprint="a" * 64,
    )
    state = {"run": "run-a"}

    async def active(authorized_owner, authorized_target, *, now):
        assert authorized_owner == owner and authorized_target.lease.target_kind == "ec2-instance"
        if state["run"] is None:
            raise HTTPException(409, detail={"error": "lease_fenced"})
        return state["run"]

    monkeypatch.setattr(model_token, "active_crew_run", active)
    monkeypatch.setattr(model_token, "_secret", lambda: b"fictional-test-signing-key")
    return owner, target, state


async def test_guest_token_binds_owner_run_and_generation_and_refreshes(current):
    owner, target, state = current
    first = await model_token.mint_model_token(owner, target, now=100)
    claims = await model_token.verify_model_token(first, target, now=105)
    assert (claims["owner"], claims["tenant"], claims["run"], claims["generation"]) == (
        "owner-a",
        "tenant-a",
        "run-a",
        3,
    )
    second = await model_token.mint_model_token(owner, target, now=150)
    assert second != first
    assert (await model_token.verify_model_token(second, target, now=159))["exp"] == 210
    with pytest.raises(HTTPException) as expired:
        await model_token.verify_model_token(first, target, now=160)
    assert expired.value.status_code == 401
    state["run"] = None
    with pytest.raises(HTTPException) as revoked:
        await model_token.verify_model_token(second, target, now=160)
    assert revoked.value.status_code == 401


async def test_guest_token_refuses_other_host_owner_or_tampering(current):
    from dataclasses import replace

    owner, target, _ = current
    token = await model_token.mint_model_token(owner, target, now=100)
    for other in (
        replace(target, host_fingerprint="b" * 64),
        replace(target, host_instance_ref="i-retired"),
        replace(target, lease=target.lease.model_copy(update={"generation": 4})),
        replace(target, lease=target.lease.model_copy(update={"user_id": "other-owner"})),
    ):
        with pytest.raises(HTTPException) as denied:
            await model_token.verify_model_token(token, other, now=101)
        assert denied.value.status_code == 401
    with pytest.raises(HTTPException) as forged:
        await model_token.verify_model_token(token + "x", target, now=101)
    assert forged.value.status_code == 401
    with pytest.raises(HTTPException) as wrong_owner:
        await model_token.mint_model_token(CrewOwner("tenant-a", "other-owner"), target, now=101)
    assert wrong_owner.value.status_code == 409
