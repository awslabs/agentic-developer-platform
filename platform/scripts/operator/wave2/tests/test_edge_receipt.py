#!/usr/bin/env python3
"""The control endpoint must come from a BOUND receipt, not an arbitrary https URL.

Root's requirement 2: "Consume account/run/nonce-bound #5836 output, not arbitrary
HTTPS."

The original checks on `--worker-control-endpoint` were `startswith("https://")` and
"does not contain the ordinary gateway's API id". Both are kept. Neither distinguishes
this run's disposable edge from another run's: in the same account, in the same
region, every fixture edge has a plausible `execute-api` hostname and none of them
contains the ordinary API id. So the single input deciding where a control-enabled
worker sends its bootstrap was the one value taken on the operator's word.

THE PRODUCER'S REAL DOCUMENT
----------------------------
The first revision of these tests invented a receipt field, and root caught it
(5809603844). `output "ownership"` carries the BINDINGS and the inventory; the
endpoint is a SEPARATE top-level `output "worker_control_endpoint"`. So
`terraform output -json ownership` can never supply an endpoint, and a consumer
reading it would have refused every real receipt while these tests passed.

`_OWNERSHIP` and `outputs()` below are therefore transcribed from #5836's actual
`modules/gateway/infra/fixture-edge/outputs.tf` at head 4bbe3d75 -- including the
fields this consumer does not read (`teardown`, `resources`, `ledger_owned_k8s`,
`allowed_caller_role_arns`, `ssm_provenance_parameter_name`), because a consumer that
only tolerates the subset it uses is one that breaks on the producer's real bytes.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/test_edge_receipt.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

WAVE2 = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "w2_edge_receipt", WAVE2 / "lib" / "edge_receipt.py")
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

NONCE = "a1b2c3d4e5f60718"
ACCOUNT = "879318057152"
REGION = "us-east-1"
ENVIRONMENT = "dev"
API_ID = "fx9k2mq1ab"
# The stage name IS the environment: #5836's main.tf:693 `stage_name = var.environment`.
ENDPOINT = (f"https://{API_ID}.execute-api.{REGION}.amazonaws.com/{ENVIRONMENT}"
            "/internal/v1/agent")
ORDINARY_API_ID = "prod7x8y9z"


def _ownership(**over) -> dict:
    """#5836's `ownership` output value, transcribed from their outputs.tf.

    Note what is NOT here: `worker_control_endpoint`. That absence is the point --
    it is a separate top-level output, and a consumer reading only this object
    cannot find an endpoint in it.
    """
    doc = {
        "run_nonce": NONCE,
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "rest_api_id": API_ID,
        "teardown": {
            "mechanism": "terraform destroy against the isolated per-run state key",
            "state_key": f"fixture-edge/{ENVIRONMENT}/{ACCOUNT}/{NONCE}/terraform.tfstate",
            "command": f"scripts/fixture-lifecycle.sh destroy --nonce {NONCE} "
                       f"--account-id {ACCOUNT}",
            "must_run_before": "the fixture Ingress/ALB deletion",
            "not_supported": "deletion by name or tag prefix",
        },
        "ledger_owned_k8s": [
            {"kind": "Ingress", "delete_by": "#3968 90-cleanup-ledger.sh (uid-gated)"},
            {"kind": "Secret", "delete_by": "#3968 90-cleanup-ledger.sh (uid-gated)"},
        ],
        "resources": [
            {"kind": "apigateway-rest-api", "type": "aws_api_gateway_rest_api",
             "id": API_ID, "name": "adp-fixture-edge-dev", "verify": "..."},
            {"kind": "apigateway-stage", "type": "aws_api_gateway_stage",
             "id": f"ags-{API_ID}-{ENVIRONMENT}", "name": ENVIRONMENT, "verify": "..."},
        ],
    }
    doc.update(over)
    return doc


def outputs(*, ownership=None, endpoint=ENDPOINT, wrapped=True, **over) -> dict:
    """A document shaped exactly like #5836's `terraform output -json` (all outputs).

    `wrapped` selects between `terraform output -json` (every output under
    {"value", "type"}) and the same document piped through a jq unwrap.
    """
    own = _ownership() if ownership is None else ownership
    # The top-level copies mirror `ownership`'s, because that is what Terraform
    # emits: both read the same resource attribute. A test that wants them to
    # DISAGREE says so explicitly via **over.
    raw = {
        "fixture_edge_enabled": True,
        "run_nonce": own.get("run_nonce", NONCE) if isinstance(own, dict) else NONCE,
        "rest_api_id": own.get("rest_api_id", API_ID) if isinstance(own, dict) else API_ID,
        "allowed_caller_role_arns": [
            f"arn:aws:iam::{ACCOUNT}:role/adp-fixture-caller"],
        "worker_control_endpoint": endpoint,
        "ssm_provenance_parameter_name": f"/adp/fixture-edge/{NONCE}/provenance-secret",
        "ownership": own,
    }
    raw.update(over)
    if not wrapped:
        return raw
    return {
        key: {"value": value, "type": "object" if isinstance(value, dict) else "string"}
        for key, value in raw.items()
    }


def receipt(**over) -> dict:
    """The normalized receipt, as the consumer sees it after load/normalize."""
    endpoint = over.pop("worker_control_endpoint", ENDPOINT)
    return er.normalize_outputs(outputs(ownership=_ownership(**over), endpoint=endpoint))


def resolve(doc=None, **over):
    kwargs = {"run_nonce": NONCE, "account_id": ACCOUNT, "region": REGION,
              "environment": ENVIRONMENT}
    kwargs.update(over)
    return er.resolve_worker_endpoint(doc if doc is not None else receipt(), **kwargs)


# ---------------------------------------------------------------------------
# the producer's real document shapes
# ---------------------------------------------------------------------------
def test_the_producers_real_outputs_document_resolves() -> None:
    """The whole point: this must work against #5836's actual emitted structure.

    Built from their outputs.tf rather than from what this consumer finds convenient,
    including the four outputs it does not read.
    """
    assert resolve(er.normalize_outputs(outputs())) == ENDPOINT


def test_the_same_document_unwrapped_resolves_identically() -> None:
    """An operator who piped it through jq has not changed a security property."""
    assert resolve(er.normalize_outputs(outputs(wrapped=False))) == ENDPOINT


def test_the_bare_ownership_receipt_is_refused_by_name_with_the_right_command() -> None:
    """The regression root found, pinned.

    `terraform output -json ownership` -- and the `ownership.json` #5836's own `apply`
    writes to its artifact directory -- carry no endpoint at all. A consumer reading
    that document can only ever report a missing field, which reads like a producer
    bug rather than like "you read the wrong document". It must name the shape and say
    what to run instead.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.normalize_outputs(_ownership())
    message = str(exc.value)
    assert "bare `ownership` receipt" in message
    assert "separate top-level" in message
    assert "terraform output -json >" in message
    # And it must warn off the file that looks like the obvious candidate.
    assert "ownership.json" in message


