"""Independent readback assertions for the Security live qualification."""

import hashlib
import json

from urllib.parse import quote


def product_sources(run, client, *, max_bytes=64 * 1024 * 1024):
    """Read pinned, retained snapshots through the current Product authorization."""
    scope = run["scope"]
    base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/security/runs/"
            f"{quote(run['run_id'], safe='')}")

    def read_json(path):
        response = client.get(path, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        return response.json()

    manifest = read_json(base + "/source-manifest")
    sources = run["input"]["sources"]
    snapshots = run["admission"]["source_snapshots"]
    ids = manifest.get("source_snapshot_ids")
    if (manifest.get("run_id") != run["run_id"] or manifest.get("scope") != scope
            or manifest.get("sources") != sources or len(sources) != len(snapshots)
            or not isinstance(ids, list) or len(ids) != len(sources) or len(set(ids)) != len(ids)
            or any(snapshot.get("source_ref") != source or snapshot.get("snapshot_id") != identifier
                   for source, snapshot, identifier in zip(sources, snapshots, ids, strict=True))):
        raise ValueError("Product source manifest differs from selected run")

    def read_bytes(path, artifact):
        expected = artifact["bytes"]
        if type(expected) is not int or not 0 <= expected <= max_bytes:
            raise ValueError("Product source exceeds private readback bound")
        with client.stream("GET", path, headers={"Cache-Control": "no-cache"}) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > expected:
                    raise ValueError("Product source exceeds pinned size")
            if (len(content) != expected or hashlib.sha256(content).hexdigest() != artifact["sha256"]
                    or response.headers.get("ETag") != f'"{artifact["sha256"]}"'):
                raise ValueError("Product source differs from pinned snapshot")
            return bytes(content)

    verified = []
    for source, snapshot, identifier in zip(sources, snapshots, ids, strict=True):
        path = base + "/source-snapshots/" + quote(identifier, safe="")
        if read_json(path) != snapshot:
            raise ValueError("Product snapshot receipt differs from selected run")
        inventory = read_bytes(path + "/manifest", snapshot["file_manifest"])
        if json.loads(inventory).get("source_ref") != source:
            raise ValueError("Product source inventory differs from selected revision")
        verified.append({"source_ref": source, "content": read_bytes(path + "/content", snapshot["content"])})
    return verified


def verify_setup(selection, observations, attempts, read_api):
    """Compare actual client observations with separately authorized API reads.

    The caller must retain raw CLI, served UI and MCP attempts privately;
    read_api must fetch the deployed API rather than replay client responses.
    """
    if not 1 <= len(attempts) <= selection["max_attempts"]:
        raise AssertionError("setup attempts exceed the authorized bound or are missing")
    for operation, effect_id in selection["effects"].items():
        effects = {attempt["effect_id"] for attempt in attempts
                   if attempt["operation"] == operation and attempt.get("effect_id")}
        if effects != {effect_id}:
            raise AssertionError(f"{operation} missing or produced multiple effects")
    if {attempt["operation"] for attempt in attempts} != set(selection["effects"]):
        raise AssertionError("unselected setup operation attempted")
    if {attempt["surface"] for attempt in attempts} != {"cli", "ui", "mcp"}:
        raise AssertionError("not all selected client surfaces were exercised")
    scope = selection["scope"]
    base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/security")
    configuration = read_api(base + "/configuration")
    binding = read_api(base + "/bindings/" + quote(selection["binding_id"], safe=""))
    run = read_api(base + "/runs/" + quote(selection["run_id"], safe=""))

    for name, record in (("configuration", configuration), ("binding", binding), ("run", run)):
        if any(record["scope"].get(field) != scope[field]
               for field in ("tenant_id", "product_id", "project_id")):
            raise AssertionError(f"{name} has a different tenant or Product scope")
    if configuration["configuration_id"] != selection["configuration_id"]:
        raise AssertionError("configuration identity differs from selection")
    if (binding["binding_id"] != selection["binding_id"]
            or binding["configuration_id"] != configuration["configuration_id"]
            or binding["connection_ref"] != selection["connection_ref"]):
        raise AssertionError("binding or selected account differs from configuration")
    if binding["provider_identity"] != selection["provider_identity"]:
        raise AssertionError("native account identity differs from selected provider")
    if binding["state"] != selection["binding_state"] or binding["readiness"] != selection["readiness"]:
        raise AssertionError("binding readiness differs from independently selected state")
    if (run["run_id"] != selection["run_id"]
            or run["admission"]["configuration_id"] != configuration["configuration_id"]
            or run["admission"]["configuration_revision"] != configuration["revision"]
            or run["input"]["binding_id"] != binding["binding_id"]
            or run["input"]["sources"] != selection["sources"]):
        raise AssertionError("run does not pin selected configuration, binding and sources")

    for surface in ("cli", "ui", "mcp"):
        client = observations[surface]
        for name, independent, identity in (("configuration", configuration, "configuration_id"),
                                            ("binding", binding, "binding_id"), ("run", run, "run_id")):
            observed = client[name]
            details = {"configuration": ("settings",),
                       "binding": ("connection_ref", "provider_identity", "readiness", "state"),
                       "run": ("input", "admission")}
            for field in (identity, "revision", "scope", *details[name]):
                if observed[field] != independent[field]:
                    raise AssertionError(f"{surface} {name} {field} differs from API readback")
    return {"configuration_revision": configuration["revision"], "binding_revision": binding["revision"],
            "run_revision": run["revision"], "run_id": run["run_id"], "attempt_count": len(attempts)}


def api_reader(client):
    """Read through a separately authorized HTTP client, never client-side cached results."""
    def read(path):
        response = client.get(path, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        return response.json()

    return read


def read_assessment(scope, run_id, result_id, read_api):
    """Fetch run, result and every finding page from the Product API."""
    base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/security")
    run = read_api(base + "/runs/" + quote(run_id, safe=""))
    result_path = base + "/results/" + quote(result_id, safe="")
    result = read_api(result_path)
    if run["run_id"] != run_id or result["result_id"] != result_id or result["run_id"] != run_id:
        raise AssertionError("result readback is not linked to the selected run")
    findings = []
    cursor = None
    seen = set()
    while True:
        path = result_path + "/findings?limit=100"
        if cursor is not None:
            path += "&cursor=" + quote(cursor, safe="")
        page = read_api(path)
        findings.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
        if cursor in seen or len(seen) >= 100 or len(findings) >= 10000:
            raise AssertionError("finding pagination did not finish within the bound")
        seen.add(cursor)
    if len(findings) != result["finding_count"]:
        raise AssertionError("finding pages differ from persisted count")
    return run, result, findings


def verify_trigger_result(receipts, run, result, native_jobs):
    """Require duplicate event deliveries to resolve to one persisted/native job."""
    if len(receipts) < 2:
        raise AssertionError("duplicate event delivery was not exercised")
    event_id = receipts[0]["event_id"]
    if (any(receipt["event_id"] != event_id or receipt["run_id"] != run["run_id"]
            or receipt["state"] != "admitted" for receipt in receipts)
            or run["admission"].get("event_id") != event_id
            or result["run_id"] != run["run_id"]):
        raise AssertionError("event deliveries disagree with persisted run or result")
    if len(native_jobs) != 1 or native_jobs[0]["native_job_ref"] != result["native_job_ref"]:
        raise AssertionError("duplicate event created an extra native job")
    return {"event_id": event_id, "run_id": run["run_id"], "native_job_ref": result["native_job_ref"]}


def read_context_evidence(scope, run_id, result_id, publication_id, read_api, context_client, query_agent):
    """Read Product and Context independently, then invoke the later agent."""
    run, result, findings = read_assessment(scope, run_id, result_id, read_api)
    response = context_client.get("/internal/v1/context/l5-contributions/" + quote(publication_id, safe=""),
                                  headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    return run, result, findings, response.json(), query_agent(scope["product_id"])
