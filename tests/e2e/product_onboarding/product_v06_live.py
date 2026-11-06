"""Deployed issue-8256 AC-01 source producer: a real client run, not a report reader.

The reviewed SCN-01 check in ``test_v06.py`` validates a protected attempt
report. Nothing in the repository produced that report, so 8256/AC-01 had no
deployed test producer and the fail-closed completion inventory refused the
regression attempt before any live case could run.

This module performs the deployed work behind the SCN-01 report against the
existing production contract in ``modules/gateway/src/products/source_routes.py``
and the served client in ``modules/gateway/cli/adp-product.py``:

* ``GET .../sources`` first enumerates ``items[].connection_ref``; a real
  authorized connection pair ``connection_kind={github_installation,credential}``
  plus ``connection_id`` is then queried and its ``items[].resources`` inspected.
  Only resources matching the identities authorized for this run are selected.
* ``POST .../bindings`` sends a flat ``BindingInput`` (``kind``, ``provider``,
  ``connection_ref``, ``namespace``, ``native_id`` and the environment metadata
  an environment selection requires). Binding creation, verification and
  unlinking each send ``Idempotency-Key`` and ``If-Profile-Revision``, with the
  profile revision refreshed from the Product itself and the ``Profile-Revision``
  response headers retained as evidence.
* ``GET .../bindings`` reads ``{"items": [...], "next_cursor": ...}``, as do the
  served UI and the installed ``adp product source-bindings`` command.
* ``DELETE .../bindings/{id}`` answers an unlinked tombstone that stays in the
  readback, so cleanup establishes owned status ``unlinked`` for every binding
  this run created instead of expecting it to disappear.

The reviewed validator is applied to the assembled report before it is written,
so an incomplete, shared or uncleaned observation fails the producer instead of
being published. Deployment access is injected as named ports, so a capability
that is not authorized for the run raises ``SourcePrerequisite`` naming the
missing endpoint or fixture instead of fabricating an observation.

``run_source_status`` adds the deployed SCN-02 work behind 8256/AC-02: one owned
environment binding is observed before its pinned expiry, again while coverage
stays partial, exactly at the ``stale_after`` boundary the deployed binding
itself published, and then through both restricted phases - each time on the
paired sources read, the bindings read, the profile, the pinned historical
revision and the onboarding session, with the installed client executed and the
served UI read for every phase. Deployed UTC instants are recorded in the one
representation the reviewed clock accepts, without moving the instant.

``run_source_recovery`` adds the deployed SCN-03 work behind 8256/AC-03: a
bounded ``limit=1`` resource read is interrupted and resumed on the
``resource_cursor`` the deployed read itself published, one verification is
replayed under a single idempotency key for the same operation receipt, and only
the binding this run owns is unlinked while a peer binding and the confirmed
Product values are read back intact - again on the paired API read, the
installed client, the served UI and a read-only provider readback.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from tests.e2e.product_onboarding.test_v06 import (
    LIVE_SURFACES,
    RECOVERY_PHASES,
    STATUS_ARTIFACTS,
    collect_pinned_source_reuse,
    pinned_source_recovery_case,
    pinned_source_status_case,
    source_status_clock,
    verify_live_source_recovery,
    verify_live_source_reuse,
    verify_live_source_status,
)

CLIENT_SURFACES = ("api", "installed_cli", "browser")
SELECTION_KINDS = ("repository", "environment")
CONNECTION_KINDS = ("github_installation", "credential")
BINDING_ID_CHARACTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
CLI_FILES = ("adp", "adp-product.py")
MAX_PAGES = 5

PREREQUISITES = {
    "discover": (
        "Deployed source discovery is unavailable: GET /api/v1/products/{product}/projects/{project}/sources"
        " must answer for the run-owned Project before issue-8256 can be observed"
    ),
    "authorized_identities": (
        "Issue-8256 needs the source identities this run is authorized to select, so no arbitrary discovered "
        "resource is bound: publish PRODUCT_V06_REPOSITORY_NATIVE_ID (or GITHUB_REPOSITORY_ID), "
        "PRODUCT_V06_ENVIRONMENT_ACCOUNT_ID (or ACCOUNT_ID), PRODUCT_V06_ENVIRONMENT_REGION (or "
        "PRODUCT_TARGET_REGION) and PRODUCT_V06_ENVIRONMENT_ROLE_REF; the protected browser fixture "
        "configuration publishes none of them"
    ),
    "repository_selection": (
        "No authorized GitHub repository resource for this run was returned by GET /api/v1/products/{product}"
        "/projects/{project}/sources?connection_kind=github_installation&connection_id=... in items[].resources"
    ),
    "environment_selection": (
        "No authorized AWS environment resource for this run was returned by GET /api/v1/products/{product}"
        "/projects/{project}/sources?connection_kind=credential&connection_id=... in items[].resources, so no "
        "verified_environment can be produced"
    ),
    "installed_cli": (
        "The run has no digest-verified installed Product client: the served CLI installation must publish "
        "PRODUCT_RESUME_CLI_PATH and PRODUCT_RESUME_CLI_HASHES covering adp and adp-product.py before "
        "'adp product sources' and 'adp product source-bindings' can be executed"
    ),
    "browser_sources": (
        "The served UI exposes no run-observable sources surface for this Project, so the browser surface of "
        "issue-8256 cannot be observed"
    ),
    "create_binding": (
        "POST /api/v1/products/{product}/projects/{project}/bindings is unavailable for the run-owned Project"
    ),
    "verify_binding": (
        "POST /api/v1/products/{product}/projects/{project}/bindings/{binding}/verify is unavailable for the "
        "run-owned Project"
    ),
    "list_bindings": (
        "GET /api/v1/products/{product}/projects/{project}/bindings is unavailable, so bindings cannot be "
        "read back independently"
    ),
    "unlink_binding": (
        "DELETE /api/v1/products/{product}/projects/{project}/bindings/{binding} is unavailable, so the run "
        "cannot own its own cleanup"
    ),
    "read_surface": "An independent client surface for issue-8256 is not available to this run",
    "read_provider": (
        "Issue-8256 needs an independent read-only provider readback of the selected identities (authorized "
        "GitHub GET /repositories/{id} with GITHUB_TOKEN or GH_TOKEN, and sts:GetCallerIdentity in the "
        "authorized account and role); no such independent provider credential is published to this run"
    ),
    "provider_calls": "The independent provider readback does not record its own calls",
    "artifact": "The run has no private capture directory for durable surface artifacts",
    "status_surfaces": (
        "Issue-8256 SCN-02 needs every project surface of one status phase: the paired GET /api/v1/products/"
        "{product}/projects/{project}/sources, GET .../bindings, GET <project>, GET <project>/revisions/"
        "{revision} and GET <project>/onboarding-sessions/{session} are not all available to this run"
    ),
    "status_clock": (
        "SCN-02 requires a pinned UTC observation clock: the deployed binding readback published no "
        "observed_at and stale_after that resolve to a single UTC instant"
    ),
    "await_expiry": (
        "SCN-02 must observe the pinned expiry boundary on the deployed clock, so the run has to wait for "
        "its own verified binding to go stale: publish PRODUCT_V06_STATUS_WAIT_SECONDS covering the "
        "deployed verification lifetime"
    ),
    "status_restriction": (
        "SCN-02 needs the restricted phases of this run's own source connection: the fixture must withdraw "
        "and restore the verification evidence of the run-owned AWS environment connection while it stays "
        "visible, because a connection that stops being authorized answers HTTP 404 on the paired sources "
        "read instead of restricted coverage; no withdraw_source_authorization and "
        "restore_source_authorization capability is published to this run"
    ),
    "provider_identity": (
        "SCN-02 must report the independent provider identity (account_id and role_ref) it actually read "
        "back, so revoked phases can be checked against it"
    ),
    "recovery_pages": (
        "SCN-03 needs an interruptible deployed resource page: GET /api/v1/products/{product}/projects/"
        "{project}/sources?connection_kind=github_installation&connection_id=...&limit=1 must answer one "
        "resource plus a resource_cursor that resumes on a second, different authorized repository; the "
        "connections authorized for this run publish a single resource page"
    ),
    "recovery_isolation": (
        "SCN-03 must observe that owned cleanup preserves a binding it does not unlink, so the run-owned "
        "Project has to start with no pre-existing source bindings and allow one peer repository binding "
        "to be created beside the owned one"
    ),
    "recovery_values": (
        "SCN-03 must prove the confirmed Product values survive owned cleanup, but GET /api/v1/products/"
        "{product}/projects/{project} published no confirmed product or project values to compare"
    ),
    "recovery_surfaces": (
        "SCN-03 needs every independent surface of one recovery phase: the paged GET .../sources, "
        "GET .../bindings, the served 'adp product sources --limit 1 --resource-cursor' client and the "
        "served UI bindings view are not all available to this run"
    ),
    "repository_provider": (
        "SCN-03 needs a read-only independent readback of both paged repository identities (authorized "
        "GitHub GET /repositories/{id} with GITHUB_TOKEN or GH_TOKEN, and no other provider call); no "
        "such independent provider credential is published to this run"
    ),
    "verify_receipt": (
        "SCN-03 must replay POST /api/v1/products/{product}/projects/{project}/bindings/{binding}/verify "
        "with one idempotency key and read the same operation receipt back; the deployed verification "
        "published no operation identifier to replay"
    ),
}


class SourcePrerequisite(RuntimeError):
    """An authorized deployed capability for issue-8256 is not published to this run."""


def utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def port(ports, name):
    """Return a published port, or refuse with the exact missing endpoint or fixture."""
    value = ports.get(name) if isinstance(ports, dict) else None
    if not callable(value):
        raise SourcePrerequisite(PREREQUISITES.get(name, "Deployed capability " + str(name) + " is unavailable"))
    return value


def checked_ref(value, surface):
    if not isinstance(value, str) or not value:
        raise ValueError("The " + str(surface) + " surface produced no durable artifact reference")
    return value


def checked_binding_id(binding):
    identifier = binding.get("id") if isinstance(binding, dict) else binding
    if (not isinstance(identifier, str) or not identifier or len(identifier) > 64
            or set(identifier) - BINDING_ID_CHARACTERS):
        raise ValueError("Deployed source binding readback has no bounded identifier")
    return identifier


def idempotency_key(run_id, suffix):
    key = str(run_id) + "-" + suffix
    if not IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError("The run cannot form a bounded idempotency key for " + suffix)
    return key


def pinned_identities(environ=None):
    """The source identities this run is authorized to select, never an arbitrary resource."""
    environ = os.environ if environ is None else environ
    identities = {
        "repository_native_id": environ.get("PRODUCT_V06_REPOSITORY_NATIVE_ID") or environ.get("GITHUB_REPOSITORY_ID"),
        "environment_namespace": environ.get("PRODUCT_V06_ENVIRONMENT_ACCOUNT_ID") or environ.get("ACCOUNT_ID"),
        "environment_native_id": environ.get("PRODUCT_V06_ENVIRONMENT_REGION") or environ.get("PRODUCT_TARGET_REGION"),
        "environment_role_ref": environ.get("PRODUCT_V06_ENVIRONMENT_ROLE_REF"),
    }
    if not all(isinstance(value, str) and value for value in identities.values()):
        raise SourcePrerequisite(PREREQUISITES["authorized_identities"])
    return identities


def require_selections(selections):
    """Refuse to continue unless both selections are complete deployed BindingInput values."""
    if not isinstance(selections, dict) or set(selections) != set(SELECTION_KINDS):
        raise SourcePrerequisite(PREREQUISITES["environment_selection"])
    for kind in SELECTION_KINDS:
        selection = selections[kind]
        reference = selection.get("connection_ref") if isinstance(selection, dict) else None
        if (not isinstance(selection, dict) or selection.get("kind") != kind
                or not isinstance(reference, dict) or reference.get("kind") not in CONNECTION_KINDS
                or not isinstance(reference.get("id"), str) or not reference["id"]
                or not all(isinstance(selection.get(field), str) and selection[field]
                           for field in ("provider", "namespace", "native_id"))):
            raise SourcePrerequisite(PREREQUISITES[kind + "_selection"])
    environment = selections["environment"].get("environment")
    if (not isinstance(environment, dict) or not isinstance(environment.get("name"), str)
            or not environment["name"] or environment.get("environment_class") != "development"
            or environment.get("production") is not False):
        raise SourcePrerequisite(PREREQUISITES["environment_selection"])
    return selections


def surface_views(views, surface):
    """Keep a client surface from reporting a source it did not actually read back."""
    if not isinstance(views, dict):
        raise ValueError("The " + str(surface) + " surface produced no source readback")
    for kind in SELECTION_KINDS:
        view = views.get(kind)
        if not isinstance(view, dict) or view.get("kind") != kind:
            raise ValueError("The " + str(surface) + " surface did not read back the " + kind + " source")
    return views


def protected_report(path, payload):
    """Write a run-owned report that only this run can read."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Protected source report must not be a symlink")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    path.chmod(0o600)
    return str(path)


