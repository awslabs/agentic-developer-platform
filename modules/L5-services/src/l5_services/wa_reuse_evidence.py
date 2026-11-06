"""Compare installed client and peer receipts for one retained WA result."""

from l5_services.installed_release_probe import digest, require


def verify_wa_reuse(case, fixture, safety):
    require(isinstance(case, dict) and case.get("source") == "authenticated-runtime"
            and isinstance(case.get("evidence_ref"), str) and case["evidence_ref"],
            "wa_reuse_private_case_missing")
    result = case.get("result")
    safety_result = safety.get("result") if isinstance(safety, dict) else None
    scope = case.get("scope")
    require(isinstance(scope, dict)
            and all(isinstance(scope.get(field), str) and scope[field]
                    for field in ("tenant_id", "product_id", "project_id"))
            and isinstance(result, dict) and isinstance(safety_result, dict)
            and result.get("source") == "installed-api"
            and isinstance(result.get("evidence_ref"), str) and result["evidence_ref"]
            and result.get("result_id") == safety_result.get("result_id")
            and result.get("revision") == safety_result.get("revision")
            and result.get("run_id") == safety.get("recovery", {}).get("run_id")
            and result.get("scope") == scope
            and all(scope.get(field) == safety.get("scope", {}).get(field)
                    for field in ("tenant_id", "project_id"))
            and result.get("sha256") == safety_result.get("after_sha256")
            and digest(result.get("sha256")), "wa_reuse_result_changed")
    reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "source_connection_id": "l5-well-architected",
                 "record_type": "well_architected_result", "record_id": result["result_id"],
                 "record_revision": str(result["revision"])}
    clients = case.get("clients")
    expected = {"api": fixture.gateway_image_digest, "frontend": fixture.frontend_asset_sha256,
                "cli": fixture.cli_manifest_sha256, "mcp": case.get("mcp_artifact_sha256")}
    require(digest(expected["mcp"]) and isinstance(clients, list) and len(clients) == 4
            and all(isinstance(client, dict) for client in clients)
            and {client.get("client") for client in clients} == set(expected),
            "wa_reuse_clients_missing")
    refs = set()
    for client in clients:
        require(client.get("source") == "installed-client"
                and isinstance(client.get("evidence_ref"), str) and client["evidence_ref"]
                and client.get("source_revision") == fixture.source_revision
                and client.get("artifact_sha256") == expected[client["client"]]
                and client.get("status") == 200
                and client.get("run_id") == result["run_id"]
                and client.get("result_ref") == reference
                and client.get("result_sha256") == result["sha256"]
                and set(client.get("journey", [])) >= {
                    "configure", "check", "run", "read_result", "publication_status"},
                "wa_reuse_client_parity_unverified")
        refs.add(client["evidence_ref"])
    require(len(refs) == len(clients), "wa_reuse_clients_missing")
    publications = case.get("publications")
    destinations = {"product-context", "engineering-insights"}
    require(isinstance(publications, list) and len(publications) == 2
            and all(isinstance(item, dict) for item in publications)
            and {item.get("destination") for item in publications} == destinations,
            "wa_reuse_consumers_missing")
    by_destination = {item["destination"]: item for item in publications}
    for publication in publications:
        require(publication.get("source") == "installed-consumer"
                and isinstance(publication.get("evidence_ref"), str) and publication["evidence_ref"]
                and publication.get("state") == "published"
                and publication.get("scope") == scope
                and publication.get("schema_version") == "1.0"
                and publication.get("record_refs") == [reference]
                and publication.get("projection_verified_on_ack") is True
                and digest(publication.get("contribution_hash"))
                and all(isinstance(publication.get(field), str) and publication[field]
                        for field in ("publication_id", "receipt_id", "consumer_revision")),
                "wa_reuse_consumer_ack_unverified")
    require(len({row["publication_id"] for row in publications}) == 2
            and len({row["receipt_id"] for row in publications}) == 2,
            "wa_reuse_consumers_missing")
    usage = case.get("later_usage")
    require(isinstance(usage, dict) and usage.get("source") == "installed-api"
            and isinstance(usage.get("evidence_ref"), str) and usage["evidence_ref"]
            and isinstance(usage.get("task_id"), str) and usage["task_id"]
            and usage["task_id"] != result["run_id"]
            and isinstance(usage.get("actor_id"), str) and usage["actor_id"]
            and usage.get("result_ref") == reference and usage.get("status") == 200
            and usage.get("result_sha256") == result["sha256"]
            and usage.get("citation") == f"well_architected_result:{result['result_id']}@{result['revision']}"
            and type(usage.get("provider_calls_before")) is int
            and usage["provider_calls_before"] == safety.get("recovery", {}).get("start_call_count")
            and usage.get("provider_calls_after") == usage["provider_calls_before"],
            "wa_reuse_later_task_unverified")
    for destination, field in (("product-context", "retrieval_receipt"),
                               ("engineering-insights", "usage_receipt")):
        receipt = usage.get(field)
        require(isinstance(receipt, dict) and isinstance(receipt.get("receipt_id"), str)
                and receipt["receipt_id"] and receipt.get("task_id") == usage["task_id"]
                and receipt.get("actor_id") == usage["actor_id"]
                and receipt.get("result_ref") == reference
                and receipt.get("citation") == usage["citation"]
                and receipt.get("contribution_id") == by_destination[destination]["publication_id"]
                and (destination != "engineering-insights" or receipt.get("retrieval_receipt_id")
                     == usage["retrieval_receipt"]["receipt_id"]),
                "wa_reuse_later_receipt_unverified")
    denials = case.get("denials")
    require(isinstance(denials, list) and len(denials) == 2
            and all(isinstance(denial, dict) for denial in denials)
            and {denial.get("kind") for denial in denials} == {"foreign_actor", "revoked_source"},
            "wa_reuse_denials_missing")
    for denial in denials:
        request_scope = denial.get("request_scope")
        require(denial.get("source") == "installed-client"
                and isinstance(denial.get("evidence_ref"), str) and denial["evidence_ref"]
                and isinstance(denial.get("actor_id"), str) and denial["actor_id"]
                and denial.get("result_ref") == reference
                and denial.get("status") == 404
                and denial.get("private_counts_visible") is False
                and denial.get("derived_content_bytes") == 0
                and (denial["kind"] != "foreign_actor" or denial["actor_id"] != usage["actor_id"]
                     and isinstance(request_scope, dict)
                     and (request_scope.get("tenant_id"), request_scope.get("project_id"))
                     != (scope["tenant_id"], scope["project_id"]))
                and (denial["kind"] != "revoked_source" or request_scope == scope
                     and denial.get("source_revoked") is True),
                "wa_reuse_denied_content_unverified")
    require(denials[0]["evidence_ref"] != denials[1]["evidence_ref"],
            "wa_reuse_denials_missing")
    return {"result_ref": reference, "task_id": usage["task_id"]}
