"""Bounded installation-owner acknowledgement of proven expired deliveries.

This is queue maintenance, not execution or approval. The caller must hold the
installation lock with API dispatch and workers paused. It supplies the installed
Gateway's recovered_delivery proof reader, never caller-provided absence facts.
Unproven messages are retained; each deletion has an immutable intent receipt.
"""

import hashlib
import json
import re
import time


async def drain(
    *,
    receive,
    proven,
    record,
    acknowledge,
    release,
    identity,
    max_messages=400,
    max_seconds=120,
    clock=time.monotonic,
):
    if not 1 <= max_messages <= 500 or not 1 <= max_seconds <= 120:
        raise ValueError("bounded cleanup required")
    if set(identity) != {"org_id", "workspace_id", "operation_id"}:
        raise ValueError("exact original operation required")
    started = clock()
    held = []
    seen = set()
    result = {"examined": 0, "acknowledged": 0, "retained": 0}
    try:
        while result["examined"] < max_messages and clock() - started < max_seconds:
            messages = receive(min(10, max_messages - result["examined"]))
            if not messages:
                break
            # Retain handles before inspecting any message, including on errors.
            held.extend(messages)
            for message in messages:
                if clock() - started >= max_seconds:
                    break
                result["examined"] += 1
                message_id = message["MessageId"]
                if message_id in seen:
                    continue
                seen.add(message_id)
                try:
                    envelope = json.loads(message["Body"])
                except (ValueError, TypeError):
                    continue
                domain = (
                    envelope.get("domain_operation")
                    if isinstance(envelope, dict)
                    else None
                )
                if not isinstance(domain, dict) or any(
                    domain.get(k) != v for k, v in identity.items()
                ):
                    continue
                if not await proven(envelope):
                    continue
                digest = hashlib.sha256(message["Body"].encode()).hexdigest()
                # No credential delivery, lease, approval or existing assignment
                # is mutated. A lost delete response retains the intent receipt.
                record(message_id, digest, envelope["message_id"], "intended")
                acknowledge(message["ReceiptHandle"])
                held.remove(message)
                record(message_id, digest, envelope["message_id"], "acknowledged")
                result["acknowledged"] += 1
    finally:
        # Fresh, unrelated and unproven messages remain in the original queue.
        # No receipt handles are written into the durable evidence.
        result["retained"] = len(held)
        failures = []
        for message in held:
            try:
                release(message["ReceiptHandle"])
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError(
                f"Could not release {len(failures)} retained deliveries"
            ) from failures[0]
    return result