def retire_owned(ports, owned, run_id, source_revision, provider_before):
    """Unlink exactly what this run created and establish each owned tombstone."""
    owned = sorted(owned)
    unlink = port(ports, "unlink_binding")
    receipts = {}
    for binding_id in owned:
        receipts[binding_id] = unlink(binding_id)
    readback = {}
    for view in port(ports, "list_bindings")():
        if isinstance(view, dict) and isinstance(view.get("id"), str):
            readback[view["id"]] = view
    statuses = {
        binding_id: {
            "receipt": (receipts.get(binding_id) or {}).get("status"),
            "readback": (readback.get(binding_id) or {}).get("status"),
        }
        for binding_id in owned
    }
    tombstoned = [binding_id for binding_id in owned
                  if statuses[binding_id]["receipt"] == "unlinked" and statuses[binding_id]["readback"] == "unlinked"]
    provider_after = port(ports, "read_provider")()
    return {
        "run_id": run_id,
        "source_revision": source_revision,
        "unlinked_binding_ids": owned,
        "tombstoned_binding_ids": tombstoned,
        "remaining_owned_binding_ids": [binding_id for binding_id in owned if binding_id not in tombstoned],
        "owned_binding_status": statuses,
        "independent_provider_unchanged": provider_after == provider_before,
        "cleanup_attempts": len(owned),
        "observed_at": utc_now(),
    }


