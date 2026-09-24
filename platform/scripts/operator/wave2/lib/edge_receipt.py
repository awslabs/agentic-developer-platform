#!/usr/bin/env python3
"""Resolve the fixture worker's control endpoint from #5836's BOUND receipt.

Root's requirement 2, final clause: "Consume account/run/nonce-bound #5836 output,
not arbitrary HTTPS."

WHAT WAS WRONG
--------------
`10-create-fixture.sh --worker-control-endpoint <url>` accepted any string and
checked two things about it: that it starts with ``https://``, and that it does not
contain the ordinary gateway's API id. Both are real checks and both are kept. But
together they establish only that the value is *not* one specific known-bad edge --
and "not production" is a very long way from "the disposable edge this run's ledger
is bound to".

Everything else in this tooling binds evidence to identities. The endpoint, which is
the single input that decides WHERE a control-enabled worker sends its bootstrap, was
the one value taken on the operator's word. A typo, a stale shell variable, or a URL
copied from a colleague's run all satisfy both checks, and the worker then bootstraps
against an edge belonging to some other run -- in the same account, under the same
region, with a completely plausible-looking execute-api hostname.

TWO CORRECTIONS ROOT FOUND IN THE FIRST ATTEMPT AT THIS (5809603844)
--------------------------------------------------------------------
1. **The document this consumed did not exist.** The first revision read
   ``terraform output -json ownership`` and took ``worker_control_endpoint`` out of
   it. #5836's ``output "ownership"`` carries no such field -- the endpoint is a
   SEPARATE top-level output (verified against their head 4bbe3d75,
   ``modules/gateway/infra/fixture-edge/outputs.tf``). So on a real receipt this
   refused every time, and only passed because the tests invented the field. A
   consumer that has never been run against the producer's real bytes is not
   integrated with it, whatever its tests say.

   The fix is to consume the whole outputs document -- ``terraform output -json``
   with no name -- which carries ``ownership`` (the bindings) AND
   ``worker_control_endpoint`` (the endpoint) AND top-level ``rest_api_id``. The two
   halves are then cross-checked against each other, because a document can pair a
   correct inventory with an endpoint describing something else. An ownership-only
   document is refused with the command that produces the right one, rather than
   silently missing its endpoint.

2. **"The API id appears in the URL" does not bind the URL.** Substring containment
   accepted all three of these against a receipt whose ``rest_api_id`` was
   ``abc123xyz0``::

       https://abc123xyz0.unrelated.example                      (not AWS at all)
       https://abc123xyz0.execute-api.eu-west-1.amazonaws.com    (another region)
       https://other.execute-api.us-east-1.amazonaws.com/abc123xyz0  (id in the PATH)

   The URL is now parsed and every component checked against the producer's
   contract: the exact ``<api-id>.execute-api.<region>.amazonaws.com`` host, the
   exact ``/<stage>/internal/v1/agent`` path (the stage IS the environment --
   #5836's ``main.tf`` sets ``stage_name = var.environment``), and no userinfo,
   port, query or fragment. The production bootstrap client does not enforce an AWS
   host, so nothing downstream closes this gap: this is the only place it is closed.

3. **A list of component checks omits whatever nobody thought of (5810697519).** The
   component checks above accepted the correct URL with a bare ``?`` or ``#`` glued on,
   and with an empty ``:`` where a port would go::

       https://<id>.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent?
       https://<id>.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent#
       https://<id>.execute-api.us-east-1.amazonaws.com:/dev/internal/v1/agent

   Not an exotic input: ``urlsplit`` reports an EMPTY query/fragment/port for each, so
   every check that asks "is there a query string?" answers no while the delimiter is
   still sitting in the string. ``RunIdentitySession`` then appends ``/bootstrap``, and
   the request line becomes ``/dev/internal/v1/agent?/bootstrap`` -- the bootstrap path
   is now a query string, the edge has no route for it, and the worker's registration
   fails in a way that reads as an edge fault rather than a malformed endpoint.

   Fixed twice over, deliberately. The delimiters are refused by name, because an
   operator holding a URL with a stray ``?`` needs to be told that; and then the whole
   string is compared for EQUALITY against ``expected_endpoint()``, which is the one URL
   the receipt's own fields imply. Equality cannot omit a component. The named checks
   run first so the explanation stays specific, and the equality runs last so anything
   they do not anticipate still fails closed.

WHAT THIS REQUIRES
------------------
  * the receipt's ``run_nonce`` must be this run's nonce, from the shared ledger;
  * its ``account_id`` must be the account this session actually resolved;
  * its ``region`` and ``environment`` must be this run's -- ``environment`` because
    it is part of #5836's per-run state contract AND because it is the API stage, so
    a disagreement means the path below is wrong too;
  * the endpoint must be the document's ``worker_control_endpoint``, and must parse
    to exactly the host and path that the receipt's own ``rest_api_id``, region and
    environment imply.

WHY THIS IS NOT A WEAKER FORM OF TRUST
--------------------------------------
The receipt is produced by `terraform output` from the per-run isolated state that
#5836's `apply` wrote -- it is not authored by this process, and not by the worker.
It is the same class of artifact as the expected-identity document: written outside
the thing being constrained, describing server-assigned identities. A receipt from
another run is refused rather than accepted with a warning, because the failure it
prevents (a protected worker bootstrapping against an edge this run does not own and
cannot tear down) is not one a warning mitigates.

This module performs NO cloud calls and NO mutation. It reads two JSON files.
"""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The fields the receipt must agree with this run about, before anything is taken
# OUT of it. `environment` is here for two reasons: #5836 binds its per-run state
# key on it, and it is the API Gateway stage name, so it also decides the path.
RECEIPT_BINDING_FIELDS = ("run_nonce", "account_id", "region", "environment")

# The endpoint contract, traced from #5836's outputs.tf rather than assumed:
#   https://${rest_api.id}.execute-api.${region}.amazonaws.com/${stage.name}/internal/v1/agent
# with main.tf:693 `stage_name = var.environment`.
EXECUTE_API_SUFFIX = "execute-api.{region}.amazonaws.com"
CONTROL_PATH_SUFFIX = "/internal/v1/agent"

# #5836's ALB-source output (their e3c7e6f1), and the fields of it this consumer
# reads. The fixture gateway's NetworkPolicy admits callers by namespaceSelector,
# which cannot match an ALB: with `target-type: ip` the load balancer connects from
# its OWN elastic network interfaces, which belong to no pod and no namespace.
#
# The failure mode is why this is a hard requirement rather than a refinement.
# NOTHING IN TERRAFORM OR KUBERNETES ERRORS: the plan applies and the Ingress
# reconciles, because a NetworkPolicy is not a validation of anything reachable. The
# worker's bootstrap handshake then never completes, and the run reads as "the
# protected worker failed its bootstrap" -- the very conclusion Wave 2 exists to
# establish or refute, reached from a networking artefact instead.
#
# WHAT THIS IS *NOT*, corrected after root's review of #5836's e3c7e6f1: the ALB does
# NOT keep reporting its targets healthy. With `target-type: ip` the health check
# takes the SAME path to the same pod port as a real request, so a policy that denies
# the ALB denies the health check too and the targets go UNHEALTHY. An earlier
# revision of this module claimed both things at once (copied from the producer's
# description, which root also asked #5836 to correct). The distinction is practical:
# unhealthy targets are a real signal an operator can see, and the failure is
# *diagnosable* there -- it is the bootstrap timeout downstream that is misleading, so
# that is where this refusal argues, not from a false claim of a silent green ALB.
ALB_SOURCE_FIELD = "fixture_alb_network_policy_source"

