#!/usr/bin/env python3
"""Independently read installed Security work after the authorized live selectors."""

import base64
import boto3
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/live"))
from cleanup import verify_owned_cleanup, verify_scope_denials
from continuation import verify_recovery
from clients import api_reader, product_sources, read_assessment, verify_setup
from evidence import FIELDS, AttemptJournal
from fixtures import load_live_fixture
from l5_services.publication.contributions import contribution_pages
from l5_services.selector_receipts import read_selector_receipts
from providers import verify_assessment
from recovery_live import LiveRecoveryDriver, read_scope_probes, recovery_progress


def gateway_deployment(environment):
    if not environment.get("GH_TOKEN"):
        raise ValueError("authenticated Gateway deployment receipt unavailable")
    receipt_environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    receipt_environment.update({"GH_TOKEN": environment["GH_TOKEN"],
                           "GITHUB_REPOSITORY": environment["GITHUB_REPOSITORY"],
                           "GITHUB_RUN_ID": environment["GITHUB_RUN_ID"],
                           "ADP_RECEIPT_ROLE": environment["L5_MUTATION_ROLE_ARN"],
                           "ADP_RECEIPT_TARGET": "adp-gateway-deploy-" + environment["L5_ENVIRONMENT"],
                           "ADP_RECEIPT_ENVIRONMENT": environment["L5_ENVIRONMENT"],
                           "ADP_RECEIPT_REGION": environment["AWS_REGION"],
                           "ADP_RECEIPT_REPOSITORY_ID": environment["GITHUB_REPOSITORY_ID"]})
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "gateway" /
                           "scripts/gateway-deployment-receipt.py"), "readback"], env=receipt_environment,
                            capture_output=True, check=True, timeout=120)
    return json.loads(result.stdout)


def gateway_status(environment, *, session=None, kubernetes=None, deployment=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from release_context import verify_principal

    if kubernetes is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway"))
        from src.orchestration.deployment_runtime_reader import KubernetesRuntime

        kubernetes = KubernetesRuntime
    region = environment["AWS_REGION"]
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", region):
        raise ValueError("Gateway region is not selected")
    session = session or boto3.Session(region_name=region)
    if session.region_name != region:
        raise ValueError("Gateway reader region differs from selected region")
    verify_principal(environment, session.client("sts").get_caller_identity())
    role = environment["L5_MUTATION_ROLE_ARN"]
    account = role.split(":")[4]
    source = environment["SOURCE_SHA"]
    target = environment["L5_ENVIRONMENT"]
    if not re.fullmatch(r"[0-9a-f]{40}", source) or target not in {"dev", "test", "prod"}:
        raise ValueError("Gateway source or environment is not selected")
    wheel_digest = environment["WHEEL_DIGEST"]
    if not re.fullmatch(r"[0-9a-f]{64}", wheel_digest):
        raise ValueError("Gateway wheel digest is not selected")
    deployment = deployment if deployment is not None else gateway_deployment(environment)
    if (not isinstance(deployment, dict) or deployment.get("source_revision") != source
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", deployment.get("image_digest", ""))):
        raise ValueError("Gateway deployment receipt does not match selected source")
    cluster = f"adp-{target}-eks-cluster"
    description = session.client("eks").describe_cluster(name=cluster)["cluster"]
    if (description.get("arn") != f"arn:aws:eks:{region}:{account}:cluster/{cluster}"
            or description.get("status") != "ACTIVE"):
        raise ValueError("Gateway cluster differs from the selected account or environment")
    images = session.client("ecr").describe_images(
        repositoryName="adp-gateway", imageIds=[{"imageTag": source}])["imageDetails"]
    if (len(images) != 1 or source not in images[0].get("imageTags", [])
            or images[0].get("imageDigest") != deployment["image_digest"]):
        raise ValueError("Gateway source image digest is unavailable")
    digest = images[0]["imageDigest"]
    runtime = kubernetes(session, description, "adp-gateway")
    try:
        pods, migration_head = runtime.gateway(
            digest=digest, source_revision=source, account=account, region=region,
            wheel_sha256=wheel_digest)
    finally:
        runtime.close()
    if not pods or len(set(pods)) != len(pods) or not migration_head:
        raise ValueError("Gateway serving revision has no complete pod readback")
    return {"revision": source, "image_digest": digest, "wheel_sha256": wheel_digest,
            "account_id": account,
            "region": region, "cluster": cluster, "namespace": "adp-gateway",
            "pod_uids": pods, "migration_head": migration_head,
            "observed_at": datetime.now(UTC).isoformat()}


def verify_setup_readback(case, max_attempts, driver):
    selected = dict(case["selected"])
    if (selected.get("scope") != case["scope"] or not selected.get("sources")
            or case["run_input"].get("sources") != selected["sources"]
            or case["run_input"].get("binding_id") != selected.get("binding_id")):
        raise ValueError("selected setup source or binding changed")
    operations = (("cli_update", case["cli_receipt"]), ("ui_edit", case["ui_receipt"]),
                  ("mcp_submit", case["mcp_receipt"]))
    entries = AttemptJournal(case["journal_file"]).read()
    if len(entries) != 2 * len(operations):
        raise ValueError("setup attempts are missing or duplicated")
    effects = {}
    for operation, receipt in operations:
        prepared = [entry for entry in entries if entry["operation"] == operation
                    and entry["status"] == "prepared"]
        acknowledged = [entry for entry in entries if entry["operation"] == operation
                        and entry["status"] == "acknowledged"]
        if (len(prepared) != 1 or len(acknowledged) != 1
                or prepared[0]["attempt_id"] != receipt or acknowledged[0]["attempt_id"] != receipt
                or not acknowledged[0]["effect_id"] or acknowledged[0]["idempotency_key"] != receipt):
            raise ValueError("setup effect lacks a selected, acknowledged attempt")
        effects[operation] = acknowledged[0]["effect_id"]
    selected.update(run_id=effects["mcp_submit"], effects=effects, max_attempts=max_attempts)
    observations = {"cli": driver.cli_observations(selected["binding_id"], selected["run_id"]),
                    "ui": driver.ui_observations(selected["binding_id"], selected["run_id"]),
                    "mcp": driver.mcp_observations(selected["binding_id"], selected["run_id"])}
    result = verify_setup(selected, observations, driver.attempts(selected), driver.read_api)
    configuration = observations["ui"]["configuration"]
    if configuration.get("settings", {}).get("region") != case["ui_region"]:
        raise ValueError("served UI did not retain the selected configuration edit")
    return {"scope": case["scope"], "sources": selected["sources"], "run_id": result["run_id"],
            "configuration_revision": result["configuration_revision"],
            "binding_revision": result["binding_revision"], "run_revision": result["run_revision"],
            "observed_at": datetime.now(UTC).isoformat()}


def read_setup(case, limits, credentials):
    from security_manual_setup import ManualSetupDriver

    # This is an independent pass through the same current protected import.
    # It performs no save, initial submission, browser import or session injection.
    previous = os.environ.copy()
    try:
        os.environ.update(credentials)
        filename = os.environ["PRIVATE_FIXTURE"]
        fixture = load_live_fixture(filename)
        driver = ManualSetupDriver(case, fixture, filename, readback=True)
        try:
            return verify_setup_readback(case, limits["max_attempts"], driver)
        finally:
            driver.close()
    finally:
        os.environ.clear()
        os.environ.update(previous)


def verify_replay_journal(case, publication, agent_run_id, *, now=None):
    selection = case["context_reuse"]
    path = Path(selection["journal_file"])
    if (not path.is_absolute() or path.parent.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3]) or path.parent.stat().st_mode & 0o077):
        raise ValueError("Context attempts need a private selected journal")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise ValueError("Context attempt journal is unsafe or oversized")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(65537)
    finally:
        os.close(descriptor)
    if not content or len(content) > 65536 or not content.endswith(b"\n"):
        raise ValueError("Context attempt journal is incomplete")
    entries = [json.loads(line) for line in content.splitlines()]
    pages = contribution_pages(SimpleNamespace(payload=publication["payload"],
                                               contribution_hash=publication["contribution_hash"]))
    if (not pages or 2 * len(pages) + 1 > selection["max_attempts"]
            or len(entries) != 4 * len(pages) + 2
            or any(not isinstance(entry, dict) or set(entry) != set(FIELDS) for entry in entries)):
        raise ValueError("Context replay attempts do not match the selected contribution")
    key = (f"{case['scope']['tenant_id']}/{publication['result_id']}/"
           f"{publication['result_version']}/{publication['destination']}/{publication['schema_version']}")
    expected = []
    for repeat in range(2):
        for index in range(len(pages)):
            attempt_id = f"replay-{repeat}-{index}"
            page_key = key + (f"/pages/{index}" if len(pages) > 1 else "")
            expected.extend(("context_replay", status, attempt_id, publication["publication_id"], page_key)
                            for status in ("prepared", "acknowledged"))
    expected.extend((("later_agent_query", "prepared", "later-query", publication["publication_id"],
                      selection["query_receipt"]),
                     ("later_agent_query", "acknowledged", "later-query", agent_run_id,
                      selection["query_receipt"])))
    now = datetime.now(UTC) if now is None else now
    previous = None
    for entry, expected_entry in zip(entries, expected, strict=True):
        try:
            observed = datetime.fromisoformat(entry["observed_at"])
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("Context attempt lacks an observation time") from error
        if (tuple(entry[field] for field in ("operation", "status", "attempt_id", "work_id",
                                                 "idempotency_key")) != expected_entry
                or entry["run_id"] != selection["run_id"]
                or entry["native_job_id"] is not None or entry["effect_id"] is not None
                or observed.utcoffset() is None or not 0 <= (now - observed).total_seconds() <= 900
                or previous is not None and observed < previous):
            raise ValueError("Context replay journal is stale, foreign or incomplete")
        previous = observed
    return {"sha256": hashlib.sha256(content).hexdigest(), "attempts": 2 * len(pages),
            "page_keys": [key + (f"/pages/{index}" if len(pages) > 1 else "")
                          for index in range(len(pages))],
            "observed_at": previous.isoformat()}


