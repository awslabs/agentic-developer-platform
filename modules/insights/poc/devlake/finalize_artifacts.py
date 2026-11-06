import datetime
import os
import uuid

import finalize


MAX_ATTEMPTS = 3


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def inspect_image(scope, component, artifact):
    _, _, account, region, _, _, label = scope
    repository = "devlake-poc/" + component
    tag = artifact["tag"]
    fetched = finalize.aws(label, "ecr", "batch-get-image", "--region", region,
                           "--repository-name", repository, "--image-ids", "imageTag=" + tag)
    if fetched["failures"]:
        if (fetched["images"] or fetched["failures"] !=
                [{"imageId": {"imageTag": tag}, "failureCode": "ImageNotFound"}]):
            raise ValueError("ECR image lookup failed for registered tag")
        digest = None
    else:
        if len(fetched["images"]) != 1:
            raise ValueError("ECR image lookup is ambiguous")
        image = fetched["images"][0]
        identity = image["imageId"]
        if (image["registryId"] != account or image["repositoryName"] != repository or
                identity["imageTag"] != tag or identity["imageDigest"] != artifact["digest"]):
            raise ValueError("ECR tag is not the registered artifact")
        digest = identity["imageDigest"]
    listed = finalize.aws(label, "ecr", "list-images", "--region", region,
                          "--repository-name", repository, "--filter", "tagStatus=TAGGED")
    if listed.get("nextToken"):
        raise ValueError("ECR image inventory is incomplete")
    matches = [image["imageDigest"] for image in listed["imageIds"] if image.get("imageTag") == tag]
    if matches != ([] if digest is None else [digest]):
        raise ValueError("independent ECR image readback differs from registered tag")
    return digest


def receipt(scope, component, artifact, outcome):
    return {"pilot_id": scope[0], "component": component, "repository_uri": artifact["repository_uri"],
            "tag": artifact["tag"], "digest": artifact["digest"], "outcome": outcome,
            "verified_at": now().isoformat(), "checks": ["batch-get-image", "list-images"]}


def retire(scope, component, artifact):
    digest = inspect_image(scope, component, artifact)
    if artifact["state"] == "held":
        if digest is None:
            raise ValueError("approved held image is missing; no retention receipt")
        return receipt(scope, component, artifact, "retained")
    if digest is not None:
        _, _, _, region, _, _, label = scope
        deleted = finalize.aws(label, "ecr", "batch-delete-image", "--region", region,
                               "--repository-name", "devlake-poc/" + component,
                               "--image-ids", "imageTag=" + artifact["tag"])
        if (deleted["failures"] or deleted["imageIds"] !=
                [{"imageDigest": artifact["digest"], "imageTag": artifact["tag"]}]):
            raise ValueError("ECR deletion did not acknowledge the registered tag")
    if inspect_image(scope, component, artifact) is not None:
        raise ValueError("ECR still contains the registered tag; no absence receipt")
    return receipt(scope, component, artifact, "absent")


