#!/usr/bin/env python3
"""Read and manage per-principal persona model preferences.

The human path is stdlib-only and reuses the ADP Cognito session.  Setting
``ADP_GATEWAY_ENDPOINT`` selects the service-principal path: requests are then
signed for execute-api with temporary role credentials and never fall back to
the human bearer session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

CliError = common.CliError
Parser = common.Parser

COMMAND = "models"
MACHINE_ENDPOINT_ENV = "ADP_GATEWAY_ENDPOINT"
WAITABLE_REASONS = {"probing_disabled", "evidence_stale", "catalogue_unavailable"}
CALLER_MODE_HUMAN = "human/bearer"
CALLER_MODE_MACHINE = "machine/SigV4"
CALLER_MODES = {CALLER_MODE_HUMAN, CALLER_MODE_MACHINE}
_CREDENTIAL_OPTION = re.compile(
    r"(?:^|[-_])(?:password|passwd|secret|token)(?:$|[-_])"
    r"|(?:^|[-_])(?:access|api|private)[-_]?key(?:$|[-_])",
    re.IGNORECASE,
)


MODEL_MESSAGES = {
    "probing_disabled": "Model choices are not ready yet. Contact your ADP administrator.",
    "not_yet_certified": "This model is not ready to use yet. Choose another model.",
    "evidence_stale": "Model availability needs checking. Try another available model or contact your ADP administrator.",
    "catalogue_unavailable": "Model choices could not be loaded. Try again shortly.",
    "not_invocable": "This model is currently unavailable. Choose another model.",
    "retired": "This model has been retired. Choose another model.",
    "not_permitted": "Your organization does not allow this model. Choose another model or contact your ADP administrator.",
    "harness_incompatible": "This persona does not support this model.",
    "unknown_model": "This model is not listed. Check `adp models catalog --persona ...` for available choices.",
}


class HttpError(CliError):
    """A safely summarized HTTP refusal with its status retained for 409 logic."""

    def __init__(self, status, reason="http_error"):
        hints = {
            401: "Sign in again with adp login or adp admin login.",
            403: "This operation requires an authorized ADP administrator.",
            404: "Check the target; the gateway may need an upgrade.",
            409: "Configuration changed. Read its current status before retrying.",
            429: "Wait a minute before retrying.",
        }
        exit_code = 2 if status == 401 else 3 if status == 403 else 4 if reason in WAITABLE_REASONS else 5
        super().__init__(
            MODEL_MESSAGES.get(reason) or hints.get(status, "ADP could not complete the request. Read your settings before retrying; the request may have changed them."),
            reason,
            exit_code,
        )
        self.status = status


def _safe_http_error(exc):
    reason = "http_error"
    try:
        detail = json.load(exc).get("detail", {})
        candidate = detail.get("error", detail.get("reason", "")) if isinstance(detail, dict) else ""
        if re.fullmatch(r"[a-z_]{1,80}", candidate):
            reason = candidate
    except (ValueError, AttributeError):
        pass
    return HttpError(exc.code, reason)


def _validate_machine_endpoint(value):
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CliError(
            f"{MACHINE_ENDPOINT_ENV} must be an HTTPS execute-api invoke URL without credentials, query or fragment.",
            "invalid_machine_endpoint",
        )
    match = re.search(r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com$", parsed.hostname)
    if not match:
        raise CliError(
            f"{MACHINE_ENDPOINT_ENV} must name an AWS execute-api endpoint so the signing region is unambiguous.",
            "invalid_machine_endpoint",
        )
    return value.rstrip("/"), match.group(1)


class ModelsApi:
    """One response contract over bearer and SigV4 transports."""

    def __init__(self):
        endpoint = os.environ.get(MACHINE_ENDPOINT_ENV, "").strip()
        self.machine = bool(endpoint)
        self.caller_mode = CALLER_MODE_MACHINE if self.machine else CALLER_MODE_HUMAN
        self.opener = urllib.request.build_opener(common.NoRedirect())
        if self.machine:
            self.base, self.region = _validate_machine_endpoint(endpoint)
        else:
            self.base = common.gateway_url()
            self.region = None

    def _machine_headers(self, method, url, body):
        try:
            import botocore.auth
            import botocore.awsrequest
            import botocore.credentials
            import botocore.session
        except ImportError:
            raise CliError(
                "Service-principal model commands require botocore and temporary role credentials.",
                "signing_dependency_missing",
            ) from None

        # SigV4Auth can log the canonical request (including signed headers).
        logging.getLogger("botocore.auth").setLevel(logging.INFO)
        credentials = botocore.session.get_session().get_credentials()
        if credentials is None:
            raise CliError("No AWS role credentials were found for the service-principal request.", "role_credentials_missing")
        if not isinstance(credentials, botocore.credentials.RefreshableCredentials):
            raise CliError(
                "Service-principal model commands accept only refreshable temporary role credentials; static access keys are refused.",
                "static_credentials_refused",
            )
        frozen = credentials.get_frozen_credentials()
        if not frozen.token:
            raise CliError(
                "Service-principal model commands accept only temporary role credentials with a session token.",
                "static_credentials_refused",
            )
        headers = {"Content-Type": "application/json"}
        aws_request = botocore.awsrequest.AWSRequest(method=method, url=url, data=body, headers=headers)
        botocore.auth.SigV4Auth(frozen, "execute-api", self.region).add_auth(aws_request)
        return dict(aws_request.headers.items())

    def request(self, method, path, body=None, timeout=120):
        if not path.startswith("/") or path.startswith("//"):
            raise CliError("Invalid ADP API path.")
        wire_path = "/agent" + path if self.machine else path
        url = self.base + wire_path
        encoded = json.dumps(body).encode() if body is not None else None
        headers = (
            self._machine_headers(method, url, encoded)
            if self.machine
            else {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + common.access_token(),
            }
        )
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                payload = response.read()
                if not payload.strip() and response.status in (204, 205):
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise CliError(
                "ADP could not be reached or returned an invalid response. Check status before retrying.",
                "gateway_unavailable",
            ) from None


def segment(value):
    return urllib.parse.quote(value, safe="")


def _base_path(service_principal=None):
    if service_principal:
        return f"/service-principals/{segment(service_principal)}/persona-models"
    return "/me/persona-models"


def _assert_target_allowed(client, service_principal):
    if client.machine and service_principal:
        raise CliError(
            "A service principal can manage only its own mappings; remove --service-principal.",
            "usage_error",
            1,
        )


def _entry_for(response, persona):
    return next((row for row in response.get("entries", []) if row.get("persona_key") == persona), None)


def _list(client, service_principal=None):
    _assert_target_allowed(client, service_principal)
    return client.request("GET", _base_path(service_principal))


def _explain(client, persona, service_principal=None):
    _assert_target_allowed(client, service_principal)
    return client.request("GET", _base_path(service_principal) + f"/explain/{segment(persona)}")


def _catalogue(client, persona, service_principal=None):
    _assert_target_allowed(client, service_principal)
    return client.request(
        "GET",
        _base_path(service_principal) + "/catalog?" + urllib.parse.urlencode({"persona_key": persona}),
    )


def _match_catalogue_model(catalogue, requested):
    """Use only aliases published by the server; never maintain a local map."""
    exact = []
    folded = requested.casefold()
    for row in catalogue.get("models", []):
        names = [row.get("canonical_model_id"), row.get("alias"), *(row.get("aliases") or [])]
        if any(isinstance(name, str) and name.casefold() == folded for name in names):
            exact.append(row)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise CliError("The server catalogue returned an ambiguous model alias.", "ambiguous_model")
    raise CliError(
        f"Model '{requested}' is not present among the canonical IDs or pinned aliases returned by the server catalogue. "
        "Use a value shown by `adp models catalog --persona ...`.",
        "unknown_model",
    )


def _published_model(client, persona, model, service_principal=None):
    """Resolve only canonical IDs and aliases published by the target catalogue."""
    catalogue = _catalogue(client, persona, service_principal)
    row = _match_catalogue_model(catalogue, model)
    reason = row.get("reason")
    if not row.get("selectable"):
        exit_code = 4 if reason in WAITABLE_REASONS else 5
        raise CliError(
            f"Model '{model}' cannot be selected for '{persona}'. "
            + MODEL_MESSAGES.get(reason, "Choose another available model."),
            reason or "not_selectable",
            exit_code,
        )
    return row, catalogue


def reject_secret_arguments(argv):
    """Refuse credential-shaped flags without reflecting their value."""
    for value in argv:
        name = value.split("=", 1)[0].lstrip("-")
        if value.startswith("-") and _CREDENTIAL_OPTION.search(name):
            raise CliError(
                "Model commands accept no credential arguments. Use the existing ADP session or workload role.",
                "secret_in_argv",
                1,
            )


def _principal_id(response):
    """Accept the common human field and the explicit service-principal field."""
    return response.get("canonical_service_principal_id") or response.get("principal_id")


def _caller_mode(client):
    """Return the bounded mode selected by the concrete API transport."""
    caller_mode = getattr(client, "caller_mode", None)
    if caller_mode not in CALLER_MODES:
        raise CliError("The selected models transport did not identify its caller mode.", "invalid_response")
    return caller_mode


def _tenant_id(response):
    """Require the server-resolved active tenant; never infer it client-side."""
    tenant_id = response.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise CliError(
            "The gateway response did not name the authenticated active tenant. Verify mapping state before retrying and upgrade the gateway.",
            "invalid_response",
        )
    return tenant_id


def _assert_same_tenant(expected, response):
    actual = _tenant_id(response)
    if actual != expected:
        raise CliError(
            "The gateway returned inconsistent tenant context across one model command. Verify mapping state before retrying.",
            "invalid_response",
        )


def _confirm(args, action):
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise CliError("Non-interactive changes require --yes. Use --dry-run on set to inspect validation first.", "usage_error", 1)
    print(action, file=sys.stderr)
    print("Continue? Type yes: ", end="", file=sys.stderr, flush=True)
    if input().strip() != "yes":
        raise CliError("Cancelled; no changes made.", "cancelled")


def _same_entry(left, right):
    keys = ("saved_model_id", "revision", "status", "effective_model_id", "source")
    return all((left or {}).get(key) == (right or {}).get(key) for key in keys)


def _is_stale_response(response):
    """Recognise the server's supported stale markers without scanning unrelated text."""
    evidence = response.get("evidence")
    return response.get("status") == "stale" or response.get("stale") is True or (isinstance(evidence, dict) and evidence.get("stale") is True)


