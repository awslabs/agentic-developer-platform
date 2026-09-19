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
SECRET_FLAGS = {
    "--access-key",
    "--api-key",
    "--password",
    "--secret",
    "--secret-key",
    "--session-token",
    "--token",
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
            f"ADP returned HTTP {status} ({reason}). "
            + hints.get(status, "Check status before retrying; an interrupted request may have changed configuration."),
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
            f"Model '{model}' is not selectable for persona '{persona}' ({reason or 'not_selectable'}).",
            reason or "not_selectable",
            exit_code,
        )
    return row, catalogue


def reject_secret_arguments(argv):
    """Refuse credential-shaped flags without reflecting their value."""
    for value in argv:
        name = value.split("=", 1)[0]
        if name in SECRET_FLAGS:
            raise CliError(
                "Model commands accept no credential arguments. Use the existing ADP session or workload role.",
                "secret_in_argv",
                1,
            )


def _principal_id(response):
    """Accept the common human field and the explicit service-principal field."""
    return response.get("canonical_service_principal_id") or response.get("principal_id")


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


def _command_name(args):
    pieces = ["models", args.area]
    if getattr(args, "action", None):
        pieces.append(args.action)
    return " ".join(pieces)


def run(args, client):
    if args.area == "catalog":
        return common.envelope("ok", _command_name(args), _catalogue(client, args.persona))

    if args.area == "service-principals":
        if client.machine:
            raise CliError("A service principal cannot enumerate other principals.", "usage_error", 1)
        return common.envelope(
            "ok",
            _command_name(args),
            client.request("GET", "/me/persona-models/manageable-service-principals"),
        )

    target = args.service_principal
    if args.action == "list":
        return common.envelope("ok", _command_name(args), _list(client, target))
    if args.area == "explain":
        return common.envelope("ok", _command_name(args), _explain(client, args.persona, target))

    before_response = _list(client, target)
    before = _entry_for(before_response, args.persona)
    if before is None:
        raise CliError(f"The gateway did not return persona '{args.persona}' in the effective mapping list.", "invalid_response")

    if args.action == "set":
        model_row, catalogue = _published_model(client, args.persona, args.model, target)
        canonical = model_row["canonical_model_id"]
        already_saved = before.get("saved_model_id") == canonical
        detail = {
            "principal_kind": before_response.get("principal_kind"),
            "principal_id": _principal_id(before_response),
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
            detail["changed"] = False
            return common.envelope("ok", _command_name(args), detail)
        _confirm(args, f"Set persona '{args.persona}' to '{canonical}' for principal {_principal_id(before_response)}.")
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
        result["changed"] = True
        return common.envelope("ok", _command_name(args), result)

    if before.get("saved_model_id") is None:
        result = _explain(client, args.persona, target)
        result["changed"] = False
        return common.envelope("ok", _command_name(args), result)
    _confirm(args, f"Reset persona '{args.persona}' for principal {_principal_id(before_response)} to its class default.")
    current = _entry_for(_list(client, target), args.persona)
    if not _same_entry(before, current):
        raise CliError("The mapping changed while this command was preparing. Read it and retry.", "revision_conflict")
    result = client.request("DELETE", _base_path(target) + f"/{segment(args.persona)}")
    result["changed"] = True
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
        return common.emit(result, args.json)
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
