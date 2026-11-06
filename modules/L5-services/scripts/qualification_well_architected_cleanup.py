"""Explicit owned cleanup, followed by independent absence and retained-result reads."""

from copy import deepcopy
from hashlib import sha256
import re
import time
from urllib.parse import quote

from l5_services.adapters.well_architected.qualification import ProductReader, emit_native_case, read_selected_review
from qualification_well_architected_bootstrap import wait_for
from qualification_well_architected_journal import Journal, encoded
import qualification_well_architected_clients as clients


def validate(selection):
    option = selection.get("qualification_cleanup")
    if option is None:
        return
    if (not isinstance(option, dict) or set(option) != {"owned_cleanup_confirmed", "idempotency_key", "timeout_seconds", "poll_seconds"}
            or option["owned_cleanup_confirmed"] is not True
            or not isinstance(option["idempotency_key"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", option["idempotency_key"])
            or type(option["timeout_seconds"]) is not int or not 30 <= option["timeout_seconds"] <= 3600
            or type(option["poll_seconds"]) is not int or not 1 <= option["poll_seconds"] <= 60):
        raise ValueError("cleanup requires explicit bounded owned-resource selection")


def execute(selections, directory, context, connection, *, clock=time.monotonic, sleep=time.sleep):
    for selection in selections:
        validate(selection)
    journal = Journal(directory / "cleanup-journal", context)
    for selection in selections:
        option = selection.get("qualification_cleanup")
        if option is None:
            continue
        with connection(selection) as client:
            reader = ProductReader(client, selection["scope"])
            binding = reader.get("bindings/" + quote(selection["binding_id"], safe=""))
            if (binding.get("ownership") != "adp_created" or binding.get("binding_id") != selection["binding_id"]
                    or any(binding.get("scope", {}).get(field) != selection["scope"][field]
                           for field in ("tenant_id", "product_id", "project_id"))):
                raise ValueError("cleanup selection is not owned by the selected Product")
            if binding.get("state") == "ready":
                if binding.get("revision") != selection["binding_revision"]:
                    raise ValueError("cleanup binding changed since native verification")
            else:
                stem = sha256(encoded({"scope": selection["scope"], "path": "binding-operations"})).hexdigest()
                if journal.read(stem + "-intent.json") is None:
                    raise ValueError("cleanup already changed without this release's retained intent")
            operation = journal.post(reader, selection, "binding-operations", {
                "action": "cleanup", "configuration_revision": selection["configuration_revision"],
                "binding_id": selection["binding_id"], "owned_cleanup_confirmed": True,
            }, option["idempotency_key"], headers={"If-Match": f'"{selection["binding_revision"]}"'})
            operation_id = operation["operation_id"]
            completed = wait_for(reader, "binding-operations/" + quote(operation_id, safe=""), operation,
                clock() + option["timeout_seconds"], option["poll_seconds"], identifier="operation_id", expected=operation_id,
                clock=clock, sleep=sleep)
            if completed.get("binding_id") != selection["binding_id"] or completed.get("action") != "cleanup":
                raise ValueError("cleanup operation belongs to another binding")


def observe(selections, cases, prior_evaluations, driver):
    """Recheck every result/source; native absence replaces deleted-Profile reads.

    The original independently read native evidence remains a private immutable
    artifact in this release. Its selected result must still equal the current
    Product result byte-for-byte in canonical JSON after cleanup.
    """
    if len(selections) != 2 or len(cases) != 2 or len(prior_evaluations) != 2:
        raise ValueError("cleanup verification requires both original selected cases")
    return [observe_case(selection, case, prior, driver)
            for selection, case, prior in zip(selections, cases, prior_evaluations, strict=True)]


def observe_case(selection, case, prior, driver):
    """Read one original case, including exact owned cleanup when selected."""
    evidence = deepcopy(prior)
    with driver.product_connection(selection) as client:
        product = ProductReader(client, selection["scope"])
        if "qualification_cleanup" in selection:
            current = product.get("runs/" + quote(selection["run_id"], safe="") + "/cleanup-observations")
            if (current.get("schema_version") != 1
                    or any(current.get(field) != selection[field] for field in (
                        "run_id", "run_revision", "configuration_revision", "binding_id", "result_id", "result_revision", "provider_identity"))
                    or current.get("scope") != {field: selection["scope"][field] for field in ("tenant_id", "product_id", "project_id")}
                    or type(current.get("binding_revision")) is not int or current["binding_revision"] <= selection["binding_revision"]
                    or current.get("native_job_ref") != case["native_job_ref"]
                    or current.get("source_manifest") != case["source_manifest"]
                    or current.get("cleanup", {}).get("owned_absent") is not True
                    or current["cleanup"].get("source") != "independent-native-read"
                    or current["cleanup"].get("result_retained") is not True
                    or current["cleanup"].get("native_profile_arn") != case["native_job_ref"]["profile_arn"]):
                raise ValueError("cleanup readback differs from selected native evidence")
            result = product.get("results/" + quote(selection["result_id"], safe=""))
            if clients.canonical(result) != clients.canonical(case["result"]):
                raise ValueError("cleanup did not retain the original result revision")
            driver.verify_retained_sources(product, selection, case["source_manifest"])
            evidence["runtime"] = current["runtime"]
            evidence["cleanup"] = current["cleanup"]
        else:
            native = driver.ProductNativeSession(selection)
            observed = read_selected_review(product, native, selection)
            if clients.canonical(emit_native_case(observed)) != clients.canonical(case):
                raise ValueError("retained native project changed during owned cleanup")
            evidence["runtime"] = native.observation.get("runtime", {})
            evidence["adopted_profile_unchanged"] = observed["product"]["binding"].get("ownership") == "customer_linked"
    if "qualification_clients" in selection:
        evidence["clients"] = clients.collect(selection, driver.product_connection, read_only=True)
    if "qualification_periodic" in selection:
        import qualification_well_architected_periodic as periodic
        evidence["periodic"] = periodic.collect(selection, driver.product_connection)
    return evidence