def test_an_ownership_object_that_gains_an_endpoint_is_accepted() -> None:
    """Adding the endpoint to `ownership` is one of the two resolutions on the table.

    If #5836 takes it, this consumer must not need a lockstep change to accept the
    improvement -- and must not be the reason the producer cannot make it.
    """
    doc = er.normalize_outputs(dict(_ownership(), worker_control_endpoint=ENDPOINT))
    assert resolve(doc) == ENDPOINT
    assert doc["_shape"] == "ownership-with-endpoint"


def test_a_document_that_disagrees_with_itself_is_refused() -> None:
    """`rest_api_id` and `run_nonce` are published twice; copies that differ are not
    a document either copy can be believed from, and choosing one would be guessing."""
    doc = outputs()
    doc["rest_api_id"]["value"] = "otherapiid1"
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.normalize_outputs(doc)
    assert "disagree with themselves" in str(exc.value)


def test_an_unrecognisable_document_is_refused_with_the_command() -> None:
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.normalize_outputs({"something_else": {"value": "x", "type": "string"}})
    assert "not recognisable" in str(exc.value)


def test_a_scalar_ownership_field_is_refused() -> None:
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.normalize_outputs(outputs(ownership="a string"))
    assert "not an object" in str(exc.value)


# ---------------------------------------------------------------------------
# the accepting case, first: refusals only mean something if something passes
# ---------------------------------------------------------------------------
def test_a_bound_receipt_yields_its_endpoint() -> None:
    assert resolve() == ENDPOINT


