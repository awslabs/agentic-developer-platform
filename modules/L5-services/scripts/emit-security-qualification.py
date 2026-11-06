#!/usr/bin/env python3
"""Emit native Security readbacks without claiming unobserved release checks."""

import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from release_context import WORKFLOW, authorize_engine, release_identity


WORKFLOW_PATH = f".github/workflows/{WORKFLOW}"


def rollup_module():
    specification = importlib.util.spec_from_file_location(
        "security_qualification_rollup", Path(__file__).with_name("qualification-rollup.py"))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def private_readback(environment):
    directory = Path(environment["PRIVATE_EVIDENCE_DIR"]).resolve(strict=True)
    if directory.is_relative_to(Path(__file__).resolve().parents[3]) or directory.stat().st_mode & 0o077:
        raise ValueError("native readback must remain in a private operator directory")
    path = directory / (f"security-readback-{environment['GITHUB_RUN_ID']}-"
                        f"{environment['GITHUB_RUN_ATTEMPT']}.json")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("native readback has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144:
        raise ValueError("native readback exceeds its bound")
    return json.loads(content), hashlib.sha256(content).hexdigest()


def selected_fixture(environment, context):
    path = Path(environment["PRIVATE_FIXTURE"])
    if (not path.is_absolute() or path.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3])):
        raise ValueError("selected Security fixture cannot be read from the checkout")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("selected Security fixture has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if (len(content) > 262144 or hashlib.sha256(content).hexdigest() != environment["L5_FIXTURE_DIGEST"]):
        raise ValueError("selected Security fixture changed after release preflight")
    fixture = json.loads(content)
    if (not isinstance(fixture, dict)
            or not isinstance(fixture.get("authorization"), dict)
            or fixture["authorization"].get("source_sha") != context["source_revision"]
            or not isinstance(fixture.get("release"), dict)
            or fixture["release"].get("worker") != context["source_revision"]
            or not isinstance(fixture.get("cases"), list) or len(fixture["cases"]) != 2):
        raise ValueError("selected Security fixture belongs to another release")
    return fixture


def verify_review_receipt(plan, record):
    path = Path(plan["review_file"])
    if (not path.is_absolute() or path.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3])):
        raise ValueError("independent rubric must remain outside the checkout")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("independent rubric has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if (len(content) > 262144 or hashlib.sha256(content).hexdigest() != record["rubric_sha256"]):
        raise ValueError("independent rubric changed after native readback")
    rubric = json.loads(content)
    if (not isinstance(rubric, dict) or rubric.get("run_id") != record["run_id"]
            or rubric.get("result_id") != record["result_id"]
            or rubric.get("native_job_ref") != record["native_job_ref"]
            or not isinstance(rubric.get("rubric_revision"), str) or not rubric["rubric_revision"]
            or not isinstance(rubric.get("review_sample"), list)
            or not isinstance(rubric.get("known_controls"), list)
            or not isinstance(rubric.get("reviews"), dict)
            or len(rubric["reviews"]) != record["reviewed"]):
        raise ValueError("independent rubric belongs to another Product result")
    reviews = rubric["reviews"]
    source_revisions = {source["record_revision"] for source in plan.get("input", {}).get("sources", record.get("sources", []))}
    finding_ids = record.get("native_finding_ids")
    if (not isinstance(finding_ids, list) or len(finding_ids) != record["finding_count"]
            or any(not isinstance(native_id, str) or not native_id for native_id in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)):
        raise ValueError("independent rubric lacks native provider finding identities")
    if any(not isinstance(native_id, str) or not native_id for native_id in rubric["review_sample"]):
        raise ValueError("independent rubric has invalid sampled finding identities")
    required = set(finding_ids) if len(finding_ids) < 3 else set(rubric["review_sample"])
    if (len(required) != record["reviewed"] or not required <= set(finding_ids)
            or set(reviews) != required
            or record["finding_count"] == 0 and not rubric["known_controls"]
            or len(finding_ids) >= 3 and len(rubric["review_sample"]) != 3
            or any(not isinstance(control, str) or not control for control in rubric["known_controls"])
            or any(not isinstance(native_id, str) or not native_id
                   or not isinstance(review, dict)
                   or review.get("judgment") not in {"supported", "contradicted", "uncertain"}
                   or not isinstance(review.get("evidence_ref"), str) or not review["evidence_ref"]
                   or review.get("source_revision") not in source_revisions
                   for native_id, review in reviews.items())):
        raise ValueError("independent rubric lacks cited source judgments or checked controls")


def verify_pinned_sources(sources, scope):
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(source, dict)
                   or source.get("tenant_id") != scope.get("tenant_id")
                   or source.get("project_id") != scope.get("project_id")
                   or not isinstance(source.get("record_revision"), str)
                   or not source["record_revision"].strip()
                   or source["record_revision"].strip().lower() in {"head", "latest", "null"}
                   for source in sources)):
        raise ValueError("selected Security source is foreign or not pinned")


def verify_selected_products(products, fixture):
    cases = fixture["cases"]
    if (any(not isinstance(case, dict) or not isinstance(case.get("scope"), dict)
            or not isinstance(case.get("assessments"), list) or len(case["assessments"]) != 2
            for case in cases)
            or {json.dumps(case["scope"], sort_keys=True) for case in cases}
            != {json.dumps(product["scope"], sort_keys=True) for product in products}):
        raise ValueError("native Product results differ from the selected fixture scopes")
    for case in cases:
        product = next(item for item in products if item["scope"] == case["scope"])
        provider = case.get("provider")
        if (not isinstance(provider, dict)
                or any(not provider.get(field) for field in ("account_id", "role_arn", "region"))):
            raise ValueError("selected Product provider identity is incomplete")
        plans = {item.get("capability"): item for item in case["assessments"]
                 if isinstance(item, dict)}
        if set(plans) != {"code_review", "threat_model"} or len(plans) != 2:
            raise ValueError("selected Security assessment plans are incomplete")
        for assessment in product["assessments"]:
            plan = plans[assessment["capability"]]
            selection = plan.get("input")
            verify_pinned_sources(assessment["sources"], case["scope"])
            if (not isinstance(selection, dict) or assessment["sources"] != selection.get("sources")
                    or assessment.get("binding_id") != plan.get("binding_id")
                    or selection.get("binding_id") != plan.get("binding_id")
                    or not isinstance(assessment.get("provider_identity"), dict)
                    or any(assessment["provider_identity"].get(field) != provider[field]
                           for field in ("account_id", "role_arn", "region"))):
                raise ValueError("native Product result differs from the pinned source and provider binding")
            source_digests = assessment.get("source_sha256")
            if (not isinstance(source_digests, list) or len(source_digests) != len(assessment["sources"])
                    or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                           for digest in source_digests)
                    or assessment.get("snapshot_sha256") != source_digests
                    or not isinstance(assessment.get("rubric_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", assessment["rubric_sha256"])
                    or type(assessment.get("finding_count")) is not int or assessment["finding_count"] < 0
                    or type(assessment.get("reviewed")) is not int
                    or assessment["reviewed"] != min(assessment["finding_count"], 3)):
                raise ValueError("native assessment lacks measured source and review evidence")
            verify_review_receipt(plans[assessment["capability"]], assessment)
    records = [record for product in products for record in product["assessments"]]
    if (len({record["run_id"] for record in records}) != 4
            or len({record["result_id"] for record in records}) != 4
            or any(not isinstance(record["native_job_ref"], dict) for record in records)
            or len({json.dumps(record["native_job_ref"], sort_keys=True) for record in records}) != 4):
        raise ValueError("native Product results share a run, result or provider job")


