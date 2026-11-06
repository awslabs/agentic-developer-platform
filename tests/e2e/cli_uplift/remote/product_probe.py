"""Read the deployed Product API schema over the serving pods' admitted path."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re

import common
from tests.e2e.product_onboarding.product_profile_api import PROTECTED_SELECTIONS


PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
OPERATIONS = {
    ("/v1/products", "post"): "createProduct",
    ("/v1/products/{product_id}/projects/{project_id}", "get"): "getProfile",
    ("/v1/products/{product_id}/projects/{project_id}", "patch"): "updateProduct",
    ("/v1/products/{product_id}/projects/{project_id}/changes", "get"): "listChanges",
    (
        "/v1/products/{product_id}/projects/{project_id}/revisions/{revision}",
        "get",
    ): "getRevision",
}

# A mounted app path appended to the reviewed gateway base, exactly like every
# other live read in this directory: the configured base already carries the
# external `/api` prefix CloudFront strips, so this arrives at the serving
# process as `/openapi.json`.
SCHEMA_PATH = "/openapi.json"


# WHY THIS NO LONGER CONNECTS TO `http://<pod-ip>:8080/openapi.json`
#
# That read could never arrive, and no attempt budget could have made it. The
# last live attempt (#8243, deploy run 39937936160) refused with
# `attempt1/10s=URLError/timeout, attempt2/30s=URLError/timeout,
# attempt3/30s=URLError/timeout, tcp=timeout` -- a DROPPED SYN from the
# evaluator's own instance, which is neither a refusal nor a slow process. That
# is the documented behaviour of a pod-selector ingress policy for an
# off-cluster client (the TLS-X02 "door5" precedent, #7870), and this instance
# is off-cluster by design: it is a disposable guest that holds no cluster
# credential. Opening that policy, or handing this instance a cluster or
# protected-edge credential, widens the boundary the evaluation exists to
# respect, so neither is a repair.
#
# A serving pod admits two inbound paths: the gateway origin, and the protected
# per-pod runtime-proof edge that only the orchestrator's reader role may call
# (`ADP_RUNTIME_PROOF_ROLE_ARNS`). So this stage reads the schema the
# deployment actually serves through the origin -- the same path the journey
# below mutates through, and the only path a client of the product has either
# -- once per inventoried serving pod, and refuses unless every read serves the
# selected Product API and every read serves the same one.
#
# Per-POD process facts are not this receipt's claim and must not be read as
# one. They are taken where they are admitted: `product_native.observe` proofs
# every serving pod over the protected edge (`KubernetesRuntime.gateway` ->
# `schema(pod, product=True)`), each answer nonce-bound and verified by the pod
# itself against the release's migration head, CLI command-manifest digest and
# Product admissions; `product_execution` re-reads the whole serving inventory
# after the journey, and `product_d01_release` matches those per-pod facts
# against the signed release artifact.


def _has_operation(paths, path, method, operation):
    methods = paths.get(path)
    entry = methods.get(method) if isinstance(methods, dict) else None
    return isinstance(entry, dict) and entry.get("operationId") == operation


def _render(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical(value):
    """The meaning of a schema fragment, independent of how it was listed.

    Two processes built from one image serve one contract, but not always one
    byte sequence: a FastAPI document materializes `required`, `enum` and
    `anyOf` members from unordered Python collections, so their listing order
    is a per-process accident. Order is not part of an OpenAPI document's
    meaning, so it must not decide whether two serving pods agree.
    """
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return sorted((_canonical(item) for item in value), key=_render)
    return value


def _digest(value):
    return hashlib.sha256(_render(_canonical(value)).encode()).hexdigest()


def _selected(schema):
    """The served document reduced to the surface this acceptance selects.

    Every part is keyed by one of OUR names, so a disagreement between two
    reads is reported precisely without publishing anything the peer sent, and
    so incidental per-request or per-process text cannot masquerade as a fleet
    serving two different products.
    """
    components = schema.get("components")
    bodies = components.get("schemas") if isinstance(components, dict) else None
    parts = {
        "openapi": schema["openapi"],
        "info.title": schema["info"]["title"],
        "components.schemas": bodies,
    }
    for path, method in OPERATIONS:
        parts[f"{method.upper()} {path}"] = schema["paths"][path][method]
    return {name: _digest(part) for name, part in parts.items()}


def _schema(config, token):
    """One served selected surface, or a refusal that names why it never came.

    `common.api` is the harness's own authenticated read: it publishes the path
    and the HTTP status or the exception CLASS and nothing the peer sent, which
    is the same bargain the direct pod read kept and the difference between
    "unavailable" and a refusal the next repair can act on.
    """
    _status, schema = common.api(config, SCHEMA_PATH, token, expect=(200,))
    common.require(
        isinstance(schema, dict)
        and isinstance(schema.get("openapi"), str)
        and schema["openapi"].startswith("3.")
        and isinstance(schema.get("info"), dict)
        and schema["info"].get("title") == "BedrockGateway"
        and isinstance(schema.get("paths"), dict)
        and all(
            _has_operation(schema["paths"], path, method, operation)
            for (path, method), operation in OPERATIONS.items()
        ),
        "The deployed release does not serve the selected Product API schema",
    )
    return _selected(schema)


def execute(config, evidence):
    inventory = config.get("product_inventory")
    expected = config.get("expected_revision")
    common.require(
        config.get("product_cases") in PROTECTED_SELECTIONS
        and isinstance(expected, str)
        and re.fullmatch(r"[a-f0-9]{40}", expected)
        and isinstance(inventory, dict)
        and inventory.get("source_revision") == expected
        and isinstance(inventory.get("digest"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", inventory["digest"])
        and isinstance(inventory.get("pods"), list)
        and 0 < len(inventory["pods"]) <= 100,
        "Product process probe needs a pinned complete serving-pod inventory",
    )
    seen_uids, seen_addresses = set(), set()
    pods = inventory["pods"]
    for pod in pods:
        address = pod.get("address") if isinstance(pod, dict) else None
        try:
            parsed = ipaddress.IPv4Address(address)
        except (ValueError, ipaddress.AddressValueError):
            raise common.RemoteError(
                "Product process probe has no private pod address"
            ) from None
        common.require(
            isinstance(pod.get("uid"), str)
            and bool(pod["uid"])
            and pod["uid"] not in seen_uids
            and address not in seen_addresses
            and pod.get("digest") == inventory["digest"]
            and any(parsed in network for network in PRIVATE_NETWORKS),
            "Product process probe has an unowned or duplicate pod",
        )
        seen_uids.add(pod["uid"])
        seen_addresses.add(address)
    token = common.load_session(config)["access_token"]
    # One read per inventoried serving pod. The origin distributes reads across
    # the whole serving fleet, so N independent reads that return one selected
    # surface is the strongest statement this admitted path can make about it,
    # and a pod serving a different Product API breaks the agreement below.
    #
    # The agreement is over the SELECTED surface, canonically, and it names the
    # parts that differ. Digesting the whole served document byte-for-byte cost
    # a live run (deploy run 39950515367, "Product serving pods disagree on
    # their served API schema"): a document carries per-request and per-process
    # text that is not the product's contract, and nothing in that refusal said
    # which. A disagreement inside the selection is still a refusal here.
    #
    # `pods` keeps naming the pinned inventory this receipt is bound to, beside
    # the digest of the selected surface the deployment served while those pods
    # -- and only those pods -- were the fleet. See the note above: it is not a
    # per-pod process measurement, and the per-pod proofs live elsewhere.
    reads = [_schema(config, token) for _pod in pods]
    differing = []
    for name, digest in reads[0].items():
        if any(read[name] != digest for read in reads[1:]):
            differing.append(name)
    differing.sort()
    common.require(
        not differing,
        "Product serving pods disagree on the selected API schema: "
        + ", ".join(differing),
    )
    schemas = [
        {"uid": pod["uid"], "schema_sha256": _digest(reads[index])}
        for index, pod in enumerate(pods)
    ]
    evidence.update(
        success=True,
        stage="complete",
        source_revision=expected,
        product_cases=config["product_cases"],
        schema_source="gateway_origin",
        schema_path=SCHEMA_PATH,
        pods=schemas,
    )


if __name__ == "__main__":
    raise SystemExit(common.run_script(execute))
