"""Bounded, pre-admission capture of exact v2 requirement and test evidence."""

import hashlib
import json

from modules.insights.schemas.validate import validate_record


REFERENCE_FIELDS = ("tenant_id", "project_id", "source_connection_id",
                    "record_type", "record_id", "record_revision")


def _dependencies(value):
    if isinstance(value, dict):
        if set(REFERENCE_FIELDS) == set(value):
            yield value
        elif set(value) == {"artifact_id", "version_id", "sha256"}:
            yield value
        else:
            for child in value.values():
                yield from _dependencies(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dependencies(child)


def _is_exact_artifact_reference(reference):
    return (isinstance(reference, dict) and
            set(reference) == {"artifact_id", "version_id", "sha256"} and
            all(isinstance(value, str) and value for value in reference.values()) and
            reference["version_id"] not in ("latest", "null") and
            len(reference["sha256"]) == 64 and
            all(character in "0123456789abcdef" for character in reference["sha256"]))


def _validate_source_snapshot(source_snapshot, source_bytes):
    if (not isinstance(source_bytes, bytes) or not _is_exact_artifact_reference(source_snapshot) or
            source_snapshot["sha256"] != hashlib.sha256(source_bytes).hexdigest()):
        raise ValueError("exact source snapshot bytes and version are required")


def _resolve_dependencies(dependencies, resolve_reference):
    resolved = []
    for dependency in dependencies:
        policy = resolve_reference(dependency)
        if (not isinstance(policy, dict) or not isinstance(policy.get("realm_id"), str) or
                not policy["realm_id"] or policy.get("visibility") not in ("shared", "private") or
                not isinstance(policy.get("policy_ref"), str) or not policy["policy_ref"] or
                type(policy.get("policy_revision")) is not int or policy["policy_revision"] < 1 or
                (resolved and policy["realm_id"] != resolved[0]["policy"]["realm_id"])):
            raise ValueError("authority did not resolve every dependency in the same realm")
        resolved.append({"reference": dependency, "policy": policy})
    return resolved


def capture_record(record, source_snapshot, source_bytes, resolve_reference, requested_audience=None):
    """Prepare a v2 record for admission; resolve every dependency with authority.

    ``resolve_reference`` reads current policy for each qualified record or exact
    artifact reference. It must not use classifications embedded in the payload.
    This function does not persist or publish the returned candidate.
    """
    errors = validate_record(record, record.get("record_type"), version="v2")
    if errors:
        raise ValueError("invalid v2 record: " + errors[0].message)
    _validate_source_snapshot(source_snapshot, source_bytes)
    if record.get("source_snapshot") is not None and record["source_snapshot"] != source_snapshot:
        raise ValueError("source snapshot differs from recorded revision")

    reference = {field: record[field] for field in REFERENCE_FIELDS}
    dependencies = [reference]
    for dependency in (*_dependencies(record), source_snapshot):
        if dependency not in dependencies:
            dependencies.append(dependency)

    resolved = _resolve_dependencies(dependencies, resolve_reference)

    if requested_audience not in (None, "private", "shared"):
        raise ValueError("unknown requested audience")
    private = any(item["policy"]["visibility"] == "private" for item in resolved)
    if requested_audience == "shared" and private:
        raise ValueError("private dependency cannot be admitted as shared")
    captured = json.loads(json.dumps(record, sort_keys=True))
    return {
        "record": captured,
        "reference": reference,
        "source_snapshot": dict(source_snapshot),
        "record_sha256": hashlib.sha256(json.dumps(captured, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "dependencies": json.loads(json.dumps(resolved)),
        "audience": "private" if private or requested_audience == "private" else "shared",
    }


def criterion_history(captured_records, story_ref, criterion_key):
    """Group already-authorized captures by exact criterion revision, never by latest.

    This offline projection does not authorize a read or infer a verdict. The
    admission/read owner must enforce current policy before supplying records.
    """
    story_identity = {field: story_ref[field] for field in REFERENCE_FIELDS if field != "record_revision"}
    history = {}
    assessments = []
    for capture in captured_records:
        record = capture["record"]
        reference = {field: record[field] for field in REFERENCE_FIELDS}
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (capture["reference"] != reference or capture["record_sha256"] != digest or
                validate_record(record, record["record_type"], version="v2")):
            raise ValueError("capture changed after source readback")
        if record["record_type"] == "acceptance_criterion":
            owner = {field: record["story_ref"][field] for field in story_identity}
            if owner != story_identity or record["criterion_key"] != criterion_key:
                continue
            key = tuple(reference[field] for field in REFERENCE_FIELDS)
            if key in history and (history[key]["criterion"] != record or
                                   history[key]["source_snapshot"] != capture["source_snapshot"]):
                raise ValueError("one criterion revision has conflicting bytes")
            history[key] = {"criterion": record, "source_snapshot": capture["source_snapshot"],
                            "assessments": []}
        elif record["record_type"] == "acceptance_assessment":
            assessments.append(record)

    for assessment in assessments:
        key = tuple(assessment["criterion_ref"][field] for field in REFERENCE_FIELDS)
        if key in history:
            history[key]["assessments"].append(assessment)
    return sorted(history.values(), key=lambda entry: (entry["criterion"]["event_time"],
                                                      entry["criterion"]["record_revision"]))


def match_delivery(criterion, test_definition, run_test, test_run, result, tested_artifact, dev_receipt):
    """Compare exact offline evidence; no input is accepted or published here."""
    records = (criterion, test_definition, run_test, test_run, result)
    types = ("acceptance_criterion", "test_definition", "run_test", "test_run", "test_result")
    if any(record.get("record_type") != record_type or validate_record(record, record_type, "v2")
           for record, record_type in zip(records, types)):
        raise ValueError("invalid or missing v2 test evidence")
    criterion_ref, test_ref, instance_ref, run_ref = (
        {field: record[field] for field in REFERENCE_FIELDS}
        for record in (criterion, test_definition, run_test, test_run)
    )
    if (run_test["requirement_binding"] != "complete" or criterion_ref not in run_test["criterion_refs"] or
            run_test["test_ref"] != test_ref or run_test["run_ref"] != run_ref or
            result["run_test_ref"] != instance_ref or test_run["selection_status"] != "complete" or
            test_run["selection_snapshot"] is None or
            test_definition["source_revision"] != test_run["source_revision"]):
        raise ValueError("criterion, selected test and result do not share exact references")
    delivery = _match_artifact_delivery(test_definition, test_run, tested_artifact, dev_receipt)
    return {"criterion_ref": criterion_ref, "test_ref": test_ref, "instance_ref": instance_ref,
            "result_ref": {field: result[field] for field in REFERENCE_FIELDS},
            "attempt": result["attempt"], "outcome": result["outcome"],
            "run_ref": run_ref, "selection_snapshot": test_run["selection_snapshot"],
            "source_revision": test_run["source_revision"],
            "tested_artifact": tested_artifact["artifact_ref"], "delivery": delivery}


def _match_artifact_delivery(test_definition, test_run, tested_artifact, dev_receipt):
    if test_definition["source_revision"] != test_run["source_revision"]:
        raise ValueError("test definition and run do not share tested code")
    if (not isinstance(tested_artifact, dict) or tested_artifact.get("source_revision") != test_run["source_revision"] or
            tested_artifact.get("selection_snapshot") != test_run["selection_snapshot"] or
            not isinstance(tested_artifact.get("artifact_ref"), dict) or
            set(tested_artifact["artifact_ref"]) != {"artifact_id", "version_id", "sha256"} or
            not all(tested_artifact["artifact_ref"].values()) or
            tested_artifact["artifact_ref"]["version_id"] in ("latest", "null") or
            len(tested_artifact["artifact_ref"]["sha256"]) != 64 or
            any(character not in "0123456789abcdef" for character in tested_artifact["artifact_ref"]["sha256"])):
        raise ValueError("tested artifact has no exact CI selection and code provenance")
    delivery = {"status": "unknown"}
    if dev_receipt is not None:
        if (dev_receipt.get("artifact_ref") != tested_artifact["artifact_ref"] or
                dev_receipt.get("source_revision") != test_run["source_revision"]):
            raise ValueError("dev delivery digest or tested code revision differs")
        if dev_receipt.get("environment") == "dev" and dev_receipt.get("source_snapshot"):
            delivery = {"status": "matched_input", "source_snapshot": dev_receipt["source_snapshot"],
                        "environment": "dev"}
    return delivery


def attempts_and_missing(test_run, planned_instances, results):
    """Report one CI run's pinned selection; never infer instances from a green run."""
    if validate_record(test_run, "test_run", version="v2"):
        raise ValueError("invalid test run")
    run_ref = {field: test_run[field] for field in REFERENCE_FIELDS}
    selected = {}
    for instance in planned_instances:
        if (validate_record(instance, "run_test", version="v2") or
                instance["run_ref"] != run_ref):
            raise ValueError("planned instance differs from this exact run")
        instance_ref = {field: instance[field] for field in REFERENCE_FIELDS}
        key = tuple(instance_ref.values())
        if key in selected:
            raise ValueError("duplicate selected instance")
        selected[key] = {"instance_ref": instance_ref, "required": instance["required"],
                         "requirement_binding": instance["requirement_binding"], "attempts": []}

    seen = set()
    for result in results:
        if validate_record(result, "test_result", version="v2"):
            raise ValueError("invalid test attempt")
        key = tuple(result["run_test_ref"][field] for field in REFERENCE_FIELDS)
        attempt_key = (key, result["attempt"])
        if key not in selected or attempt_key in seen:
            raise ValueError("test result is unselected or duplicates an attempt")
        seen.add(attempt_key)
        selected[key]["attempts"].append({"result_ref": {field: result[field] for field in REFERENCE_FIELDS},
                                          "attempt": result["attempt"], "outcome": result["outcome"]})

    instances = sorted(selected.values(), key=lambda item: tuple(item["instance_ref"].values()))
    complete_selection = (test_run["selection_status"] == "complete" and
                          test_run["selection_snapshot"] is not None and
                          test_run["requirements_snapshot"] is not None and
                          test_run["execution_status"] == "completed")
    for item in instances:
        item["attempts"].sort(key=lambda attempt: attempt["attempt"])
        present = {attempt["attempt"] for attempt in item["attempts"]}
        item["missing_attempts"] = [number for number in range(1, max(present, default=0) + 1)
                                    if number not in present]
        item["status"] = "observed" if present else "missing"
        if item["requirement_binding"] != "complete" or item["missing_attempts"]:
            complete_selection = False
    observed = sum(item["status"] == "observed" for item in instances)
    percentage = (100 * observed / len(instances)) if complete_selection and instances else None
    return {"run_ref": run_ref, "source_attempt": test_run["source_attempt"],
            "selection_snapshot": test_run["selection_snapshot"], "instances": instances,
            "completeness_percent": percentage, "acceptance": "not_assessed"}


def read_evidence_chain(read_authorized, story_ref, criterion_key, run_ref,
                        tested_artifact=None, dev_receipt=None, requested_audience="private", *,
                        resolve_reference):
    """Project one Story and CI run from a caller-authorized read, without writes.

    The reader must recheck current admission policy and return exact v2 captures.
    Its trusted policy resolver must authorize the full dependency closure,
    including separately supplied artifact and receipt references, for this reader.
    Without that sink, this selector is only exercisable with offline fixtures.
    """
    if (not callable(read_authorized) or not callable(resolve_reference) or
            requested_audience not in ("private", "shared") or
            story_ref.get("record_type") != "story_revision" or
            run_ref.get("record_type") != "test_run" or
            set(story_ref) != set(REFERENCE_FIELDS) or set(run_ref) != set(REFERENCE_FIELDS) or
            not criterion_key or any(story_ref[field] != run_ref[field] for field in ("tenant_id", "project_id"))):
        raise ValueError("one qualified Story and run with authorized read are required")
    captured = read_authorized(story_ref, criterion_key, run_ref)
    if not isinstance(captured, list) or len(captured) > 64:
        raise ValueError("controlled Story read is missing or exceeds the bound")
    records = {}
    report_dependencies = []
    for candidate in captured:
        record = candidate["record"]
        reference = {field: record[field] for field in REFERENCE_FIELDS}
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        dependencies = candidate.get("dependencies")
        private_dependency = (not isinstance(dependencies, list) or not dependencies or
                              candidate.get("source_snapshot") not in
                              [item.get("reference") for item in dependencies] or
                              any(item.get("policy", {}).get("visibility") not in ("shared", "private") or
                                  item["policy"]["visibility"] == "private" for item in dependencies))
        if (candidate["reference"] != reference or candidate["record_sha256"] != digest or
                candidate.get("audience") not in ("private", "shared") or
                candidate["audience"] == "shared" and private_dependency or
                validate_record(record, record["record_type"], version="v2") or
                any(record[field] != story_ref[field] for field in ("tenant_id", "project_id")) or
                requested_audience == "shared" and candidate["audience"] != "shared"):
            raise ValueError("unauthorized or changed evidence in controlled Story read")
        key = tuple(reference[field] for field in REFERENCE_FIELDS)
        if key in records and records[key] != record:
            raise ValueError("conflicting exact evidence reference")
        records[key] = record
        for dependency in (reference, candidate["source_snapshot"], *_dependencies(record),
                           *(item["reference"] for item in dependencies)):
            if dependency not in report_dependencies:
                report_dependencies.append(dependency)
    for evidence in (tested_artifact, dev_receipt):
        if evidence is None:
            continue
        if not isinstance(evidence, dict) or not _is_exact_artifact_reference(evidence.get("artifact_ref")):
            raise ValueError("exact artifact and receipt references are required")
        for field in ("selection_snapshot", "source_snapshot"):
            if evidence.get(field) is not None and not _is_exact_artifact_reference(evidence[field]):
                raise ValueError("exact artifact and receipt references are required")
        for dependency in _dependencies(evidence):
            if dependency not in report_dependencies:
                report_dependencies.append(dependency)
    resolved = _resolve_dependencies(report_dependencies, resolve_reference)
    if requested_audience == "shared" and any(item["policy"]["visibility"] == "private" for item in resolved):
        raise ValueError("private dependency cannot be projected as shared")
    story_records = {key: record for key, record in records.items() if record["record_type"] == "story_revision"}
    history = criterion_history(captured, story_ref, criterion_key)
    run = records.get(tuple(run_ref[field] for field in REFERENCE_FIELDS))
    gaps = []
    revisions = []
    for item in history:
        criterion = item["criterion"]
        criterion_ref = {field: criterion[field] for field in REFERENCE_FIELDS}
        if tuple(criterion["story_ref"][field] for field in REFERENCE_FIELDS) not in story_records:
            gaps.append({"criterion_ref": criterion_ref, "reason": "story_source_missing"})
        selected = [record for record in records.values() if record["record_type"] == "run_test" and
                    criterion_ref in record["criterion_refs"] and record["run_ref"] == run_ref]
        coverage, links = None, []
        if run is None or not selected:
            gaps.append({"criterion_ref": criterion_ref, "reason": "selection_missing"})
        else:
            selected_refs = [{field: record[field] for field in REFERENCE_FIELDS} for record in selected]
            results = [record for record in records.values() if record["record_type"] == "test_result" and
                       record["run_test_ref"] in selected_refs]
            coverage = attempts_and_missing(run, selected, results)
            selection_known = (run["selection_status"] == "complete" and
                               run["selection_snapshot"] is not None and
                               run["requirements_snapshot"] is not None)
            if not selection_known:
                gaps.append({"criterion_ref": criterion_ref, "reason": "selection_snapshot_missing"})
            for instance in selected:
                instance_ref = {field: instance[field] for field in REFERENCE_FIELDS}
                binding_known = instance["requirement_binding"] == "complete"
                if not binding_known:
                    gaps.append({"criterion_ref": criterion_ref, "instance_ref": instance_ref,
                                 "reason": "requirement_binding_unknown"})
                attempts = [result for result in results if result["run_test_ref"] == instance_ref]
                if not attempts:
                    gaps.append({"criterion_ref": criterion_ref, "instance_ref": instance_ref,
                                 "reason": "planned_result_missing"})
                test = records.get(tuple(instance["test_ref"][field] for field in REFERENCE_FIELDS))
                if test is None or tested_artifact is None:
                    gaps.append({"criterion_ref": criterion_ref, "instance_ref": instance_ref,
                                 "reason": "test_or_artifact_missing"})
                elif selection_known:
                    if binding_known:
                        for result in attempts:
                            links.append(match_delivery(criterion, test, instance, run, result,
                                                        tested_artifact, dev_receipt))
                    else:
                        _match_artifact_delivery(test, run, tested_artifact, dev_receipt)
        revisions.append({"criterion_ref": criterion_ref, "text": criterion["text"],
                          "source_snapshot": item["source_snapshot"], "assessments": item["assessments"],
                          "assessment_status": "recorded" if item["assessments"] else "unassessed",
                          "coverage": coverage, "links": links})
    if not history:
        gaps.append({"reason": "criterion_missing"})
    return {"story_ref": story_ref, "criterion_key": criterion_key, "run_ref": run_ref,
            "revisions": revisions, "gaps": gaps, "audience": requested_audience,
            "dependencies": json.loads(json.dumps(resolved))}


def map_github_requirement_revision(snapshot_bytes, source_snapshot, source_event,
                                    criterion_key, resolve_reference):
    """Capture one pinned GitHub issue edit and its explicit validation-table criterion.

    The source owner supplies the original issue bytes and event identity; this
    mapper neither fetches history nor invents an earlier revision from new text.
    """
    if not isinstance(snapshot_bytes, bytes) or len(snapshot_bytes) > 1048576:
        raise ValueError("one bounded native issue snapshot is required")
    try:
        native = json.loads(snapshot_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid native issue snapshot") from error
    if (not isinstance(native, dict) or not isinstance(source_event, dict) or
            not isinstance(criterion_key, str) or not criterion_key or
            any(not isinstance(native.get(field), str) or not native[field]
                for field in ("node_id", "title", "body", "html_url", "updated_at", "state")) or
            any(not isinstance(source_event.get(field), str) or not source_event[field]
                for field in ("tenant_id", "project_id", "source_connection_id", "source_event_id",
                              "record_revision", "event_time", "ingested_at", "source_action"))):
        raise ValueError("native issue and explicit source event are required")
    rows = [tuple(cell.strip() for cell in line.strip().split("|")[1:-1])
            for line in native["body"].splitlines() if line.strip().startswith("|") and line.strip().endswith("|")]
    matches = [row for row in rows if len(row) >= 3 and row[0] == criterion_key and row[2]]
    if len(matches) != 1:
        raise ValueError("criterion bytes are missing or ambiguous in this exact source revision")

    envelope = {field: source_event[field] for field in
                ("tenant_id", "project_id", "source_connection_id", "source_event_id",
                 "record_revision", "event_time", "ingested_at")}
    envelope.update(schema_version="2.0", source_system="github")
    story = {**envelope, "record_type": "story_revision", "record_id": native["node_id"],
             "title": native["title"], "brief": native["body"], "source_url": native["html_url"],
             "source_updated_at": native["updated_at"], "source_action": source_event["source_action"],
             "story_state": native["state"], "source_snapshot": source_snapshot,
             "criteria_extraction_status": "complete"}
    story_ref = {field: story[field] for field in REFERENCE_FIELDS}
    criterion = {**envelope, "source_event_id": envelope["source_event_id"] + "/" + criterion_key,
                 "record_type": "acceptance_criterion", "record_id": native["node_id"] + "/" + criterion_key,
                 "story_ref": story_ref, "criterion_key": criterion_key, "text": matches[0][2],
                 "lifecycle_status": "active"}
    return (capture_record(story, source_snapshot, snapshot_bytes, resolve_reference),
            capture_record(criterion, source_snapshot, snapshot_bytes, resolve_reference))


def map_native_ci_run(run_bytes, run_snapshot, selection_bytes, selection_snapshot,
                      result_receipts, source_event, resolve_reference):
    """Map one CI run, its explicit selection and individual attempt receipts.

    Native snapshots and source identities must be retained by their owners.
    A run summary cannot create a selected test or a per-test result.
    """
    if (not isinstance(run_bytes, bytes) or len(run_bytes) > 1048576 or
            not isinstance(source_event, dict) or not isinstance(result_receipts, list) or
            len(result_receipts) > 500 or
            (selection_bytes is None) != (selection_snapshot is None) or
            selection_bytes is not None and (not isinstance(selection_bytes, bytes) or
                                             len(selection_bytes) > 1048576)):
        raise ValueError("bounded CI run and paired selection snapshot are required")
    if selection_bytes is not None:
        _validate_source_snapshot(selection_snapshot, selection_bytes)
    try:
        native = json.loads(run_bytes)
        selection = json.loads(selection_bytes) if selection_bytes is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise ValueError("invalid native CI snapshots") from error
    if (not isinstance(native, dict) or
            any(not isinstance(source_event.get(field), str) or not source_event[field]
                for field in ("tenant_id", "project_id", "source_connection_id", "source_event_id",
                              "record_revision", "event_time", "ingested_at")) or
            not isinstance(native.get("source_run_id"), str) or not native["source_run_id"] or
            type(native.get("source_attempt")) is not int or native["source_attempt"] < 1 or
            not isinstance(native.get("source_revision"), str) or not native["source_revision"] or
            selection is not None and (not isinstance(selection, dict) or
                                       not isinstance(selection.get("instances"), list) or
                                       len(selection["instances"]) > 100) or
            selection is None and result_receipts):
        raise ValueError("native CI selection and run identity are required")

    envelope = {field: source_event[field] for field in
                ("tenant_id", "project_id", "source_connection_id", "source_event_id",
                 "record_revision", "event_time", "ingested_at")}
    envelope.update(schema_version="2.0", source_system="ci")
    run_id = native["source_run_id"] + "/" + str(native["source_attempt"])
    run = {**envelope, "record_type": "test_run", "record_id": run_id,
           "source_run_id": native["source_run_id"], "source_attempt": native["source_attempt"],
           "source_revision": native["source_revision"], "environment": native.get("environment"),
           "execution_status": native.get("execution_status"), "started_at": native.get("started_at"),
           "ended_at": native.get("ended_at"), "catalog_snapshot": native.get("catalog_snapshot"),
           "requirements_snapshot": native.get("requirements_snapshot"),
           "selection_status": "complete" if selection is not None and native.get("selection_complete") is True
                               else "unknown", "selection_snapshot": selection_snapshot}
    run_ref = {field: run[field] for field in REFERENCE_FIELDS}
    captured = [capture_record(run, run_snapshot, run_bytes, resolve_reference)]
    instances = {}
    definitions = {}
    for item in selection["instances"] if selection is not None else []:
        if not isinstance(item, dict) or not isinstance(item.get("test"), dict):
            raise ValueError("selection lacks a pinned native test definition")
        test = item["test"]
        test_id, test_revision = test.get("id"), test.get("revision")
        if not isinstance(test_id, str) or not test_id or not isinstance(test_revision, str) or not test_revision:
            raise ValueError("selection lacks an exact test identity")
        definition = {**envelope, "record_type": "test_definition", "record_id": test_id,
                      "record_revision": test_revision,
                      "source_event_id": test.get("source_event_id"),
                      "event_time": test.get("event_time"), "ingested_at": test.get("ingested_at"),
                      "name": test.get("name"), "source_path": test.get("source_path"),
                      "selector": test.get("selector"), "framework": test.get("framework"),
                      "layer": test.get("layer"), "source_revision": test.get("source_revision")}
        if test.get("source_revision") != native["source_revision"]:
            raise ValueError("selected test definition differs from tested code")
        test_ref = {field: definition[field] for field in REFERENCE_FIELDS}
        definition_key = (test_id, test_revision)
        if definition_key in definitions and definitions[definition_key] != definition:
            raise ValueError("one test revision has conflicting definitions")
        if definition_key not in definitions:
            captured.append(capture_record(definition, selection_snapshot, selection_bytes, resolve_reference))
            definitions[definition_key] = definition
        instance_id = item.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("selection lacks a planned test instance")
        instance = {**envelope, "record_type": "run_test",
                    "source_event_id": envelope["source_event_id"] + "/selected/" + test_id + "/" + instance_id,
                    "record_id": run_id + "/" + test_id + "/" + instance_id,
                    "run_ref": run_ref, "test_ref": test_ref, "instance_id": instance_id,
                    "parameter_digest": item.get("parameter_digest"), "required": item.get("required"),
                    "scenario_refs": item.get("scenario_refs"), "criterion_refs": item.get("criterion_refs"),
                    "requirement_binding": item.get("requirement_binding")}
        instance_key = (test_id, instance_id)
        if instance_key in instances:
            raise ValueError("duplicate native selected instance")
        captured.append(capture_record(instance, selection_snapshot, selection_bytes, resolve_reference))
        instances[instance_key] = instance

    seen_attempts = set()
    for receipt_bytes, receipt_snapshot in result_receipts:
        if not isinstance(receipt_bytes, bytes) or len(receipt_bytes) > 1048576:
            raise ValueError("bounded per-attempt receipt bytes are required")
        try:
            receipt = json.loads(receipt_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid native test attempt") from error
        if (not isinstance(receipt, dict) or
                receipt.get("source_run_id") != native["source_run_id"] or
                receipt.get("source_attempt") != native["source_attempt"] or
                (receipt.get("test_id"), receipt.get("instance_id")) not in instances or
                type(receipt.get("attempt")) is not int or receipt["attempt"] < 1 or
                not isinstance(receipt.get("source_event_id"), str) or not receipt["source_event_id"]):
            raise ValueError("test attempt has no exact selected run and source event")
        instance = instances[(receipt["test_id"], receipt["instance_id"])]
        attempt_key = (receipt["test_id"], receipt["instance_id"], receipt["attempt"])
        if attempt_key in seen_attempts:
            raise ValueError("duplicate native retry")
        seen_attempts.add(attempt_key)
        result = {**envelope, "record_type": "test_result",
                  "source_event_id": receipt["source_event_id"], "record_revision": receipt["source_event_id"],
                  "record_id": instance["record_id"] + "/" + str(receipt["attempt"]),
                  "run_test_ref": {field: instance[field] for field in REFERENCE_FIELDS},
                  "attempt": receipt["attempt"], "outcome": receipt.get("outcome"),
                  "started_at": receipt.get("started_at"), "ended_at": receipt.get("ended_at"),
                  "duration_ms": receipt.get("duration_ms"),
                  "failure_category": receipt.get("failure_category"),
                  "artifact_refs": receipt.get("artifact_refs", [])}
        captured.append(capture_record(result, receipt_snapshot, receipt_bytes, resolve_reference))
    return captured
