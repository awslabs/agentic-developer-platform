#!/usr/bin/env python3
"""E13 on the instance: the live API judged by its own two consumers.

E13 claims that "live API field/type/ownership assertions derive from actual
CLI/UI consumers, and UI-created and CLI-created resources are equivalently
usable". The tempting implementation is a list of expected field names. That
would assert this harness's opinion of the contract, agree with itself forever,
and survive exactly the change it exists to catch.

So no field name is written down here. The two consumers are RUN against the
deployment under test:

- The CLI consumer is imported from the installed release on this instance and
  called directly: `adp_aws.connections(api)`, `resolve_connection`,
  `existing_for`, `adp_bedrock.destinations(api)`, `find_prepared`,
  `current_mapping`. These functions subscript the rows they fetch
  (`row["service"]`, `row["credential_type"]`, `row["owner_org_id"]`, ...), so a
  server that stops returning a field makes the PRODUCT'S OWN CODE raise, on the
  exact line a user's command would have raised on. Calling them is what makes
  the assertion derived rather than declared, and importing them from the
  installed prefix — the bytes E01 hash-verified — is why this runs here and not
  in the orchestrator.
- The UI consumer cannot be imported: it is TypeScript in a browser bundle. Its
  wire types are declarations, so the orchestrator extracts them from the source
  at the revision under test and passes them in; every row is then checked
  against what the browser declares, including nullability and literal unions.
  A missing or empty contract is a hard failure, never a silent downgrade to
  checking the CLI alone.

Equivalence is then proved on a resource this run creates through the CLI, using
`adp aws connect --download`. That path needs no credentials in the destination
account — it asks the gateway for the setup package and saves it — which is why
E13 can prove it with only the EC2 and PLATFORM fixtures its ledger entry
declares. The row it creates must be visible to, resolvable by, and complete for
both consumers.

What this deliberately does NOT do is drive a browser. E13's fixtures do not
include a hosted session, and asserting a UI journey here would be exactly the
prefilled evidence this harness exists to prevent. The claim tested is parity of
the contract the SPA consumes; the browser journeys belong to E09-E12.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import common
from common import require

CREDENTIALS = "/auth/credentials"
ROUTING = "/admin/bedrock-routing"

# The live routes whose rows both consumers read, and which UI wire type governs
# each. The names are the orchestrator's keys, so a renamed TypeScript interface
# surfaces as "no UI contract for ..." rather than as a silently skipped check.
SURFACES = {
    CREDENTIALS: "CredentialItem",
    ROUTING + "/destinations": "DestinationSummary",
    ROUTING + "/mappings": "MappingSummary",
}


def _load(prefix, filename, module_name):
    """Import one installed CLI helper under a name Python can actually use.

    `adp-aws.py` is not an importable module name, and these files are installed
    without a package, so they are loaded by path. `sys.path` gets the install
    prefix because each helper does `sys.path.insert(0, ...)` for `adp_common`
    relative to its own location; loading by path keeps that working.
    """
    path = Path(prefix) / filename
    require(
        path.is_file(),
        f"{filename} is not present in the installed CLI at {prefix}; the release "
        "under test cannot be judged against its own consumers",
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    require(spec and spec.loader, f"{filename} could not be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _consumers(config, evidence):
    """The installed CLI's own reader functions, ready to call."""
    prefix = str(Path(config["cli_path"]).resolve().parent)
    if prefix not in sys.path:
        sys.path.insert(0, prefix)
    aws = _load(prefix, "adp-aws.py", "eval_adp_aws")
    bedrock = _load(prefix, "adp-bedrock.py", "eval_adp_bedrock")
    # The paths these modules were built against. If a release moved a route, the
    # surfaces below would be checked while the CLI read somewhere else.
    require(
        aws.CREDENTIALS == CREDENTIALS and bedrock.ROUTING == ROUTING,
        "The installed CLI reads different API paths than this case checks: "
        f"{aws.CREDENTIALS!r}, {bedrock.ROUTING!r}",
    )
    # Note the key names throughout this module: no evidence key contains
    # "credential", "token" or "secret", and the surface names that DO -- the
    # `/auth/credentials` route, the `CredentialItem` interface -- are recorded as
    # VALUES, never as keys. `common.redact` drops any key matching its sensitive
    # pattern at any depth, which is the correct default and is why the route this
    # case exists to check must not be spelled as a dictionary key: it would be
    # replaced by "<redacted>" in the published evidence and the check would look
    # like it never ran.
    evidence["consumers"] = {
        "cli_prefix": prefix,
        "connections_path": aws.CREDENTIALS,
        "routing_path": bedrock.ROUTING,
    }
    return aws, bedrock