def _stale_write_result(args, before_response, tenant_id, caller_mode):
    """Decline to certify a write when a success-shaped response is stale."""
    return common.envelope(
        "unavailable",
        _command_name(args),
        {
            "tenant_id": tenant_id,
            "caller_mode": caller_mode,
            "principal_kind": before_response.get("principal_kind"),
            "principal_id": _principal_id(before_response),
            "persona_key": args.persona,
            "reason": "evidence_stale",
            "changed": None,
        },
        "The gateway returned a stale-marked write response. The saved state is unknown; read it back with `adp models mappings list`.",
    )


def _command_name(args):
    pieces = ["models", args.area]
    if getattr(args, "action", None):
        pieces.append(args.action)
    return " ".join(pieces)


def _mapping_source(row):
    if row.get("source") == "principal-mapping":
        return "Saved choice"
    if row.get("source") != "system-default":
        return "Source unavailable"
    if not row.get("effective_model_id"):
        return "No default configured for this persona"
    if row.get("effective_is_candidate") or row.get("class_default_status") == "candidate":
        return "Default for this persona (not ready)"
    if row.get("class_default_status") == "proven":
        return "Default for this persona"
    return "Default for this persona (availability unconfirmed)"


def _print_mapping(row):
    persona = row.get("persona_display_name") or row.get("persona_key", "Persona")
    model = row.get("effective_model_id") or "No model configured"
    print(f"{persona}: {model}")
    print(f"  {_mapping_source(row)}")
    reason = row.get("availability_reason") or row.get("reason")
    if row.get("model_lifecycle") == "retired":
        reason = "retired"
    elif row.get("status") == "disallowed" or row.get("availability_status") == "disallowed":
        reason = "not_permitted"
    elif row.get("status") == "stale" or row.get("availability_status") == "stale":
        reason = "evidence_stale"
    if reason in MODEL_MESSAGES:
        print(f"  {MODEL_MESSAGES[reason]}")
    elif row.get("availability_status") in {"unavailable", "unknown"} or row.get("status") == "unavailable":
        print("  Model availability could not be confirmed. Check the available choices.")
    for warning in row.get("warnings") or []:
        print(f"  Warning: {warning}")