def test_the_endpoint_comes_from_the_receipt_and_not_from_the_flag() -> None:
    """Order matters: reading the flag and merely comparing it would let a missing
    receipt field silently waive the comparison."""
    doc = receipt()
    del doc["worker_control_endpoint"]
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.resolve_worker_endpoint(
            doc, run_nonce=NONCE, account_id=ACCOUNT, region=REGION,
            environment=ENVIRONMENT, supplied_endpoint=ENDPOINT)
    assert "publish no worker_control_endpoint" in str(exc.value)


def test_an_agreeing_flag_is_accepted_as_a_cross_check() -> None:
    assert resolve(supplied_endpoint=ENDPOINT) == ENDPOINT


# ---------------------------------------------------------------------------
# the binding fields
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,wrong", [
    ("run_nonce", "ffffffffffffffff"),
    ("account_id", "210987654321"),
    ("region", "eu-west-1"),
    ("environment", "staging"),
])
def test_a_receipt_from_another_run_is_refused(field, wrong) -> None:
    """Each binding axis independently. A plausible edge in the same account is the
    realistic failure, not an obviously wrong URL."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(**{field: wrong}))
    assert field in str(exc.value)
    assert "does not belong to this run" in str(exc.value)


@pytest.mark.parametrize("field", ["run_nonce", "account_id", "region", "environment"])
def test_an_absent_binding_field_is_refused_not_skipped(field) -> None:
    """Absence must not read as agreement.

    This is the shape that actually survives review: a receipt with the field simply
    missing looks like an older artifact rather than a foreign one, so the tempting
    behaviour is to skip the check. Skipping it accepts every foreign receipt.
    """
    own = _ownership()
    del own[field]
    doc = er.normalize_outputs(outputs(ownership=own))
    # `run_nonce` and `rest_api_id` are also published top-level; drop the copy too,
    # since the question here is a receipt with the field genuinely absent.
    doc.pop(field, None)
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(doc)
    assert "records no " + field in str(exc.value)


def test_an_empty_binding_field_is_refused() -> None:
    """`""` is what Terraform emits for a disabled component, not a wildcard."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(run_nonce=""))
    assert "records no run_nonce" in str(exc.value)


def test_the_bindings_are_checked_before_the_endpoint_is_read() -> None:
    """A foreign receipt must not get to supply the value that authorises the worker.

    Its endpoint here is perfectly well-formed and self-consistent -- so a check
    ordered after the endpoint extraction would return it and only then notice.
    """
    foreign = receipt(run_nonce="ffffffffffffffff")
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(foreign)
    assert "does not belong to this run" in str(exc.value)
    # And nothing about the endpoint is mentioned, because it was never consulted.
    assert "worker_control_endpoint" not in str(exc.value)