def _ui_contracts(config, evidence):
    """The browser's wire types, extracted by the orchestrator from the revision.

    Supplied rather than parsed here because the TypeScript lives in the repo and
    the instance has only the CLI. Validated on arrival: an absent, empty or
    untyped contract would quietly reduce E13 to a CLI-only check that still
    reported a pass.
    """
    supplied = config.get("ui_contracts")
    require(
        isinstance(supplied, dict) and supplied,
        "No UI wire contracts were supplied; E13 must not pass on the CLI consumer "
        "alone, because parity with the browser is the property under test",
    )
    contracts = {}
    for name, declared in supplied.items():
        require(
            isinstance(declared, dict) and declared,
            f"The UI wire contract {name} declares no fields",
        )
        for field, kind in declared.items():
            require(
                isinstance(kind, str) and kind.strip(),
                f"The UI wire contract {name}.{field} declares no type",
            )
        contracts[str(name)] = {str(k): str(v) for k, v in declared.items()}
    missing = sorted(set(SURFACES.values()) - set(contracts))
    require(
        not missing,
        "No UI wire contract was supplied for: "
        + ", ".join(missing)
        + "; the browser's declaration for those surfaces could not be checked",
    )
    # A LIST of records rather than a name-keyed map: `CredentialItem` matches the
    # redactor's sensitive-key pattern, and as a key it would be dropped from the
    # evidence -- publishing two contracts where three were checked.
    evidence["ui_contracts"] = [
        {"contract": name, "fields": sorted(fields)}
        for name, fields in sorted(contracts.items())
    ]
    return contracts


def _wire_type(value):
    """The observed wire type, in the vocabulary a TypeScript declaration uses."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _parts(declared):
    """The alternatives in a declared type, with TypeScript noise stripped."""
    text = declared.replace("Record<string, string>", "object").replace(
        "Record<string,string>", "object"
    )
    return [part.strip() for part in text.split("|") if part.strip()]


def _literals(declared):
    """The literal values a union permits, or an empty set if it is not one."""
    parts = [part for part in _parts(declared) if part != "null"]
    quoted = {part.strip("'\"") for part in parts if part[:1] in ("'", '"')}
    return quoted if quoted and len(quoted) == len(parts) else set()


def _satisfies(declared, value):
    """Is an observed value within what the browser declared for this field?

    `string | null` is a union a component handles; `string` receiving `null` is
    the crash this check exists to find. A literal union constrains the value as
    well as the type, so an unknown enum member — the shape of a server that grew
    a new state the UI cannot render — is reported too.
    """
    parts = set(_parts(declared))
    observed = _wire_type(value)
    if "any" in parts or "unknown" in parts:
        return True
    if observed == "null":
        return "null" in parts
    literals = _literals(declared)
    if literals:
        return observed == "string" and value in literals
    if observed in parts:
        return True
    # A declared object/array type in TS may be spelled as an interface name or
    # `Foo[]`; anything ending in `[]` is an array and the rest is an object.
    if observed == "array":
        return any(part.endswith("[]") for part in parts)
    if observed == "object":
        return any(
            not part.endswith("[]")
            and part not in ("string", "number", "boolean", "null")
            for part in parts
        )
    return False


def _check_ui(rows, name, contract, path):
    """Every row must satisfy the browser's declaration. Returns the problems."""
    problems = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(
                f"{path}[{index}] is {_wire_type(row)}, but the browser reads "
                f"{name} objects from it"
            )
            continue
        for field, declared in sorted(contract.items()):
            optional = field.endswith("?")
            key = field.rstrip("?")
            if key not in row:
                if optional:
                    continue
                problems.append(
                    f"{path}[{index}] has no {key!r}, which the browser declares as "
                    f"{name}.{key}: {declared}"
                )
                continue
            if not _satisfies(declared, row[key]):
                problems.append(
                    f"{path}[{index}].{key} is {_wire_type(row[key])}, but the browser "
                    f"declares {name}.{key}: {declared}; the UI would render undefined "
                    "or throw"
                )
    return problems