def emit_result(result, as_json=False):
    """Summarize choices for people; preserve the full API contract in JSON."""
    if as_json:
        return common.emit(result, True)
    command = result.get("command")
    detail = result.get("detail") or {}
    heading = f"{command}: {result['status']}"
    mutation = command in {"models mappings set", "models mappings reset"}
    if mutation:
        tenant_id = _tenant_id(detail)
        principal_id = _principal_id(detail)
        caller_mode = detail.get("caller_mode")
        if caller_mode not in CALLER_MODES or not principal_id:
            raise CliError(
                "ADP could not confirm which account these settings belong to. Read your settings before retrying.",
                "invalid_response",
            )
        account_kind = "service account" if detail.get("principal_kind") == "service_account" else "account"
        heading += f" — {account_kind} {principal_id}; organization {tenant_id}"
    print(heading)
    if command == "models catalog":
        for model in detail.get("models", []):
            model_id = model.get("canonical_model_id", "Unknown model")
            name = " ".join(filter(None, (model.get("model_family"), model.get("canonical_version"))))
            status = "Available" if model.get("selectable") else MODEL_MESSAGES.get(model.get("reason"), "Unavailable")
            print(f"{name or model_id}: {status}")
            print(f"  --model {model_id}")
            price = model.get("price_context") or {}
            if price.get("input_per_million_tokens") is not None or price.get("output_per_million_tokens") is not None:
                input_price = price.get("input_per_million_tokens")
                output_price = price.get("output_per_million_tokens")
                print(f"  Input: {'unknown' if input_price is None else '$' + str(input_price)}; "
                      f"output: {'unknown' if output_price is None else '$' + str(output_price)} per 1M tokens")
        if not detail.get("models"):
            print("No model choices are available for this persona.")
    elif command == "models service-principals":
        for principal in detail.get("principals", []):
            print(f"{principal.get('display_name') or 'Service account'}: {principal.get('canonical_service_principal_id')}")
        if not detail.get("principals"):
            print("No service accounts are available to manage.")
    elif command == "models mappings list":
        for row in detail.get("entries", []):
            _print_mapping(row)
        if not detail.get("entries"):
            print("No persona settings are available.")
    elif detail.get("dry_run"):
        print(f"Preview: {detail.get('persona_key')} → {detail.get('canonical_model_id')}")
    elif mutation and result["status"] != "ok":
        print("The saved state is unknown.")
    else:
        _print_mapping(detail)
        if mutation:
            print("Saved. Applies to new runs." if detail.get("changed") else "Already saved; no change needed.")
    if result.get("next_action"):
        print(result["next_action"])
    return 4 if result["status"] in {"pending", "unavailable"} else 5 if result["status"] == "failed" else 0