def run_source_reuse(ports, *, run_id, tmp_path, report_path):
    """SCN-01: reuse selected sources on every independent surface, then retire them."""
    pinned = collect_pinned_source_reuse(tmp_path, run_id)
    source_revision, requirements_revision = pinned["source_revision"], pinned["requirements_revision"]
    selections = require_selections(port(ports, "discover")())
    provider_before = port(ports, "read_provider")()
    owned, bindings, observations = [], {}, {}
    try:
        for kind in SELECTION_KINDS:
            binding = port(ports, "create_binding")(selections[kind])
            owned.append(checked_binding_id(binding))
            bindings[kind] = port(ports, "verify_binding")(binding)
        owned.sort()
        for surface in CLIENT_SURFACES:
            views = surface_views(port(ports, "read_surface")(surface, bindings), surface)
            observations[surface] = {
                "status": "passed",
                "artifact_ref": checked_ref(views.get("artifact_ref"), surface),
                "run_id": run_id,
                "source_revision": source_revision,
                "repository": views["repository"],
                "environment": views["environment"],
            }
        provider_after = port(ports, "read_provider")()
        calls = list(port(ports, "provider_calls")())
        observations["independent_provider"] = {
            "status": "passed",
            "artifact_ref": checked_ref(port(ports, "artifact")("independent-provider", {
                "run_id": run_id, "source_revision": source_revision, "calls": calls,
                "before": provider_before, "after": provider_after,
            }), "independent_provider"),
            "run_id": run_id,
            "source_revision": source_revision,
            "calls": calls,
            "before": provider_before,
            "after": provider_after,
        }
    finally:
        receipt = retire_owned(ports, owned, run_id, source_revision, provider_before)
    report = {
        "run_id": run_id,
        "source_revision": source_revision,
        "requirements_revision": requirements_revision,
        "owned_binding_ids": sorted(owned),
        "observations": observations,
        "cleanup": receipt,
    }
    case = collect_pinned_source_reuse(tmp_path, run_id, sorted(owned))
    if not verify_live_source_reuse(case, report):
        raise ValueError("The deployed source reuse report was not accepted by the reviewed SCN-01 check")
    return protected_report(report_path, report)


def project_path(fixture):
    return ("/api/v1/products/" + quote(fixture["product_id"], safe="")
            + "/projects/" + quote(fixture["project_id"], safe=""))


def owner_headers(owner, **extra):
    return {"Authorization": "Bearer " + owner["access_token"], **extra}


def api_call(request, owner, method, url, *, body=None, headers=None, expected=(200, 201, 202)):
    """One deployed call, returning its JSON body and its Profile-Revision header."""
    options = {"headers": owner_headers(owner, **(headers or {})), "timeout": 30_000}
    if body is not None:
        options["data"] = body
        options["headers"]["Content-Type"] = "application/json"
    response = getattr(request, method)(url, **options)
    if response.status not in expected:
        raise ValueError("The deployed source API answered HTTP " + str(response.status)
                         + " for " + method.upper() + " " + url.rpartition("/api/")[2])
    revision = response.headers.get("profile-revision")
    return ({} if response.status == 204 else response.json()), revision


def api_json(request, owner, method, url, *, body=None, headers=None, expected=(200, 201, 202)):
    return api_call(request, owner, method, url, body=body, headers=headers, expected=expected)[0]


def flatten_binding(view):
    """Present a deployed binding as the surface observation the reviewed check reads."""
    if not isinstance(view, dict):
        raise ValueError("Deployed binding readback is not an object")
    selection = view.get("selection") if isinstance(view.get("selection"), dict) else {}
    observation = {key: value for key, value in view.items() if key != "selection"}
    for key, value in selection.items():
        observation.setdefault(key, value)
    return observation


def owned_views(items, bindings):
    """Keep every surface to the bindings this run owns, matched by their own IDs."""
    kinds = {view["id"]: kind for kind, view in bindings.items() if isinstance(view.get("id"), str)}
    result = {}
    for item in items or []:
        flattened = flatten_binding(item)
        kind = kinds.get(flattened.get("id"))
        if kind is not None:
            result[kind] = flattened
    return result


def connection_pairs(listing):
    """Enumerate the real authorized connections the first sources page returned."""
    if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
        raise ValueError("Deployed source discovery did not answer with items")
    pairs = []
    for item in listing["items"]:
        reference = item.get("connection_ref") if isinstance(item, dict) else None
        if (not isinstance(reference, dict) or reference.get("kind") not in CONNECTION_KINDS
                or not isinstance(reference.get("id"), str) or not reference["id"]):
            raise ValueError("Deployed source discovery returned a connection without an authorized reference")
        pairs.append({"kind": reference["kind"], "id": reference["id"], "provider": item.get("provider"),
                      "capabilities": list(item.get("capabilities") or ())})
    return pairs