def _cli_reads(config, aws, bedrock, api, evidence):
    """Run the CLI's own readers. A dropped field raises inside the product.

    Each call is the real function a user's command goes through, so what is
    proved is not that some fields are present but that these consumers work
    against this deployment right now.
    """
    reads = {}

    # `connections()` filters on row["service"] and row["credential_type"]: both
    # are unguarded subscripts, so either one going missing raises here.
    rows = aws.connections(api)
    # "connection_rows", not "credentials_rows": the redactor drops keys matching
    # "credential" at any depth, so the latter would publish as "<redacted>".
    reads["connection_rows"] = len(rows)
    # `status_of`/`account_of` read the nested `scopes` map the UI also types.
    reads["statuses"] = sorted({aws.status_of(row) for row in rows})
    reads["accounts_present"] = sum(1 for row in rows if aws.account_of(row))
    # The list command's whole projection, which is what `adp aws list` prints.
    for row in rows:
        require(
            row["id"] and row["label"] is not None,
            "A connection row carries no id or label; adp aws list would fail",
        )

    # `destinations()` plus the ownership field `find_prepared` filters on.
    prepared = bedrock.destinations(api)
    require(
        isinstance(prepared, list),
        "GET /admin/bedrock-routing/destinations did not return a list; the CLI "
        "iterates it directly",
    )
    reads["destination_rows"] = len(prepared)
    for row in prepared:
        for field in ("id", "account_id", "label", "owner_org_id"):
            require(
                field in row,
                f"A destination row has no {field!r}; adp admin bedrock connect "
                "dereferences it while looking for a prepared destination",
            )
    reads["destinations_usable"] = sum(
        1 for row in prepared if row.get("usable_for_routing")
    )

    # The mapping reader, through the CLI's own scope construction so the shape
    # compared is the one the product builds rather than one written here.
    org_id = str(config.get("org_id") or "")
    if org_id:
        scope = {
            "scope_type": "org",
            "scope_id_org": org_id,
            "path": "org:" + org_id,
        }
        selected = bedrock.current_mapping(api, scope)
        reads["org_mapping_resolved"] = selected is not None
        if selected:
            destination = bedrock.destination_by_id(api, selected)
            require(
                destination["id"] == selected,
                "destination_by_id returned a different destination than the "
                "mapping named",
            )
            reads["org_mapping_destination_account"] = destination["account_id"]
    else:
        reads["org_mapping_note"] = "no org id was supplied by install_auth"

    evidence["cli_reads"] = reads
    return rows


def _ownership(config, token, rows, aws, api, evidence):
    """Rows belong to the caller, and another identity's are not disclosed."""
    user_id = str(config.get("test_user_id") or "")
    require(user_id, "No canonical user id was supplied; ownership cannot be checked")

    foreign = sorted(
        {
            str(row.get("user_id"))
            for row in rows
            if row.get("user_id") and str(row.get("user_id")) != user_id
        }
    )
    # Attached to the evidence BEFORE the first assertion, and mutated in place
    # afterwards. Built and attached at the end, a failing check would raise past
    # the assignment and publish no ownership evidence at all — losing the observed
    # status code and the leak count in exactly the case where a reader needs them.
    ownership = evidence["ownership"] = {
        "rows": len(rows),
        "scoped_to_caller": not foreign,
        # A row that does not state an owner is not a leak — the scope is the
        # server's to enforce — but it is counted so thin evidence is visible
        # rather than reading as a clean pass.
        "rows_declaring_owner": sum(1 for row in rows if row.get("user_id")),
    }
    require(
        not foreign,
        "The user-scoped credential list returned rows owned by another identity",
    )

    # A credential id that is not the caller's must not be deletable. Asserted
    # through the raw transport rather than the CLI, because `resolve_connection`
    # refuses an id absent from the caller's own list before any request is made —
    # which is the client-side half, not the server's authorization.
    absent = str(config.get("absent_connection_id") or "")
    if absent:
        status, _payload = common.api(
            config, f"{CREDENTIALS}/{absent}", token, method="DELETE", expect=None
        )
        ownership["unowned_delete_status"] = status
        require(
            status in (403, 404),
            f"Deleting a credential this identity does not own answered {status}; "
            "expected it to be refused or reported absent",
        )
        # And the CLI's own resolver must refuse it too, for the same id.
        try:
            aws.resolve_connection(api, absent)
            resolved = True
        except Exception:  # noqa: BLE001 - any refusal is the expected outcome
            resolved = False
        ownership["cli_refuses_unowned_id"] = not resolved
        require(
            not resolved,
            "The CLI resolved a connection id that does not belong to this identity",
        )


