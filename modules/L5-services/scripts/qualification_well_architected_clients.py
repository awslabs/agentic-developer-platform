"""Bounded first-party WA readback; never start native work or use a browser."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

from l5_services.adapters.well_architected.qualification import ProductReader
from qualification_well_architected_reuse import collect_usage, human
from qualification_well_architected_denials import collect_baseline, denied_reads, verify_baseline


def canonical(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pinned_file(value, digest, *, executable=False):
    path = Path(value)
    if (not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents))
            or not path.is_file() or path.stat().st_mode & 0o022
            or path.stat().st_uid != os.getuid() or path.stat().st_size > 8 * 1024 * 1024
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or sha256(path.read_bytes()).hexdigest() != digest
            or executable and not os.access(path, os.X_OK)):
        raise ValueError("installed client pin is unavailable")
    return path


def cli_result(selection, config, *, run=subprocess.run):
    """Call the selected installed CLI after resolving its actual deployment."""
    if set(config) != {"executable", "files_sha256", "adp_home", "deployment"}:
        raise ValueError("CLI selection must pin installed files and deployment")
    pins = config["files_sha256"]
    if not isinstance(pins, dict) or set(pins) != {"adp", "adp_deployments.py", "adp-service.py", "adp_common.py", "bg-cognito-auth.sh"}:
        raise ValueError("CLI selection must pin every installed read/auth helper")
    executable = pinned_file(config["executable"], pins["adp"], executable=True)
    if executable.name != "adp" or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", config["deployment"]):
        raise ValueError("only the installed adp client may run")
    for name, digest in pins.items():
        pinned_file(str(executable.with_name(name)), digest)
    resolver = executable.with_name("adp_deployments.py")
    home = Path(config["adp_home"])
    if (not home.is_absolute() or any(part.is_symlink() for part in (home, *home.parents))
            or not home.is_dir() or home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o077):
        raise ValueError("CLI needs a private selected deployment directory")
    environment = {key: value for key, value in os.environ.items()
                   if key in {"HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "NO_PROXY", "no_proxy"}}
    environment["ADP_HOME"] = str(home)

    def invoke(command):
        result = run(command, capture_output=True, timeout=30, env=environment, check=False)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError("installed CLI readback unavailable")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("installed CLI readback invalid")
        return value

    resolved = invoke([sys.executable, str(resolver), "resolve", "--deployment", config["deployment"], "--format", "json"])
    if (resolved.get("deployment") != config["deployment"]
            or resolved.get("gateway_url", "").rstrip("/") != selection["gateway_url"].rstrip("/") + "/api"):
        raise ValueError("installed CLI points to another gateway")
    scope = selection["scope"]
    value = invoke([str(executable), "--deployment", config["deployment"], "service", "result", "show",
                    "--service", "well_architected", "--product", scope["product_id"], "--project", scope["project_id"],
                    "--result-id", selection["result_id"], "--json"])
    if value.get("status") != "ok" or not isinstance(value.get("detail"), dict):
        raise ValueError("installed CLI did not return its selected result")
    return value["detail"]


def collect(selection, connection, *, run=subprocess.run, read_only=False):
    """Read existing Product, CLI and MCP results, plus explicit later-task use.

    Only the optional design_usage operation writes an idempotent consumer usage
    receipt; it cannot create a provider generation. Its task must already be
    authorized by the Product endpoint. No credential or caller-supplied URL is
    forwarded to a peer service.
    """
    selected = selection.get("qualification_clients", {})
    if not isinstance(selected, dict) or set(selected) - {"cli", "mcp_token_env", "design_task_id", "denied_token_env", "retained_reuse"}:
        raise ValueError("unsupported qualification client selection")
    if "retained_reuse" in selected and (selected["retained_reuse"] is not True or "design_task_id" not in selected):
        raise ValueError("retained reuse needs an explicit later task and existing owner consent")
    observed = {"scope": selection["scope"], "run_id": selection["run_id"], "result_id": selection["result_id"],
                "result_revision": selection["result_revision"], "clients": {}, "limitations": ["ui_not_exercised", "read_journey_only"]}
    path = "results/" + quote(selection["result_id"], safe="")
    with connection(selection) as client:
        product = ProductReader(client, selection["scope"])
        owner = human(client) if "denied_token_env" in selected else None
        if owner is not None and owner["org_id"] != selection["scope"]["tenant_id"]:
            raise ValueError("selected owner belongs to another tenant")
        result = product.get(path)
        if (result.get("result_id") != selection["result_id"] or result.get("revision") != selection["result_revision"]
                or result.get("run_id") != selection["run_id"]
                or any(result.get("scope", {}).get(field) != selection["scope"][field]
                       for field in ("tenant_id", "product_id", "project_id"))):
            raise ValueError("Product client returned a foreign or changed result")
        observed["clients"]["api"] = canonical(result)
        observed["publication"] = product.get(path + "/publication-status")
        if (observed["publication"].get("result_id") != result["result_id"]
                or observed["publication"].get("revision") != result["revision"]):
            raise ValueError("publication status belongs to another result")
        if "design_task_id" in selected:
            task = selected["design_task_id"]
            if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task) or task == selection["run_id"]:
                raise ValueError("later design task must be distinct and explicit")
            observed["usage"] = collect_usage(client, product, path, selection, result,
                                                observed["publication"], read_only=read_only)
        if owner is not None and selection.get("sources") and not selected.get("retained_reuse"):
            observed["denial_baseline"] = collect_baseline(product, selection, result, owner)
        if owner is not None and human(client) != owner:
            raise ValueError("owner identity changed during selected readback")
    if "cli" in selected:
        observed["clients"]["cli"] = canonical(cli_result(selection, selected["cli"], run=run))
    if "mcp_token_env" in selected:
        with connection({**selection, "token_env": selected["mcp_token_env"]}) as client:
            response = client.post("/api/v1/l5/mcp/tools/call", json={"tool": "l5___get_result", "arguments": {
                "product_id": selection["scope"]["product_id"], "project_id": selection["scope"]["project_id"],
                "service_id": "well_architected", "result_id": selection["result_id"]}})
            response.raise_for_status()
            payload = response.json()
            if payload.get("isError") or payload.get("structuredContent", {}).get("http_status") != 200:
                raise ValueError("authenticated MCP result unavailable")
            observed["clients"]["mcp"] = canonical(payload["structuredContent"]["data"])
    if any(value != observed["clients"]["api"] for value in observed["clients"].values()):
        raise ValueError("installed clients disagree on result content")
    if "denied_token_env" in selected:
        if selected["denied_token_env"] == selection["token_env"]:
            raise ValueError("denied reader must be independently selected")
        with connection({**selection, "token_env": selected["denied_token_env"]}) as client:
            reader = human(client)
            if reader == owner:
                raise ValueError("denied reader resolves to the selected owner")
            baseline = observed.get("denial_baseline")
            paths = verify_baseline(baseline, selection, result) if baseline else [path]
            denied_reads(client, ProductReader(client, selection["scope"]), paths)
            if human(client) != reader:
                raise ValueError("denied reader identity changed during readback")
            observed["denial"] = {"status": 404, "derived_content_bytes": 0, "reader": reader,
                                  "kind": "selected_reader", "source_revocation_observed": False,
                                  "source": "authenticated-product-api", "denied_paths": paths,
                                  "baseline_digest": canonical(baseline) if baseline else None}
    return observed