def run(args, client):
    if args.area == "catalog":
        result = _catalogue(client, args.persona)
        _tenant_id(result)
        return common.envelope("ok", _command_name(args), result)

    if args.area == "service-principals":
        if client.machine:
            raise CliError("A service principal cannot enumerate other principals.", "usage_error", 1)
        result = client.request("GET", "/me/persona-models/manageable-service-principals")
        _tenant_id(result)
        return common.envelope("ok", _command_name(args), result)

    target = args.service_principal
    if args.action in {"set", "reset"} and not getattr(args, "dry_run", False):
        operation = "models.mapping.managed.write" if target else "models.mapping.self.write"
        common.ensure_can_mutate(operation, request=client.request)
    if args.action == "list":
        result = _list(client, target)
        _tenant_id(result)
        return common.envelope("ok", _command_name(args), result)
    if args.area == "explain":
        result = _explain(client, args.persona, target)
        _tenant_id(result)
        return common.envelope("ok", _command_name(args), result)

    before_response = _list(client, target)
    tenant_id = _tenant_id(before_response)
    caller_mode = _caller_mode(client)
    principal_id = _principal_id(before_response)
    if not isinstance(principal_id, str) or not principal_id:
        raise CliError(
            "The gateway response did not name the server-resolved canonical principal.",
            "invalid_response",
        )
    before = _entry_for(before_response, args.persona)
    if before is None:
        raise CliError(f"The gateway did not return persona '{args.persona}' in the effective mapping list.", "invalid_response")

    if args.action == "set":
        model_row, catalogue = _published_model(client, args.persona, args.model, target)
        _assert_same_tenant(tenant_id, catalogue)
        canonical = model_row["canonical_model_id"]
        already_saved = before.get("saved_model_id") == canonical
        detail = {
            "tenant_id": tenant_id,
            "caller_mode": caller_mode,
            "principal_kind": before_response.get("principal_kind"),
            "principal_id": principal_id,
            "persona_key": args.persona,
            "requested_model": args.model,
            "canonical_model_id": canonical,
            "compatibility_class": catalogue.get("compatibility_class"),
            "changed": not already_saved,
        }
        if args.dry_run:
            detail["dry_run"] = True
            detail["effective_destination"] = model_row.get("evidence")
            detail["expected_source"] = "principal-mapping"
            return common.envelope("ok", _command_name(args), detail, "No mapping was changed.")
        if already_saved:
            detail.update(before)
            detail["caller_mode"] = caller_mode
            detail["principal_id"] = principal_id
            detail["changed"] = False
            return common.envelope("ok", _command_name(args), detail)
        _confirm(args, f"Set persona '{args.persona}' to '{canonical}' for principal {principal_id}.")
        current = _entry_for(_list(client, target), args.persona)
        if not _same_entry(before, current):
            raise CliError("The mapping changed while this command was preparing. Read it and retry.", "revision_conflict")
        try:
            result = client.request(
                "PUT",
                _base_path(target) + f"/{segment(args.persona)}",
                {"model": args.model, "expected_revision": before.get("revision")},
            )
        except HttpError as exc:
            if exc.status != 409:
                raise
            reread = None
            try:
                reread = _entry_for(_list(client, target), args.persona)
            except CliError:
                pass
            message = "The mapping write conflicted; nothing was overwritten."
            if reread:
                message += f" Revision {reread.get('revision')} was current as of the re-read."
            else:
                message += " The current revision could not be determined."
            raise CliError(message, "revision_conflict") from None
        _assert_same_tenant(tenant_id, result)
        if _is_stale_response(result):
            return _stale_write_result(args, before_response, tenant_id, caller_mode)
        result["caller_mode"] = caller_mode
        result.setdefault("principal_kind", before_response.get("principal_kind"))
        result["principal_id"] = principal_id
        result["changed"] = True
        return common.envelope("ok", _command_name(args), result)

    if before.get("saved_model_id") is None:
        result = _explain(client, args.persona, target)
        _assert_same_tenant(tenant_id, result)
        result["caller_mode"] = caller_mode
        result.setdefault("principal_kind", before_response.get("principal_kind"))
        result["principal_id"] = principal_id
        result["changed"] = False
        return common.envelope("ok", _command_name(args), result)
    _confirm(args, f"Reset persona '{args.persona}' for principal {principal_id} to its default model.")
    current = _entry_for(_list(client, target), args.persona)
    if not _same_entry(before, current):
        raise CliError("The mapping changed while this command was preparing. Read it and retry.", "revision_conflict")
    try:
        result = client.request(
            "DELETE",
            _base_path(target) + f"/{segment(args.persona)}",
            {"expected_revision": before.get("revision")},
        )
    except HttpError as exc:
        if exc.status != 409:
            raise
        reread = None
        try:
            reread = _entry_for(_list(client, target), args.persona)
        except CliError:
            pass
        message = "The mapping reset conflicted; nothing was overwritten."
        if reread:
            message += f" Revision {reread.get('revision')} was current as of the re-read."
        else:
            message += " The current revision could not be determined."
        raise CliError(message, "revision_conflict") from None
    _assert_same_tenant(tenant_id, result)
    if _is_stale_response(result):
        return _stale_write_result(args, before_response, tenant_id, caller_mode)
    removed = result.get("removed")
    if not isinstance(removed, bool):
        raise CliError(
            "The gateway reset response did not report whether it removed a mapping. "
            "The saved state is unknown; read it back with `adp models mappings list`.",
            "invalid_response",
        )
    result["caller_mode"] = caller_mode
    result.setdefault("principal_kind", before_response.get("principal_kind"))
    result["principal_id"] = principal_id
    result["changed"] = removed
    return common.envelope("ok", _command_name(args), result)