# The port the fixture gateway's pods actually serve, and therefore the only port an
# ALB ingress rule may name. Traced to `render_gateway`, which renders the fixture
# Service as 80 -> 8080 mirroring the ordinary one, and asserted against #5836's
# published `container_port` rather than trusted from it: root found that a document
# reporting 8081 satisfied every check the first revision made (an integer, and
# different from the listener port) while rendering a rule to a port nothing serves.
FIXTURE_CONTAINER_PORT = 8080

# `container_port` and `alb_listener_port` are published under separate names
# deliberately: the ALB listens on 80 and connects to the pod on 8080, so a rule
# naming the listener port blocks precisely the flow under test. Only the container
# port is read here, and a document whose two ports are equal is refused -- #5836
# asserts they differ, so equality means the value was edited or misread.
ALB_SOURCE_REQUIRED_FIELDS = ("source_cidrs", "container_port", "alb_listener_port")

# What `terraform output -json` wraps each output in. Recorded as a set so an
# unfamiliar extra key does not cause a value to be unwrapped by accident.
_TF_WRAPPER_KEYS = {"value", "type", "sensitive"}

_PRODUCE_OUTPUTS_CMD = (
    "    cd modules/gateway/infra/fixture-edge\n"
    "    terraform output -json > $EVIDENCE_DIR/edge-outputs.json"
)


class EdgeReceiptError(Exception):
    """The endpoint could not be bound to this run's edge. Never a warning."""


# ---------------------------------------------------------------------------
# reading the producer's document
# ---------------------------------------------------------------------------
def _unwrap(value: Any) -> Any:
    """Unwrap one `terraform output -json` entry: {"value": X, "type": ...} -> X."""
    if (
        isinstance(value, dict)
        and "value" in value
        and set(value).issubset(_TF_WRAPPER_KEYS)
    ):
        return value["value"]
    return value


def normalize_outputs(doc: Any, *, path: str | Path = "<receipt>") -> dict[str, Any]:
    """Flatten #5836's outputs into the fields this consumer needs, or raise.

    Accepts the shapes the producer can actually emit:

      * ``terraform output -json`` -- every output wrapped in {"value", "type"},
        including ``ownership`` and ``worker_control_endpoint``. This is the one the
        runbook and this tooling ask for, because it is the only shape that carries
        BOTH the bindings and the endpoint.
      * the same document already unwrapped (an operator who piped it through jq).
      * a bare ``ownership`` object that ALSO carries ``worker_control_endpoint`` --
        which it does not today, but #5836 may add it (that is one of the two
        resolutions under discussion), and a consumer that refuses the producer's
        improvement would have to be changed in lockstep for no reason.

    A bare ``ownership`` object WITHOUT an endpoint is refused by name, with the
    command that produces the right document. That is the shape #5836's `apply`
    writes to its artifact directory today (`ownership.json`), so this refusal is
    the one an operator following their runbook will actually hit, and it has to say
    what to run instead rather than reporting a missing field.
    """
    if not isinstance(doc, dict):
        raise EdgeReceiptError(
            f"the fixture-edge outputs at {path} must be a JSON object, got "
            f"{type(doc).__name__}."
        )

    flat = {key: _unwrap(value) for key, value in doc.items()}
    ownership = flat.get("ownership")

    if isinstance(ownership, dict):
        # The full outputs document: bindings come from `ownership`, the endpoint
        # from its own top-level output.
        receipt = dict(ownership)
        receipt["worker_control_endpoint"] = flat.get("worker_control_endpoint")
        # The ALB's own source addresses, also a separate top-level output. Carried
        # through here for the same reason as the endpoint: `terraform output -json
        # ownership` cannot supply it, so a consumer reading only `ownership` would
        # render a fixture policy that silently denies the edge.
        receipt[ALB_SOURCE_FIELD] = flat.get(ALB_SOURCE_FIELD)
        shape = "terraform-output-json"
        # The two halves must describe ONE api and ONE run. `rest_api_id` and
        # `run_nonce` are published BOTH top-level and inside `ownership`, so a
        # document whose copies disagree is not a document either half can be
        # trusted from -- and picking one would be choosing which to believe.
        for field in ("rest_api_id", "run_nonce"):
            top = flat.get(field)
            inner = ownership.get(field)
            if top and inner and top != inner:
                raise EdgeReceiptError(
                    f"the fixture-edge outputs at {path} disagree with themselves: "
                    f"top-level {field}={top!r} but ownership.{field}={inner!r}. "
                    "Those are two copies of one fact, so this document does not "
                    "describe a single edge and neither copy can be used. Re-read the "
                    "outputs from the run's state rather than editing them."
                )
    elif ownership is not None:
        raise EdgeReceiptError(
            f"the fixture-edge outputs at {path} carry an `ownership` field that is "
            f"not an object ({type(ownership).__name__}). #5836 publishes it as the "
            "run's inventory receipt; a scalar cannot be bound to a run."
        )
    elif flat.get("worker_control_endpoint"):
        # A bare ownership object that already carries the endpoint.
        receipt = dict(flat)
        shape = "ownership-with-endpoint"
    elif any(key in flat for key in ("teardown", "resources", "ledger_owned_k8s")):
        raise EdgeReceiptError(
            f"the document at {path} is #5836's bare `ownership` receipt. It carries "
            "the bindings (run_nonce/account_id/region/environment) but NOT the "
            "control endpoint -- `worker_control_endpoint` is a separate top-level "
            "output, so `terraform output -json ownership` can never supply it.\n"
            "Read the whole outputs document instead:\n"
            f"{_PRODUCE_OUTPUTS_CMD}\n"
            "Note this is NOT the `ownership.json` #5836's `apply` writes to its "
            "artifact directory: that file is the same endpoint-less shape."
        )
    else:
        raise EdgeReceiptError(
            f"the document at {path} is not recognisable as #5836's fixture-edge "
            "outputs: it carries neither an `ownership` object nor a "
            "`worker_control_endpoint`. Produce it with:\n"
            f"{_PRODUCE_OUTPUTS_CMD}"
        )

    # `rest_api_id` lives top-level as well, and on the full-outputs shape that is
    # where the runbook's own verification step reads it from.
    if not receipt.get("rest_api_id") and flat.get("rest_api_id"):
        receipt["rest_api_id"] = flat["rest_api_id"]
    receipt["_shape"] = shape
    return receipt


