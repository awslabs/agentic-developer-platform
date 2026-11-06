import asyncio
import hashlib
import json

import pytest

from installation.expired_delivery import drain

IDENTITY = dict(org_id="org", workspace_id="workspace", operation_id="operation")


def message(key, **changes):
    envelope = dict(message_id=key, domain_operation={**IDENTITY, **changes})
    return dict(MessageId=key, ReceiptHandle="handle-" + key, Body=json.dumps(envelope))


def execute(messages, *, fail=None, proven_ids=("old",), **limits):
    events = []
    batches = [messages, []]

    async def proven(envelope):
        if fail == "proof":
            raise RuntimeError("proof unavailable")
        return envelope["message_id"] in proven_ids

    def record(key, digest, invocation, phase):
        events.append((phase, key))
        assert len(digest) == 64 and invocation == key
        if fail == phase:
            raise RuntimeError(phase)

    def ack(handle):
        events.append(("delete", handle))
        if fail == "delete":
            raise RuntimeError("unknown delete outcome")

    def release(handle):
        events.append(("release", handle))
        if fail == "release":
            raise RuntimeError("visibility unavailable")

    async def run():
        return await drain(
            receive=lambda count: batches.pop(0),
            proven=proven,
            record=record,
            acknowledge=ack,
            release=release,
            identity=IDENTITY,
            **limits,
        )

    return events, run


def test_only_exact_proven_old_delivery_deleted():
    events, run = execute(
        [message("old"), message("fresh"), message("other", workspace_id="other")]
    )
    result = asyncio.run(run())
    assert result == dict(examined=3, acknowledged=1, retained=2)
    assert events == [
        ("intended", "old"),
        ("delete", "handle-old"),
        ("acknowledged", "old"),
        ("release", "handle-fresh"),
        ("release", "handle-other"),
    ]


@pytest.mark.parametrize("failure", ["proof", "intended", "delete"])
def test_failure_releases_every_held_message(failure):
    events, run = execute([message("old"), message("fresh")], fail=failure)
    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert events[-2:] == [("release", "handle-old"), ("release", "handle-fresh")]
    if failure != "delete":
        assert not any(kind == "delete" for kind, _ in events)


def test_receipt_failure_after_delete_keeps_original_intent():
    events, run = execute([message("old"), message("fresh")], fail="acknowledged")
    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert events == [
        ("intended", "old"),
        ("delete", "handle-old"),
        ("acknowledged", "old"),
        ("release", "handle-fresh"),
    ]


def test_release_failure_still_releases_rest():
    events, run = execute([message("fresh"), message("other")], fail="release")
    with pytest.raises(RuntimeError, match="release 2 retained"):
        asyncio.run(run())
    assert events == [("release", "handle-fresh"), ("release", "handle-other")]


def test_malformed_body_retained():
    malformed = message("bad")
    malformed["Body"] = "not-json"
    events, run = execute([malformed])
    assert asyncio.run(run())["acknowledged"] == 0
    assert events == [("release", "handle-bad")]


@pytest.mark.parametrize(
    "limits", [dict(max_messages=501), dict(max_seconds=121), dict(max_messages=0)]
)
def test_unbounded_rejected(limits):
    events, run = execute([], **limits)
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert not events


@pytest.mark.parametrize("seconds", [0, 30, 300])
def test_finite_visibility_only_applies_to_selected_operation(seconds):
    from installation.expired_delivery import retained_visibility

    assert retained_visibility(message("fresh"), IDENTITY, seconds) == seconds
    assert (
        retained_visibility(message("other", workspace_id="other"), IDENTITY, seconds)
        == 0
    )
    assert retained_visibility(dict(Body="invalid"), IDENTITY, seconds) == 0
    assert retained_visibility(dict(Body="[]"), IDENTITY, seconds) == 0


@pytest.mark.parametrize("seconds", [-1, 301, True, 1.5])
def test_maintenance_refuses_unbounded_visibility_before_owner_io(seconds):
    from installation.expired_delivery import maintain

    with pytest.raises((ValueError, SystemExit)):
        asyncio.run(
            maintain(
                None,
                None,
                None,
                None,
                None,
                proof_batch=None,
                read_kube=None,
                retained_visibility_seconds=seconds,
            )
        )