@pytest.mark.parametrize("field", ["run_nonce", "account_id", "region", "environment"])
def test_the_caller_must_supply_what_it_is_binding_against(field) -> None:
    """An empty expectation is not a satisfied one.

    Each axis separately, because the tempting implementation skips comparison when
    the expectation is falsy -- and `--region ""` would then waive the region check
    while the region is half of the hostname the endpoint is bound to.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(**{field: ""})
    assert f"no {field} was supplied" in str(exc.value)
    assert "must not waive the check" in str(exc.value)


# ---------------------------------------------------------------------------
# the URL itself -- parsed, not substring-matched
# ---------------------------------------------------------------------------
# Root executed all three of these against the previous revision with
# rest_api_id=abc123xyz0 and all three were ACCEPTED, because the check was
# `api_id in endpoint`. The production bootstrap client does not require an AWS
# host, so nothing downstream would have caught any of them either.
@pytest.mark.parametrize("bad,why", [
    (f"https://{API_ID}.unrelated.example/dev/internal/v1/agent",
     "the id as a label on a host that is not AWS at all"),
    (f"https://{API_ID}.execute-api.eu-west-1.amazonaws.com/dev/internal/v1/agent",
     "another region's execute-api host"),
    (f"https://other.execute-api.{REGION}.amazonaws.com/{API_ID}/internal/v1/agent",
     "the id sitting in the PATH of a different host"),
    (f"https://{API_ID}-x.execute-api.{REGION}.amazonaws.com/dev/internal/v1/agent",
     "the id as a prefix of a longer label"),
    (f"https://sub.{API_ID}.execute-api.{REGION}.amazonaws.com/dev/internal/v1/agent",
     "the id as a middle label"),
])
def test_a_url_merely_containing_the_api_id_is_refused(bad, why) -> None:
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "addresses host" in str(exc.value), why
    assert "EXACT host" in str(exc.value)


def test_userinfo_cannot_disguise_the_real_host() -> None:
    """`https://<expected-host>@evil.example/...` has the expected host as its
    USERNAME. A host comparison made before this check compares the wrong half."""
    bad = (f"https://{API_ID}.execute-api.{REGION}.amazonaws.com@evil.example"
           f"/{ENVIRONMENT}/internal/v1/agent")
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "userinfo" in str(exc.value)


def test_an_explicit_port_is_refused() -> None:
    bad = (f"https://{API_ID}.execute-api.{REGION}.amazonaws.com:8443"
           f"/{ENVIRONMENT}/internal/v1/agent")
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "explicit port" in str(exc.value)


@pytest.mark.parametrize("path,why", [
    (f"/{ENVIRONMENT}/internal/v1/agent/", "a trailing slash doubles when /bootstrap is appended"),
    ("/internal/v1/agent", "no stage reaches no deployed stage"),
    (f"/{ENVIRONMENT}/internal/v1", "a truncated route prefix"),
    ("/prod/internal/v1/agent", "another stage is another deployment"),
    (f"/{ENVIRONMENT}/internal/v1/agent/bootstrap", "the client appends /bootstrap itself"),
])
def test_the_path_must_be_exactly_the_edge_contract(path, why) -> None:
    bad = f"https://{API_ID}.execute-api.{REGION}.amazonaws.com{path}"
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "has path" in str(exc.value), why


def test_a_query_string_or_fragment_is_refused() -> None:
    for suffix in ("?x=1", "#frag"):
        with pytest.raises(er.EdgeReceiptError) as exc:
            resolve(receipt(worker_control_endpoint=ENDPOINT + suffix))
        assert "delimiter" in str(exc.value)


# ---------------------------------------------------------------------------
# root executed these four against the component checks and all four were
# ACCEPTED (5810697519). They are the empty-delimiter cases: urlsplit reports an
# empty query, fragment and port for each, so a check on the PARSED value answers
# "none present" while the delimiter is still in the string. RunIdentitySession
# then appends '/bootstrap' after it.
@pytest.mark.parametrize("suffix,consequence", [
    ("?", "/dev/internal/v1/agent?/bootstrap -- the bootstrap path becomes a query string"),
    ("#", "/dev/internal/v1/agent#/bootstrap -- the bootstrap path becomes a fragment"),
    ("?#", "both delimiters, each individually invisible to a parsed-value check"),
])
def test_an_empty_query_or_fragment_delimiter_is_refused(suffix, consequence) -> None:
    bad = ENDPOINT + suffix
    # The mechanism first, so the test explains WHY a parsed-value check missed it.
    parts = urlsplit(bad)
    assert not parts.query and not parts.fragment, \
        "if urlsplit reported these, `if parts.query or parts.fragment` would have caught it"
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "delimiter" in str(exc.value), consequence
    assert "/bootstrap" in str(exc.value), \
        "the refusal must name the consequence, not just the character"


def test_an_empty_port_delimiter_is_refused() -> None:
    """`…amazonaws.com:/dev/…` -- a port delimiter with no port.

    `parts.port` is None, so the explicit-port check cannot see it; the host parses
    correctly, so the host check passes. Accepted by every component check.
    """
    bad = (f"https://{API_ID}.execute-api.{REGION}.amazonaws.com:"
           f"/{ENVIRONMENT}/internal/v1/agent")
    parts = urlsplit(bad)
    assert parts.port is None and parts.hostname == f"{API_ID}.execute-api.{REGION}.amazonaws.com", \
        "the premise: the port check and the host check both pass on this string"
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "bare ':'" in str(exc.value)


def test_the_endpoint_must_equal_the_one_the_receipt_implies() -> None:
    """The backstop, stated on its own.

    Each named check refuses one specific defect, so a list of them passes anything
    not on the list -- which is exactly how the delimiters got through. This asserts
    the final equality exists and fires, using a difference no component check names:
    an uppercase scheme, which urlsplit normalises to https, leaving every parsed
    component correct while the string is not the producer's.
    """
    bad = "HTTPS" + ENDPOINT[len("https"):]
    parts = urlsplit(bad)
    assert parts.scheme == "https", "urlsplit lowercases the scheme, so the scheme check passes"
    assert parts.hostname and parts.path == f"/{ENVIRONMENT}/internal/v1/agent"
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=bad))
    assert "imply exactly" in str(exc.value)


def test_the_valid_endpoint_still_resolves_unchanged() -> None:
    """The point of the delimiter and equality checks is to refuse edited values, not
    to make the producer's own output unusable. Asserted beside them so a tightening
    that breaks the legitimate handoff fails here."""
    assert resolve(receipt()) == ENDPOINT
    assert resolve(receipt()) == er.expected_endpoint(
        api_id=API_ID, region=REGION, environment=ENVIRONMENT)


def test_the_stage_path_follows_the_environment_not_a_hardcoded_dev() -> None:
    """The stage IS the environment, so a staging run's endpoint is /staging/...

    A hardcoded "dev" would refuse every legitimate non-dev run while accepting a
    dev-stage URL in a staging run -- the wrong deployment, admitted.
    """
    staging_endpoint = (f"https://{API_ID}.execute-api.{REGION}.amazonaws.com"
                        "/staging/internal/v1/agent")
    doc = er.normalize_outputs(outputs(
        ownership=_ownership(environment="staging"), endpoint=staging_endpoint))
    assert resolve(doc, environment="staging") == staging_endpoint


def test_the_expected_endpoint_helper_matches_the_producers_format() -> None:
    """Pinned against #5836's outputs.tf interpolation, so a drift in either is a
    failing test rather than a runtime refusal on a correct receipt."""
    assert er.expected_endpoint(
        api_id=API_ID, region=REGION, environment=ENVIRONMENT) == ENDPOINT


# ---------------------------------------------------------------------------
# the endpoint and the inventory must describe ONE api
# ---------------------------------------------------------------------------
def test_an_endpoint_naming_a_different_api_than_the_inventory_is_refused() -> None:
    """The load-bearing case for reading a receipt at all.

    A document can pair a correct-looking resource inventory with an endpoint
    pointing elsewhere. Both halves are individually well-formed: the bindings match,
    the URL is https, the host is a real execute-api host in the right region.
    Nothing else here would notice -- and tearing down what the inventory lists would
    leave the worker's actual control plane running and unowned.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=(
            f"https://someother.execute-api.{REGION}.amazonaws.com"
            f"/{ENVIRONMENT}/internal/v1/agent")))
    assert "addresses host 'someother." in str(exc.value)
    assert API_ID in str(exc.value)