def verify_cleanup_packet(cleanup, environment):
    filename = (f"security-cleanup-{environment['GITHUB_RUN_ID']}-"
                f"{environment['GITHUB_RUN_ATTEMPT']}.json")
    reference = cleanup["readback"][0]
    match = re.fullmatch(r"private://security/native-cleanup/" + re.escape(filename)
                         + r"#sha256:([0-9a-f]{64})", reference)
    if not match:
        raise ValueError("owned-cleanup readback belongs to another release run")
    path = Path(environment["PRIVATE_EVIDENCE_DIR"]).resolve(strict=True) / filename
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("owned-cleanup packet has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144 or hashlib.sha256(content).hexdigest() != match[1]:
        raise ValueError("owned-cleanup packet differs from the native readback")
    packet = json.loads(content)
    if not isinstance(packet, dict) or packet.get("run_id") != cleanup["run_id"]:
        raise ValueError("owned-cleanup packet has another recovery run")
    ownership, resources = packet.get("ownership"), packet.get("resources")
    if (not isinstance(ownership, dict) or not isinstance(resources, dict)
            or set(ownership) != set(resources)
            or sum(value == "scenario_owned" for value in ownership.values()) != cleanup["owned_absent"]
            or sum(value == "customer_linked" for value in ownership.values()) != cleanup["linked_retained"]
            or any(value not in {"scenario_owned", "customer_linked"} for value in ownership.values())
            or any(resources[key] != ("absent" if value == "scenario_owned" else "retained")
                   for key, value in ownership.items())):
        raise ValueError("owned-cleanup packet does not establish native ownership and retention")
    return packet


def verified_selected_cleanup(selection, packet, journal, cleanup):
    owned, linked = selection.get("owned"), selection.get("linked")
    if not isinstance(owned, list) or not owned or not isinstance(linked, list):
        return False
    if (any(not isinstance(resource, str) or not resource for resource in owned + linked)
            or len(set(owned + linked)) != len(owned + linked)):
        raise ValueError("selected cleanup contains overlapping or invalid resources")
    ownership = {**{resource: "scenario_owned" for resource in owned},
                 **{resource: "customer_linked" for resource in linked}}
    if packet["ownership"] != ownership:
        raise ValueError("native cleanup differs from selected owned and linked resources")
    try:
        entries = [json.loads(line) for line in journal.splitlines()]
        attempts = [entry for entry in entries if entry.get("operation") == "cleanup"]
        cleaned_at = datetime.fromisoformat(cleanup["observed_at"])
        acknowledged_at = datetime.fromisoformat(attempts[1]["observed_at"])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise ValueError("selected cleanup has no durable acknowledged attempt") from error
    if (len(attempts) != 2 or [entry.get("status") for entry in attempts] != ["prepared", "acknowledged"]
            or any(entry.get("attempt_id") != "cleanup" or entry.get("work_id") != cleanup["run_id"]
                   or entry.get("run_id") != cleanup["run_id"] for entry in attempts)
            or acknowledged_at.utcoffset() is None or acknowledged_at > cleaned_at):
        raise ValueError("selected cleanup differs from its acknowledged work attempt")
    return True


def verified_gateway(readback, environment, now, observed_at):
    gateway = readback.get("gateway")
    if gateway is None:
        return None
    role = environment["L5_MUTATION_ROLE_ARN"]
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+", role)
    if not match or not isinstance(gateway, dict):
        raise ValueError("Gateway runtime readback has no selected account")
    cluster = f"adp-{environment['L5_ENVIRONMENT']}-eks-cluster"
    if (gateway.get("revision") != environment["SOURCE_SHA"]
            or gateway.get("account_id") != match[1]
            or gateway.get("region") != environment["AWS_REGION"]
            or gateway.get("cluster") != cluster or gateway.get("namespace") != "adp-gateway"
            or not isinstance(gateway.get("image_digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", gateway["image_digest"])
            or not isinstance(gateway.get("pod_uids"), list) or not gateway["pod_uids"]
            or any(not isinstance(uid, str) or not uid for uid in gateway["pod_uids"])
            or len(gateway["pod_uids"]) != len(set(gateway["pod_uids"]))
            or not isinstance(gateway.get("migration_head"), str) or not gateway["migration_head"]):
        raise ValueError("Gateway serving readback does not match the selected release")
    if gateway.get("wheel_sha256") != environment["WHEEL_DIGEST"]:
        raise ValueError("Gateway serving wheel differs from selected worker wheel")
    try:
        measured = datetime.fromisoformat(gateway["observed_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("Gateway serving readback has no observation time") from error
    if measured.utcoffset() is None or not 0 <= (now - measured).total_seconds() <= 900 or measured > observed_at:
        raise ValueError("Gateway serving readback is stale or after its receipt")
    return gateway["revision"]


def verified_installed_scope(installed, environment):
    reference = installed.get("scope_receipt")
    if reference is None:
        return None
    filename = f"installed-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}.json"
    match = re.fullmatch(r"private://security/installed/" + re.escape(filename)
                         + r"#sha256:([0-9a-f]{64})", reference)
    if not match:
        raise ValueError("installed scope receipt belongs to another release run")
    path = Path(environment["PRIVATE_EVIDENCE_DIR"]).resolve(strict=True) / filename
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("installed scope receipt is not a private regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144 or hashlib.sha256(content).hexdigest() != match[1]:
        raise ValueError("installed scope changed after its native readback")
    packet = json.loads(content)
    role = environment["L5_MUTATION_ROLE_ARN"]
    account = role.split(":")[4]
    region = environment["AWS_REGION"]
    target = environment["L5_ENVIRONMENT"]
    scope = packet.get("scope")
    if (packet.get("action") != "status" or packet.get("result") != "passed"
            or packet.get("observed") != {key: value for key, value in installed.items()
                                          if key != "scope_receipt"}
            or not isinstance(scope, dict) or set(scope) != {"roles", "queues"}
            or not isinstance(scope["roles"], dict) or set(scope["roles"]) != {"worker", "repair"}
            or not isinstance(scope["queues"], dict) or set(scope["queues"]) != {"work", "dead_letter"}):
        raise ValueError("installed scope differs from the selected worker and repair")
    for label in ("worker", "repair"):
        name = f"adp-{target}-l5-{label}"
        role_scope = scope["roles"][label]
        if (not isinstance(role_scope, dict)
                or role_scope.get("role_arn") != f"arn:aws:iam::{account}:role/{name}"
                or role_scope.get("policy_name") != name
                or role_scope.get("trust_policy") != {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                   "Principal": {"Service": "lambda.amazonaws.com"}}],
                }
                or not isinstance(role_scope.get("boundary_arn"), str)
                or not re.fullmatch(rf"arn:aws:iam::{account}:policy/[A-Za-z0-9_/-]+",
                                    role_scope["boundary_arn"])):
            raise ValueError("installed execution role differs from the selected account")
    for label, suffix, retention in (("work", "", 345600), ("dead_letter", "-dlq", 1209600)):
        queue = scope["queues"][label]
        if (not isinstance(queue, dict)
                or queue.get("arn") != f"arn:aws:sqs:{region}:{account}:adp-{target}-l5-work{suffix}"
                or queue.get("retention_seconds") != retention):
            raise ValueError("installed queue scope differs from the selected account")
    return match[1]


def verified_triggers(readback, fixture, products, now, observed_at):
    records = readback.get("triggers")
    cases = fixture["cases"]
    if records is None and all(case.get("trigger_event") is None for case in cases):
        return 0
    if not isinstance(records, list) or len(records) != len(cases):
        raise ValueError("selected event readback is missing or incomplete")
    selected_runs = {assessment["run_id"] for product in products for assessment in product["assessments"]}
    selected_results = {assessment["result_id"] for product in products for assessment in product["assessments"]}
    selected_jobs = {json.dumps(assessment["native_job_ref"], sort_keys=True)
                     for product in products for assessment in product["assessments"]}
    seen_events, seen_runs, seen_results, seen_jobs = set(), set(), set(), set()
    for case, record in zip(cases, records, strict=True):
        event = case.get("trigger_event")
        if not isinstance(event, dict) or not isinstance(record, dict):
            raise ValueError("selected event has no independent readback")
        selection_input = event.get("input")
        if not isinstance(selection_input, dict):
            raise ValueError("selected event has no source selection")
        sources = selection_input.get("sources")
        verify_pinned_sources(sources, case["scope"])
        digests = record.get("source_sha256")
        selected = [plan for plan in case["assessments"] if plan["capability"] == event.get("capability")]
        if (len(selected) != 1 or selection_input.get("binding_id") != selected[0].get("binding_id")
                or sources == selected[0]["input"]["sources"]
                or record.get("event_id") != event.get("event_id")
                or record.get("binding_id") != selection_input.get("binding_id")
                or not isinstance(case.get("provider"), dict)
                or not isinstance(record.get("provider_identity"), dict)
                or any(record["provider_identity"].get(field) != case["provider"].get(field)
                       or not case["provider"].get(field)
                       for field in ("account_id", "role_arn", "region"))
                or not record.get("run_id") or not record.get("result_id")
                or not isinstance(record.get("native_job_ref"), dict)
                or not isinstance(sources, list) or not sources
                or not isinstance(digests, list) or len(digests) != len(sources)
                or record.get("snapshot_sha256") != digests
                or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                       for digest in digests)
                or type(record.get("finding_count")) is not int or record["finding_count"] < 0
                or type(record.get("reviewed")) is not int
                or record["reviewed"] != min(record["finding_count"], 3)
                or not isinstance(record.get("rubric_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["rubric_sha256"])):
            raise ValueError("selected event differs from independent Product or provider result")
        job = json.dumps(record["native_job_ref"], sort_keys=True)
        if (record["event_id"] in seen_events or record["run_id"] in seen_runs | selected_runs
                or record["result_id"] in seen_results | selected_results
                or job in seen_jobs | selected_jobs):
            raise ValueError("selected event shares a Product or native job")
        seen_events.add(record["event_id"])
        seen_runs.add(record["run_id"])
        seen_results.add(record["result_id"])
        seen_jobs.add(job)
        verify_review_receipt(event, record)
        path = Path(case["assessment_journal_file"])
        if (not path.is_absolute() or path.parent.resolve(strict=True).is_relative_to(
                Path(__file__).resolve().parents[3]) or path.parent.stat().st_mode & 0o077):
            raise ValueError("selected event journal is not private")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
                raise ValueError("selected event journal is unsafe")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                content = source.read(65537)
        finally:
            os.close(descriptor)
        if (len(content) > 65536 or not content.endswith(b"\n")
                or not isinstance(record.get("journal_sha256"), str)
                or hashlib.sha256(content).hexdigest() != record["journal_sha256"]):
            raise ValueError("selected event journal differs from readback")
        entries = [json.loads(line) for line in content.splitlines()]
        if (len(entries) != 8 or any(not isinstance(entry, dict) for entry in entries)
                or any(entry.get("operation") != "assessment_submit" for entry in entries[:4])):
            raise ValueError("selected event journal is incomplete")
        previous = None
        for index, entry in enumerate(entries[4:]):
            try:
                recorded = datetime.fromtimestamp(float(entry["observed_at"]), UTC)
            except (KeyError, ValueError, TypeError, OverflowError) as error:
                raise ValueError("selected event has no bounded attempt time") from error
            if (entry.get("operation") != "event_delivery"
                    or entry.get("attempt_id") != f"{event['event_id']}-{index // 2}"
                    or entry.get("status") != ("prepared" if index % 2 == 0 else "acknowledged")
                    or entry.get("work_id") != event["event_id"]
                    or entry.get("effect_id") != (record["run_id"] if index % 2 else None)
                    or not 0 <= (now - recorded).total_seconds() <= 900 or recorded > observed_at
                    or previous is not None and recorded < previous):
                raise ValueError("selected event delivery differs from Product run")
            previous = recorded
        try:
            measured = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError("selected event has no observation time") from error
        if measured.utcoffset() is None or not 0 <= (now - measured).total_seconds() <= 900 or measured > observed_at:
            raise ValueError("selected event readback is stale or future-dated")
        inventories = []
        for phase in ("before", "after"):
            inventory_path = Path(str(path) + f".event-{phase}.json")
            descriptor = os.open(inventory_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
                    raise ValueError("selected event inventory is unsafe")
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    inventory_bytes = source.read(65537)
            finally:
                os.close(descriptor)
            if (not inventory_bytes or len(inventory_bytes) > 65536 or not inventory_bytes.endswith(b"\n")
                    or not isinstance(record.get(f"{phase}_sha256"), str)
                    or hashlib.sha256(inventory_bytes).hexdigest() != record[f"{phase}_sha256"]):
                raise ValueError("selected event inventory differs from the private readback")
            inventories.append(json.loads(inventory_bytes))
        before, after = inventories
        try:
            before_at = datetime.fromisoformat(before["observed_at"])
            after_at = datetime.fromisoformat(after["observed_at"])
            first_at = datetime.fromtimestamp(float(entries[4]["observed_at"]), UTC)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("selected event inventory has no measured transition") from error
        product = next((item for item in products if item["scope"] == case["scope"]), None)
        previous_submit = None
        for index, entry in enumerate(entries[:4]):
            plan = case["assessments"][index // 2]
            matches = ([item for item in product["assessments"]
                        if item["capability"] == plan["capability"]]
                       if product is not None else [])
            try:
                submitted_at = datetime.fromisoformat(entry["observed_at"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("selected assessment submit has no usable time") from error
            if (len(matches) != 1 or not isinstance(plan.get("receipt"), str) or not plan["receipt"]
                    or entry.get("attempt_id") != plan["receipt"]
                    or entry.get("work_id") != plan["receipt"]
                    or entry.get("status") != ("acknowledged" if index % 2 else "prepared")
                    or entry.get("run_id") is not None or entry.get("native_job_id") is not None
                    or entry.get("effect_id") != (matches[0]["run_id"] if index % 2 else None)
                    or entry.get("idempotency_key") != (plan["receipt"] if index % 2 else None)
                    or submitted_at.utcoffset() is None or submitted_at > before_at
                    or previous_submit is not None and submitted_at < previous_submit):
                raise ValueError("selected assessment submit differs from pre-event Product work")
            previous_submit = submitted_at
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or not isinstance(before.get("jobs"), list) or not isinstance(after.get("jobs"), list)
                or any(not isinstance(item, dict) for item in [*before["jobs"], *after["jobs"]])
                or before.get("event_id") != event["event_id"] or after.get("event_id") != event["event_id"]
                or before.get("scope") != case["scope"] or after.get("scope") != case["scope"]
                or before.get("space") != after.get("space") or not before.get("space")
                or before.get("run_id") not in {
                    item["run_id"] for product in products if product["scope"] == case["scope"]
                    for item in product["assessments"] if item["capability"] == event["capability"]}
                or after.get("run_id") != record["run_id"]
                or before_at.utcoffset() is None or after_at.utcoffset() is None
                or not before_at <= first_at <= previous <= after_at <= measured):
            raise ValueError("selected event inventory does not bound both deliveries")
        prior = {json.dumps(item, sort_keys=True) for item in before["jobs"]}
        current = {json.dumps(item, sort_keys=True) for item in after["jobs"]}
        if (len(prior) != len(before["jobs"]) or len(current) != len(after["jobs"])
                or not prior <= current or current - prior != {job}):
            raise ValueError("selected event created missing or duplicated native work")
    return len(records)


def verified_setup(readback, fixture, now, observed_at):
    records = readback.get("setup")
    if records is None:
        return 0
    cases = fixture["cases"]
    if (not isinstance(records, list) or len(records) != len(cases)
            or len({record.get("run_id") for record in records if isinstance(record, dict)}) != len(cases)):
        raise ValueError("native setup readback lacks distinct Product runs")
    for case, record in zip(cases, records, strict=True):
        selected = case.get("selected")
        if (not isinstance(record, dict) or not isinstance(selected, dict)
                or record.get("scope") != case["scope"] or selected.get("scope") != case["scope"]
                or record.get("sources") != selected.get("sources")
                or case.get("run_input", {}).get("sources") != record["sources"]
                or not record.get("run_id")
                or any(type(record.get(field)) is not int or record[field] < 1
                       for field in ("configuration_revision", "binding_revision", "run_revision"))):
            raise ValueError("native setup differs from the selected Product or source")
        try:
            measured = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError("native setup has no observation time") from error
        if measured.utcoffset() is None or not 0 <= (now - measured).total_seconds() <= 900 or measured > observed_at:
            raise ValueError("native setup readback is stale or after its receipt")
    return len(records)


def verify_replay_receipt(content, replay, selection, agent_run_id, now, context_at):
    keys = replay.get("page_keys")
    prefix = f"{selection['scope']['tenant_id']}/{selection['result_id']}/"
    if (not isinstance(keys, list) or not keys or 2 * len(keys) != replay.get("attempts")
            or 2 * len(keys) + 1 > selection["max_attempts"]
            or any(not isinstance(key, str) or not key.startswith(prefix) for key in keys)
            or len(keys) != len(set(keys))):
        raise ValueError("selected Context replay has foreign or unbounded page keys")
    try:
        entries = [json.loads(line) for line in content.splitlines()]
        replay_at = datetime.fromisoformat(replay["observed_at"])
    except (UnicodeError, KeyError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("selected Context replay journal is malformed") from error
    expected = []
    for repeat in range(2):
        for index, key in enumerate(keys):
            for status in ("prepared", "acknowledged"):
                expected.append(("context_replay", status, f"replay-{repeat}-{index}",
                                 selection["publication_id"], key))
    expected.extend((("later_agent_query", "prepared", "later-query", selection["publication_id"],
                      selection["query_receipt"]),
                     ("later_agent_query", "acknowledged", "later-query", agent_run_id,
                      selection["query_receipt"])))
    if len(entries) != len(expected):
        raise ValueError("selected Context replay journal has incomplete attempts")
    previous = None
    for entry, expected_entry in zip(entries, expected, strict=True):
        if (not isinstance(entry, dict) or set(entry) != {
                "attempt_id", "run_id", "operation", "status", "work_id", "observed_at",
                "native_job_id", "effect_id", "idempotency_key"}):
            raise ValueError("selected Context replay journal contains an invalid attempt")
        try:
            observed = datetime.fromisoformat(entry["observed_at"])
            actual = tuple(entry[field] for field in ("operation", "status", "attempt_id", "work_id",
                                                     "idempotency_key"))
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError("selected Context replay journal contains an incomplete attempt") from error
        if (actual != expected_entry or entry.get("run_id") != selection["run_id"]
                or entry.get("effect_id") is not None or entry.get("native_job_id") is not None
                or observed.utcoffset() is None or not 0 <= (now - observed).total_seconds() <= 900
                or observed > context_at or previous is not None and observed < previous):
            raise ValueError("selected Context replay journal differs from selected work")
        previous = observed
    if previous != replay_at:
        raise ValueError("selected Context replay journal has another observation time")


def verified_context(readback, fixture, products, now, observed_at):
    context = readback.get("context")
    selected_case = fixture["cases"][0]
    selection = selected_case.get("context_reuse")
    if context is None:
        if selection is not None:
            raise ValueError("selected Context readback is missing")
        return 0, 0, False
    assessments = next(item["assessments"] for item in products if item["scope"] == selected_case["scope"])
    plan = selection.get("assessment_plan") if isinstance(selection, dict) else None
    plan_input = plan.get("input") if isinstance(plan, dict) else None
    if (not isinstance(context, dict) or not isinstance(selection, dict)
            or not isinstance(plan_input, dict)
            or context.get("scope") != selected_case["scope"]
            or selection.get("scope") != selected_case["scope"]
            or any(context.get(field) != selection.get(field)
                   for field in ("run_id", "result_id", "publication_id"))
            or context.get("sources") != selection.get("sources")
            or plan_input.get("sources") != context["sources"]
            or not any(record["run_id"] == context["run_id"]
                       and record["result_id"] == context["result_id"]
                       and record["capability"] == plan_input.get("capability")
                       and record["sources"] == context["sources"] for record in assessments)
            or not isinstance(context.get("agent_run_id"), str) or not context["agent_run_id"]
            or context["agent_run_id"] == context["run_id"]):
        raise ValueError("Context snapshot differs from selected Product or contribution")
    try:
        measured = datetime.fromisoformat(context["observed_at"])
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("Context snapshot has no observation time") from error
    if measured.utcoffset() is None or not 0 <= (now - measured).total_seconds() <= 900 or measured > observed_at:
        raise ValueError("Context snapshot is stale or after its receipt")
    schema_fields = {"schema_version", "admitted_schema", "consumer_revision"}
    native_schema = bool(schema_fields & context.keys())
    if native_schema and (not schema_fields <= context.keys()
                          or context["schema_version"] != "1.0"
                          or context["admitted_schema"] != context["schema_version"]
                          or not isinstance(context["consumer_revision"], str)
                          or not context["consumer_revision"]):
        raise ValueError("selected producer and consumer schemas differ")
    replay = context.get("replay")
    if replay is None:
        return 1, 0, native_schema
    if not isinstance(replay, dict):
        raise ValueError("selected Context replay is not a readback")
    path = Path(selection["journal_file"])
    if (not path.is_absolute() or path.parent.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3]) or path.parent.stat().st_mode & 0o077):
        raise ValueError("selected Context replay journal is not private")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise ValueError("selected Context replay journal is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(65537)
    finally:
        os.close(descriptor)
    if (not content or len(content) > 65536 or not content.endswith(b"\n")
            or not re.fullmatch(r"[0-9a-f]{64}", replay.get("sha256", ""))
            or hashlib.sha256(content).hexdigest() != replay["sha256"]):
        raise ValueError("selected Context replay differs from private attempts")
    verify_replay_receipt(content, replay, selection, context["agent_run_id"], now, measured)
    return 1, replay["attempts"], native_schema


def verified_scope_denials(readback, fixture, cleanup, now, observed_at):
    denial = readback.get("scope_denials")
    if denial is None:
        return False
    selected, foreign = fixture["cases"]
    selection = selected.get("recovery")
    if (not isinstance(denial, dict) or set(denial) != {"run_id", "scope", "probes", "observed_at"}
            or not isinstance(selection, dict) or selection.get("scope") != selected["scope"]
            or not isinstance(selected.get("selected"), dict)
            or selection.get("sources") != selected["selected"].get("sources")
            or selection.get("binding_id") != selected["selected"].get("binding_id")
            or not isinstance(selection.get("foreign_product_id"), str)
            or not selection["foreign_product_id"]
            or selection["foreign_product_id"] == selected["scope"]["product_id"]
            or not isinstance(selection.get("revoked_token_env"), str)
            or not selection["revoked_token_env"]
            or denial["run_id"] != cleanup["run_id"] or denial["scope"] != selected["scope"]
            or foreign["scope"]["tenant_id"] == selected["scope"]["tenant_id"]
            or not isinstance(denial["probes"], list) or len(denial["probes"]) != 3
            or {item.get("case") for item in denial["probes"] if isinstance(item, dict)} != {
                "foreign_product", "foreign_tenant", "revoked_access"}
            or any(not isinstance(item, dict) or set(item) != {"case", "status", "disclosed_resources"}
                   or type(item["status"]) is not int or item["status"] not in (403, 404)
                   or item["disclosed_resources"] != [] for item in denial["probes"])):
        raise ValueError("native denial readback differs from selected recovery and denied scopes")
    try:
        measured = datetime.fromisoformat(denial["observed_at"].replace("Z", "+00:00"))
        cleaned = datetime.fromisoformat(cleanup["observed_at"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("native denial readback has no observation time") from error
    if (measured.utcoffset() is None or not 0 <= (now - measured).total_seconds() <= 900
            or not cleaned <= measured <= observed_at):
        raise ValueError("native denial readback is stale or predates cleaned recovery")
    return True


def verified_recovery(readback, fixture, cleanup, packet, now, observed_at):
    recovery = readback.get("recovery")
    if recovery is None:
        return 0, False, False
    selected = fixture["cases"][0]
    selection = selected.get("recovery")
    fields = {"run_id", "scope", "native_job_ref", "publication_id", "attempts", "journal_sha256", "observed_at"}
    if (not isinstance(recovery, dict) or set(recovery) not in (fields, fields | {"transition_sha256"})
            or not isinstance(selection, dict) or selection.get("scope") != selected["scope"]
            or not isinstance(selected.get("selected"), dict)
            or selection.get("sources") != selected["selected"].get("sources")
            or selection.get("binding_id") != selected["selected"].get("binding_id")
            or recovery["run_id"] != cleanup["run_id"] or recovery["scope"] != selected["scope"]
            or not isinstance(recovery["native_job_ref"], dict) or not recovery["native_job_ref"]
            or not isinstance(recovery["publication_id"], str) or not recovery["publication_id"]
            or type(recovery["attempts"]) is not int
            or not 1 <= recovery["attempts"] <= selection["max_attempts"]
            or not isinstance(recovery["journal_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", recovery["journal_sha256"])):
        raise ValueError("recovery record differs from selected Product and cleanup")
    try:
        measured = datetime.fromisoformat(recovery["observed_at"])
        cleaned = datetime.fromisoformat(cleanup["observed_at"])
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("recovery record lacks an observation time") from error
    if (measured.utcoffset() is None or not 0 <= (now - measured).total_seconds() <= 900
            or not cleaned <= measured <= observed_at):
        raise ValueError("recovery observation is stale or predates cleaned work")
    path = Path(selection["journal_file"])
    if (not path.is_absolute() or path.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3]) or not path.parent.is_dir()
            or path.parent.stat().st_mode & 0o077):
        raise ValueError("recovery journal must remain private outside the checkout")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("recovery journal has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144 or hashlib.sha256(content).hexdigest() != recovery["journal_sha256"]:
        raise ValueError("recovery journal changed after independent readback")
    selected_cleanup = verified_selected_cleanup(selection, packet, content, cleanup)
    transition = recovery.get("transition_sha256")
    if transition is None:
        return recovery["attempts"], False, selected_cleanup
    if not isinstance(transition, str) or not re.fullmatch(r"[0-9a-f]{64}", transition):
        raise ValueError("fault-time recovery digest is invalid")
    snapshot_path = Path(str(path) + ".fault.json")
    descriptor = os.open(snapshot_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("fault-time recovery snapshot has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            snapshot_bytes = source.read(262145)
    finally:
        os.close(descriptor)
    if len(snapshot_bytes) > 262144 or hashlib.sha256(snapshot_bytes).hexdigest() != transition:
        raise ValueError("fault-time recovery snapshot changed after readback")
    snapshot = json.loads(snapshot_bytes)
    if (not isinstance(snapshot, dict) or set(snapshot) != {"receipt", "run", "publication", "observed_at"}
            or snapshot["receipt"] != selection.get("receipt")
            or not isinstance(snapshot["run"], dict)
            or snapshot["run"].get("run_id") != recovery["run_id"]
            or snapshot["run"].get("scope") != recovery["scope"]
            or snapshot["run"].get("input") != selection.get("input")
            or not isinstance(snapshot["publication"], dict)
            or snapshot["publication"].get("state") != "retry_wait"
            or snapshot["publication"].get("publication_id") != recovery["publication_id"]):
        raise ValueError("fault-time recovery snapshot belongs to another selected run")
    try:
        captured = datetime.fromisoformat(snapshot["observed_at"])
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("fault-time recovery lacks an observation time") from error
    entries = [json.loads(line) for line in content.splitlines()]
    if not entries or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("fault-time recovery journal is incomplete")
    restarts = [entry for entry in entries if entry.get("operation") == "worker_restart"
                and entry.get("status") == "acknowledged"]
    retained = [entry for entry in entries if entry.get("operation") not in {
        "fault_arm", "run_submit", "worker_restart", "cleanup"}]
    if (len(restarts) != 1 or not any(entry.get("operation") == "fault_arm"
                                     and entry.get("status") == "acknowledged" for entry in entries)
            or not any(entry.get("operation") == "cleanup" and entry.get("status") == "acknowledged"
                       and entry.get("work_id") == recovery["run_id"] for entry in entries)
            or len(retained) != recovery["attempts"]
            or len({entry.get("attempt_id") for entry in retained}) != len(retained)
            or any(entry.get("run_id") != recovery["run_id"] for entry in retained)
            or not isinstance(snapshot["run"].get("attempts"), list)
            or not snapshot["run"]["attempts"]
            or not {item.get("attempt_id") for item in snapshot["run"]["attempts"]
                    if isinstance(item, dict)} <= {entry.get("attempt_id") for entry in retained}):
        raise ValueError("fault-time recovery journal differs from verified worker attempts")
    try:
        restarted = datetime.fromisoformat(restarts[0]["observed_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("fault-time recovery journal lacks restart time") from error
    if (captured.utcoffset() is None or restarted.utcoffset() is None
            or not captured <= restarted <= cleaned <= measured):
        raise ValueError("fault-time recovery does not precede terminal readback")
    return recovery["attempts"], True, selected_cleanup


def validate_native(readback, context, environment, fixture, *, now=None):
    now = datetime.now(UTC) if now is None else now
    if not isinstance(readback, dict):
        raise ValueError("native readback is not an observation")
    selected = {
        "source_revision": context["source_revision"],
        "definition_revision": context["definition_revision"],
        "workflow_path": WORKFLOW_PATH,
        "run_id": str(context["run_id"]), "run_attempt": str(context["run_attempt"]),
        "wheel_digest": environment["WHEEL_DIGEST"], "archive_digest": environment["ARCHIVE_DIGEST"],
        "manifest_version": environment["MANIFEST_VERSION"],
        "fixture_digest": environment["L5_FIXTURE_DIGEST"],
    }
    if set(readback) - set(selected) - {
        "observed_at", "installed", "gateway", "setup", "context", "selector_receipts",
        "products", "triggers", "cleanup", "scope_denials", "recovery",
    }:
        raise ValueError("native readback includes unrecognized evidence")
    if any(readback.get(field) != value for field, value in selected.items()):
        raise ValueError("native readback belongs to another source, artifact, fixture or run")
    if any(not re.fullmatch(r"[0-9a-f]{64}", environment[field])
           for field in ("WHEEL_DIGEST", "ARCHIVE_DIGEST", "L5_FIXTURE_DIGEST")):
        raise ValueError("selected artifact digest is invalid")
    try:
        observed_at = datetime.fromisoformat(readback["observed_at"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("native readback has no observation time") from error
    if (observed_at.utcoffset() is None or not 0 <= (now - observed_at).total_seconds() <= 900):
        raise ValueError("native readback is stale or future-dated")
    from l5_services.selector_receipts import read_selector_receipts

    selectors = read_selector_receipts(environment["PRIVATE_EVIDENCE_DIR"], environment, observed_at)
    if readback.get("selector_receipts") != selectors:
        raise ValueError("native readback differs from the executed Security selectors")
    installed = readback.get("installed")
    if not isinstance(installed, dict):
        raise ValueError("native installed state is not a readback")
    digest = base64.b64encode(bytes.fromhex(environment["ARCHIVE_DIGEST"])).decode()
    if (installed.get("worker_sha256") != digest or installed.get("repair_sha256") != digest
            or installed.get("mapping_state") != "Enabled" or installed.get("repair_state") != "ENABLED"):
        raise ValueError("native readback does not describe the pinned installed workers")
    installed_scope_sha256 = verified_installed_scope(installed, environment)
    products = readback.get("products")
    if (not isinstance(products, list) or len(products) != 2
            or any(not isinstance(item, dict) or not isinstance(item.get("scope"), dict)
                   or not all(item["scope"].get(field) for field in ("tenant_id", "product_id", "project_id"))
                   or not isinstance(item.get("assessments"), list) or len(item["assessments"]) != 2
                   or any(not isinstance(record, dict) for record in item["assessments"])
                   or {record.get("capability") for record in item["assessments"]} != {"code_review", "threat_model"}
                   or any(not record.get("run_id") or not record.get("result_id")
                          or not record.get("native_job_ref") or not record.get("sources")
                          for record in item["assessments"]) for item in products)
            or len({item["scope"]["tenant_id"] for item in products}) != 2
            or len({item["scope"]["product_id"] for item in products}) != 2):
        raise ValueError("native readback has incomplete or shared Product results")
    verify_selected_products(products, fixture)
    triggered_runs = verified_triggers(readback, fixture, products, now, observed_at)
    cleanup = readback.get("cleanup")
    if (not isinstance(cleanup, dict) or not isinstance(cleanup.get("run_id"), str)
            or not cleanup["run_id"] or type(cleanup.get("owned_absent")) is not int
            or cleanup["owned_absent"] < 1 or type(cleanup.get("linked_retained")) is not int
            or cleanup["linked_retained"] < 0 or not isinstance(cleanup.get("readback"), list)
            or len(cleanup["readback"]) != 1 or not isinstance(cleanup["readback"][0], str)
            or not cleanup["readback"][0].startswith("private://security/native-cleanup/")):
        raise ValueError("native owned-cleanup readback is incomplete")
    try:
        cleanup_at = datetime.fromisoformat(cleanup["observed_at"])
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("native owned-cleanup readback has no observation time") from error
    if (cleanup_at.utcoffset() is None or not 0 <= (now - cleanup_at).total_seconds() <= 900
            or cleanup_at > observed_at):
        raise ValueError("native owned-cleanup readback is stale or after its receipt")
    packet = verify_cleanup_packet(cleanup, environment)
    context_observations, context_replays, context_schema = verified_context(
        readback, fixture, products, now, observed_at)
    recovery_attempts, recovery_transition, owned_cleanup = verified_recovery(
        readback, fixture, cleanup, packet, now, observed_at)
    gateway_revision = verified_gateway(readback, environment, now, observed_at)
    return {"products": len(products), "assessments": sum(len(item["assessments"]) for item in products),
            "triggered_runs": triggered_runs,
            "observed_at": observed_at.isoformat(), "owned_absent": cleanup["owned_absent"],
            "linked_retained": cleanup["linked_retained"],
            "setup_products": verified_setup(readback, fixture, now, observed_at),
            "context_observations": context_observations, "context_replays": context_replays,
            "scope_denials": verified_scope_denials(readback, fixture, cleanup, now, observed_at),
            "recovery_attempts": recovery_attempts, "recovery_transition": recovery_transition,
            "owned_cleanup": owned_cleanup,
            "installed_scope_sha256": installed_scope_sha256,
            "selector_receipts": selectors,
            "gateway_revision": gateway_revision,
            "schema_compatible": context_schema and gateway_revision is not None}


def verified_native_checks(readback, native, context, digest, environment):
    filename = f"security-readback-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}.json"
    receipt = f"private://security/native-readback/{filename}#sha256:{digest}"
    checks = {
        "real-threat-model": (receipt, readback["observed_at"]),
        "real-static-review": (receipt, readback["observed_at"]),
        "source-pinning": (receipt, readback["observed_at"]),
    }
    if readback.get("setup") is not None:
        checks["ui-cli-mcp-parity"] = (receipt, readback["observed_at"])
    if native["gateway_revision"] is not None:
        checks["runtime-revisions"] = (receipt, readback["observed_at"])
        checks["same-wheel"] = (receipt, readback["observed_at"])
    if native["context_replays"] >= 2:
        checks["context-reuse"] = (receipt, readback["observed_at"])
    if native["recovery_transition"]:
        checks["durable-recovery"] = (receipt, readback["observed_at"])
    if native["schema_compatible"]:
        checks["schema-compatible"] = (receipt, readback["observed_at"])
    if native["owned_cleanup"]:
        checks["owned-cleanup"] = (receipt, readback["cleanup"]["observed_at"])
    return [{"id": identifier, "outcome": "passed", "evidence_verified": True,
             "requirement_revision": context["requirement_revision"],
             "source_revision": context["source_revision"],
             "definition_revision": context["definition_revision"],
             "workflow_path": WORKFLOW_PATH,
             "runtime_revision": context["source_revision"],
             "run_id": str(context["run_id"]), "run_attempt": str(context["run_attempt"]),
             "verification_layer": "independent-live", "observation": "independent-live",
             "evidence_ref": reference, "observed_at": observed_at}
            for identifier, (reference, observed_at) in checks.items()]


def verified_native_coverage(readback, native, context, digest, environment):
    filename = f"security-readback-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}.json"
    reference = f"private://security/native-readback/{filename}#sha256:{digest}"
    observed = {
        "SA16/AC-01": native["setup_products"] == 2 and native["gateway_revision"] is not None,
        "SA16/AC-02": native["triggered_runs"] == 2 and native["assessments"] == 4,
        "SA16/AC-04": native["context_replays"] >= 2 and native["schema_compatible"],
    }
    record = {"outcome": "passed", "evidence_verified": True,
              "requirement_revision": context["requirement_revision"],
              "source_revision": context["source_revision"],
              "definition_revision": context["definition_revision"],
              "workflow_path": WORKFLOW_PATH,
              "runtime_revision": context["source_revision"],
              "run_id": str(context["run_id"]), "run_attempt": str(context["run_attempt"]),
              "verification_layer": "independent-live", "evidence_ref": reference,
              "observed_at": readback["observed_at"]}
    return {"story_criteria": [{"id": identifier, **record} for identifier, verified in observed.items() if verified],
            "scenarios": [{"id": identifier.replace("/AC-", "/SCN-"), **record}
                          for identifier, verified in observed.items() if verified]}


def emit(environment):
    identity = release_identity(environment)
    context = identity.copy()
    if context["service"] != "security":
        raise ValueError("Security evidence cannot qualify another service")
    rollup = rollup_module()
    context["requirement_revision"] = rollup.proposal_revision()
    fixture = selected_fixture(environment, context)
    raw, digest = private_readback(environment)
    native = validate_native(raw, context, environment, fixture)
    target = None
    if context["contract_digest"] is not None and native["gateway_revision"] is not None:
        selected = authorize_engine(environment, identity, target=True)
        if (selected is None or selected["account_id"] != environment["L5_MUTATION_ROLE_ARN"].split(":")[4]
                or selected["region"] != environment["AWS_REGION"]):
            raise ValueError("Engine target differs from independently read deployed account and region")
        target = {field: selected[field] for field in ("connection_id", "resource_kind", "resource_id")}
    bundle = {"schema_version": 1, "run_id": str(context["run_id"]),
              "run_attempt": str(context["run_attempt"]), "source_revision": context["source_revision"],
              "definition_revision": context["definition_revision"], "workflow_path": WORKFLOW_PATH,
              "gateway_revision": native["gateway_revision"],
              "reader": {"identity": "security-native-product-provider-worker", "revision": context["source_revision"]},
              **({"target": target} if target is not None else {}),
              "live_checks": verified_native_checks(raw, native, context, digest, environment),
              **({"cleanup": {"outcome": "passed", "readback": raw["cleanup"]["readback"],
                              "observed_at": raw["cleanup"]["observed_at"]}}
                 if native["owned_cleanup"] else {}),
              **verified_native_coverage(raw, native, context, digest, environment)}
    report = rollup.roll_up(bundle, {"source_revision": context["source_revision"],
        "requirement_revision": context["requirement_revision"],
        "definition_revision": context["definition_revision"], "run_id": str(context["run_id"]),
        "run_attempt": str(context["run_attempt"]), "correlation_id": context["correlation"],
        "contract_digest": context["contract_digest"]}, rollup.expected_records(), datetime.now(UTC), 900)
    report["native_observation"] = {"sha256": digest, **native,
        "source_revision": context["source_revision"],
        "definition_revision": context["definition_revision"],
        "workflow_path": WORKFLOW_PATH,
        "run_id": str(context["run_id"]),
        "run_attempt": str(context["run_attempt"]),
        "wheel_sha256": environment["WHEEL_DIGEST"], "archive_sha256": environment["ARCHIVE_DIGEST"],
        "fixture_sha256": environment["L5_FIXTURE_DIGEST"]}
    if any(item["outcome"] != "passed" for section in ("epic_criteria", "story_criteria", "scenarios")
           for item in report[section]):
        report["unresolved"] = sorted(set(report["unresolved"]) | {
            "native-readbacks:required-scenarios-not-evaluated"})
    if native["context_observations"] and not native["context_replays"]:
        report["unresolved"] = sorted(set(report["unresolved"]) | {
            "native-readbacks:context-replay-not-independently-evaluated"})
    root = Path(environment["RUNNER_TEMP"]).resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("qualification outputs cannot enter the checkout")
    qualification = root / "l5-security-qualification/qualification.json"
    delivery = root / "delivery-evidence/delivery-evidence.json"
    if qualification.exists() or delivery.exists() or qualification.is_symlink() or delivery.is_symlink():
        raise ValueError("existing qualification outputs need reconciliation")
    previous_umask = os.umask(0o077)
    try:
        for parent in (qualification.parent, delivery.parent):
            if parent.is_symlink():
                raise ValueError("qualification directory cannot be a link")
            parent.mkdir(mode=0o700, exist_ok=True)
            if parent.stat().st_mode & 0o077:
                raise ValueError("qualification directory must be private")
        rollup.write_private(qualification, report)
        delivery_report = rollup.delivery_evidence(report)
        delivery_report["qualification_sha256"] = hashlib.sha256(qualification.read_bytes()).hexdigest()
        delivery_report["native_observation"] = {
            field: report["native_observation"][field] for field in (
                "sha256", "source_revision", "definition_revision", "workflow_path",
                "run_id", "run_attempt", "wheel_sha256", "archive_sha256", "fixture_sha256",
                "installed_scope_sha256", "gateway_revision", "selector_receipts")}
        rollup.write_private(delivery, delivery_report)
    finally:
        os.umask(previous_umask)
    return report


def preflight(environment):
    context = release_identity(environment)
    if context["service"] != "security":
        raise ValueError("Security evidence cannot qualify another service")
    selected_fixture(environment, context)
    directory = Path(environment["PRIVATE_EVIDENCE_DIR"])
    if (not directory.is_absolute() or directory.is_symlink()
            or directory.resolve(strict=True).is_relative_to(Path(__file__).resolve().parents[3])
            or not directory.is_dir() or directory.stat().st_mode & 0o077):
        raise ValueError("Security readback directory must be private and outside the checkout")


if __name__ == "__main__":
    if os.environ.get("L5_QUALIFICATION_PREFLIGHT") == "1":
        try:
            preflight(os.environ)
        except (KeyError, TypeError, ValueError, OSError):
            raise SystemExit("Security qualification preflight unavailable") from None
        raise SystemExit(0)
    try:
        result = emit(os.environ)
    except (KeyError, TypeError, ValueError, OSError):
        raise SystemExit("Security qualification input unavailable; no result was accepted") from None
    if result["unresolved"]:
        raise SystemExit("Security qualification incomplete; inspect private report before another attempt")
    print("Security qualification independently verified")