def _equivalence(config, cli, aws, api, contracts, evidence):
    """A resource this run creates through the CLI, judged by both consumers.

    `--download` is the create path that needs no credentials in the target
    account: the gateway saves a pending connection and returns its setup package.
    That makes it the one E13 can prove with the EC2 and PLATFORM fixtures its
    ledger entry declares, and it exercises the same records, endpoints and role
    template the Credentials page uses — which is the parity claim.
    """
    name = config["connection_name"]
    # The intent, recorded BEFORE the create call. R8's lesson: a connection that
    # exists but was never written down is a leak nothing can attribute, and an
    # abort between the POST and the response is exactly when that happens. The
    # name is the orchestrator's own per-run label, so it is enough to find the row
    # again even if the id never comes back.
    evidence["correlation"] = {"parity_connection_name": name}
    with tempfile.TemporaryDirectory(prefix="adp-parity-handoff-") as handoff:
        directory = Path(handoff) / "setup"
        directory.mkdir(mode=0o700)
        payload = cli.json(
            [
                "aws",
                "connect",
                "--account",
                str(config["destination_account"]),
                "--name",
                name,
                "--region",
                str(config["region"]),
                "--download",
                str(directory),
                "--yes",
            ],
            # A saved-but-unverified handoff is `pending`, which the CLI reports
            # as exit 4. That is the expected outcome of --download.
            expected=4,
        )
        require(
            payload.get("status") == "pending",
            f"adp aws connect --download reported {payload.get('status')!r}",
        )
        detail = payload.get("detail") or {}
        connection_id = detail.get("connection_id") or ""
        require(connection_id, "The created connection was not reported by the CLI")
        evidence["correlation"]["parity_connection_id"] = connection_id
        # Declared for the manifest even though this case disconnects it itself.
        # If anything below this line fails, or the whole script is killed, the
        # sweep is what removes it — and the sweep can only remove what was
        # reported. `resources` is read by the journeys stage before it grades the
        # case, so a failed case still hands over its leaks.
        evidence.setdefault("resources", []).append(["adp_connection", connection_id])
        # The handoff package is the UI's own download, so its presence is part of
        # the equivalence claim. Contents are NOT read into evidence: parameters
        # .json carries this connection's ExternalId.
        saved = sorted(item.name for item in directory.iterdir())

    # Both consumers must now see it. `existing_for` is the reuse path an
    # interrupted setup depends on, so it is the CLI's own answer to "is this the
    # same resource" rather than a comparison written here.
    listed = aws.connections(api)
    visible = [row for row in listed if row["id"] == connection_id]
    require(
        visible,
        "A connection the CLI just created is absent from the list both surfaces "
        "read; the CLI and UI do not see the same resources",
    )
    resolved = aws.resolve_connection(api, name)
    require(
        resolved["id"] == connection_id,
        "The CLI resolved a different connection for the name it just created",
    )
    require(
        aws.account_of(resolved) == str(config["destination_account"]),
        "The created connection reports a different AWS account than it was created "
        "for",
    )

    equivalence = {
        "created_via": "adp aws connect --download",
        "connection_id": connection_id,
        "handoff_files": saved,
        "visible_to_shared_list_endpoint": True,
        "resolvable_by_name": True,
        "status": aws.status_of(resolved),
    }

    # And the row must be complete under the BROWSER's declaration, not just the
    # CLI's: a field only the UI reads could be absent on a CLI-created row and
    # nothing above would have noticed.
    # The VALIDATED contract, not `config["ui_contracts"]` raw: the raw payload has
    # not been through the arrival checks in `_ui_contracts`, so reading it here
    # would let a malformed declaration reach this comparison unnoticed.
    problems = _check_ui(
        [resolved],
        SURFACES[CREDENTIALS],
        contracts[SURFACES[CREDENTIALS]],
        CREDENTIALS,
    )
    require(
        not problems,
        "A CLI-created connection is incomplete for the UI that also renders it: "
        + "; ".join(problems),
    )
    equivalence["complete_for_ui_contract"] = True
    evidence["equivalence"] = equivalence
    return connection_id


def _session(config, home):
    """Materialize the session install_auth established, in an isolated HOME."""
    directory = Path(home) / ".bedrock-gateway"
    directory.mkdir(mode=0o700, exist_ok=True)
    config_file = directory / "config.json"
    config_file.write_text(
        json.dumps({"gateway_url": config["gateway_url"].rstrip("/")})
    )
    config_file.chmod(0o600)
    tokens = directory / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                "access_token": config["access_token"],
                "id_token": config.get("id_token", ""),
                "refresh_token": config.get("refresh_token", ""),
                "expires_at": config["session_expires_at"],
            }
        )
    )
    tokens.chmod(0o600)