def test_a_receipt_with_no_api_id_cannot_link_its_halves() -> None:
    doc = receipt()
    doc["rest_api_id"] = ""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(doc)
    assert "no rest_api_id" in str(exc.value)


def test_a_disabled_edge_publishes_no_endpoint_and_is_refused() -> None:
    """#5836 emits "" for every output when fixture_edge_enabled is false.

    That is the common real case -- the edge was never applied -- and it must read as
    "there is no endpoint", never as an endpoint of "".
    """
    doc = er.normalize_outputs(outputs(
        ownership=_ownership(rest_api_id=""), endpoint="",
        fixture_edge_enabled=False, rest_api_id=""))
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(doc)
    assert "never applied" in str(exc.value)


def test_a_non_https_endpoint_in_the_receipt_is_still_refused() -> None:
    """The receipt is bound, not infallible. run_identity.py rejects other schemes."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(receipt(worker_control_endpoint=(
            f"http://{API_ID}.execute-api.{REGION}.amazonaws.com"
            f"/{ENVIRONMENT}/internal/v1/agent")))
    assert "not https" in str(exc.value)


def test_the_ordinary_edge_is_refused_even_when_a_receipt_names_it() -> None:
    """A receipt saying so does not make production a fixture.

    Kept as a check on this side because the consequence -- a control-enabled worker
    bootstrapping against live traffic -- is the one outcome this tooling refused
    outright before #5836 existed.
    """
    prod = (f"https://{ORDINARY_API_ID}.execute-api.{REGION}.amazonaws.com"
            f"/{ENVIRONMENT}/internal/v1/agent")
    doc = er.normalize_outputs(outputs(
        ownership=_ownership(rest_api_id=ORDINARY_API_ID), endpoint=prod,
        rest_api_id=ORDINARY_API_ID))
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(doc, ordinary_api_id=ORDINARY_API_ID)
    assert "ORDINARY" in str(exc.value)


def test_a_disagreeing_flag_refuses_both_rather_than_choosing() -> None:
    """One of the two is not this run's edge, and guessing which IS the defect."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolve(supplied_endpoint=(
            f"https://typo.execute-api.{REGION}.amazonaws.com"
            f"/{ENVIRONMENT}/internal/v1/agent"))
    assert "Refusing both rather than choosing" in str(exc.value)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def test_a_real_outputs_file_loads_and_resolves(tmp_path) -> None:
    path = tmp_path / "edge-outputs.json"
    path.write_text(json.dumps(outputs()))
    doc = er.load_receipt(path)
    assert doc["rest_api_id"] == API_ID
    assert resolve(doc) == ENDPOINT