def load_receipt(path: str | Path) -> dict[str, Any]:
    """Read #5836's fixture-edge outputs, refusing anything unreadable.

    An unreadable receipt is a refusal and not a fallback to the flag. "Could not
    check" must never render as "checked and fine" -- the same rule 10- already
    applies to the ordinary-API-id lookup in SSM.
    """
    path = Path(path)
    if not path.is_file():
        raise EdgeReceiptError(
            f"no fixture-edge outputs document at {path}. It is produced from #5836's "
            "applied per-run state:\n"
            f"{_PRODUCE_OUTPUTS_CMD}\n"
            "Without it the control endpoint is an unverified string, and an endpoint "
            "nobody can tie to this run is not an isolated fixture."
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeReceiptError(
            f"the fixture-edge outputs at {path} are not valid JSON ({exc}). Refusing "
            "rather than falling back to the supplied endpoint: a document that cannot "
            "be read cannot establish which edge this is."
        ) from exc
    return normalize_outputs(doc, path=path)


# ---------------------------------------------------------------------------
# binding the URL itself
# ---------------------------------------------------------------------------
def expected_endpoint(*, api_id: str, region: str, environment: str) -> str:
    """The one endpoint this receipt's own fields imply."""
    host = f"{api_id}." + EXECUTE_API_SUFFIX.format(region=region)
    return f"https://{host}/{environment}{CONTROL_PATH_SUFFIX}"


def check_endpoint_url(
    endpoint: str, *, api_id: str, region: str, environment: str
) -> None:
    """Refuse any endpoint that is not exactly the edge contract's URL.

    Every component is checked, because the failure mode is a URL that LOOKS like
    the fixture edge. Substring containment of the API id accepted a non-AWS host,
    another region's execute-api host, and the id appearing in the path of an
    entirely different host -- all three verified by root against the previous
    revision. The production bootstrap client (`lib/run_identity.py`) requires https
    with a bare host but does not require an AWS host, so nothing downstream would
    have caught any of them.

    The component checks then run out and the whole string is compared for equality
    against `expected_endpoint()`. That is not redundancy: a list of component checks
    passes anything it does not mention, which is how `…/agent?` and `…/agent#` were
    accepted (`urlsplit` reports an EMPTY query and fragment for both, so asking "is
    there a query string?" answers no while the delimiter remains in the string, and
    `RunIdentitySession` appends '/bootstrap' AFTER it). Equality has nothing to omit.
    The named checks keep running first because each produces a specific explanation,
    and an operator holding a subtly wrong URL needs that rather than "not equal".
    """
    want_host = f"{api_id}." + EXECUTE_API_SUFFIX.format(region=region)
    want_path = f"/{environment}{CONTROL_PATH_SUFFIX}"
    canonical = expected_endpoint(api_id=api_id, region=region, environment=environment)

    try:
        parts = urlsplit(endpoint)
    except ValueError as exc:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint {endpoint!r} cannot be parsed as a "
            f"URL ({exc}). An endpoint that cannot be parsed cannot be bound."
        ) from exc

    if parts.scheme != "https":
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint {endpoint!r} is not https "
            f"(scheme {parts.scheme!r}). lib/run_identity.py rejects any other scheme "
            "before attempting a bootstrap, and a control plane carrying a workload "
            "identity token must not be reachable in clear text."
        )

    # Userinfo first: `https://<api-id>.execute-api.us-east-1.amazonaws.com@evil.example`
    # has the expected host as its USERNAME and `evil.example` as its host. Any
    # host comparison made before this one is comparing the wrong half.
    if "@" in parts.netloc:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint {endpoint!r} contains userinfo "
            "(an '@' in the authority). #5836 emits none, and userinfo is how a URL is "
            f"made to read like {want_host} while actually addressing "
            f"{parts.hostname!r}."
        )

    # An EMPTY port delimiter -- `…amazonaws.com:/dev/…` -- reports parts.port as None,
    # so the check below cannot see it. Refused on the raw authority for the same reason
    # the delimiters below are: #5836 emits none, so its presence means this string was
    # edited after the producer wrote it, and the one thing this function establishes is
    # that it was not.
    if parts.netloc.endswith(":"):
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint {endpoint!r} ends its authority with a "
            "bare ':' -- a port delimiter with no port. urllib reports no port for it, so a "
            "port check cannot see it, and #5836 emits neither. A value that does not match "
            "the producer's output byte for byte is not the producer's output."
        )

    try:
        port = parts.port
    except ValueError as exc:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint {endpoint!r} has an unparseable "
            f"port ({exc})."
        ) from exc
    if port is not None:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint {endpoint!r} specifies an explicit "
            f"port ({port}). #5836 emits none, so this value is not the output it "
            "claims to be -- and a port is how execute-api's hostname is reused to "
            "reach something else."
        )

    host = (parts.hostname or "").lower()
    if host != want_host:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint addresses host {host!r}, but this "
            f"receipt's own fields say the fixture edge is {want_host!r} "
            f"(rest_api_id={api_id!r}, region={region!r}).\n"
            "This is checked as the EXACT host and not as 'the API id appears "
            "somewhere in the URL': containment also accepts a non-AWS host wearing "
            "the id as a label, another region's execute-api host, and the id sitting "
            "in the path of a completely different host. A control-enabled worker "
            "would have posted its bootstrap to whichever of those was supplied."
        )

    # The DELIMITER, not the parsed value. `…/agent?` and `…/agent#` both parse to an
    # empty query/fragment, so `if parts.query or parts.fragment` answers "neither" while
    # the character is still there -- and run_identity appends '/bootstrap' after it,
    # making the bootstrap path into a query string or a fragment. Checked on the raw
    # string because that is the only place the delimiter survives.
    for char, what in (("?", "a query delimiter"), ("#", "a fragment delimiter")):
        if char in endpoint:
            raise EdgeReceiptError(
                f"the receipt's worker_control_endpoint {endpoint!r} contains {what} "
                f"({char!r}). #5836 emits none, and an EMPTY one is the dangerous case: "
                f"urllib reports no query and no fragment for {canonical + char!r}, so a "
                "check on the parsed value passes it. lib/run_identity.py then appends "
                f"'/bootstrap', and the request goes to '{canonical + char}/bootstrap' -- the "
                "bootstrap path becomes part of the query string or the fragment, the edge "
                "has no route for it, and the worker's registration fails in a way that "
                "reads as an edge fault rather than a malformed endpoint."
            )

    if parts.path != want_path:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint has path {parts.path!r}, but the "
            f"edge contract is {want_path!r} -- the API stage is the environment "
            f"({environment!r}; #5836's main.tf sets stage_name = var.environment) and "
            "the pod registers its routes under /internal/v1/agent.\n"
            "Compared exactly, because run_identity appends '/bootstrap' to this base: "
            "a trailing slash produces a double slash, a missing stage reaches no "
            "deployed stage, and another stage's path is another deployment."
        )

    # THE BACKSTOP. Everything above names a specific way the URL can be wrong, and a
    # list of named ways passes anything not on the list -- which is precisely how the
    # empty query/fragment/port delimiters got through a set of checks that already
    # covered scheme, userinfo, port, host and path. This compares the whole string to
    # the one URL the receipt's own fields imply, so a component nobody has thought of
    # yet cannot slip past. Deliberately LAST: reached only when every specific check
    # has passed, so its less specific message is never the one an operator sees for a
    # difference that has its own explanation.
    if endpoint != canonical:
        raise EdgeReceiptError(
            f"the receipt's worker_control_endpoint is {endpoint!r}, but this receipt's own "
            f"fields imply exactly {canonical!r} (rest_api_id={api_id!r}, region={region!r}, "
            f"environment={environment!r}).\n"
            "Every component check above passed, so the difference is in something they do "
            "not name. It is refused anyway: this value decides where a worker holding "
            "protected authority sends its bootstrap, and #5836 emits one exact string for "
            "it. Anything else means the value was edited after the producer wrote it."
        )


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
def bind_receipt(
    receipt: dict[str, Any],
    *,
    run_nonce: str,
    account_id: str,
    region: str,
    environment: str,
) -> None:
    """Establish that this document describes THIS run's edge, or raise.

    Extracted so that every value taken out of a receipt is bound by the SAME check,
    rather than by whichever caller happened to run first. Root found the reason
    (review of 8a8d81b8): ``resolve_alb_policy_source``'s docstring said the binding
    "was already established by ``resolve_worker_endpoint``", but the renderer's CLI
    called only ``load_receipt`` and then the ALB resolver -- so on the path that
    actually renders the policy, no account, region or environment was ever compared.
    A docstring is not a prerequisite. A function call is.

    That mattered specifically because the ALB source is the one field whose misuse
    is silent: a document from another account's edge would supply addresses that are
    simply wrong for this VPC, and a wrong ipBlock denies rather than errors.
    """
    # Each expectation must be PRESENT before it is compared. A falsy expectation
    # that silently skipped its comparison would be the same defect one level up:
    # `--region ""` would have waived the region check entirely, and the region is
    # half of the hostname the endpoint is bound to.
    supplied_bindings = {
        "run_nonce": (run_nonce, (
            "The nonce comes from the shared fixture ledger, which is what makes this "
            "run's edge distinguishable from another run's in the same account.")),
        "account_id": (account_id, (
            "The same edge name and the same run nonce could exist in another "
            "account.")),
        "region": (region, (
            "The region is half of the execute-api hostname, so without it the "
            "endpoint's host cannot be checked at all.")),
        "environment": (environment, (
            "The environment is #5836's per-run state key component AND the API "
            "Gateway stage name, so without it the endpoint's path cannot be "
            "checked.")),
    }
    for field, (value, why) in supplied_bindings.items():
        if not value:
            raise EdgeReceiptError(
                f"no {field} was supplied to bind the receipt against. {why} "
                "Refusing rather than skipping the comparison: an expectation that is "
                "missing must not waive the check it exists for."
            )

    expected = {
        "run_nonce": run_nonce,
        "account_id": account_id,
        "region": region,
        "environment": environment,
    }
    mismatches = []
    for field in RECEIPT_BINDING_FIELDS:
        want = expected[field]
        got = receipt.get(field)
        if got is None or got == "":
            mismatches.append(
                f"the receipt records no {field}, so it cannot be shown to describe this "
                f"run's edge (expected {want!r})"
            )
        elif got != want:
            mismatches.append(
                f"the receipt records {field}={got!r} but this run is {want!r}"
            )
    if mismatches:
        raise EdgeReceiptError(
            "the fixture-edge receipt does not belong to this run:\n  - "
            + "\n  - ".join(mismatches)
            + "\nRefusing to use it. An edge this run does not own is one it also cannot "
            "tear down, so a worker pointed at it would hold protected authority against "
            "infrastructure outside this run's ledger, and a policy rendered from its "
            "addresses would admit a load balancer this run has no record of. This is the "
            "same check #5836's own destroy guard applies to the same receipt."
        )