async def maintain(
    installer,
    enrollment,
    original,
    identity,
    session,
    *,
    proof_batch,
    read_kube,
    retained_visibility_seconds=0,
    immediate_invocation_id=None,
    deferred_body_sha256=(),
):
    """Run under the original installer lock after dispatch/worker quiescence.

    proof_batch calls the installed Gateway recovered_delivery reader for each
    body. It returns the exact message IDs it proves, never absence assertions.
    The infrastructure owner's protected table journals individual acknowledgements.
    An optional immediate invocation is retained without acknowledgement and
    released with zero visibility delay; other matching deliveries may be deferred
    for at most 300 seconds. This scheduling exception confers no execution authority
    and is recorded in the maintenance receipt. While selecting that invocation,
    an owner may also defer at most four exact, recorded envelope bodies from the
    same domain organization. This only schedules delivery for at most 300 seconds;
    it never makes a neighboring operation eligible for acknowledgement or execution.
    """
    from .provider_authority import contract, require
    from datetime import datetime, timezone

    require(
        type(retained_visibility_seconds) is int
        and 0 <= retained_visibility_seconds <= 300
    )
    require(
        immediate_invocation_id is None
        or isinstance(immediate_invocation_id, str)
        and re.fullmatch(r"domain-[0-9a-f]{32}", immediate_invocation_id)
    )
    require(
        isinstance(deferred_body_sha256, (list, tuple))
        and len(deferred_body_sha256) <= 4
        and all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in deferred_body_sha256
        )
        and len(set(deferred_body_sha256)) == len(deferred_body_sha256)
        and (
            not deferred_body_sha256
            or immediate_invocation_id is not None
            and retained_visibility_seconds > 0
        )
    )
    schema = contract()
    authority = schema.validate(enrollment["authority"])
    require(
        installer.receipt.get("remote_lock")
        and installer.owner == authority["installation_id"]
    )
    require(
        original["installation_id"] == installer.owner
        and original["status"] == "installed-and-verified"
    )
    require(
        identity["org_id"] == authority["org_id"]
        and identity["workspace_id"] == authority["workspace_id"]
    )
    caller = session.client("sts").get_caller_identity()
    require(
        caller["Account"] == authority["account_id"]
        and caller["UserId"].split(":", 1)[0] == enrollment["operator_role_id"]
    )
    namespace = original["environment"]["namespace"]

    def quiescent():
        scaled = json.loads(
            read_kube(
                "-n",
                namespace,
                "get",
                "scaledjob",
                "superplane-paid-worker",
                "-o",
                "json",
            )
        )
        require(
            scaled["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true"
        )
        api = json.loads(
            read_kube(
                "-n", namespace, "get", "deployment", "superplane-api", "-o", "json"
            )
        )
        container = next(
            c
            for c in api["spec"]["template"]["spec"]["containers"]
            if c["name"] == "superplane-api"
        )
        require(
            next(
                e["value"]
                for e in container["env"]
                if e["name"] == "SUPERPLANE_OPERATION_DISPATCH_ENABLED"
            )
            == "false"
        )
        require(
            api["status"].get("observedGeneration", 0) >= api["metadata"]["generation"]
        )
        require(
            api["status"].get("updatedReplicas")
            == api["status"].get("replicas")
            == api["spec"]["replicas"]
        )
        pods = json.loads(
            read_kube(
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                "scaledjob.keda.sh/name=superplane-paid-worker",
                "-o",
                "json",
            )
        )["items"]
        require(all(p["status"]["phase"] in {"Succeeded", "Failed"} for p in pods))

    quiescent()
    management_region = enrollment.get("management_region", authority["region"])
    require(
        management_region == original["environment"]["region"]
        and authority["account_id"] == original["environment"]["account_id"]
    )
    queue = original["environment"]["paid_worker"]["queue_url"]
    require(
        queue.startswith(
            f"https://sqs.{management_region}.amazonaws.com/{authority['account_id']}/"
        )
    )
    sqs = session.client("sqs", region_name=management_region)
    db = session.client("dynamodb", region_name=management_region)
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    current = db.get_item(
        TableName=table,
        Key={"record_id": {"S": authority["credential_id"]}},
        ConsistentRead=True,
    ).get("Item")
    require(current and current["document"]["S"] == schema.canonical(authority))
    cache = {}
    received = {}

    def receive(count):
        quiescent()
        messages = sqs.receive_message(
            QueueUrl=queue,
            MaxNumberOfMessages=count,
            VisibilityTimeout=180,
            WaitTimeSeconds=1,
        ).get("Messages", [])
        received.update({m["ReceiptHandle"]: m for m in messages})
        return messages

    # Read proof lazily: drain owns all visibility receipts before proof can fail.
    async def proven(envelope):
        if (
            immediate_invocation_id is not None
            and envelope.get("message_id") == immediate_invocation_id
        ):
            return False
        envelope_digest = schema.digest(envelope)
        if envelope_digest not in cache:
            result = await proof_batch([envelope])
            require(
                isinstance(result, list)
                and len(result) == 1
                and isinstance(result[0], bool)
            )
            cache[envelope_digest] = result[0]
        return cache[envelope_digest]

    def record(message_id, body_digest, invocation, phase):
        key = "spqd1:" + schema.digest(
            {"queue": queue, "message_id": message_id, "phase": phase}
        )
        value = {
            "version": 1,
            "record_id": key,
            "queue": queue,
            "message_id": message_id,
            "body_sha256": body_digest,
            "invocation": invocation,
            "phase": phase,
            "identity": identity,
            "installation_id": installer.owner,
            "operator_role_id": enrollment["operator_role_id"],
            "proof_reader": "installed-gateway-recovered_delivery",
            "run_id": installer.run_id,
        }
        item = {
            "record_id": {"S": key},
            "document": {"S": schema.canonical(value)},
            "revision": {"S": schema.digest(value)},
        }
        try:
            db.put_item(
                TableName=table,
                Item=item,
                ConditionExpression="attribute_not_exists(record_id)",
            )
        except db.exceptions.ConditionalCheckFailedException:
            require(
                db.get_item(
                    TableName=table, Key={"record_id": {"S": key}}, ConsistentRead=True
                ).get("Item")
                == item
            )

    result = await drain(
        receive=receive,
        proven=proven,
        record=record,
        acknowledge=lambda handle: sqs.delete_message(
            QueueUrl=queue, ReceiptHandle=handle
        ),
        release=lambda handle: sqs.change_message_visibility(
            QueueUrl=queue,
            ReceiptHandle=handle,
            VisibilityTimeout=retained_visibility(
                received[handle],
                identity,
                retained_visibility_seconds,
                immediate_invocation_id=immediate_invocation_id,
                deferred_body_sha256=deferred_body_sha256,
            ),
        ),
        identity=identity,
    )
    installer.receipt["expired_delivery_maintenance"] = {
        **result,
        "identity": identity,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "retained_visibility_seconds": retained_visibility_seconds,
        "immediate_invocation_id": immediate_invocation_id,
    }
    if deferred_body_sha256:
        installer.receipt["expired_delivery_maintenance"]["deferred_body_sha256"] = (
            list(deferred_body_sha256)
        )
    installer.save()
    return result


def retained_visibility(
    message, identity, seconds, *, immediate_invocation_id=None, deferred_body_sha256=()
):
    """A finite scheduling lease only for the selected original operation.

    No acknowledgement or execution authority is inferred. Restored dispatch
    can publish the unchanged current invocation while these copies remain held.
    Unselected or malformed messages are released immediately. The optional exact
    same-organization body hashes only defer delivery; drain still excludes every
    other operation from its acknowledgement proof and deletion path.
    """
    try:
        envelope = json.loads(message["Body"])
        domain = envelope.get("domain_operation")
        if isinstance(domain, dict) and all(
            domain.get(k) == v for k, v in identity.items()
        ):
            if (
                immediate_invocation_id is not None
                and envelope.get("message_id") == immediate_invocation_id
            ):
                return 0
            return seconds
        if (
            immediate_invocation_id is not None
            and isinstance(domain, dict)
            and domain.get("domain") == "superplane"
            and domain.get("org_id") == identity["org_id"]
            and hashlib.sha256(message["Body"].encode()).hexdigest()
            in deferred_body_sha256
        ):
            return seconds
    except (ValueError, TypeError, AttributeError):
        pass
    return 0