def verify_context_snapshot(case, foreign, driver):
    selected = case["context_reuse"]
    scope = case["scope"]
    sources = selected["sources"]
    if (selected["scope"] != scope or foreign["scope"]["tenant_id"] == scope["tenant_id"]
            or not sources or selected["assessment_plan"]["input"]["sources"] != sources
            or any(source.get("tenant_id") != scope["tenant_id"]
                   or source.get("project_id") != scope["project_id"]
                   or not source.get("record_revision")
                   or source["record_revision"].lower() in {"head", "latest", "null"} for source in sources)):
        raise ValueError("Context selection has foreign or unpinned sources")
    run, result, findings = driver.read_product(selected)
    if (run["state"] != "succeeded" or run["scope"] != scope or result["scope"] != scope
            or run["run_id"] != selected["run_id"] or result["result_id"] != selected["result_id"]
            or result["run_id"] != run["run_id"] or result["finding_count"] != len(findings)
            or run["input"]["sources"] != sources):
        raise ValueError("Context Product result differs from selected Security run")
    driver.verify_result(run, result, findings, selected["assessment_plan"])
    publication = driver.read_publication(selected)
    receipt = driver.read_receipt(selected)
    payload = publication["payload"]
    result_ref = payload["result_ref"]
    if (publication["state"] != "published" or publication["publication_id"] != selected["publication_id"]
            or publication["schema_version"] != "1.0"
            or payload.get("schema_version") != publication["schema_version"]
            or publication["result_id"] != result["result_id"] or publication["result_version"] != result["revision"]
            or publication["destination"] != "product-context" or payload["scope"] != scope
            or payload["run_id"] != run["run_id"] or payload["source_refs"] != sources
            or payload["native_job_ref"] != result["native_job_ref"]
            or result_ref["record_id"] != result["result_id"]
            or result_ref["record_revision"] != str(result["revision"])
            or receipt != publication["receipt"] or receipt["contribution_id"] != publication["publication_id"]
            or receipt["admitted_schema"] != publication["schema_version"]
            or result_ref not in receipt["record_refs"] or not receipt["consumer_revision"]):
        raise ValueError("Context contribution is not the selected native Security result")
    agent_response = driver.request("GET", selected["agent_read_url"], selected["agent_read_token_env"])
    agent_response.raise_for_status()
    agent = agent_response.json()
    count_response = driver.request("GET", selected["context_count_url"], selected["context_token_env"])
    count_response.raise_for_status()
    count = count_response.json()
    visibility = driver.visibility(selected)
    denials = driver.denied_reads(selected, foreign)
    audit = driver.event_audit(selected)
    try:
        consumer_at = datetime.fromisoformat(agent["observed_at"])
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("later Context consumer has no observation time") from error
    if consumer_at.utcoffset() is None or not 0 <= (datetime.now(UTC) - consumer_at).total_seconds() <= 900:
        raise ValueError("later Context consumer readback is stale or future-dated")
    if (count["scope"] != scope or count["contribution_id"] != publication["publication_id"]
            or count["count"] != agent["consumer_count"] or count["count"] != 1
            or not agent["agent_run_id"] or agent["agent_run_id"] == run["run_id"]
            or agent["contribution_id"] != publication["publication_id"]
            or agent["queried_product_id"] != scope["product_id"] or agent["scope"] != scope
            or agent["source"] != "context" or not agent["observed_at"]
            or visibility["scope"] != scope or agent["current_visibility"] != visibility["visibility"]
            or agent["visibility_revision"] != visibility["revision"]
            or not any(citation.get("result_ref") == result_ref
                       and citation.get("manifest_ref") == payload["manifest_ref"]
                       and citation.get("source_ref") in sources
                       and citation.get("source_revision") == citation["source_ref"]["record_revision"]
                       and citation.get("validation") == payload["validation"]
                       for citation in agent["citations"])
            or len(denials) != 2 or {item["case"] for item in denials} != {"foreign_product", "revoked_access"}
            or any(item["status"] not in {403, 404} or item["disclosed_resources"] for item in denials)
            or audit["complete"] is not True
            or any(event.get("causal_origin") == run["run_id"] for event in audit["items"])):
        raise ValueError("later Context read lacks native citation, scope or visibility evidence")
    replay = verify_replay_journal(case, publication, agent["agent_run_id"]) if selected.get("journal_file") else None
    return {"scope": scope, "run_id": run["run_id"], "result_id": result["result_id"],
            "publication_id": publication["publication_id"], "agent_run_id": agent["agent_run_id"],
            "sources": sources, "replay": replay, "schema_version": publication["schema_version"],
            "admitted_schema": receipt["admitted_schema"], "consumer_revision": receipt["consumer_revision"],
            "observed_at": datetime.now(UTC).isoformat()}


