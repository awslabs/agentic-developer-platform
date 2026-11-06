"""Observe real result/query/source artifacts before testing denied readers.

WA's generic artifact routes are intentionally unsupported. Their unconditional
404 cannot prove confidentiality. Only admitted source snapshots with successful
owner byte reads are included here; no arbitrary paths or URLs are selected.
"""

from datetime import UTC, datetime
from hashlib import sha256
import re
from urllib.parse import quote

from l5_services.source_manifest import _verify_artifact
from qualification_well_architected_reuse import canonical, timestamp


def result_path(selection):
    return "results/" + quote(selection["result_id"], safe="")


def snapshot_paths(selection, manifest):
    sources = selection.get("sources")
    snapshots = manifest.get("source_snapshot_ids")
    if (not isinstance(sources, list) or not sources or manifest.get("sources") != sources
            or manifest.get("run_id") != selection["run_id"]
            or any(manifest.get("scope", {}).get(key) != selection["scope"][key]
                   for key in ("tenant_id", "product_id", "project_id"))
            or not isinstance(snapshots, list) or len(snapshots) != len(sources)
            or any(not isinstance(value, str) or not value for value in snapshots)
            or len(set(snapshots)) != len(snapshots)):
        raise ValueError("denial baseline differs from the admitted source snapshots")
    root = "runs/" + quote(selection["run_id"], safe="") + "/source-snapshots/"
    return [root + quote(value, safe="") for value in snapshots]


def verify_baseline(baseline, selection, result, native_manifest=None):
    """Validate the exact successful surfaces retained by the live collector."""
    manifest = baseline["manifest"]
    roots = snapshot_paths(selection, manifest)
    if (baseline.get("source") != "authenticated-product-api"
            or baseline.get("result_digest") != canonical(result)
            or baseline.get("manifest_digest") != canonical(manifest)
            or manifest.get("archive") != result.get("manifest_ref")
            or manifest.get("architecture", {}).get("files") != result.get("coverage")
            or native_manifest is not None and manifest != native_manifest
            or baseline.get("query", {}).get("path") != result_path(selection) + "/findings"
            or not re.fullmatch(r"[a-f0-9]{64}", baseline.get("query", {}).get("sha256", ""))
            or len(baseline.get("snapshots", [])) != len(roots)):
        raise ValueError("denial baseline is not the selected result/query/source revision")
    paths = [result_path(selection), result_path(selection) + "/findings",
             "runs/" + quote(selection["run_id"], safe="") + "/source-manifest"]
    for index, (root, item) in enumerate(zip(roots, baseline["snapshots"], strict=True)):
        receipt = item.get("receipt", {})
        if (receipt.get("snapshot_id") != manifest["source_snapshot_ids"][index]
                or receipt.get("source_ref") != selection["sources"][index]
                or item.get("receipt_digest") != canonical(receipt)):
            raise ValueError("denial artifact differs from the admitted snapshot")
        paths.append(root)
        for part, field in (("manifest", "file_manifest"), ("content", "content")):
            reference = receipt.get(field, {})
            observation = item.get(part, {})
            if (not isinstance(reference.get("artifact_id"), str)
                    or not reference["artifact_id"].startswith("products/source-snapshots/")
                    or not reference.get("version") or reference["version"] in {"null", "latest", "HEAD"}
                    or not re.fullmatch(r"[a-f0-9]{64}", reference.get("sha256", ""))
                    or type(reference.get("bytes")) is not int or not 0 <= reference["bytes"] <= 64 * 1024 * 1024
                    or observation != {"path": root + "/" + part, "sha256": reference["sha256"], "bytes": reference["bytes"]}):
                raise ValueError("denial artifact needs a successful pinned owner byte read")
            paths.append(root + "/" + part)
    return paths


def collect_baseline(product, selection, result, actor):
    query_path = result_path(selection) + "/findings"
    query = product.get(query_path)
    if not isinstance(query, dict) or not isinstance(query.get("items"), list):
        raise ValueError("the selected result query is unavailable to its owner")
    manifest = product.get("runs/" + quote(selection["run_id"], safe="") + "/source-manifest")
    roots = snapshot_paths(selection, manifest)
    baseline = {"source": "authenticated-product-api", "reader": actor, "result_digest": canonical(result),
                "manifest": manifest, "manifest_digest": canonical(manifest),
                "query": {"path": query_path, "sha256": canonical(query)}, "snapshots": []}
    for root in roots:
        receipt = product.get(root)
        if timestamp(receipt.get("expires_at", "")) <= datetime.now(UTC):
            raise ValueError("denial baseline source snapshot has expired")
        item = {"receipt": receipt, "receipt_digest": canonical(receipt)}
        for part, field in (("manifest", "file_manifest"), ("content", "content")):
            response = product.client.get(product.path + "/" + root + "/" + part)
            response.raise_for_status()
            _verify_artifact(response.content, receipt[field], 64 * 1024 * 1024)
            item[part] = {"path": root + "/" + part, "sha256": sha256(response.content).hexdigest(), "bytes": len(response.content)}
        baseline["snapshots"].append(item)
    verify_baseline(baseline, selection, result)
    # Close the read interval against changes to the selected result and manifest.
    if (canonical(product.get(result_path(selection))) != baseline["result_digest"]
            or product.get("runs/" + quote(selection["run_id"], safe="") + "/source-manifest") != manifest):
        raise ValueError("selected result/source changed during denial baseline collection")
    return baseline


def denied_reads(client, product, paths):
    for path in paths:
        response = client.get(product.path + "/" + path)
        if response.status_code != 404 or response.json() != {"detail": {"code": "not_found"}}:
            raise ValueError("selected reader received content or an unverified denial")