def resolve_worker_endpoint(
    receipt: dict[str, Any],
    *,
    run_nonce: str,
    account_id: str,
    region: str,
    environment: str,
    supplied_endpoint: str | None = None,
    ordinary_api_id: str | None = None,
) -> str:
    """The endpoint this run's worker may use, or raise.

    ``supplied_endpoint``, when given, is a CROSS-CHECK and never the source: if the
    operator passed one it must equal the receipt's. That ordering matters. Taking
    the flag and merely comparing it to the receipt would mean a missing receipt
    field silently waives the comparison -- the exact defect root found in #5836's
    own backend check ("a missing expected value must never waive a comparison").

    ``ordinary_api_id`` is checked here too when the caller resolved it, so the
    production-edge refusal survives even if a receipt somehow names it.
    """
    # The binding fields, before anything is derived from the receipt. Ordered first
    # deliberately: every value below (including the endpoint itself) comes OUT of
    # this document, so a receipt belonging to another run would otherwise supply the
    # endpoint used to authorise the worker.
    bind_receipt(receipt, run_nonce=run_nonce, account_id=account_id,
                 region=region, environment=environment)

    # --- the endpoint, taken FROM the bound receipt ------------------------------
    endpoint = (receipt.get("worker_control_endpoint") or "").strip()
    if not endpoint:
        raise EdgeReceiptError(
            "the fixture-edge outputs are bound to this run but publish no "
            "worker_control_endpoint. #5836 emits an EMPTY string when "
            "fixture_edge_enabled is false, so this most likely means the edge was never "
            "applied. There is no endpoint to use, and the supplied flag must not stand in "
            "for one: that would reduce the receipt to decoration."
        )

    # The id and the URL must describe ONE api. A receipt can pair a correct
    # inventory with an endpoint pointing somewhere else; nothing else here would
    # notice, because both halves are individually well-formed.
    api_id = (receipt.get("rest_api_id") or "").strip()
    if not api_id:
        raise EdgeReceiptError(
            "the fixture-edge receipt publishes no rest_api_id, so its endpoint cannot be "
            "tied to the API its resource inventory describes. Those are the two halves of "
            "the same claim, and an unlinked pair is not evidence."
        )

    # Every component of the URL, against the contract the receipt's own fields
    # imply. The region and environment used here are this run's, which the binding
    # check above has just established the receipt agrees with.
    check_endpoint_url(
        endpoint, api_id=api_id, region=region, environment=environment
    )

    # Production, refused on the host label AND on containment anywhere in the URL.
    # The asymmetry is deliberate: over-refusing something that merely mentions the
    # ordinary API costs an operator one clarifying question, while under-refusing it
    # points a control-enabled worker at live traffic.
    if ordinary_api_id:
        host = (urlsplit(endpoint).hostname or "").lower()
        if host.split(".")[0] == ordinary_api_id.lower() or ordinary_api_id in endpoint:
            raise EdgeReceiptError(
                f"the receipt's endpoint ({endpoint}) names the ORDINARY gateway's API "
                f"({ordinary_api_id}). That is live production traffic, not an isolated "
                "fixture. A receipt naming it does not make it a fixture edge."
            )

    if supplied_endpoint and supplied_endpoint.strip() != endpoint:
        raise EdgeReceiptError(
            f"--worker-control-endpoint was given {supplied_endpoint!r} but this run's "
            f"bound edge receipt publishes {endpoint!r}. Refusing both rather than "
            "choosing: one of them is not this run's edge, and guessing which would be the "
            "whole defect. Drop the flag to use the receipt, or resolve the disagreement."
        )

    return endpoint