def run(mode, action="probe", component=None):
    finalize.event_gate(mode)
    if mode == "scheduled" and action != "probe":
        raise ValueError("scheduler can only probe or reconcile protected ledger")
    if action not in ("probe", "abort", "failure", "hold"):
        raise ValueError("unsupported finalizer action")
    scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
    actor = os.environ.get("GITHUB_ACTOR")
    if action != "probe" and (mode != "manual" or actor != record["operator"]):
        raise ValueError("only the registered operator may request abort, failure or hold")
    if action == "hold":
        if record.get("finalization_requested") or component not in record["artifacts"]:
            raise ValueError("hold requires an active registered artifact")
        artifact = record["artifacts"][component]
        if artifact["state"] in ("held", "cleaned"):
            raise ValueError("hold cannot replace an existing disposition")
        if inspect_image(scope, component, artifact) is None:
            raise ValueError("hold requires independently verified image retention")
        artifact["state"] = "held"
        artifact["hold"] = {"operator": actor, "approved_at": now().isoformat()}
        finalize.save(scope, previous, record)
        return

    request = record.get("finalization_requested")
    if request is None:
        expired = finalize.timestamp(record["expires_at"]) <= now()
        if action == "probe" and not expired:
            if finalize.timestamp(record["expires_at"]) > now() + datetime.timedelta(
                    seconds=record["deadline_seconds"]):
                finalize.probe(mode)
            return
        if action == "probe" and mode != "scheduled":
            raise ValueError("manual finalization requires explicit abort or failure")
        reason = "expiry" if action == "probe" else action
        record["finalization_requested"] = {"reason": reason, "at": now().isoformat()}
        if reason == "expiry":
            record["finalization_requested"]["scheduler_delay_seconds"] = max(
                0, int((now() - finalize.timestamp(record["expires_at"])).total_seconds()))
        finalize.save(scope, previous, record)
        request = record["finalization_requested"]
    elif action not in ("probe", request["reason"]):
        raise ValueError("terminal reason cannot be replaced")
    delayed = request.get("scheduler_delay_seconds", 0) > record["poll_seconds"]
    overdue = (now() - finalize.timestamp(request["at"])).total_seconds() > record["deadline_seconds"]
    if ((overdue or (delayed and mode == "manual")) and
            (mode != "manual" or actor != record["operator"])):
        raise ValueError("cleanup deadline or polling bound exceeded; registered operator must resume manually")
    if record.get("tool_sources"):
        import tool_sources
        if not tool_sources.complete(record["tool_sources"]):
            tool_sources.finalize_owned()
            scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
            if not tool_sources.complete(record["tool_sources"]):
                raise ValueError("owned tool cleanup is incomplete")
    if record.get("optional_resources"):
        raise ValueError("optional resource cleanup requires a qualified finalizer before artifact retirement")

    if record.get("finalization_complete"):
        if delayed and not request.get("operator_acknowledged_at"):
            if mode == "scheduled":
                raise ValueError("scheduler missed the registered polling bound; operator review required")
            request["operator_acknowledged_at"] = now().isoformat()
            finalize.save(scope, previous, record)
        return

    for selected in record["artifacts"]:
        scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
        artifact = record["artifacts"][selected]
        if artifact["state"] == "cleaned" or artifact.get("receipt"):
            continue
        progress = artifact.setdefault("cleanup", {"attempts": 0})
        lease = progress.get("lease")
        if lease and finalize.timestamp(lease["expires_at"]) > now():
            raise ValueError("another finalizer owns the cleanup lease")
        if progress["attempts"] >= MAX_ATTEMPTS:
            raise ValueError("cleanup retry limit reached; operator escalation required")
        progress["attempts"] += 1
        progress["lease"] = {"id": uuid.uuid4().hex,
                             "expires_at": (now() + datetime.timedelta(seconds=record["deadline_seconds"])).isoformat()}
        finalize.save(scope, previous, record)
        scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
        artifact = record["artifacts"][selected]
        try:
            proof = retire(scope, selected, artifact)
        except (ValueError, KeyError, OSError):
            artifact["cleanup"]["lease"] = None
            artifact["cleanup"]["last_error"] = "artifact readback or cleanup failed"
            finalize.save(scope, previous, record)
            raise
        artifact["receipt"] = proof
        artifact["cleanup"]["lease"] = None
        if proof["outcome"] == "absent":
            artifact["state"] = "cleaned"
        finalize.save(scope, previous, record)

    scope, previous, record = finalize.get_registered("finalizer", allow_expired=True)
    if all(artifact.get("receipt", {}).get("outcome") in ("absent", "retained")
           for artifact in record["artifacts"].values()):
        record["finalization_complete"] = {"at": now().isoformat(),
                                           "remaining_cost": "held artifacts require operator review" if any(
                                               artifact["state"] == "held" for artifact in record["artifacts"].values())
                                           else "registered tags absent; registry layer retention unverified"}
        finalize.save(scope, previous, record)
    if mode == "scheduled" and delayed and not request.get("operator_acknowledged_at"):
        raise ValueError("scheduler missed the registered polling bound; operator review required")
