"""Fail-closed qualification of a source-bound Product native inquiry."""

import re
from hashlib import sha256


def matches_scope(observed, selected):
    return isinstance(observed, dict) and all(observed.get(field) == selected[field]
                                            for field in ("tenant_id", "product_id", "project_id"))


def evaluate(manifest, run, result, answer, clients, publication):
    scope = manifest["scope"]
    native = result.get("native")
    if not isinstance(native, dict) or not isinstance(result.get("billing_scope"), dict):
        raise ValueError("native Product result is missing or outside the pinned revision and scope")
    artifact = native.get("answer_artifact")
    oracle = result.get("oracle")
    if not isinstance(oracle, dict):
        raise ValueError("independent, complete CE page evidence is unavailable")
    if (not matches_scope(run.get("scope"), scope) or run.get("state") not in {"succeeded", "partial"}
            or run.get("input") != manifest["input"]
            or (run.get("admission") or {}).get("native_agent_ref") != manifest["native_agent_ref"]
            or not isinstance(result.get("revision"), int) or result["revision"] < 1
            or result.get("kind") != "finops_cost_inquiry" or not matches_scope(result.get("scope"), scope)
            or result.get("run_id") != run.get("run_id")
            or result.get("query") != manifest["input"]["query"]
            or any(result.get("billing_scope", {}).get(field) != manifest["billing_scope"][field]
                   for field in ("view_ref", "account_ids"))
            or result.get("attribution") != "product_verified"
            or native.get("status") != "answered" or not native.get("conversation_id")
            or sum(isinstance(native.get(field), str) and bool(native[field])
                   for field in ("turn_id", "turn_locator")) != 1
            or not isinstance(artifact, dict)
            or artifact != (run.get("provider_readback_ref") or {}).get("answer_artifact")):
        raise ValueError("native Product result is missing or outside the pinned revision and scope")
    text = answer.get("text") if isinstance(answer, dict) else None
    if (not isinstance(text, str) or not text.strip()
            or answer.get("artifact") != artifact or answer.get("run_id") != run["run_id"]
            or answer.get("result_id") != result["result_id"] or answer.get("revision") != result["revision"]
            or sha256(text.encode("utf-8")).hexdigest() != artifact.get("sha256")):
        raise ValueError("original native answer content is not source-bound")
    pages = oracle.get("pages")
    if (not oracle.get("collection_id") or not isinstance(pages, list) or not pages
            or not isinstance(oracle.get("measure"), dict)
            or oracle["measure"].get("coverage") != "complete"
            or not isinstance(oracle.get("query_hash"), str)
            or re.fullmatch(r"[a-f0-9]{64}", oracle["query_hash"]) is None
            or any(not isinstance(page, dict) or set(page) != {"artifact_id", "version", "sha256"}
                   or page["artifact_id"] != (f"l5/finops/work/{run['run_id']}/billing/"
                                                f"{oracle['collection_id']}/{page['sha256']}.json")
                   or not isinstance(page["version"], str) or not page["version"]
                   or not isinstance(page["sha256"], str)
                   or re.fullmatch(r"[a-f0-9]{64}", page["sha256"]) is None for page in pages)
            or len({page["artifact_id"] for page in pages}) != len(pages)):
        raise ValueError("independent, complete CE page evidence is unavailable")
    if (not isinstance(clients, dict) or set(clients) != {"cli", "mcp", "ui"}
            or any(clients[surface] != result for surface in ("cli", "mcp"))
            or clients["ui"] != {"result_id": result["result_id"], "revision": result["revision"],
                                 "native_artifact": artifact, "billing_pages": pages, "answer_text": text}
            or not isinstance(publication, dict)
            or publication.get("result_id") != result["result_id"]
            or publication.get("revision") != result["revision"]
            or not isinstance(publication.get("consumers"), list) or len(publication["consumers"]) != 2
            or any(not isinstance(item, dict) for item in publication["consumers"])
            or {item.get("destination") for item in publication["consumers"]} != {
                "product-context", "engineering-insights"}
            or any(item.get("state") != "published" or not item.get("receipt_id")
                   or item.get("projection_verified_on_ack") is not True
                   for item in publication["consumers"])):
        raise ValueError("independent Product client or publication observations differ")
    return {"status": "schema_verified", "release_sha": manifest["release_sha"],
            "scope": scope, "run_id": run["run_id"], "result_id": result["result_id"],
            "revision": result["revision"], "native_artifact": artifact,
            "billing_collection_id": oracle["collection_id"], "billing_pages": pages}