def resolve_alb_policy_source(
    receipt: dict[str, Any],
    *,
    run_nonce: str,
    account_id: str,
    region: str,
    environment: str,
    expected_alb_arn: str,
    expected_container_port: int = FIXTURE_CONTAINER_PORT,
) -> dict[str, Any]:
    """The ALB source addresses this run's fixture policy must admit, or raise.

    Returns ``{"cidrs": [...], "container_port": int, "alb_listener_port": int,
    "run_nonce": ..., "alb_arn": ...}``.

    WHAT ROOT FOUND IN THE FIRST ATTEMPT (review of 8a8d81b8)
    ---------------------------------------------------------
    Four documents reached the renderer and produced a manifest, each of which should
    have been refused:

    1. ``source_cidrs = ["not-an-ip/32"]``. The width check was
       ``cidr.endswith("/32")``, and **suffix matching is not CIDR parsing**. The
       string satisfied it, went into the manifest, and the API server would then
       reject the whole NetworkPolicy mid-run -- after the gateway exists. Now parsed
       with ``ipaddress.IPv4Network``, which is also what rejects a host bit set
       (``10.0.11.37/24`` as a "network") and an IPv6 address the ipBlock cannot mix.
    2. ``container_port = 8081`` with ``alb_listener_port = 80``. Both integers, and
       they differ, so every check passed -- but the fixture Service targets 8080, so
       the rendered rule admitted the ALB on a port nothing listens on. "Differs from
       the listener" was never the property worth checking; "is the port the fixture
       pod actually serves" is. Now compared against ``expected_container_port``,
       which the caller passes from the same constant the Service is rendered from.
    3. ``alb_arn = null``, and a FOREIGN ARN with a correct nonce. Both accepted; the
       null one was normalised to ``""``. The ARN is the only field that says WHICH
       load balancer was observed, so leaving it unchecked meant the addresses could
       come from any ALB in the account as long as the nonce matched. Now required and
       compared against the ALB this run's ledger records, including its account and
       region components.
    4. The receipt's own account/region/environment were never checked on this path
       at all, because the renderer's CLI called ``load_receipt`` and came straight
       here. ``bind_receipt`` is now called from inside this function, so the
       prerequisite is established rather than assumed.

    The nonce inside the source document is checked IN ADDITION to the receipt's,
    because #5836 puts it there precisely so a pasted stale block is visibly not this
    run's; trusting only the enclosing document would let a hand-edited source through
    on the strength of the endpoint's binding.

    FRESHNESS
    ---------
    These are the addresses the observed ALB held WHEN IT WAS READ. A stale list is
    not categorically safe: an elastic network interface can be released and its
    address reassigned -- in this same VPC, plausibly to another load balancer -- so a
    stale allowance can become a real admission of something else, not merely a
    denial of this edge. (#5836's output description and an earlier revision of this
    module both said staleness was denial-only; root corrected that, and it is
    corrected here.) The nonce binding is what forces a per-run read; the caller must
    still re-observe before execution.
    """
    bind_receipt(receipt, run_nonce=run_nonce, account_id=account_id,
                 region=region, environment=environment)

    if not expected_alb_arn:
        raise EdgeReceiptError(
            "no expected ALB ARN was supplied to bind the policy source against. The ARN is "
            "the only field in this document that says WHICH load balancer the addresses "
            "were read from; without it, any ALB in the account satisfies the check as long "
            "as the nonce matches. An expectation that is missing must not waive the check "
            "it exists for."
        )
    if not isinstance(expected_container_port, int) or isinstance(expected_container_port, bool):
        raise EdgeReceiptError(
            f"the expected container port is {expected_container_port!r}, not a number. It "
            "must be the port the fixture pod actually serves, passed from the same constant "
            "the fixture Service is rendered from."
        )

    source = receipt.get(ALB_SOURCE_FIELD)
    if source is None or source == "" or source == {}:
        raise EdgeReceiptError(
            f"the fixture-edge outputs publish no {ALB_SOURCE_FIELD}. The fixture gateway's "
            "NetworkPolicy admits callers by namespaceSelector, and an ALB with "
            "`target-type: ip` connects from its own network interfaces, which belong to no "
            "pod and no namespace -- so without this output the policy denies the edge.\n"
            "Neither the plan nor the Ingress would error; the worker's bootstrap would "
            "simply never complete, which reads as 'the protected worker failed its "
            "bootstrap' -- the conclusion Wave 2 exists to establish or refute.\n"
            "Read the whole outputs document from an APPLIED edge:\n"
            f"{_PRODUCE_OUTPUTS_CMD}"
        )
    if not isinstance(source, dict):
        raise EdgeReceiptError(
            f"the fixture-edge outputs carry {ALB_SOURCE_FIELD} as "
            f"{type(source).__name__}, not an object. #5836 publishes it as a document "
            "carrying source_cidrs, container_port, alb_listener_port and run_nonce; a "
            "scalar cannot be bound to a run or checked for a port confusion."
        )

    missing = [f for f in ALB_SOURCE_REQUIRED_FIELDS
               if source.get(f) is None or source.get(f) == ""]
    if missing:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document is missing {sorted(missing)}. Each is load "
            "bearing: source_cidrs is what the policy admits, and the two ports are "
            "published separately so the rule cannot be written against the listener port "
            "by mistake. A partial document is refused rather than filled in with a default "
            "-- a defaulted port here silently blocks the flow under test."
        )

    # The nonce INSIDE the source document, which is what catches a pasted value from
    # an earlier run even when the surrounding receipt is this run's.
    got_nonce = str(source.get("run_nonce") or "")
    if not got_nonce:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document records no run_nonce, so it cannot be shown "
            f"to describe THIS run's edge (expected {run_nonce!r}). #5836 includes the nonce "
            "for exactly this check; a source block without one is indistinguishable from a "
            "stale paste, and stale addresses may since have been reassigned -- to another "
            "load balancer, in this same VPC."
        )
    if got_nonce != run_nonce:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document belongs to run {got_nonce!r}, but this run is "
            f"{run_nonce!r}. These are the addresses of ANOTHER run's load balancer, and "
            "those interfaces may since have been released and reassigned. Admitting them "
            "would both deny this run's edge (a bootstrap failure that looks like the "
            "finding under test) and admit whatever now holds the address."
        )

    cidrs = source.get("source_cidrs")
    if not isinstance(cidrs, list) or not cidrs:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document's source_cidrs is {cidrs!r}. It must be a "
            "non-empty list: an ALB has one interface per subnet it occupies, and an empty "
            "list would render a policy admitting nothing, which denies the edge."
        )

    clean: list[str] = []
    for entry in cidrs:
        if not isinstance(entry, str) or not entry.strip():
            raise EdgeReceiptError(
                f"the {ALB_SOURCE_FIELD} document's source_cidrs contains {entry!r}, which "
                "is not an address. A malformed entry cannot be admitted, and skipping it "
                "would produce a policy that denies one of the ALB's interfaces -- an "
                "intermittent bootstrap failure, which is the worst possible shape for this."
            )
        cidr = entry.strip()
        # An explicit prefix is required. `ipaddress.IPv4Network("10.0.10.41")` is a
        # valid /32, but a NetworkPolicy's `ipBlock.cidr` is a CIDR field and a bare
        # address is not one -- so accepting it here would move the rejection to the
        # API server, which is exactly the failure this parsing replaced.
        if "/" not in cidr:
            raise EdgeReceiptError(
                f"the {ALB_SOURCE_FIELD} document offers {cidr!r}, which carries no prefix "
                "length. A NetworkPolicy ipBlock takes a CIDR, not a bare address; #5836 "
                "publishes source_cidrs already suffixed (source_ips is the unsuffixed list, "
                "and reading that one by mistake is the likeliest way to get here)."
            )
        # PARSED, not pattern-matched. The previous revision checked
        # `cidr.endswith("/32")`, and root found that "not-an-ip/32" satisfied it and
        # reached the manifest -- where the API server would reject the entire
        # NetworkPolicy mid-run, after the gateway and its policies already exist.
        # Parsing also rejects a value with host bits set and an IPv6 address, neither
        # of which a suffix check can see.
        try:
            network = ipaddress.IPv4Network(cidr, strict=True)
        except ValueError as exc:
            raise EdgeReceiptError(
                f"the {ALB_SOURCE_FIELD} document offers {cidr!r}, which is not a valid IPv4 "
                f"CIDR ({exc}). A string that merely looks like one is worse than a missing "
                "value: it renders into the manifest, and the API server then rejects the "
                "whole NetworkPolicy -- mid-run, after the gateway and its policies exist. "
                "#5836 derives these from the load balancer's own interface addresses, so a "
                "value that does not parse means the document was edited or hand-assembled."
            ) from exc
        # A /32 per interface, which is what the producer publishes. Anything wider is
        # refused rather than narrowed: the tempting "fixes" #5836 enumerates are all
        # width -- 0.0.0.0/0 admits every other ALB in the VPC, and the SUBNET cidr is
        # worse than it looks, because both ordinary gateway ALBs sit in the same
        # subnet, so a subnet rule would admit PRODUCTION's edge to the fixture.
        if network.prefixlen != 32:
            raise EdgeReceiptError(
                f"the {ALB_SOURCE_FIELD} document offers {cidr!r}, which is a /"
                f"{network.prefixlen} rather than a single address (/32). This rule exists to "
                "admit exactly this run's load balancer interfaces; anything wider admits "
                "other load balancers in the same VPC. The ordinary gateway's ALBs share a "
                "subnet with this one, so a subnet CIDR would admit production's edge to the "
                "fixture."
            )
        # Normalised through the parser, so two spellings of one address cannot both
        # survive the duplicate check below.
        canonical = str(network)
        if canonical not in clean:
            clean.append(canonical)

    # Ports are validated by TYPE and RANGE, and never coerced. `terraform output
    # -json` emits a number as a JSON number, so a string here means the document was
    # edited or hand-assembled -- and `int("8080")` would accept that silently while
    # `int(8080.5)` would round a nonsense value into a plausible one. A port outside
    # 1-65535 cannot appear in a NetworkPolicy at all: the API server would reject the
    # manifest mid-run, after the gateway and its policies already exist.
    ports = {}
    for field in ("container_port", "alb_listener_port"):
        value = source.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise EdgeReceiptError(
                f"the {ALB_SOURCE_FIELD} document's {field} is {value!r} "
                f"({type(value).__name__}), not a number. #5836 publishes it as a Terraform "
                "number, so another type means this document was edited or hand-assembled. "
                "Refusing rather than coercing: the rule is written against this value, and "
                "a coerced one that happens to parse would block the flow under test."
            )
        if not 1 <= value <= 65535:
            raise EdgeReceiptError(
                f"the {ALB_SOURCE_FIELD} document's {field} is {value}, which is not a "
                "usable TCP port. A NetworkPolicy naming it would be rejected by the API "
                "server, and that rejection would land mid-run, after the gateway and its "
                "policies already exist."
            )
        ports[field] = value
    container_port = ports["container_port"]
    listener_port = ports["alb_listener_port"]
    if container_port == listener_port:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document reports container_port and alb_listener_port "
            f"as the same value ({container_port}). #5836 publishes them separately BECAUSE "
            "they differ -- the ALB listens on one port and connects to the pod on another "
            "-- and asserts the difference in its own tests. Equal values mean this document "
            "was edited or misread, and a rule naming the listener port blocks precisely the "
            "flow under test."
        )
    # The port must be the one the fixture pod ACTUALLY serves, not merely a different
    # number from the listener. Root found the gap (review of 8a8d81b8): a document
    # reporting container_port=8081 with alb_listener_port=80 passed every check --
    # both integers, and they differ -- and rendered a rule admitting the ALB on a port
    # nothing listens on. That denial is indistinguishable from the one this whole seam
    # exists to prevent. Compared against the caller's constant rather than a literal
    # here, so the renderer and the Service cannot drift apart from this check.
    if container_port != expected_container_port:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document reports container_port={container_port}, but "
            f"the fixture gateway's pods serve {expected_container_port} (the fixture Service "
            f"maps {listener_port} -> {expected_container_port}). A rule naming any other "
            "port admits the load balancer to a port nothing is listening on, which denies "
            "the flow under test while reading as configured -- the bootstrap simply never "
            "completes. Refusing rather than rendering: 'differs from the listener port' was "
            "never the property that mattered."
        )

    # The ARN, which is the only field naming WHICH load balancer these addresses were
    # read from. Root found both halves of this gap: a null ARN was accepted and
    # normalised to "", and a FOREIGN ARN passed as long as the nonce matched. The
    # nonce says "this run"; only the ARN says "this run's ALB", and #5836 reads the
    # interfaces from whatever `fixture_alb_arn` it was pointed at.
    got_arn = source.get("alb_arn")
    if not isinstance(got_arn, str) or not got_arn.strip():
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document records alb_arn={got_arn!r}, so it does not say "
            "which load balancer these addresses belong to. #5836 emits an empty string when "
            "the ALB could not be read, which means the addresses are not this edge's either. "
            "Without the ARN the nonce alone would admit addresses read from any load balancer "
            "in the account."
        )
    got_arn = got_arn.strip()
    if got_arn != expected_alb_arn.strip():
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document's addresses were read from {got_arn!r}, but "
            f"this run's fixture load balancer is {expected_alb_arn.strip()!r}. These are the "
            "interfaces of a DIFFERENT load balancer: admitting them would both deny this "
            "run's edge and open the fixture to whatever now holds those addresses. A "
            "matching run_nonce does not make it this run's ALB -- #5836 reads whichever ALB "
            "its fixture_alb_arn names."
        )
    # The ARN must also agree with the account and region this run is bound to. An ARN
    # is self-describing (arn:aws:elasticloadbalancing:<region>:<account>:...), so a
    # ledger that recorded a cross-account or cross-region ALB is caught here rather
    # than at apply time.
    arn_fields = got_arn.split(":")
    if len(arn_fields) < 6 or arn_fields[0] != "arn":
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document's alb_arn ({got_arn!r}) is not an ARN, so its "
            "account and region cannot be checked against this run's."
        )
    arn_region, arn_account = arn_fields[3], arn_fields[4]
    if arn_region != region or arn_account != account_id:
        raise EdgeReceiptError(
            f"the {ALB_SOURCE_FIELD} document's alb_arn names account {arn_account!r} in "
            f"region {arn_region!r}, but this run is bound to account {account_id!r} in "
            f"{region!r}. A load balancer in another account or region has interface "
            "addresses from another VPC entirely, so a policy rendered from them admits "
            "addresses this cluster's traffic can never carry -- and may admit addresses that "
            "belong to something else here."
        )

    return {
        "cidrs": clean,
        "container_port": container_port,
        "alb_listener_port": listener_port,
        "run_nonce": got_nonce,
        "alb_arn": got_arn,
    }