def test_current_invocation_stays_visible_while_old_same_operation_is_deferred():
    from installation.expired_delivery import retained_visibility

    current = "domain-" + "a" * 32
    options = dict(immediate_invocation_id=current)
    assert retained_visibility(message(current), IDENTITY, 300, **options) == 0
    assert retained_visibility(message("older"), IDENTITY, 300, **options) == 300
    assert (
        retained_visibility(
            message("other", workspace_id="other"), IDENTITY, 300, **options
        )
        == 0
    )


@pytest.mark.parametrize(
    "invocation", ["", "domain-invalid", True, [], "domain-" + "A" * 32]
)
def test_invalid_immediate_invocation_refused_before_owner_io(invocation):
    from installation.expired_delivery import maintain

    with pytest.raises((ValueError, SystemExit)):
        asyncio.run(
            maintain(
                None,
                None,
                None,
                None,
                None,
                proof_batch=None,
                read_kube=None,
                immediate_invocation_id=invocation,
            )
        )


@pytest.mark.parametrize(
    "provider_region,management_region,queue_region,recorded_region,accepted",
    [
        ("us-east-1", None, "us-east-1", "us-east-1", True),
        ("us-west-2", "us-east-1", "us-east-1", "us-east-1", True),
        ("us-west-2", "us-east-1", "us-west-2", "us-east-1", False),
        ("us-west-2", "us-west-1", "us-west-1", "us-east-1", False),
    ],
)
@pytest.mark.parametrize("immediate", [None, "domain-" + "a" * 32])
@pytest.mark.parametrize("defer_neighbor", [False, True])
def test_maintenance_uses_recorded_management_region(
    monkeypatch,
    provider_region,
    management_region,
    queue_region,
    recorded_region,
    accepted,
    immediate,
    defer_neighbor,
):
    from types import SimpleNamespace
    from installation import expired_delivery, provider_authority

    authority = dict(
        installation_id="installation",
        org_id="org",
        workspace_id="workspace",
        region=provider_region,
        account_id="123456789012",
        credential_id="provider",
    )
    enrollment = dict(
        authority=authority, operator_role_id="AROAOWNER", environment="dev"
    )
    if management_region:
        enrollment["management_region"] = management_region
    schema = SimpleNamespace(
        validate=lambda value: value,
        canonical=lambda value: json.dumps(value, sort_keys=True),
        digest=lambda value: hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(provider_authority, "contract", lambda: schema)
    calls = []
    released = []
    proof_calls = []
    batches = [[message(immediate), message("older")], []] if immediate else [[]]
    neighbor = message("neighbor", domain="superplane", workspace_id="neighbor")
    deferred = (
        [hashlib.sha256(neighbor["Body"].encode()).hexdigest()]
        if immediate and defer_neighbor
        else []
    )
    if deferred:
        batches[0].append(neighbor)

    class Session:
        def client(self, service, **kwargs):
            calls.append((service, kwargs))
            if service == "sts":
                return SimpleNamespace(
                    get_caller_identity=lambda: dict(
                        Account="123456789012", UserId="AROAOWNER:run"
                    )
                )
            if service == "dynamodb":
                return SimpleNamespace(
                    get_item=lambda **kw: {
                        "Item": {"document": {"S": schema.canonical(authority)}}
                    }
                )
            assert service == "sqs"
            return SimpleNamespace(
                receive_message=lambda **kw: {"Messages": batches.pop(0)},
                change_message_visibility=lambda **kw: released.append(kw),
            )

    def read_kube(*args):
        kind = args[3]
        if kind == "scaledjob":
            value = {
                "metadata": {"annotations": {"autoscaling.keda.sh/paused": "true"}}
            }
        elif kind == "deployment":
            value = {
                "metadata": {"generation": 1},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "superplane-api",
                                    "env": [
                                        {
                                            "name": "SUPERPLANE_OPERATION_DISPATCH_ENABLED",
                                            "value": "false",
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                },
                "status": {
                    "observedGeneration": 1,
                    "updatedReplicas": 1,
                    "replicas": 1,
                },
            }
        else:
            assert kind == "pods"
            value = {"items": []}
        return json.dumps(value)

    installer = SimpleNamespace(
        receipt={"remote_lock": {"etag": "owned"}},
        owner="installation",
        run_id="run",
        save=lambda: None,
    )
    original = dict(
        installation_id="installation",
        status="installed-and-verified",
        environment={
            "namespace": "superplane",
            "region": recorded_region,
            "account_id": "123456789012",
            "paid_worker": {
                "queue_url": f"https://sqs.{queue_region}.amazonaws.com/123456789012/queue"
            },
        },
    )

    async def proof_batch(envelopes):
        assert immediate and envelopes[0]["message_id"] == "older"
        proof_calls.append(envelopes[0]["message_id"])
        return [False]

    async def run():
        return await expired_delivery.maintain(
            installer,
            enrollment,
            original,
            IDENTITY,
            Session(),
            proof_batch=proof_batch,
            read_kube=read_kube,
            retained_visibility_seconds=300,
            immediate_invocation_id=immediate,
            deferred_body_sha256=deferred,
        )

    if not accepted:
        with pytest.raises((ValueError, SystemExit)):
            asyncio.run(run())
        assert [service for service, _ in calls] == ["sts"]
    else:
        count = (2 if immediate else 0) + bool(deferred)
        assert asyncio.run(run()) == dict(
            examined=count, acknowledged=0, retained=count
        )
        assert (
            installer.receipt["expired_delivery_maintenance"]["immediate_invocation_id"]
            == immediate
        )
        assert [r["VisibilityTimeout"] for r in released] == (
            ([0, 300] if immediate else []) + ([300] if deferred else [])
        )
        if deferred:
            assert (
                installer.receipt["expired_delivery_maintenance"][
                    "deferred_body_sha256"
                ]
                == deferred
            )
        assert proof_calls == (["older"] if immediate else [])
        assert calls[-2:] == [
            ("sqs", {"region_name": recorded_region}),
            ("dynamodb", {"region_name": recorded_region}),
        ]


@pytest.mark.parametrize(
    "hashes,immediate,seconds",
    [
        (["a" * 64], None, 300),
        (["a" * 64], "domain-" + "a" * 32, 0),
        (["invalid"], "domain-" + "a" * 32, 300),
        ([True], "domain-" + "a" * 32, 300),
        (["a" * 64] * 2, "domain-" + "a" * 32, 300),
        ([str(i) * 64 for i in range(5)], "domain-" + "a" * 32, 300),
        ("a" * 64, "domain-" + "a" * 32, 300),
    ],
)
def test_neighbor_deferral_requires_exact_finite_observer_selection(
    hashes, immediate, seconds
):
    from installation.expired_delivery import maintain

    with pytest.raises((ValueError, SystemExit)):
        asyncio.run(
            maintain(
                None,
                None,
                None,
                None,
                None,
                proof_batch=None,
                read_kube=None,
                retained_visibility_seconds=seconds,
                immediate_invocation_id=immediate,
                deferred_body_sha256=hashes,
            )
        )


def test_neighbor_deferral_cannot_acknowledge_or_select_another_tenant():
    from installation.expired_delivery import retained_visibility

    neighbor = message("neighbor", domain="superplane", workspace_id="neighbor")
    options = dict(
        immediate_invocation_id="domain-" + "a" * 32,
        deferred_body_sha256=[hashlib.sha256(neighbor["Body"].encode()).hexdigest()],
    )
    assert retained_visibility(neighbor, IDENTITY, 300, **options) == 300
    changed = {**neighbor, "Body": neighbor["Body"] + " "}
    assert retained_visibility(changed, IDENTITY, 300, **options) == 0
    foreign = message("neighbor", domain="superplane", org_id="foreign")
    options["deferred_body_sha256"] = [
        hashlib.sha256(foreign["Body"].encode()).hexdigest()
    ]
    assert retained_visibility(foreign, IDENTITY, 300, **options) == 0
    events, run = execute([neighbor], proven_ids=("neighbor",))
    assert asyncio.run(run()) == dict(examined=1, acknowledged=0, retained=1)
    assert events == [("release", "handle-neighbor")]