def parser():
    root = Parser(prog="adp models", description="Manage persona-to-model mappings for the authenticated principal.")
    areas = root.add_subparsers(dest="area", required=True)

    catalog = areas.add_parser("catalog")
    catalog.add_argument("--persona", required=True)
    catalog.add_argument("--json", action="store_true")

    mappings = areas.add_parser("mappings")
    mapping_actions = mappings.add_subparsers(dest="action", required=True)
    listing = mapping_actions.add_parser("list")
    listing.add_argument("--service-principal")
    listing.add_argument("--json", action="store_true")
    setting = mapping_actions.add_parser("set")
    setting.add_argument("--persona", required=True)
    setting.add_argument("--model", required=True)
    setting.add_argument("--service-principal")
    setting.add_argument("--dry-run", action="store_true")
    setting.add_argument("--yes", action="store_true")
    setting.add_argument("--json", action="store_true")
    resetting = mapping_actions.add_parser("reset")
    resetting.add_argument("--persona", required=True)
    resetting.add_argument("--service-principal")
    resetting.add_argument("--yes", action="store_true")
    resetting.add_argument("--json", action="store_true")

    explain = areas.add_parser("explain")
    explain.set_defaults(action=None)
    explain.add_argument("--persona", required=True)
    explain.add_argument("--service-principal")
    explain.add_argument("--json", action="store_true")

    principals = areas.add_parser("service-principals")
    principal_actions = principals.add_subparsers(dest="action", required=True)
    principal_list = principal_actions.add_parser("list")
    principal_list.add_argument("--json", action="store_true")
    return root


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        reject_secret_arguments(argv)
        args = parser().parse_args(argv)
        result = run(args, ModelsApi())
        return emit_result(result, args.json)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, COMMAND, as_json)
    except KeyboardInterrupt:
        return common.report_error(
            CliError(
                "Interrupted. A write may already have been accepted; run `adp models mappings list` to verify.",
                "interrupted",
                130,
            ),
            COMMAND,
            as_json,
        )


if __name__ == "__main__":
    sys.exit(main())