def selected_resource(payload, kind, accepted):
    """Inspect items[].resources for the one resource this run is authorized to select."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Deployed connection readback did not answer with items")
    for item in payload["items"]:
        for resource in (item.get("resources") or []) if isinstance(item, dict) else []:
            if isinstance(resource, dict) and resource.get("kind") == kind and accepted(resource):
                return item, resource
    return None, None


def github_repository(url, token):
    import urllib.request

    request = urllib.request.Request(url, headers={  # noqa: S310 - fixed https host
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read().decode("utf-8"))


def caller_identity():
    import boto3

    return boto3.client("sts").get_caller_identity()


def role_name(value):
    text = str(value or "")
    for marker in ("assumed-role/", "role/"):
        if marker in text:
            return text.split(marker, 1)[1].split("/")[0]
    return ""


def independent_provider_readback(discovered, calls, identities, *, environ=None, fetch=None, identity=None):
    """Read the selected identities back from the real providers, read-only."""
    environ = os.environ if environ is None else environ
    native_id = (discovered.get("repository") or {}).get("native_id") or identities["repository_native_id"]
    token = environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN")
    if not native_id or not token:
        raise SourcePrerequisite(PREREQUISITES["read_provider"])
    url = "https://api.github.com/repositories/" + quote(str(native_id), safe="")
    calls.append("GET " + url)
    repository = (fetch or github_repository)(url, token)
    try:
        observed = {
            "id": repository["id"], "owner": {"id": repository["owner"]["id"]},
            "updated_at": repository["updated_at"], "name": repository.get("name"),
            "default_branch": repository.get("default_branch"), "visibility": repository.get("visibility"),
        }
    except (KeyError, TypeError) as error:
        raise ValueError("The independent GitHub readback returned no repository identity") from error
    caller = (identity or caller_identity)()
    calls.append("sts:GetCallerIdentity")
    account, arn = (caller or {}).get("Account"), (caller or {}).get("Arn")
    role = identities["environment_role_ref"]
    if (account != identities["environment_namespace"] or not role_name(arn)
            or role_name(arn) != role_name(role)):
        raise SourcePrerequisite(PREREQUISITES["read_provider"])
    return {
        "repository": observed,
        "environment": {"account_id": account, "region": identities["environment_native_id"],
                        "role_ref": role, "caller_arn": arn, "caller_user_id": (caller or {}).get("UserId")},
    }


def installed_cli_runner(provider, fixture, environ, record=None):
    """Run the served, digest-verified installed Product client for this run."""
    path = environ.get("PRODUCT_RESUME_CLI_PATH")
    try:
        checksums = json.loads(environ.get("PRODUCT_RESUME_CLI_HASHES") or "null")
    except ValueError:
        checksums = None
    if (not path or not isinstance(checksums, dict)
            or not all(isinstance(checksums.get(name), str) and checksums[name] for name in CLI_FILES)):
        raise SourcePrerequisite(PREREQUISITES["installed_cli"])
    from tests.e2e.new_ui.test_product_resume import installed_cli

    def run(arguments):
        return installed_cli(path, checksums, fixture["owner"], provider.base_url, arguments,
                             command="product", record=record)

    return run


def build_source_ports(provider, fixture, directory, *, page=None, environ=None, cli=None, fetch=None, identity=None):
    """Wire the deployed capabilities this run can authorize; omit the rest by name."""
    from tests.e2e.product_onboarding.product_v02_record import private_json

    environ = os.environ if environ is None else environ
    base = provider.base_url + project_path(fixture)
    owner, directory = fixture["owner"], Path(directory)
    record = fixture.get("evidence_record")
    request = record.request(provider.request) if record is not None else provider.request
    calls, discovered, revisions = [], {}, []

    def artifact(name, payload):
        if not isinstance(payload, (dict, list)) or not payload:
            raise ValueError("A durable artifact must record the data the surface returned")
        return private_json(directory / (fixture["run_id"] + "-v06-" + name + ".json"), payload)

    def bindings_url(binding_id=None, suffix=""):
        if binding_id is None:
            return base + "/bindings"
        return base + "/bindings/" + quote(binding_id, safe="") + suffix

    def profile_revision():
        """Refresh the current profile revision before any mutation."""
        body, header = api_call(request, owner, "get", base, expected=(200,))
        revision = body.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("The deployed Product profile published no current revision")
        revisions.append({"call": "GET profile", "profile_revision": header, "revision": revision})
        return revision

    def mutation_headers(suffix):
        return {"Idempotency-Key": idempotency_key(fixture["run_id"], suffix),
                "If-Profile-Revision": str(profile_revision())}

    def sources(connection=None, resource_cursor=None):
        query = {"limit": 50}
        if connection is not None:
            query["connection_kind"] = connection["kind"]
            query["connection_id"] = connection["id"]
        if resource_cursor is not None:
            query["resource_cursor"] = resource_cursor
        return api_json(request, owner, "get", base + "/sources?" + urlencode(query), expected=(200,))

    def discover():
        identities = pinned_identities(environ)
        pairs = connection_pairs(sources())
        selections = {}
        for connection in pairs:
            capabilities = connection["capabilities"]
            for kind in SELECTION_KINDS:
                if kind in selections or kind not in capabilities:
                    continue
                item, resource = selected_resource(
                    sources(connection), kind,
                    (lambda found: found.get("native_id") == identities["repository_native_id"]) if kind == "repository"
                    else (lambda found: found.get("namespace") == identities["environment_namespace"]
                          and found.get("native_id") == identities["environment_native_id"]),
                )
                if resource is None:
                    continue
                discovered[kind] = resource
                selection = {
                    "kind": kind,
                    "provider": item.get("provider") or connection["provider"],
                    "connection_ref": {"kind": connection["kind"], "id": connection["id"]},
                    "namespace": resource.get("namespace"),
                    "native_id": resource.get("native_id"),
                }
                if kind == "environment":
                    selection["environment"] = {
                        "name": resource.get("name") or (str(resource.get("namespace")) + " " + str(resource.get("native_id"))),
                        "environment_class": "development",
                        "production": False,
                    }
                selections[kind] = selection
        for kind in SELECTION_KINDS:
            if kind not in selections:
                raise SourcePrerequisite(PREREQUISITES[kind + "_selection"])
        return selections

    def create_binding(selection):
        body, revision = api_call(request, owner, "post", bindings_url(), body=selection,
                                  headers=mutation_headers("v06-bind-" + str(selection.get("kind"))),
                                  expected=(200, 201))
        revisions.append({"call": "POST bindings", "profile_revision": revision})
        return body

    def list_bindings():
        items, cursor = [], None
        for _ in range(MAX_PAGES):
            query = {"limit": 50}
            if cursor is not None:
                query["cursor"] = cursor
            payload = api_json(request, owner, "get", bindings_url() + "?" + urlencode(query), expected=(200,))
            page_items = payload.get("items")
            if not isinstance(page_items, list):
                raise ValueError("Deployed binding readback did not answer with items")
            items.extend(page_items)
            cursor = payload.get("next_cursor")
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor:
                raise ValueError("Deployed binding readback returned an invalid cursor")
        raise ValueError("Deployed binding readback exceeded its page bound")

    def verify_binding(binding):
        binding_id = checked_binding_id(binding)
        _, revision = api_call(request, owner, "post", bindings_url(binding_id, "/verify"),
                               headers=mutation_headers("v06-verify-" + binding_id), expected=(200, 202))
        revisions.append({"call": "POST verify", "profile_revision": revision})
        for item in list_bindings():
            view = flatten_binding(item)
            if view.get("id") == binding_id:
                return view
        raise ValueError("A verified source binding is missing from its own independent readback")

    def unlink_binding(binding_id):
        tombstone, revision = api_call(request, owner, "delete", bindings_url(binding_id),
                                       headers=mutation_headers("v06-unlink-" + binding_id),
                                       expected=(200, 202))
        revisions.append({"call": "DELETE binding", "profile_revision": revision})
        return tombstone if isinstance(tombstone, dict) else {}

    def read_api_surface(bindings):
        items = list_bindings()
        views = owned_views(items, bindings)
        return {**views, "artifact_ref": artifact("api", {"items": items, "revisions": list(revisions)})}

    def read_installed_cli_surface(bindings):
        run = cli if cli is not None else installed_cli_runner(provider, fixture, environ, record)
        selection = next(iter(discovered.values()), {})
        reference = (bindings.get("repository") or {}).get("connection_ref") or {}
        arguments = ["sources", "--product", fixture["product_id"], "--project", fixture["project_id"]]
        if reference.get("kind") in CONNECTION_KINDS and isinstance(reference.get("id"), str):
            arguments += ["--connection-kind", reference["kind"], "--connection-id", reference["id"]]
        listed = run(arguments)
        bound = run(["source-bindings", "--product", fixture["product_id"], "--project", fixture["project_id"]])
        if not isinstance(bound, dict) or not isinstance(bound.get("items"), list):
            raise ValueError("The installed Product client returned no binding items")
        views = owned_views(bound["items"], bindings)
        return {**views, "artifact_ref": artifact("installed-cli", {
            "sources": listed, "source_bindings": bound, "selected": selection})}

    def read_browser_surface(bindings):
        if page is None:
            raise SourcePrerequisite(PREREQUISITES["browser_sources"])
        from tests.e2e.chat.helpers import inject_tokens_and_navigate
        from tests.e2e.new_ui.product_navigation import open_product_section

        inject_tokens_and_navigate(page, owner, path=project_path(fixture).replace("/api/v1", ""))
        with page.expect_response(
            lambda response: "/bindings" in response.url and response.request.method == "GET"
        ) as captured:
            open_product_section(page, "Sources")
        body = captured.value.json()
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise ValueError("The served UI bindings response returned no items")
        views = owned_views(body["items"], bindings)
        return {**views, "artifact_ref": artifact("browser", body)}

    def read_surface(surface, bindings):
        readers = {"api": read_api_surface, "installed_cli": read_installed_cli_surface,
                   "browser": read_browser_surface}
        reader = readers.get(surface)
        if reader is None:
            raise SourcePrerequisite(PREREQUISITES["read_surface"])
        return reader(bindings)

    return {
        "discover": discover,
        "create_binding": create_binding,
        "verify_binding": verify_binding,
        "list_bindings": list_bindings,
        "unlink_binding": unlink_binding,
        "read_surface": read_surface,
        "read_provider": lambda: independent_provider_readback(
            discovered, calls, pinned_identities(environ), environ=environ, fetch=fetch, identity=identity),
        "provider_calls": lambda: list(calls),
        "artifact": artifact,
        "revisions": lambda: list(revisions),
        "profile_revision": profile_revision,
        "mutation_headers": mutation_headers,
    }


STATUS_SURFACE_KEYS = ("source", "binding", "profile", "revision", "session")
RESTRICTED_PHASES = ("unavailable", "revoked")
PINNED_CLOCK_KEYS = ("observed_at", "stale_after", "verified_at")
UTC_OFFSET = "+00:00"


def pinned_now():
    """This run's own observation instant in the one pinned UTC representation."""
    return datetime.now(UTC).isoformat().replace(UTC_OFFSET, "Z")