def test_a_missing_document_is_a_refusal_with_the_command_to_produce_it(tmp_path) -> None:
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.load_receipt(tmp_path / "absent.json")
    message = str(exc.value)
    assert "terraform output -json >" in message
    # Specifically NOT the endpoint-less command the previous revision documented.
    assert "terraform output -json ownership" not in message


def test_an_unreadable_document_never_falls_back_to_the_flag(tmp_path) -> None:
    """"Could not check" must not render as "checked and fine".

    The same rule 10- already applies to its SSM lookup of the ordinary API id.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.load_receipt(broken)
    assert "Refusing rather than falling back" in str(exc.value)


def test_a_document_that_is_not_an_object_is_refused(tmp_path) -> None:
    listy = tmp_path / "list.json"
    listy.write_text("[]")
    with pytest.raises(er.EdgeReceiptError):
        er.load_receipt(listy)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
def test_provenance_records_that_the_endpoint_came_from_a_bound_receipt() -> None:
    """Otherwise a receipt-derived endpoint and a flag-supplied one look identical.

    The fixture's own record is what a later reader has; "the endpoint was verified"
    is not observable from the URL itself.
    """
    prov = er.endpoint_provenance(receipt(), "/ev/edge-outputs.json")
    assert prov["source"] == "5836-fixture-edge-outputs"
    assert prov["rest_api_id"] == API_ID
    assert prov["bound_to"] == {
        "run_nonce": NONCE, "account_id": ACCOUNT,
        "region": REGION, "environment": ENVIRONMENT,
    }
    assert prov["receipt_path"] == "/ev/edge-outputs.json"


def test_provenance_records_which_url_components_were_checked() -> None:
    """A reader cannot otherwise distinguish "the host matched exactly" from "the id
    appeared somewhere in the URL" -- and that distinction is the whole finding."""
    prov = er.endpoint_provenance(receipt(), "/ev/edge-outputs.json")
    checked = " ".join(prov["url_checked"])
    assert "host is exactly" in checked
    assert "path is exactly" in checked
    assert "userinfo" in checked
    assert prov["document_shape"] == "terraform-output-json"