def read_context(case, foreign, limits, credentials):
    from assessment_live import LiveAssessmentDriver
    from clients_setup import LiveSetupDriver
    from context_live import LiveContextDriver

    selected = case["context_reuse"]
    required = {case["api_token_env"]}
    required.update(selected[field] for field in (
        "publication_token_env", "context_token_env", "agent_read_token_env", "visibility_token_env",
        "audit_token_env", "foreign_product_token_env", "revoked_token_env"))
    previous = os.environ.copy()
    try:
        os.environ.update({name: credentials[name] for name in required})
        driver = object.__new__(LiveContextDriver)
        driver.case = {**case, "deadline_seconds": limits["deadline_seconds"]}
        driver.deadline = time.monotonic() + limits["deadline_seconds"]
        driver.api = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                  headers={"Authorization": "Bearer " + credentials[case["api_token_env"]]})
        driver.read_api = api_reader(driver.api)
        driver.assessment = object.__new__(LiveAssessmentDriver)
        driver.assessment.case, driver.assessment.deadline = driver.case, driver.deadline
        driver.assessment.api, driver.assessment.read_api = driver.api, driver.read_api
        driver.assessment.setup = object.__new__(LiveSetupDriver)
        driver.assessment.setup.case = driver.case
        driver.assessment.setup.deadline = driver.deadline
        try:
            return verify_context_snapshot(case, foreign, driver)
        finally:
            driver.close()
    finally:
        os.environ.clear()
        os.environ.update(previous)