def check_alb_source_is_current(
    rendered: dict[str, Any],
    *,
    observed_cidrs: Any,
    observed_arn: str,
    expected_alb_arn: str,
) -> dict[str, Any]:
    """Confirm the rendered policy still admits the ALB's CURRENT addresses, or raise.

    Root's requirement, stated twice (5810972205, 5810972553): "Require a fresh
    observation and matching policy before execution; stale allowances can refer to
    reassigned addresses."

    WHY A SECOND OBSERVATION IS NOT REDUNDANT
    -----------------------------------------
    ``resolve_alb_policy_source`` establishes that the document belongs to this run and
    this load balancer. It cannot establish that the addresses are still the ALB's,
    because a Terraform output records what was true WHEN IT RAN. Between that apply
    and the experiment, an interface can be replaced -- an AZ rebalance, a subnet
    added, the ALB recreated -- and the consequences differ in kind:

      * an address the ALB no longer holds is still ADMITTED by the policy, and it may
        since have been REASSIGNED, in this same VPC, plausibly to another load
        balancer. This is why staleness is not "safe because a missing address is only
        a denial" -- the claim an earlier revision of this module made, copied from the
        producer's output description, and which root corrected. A stale /32 is a live
        admission of whatever holds it now.
      * an address the ALB has GAINED is not admitted, so some connections are dropped
        and others are not, by which interface the load balancer happened to pick.
        That is an INTERMITTENT bootstrap failure, the hardest shape to diagnose and
        the easiest to record as flakiness in the software under test.

    Both are refusals. Set equality is required in both directions, not containment.

    This function makes NO cloud call: the caller observes (``aws ec2
    describe-network-interfaces``, which is what #5836's ``verify`` field publishes) and
    this decides, the same split ``stage_gate`` uses. An observation that could not be
    made is a refusal, never an empty set -- "could not check" is not "checked and
    fine", and here it would mean rendering from a document nothing corroborates.
    """
    if not expected_alb_arn or not expected_alb_arn.strip():
        raise EdgeReceiptError(
            "no expected ALB ARN was supplied to check the observation against, so a "
            "re-observation of the WRONG load balancer would satisfy this check. An "
            "expectation that is missing must not waive the check it exists for."
        )
    if not isinstance(observed_arn, str) or not observed_arn.strip():
        raise EdgeReceiptError(
            f"the fresh ALB observation names no load balancer ({observed_arn!r}), so it "
            "cannot be shown to describe the ALB the policy was rendered for. Refusing "
            "rather than treating it as a match: an unattributed observation is not "
            "evidence about anything."
        )
    if observed_arn.strip() != expected_alb_arn.strip():
        raise EdgeReceiptError(
            f"the fresh addresses were observed on {observed_arn.strip()!r}, but the policy "
            f"was rendered for {expected_alb_arn.strip()!r}. Comparing one load balancer's "
            "current addresses against another's rendered rule proves nothing about either."
        )
    # A MISSING observation is a refusal, not an empty set. `None` here would otherwise
    # compare equal to "no addresses" and, if the rendered list were also somehow
    # empty, report agreement between two absences.
    if observed_cidrs is None or not isinstance(observed_cidrs, list):
        raise EdgeReceiptError(
            f"the fresh ALB address observation is {observed_cidrs!r}, not a list. The "
            "policy cannot be shown to match the load balancer's CURRENT interfaces, and "
            "the addresses it admits may since have been reassigned -- in this VPC, "
            "plausibly to another load balancer. 'Could not observe' is not 'unchanged'."
        )

    fresh: list[str] = []
    for entry in observed_cidrs:
        if not isinstance(entry, str) or "/" not in entry:
            raise EdgeReceiptError(
                f"the fresh ALB observation contains {entry!r}, which is not a CIDR. It is "
                "compared against the rendered rule, so an entry that cannot be parsed "
                "would silently drop out of the comparison and read as agreement."
            )
        try:
            network = ipaddress.IPv4Network(entry.strip(), strict=True)
        except ValueError as exc:
            raise EdgeReceiptError(
                f"the fresh ALB observation contains {entry!r}, which is not a valid IPv4 "
                f"CIDR ({exc})."
            ) from exc
        if network.prefixlen != 32:
            raise EdgeReceiptError(
                f"the fresh ALB observation contains {entry!r}, which is not a single "
                "address. The rendered rule is a set of /32s, so a wider observation cannot "
                "be compared with it -- and must not be, since agreement would then mean "
                "the policy admits less than the observation claims."
            )
        if str(network) not in fresh:
            fresh.append(str(network))

    if not fresh:
        raise EdgeReceiptError(
            "the fresh ALB observation found no addresses. #5836's own precondition treats "
            "an empty result as a refusal, and it means the same thing here: either the "
            "load balancer is gone or the query is wrong. Proceeding would run the "
            "experiment against a policy nothing corroborates."
        )

    admitted = set(rendered.get("cidrs") or [])
    current = set(fresh)
    if admitted == current:
        return {
            "alb_arn": observed_arn.strip(),
            "cidrs": sorted(current),
            "matches_rendered_policy": True,
        }

    stale = sorted(admitted - current)
    unadmitted = sorted(current - admitted)
    problems = []
    if stale:
        problems.append(
            f"the policy admits {stale}, which the load balancer no longer holds. Those "
            "addresses may have been released and REASSIGNED -- in this VPC, plausibly to "
            "another load balancer -- so this is a live admission of something else, not a "
            "harmless leftover"
        )
    if unadmitted:
        problems.append(
            f"the load balancer now holds {unadmitted}, which the policy does NOT admit. "
            "Connections would succeed or fail by which interface it happened to use: an "
            "intermittent bootstrap failure, which reads as flakiness in the software "
            "under test"
        )
    raise EdgeReceiptError(
        "the rendered policy no longer matches the fixture load balancer's addresses:\n  - "
        + "\n  - ".join(problems)
        + "\nRe-read the edge outputs and re-render the policy before running the "
          "experiment. Both directions are refusals: a policy that admits more than the "
          "ALB holds is an opening, and one that admits less produces a measurement of "
          "the fixture's own networking rather than of the feature."
    )