# ---------------------------------------------------------------------------
# the CLI seam the shell uses
# ---------------------------------------------------------------------------
def _cli_args(path, **over) -> list[str]:
    args = {"--run-nonce": NONCE, "--account-id": ACCOUNT, "--region": REGION,
            "--environment": ENVIRONMENT}
    args.update(over)
    out = ["--receipt", str(path)]
    for flag, value in args.items():
        out += [flag, value]
    return out


def test_the_cli_prints_only_the_endpoint_on_stdout(tmp_path, capsys) -> None:
    """The shell captures stdout directly, so any extra line would corrupt the value."""
    path = tmp_path / "edge-outputs.json"
    path.write_text(json.dumps(outputs()))
    assert er.main(_cli_args(path)) == 0
    assert capsys.readouterr().out.strip() == ENDPOINT


def test_the_cli_exits_nonzero_and_says_why_on_a_foreign_receipt(tmp_path, capsys) -> None:
    path = tmp_path / "edge-outputs.json"
    path.write_text(json.dumps(outputs(
        ownership=_ownership(run_nonce="ffffffffffffffff"))))
    assert er.main(_cli_args(path)) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == "", "nothing may reach stdout: the shell captures it"
    assert "does not belong to this run" in captured.err


def test_the_cli_refuses_the_endpoint_less_ownership_document(tmp_path, capsys) -> None:
    """The operator-facing half of root's finding: following #5836's runbook produces
    `ownership.json`, and the refusal has to send them to the right command."""
    path = tmp_path / "ownership.json"
    path.write_text(json.dumps(_ownership()))
    assert er.main(_cli_args(path)) == 1
    assert "terraform output -json >" in capsys.readouterr().err


def test_the_cli_writes_provenance_when_asked(tmp_path) -> None:
    path = tmp_path / "edge-outputs.json"
    path.write_text(json.dumps(outputs()))
    prov = tmp_path / "nested" / "provenance.json"
    assert er.main(_cli_args(path, **{"--provenance-out": str(prov)})) == 0
    assert json.loads(prov.read_text())["source"] == "5836-fixture-edge-outputs"


def test_the_cli_writes_no_provenance_for_a_refused_receipt(tmp_path) -> None:
    """A provenance file for an endpoint that was never accepted would be a record of
    a binding that does not hold."""
    path = tmp_path / "edge-outputs.json"
    path.write_text(json.dumps(outputs(ownership=_ownership(account_id="210987654321"))))
    prov = tmp_path / "provenance.json"
    assert er.main(_cli_args(path, **{"--provenance-out": str(prov)})) == 1
    assert not prov.exists()