def pinned_clock(value, field):
    """Keep a deployed UTC timestamp's instant while recording it in the pinned representation.

    ``modules/gateway/src/products/source_routes.py`` stores ``observed_at`` and ``stale_after`` as
    ``datetime.now(UTC).isoformat()``, which ends in ``+00:00``; the reviewed SCN-02 clock only accepts the
    equivalent ``Z`` form. Only the representation is changed, and the normalised text has to parse back to
    exactly the same instant, so no observation is moved, rounded or invented.
    """
    if not isinstance(value, str) or not value.strip():
        raise SourcePrerequisite(PREREQUISITES["status_clock"])
    text = value.strip()
    if not text.endswith("Z") and not text.endswith(UTC_OFFSET):
        raise SourcePrerequisite(PREREQUISITES["status_clock"])
    normalized = text[: -len(UTC_OFFSET)] + "Z" if text.endswith(UTC_OFFSET) else text
    if source_status_clock(normalized) != datetime.fromisoformat(text.replace("Z", UTC_OFFSET)):
        raise ValueError("Deployed " + str(field) + " does not resolve to the pinned UTC instant")
    return normalized


def pinned_timestamps(view):
    """Record a deployed view unchanged except for the representation of its UTC instants."""
    if isinstance(view, list):
        return [pinned_timestamps(item) for item in view]
    if isinstance(view, dict):
        return {
            key: pinned_clock(value, key) if key in PINNED_CLOCK_KEYS and isinstance(value, str) and value
            else pinned_timestamps(value)
            for key, value in view.items()
        }
    return view


def provider_identity(readback):
    """The independent provider identity this run actually read back, never a configured guess."""
    environment = readback.get("environment") if isinstance(readback, dict) else None
    identity = {
        "account_id": (environment or {}).get("account_id"),
        "role_ref": (environment or {}).get("role_ref"),
    }
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise SourcePrerequisite(PREREQUISITES["provider_identity"])
    return identity


def served_bindings_view(page, owner, fixture, label):
    """Read the served UI's own bindings response for one observation, through the real page."""
    if page is None:
        raise SourcePrerequisite(PREREQUISITES["browser_sources"])
    from tests.e2e.chat.helpers import inject_tokens_and_navigate
    from tests.e2e.new_ui.product_navigation import open_product_section

    inject_tokens_and_navigate(page, owner, path=project_path(fixture).replace("/api/v1", ""))
    with page.expect_response(
        lambda response: "/bindings" in response.url and response.request.method == "GET"
    ) as captured:
        open_product_section(page, "Sources")
    body = captured.value.json()
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ValueError("The served UI bindings response returned no items in " + str(label))
    return body


def status_attempt(ports, phase, binding_id, *, run_id, source_revision, clock=None):
    """Observe one deployed status phase on every project surface, with its own durable artifacts."""
    view = port(ports, "status_surfaces")(phase, binding_id)
    if not isinstance(view, dict):
        raise ValueError("The " + str(phase) + " status phase produced no surface readback")
    artifacts = view.get("artifacts")
    if (not isinstance(artifacts, dict) or set(artifacts) != set(STATUS_ARTIFACTS)
            or any(not isinstance(ref, str) or not ref for ref in artifacts.values())
            or len(set(artifacts.values())) != len(STATUS_ARTIFACTS)):
        raise ValueError("The " + str(phase) + " status phase captured no independent artifact per surface")
    surfaces = {key: pinned_timestamps(view.get(key)) for key in STATUS_SURFACE_KEYS}
    if not isinstance(surfaces["source"], dict) or not isinstance(surfaces["binding"], dict):
        raise ValueError("The " + str(phase) + " status phase read back no source or binding view")
    if surfaces["binding"].get("id") != binding_id:
        raise ValueError("The " + str(phase) + " status phase did not read back this run's own binding")
    native_id = view.get("native_id")
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("The " + str(phase) + " status phase has no selected native identity")
    surfaces["native_id"] = native_id
    return {
        "status": "passed",
        "run_id": run_id,
        "source_revision": source_revision,
        "clock": pinned_clock(clock, "clock") if clock is not None else pinned_now(),
        "observed_instant": pinned_now(),
        "artifacts": dict(artifacts),
        "surfaces": surfaces,
    }


def run_source_status(ports, *, run_id, tmp_path, report_path):
    """SCN-02: observe one owned source through expiry and restriction, then retire it."""
    pinned = collect_pinned_source_reuse(tmp_path, run_id)
    source_revision, requirements_revision = pinned["source_revision"], pinned["requirements_revision"]
    selection = require_selections(port(ports, "discover")())["environment"]
    provider_before = port(ports, "read_provider")()
    identity = provider_identity(provider_before)
    owned, attempts = [], {}
    try:
        binding = port(ports, "create_binding")(selection)
        binding_id = checked_binding_id(binding)
        owned.append(binding_id)
        verified = port(ports, "verify_binding")(binding)
        observed_at = pinned_clock(verified.get("observed_at"), "observed_at")
        stale_after = pinned_clock(verified.get("stale_after"), "stale_after")
        for phase in ("before_expiry", "partial"):
            attempts[phase] = status_attempt(ports, phase, binding_id, run_id=run_id,
                                             source_revision=source_revision)
        port(ports, "await_expiry")(stale_after)
        attempts["at_expiry"] = status_attempt(ports, "at_expiry", binding_id, run_id=run_id,
                                               source_revision=source_revision, clock=stale_after)
        restrict, restore = port(ports, "restrict"), port(ports, "restore")
        try:
            for phase in RESTRICTED_PHASES:
                restrict(phase)
                attempts[phase] = status_attempt(ports, phase, binding_id, run_id=run_id,
                                                 source_revision=source_revision)
        finally:
            restore()
    finally:
        receipt = retire_owned(ports, owned, run_id, source_revision, provider_before)
    report = {
        "run_id": run_id,
        "source_revision": source_revision,
        "requirements_revision": requirements_revision,
        "owned_binding_ids": sorted(owned),
        "observed_at": observed_at,
        "stale_after": stale_after,
        "provider_identity": identity,
        "attempts": attempts,
        "cleanup": receipt,
    }
    case = pinned_source_status_case(source_revision, requirements_revision, run_id, observed_at, stale_after,
                                     owned_binding_ids=sorted(owned))
    if not verify_live_source_status(case, report):
        raise ValueError("The deployed source status report was not accepted by the reviewed SCN-02 check")
    return protected_report(report_path, report)