def endpoint_provenance(receipt: dict[str, Any], receipt_path: str | Path) -> dict[str, Any]:
    """How the endpoint was established, for the fixture's own record.

    Recorded so a later reader can see the endpoint came from a bound receipt rather
    than from a flag -- which is otherwise indistinguishable in the output -- and
    which of the producer's document shapes it came out of.
    """
    return {
        "source": "5836-fixture-edge-outputs",
        "receipt_path": str(receipt_path),
        "document_shape": receipt.get("_shape"),
        "rest_api_id": receipt.get("rest_api_id"),
        "bound_to": {field: receipt.get(field) for field in RECEIPT_BINDING_FIELDS},
        "url_checked": [
            "scheme is https",
            "authority carries no userinfo and no explicit port",
            "host is exactly <rest_api_id>.execute-api.<region>.amazonaws.com",
            "path is exactly /<environment>/internal/v1/agent",
            "no query string and no fragment",
        ],
        "_why": (
            "the endpoint was READ FROM a document whose ownership bindings "
            "(run_nonce/account_id/region/environment) match this run's ledger, and then "
            "PARSED and checked component by component against the host and path those "
            "same fields imply. An arbitrary https URL satisfies 'not the ordinary API' "
            "while belonging to another run's edge; a URL merely CONTAINING the API id "
            "satisfies that too while addressing a different host entirely."
        ),
    }