def read_review(plan, run, result, native):
    path = Path(plan["review_file"])
    if (not path.is_absolute() or path.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3])):
        raise ValueError("review rubric must remain outside the checkout")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("review rubric has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144:
        raise ValueError("review rubric exceeds its byte limit")
    rubric = json.loads(content)
    if (not isinstance(rubric, dict) or rubric.get("run_id") != run["run_id"]
            or rubric.get("result_id") != result["result_id"]
            or rubric.get("native_job_ref") != native["native_job_ref"]
            or not rubric.get("rubric_revision")
            or not isinstance(rubric.get("review_sample"), list)
            or not isinstance(rubric.get("known_controls"), list)
            or not isinstance(rubric.get("reviews"), dict)):
        raise ValueError("independent rubric differs from the selected native result")
    native["review_rubric_revision"] = rubric["rubric_revision"]
    native["review_sample"] = rubric["review_sample"]
    native["known_controls"] = rubric["known_controls"]
    return rubric["reviews"], hashlib.sha256(content).hexdigest()


def assess_case(case, read_api, read_native, read_sources):
    scope = case["scope"]
    base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/security")
    attempts = AttemptJournal(case["assessment_journal_file"]).read()
    expected = {plan["receipt"]: plan for plan in case["assessments"]}
    if len(expected) != 2 or {plan["capability"] for plan in expected.values()} != {"code_review", "threat_model"}:
        raise ValueError("assessment selection changed")
    submitted = [entry for entry in attempts if entry["operation"] == "assessment_submit"]
    acknowledged = [entry for entry in submitted if entry["status"] == "acknowledged"]
    prepared = [entry for entry in submitted if entry["status"] == "prepared"]
    if (len(submitted) != 4 or len(acknowledged) != 2 or len(prepared) != 2
            or {entry["attempt_id"] for entry in acknowledged} != set(expected)
            or {entry["attempt_id"] for entry in prepared} != set(expected)
            or any(not entry["effect_id"] or entry["run_id"] not in (None, entry["effect_id"])
                   for entry in acknowledged)):
        raise ValueError("selected assessment attempts lack distinct durable runs")
    observed = []
    for entry in acknowledged:
        plan = expected[entry["attempt_id"]]
        run_id = entry["effect_id"]
        run = read_api(base + "/runs/" + quote(run_id, safe=""))
        sources = run.get("input", {}).get("sources") or []
        snapshots = run.get("admission", {}).get("source_snapshots") or []
        if (not sources or len(sources) != len(snapshots)
                or any(snapshot.get("source_ref") != source
                       for snapshot, source in zip(snapshots, sources, strict=True))
                or run.get("run_id") != run_id or run.get("scope") != scope
                or sources != plan["input"]["sources"]
                or run["input"].get("capability") != plan["capability"]
                or run["input"].get("binding_id") != plan["binding_id"]
                or run.get("state") != "succeeded" or run.get("provider_state") != "completed"):
            raise ValueError("Product run differs from selected assessment")
        binding = read_api(base + "/bindings/" + quote(plan["binding_id"], safe=""))
        provider = case["provider"]
        if (binding.get("scope") != scope or binding.get("binding_id") != plan["binding_id"]
                or binding.get("state") != "ready" or binding.get("gaps")
                or any(binding.get("provider_identity", {}).get(field) != provider[field]
                       for field in ("account_id", "role_arn", "region"))
                or (run.get("admission", {}).get("provider_identity") is not None
                    and run["admission"]["provider_identity"] != binding["provider_identity"])
                or binding.get("native_resource_ref", {}).get("record_id") != (
                    run["admission"]["native_space_ref"]["record_id"] if plan["capability"] == "code_review"
                    else run["native_job_ref"]["agent_space_id"])):
            raise ValueError("Product binding differs from selected provider or source scope")
        matches, cursor, visited = [], None, set()
        while True:
            page = read_api(base + "/results?limit=100" +
                            ("&cursor=" + quote(cursor, safe="") if cursor else ""))
            matches.extend(item for item in page["items"] if item.get("run_id") == run_id)
            cursor = page.get("next_cursor")
            if not cursor:
                break
            if cursor in visited or len(visited) >= 100:
                raise ValueError("Product result pagination is incomplete")
            visited.add(cursor)
        if len(matches) != 1:
            raise ValueError("selected assessment has missing or duplicate results")
        product_run, result, findings = read_assessment(scope, run_id, matches[0]["result_id"], read_api)
        native = read_native(run, provider)
        native_findings = {item["native_id"]: item["claim"] for item in native["findings"]}
        if (product_run != run or result.get("scope") != scope
                or result.get("native_job_ref") != run.get("native_job_ref")
                or native.get("native_job_ref") != run.get("native_job_ref")
                or native.get("status") != "COMPLETED"
                or native.get("findings_coverage") != "complete"
                or result.get("coverage") != "complete"
                or result.get("finding_count") != len(findings)
                or len(native_findings) != len(native["findings"])
                or len(findings) != len(native_findings)
                or any(finding.get("native_id") not in native_findings
                       or finding.get("claim") != native_findings[finding["native_id"]]
                       or finding.get("native_job_ref") != run["native_job_ref"] for finding in findings)):
            raise ValueError("Product result differs from native provider result")
        fetched = read_sources(run)
        reviews, rubric_digest = read_review(plan, run, result, native)
        verified = verify_assessment(run, result, findings, native, fetched, reviews)
        observed.append({"run_id": run_id, "result_id": result["result_id"],
                         "capability": plan["capability"], "binding_id": plan["binding_id"],
                         "provider_identity": binding["provider_identity"],
                         "native_job_ref": run["native_job_ref"],
                         "sources": run["input"]["sources"], "finding_count": len(findings),
                         "native_finding_ids": [item["native_id"] for item in native["findings"]],
                         "source_sha256": [hashlib.sha256(item["content"]).hexdigest() for item in fetched],
                         "snapshot_sha256": [snapshot["content"]["sha256"] for snapshot in snapshots],
                         "rubric_sha256": rubric_digest, "reviewed": verified["reviewed"]})
    return observed


def read_trigger(case, credentials, read_api, read_native, read_sources, *,
                 client_factory=httpx.Client, inventory=None):
    event = case["trigger_event"]
    scope = case["scope"]
    plans = [plan for plan in case["assessments"] if plan["capability"] == event["capability"]]
    if (len(plans) != 1 or event["input"]["binding_id"] != plans[0]["binding_id"]
            or event["input"]["sources"] == plans[0]["input"]["sources"]
            or not event["input"]["sources"] or not event["event_id"]):
        raise ValueError("selected event is not a new scoped assessment")
    journal = Path(case["assessment_journal_file"])
    if (not journal.is_absolute() or journal.parent.resolve(strict=True).is_relative_to(
            Path(__file__).resolve().parents[3]) or journal.parent.stat().st_mode & 0o077):
        raise ValueError("selected event journal is not private")
    descriptor = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise ValueError("selected event journal is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(65537)
    finally:
        os.close(descriptor)
    if not content or len(content) > 65536 or not content.endswith(b"\n"):
        raise ValueError("selected event journal is incomplete")
    entries = [json.loads(line) for line in content.splitlines()]
    if (len(entries) != 8 or any(not isinstance(entry, dict) or set(entry) != set(FIELDS)
                                 for entry in entries)):
        raise ValueError("selected event journal has missing or foreign attempts")
    deliveries = [entry for entry in entries if entry["operation"] == "event_delivery"]
    if (len(deliveries) != 4 or len(entries) - len(deliveries) != 4
            or any(entry["operation"] != "assessment_submit" for entry in entries[:4])
            or entries[4:] != deliveries):
        raise ValueError("selected event deliveries did not follow both assessments")
    now = datetime.now(UTC)
    previous = None
    for index, entry in enumerate(deliveries):
        try:
            observed = datetime.fromtimestamp(float(entry["observed_at"]), UTC)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("selected event attempt has no usable time") from error
        if (entry["attempt_id"] != f"{event['event_id']}-{index // 2}"
                or entry["status"] != ("prepared" if index % 2 == 0 else "acknowledged")
                or entry["work_id"] != event["event_id"]
                or (index % 2 == 0 and entry["effect_id"] is not None)
                or observed > now or (now - observed).total_seconds() > 900
                or previous is not None and observed < previous):
            raise ValueError("selected event delivery attempts do not agree")
        previous = observed
    if (deliveries[1]["effect_id"] is None
            or deliveries[1]["effect_id"] != deliveries[3]["effect_id"]):
        raise ValueError("duplicate event delivery lacks one durable run")
    baseline_path = Path(str(journal) + ".event-before.json")
    descriptor = os.open(baseline_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise ValueError("event baseline has unsafe permissions or size")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            baseline_bytes = source.read(65537)
    finally:
        os.close(descriptor)
    if not baseline_bytes or len(baseline_bytes) > 65536 or not baseline_bytes.endswith(b"\n"):
        raise ValueError("event baseline is missing or incomplete")
    baseline = json.loads(baseline_bytes)
    selected_runs = {entry.get("effect_id") for entry in entries[:4]
                     if entry["status"] == "acknowledged"}
    try:
        captured = datetime.fromisoformat(baseline["observed_at"])
        first_delivery = datetime.fromtimestamp(float(deliveries[0]["observed_at"]), UTC)
    except (KeyError, ValueError, TypeError, OverflowError) as error:
        raise ValueError("event baseline has no time before delivery") from error
    if (not isinstance(baseline, dict) or baseline.get("event_id") != event["event_id"]
            or baseline.get("scope") != scope
            or baseline.get("run_id") not in selected_runs or not isinstance(baseline.get("jobs"), list)
            or any(not isinstance(job, dict) for job in baseline["jobs"])
            or not baseline.get("space") or captured.utcoffset() is None
            or not 0 <= (now - captured).total_seconds() <= 900 or captured > first_delivery):
        raise ValueError("event baseline differs from selected pre-delivery inventory")
    previous_submit = None
    for index, entry in enumerate(entries[:4]):
        plan = case["assessments"][index // 2]
        expected_run = entry["effect_id"] if index % 2 else None
        try:
            submitted_at = datetime.fromisoformat(entry["observed_at"])
        except (TypeError, ValueError) as error:
            raise ValueError("selected assessment submit has no usable time") from error
        if (entry["attempt_id"] != plan["receipt"] or entry["work_id"] != plan["receipt"]
                or entry["status"] != ("acknowledged" if index % 2 else "prepared")
                or entry["run_id"] is not None or entry["native_job_id"] is not None
                or entry["effect_id"] != expected_run
                or entry["idempotency_key"] != (plan["receipt"] if index % 2 else None)
                or submitted_at.utcoffset() is None or submitted_at > captured
                or previous_submit is not None and submitted_at < previous_submit
                or (index % 2 and (not isinstance(expected_run, str) or not expected_run))):
            raise ValueError("selected assessment submit differs from pre-event Product work")
        previous_submit = submitted_at
    url = urlsplit(event["receipt_url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or quote(event["event_id"], safe="") not in url.path.split("/")):
        raise ValueError("selected event receipt endpoint differs from the event")
    with client_factory(timeout=20, follow_redirects=False) as client:
        response = client.get(event["receipt_url"], headers={
            "Authorization": "Bearer " + credentials[event["token_env"]], "Cache-Control": "no-cache"})
        response.raise_for_status()
        receipt = response.json()
    run_id = deliveries[1]["effect_id"]
    if (receipt.get("event_id") != event["event_id"] or receipt.get("run_id") != run_id
            or receipt.get("state") != "admitted"):
        raise ValueError("independent event receipt differs from both deliveries")
    base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/security")
    run = read_api(base + "/runs/" + quote(run_id, safe=""))
    selected_run = read_api(base + "/runs/" + quote(baseline["run_id"], safe=""))
    for index, plan in enumerate(case["assessments"]):
        selected_id = entries[index * 2 + 1]["effect_id"]
        selected = (selected_run if selected_id == baseline["run_id"] else
                    read_api(base + "/runs/" + quote(selected_id, safe="")))
        if (selected.get("run_id") != selected_id or selected.get("scope") != scope
                or selected.get("input", {}).get("capability") != plan["capability"]
                or selected.get("input", {}).get("binding_id") != plan["binding_id"]
                or selected.get("input", {}).get("sources") != plan["input"]["sources"]
                or selected.get("state") != "succeeded" or selected.get("provider_state") != "completed"):
            raise ValueError("selected assessment submit has no matching Product work")
    selected_space = (selected_run.get("admission", {}).get("native_space_ref", {}).get("record_id")
                      if event["capability"] == "code_review"
                      else selected_run.get("native_job_ref", {}).get("agent_space_id"))
    event_space = (run.get("admission", {}).get("native_space_ref", {}).get("record_id")
                   if event["capability"] == "code_review"
                   else run.get("native_job_ref", {}).get("agent_space_id"))
    if (selected_run.get("scope") != scope or selected_space != baseline["space"]
            or event_space != baseline["space"]):
        raise ValueError("event used another native provider space")
    sources = event["input"]["sources"]
    snapshots = run.get("admission", {}).get("source_snapshots", [])
    if (run.get("run_id") != run_id or run.get("scope") != scope
            or any(run.get("input", {}).get(field) != event["input"].get(field)
                   for field in ("capability", "binding_id", "sources"))
            or not sources or len(snapshots) != len(sources)
            or any(snapshot.get("source_ref") != source for snapshot, source in zip(snapshots, sources, strict=True))
            or run.get("state") != "succeeded"
            or run.get("provider_state") != "completed"
            or run.get("admission", {}).get("event_id") != event["event_id"]):
        raise ValueError("event run differs from selected source and receipt")
    binding = read_api(base + "/bindings/" + quote(event["input"]["binding_id"], safe=""))
    provider = case["provider"]
    if (binding.get("scope") != scope or binding.get("binding_id") != event["input"]["binding_id"]
            or binding.get("state") != "ready" or binding.get("gaps")
            or any(binding.get("provider_identity", {}).get(field) != provider[field]
                   for field in ("account_id", "role_arn", "region"))
            or run.get("admission", {}).get("provider_identity") != binding["provider_identity"]
            or binding.get("native_resource_ref", {}).get("record_id") != event_space):
        raise ValueError("event provider binding differs from selected source and native space")
    native = read_native(run, case["provider"])
    if (native.get("native_job_ref") != run.get("native_job_ref")
            or native.get("status") != "COMPLETED"):
        raise ValueError("event native job differs from Product run")
    results = []
    cursor, visited = None, set()
    while True:
        page = read_api(base + "/results?limit=100" +
                        ("&cursor=" + quote(cursor, safe="") if cursor else ""))
        results.extend(item for item in page["items"] if item.get("run_id") == run_id)
        cursor = page.get("next_cursor")
        if not cursor:
            break
        if cursor in visited or len(visited) >= 100:
            raise ValueError("event result pagination is incomplete")
        visited.add(cursor)
    if len(results) != 1:
        raise ValueError("selected event has missing or duplicated Product results")
    product_run, result, findings = read_assessment(scope, run_id, results[0]["result_id"], read_api)
    if (product_run != run or result.get("scope") != scope
            or result.get("native_job_ref") != run.get("native_job_ref")
            or result.get("coverage") != "complete"):
        raise ValueError("event Product result differs from scoped native job")
    fetched = read_sources(run)
    reviews, rubric_sha256 = read_review(event, run, result, native)
    verified = verify_assessment(run, result, findings, native, fetched, reviews)
    source_sha256 = [hashlib.sha256(item["content"]).hexdigest() for item in fetched]
    if source_sha256 != [snapshot["content"]["sha256"] for snapshot in snapshots]:
        raise ValueError("event source bytes differ from admitted snapshots")
    if inventory is None:
        raise ValueError("Product-authorized native inventory reader unavailable")
    current_jobs = inventory(run, case["provider"])
    if not isinstance(current_jobs, list) or any(not isinstance(job, dict) for job in current_jobs):
        raise ValueError("event provider inventory is missing")
    prior = {json.dumps(job, sort_keys=True) for job in baseline["jobs"]}
    current = {json.dumps(job, sort_keys=True) for job in current_jobs}
    if (len(prior) != len(baseline["jobs"]) or len(current) != len(current_jobs)
            or not prior <= current or current - prior != {json.dumps(run["native_job_ref"], sort_keys=True)}):
        raise ValueError("duplicate event created unexpected native provider work")
    after_path = Path(str(journal) + ".event-after.json")
    after = (json.dumps({"event_id": event["event_id"], "scope": scope, "run_id": run_id,
                         "space": event_space, "jobs": current_jobs,
                         "observed_at": datetime.now(UTC).isoformat()},
                        sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(after) > 65536:
        raise ValueError("event provider inventory exceeds private evidence bound")
    descriptor = os.open(after_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(after)
        destination.flush()
        os.fsync(destination.fileno())
    return {"event_id": event["event_id"], "run_id": run_id,
            "result_id": result["result_id"], "native_job_ref": run["native_job_ref"],
            "binding_id": binding["binding_id"], "provider_identity": binding["provider_identity"],
            "source_sha256": source_sha256, "snapshot_sha256": source_sha256,
            "rubric_sha256": rubric_sha256,
            "finding_count": len(findings), "reviewed": verified["reviewed"],
            "native_finding_ids": [item["native_id"] for item in native["findings"]],
            "before_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "journal_sha256": hashlib.sha256(content).hexdigest(),
            "observed_at": datetime.now(UTC).isoformat()}


def native_job(run, provider, read_api):
    scope = run["scope"]
    base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/security/runs/"
            f"{quote(run['run_id'], safe='')}")
    if any(run["admission"]["provider_identity"].get(key) != provider[key]
           for key in ("account_id", "role_arn", "region")):
        raise ValueError("selected native provider identity changed")
    return read_api(base + "/native-readback/job")


def native_inventory(run, provider, read_api):
    scope = run["scope"]
    if any(run["admission"]["provider_identity"].get(key) != provider[key]
           for key in ("account_id", "role_arn", "region")):
        raise ValueError("selected native provider identity changed")
    return read_api(f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                    f"{quote(scope['project_id'], safe='')}/services/security/runs/"
                    f"{quote(run['run_id'], safe='')}/native-readback/inventory")


def read_recovery(case, credentials, *, client_factory=httpx.Client, inventory=None):
    selection = case["recovery"]
    if (selection.get("scope") != case["scope"] or selection.get("sources") != case["selected"]["sources"]
            or selection.get("binding_id") != case["selected"]["binding_id"]
            or selection.get("exclusive_agent_space") is not True
            or not 1 <= selection["max_attempts"] <= case["max_attempts"]):
        raise ValueError("recovery selection differs from the selected scope or attempt bound")
    if not credentials.get(case["api_token_env"]):
        raise ValueError("Product recovery read authority is unavailable")
    journal_path = Path(selection["journal_file"])
    if journal_path.stat().st_size > 262144:
        raise ValueError("recovery journal exceeds private readback bound")
    entries = AttemptJournal(journal_path).read()
    events = {(entry["operation"], entry["status"]) for entry in entries}
    if not {("fault_arm", "acknowledged"), ("worker_restart", "acknowledged"),
            ("cleanup", "acknowledged")} <= events:
        raise ValueError("recovery journal does not contain the selected fault and cleanup")
    retained = [entry for entry in entries if entry["operation"] not in {
        "fault_arm", "run_submit", "worker_restart", "cleanup"}]
    if (not 1 <= len(retained) <= selection["max_attempts"]
            or len({entry["attempt_id"] for entry in retained}) != len(retained)):
        raise ValueError("recovery journal does not retain bounded distinct worker attempts")

    with client_factory(base_url=case["api_url"], timeout=20, follow_redirects=False,
                        headers={"Authorization": "Bearer " + credentials[case["api_token_env"]]}) as client:
        driver = object.__new__(LiveRecoveryDriver)
        driver.case = case
        driver.api = client
        driver.read_api = api_reader(client)
        driver.run_id = None
        try:
            run = driver._worker(selection)["run"]
            publication = driver._publication(selection)
        except AssertionError as error:
            raise ValueError("Product recovery differs from the selected receipt or source") from error
        if inventory is None:
            current_inventory = native_inventory(run, case["provider"], driver.read_api)
        else:
            current_inventory = inventory(run, case["provider"])
    run_id = run.get("run_id")
    if (run.get("scope") != case["scope"] or run.get("input") != selection["input"]
            or run.get("state") != "succeeded" or run.get("provider_state") != "completed"
            or not run.get("work", {}).get("completed_at")
            or len(run.get("attempts", [])) != len(retained)
            or {attempt.get("attempt_id") for attempt in run["attempts"]} != {
                entry["attempt_id"] for entry in retained}
            or any(entry["run_id"] != run_id or not entry["work_id"] for entry in retained)
            or any(entry["work_id"] != run_id for entry in entries if entry["operation"] == "cleanup")):
        raise ValueError("recovered Product run differs from selected durable attempts")
    if not isinstance(publication, dict):
        raise ValueError("Product recovery has no publication")
    receipt = publication.get("receipt")
    if (publication.get("state") != "published"
            or not isinstance(receipt, dict) or not receipt.get("receipt_id")
            or not publication.get("result_id") or not publication.get("result_version")
            or not isinstance(publication.get("publication_id"), str) or not publication["publication_id"]
            or receipt.get("contribution_id") != publication.get("publication_id")
            or receipt.get("admitted_schema") != publication.get("schema_version")
            or type(publication.get("attempts")) is not int or publication["attempts"] < 2):
        raise ValueError("recovered publication lacks a matching native receipt")
    reference = run.get("native_job_ref")
    if (not isinstance(reference, dict) or current_inventory != [reference]
            or any(attempt.get("native_job_ref") not in (None, reference) for attempt in run["attempts"])
            or any(entry["native_job_id"] not in (None, reference.get("record_id"))
                   for entry in retained)):
        raise ValueError("recovered work has missing or duplicate native provider jobs")
    descriptor = os.open(journal_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        content = os.read(descriptor, 262145)
    finally:
        os.close(descriptor)
    canonical_entries = [json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
                         for entry in entries]
    if len(content) > 262144 or content.splitlines() != canonical_entries:
        raise ValueError("recovery journal changed during independent readback")
    snapshot_path = Path(str(journal_path) + ".fault.json")
    descriptor = os.open(snapshot_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("fault-time snapshot must be a bounded private file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            snapshot_bytes = source.read(262145)
    finally:
        os.close(descriptor)
    if len(snapshot_bytes) > 262144:
        raise ValueError("fault-time snapshot exceeds private readback bound")
    snapshot = json.loads(snapshot_bytes)
    if (not isinstance(snapshot, dict) or set(snapshot) != {"receipt", "run", "publication", "observed_at"}
            or snapshot["receipt"] != selection["receipt"] or not isinstance(snapshot["run"], dict)
            or not isinstance(snapshot["publication"], dict)):
        raise ValueError("fault-time snapshot differs from the selected recovery")
    try:
        captured = datetime.fromisoformat(snapshot["observed_at"])
        restarted = min(datetime.fromisoformat(entry["observed_at"]) for entry in entries
                        if entry["operation"] == "worker_restart")
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("fault-time recovery has no comparable observation time") from error
    if (captured.utcoffset() is None or restarted.utcoffset() is None
            or not captured <= restarted <= datetime.now(UTC)):
        raise ValueError("fault-time snapshot does not precede the worker restart")
    result = verify_recovery(snapshot["run"], run, retained,
                             [{"native_job_ref": reference}], snapshot["publication"], publication,
                             {**recovery_progress(snapshot["run"], run, entries),
                              "max_attempts": selection["max_attempts"]})
    if (result["run_id"] != run_id or result["native_job_ref"] != reference
            or result["publication_id"] != publication["publication_id"]
            or result["attempts"] != len(retained)):
        raise ValueError("fault-time recovery differs from independent terminal readbacks")
    return {"run_id": run_id, "scope": case["scope"], "native_job_ref": reference,
            "publication_id": publication["publication_id"], "attempts": len(retained),
            "transition_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "journal_sha256": hashlib.sha256(content).hexdigest(),
            "observed_at": datetime.now(UTC).isoformat()}


def read_cleanup(case, credentials, directory, run_id, run_attempt, *, client_factory=httpx.Client):
    selection = case["recovery"]
    entries = [entry for entry in AttemptJournal(selection["journal_file"]).read()
               if entry["operation"] == "cleanup"]
    if (len(entries) != 2 or [entry["status"] for entry in entries] != ["prepared", "acknowledged"]
            or any(entry["attempt_id"] != "cleanup" or not entry["work_id"]
                   or entry["work_id"] != entries[0]["work_id"]
                   or entry["run_id"] != entry["work_id"] for entry in entries)
            or not selection["owned"] or set(selection["owned"]) & set(selection["linked"])):
        raise ValueError("owned cleanup has no single acknowledged attempt")
    with client_factory(base_url=case["api_url"], timeout=20, follow_redirects=False,
                        headers={"Authorization": "Bearer " + credentials[case["api_token_env"]]}) as client:
        driver = object.__new__(LiveRecoveryDriver)
        driver.case, driver.run_id, driver.read_api = case, entries[0]["work_id"], api_reader(client)
        packet = driver._cleanup_packet(selection)
        if driver._owned_binding(selection).get("state") != "cleaned":
            raise ValueError("Product cleanup operation is not complete")
    expected_ownership = {**{resource: "scenario_owned" for resource in selection["owned"]},
                          **{resource: "customer_linked" for resource in selection["linked"]}}
    if (not isinstance(packet, dict) or packet.get("run_id") != entries[0]["work_id"]
            or packet.get("ownership") != expected_ownership):
        raise ValueError("native cleanup readback differs from selected ownership or run")
    verified = verify_owned_cleanup(selection["owned"], selection["linked"], packet["resources"])
    content = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    filename = f"security-cleanup-{run_id}-{run_attempt}.json"
    descriptor = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())
    return {"run_id": packet["run_id"], **verified,
            "readback": [f"private://security/native-cleanup/{filename}#sha256:{digest}"],
            "observed_at": datetime.now(UTC).isoformat()}


def read_scope_denials(case, foreign_case, cleanup, credentials, *, client_factory=httpx.Client):
    selection = case.get("recovery")
    if (not isinstance(selection, dict) or selection.get("scope") != case["scope"]
            or selection.get("sources") != case["selected"]["sources"]
            or selection.get("binding_id") != case["selected"]["binding_id"]
            or not isinstance(cleanup.get("run_id"), str) or not cleanup["run_id"]
            or foreign_case["scope"]["tenant_id"] == case["scope"]["tenant_id"]
            or selection.get("foreign_product_id") in (None, case["scope"]["product_id"])):
        raise ValueError("selected recovery denial scope or cleaned run is incomplete")
    token_keys = (case["api_token_env"], foreign_case["api_token_env"], selection["revoked_token_env"])
    if any(not credentials.get(key) for key in token_keys) or len({credentials[key] for key in token_keys}) != 3:
        raise ValueError("denial probes require separate current Product and revoked credentials")
    with client_factory(base_url=case["api_url"], timeout=20, follow_redirects=False) as client:
        probes = read_scope_probes(case, cleanup["run_id"], selection, foreign_case,
                                   client, credentials.__getitem__, lambda: None)
    verify_scope_denials(probes)
    return {"run_id": cleanup["run_id"], "scope": case["scope"],
            "probes": probes, "observed_at": datetime.now(UTC).isoformat()}


def installed_status(directory, environment):
    role = environment["L5_MUTATION_ROLE_ARN"]
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+", role)
    if not match or not re.fullmatch(r"[0-9a-f]{64}", environment["ARCHIVE_DIGEST"]):
        raise ValueError("selected deployment role or archive digest unavailable")
    receipt = directory / f"installed-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}.json"
    if receipt.exists() or receipt.is_symlink():
        raise ValueError("installed state needs a fresh private readback path")
    command = [sys.executable, str(Path(__file__).with_name("worker-lifecycle.py")), "status",
               "--environment", environment["L5_ENVIRONMENT"], "--account", match.group(1),
               "--role-arn", role, "--receipt", str(receipt)]
    subprocess.run(command, capture_output=True, check=True, timeout=120)
    descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 262144:
            raise ValueError("installed lifecycle receipt is not a private regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(262145)
    finally:
        os.close(descriptor)
    if len(content) > 262144:
        raise ValueError("installed lifecycle receipt exceeds its byte bound")
    state = json.loads(content)
    expected = base64.b64encode(bytes.fromhex(environment["ARCHIVE_DIGEST"])).decode()
    if (state.get("result") != "passed" or state.get("observed", {}).get("worker_sha256") != expected
            or state["observed"].get("repair_sha256") != expected
            or state["observed"].get("mapping_state") != "Enabled"
            or state["observed"].get("repair_state") != "ENABLED"
            or state.get("action") != "status" or not isinstance(state.get("scope"), dict)
            or set(state["scope"]) != {"roles", "queues"}):
        raise ValueError("installed worker differs from published archive or is inactive")
    return {**state["observed"], "scope_receipt": (
        f"private://security/installed/{receipt.name}#sha256:{hashlib.sha256(content).hexdigest()}")}


def main(environment):
    environment = dict(environment)
    os.environ.pop("GH_TOKEN", None)
    directory = Path(environment["PRIVATE_EVIDENCE_DIR"]).resolve(strict=True)
    fixture_path = Path(environment["PRIVATE_FIXTURE"]).resolve(strict=True)
    if (directory.is_relative_to(Path(__file__).resolve().parents[3])
            or directory.stat().st_mode & 0o077
            or hashlib.sha256(fixture_path.read_bytes()).hexdigest() != environment["L5_FIXTURE_DIGEST"]):
        raise ValueError("private Security fixture changed after owner preflight")
    credentials = json.loads(environment["ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON"])
    if (not isinstance(credentials, dict) or not credentials or set(credentials) & set(os.environ)
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                   or not isinstance(value, str) or not value for name, value in credentials.items())):
        raise ValueError("private Product credentials unavailable")
    original = os.environ.copy()
    try:
        os.environ.update(credentials)
        fixture = load_live_fixture(fixture_path)
    finally:
        os.environ.clear()
        os.environ.update(original)
    expiry = datetime.fromisoformat(fixture["authorization"]["expires_at"].replace("Z", "+00:00"))
    if (expiry.tzinfo is None or expiry <= datetime.now(UTC)
            or fixture["authorization"]["source_sha"] != environment["SOURCE_SHA"]
            or fixture["release"]["worker"] != environment["SOURCE_SHA"]):
        raise ValueError("qualified Product release differs from reviewed source")
    selector_receipts = read_selector_receipts(directory, environment, datetime.now(UTC))
    installed = installed_status(directory, environment)
    gateway = gateway_status(environment)
    setup = [read_setup(case, fixture["limits"], credentials) for case in fixture["cases"]]
    context = read_context(fixture["cases"][0], fixture["cases"][1], fixture["limits"], credentials)
    records = []
    triggers = []
    for case in fixture["cases"]:
        with httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                          headers={"Authorization": "Bearer " + credentials[case["api_token_env"]]}) as client:
            read_api = api_reader(client)
            records.append({"scope": case["scope"], "assessments": assess_case(
                case, read_api, lambda run, provider: native_job(run, provider, read_api),
                lambda run: product_sources(run, client))})
            triggers.append(read_trigger(case, credentials, read_api,
                lambda run, provider: native_job(run, provider, read_api),
                lambda run: product_sources(run, client),
                inventory=lambda run, provider: native_inventory(run, provider, read_api)))
    cleanup = read_cleanup(fixture["cases"][0], credentials, directory,
                           environment["GITHUB_RUN_ID"], environment["GITHUB_RUN_ATTEMPT"])
    scope_denials = read_scope_denials(fixture["cases"][0], fixture["cases"][1], cleanup, credentials)
    recovery = read_recovery(fixture["cases"][0], credentials)
    output = directory / f"security-readback-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}.json"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as destination:
        json.dump({"source_revision": environment["SOURCE_SHA"],
                   "definition_revision": environment["GITHUB_WORKFLOW_SHA"],
                   "workflow_path": ".github/workflows/l5-services-release.yml",
                   "wheel_digest": environment["WHEEL_DIGEST"], "archive_digest": environment["ARCHIVE_DIGEST"],
                   "manifest_version": environment["MANIFEST_VERSION"],
                   "fixture_digest": environment["L5_FIXTURE_DIGEST"],
                   "run_id": environment["GITHUB_RUN_ID"], "run_attempt": environment["GITHUB_RUN_ATTEMPT"],
                   "observed_at": datetime.now(UTC).isoformat(), "installed": installed,
                   "gateway": gateway, "setup": setup, "context": context,
                   "selector_receipts": selector_receipts,
                   "products": records, "triggers": triggers, "cleanup": cleanup, "scope_denials": scope_denials,
                   "recovery": recovery}, destination, sort_keys=True)
        destination.write("\n")
    return output


if __name__ == "__main__":
    try:
        main(os.environ)
    except (AssertionError, KeyError, TypeError, ValueError, OSError,
            subprocess.SubprocessError, httpx.HTTPError):
        raise SystemExit("independent Security readback unavailable; inspect private evidence") from None
    print("independent native Security readback retained privately; delivery evidence remains pending")