def build_status_ports(provider, fixture, directory, *, page=None, environ=None, cli=None, fetch=None,
                       identity=None, sleep=None):
    """Wire the deployed SCN-02 status surfaces on top of the SCN-01 source ports."""
    environ = os.environ if environ is None else environ
    ports = build_source_ports(provider, fixture, directory, page=page, environ=environ, cli=cli, fetch=fetch,
                               identity=identity)
    base = provider.base_url + project_path(fixture)
    owner = fixture["owner"]
    record = fixture.get("evidence_record")
    request = record.request(provider.request) if record is not None else provider.request
    artifact = ports["artifact"]
    state = {"selection": None, "revision": None, "session": None}

    def discover():
        selections = port(ports, "discover")()
        state["selection"] = selections.get("environment") if isinstance(selections, dict) else None
        return selections

    def create_binding(selection):
        binding = port(ports, "create_binding")(selection)
        revision = binding.get("revision") if isinstance(binding, dict) else None
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("The deployed binding readback published no profile revision to read back")
        state["revision"] = revision
        return binding

    def session_id():
        if state["session"] is None:
            published = fixture.get("onboarding_session_id") or environ.get("PRODUCT_V06_ONBOARDING_SESSION_ID")
            if isinstance(published, str) and published:
                state["session"] = published
            else:
                created = api_json(request, owner, "post", base + "/onboarding-sessions", body={},
                                   headers=ports["mutation_headers"]("v06-status-session"),
                                   expected=(200, 201))
                identifier = created.get("id") if isinstance(created, dict) else None
                if not isinstance(identifier, str) or not identifier:
                    raise SourcePrerequisite(PREREQUISITES["status_surfaces"])
                state["session"] = identifier
        return state["session"]

    def paired_sources():
        reference = (state["selection"] or {}).get("connection_ref") or {}
        if reference.get("kind") not in CONNECTION_KINDS or not isinstance(reference.get("id"), str):
            raise SourcePrerequisite(PREREQUISITES["environment_selection"])
        query = {"limit": 1, "connection_kind": reference["kind"], "connection_id": reference["id"]}
        body, _ = api_call(request, owner, "get", base + "/sources?" + urlencode(query), expected=(200, 404))
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise SourcePrerequisite(PREREQUISITES["status_restriction"])
        return items[0]

    def optional_progress(url):
        body, _ = api_call(request, owner, "get", url, expected=(200, 403, 404))
        return body if isinstance(body, dict) and isinstance(body.get("progress"), dict) else None

    def project_views(phase):
        source = paired_sources()
        bindings = api_json(request, owner, "get", base + "/bindings?" + urlencode({"limit": 50}), expected=(200,))
        if not isinstance(bindings.get("items"), list):
            raise SourcePrerequisite(PREREQUISITES["list_bindings"])
        profile = optional_progress(base)
        revision = optional_progress(base + "/revisions/" + quote(str(state["revision"]), safe=""))
        session = optional_progress(base + "/onboarding-sessions/" + quote(session_id(), safe=""))
        if phase not in RESTRICTED_PHASES and None in (profile, revision, session):
            raise SourcePrerequisite(PREREQUISITES["status_surfaces"])
        return {"source": source, "bindings": bindings, "profile": profile, "revision": revision,
                "session": session}

    def owned_binding(items, binding_id):
        for item in items or []:
            if isinstance(item, dict) and item.get("id") == binding_id:
                return item
        raise ValueError("The owned source binding is missing from its own status readback")

    def cli_surface(phase, binding, source):
        """Execute the served, digest-verified client and refuse a surface that disagrees with the API."""
        run = cli if cli is not None else installed_cli_runner(provider, fixture, environ, record)
        reference = (state["selection"] or {}).get("connection_ref") or {}
        if reference.get("kind") not in CONNECTION_KINDS or not isinstance(reference.get("id"), str):
            raise SourcePrerequisite(PREREQUISITES["environment_selection"])
        listed = run(["sources", "--product", fixture["product_id"], "--project", fixture["project_id"],
                      "--connection-kind", reference["kind"], "--connection-id", reference["id"]])
        bound = run(["source-bindings", "--product", fixture["product_id"], "--project", fixture["project_id"]])
        sources = listed.get("items") if isinstance(listed, dict) else None
        bindings = bound.get("items") if isinstance(bound, dict) else None
        if (not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict)
                or not isinstance(bindings, list)):
            raise ValueError("The installed Product client read back no source or binding items in " + str(phase))
        if sources[0].get("coverage") != source.get("coverage"):
            raise ValueError("The installed Product client disagrees with the API coverage in " + str(phase))
        if owned_binding(bindings, binding.get("id")).get("status") != binding.get("status"):
            raise ValueError("The installed Product client disagrees with the API binding status in " + str(phase))
        return {"sources": listed, "source_bindings": bound}

    def status_surfaces(phase, binding_id):
        views = project_views(phase)
        binding = owned_binding(views["bindings"].get("items"), binding_id)
        browser = served_bindings_view(page, owner, fixture, phase)
        observed = port(ports, "read_provider")()
        artifacts = {
            "api": artifact(str(phase) + "-api", {
                "source": views["source"], "bindings": views["bindings"], "profile": views["profile"],
                "revision": views["revision"], "session": views["session"],
                "revisions": port(ports, "revisions")(),
            }),
            "installed_cli": artifact(str(phase) + "-installed-cli", cli_surface(phase, binding, views["source"])),
            "browser": artifact(str(phase) + "-browser", browser),
            "independent_provider": artifact(str(phase) + "-independent-provider", {
                "calls": list(port(ports, "provider_calls")()), "observed": observed,
            }),
        }
        return {
            "source": views["source"], "binding": binding, "profile": views["profile"],
            "revision": views["revision"], "session": views["session"],
            "native_id": (state["selection"] or {}).get("native_id"), "artifacts": artifacts,
        }

    def await_expiry(stale_after):
        """Wait on the real deployed clock for this run's own verification to expire."""
        boundary = source_status_clock(stale_after)
        try:
            allowed = int(environ.get("PRODUCT_V06_STATUS_WAIT_SECONDS"))
        except (TypeError, ValueError):
            raise SourcePrerequisite(PREREQUISITES["await_expiry"]) from None
        pause = sleep or time.sleep
        if allowed < 1 or (boundary - datetime.now(UTC)).total_seconds() > allowed:
            raise SourcePrerequisite(PREREQUISITES["await_expiry"])
        while True:
            remaining = (boundary - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return pinned_now()
            pause(min(remaining, 15))

    def authorization_hook(name):
        hook = fixture.get(name)
        if not callable(hook):
            raise SourcePrerequisite(PREREQUISITES["status_restriction"])
        return hook

    def restrict(phase):
        if phase not in RESTRICTED_PHASES:
            raise ValueError("Unknown restricted source status phase " + str(phase))
        return authorization_hook("withdraw_source_authorization")(phase)

    def restore():
        return authorization_hook("restore_source_authorization")()

    return {**ports, "discover": discover, "create_binding": create_binding, "status_surfaces": status_surfaces,
            "await_expiry": await_expiry, "restrict": restrict, "restore": restore}


def recovery_page(payload, cursor, page_count):
    """One bounded deployed resource page, as the reviewed resource_cursor contract publishes it.

    ``modules/gateway/src/products/source_routes.py`` answers ``items[0]["resources"]`` for the paired
    connection read and publishes the next page in ``items[0]["resource_cursor"]`` (a decimal page number
    for github and gitlab), so an interrupted discovery resumes exactly where it stopped.
    """
    item = (payload.get("items") or [None])[0] if isinstance(payload, dict) else None
    resources = item.get("resources") if isinstance(item, dict) else None
    if not isinstance(resources, list) or len(resources) != 1 or not isinstance(resources[0], dict):
        raise SourcePrerequisite(PREREQUISITES["recovery_pages"])
    native_id = resources[0].get("native_id")
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("A deployed repository resource page published no native identity")
    next_cursor = item.get("resource_cursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
        raise ValueError("A deployed repository resource page published an unusable resource cursor")
    return {"requested_cursor": cursor, "resource_cursor": next_cursor, "resource_ids": [native_id],
            "page_count": page_count, "source": item}


def recovery_attempt(ports, phase, *, run_id, source_revision, detail, evidence=None):
    """Observe one deployed recovery phase on every independent surface, with its own artifacts."""
    if not isinstance(detail, dict) or not detail:
        raise ValueError("The " + str(phase) + " recovery phase reported no deployed detail")
    view = port(ports, "recovery_surfaces")(phase, detail, evidence)
    artifacts = view.get("artifacts") if isinstance(view, dict) else None
    if (not isinstance(artifacts, dict) or set(artifacts) != set(LIVE_SURFACES)
            or any(not isinstance(ref, str) or not ref for ref in artifacts.values())
            or len(set(artifacts.values())) != len(LIVE_SURFACES)):
        raise ValueError("The " + str(phase) + " recovery phase captured no independent artifact per surface")
    return {"status": "passed", "run_id": run_id, "source_revision": source_revision,
            "observed_at": pinned_now(), "artifacts": dict(artifacts), **detail}


def run_source_recovery(ports, *, run_id, tmp_path, report_path):
    """SCN-03: resume an interrupted deployed source run, replay one verification and unlink only owned.

    The peer binding is created beside the owned one so the deployed unlink can be observed preserving a
    binding it does not own, exactly as the reviewed local recovery case does. It is excluded from the
    cleanup receipt's owned set and retired by this run's own teardown, which the report records, so the
    run still leaves the Project as it found it.
    """
    pinned = collect_pinned_source_reuse(tmp_path, run_id)
    source_revision, requirements_revision = pinned["source_revision"], pinned["requirements_revision"]
    started_at = pinned_now()
    selection = require_selections(port(ports, "discover")())["repository"]
    port(ports, "require_isolation")()
    first = port(ports, "source_page")(None, 1)
    resumed = port(ports, "source_page")(first["resource_cursor"], 2)
    if (not isinstance(first["resource_cursor"], str) or resumed["resource_cursor"] is not None
            or first["resource_ids"] == resumed["resource_ids"]):
        raise SourcePrerequisite(PREREQUISITES["recovery_pages"])
    port(ports, "pin_resources")(first["resource_ids"] + resumed["resource_ids"])
    provider_before = port(ports, "read_provider")()
    owned, preserved, attempts = [], None, {}
    try:
        for phase, page in (("interrupted", first), ("resumed", resumed)):
            detail = {key: value for key, value in page.items() if key != "source"}
            attempts[phase] = recovery_attempt(ports, phase, run_id=run_id, source_revision=source_revision,
                                               detail=detail, evidence=page["source"])
        preserved = checked_binding_id(port(ports, "create_binding")(
            {**selection, "native_id": resumed["resource_ids"][0]}))
        binding = port(ports, "create_binding")({**selection, "native_id": first["resource_ids"][0]})
        binding_id = checked_binding_id(binding)
        owned.append(binding_id)
        receipts = port(ports, "replay_verify")(binding_id)
        live = port(ports, "live_bindings")()
        attempts["reverified"] = recovery_attempt(
            ports, "reverified", run_id=run_id, source_revision=source_revision,
            detail={"binding_id": binding_id, "binding_ids": sorted(live),
                    "selected_native_id": first["resource_ids"][0],
                    "binding_native_id": port(ports, "binding_native_id")(binding_id), **receipts},
            evidence=receipts)
        before_values = port(ports, "confirmed_values")()
        tombstone = port(ports, "unlink_binding")(binding_id)
        after_values = port(ports, "confirmed_values")()
        attempts["unlinked"] = recovery_attempt(
            ports, "unlinked", run_id=run_id, source_revision=source_revision,
            detail={"unlinked_binding_ids": sorted(owned), "remaining_binding_ids": port(ports, "live_bindings")(),
                    "confirmed_values_before": before_values, "confirmed_values_after": after_values,
                    "cleanup_attempts": 1},
            evidence={"tombstone": tombstone})
    finally:
        receipt = retire_owned(ports, owned, run_id, source_revision, provider_before)
        retired_peer = port(ports, "retire_peer")(preserved)
    finished_at = pinned_now()
    provider_after = port(ports, "read_provider")()
    calls = list(port(ports, "provider_calls")())
    artifact = port(ports, "artifact")
    report = {
        "run_id": run_id,
        "source_revision": source_revision,
        "requirements_revision": requirements_revision,
        "owned_binding_ids": sorted(owned),
        "preserved_binding_id": preserved,
        "started_at": started_at,
        "finished_at": finished_at,
        "attempts": attempts,
        "independent_provider": {
            "artifact_ref": checked_ref(artifact("recovery-independent-provider", {
                "calls": calls, "before": provider_before, "after": provider_after}), "independent_provider"),
            "before": provider_before, "after": provider_after, "calls": calls,
        },
        "preserved_cleanup": retired_peer,
        "cleanup": {
            **receipt,
            "recovery_ref": checked_ref(artifact("recovery-attempts", attempts), "recovery"),
            "owned_readback_ref": checked_ref(artifact("recovery-owned-readback", {
                "owned": receipt, "preserved": retired_peer}), "owned_readback"),
        },
    }
    case = pinned_source_recovery_case(source_revision, requirements_revision, run_id, started_at,
                                       sorted(owned), preserved)
    if set(attempts) != set(RECOVERY_PHASES):
        raise ValueError("The deployed recovery run did not observe every reviewed phase")
    if not verify_live_source_recovery(case, report):
        raise ValueError("The deployed source recovery report was not accepted by the reviewed SCN-03 check")
    return protected_report(report_path, report)


def build_recovery_ports(provider, fixture, directory, *, page=None, environ=None, cli=None, fetch=None):
    """Wire the deployed SCN-03 recovery surfaces on top of the SCN-01 source ports."""
    environ = os.environ if environ is None else environ
    ports = build_source_ports(provider, fixture, directory, page=page, environ=environ, cli=cli, fetch=fetch)
    base = provider.base_url + project_path(fixture)
    owner = fixture["owner"]
    record = fixture.get("evidence_record")
    request = record.request(provider.request) if record is not None else provider.request
    artifact = ports["artifact"]
    state = {"selection": None, "natives": []}
    calls = []

    def discover():
        selections = port(ports, "discover")()
        state["selection"] = selections.get("repository") if isinstance(selections, dict) else None
        return selections

    def live_bindings():
        """The bindings the Project still holds, ignoring the tombstones cleanup leaves behind."""
        return sorted(view["id"] for view in port(ports, "list_bindings")()
                      if isinstance(view, dict) and isinstance(view.get("id"), str)
                      and view.get("status") != "unlinked")

    def require_isolation():
        if live_bindings():
            raise SourcePrerequisite(PREREQUISITES["recovery_isolation"])
        return True

    def source_page(cursor, page_count):
        reference = (state["selection"] or {}).get("connection_ref") or {}
        if reference.get("kind") not in CONNECTION_KINDS or not isinstance(reference.get("id"), str):
            raise SourcePrerequisite(PREREQUISITES["repository_selection"])
        query = {"limit": 1, "connection_kind": reference["kind"], "connection_id": reference["id"]}
        if cursor is not None:
            query["resource_cursor"] = cursor
        payload = api_json(request, owner, "get", base + "/sources?" + urlencode(query), expected=(200,))
        return recovery_page(payload, cursor, page_count)

    def pin_resources(native_ids):
        pinned = sorted({value for value in native_ids if isinstance(value, str) and value})
        if len(pinned) != 2:
            raise SourcePrerequisite(PREREQUISITES["recovery_pages"])
        state["natives"] = pinned
        return pinned

    def read_provider():
        """Read both paged repository identities back from GitHub itself, with GET calls only."""
        token = environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN")
        if not token or not state["natives"]:
            raise SourcePrerequisite(PREREQUISITES["repository_provider"])
        observed = {}
        for native_id in state["natives"]:
            url = "https://api.github.com/repositories/" + quote(native_id, safe="")
            calls.append("GET " + url)
            payload = (fetch or github_repository)(url, token)
            try:
                observed[native_id] = {"id": payload["id"], "owner": {"id": payload["owner"]["id"]},
                                       "updated_at": payload["updated_at"], "name": payload.get("name")}
            except (KeyError, TypeError) as error:
                raise ValueError("The independent GitHub readback returned no repository identity") from error
        return observed

    def replay_verify(binding_id):
        """Replay one verification under a single idempotency key and read one receipt back."""
        url = base + "/bindings/" + quote(binding_id, safe="") + "/verify"
        headers = ports["mutation_headers"]("v06-recover-" + binding_id)
        receipts = []
        for _ in range(2):
            body, _ = api_call(request, owner, "post", url, headers=dict(headers), expected=(200, 202))
            identifier = (body.get("operation_id") or body.get("id")) if isinstance(body, dict) else None
            if not isinstance(identifier, str) or not identifier:
                raise SourcePrerequisite(PREREQUISITES["verify_receipt"])
            receipts.append(identifier)
        if receipts[0] != receipts[1]:
            raise ValueError("Repeated deployed verification did not reuse one operation receipt")
        return {"verify_receipt_ids": receipts, "verify_attempts": len(receipts)}

    def binding_native_id(binding_id):
        for view in port(ports, "list_bindings")():
            observed = flatten_binding(view)
            if observed.get("id") == binding_id:
                native_id = observed.get("native_id")
                if not isinstance(native_id, str) or not native_id:
                    raise ValueError("The owned source binding readback published no native identity")
                return native_id
        raise ValueError("The owned source binding is missing from its own recovery readback")

    def confirmed_values():
        body = api_json(request, owner, "get", base, expected=(200,))
        sections = {key: body.get(key) if isinstance(body.get(key), dict) else {} for key in ("product", "project")}
        values = {}
        for section, keys in (("product", ("name", "purpose")), ("project", ("name", "environment_class"))):
            for key in keys:
                value = sections[section].get(key)
                if isinstance(value, str) and value:
                    values[section + "_" + key] = value
        if not values:
            raise SourcePrerequisite(PREREQUISITES["recovery_values"])
        return values

    def cli_recovery(phase, cursor):
        run = cli if cli is not None else installed_cli_runner(provider, fixture, environ, record)
        reference = (state["selection"] or {}).get("connection_ref") or {}
        scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
        arguments = ["sources", *scope, "--connection-kind", str(reference.get("kind")),
                     "--connection-id", str(reference.get("id")), "--limit", "1"]
        if cursor is not None:
            arguments += ["--resource-cursor", cursor]
        listed = run(arguments)
        bound = run(["source-bindings", *scope])
        items = listed.get("items") if isinstance(listed, dict) else None
        if (not isinstance(items, list) or len(items) != 1 or not isinstance(bound, dict)
                or not isinstance(bound.get("items"), list)):
            raise ValueError("The installed Product client read back no paged source or bindings in " + str(phase))
        return {"sources": listed, "source_bindings": bound}

    def recovery_surfaces(phase, detail, evidence=None):
        bindings = api_json(request, owner, "get", base + "/bindings?" + urlencode({"limit": 50}), expected=(200,))
        if not isinstance(bindings.get("items"), list):
            raise SourcePrerequisite(PREREQUISITES["list_bindings"])
        client = cli_recovery(phase, detail.get("requested_cursor"))
        browser = served_bindings_view(page, owner, fixture, phase)
        observed = read_provider()
        name = str(phase) + "-recovery"
        return {"artifacts": {
            "api": artifact(name + "-api", {"detail": detail, "evidence": evidence, "bindings": bindings,
                                            "revisions": port(ports, "revisions")()}),
            "installed_cli": artifact(name + "-installed-cli", client),
            "browser": artifact(name + "-browser", browser),
            "independent_provider": artifact(name + "-independent-provider", {
                "calls": list(calls), "observed": observed}),
        }}

    def unlink_binding(binding_id):
        """Unlink the owned binding once; the owned retirement then reads its deployed tombstone back.

        SCN-03 unlinks inside the observed run, and ``retire_owned`` retires the same binding again in the
        run's teardown. The deployed DELETE answers a tombstone that stays in the readback, so a second
        request is not sent for an already unlinked binding; its published tombstone is read instead.
        """
        for view in port(ports, "list_bindings")():
            if isinstance(view, dict) and view.get("id") == binding_id and view.get("status") == "unlinked":
                return dict(view)
        return port(ports, "unlink_binding")(binding_id)

    def retire_peer(binding_id):
        """Retire the peer binding this run created beside the owned one."""
        if binding_id is None:
            return {"preserved_binding_id": None, "status": None, "readback": None}
        tombstone = port(ports, "unlink_binding")(binding_id)
        readback = {view.get("id"): view.get("status") for view in port(ports, "list_bindings")()
                    if isinstance(view, dict)}
        return {"preserved_binding_id": binding_id, "status": tombstone.get("status"),
                "readback": readback.get(binding_id), "observed_at": utc_now()}

    return {**ports, "discover": discover, "require_isolation": require_isolation, "source_page": source_page,
            "pin_resources": pin_resources, "read_provider": read_provider, "provider_calls": lambda: list(calls),
            "replay_verify": replay_verify, "binding_native_id": binding_native_id,
            "confirmed_values": confirmed_values, "recovery_surfaces": recovery_surfaces,
            "live_bindings": live_bindings, "retire_peer": retire_peer, "unlink_binding": unlink_binding}