def execute(config, evidence):
    os.umask(0o077)
    require(
        config.get("access_token"),
        "api_parity needs the session install_auth established",
    )
    require(
        config.get("cli_path"),
        "api_parity needs the CLI install_auth left on this instance",
    )
    token = config["access_token"]

    with tempfile.TemporaryDirectory(prefix="adp-parity-") as temporary:
        home = Path(temporary)
        env = common.clean_env(
            config,
            HOME=str(home),
            AWS_CONFIG_FILE=str(home / "aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
        )
        _session(config, home)
        cli = common.Cli(Path(config["cli_path"]), env, evidence["transcript"])

        evidence["stage"] = "contracts"
        contracts = _ui_contracts(config, evidence)

        # The CLI's transport, pointed at this session. HOME is set for the whole
        # process so `adp_common.config_path()` and the auth helper resolve into
        # the isolated directory rather than the invoking user's.
        os.environ["HOME"] = str(home)
        aws, bedrock = _consumers(config, evidence)

        evidence["stage"] = "cli_consumer"
        api = aws.Api()
        # The token is supplied per request: `access_token()` would shell out to
        # bg-cognito-auth.sh, and this journey is checking API contracts, not
        # re-running the login E02 already proved.
        original = api.request

        def request(method, path, body=None, **kwargs):
            kwargs.setdefault("token", token)
            return original(method, path, body, **kwargs)

        api.request = request
        rows = _cli_reads(config, aws, bedrock, api, evidence)
        evidence["checks"].append("installed_cli_readers_ran_against_live_responses")

        evidence["stage"] = "ui_consumer"
        problems, checked = [], []
        for path, name in sorted(SURFACES.items()):
            _status, payload = common.api(config, path, token)
            items = (
                payload
                if isinstance(payload, list)
                else (payload or {}).get("items")
                if isinstance((payload or {}).get("items"), list)
                else []
            )
            require(
                isinstance(payload, list) or items,
                f"{path} returned neither a list nor an items page; both consumers "
                "iterate it",
            )
            problems.extend(_check_ui(items, name, contracts[name], path))
            # Route in a value, for the redaction reason above: `/auth/credentials`
            # as a key would be erased.
            checked.append({"route": path, "contract": name, "rows": len(items)})
        evidence["ui_checked"] = checked
        require(
            not problems,
            "The live API does not satisfy the browser's declared wire types: "
            + "; ".join(problems),
        )
        evidence["checks"].append("live_rows_satisfy_the_browser_declared_wire_types")

        evidence["stage"] = "ownership"
        _ownership(config, token, rows, aws, api, evidence)
        evidence["checks"].append("rows_are_scoped_to_the_calling_identity")

        evidence["stage"] = "equivalence"
        connection_id = _equivalence(config, cli, aws, api, contracts, evidence)
        evidence["checks"].append("cli_created_resource_is_complete_for_both_consumers")

        evidence["correlation"].update(
            parity_surfaces_checked=len(checked),
            parity_rows_checked=sum(entry["rows"] for entry in checked),
        )
        # This run's own connection, disconnected here rather than left for
        # cleanup: `disconnect` is itself a consumer (it reads the list back
        # because a 204 is indistinguishable from an unreachable gateway), so
        # removing it proves the delete path as well as leaving nothing behind.
        # Cleanup still holds the id and re-checks absence — this is the fast path,
        # not the guarantee.
        removed = cli.json(["aws", "disconnect", connection_id, "--yes"], expected=0)
        disconnected = bool((removed.get("detail") or {}).get("disconnected"))
        evidence["equivalence"]["disconnected"] = disconnected
        require(
            disconnected,
            "The connection this case created could not be disconnected through the "
            "CLI; it would be left behind for cleanup to chase",
        )
        require(
            not [row for row in aws.connections(api) if row["id"] == connection_id],
            "ADP still lists the connection after a reported disconnect",
        )
        # Only now — absence asserted against the live API through the consumer
        # itself, not inferred from a 204 the transport cannot distinguish from an
        # unreachable gateway. This is what lets the manifest close the record; a
        # journey that merely called delete must leave it pending for the sweep.
        evidence["removed"] = [["adp_connection", connection_id]]
        evidence["correlation"]["parity_connection_removed"] = True
        evidence["checks"].append("created_connection_was_removed_through_the_cli")
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    sys.exit(common.run_script(execute))
