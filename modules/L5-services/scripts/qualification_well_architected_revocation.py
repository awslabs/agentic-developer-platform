"""Read a selected real unlink after retaining this release's native evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import re
from urllib.parse import quote

from l5_services.adapters.well_architected.qualification import ProductReader
from qualification_well_architected_observations import digest, selections
from qualification_well_architected_reuse import canonical, human, timestamp
from qualification_well_architected_denials import denied_reads, verify_baseline


def selected_operations(value, cases):
    if (not isinstance(value, dict) or not value or len(value) > len(cases)
            or any(not isinstance(key, str) or key not in {case["run_id"] for case in cases}
                   or not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", operation)
                   for key, operation in value.items())
            or len(set(value.values())) != len(value)):
        raise ValueError("select exact existing unlink operation IDs for the original release runs")
    if any("qualification_cleanup" in case and case["run_id"] in value for case in cases):
        raise ValueError("combined native cleanup and source revocation needs separate retained transition evidence")
    return value


def retained_selection(case, native, prior):
    selected = selections(case)
    recorded = prior.get("qualification_observations") or {}
    if (recorded.get("source") != "authenticated-product-api" or recorded.get("scope") != case["scope"]
            or recorded.get("run_id") != case["run_id"] or recorded.get("run_revision") != case["run_revision"]
            or recorded.get("execution_authorized") is not False
            or native.get("run_id") != case["run_id"] or prior.get("run_id") != case["run_id"]
            or native.get("result_revision") != case["result_revision"] or prior.get("result_revision") != case["result_revision"]):
        raise ValueError("source revocation needs this exact release's original observation receipts")
    matches = []
    for item in recorded.get("items", []):
        receipt = item.get("receipt") or {}
        if receipt.get("selection", {}).get("source_revocation") is None:
            continue
        if (item.get("receipt_sha256") != digest(receipt)
                or item.get("observation_sha256") != digest(item.get("observation"))
                or {key: receipt.get(key) for key in ("experiment_id", "revision", "selection_digest")} not in selected
                or digest(receipt.get("selection")) != receipt.get("selection_digest")):
            raise ValueError("the retained source selection is changed or unselected")
        frozen = receipt["selection"]["source_revocation"]
        result = frozen.get("result") or {}
        if (result.get("result_id") != case["result_id"] or result.get("revision") != case["result_revision"]
                or result.get("result_digest") != digest(native.get("result"))
                or result.get("run_revision") != case["run_revision"]
                or result.get("native_job_ref_digest") != digest(native.get("native_job_ref"))
                or result.get("source_manifest_digest") != digest(native.get("source_manifest"))
                or receipt["selection"].get("run_input_digest") != digest(native.get("run_input"))
                or frozen.get("review_settings_digest") != digest(native.get("settings"))
                or frozen.get("source_ref") not in case["sources"]):
            raise ValueError("the source selection did not freeze this terminal result before unlink")
        manifest, projection = native["source_manifest"], native["result"]
        if (native.get("project_kind") != case.get("project_kind") or case.get("project_kind") != "non_adp"
                or native.get("architecture") != case["sources"][0]
                or not native["settings"].get("goal_refs") or native.get("goal") != native["settings"]["goal_refs"][0]
                or native.get("source_receipt") != {"architecture": case["sources"][0], "native_job_ref": projection["native_job_ref"],
                    "sources": case["sources"], "files": projection["coverage"], "archive": projection["manifest_ref"],
                    "provider_source_uri": manifest["provider_source_uri"], "provider_coverage": "provider_coverage_unverified"}):
            raise ValueError("retained native projection differs from the frozen result/source/settings evidence")
        matches.append(receipt)
    if len(matches) != 1:
        raise ValueError("select exactly one retained source transition for this release run")
    return matches[0], recorded["reader"]


def verify_proof(value, case, retained, actor, operation_id, baseline_at, started):
    frozen = retained["selection"]["source_revocation"]
    now = datetime.now(UTC)
    if (value.get("schema_version") != 1 or value.get("source") != "durable-product-source-transition"
            or value.get("scope") != {key: case["scope"][key] for key in ("tenant_id", "product_id", "project_id")}
            or value.get("actor_id") != actor["user_id"] or value.get("run_id") != case["run_id"]
            or value.get("experiment_id") != retained["experiment_id"]
            or value.get("selection_digest") != retained["selection_digest"]
            or value.get("source_ref_digest") != digest(frozen["source_ref"])
            or value.get("source_binding_id") != frozen["binding_id"]
            or value.get("generation_before") != frozen["generation"]
            or value.get("generation_after") != frozen["generation"] + 1
            or value.get("result") != frozen["result"]
            or value.get("operation_id") != operation_id or value.get("operation_kind") != "unlinkBinding"
            or value.get("operation_state") != "succeeded" or not isinstance(value.get("change_id"), str) or not value["change_id"]
            or type(value.get("change_revision")) is not int or value["change_revision"] <= frozen["product_revision"]
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                   for key in ("operation_digest", "change_digest"))
            or timestamp(value.get("selected_at", "")) != timestamp(retained["created_at"])
            or not timestamp(retained["created_at"]) <= baseline_at <= timestamp(value.get("unlinked_at", ""))
                   <= timestamp(value.get("recorded_at", "")) <= timestamp(value.get("observed_at", ""))
            or not started - timedelta(seconds=30) <= timestamp(value["observed_at"]) <= now + timedelta(seconds=30)
            or value.get("current_source_access") != "denied" or value.get("execution_authorized") is not False
            or value.get("provider_effect_count") is not None):
        raise ValueError("completed unlink provenance does not match the earlier selected source and terminal result")
    return value


def stable_proof(value):
    return {key: item for key, item in value.items() if key != "observed_at"}


def collect(case, native, prior, operation_id, baseline_at, connection):
    retained, recorded_actor = retained_selection(case, native, prior)
    started = datetime.now(UTC)
    with connection(case) as client:
        actor = human(client)
        if actor != recorded_actor or actor["org_id"] != case["scope"]["tenant_id"]:
            raise ValueError("source revocation requires the original authenticated owner")
        reader = ProductReader(client, case["scope"])
        path = "qualification-experiments/" + quote(retained["experiment_id"], safe="")

        def current():
            receipt = reader.get(path)
            if (receipt != retained or receipt.get("active") is not True or receipt.get("revoked_at") is not None
                    or timestamp(receipt.get("expires_at", "")) <= datetime.now(UTC)):
                raise ValueError("original source selection is expired, revoked or changed")
            return verify_proof(reader.get(path + "/source-revocation-observations/" + quote(operation_id, safe="")),
                                case, retained, actor, operation_id, baseline_at, started)

        before = current()
        paths = ["results/" + quote(case["result_id"], safe=""), "runs/" + quote(case["run_id"], safe="") + "/source-manifest"]
        baseline = prior.get("clients", {}).get("denial_baseline")
        if baseline is not None:
            if baseline.get("reader") != actor:
                raise ValueError("source denial baseline belongs to another owner")
            paths = verify_baseline(baseline, case, native["result"], native["source_manifest"])
        else:
            paths.append(paths[0] + "/findings")
        paths.append(paths[0] + "/publication-status")
        task_id = prior.get("clients", {}).get("usage", {}).get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                raise ValueError("retained later task identity is invalid")
            paths.append(paths[0] + "/design-usage/" + quote(task_id, safe=""))
        denied_reads(client, reader, paths)
        after = current()
        if stable_proof(before) != stable_proof(after) or human(client) != actor:
            raise ValueError("source transition or reader changed during denied-content readback")
    return {"source": "authenticated-product-api", "scope": case["scope"], "run_id": case["run_id"],
            "result_id": case["result_id"], "result_revision": case["result_revision"], "reader": actor,
            "proof": after, "proof_sha256": digest(after), "denied_paths": paths,
            "baseline_digest": canonical(baseline) if baseline else None, "baseline_at": baseline_at.isoformat(),
            "status": 404, "derived_content_bytes": 0, "source_revocation_observed": True,
            "provider_effect_count": None, "execution_authorized": False}


def attach(prior, observed):
    result = deepcopy(prior)
    result["source_revocation"] = observed
    # Keep foreign-reader denial separate; revocation is observed with the same
    # owner who had source access before the real Product unlink.
    if "clients" in result:
        result["clients"]["source_revocation"] = observed
    return result


def stable_evaluations(evaluations):
    """Compare durable facts while retaining fresh read timestamps separately."""
    result = deepcopy(evaluations)
    for item in result:
        item.get("reuse_interval", {}).get("native_audit", {}).pop("observed_at", None)
        for interval in item.get("periodic", {}).get("intervals", []):
            interval["document"].get("native_audit", {}).pop("observed_at", None)
        for observation in item.get("qualification_observations", {}).get("items", []):
            observation.get("observation", {}).pop("observed_at", None)
            observation.pop("observation_sha256", None)
        for observation in (item.get("source_revocation"), item.get("clients", {}).get("source_revocation")):
            if observation is not None:
                observation["proof"].pop("observed_at", None)
                observation.pop("proof_sha256", None)
    return result


def observe(selections, cases, prior, operations, baseline_at, driver):
    from l5_services.adapters.well_architected.qualification import emit_native_case, read_selected_review
    import qualification_well_architected_cleanup as cleanup
    import qualification_well_architected_clients as clients
    import qualification_well_architected_observations as experiments
    import qualification_well_architected_portability as portability

    selected_operations(operations, selections)
    if len(selections) != 2 or len(cases) != 2 or len(prior) != 2:
        raise ValueError("staged revocation requires both original release cases")
    evaluations = []
    for selection, case, previous in zip(selections, cases, prior, strict=True):
        if selection["run_id"] in operations:
            evaluations.append(attach(previous, collect(selection, case, previous, operations[selection["run_id"]],
                                                        baseline_at, driver.product_connection)))
            continue
        if "qualification_cleanup" in selection:
            current = cleanup.observe_case(selection, case, previous, driver)
            if stable_evaluations([current]) != stable_evaluations([previous]):
                raise ValueError("the other case's owned cleanup or retained facts changed during source revocation")
            evaluations.append(current)
            continue
        session = driver.ProductNativeSession(selection)
        with driver.product_connection(selection) as client:
            observed = read_selected_review(ProductReader(client, selection["scope"]), session, selection)
        if digest(emit_native_case(observed)) != digest(case):
            raise ValueError("the other native case changed during staged source revocation")
        current = {"run_id": selection["run_id"], "scope": case["result"]["scope"],
                   "result_revision": case["result_revision"], "runtime": session.observation.get("runtime", {})}
        if "qualification_clients" in selection:
            current["clients"] = clients.collect(selection, driver.product_connection, read_only=True)
        if "qualification_experiments" in selection:
            current["qualification_observations"] = experiments.collect(selection, driver.product_connection)
        if "qualification_portability" in selection:
            current["portability"] = portability.collect(selection, case, session, driver.product_connection)
        if stable_evaluations([current]) != stable_evaluations([previous]):
            raise ValueError("the other case's authority or retained facts changed during source revocation")
        evaluations.append(current)
    return evaluations