# ---------------------------------------------------------------------------
# CLI -- the seam the shell launcher calls
# ---------------------------------------------------------------------------
def _main_alb(argv: list[str]) -> int:
    """The two ALB subcommands. Dispatched by leading verb; see ``main``."""
    import argparse
    import sys

    verb, rest = argv[0], argv[1:]
    parser = argparse.ArgumentParser(prog=f"edge_receipt.py {verb}", description=__doc__)
    if verb == "resolve-alb-source":
        parser.add_argument("--receipt", required=True)
        parser.add_argument("--run-nonce", required=True)
        parser.add_argument("--account-id", required=True)
        parser.add_argument("--region", required=True)
        parser.add_argument("--environment", required=True)
        parser.add_argument("--expected-alb-arn", required=True,
                            help="the fixture ALB ARN this run's ledger records. Required: a "
                                 "matching nonce alone would admit addresses read from any ALB "
                                 "in the account, because the producer reads whichever load "
                                 "balancer it is pointed at")
        parser.add_argument("--out", default=None)
    else:  # check-alb-current
        parser.add_argument("--rendered", required=True,
                            help="the document `resolve-alb-source` wrote")
        parser.add_argument("--observed-cidrs", required=True,
                            help="comma-separated /32s observed on the load balancer NOW")
        parser.add_argument("--observed-arn", required=True,
                            help="the ALB the addresses were observed on. An unattributed "
                                 "observation is not evidence about anything")
        parser.add_argument("--expected-alb-arn", required=True)
    args = parser.parse_args(rest)

    try:
        if verb == "resolve-alb-source":
            receipt = load_receipt(args.receipt)
            result = resolve_alb_policy_source(
                receipt,
                run_nonce=args.run_nonce, account_id=args.account_id, region=args.region,
                environment=args.environment, expected_alb_arn=args.expected_alb_arn,
            )
        else:
            observed = [part.strip() for part in args.observed_cidrs.split(",")]
            if not all(observed):
                # An empty element would be one fewer address required, i.e. one address
                # of this run's load balancer the fixture silently does not admit.
                print(f"FAIL: --observed-cidrs {args.observed_cidrs!r} contains an empty "
                      "entry. Refusing rather than dropping it.", file=sys.stderr)
                return 2
            with open(args.rendered, encoding="utf-8") as handle:
                rendered = json.load(handle)
            result = check_alb_source_is_current(
                rendered,
                observed_cidrs=observed, observed_arn=args.observed_arn,
                expected_alb_arn=args.expected_alb_arn,
            )
    except EdgeReceiptError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        # Each verb has exactly one input path and they are named differently, so the
        # path is chosen by verb rather than by `getattr(..., default)`: argparse's
        # Namespace raises for the absent one, and a default argument is evaluated
        # EAGERLY -- which turned the unreadable-file refusal into an AttributeError
        # traceback on this verb, i.e. a crash where a diagnosis was meant to be.
        unreadable = args.receipt if verb == "resolve-alb-source" else args.rendered
        print(f"FAIL: could not read {unreadable}: {exc}\n"
              "'Could not read it' is not 'it is fine'.", file=sys.stderr)
        return 1

    payload = json.dumps(result, indent=2, sort_keys=True)
    out = getattr(args, "out", None)
    if out:
        # Written only on success, so the file's existence is itself evidence: a
        # refusal must not leave a later step an artifact to read as a passed check.
        try:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(payload + "\n")
        except OSError as exc:
            print(f"FAIL: decided, but could not write {out}: {exc}", file=sys.stderr)
            return 1
    print(payload)
    return 0


# The ALB verbs are dispatched by a leading positional rather than by argparse
# subparsers, because this module's original CLI is FLAT (`--receipt ... ` with no
# subcommand) and `10-create-fixture.sh` already invokes it that way to resolve the
# control endpoint. Converting to subparsers would have made that existing call a usage
# error -- a break in the one path that is already relied upon, in exchange for tidier
# help output.
_ALB_VERBS = ("resolve-alb-source", "check-alb-current")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in _ALB_VERBS:
        return _main_alb(list(argv))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True,
                        help="path to #5836's `terraform output -json` (all outputs)")
    parser.add_argument("--run-nonce", required=True, help="this run's nonce, from the ledger")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True,
                        help="this run's environment; also the API Gateway stage name")
    parser.add_argument("--supplied-endpoint", default=None,
                        help="optional cross-check; must equal the receipt's endpoint")
    parser.add_argument("--ordinary-api-id", default=None)
    parser.add_argument("--provenance-out", default=None,
                        help="where to write how the endpoint was established")
    args = parser.parse_args(argv)

    try:
        receipt = load_receipt(args.receipt)
        endpoint = resolve_worker_endpoint(
            receipt,
            run_nonce=args.run_nonce, account_id=args.account_id, region=args.region,
            environment=args.environment,
            supplied_endpoint=args.supplied_endpoint,
            ordinary_api_id=args.ordinary_api_id,
        )
    except EdgeReceiptError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.provenance_out:
        out = Path(args.provenance_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(endpoint_provenance(receipt, args.receipt), indent=2, sort_keys=True)
            + "\n"
        )

    # stdout is the endpoint ALONE, so the shell can capture it directly. Every
    # explanatory line goes to stderr for exactly this reason.
    print(endpoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
