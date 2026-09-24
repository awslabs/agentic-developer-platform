"""Preflight: prove the target before mutating anything.

Order matters. Every check here is read-only, and all of them run before the
first fixture is created. The reason is the failure mode this evaluation exists
to avoid: a confident green report about the wrong account, the wrong revision,
or a contaminated CLI configuration.

A missing fixture CLASS does not abort the run — it blocks exactly the cases
that need it (see `cases.block_missing_fixtures`) so the rest can still make
progress honestly. A wrong ACCOUNT, a wrong REVISION or a contaminated harness
does abort: those invalidate every case, so continuing would be misleading.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import cases, cleanup, config


class PreflightError(RuntimeError):
    """The target is not what the config says it is. Nothing was mutated."""


def require(condition, message):
    if not condition:
        raise PreflightError(message)


def http_json(url, *, token=None, timeout=60, expect=200):
    """Read JSON from the gateway. Redirects are refused before a token is sent."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            raise PreflightError(
                f"{url} redirected; refusing to follow before sending credentials"
            )

    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise PreflightError(f"{url} is unreachable: {type(exc).__name__}") from None
    require(status == expect, f"{url} returned HTTP {status}, expected {expect}")
    try:
        return json.loads(body)
    except ValueError:
        raise PreflightError(f"{url} did not return JSON") from None


def http_status(url, *, timeout=60):
    """The status code alone, with no body read and no token sent.

    Separate from `http_json` rather than a mode of it: that function's contract is
    "the body, having required an exact status", and every caller relies on the
    require(). A probe that cares only whether a route is mounted needs the
    opposite — no expected status and no body at all — and expressing it as
    `expect=None` would make the require() compare against None and fail on every
    reachable URL.
    """

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            raise PreflightError(f"{url} redirected; refusing to follow")

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        # An error status IS the answer here: 401 proves the route is mounted.
        return exc.code
    except urllib.error.URLError as exc:
        raise PreflightError(f"{url} is unreachable: {type(exc).__name__}") from None


# The document `cli/bg-cognito-auth.sh` actually fetches before it has a token.
# R2: this used to be "/cli/discovery", which is not a path the product serves —
# the CLI download route allows ten fixed script names and `discovery` is not one
# of them, so preflight asserted a 200 on a route that can only 404. The real
# discovery document is prefix-free (`well_known_router` carries no prefix) and is
# the same URL the login flow depends on, which is what makes it worth checking.
DISCOVERY_PATH = "/.well-known/cognito-config"

# Keys the route returns (src/auth/routes.py::cognito_config). `identity_pool_id`
# is deliberately always empty there, so it must be PRESENT but is not required to
# be populated — asserting a value would fail against correct product behaviour.
DISCOVERY_KEYS = (
    "user_pool_id",
    "client_id",
    "cli_client_id",
    "identity_pool_id",
    "region",
)
DISCOVERY_POPULATED = ("user_pool_id", "client_id", "cli_client_id", "region")


# "The caller did not fetch it" — distinct from "the caller fetched it and got
# nothing useful". Without this distinction, a stage that passed the SPA fallback
# through (a 200 whose body is HTML, so not a dict) would land in the `is None`
# branch and this helper would issue its OWN urllib request, bypassing the
# transport the run is supposed to make every gateway call through.
UNFETCHED = object()


def check_unauthenticated_discovery(cfg, record, *, discovery=UNFETCHED):
    """E01's precondition: discovery is reachable, correct, and needs no token.

    Also the first real proof that `gateway_url` points at a live ADP gateway
    rather than, say, an S3 error page that answers 200 to everything. Validating
    the response contract is what makes that proof real: the CloudFront SPA
    fallback answers 200 with HTML for an unknown path, and a JSON-shaped 200 from
    some other service would otherwise satisfy an isinstance check.

    The pool and client are compared against the configured fixtures, so pointing
    the run at a gateway backed by a different Cognito pool aborts here rather
    than surfacing later as unexplained login failures.

    `discovery` lets the production stage pass a document it already fetched
    through its own HTTP transport, so the contract is asserted in exactly one
    place instead of being duplicated (and drifting) between the two callers.
    """
    if discovery is UNFETCHED:
        discovery = http_json(
            cfg["gateway_url"].rstrip("/") + DISCOVERY_PATH, expect=200
        )
    require(
        isinstance(discovery, dict),
        f"{DISCOVERY_PATH} did not return an object; gateway_url may not be an ADP gateway",
    )
    missing = [key for key in DISCOVERY_KEYS if key not in discovery]
    require(
        not missing,
        f"{DISCOVERY_PATH} is missing {', '.join(missing)}; "
        "this is not the ADP Cognito discovery document",
    )
    empty = [key for key in DISCOVERY_POPULATED if not str(discovery.get(key) or "")]
    require(
        not empty,
        f"{DISCOVERY_PATH} returned empty {', '.join(empty)}; "
        "the deployment's Cognito client is not configured and CLI login cannot work",
    )
    record["discovery_keys"] = sorted(discovery)
    # Not secret: the client_id ships to every browser as VITE_COGNITO_CLIENT_ID
    # and the app client is created with generate_secret = false.
    record["discovery_user_pool_id"] = discovery["user_pool_id"]
    record["discovery_region"] = discovery["region"]
    require(
        discovery["user_pool_id"] == cfg["cognito_user_pool_id"],
        f"The gateway's Cognito pool {discovery['user_pool_id']!r} is not the configured "
        f"{cfg['cognito_user_pool_id']!r}; the test identity would be created in the wrong pool",
    )
    require(
        discovery["region"] == cfg["region"],
        f"The gateway reports Cognito region {discovery['region']!r}, not {cfg['region']!r}",
    )
    return discovery


def check_revision(cfg, record):
    """Assert the deployed backend is the revision we were told to evaluate.

    Without this, a run can pass against yesterday's code and be reported as
    acceptance of today's. `expected_revision` is required config for that
    reason — there is no 'whatever is deployed' mode.
    """
    health = http_json(cfg["gateway_url"] + "/health", expect=200)
    deployed = str(
        health.get("revision") or health.get("git_sha") or health.get("version") or ""
    )
    record["deployed_revision"] = deployed
    record["expected_revision"] = cfg["expected_revision"]
    require(
        deployed,
        "Gateway /health does not report a revision; cannot bind results to a deployment",
    )
    require(
        deployed.startswith(cfg["expected_revision"][: len(deployed)])
        or cfg["expected_revision"].startswith(deployed),
        f"Deployed revision {deployed!r} is not the expected revision {cfg['expected_revision']!r}",
    )
    return deployed


def http_bytes(url, *, timeout=60):
    """Read raw bytes from the gateway, unauthenticated."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            require(response.status == 200, f"{url} returned HTTP {response.status}")
            return response.read()
    except urllib.error.HTTPError as exc:
        raise PreflightError(f"{url} returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise PreflightError(f"{url} is unreachable: {type(exc).__name__}") from None


def check_served_cli_hashes(cfg, expected_hashes, record, *, fetch=None):
    """Compare the bytes the gateway serves against the revision under test.

    A CLI evaluation that installs different code than the release serves is
    testing nothing. Hash mismatch aborts rather than blocks.

    `expected_hashes` must come from somewhere the gateway cannot influence —
    `release.manifest(expected_revision)` reads them out of the git object store.
    Passing in hashes observed from this same download would make the comparison
    vacuous, which is precisely the R10 defect; that is why there is no default.

    `fetch` is injectable so the stage can reuse its own HTTP transport (and so
    offline tests can drive this through a transport double rather than by
    replacing this function).
    """
    import hashlib

    require(
        expected_hashes,
        "No expected release hashes were supplied; served bytes cannot be judged",
    )
    read = fetch or (lambda url: http_bytes(url))
    served = {}
    for name in sorted(expected_hashes):
        url = f"{cfg['gateway_url'].rstrip('/')}/cli/{name}"
        payload = read(url)
        # A misrouted request returns the SPA's index.html with HTTP 200, which
        # would otherwise hash cleanly as "some file we served".
        require(
            isinstance(payload, bytes) and payload.startswith(b"#!"),
            f"{url} did not return a script; the release route is misconfigured",
        )
        served[name] = hashlib.sha256(payload).hexdigest()

    from . import release

    ok, report = release.compare(served, expected_hashes)
    record["served_cli_hashes"] = served
    record["release_comparison"] = report
    require(
        ok,
        "The gateway is not serving the release under test — "
        f"missing: {report['missing'] or 'none'}; "
        f"mismatched: {report['mismatched'] or 'none'}. "
        "Refusing to evaluate a mixed or stale CLI release.",
    )
    return served


def check_harness_isolation(auth_helper_text, record):
    """The BG_CONFIG_DIR contract.

    At the pinned harness revision, bg-cognito-auth.sh honours BG_CONFIG_DIR; on
    main it hardcodes ${HOME}/.bedrock-gateway. If the harness root we assembled
    lacks the honouring line, the on-instance worker writes into the invoking
    user's real config directory and can pick up another identity's session —
    a false green. Abort instead.
    """
    present = config.REQUIRED_CLI_CONFIG_DIR_LINE in auth_helper_text
    record["cli_config_dir_isolation"] = present
    require(
        present,
        "The assembled harness CLI helper does not honour BG_CONFIG_DIR "
        f"(expected {config.REQUIRED_CLI_CONFIG_DIR_LINE!r} in {config.CLI_AUTH_HELPER}); "
        "refusing to run with a contaminated configuration directory",
    )
    return present


def check_accounts(sts_identities, cfg, record, *, destination_required=True):
    """Both accounts must resolve to what config claims, and must differ.

    `sts_identities` is {"platform": {...}, "destination": {...}} as returned by
    GetCallerIdentity, passed in so this stays unit-testable offline.
    """
    for name, key in (
        ("platform", "platform_account"),
        ("destination", "destination_account"),
    ):
        if name == "destination" and not destination_required:
            continue
        identity = sts_identities.get(name) or {}
        actual = str(identity.get("Account") or "")
        require(actual, f"No STS identity was resolved for the {name} account")
        require(
            actual == str(cfg[key]),
            f"The {name} credentials resolve to account {actual}, not the configured {cfg[key]}",
        )
        record[f"{name}_arn"] = identity.get("Arn")
        record[f"{name}_account"] = actual
    if not destination_required:
        return True
    require(
        record["platform_account"] != record["destination_account"],
        "Platform and destination credentials resolve to the same account; cross-account routing cannot be proven",
    )
    return True


def check_subnet(subnet, cfg, record):
    """The EC2 subnet must be private, in the configured VPC, and owned by us."""
    require(
        subnet.get("VpcId") == cfg["vpc_id"],
        f"Subnet {cfg['private_subnet_id']} is not in VPC {cfg['vpc_id']}",
    )
    require(
        str(subnet.get("OwnerId")) == str(cfg["platform_account"]),
        f"Subnet {cfg['private_subnet_id']} is owned by {subnet.get('OwnerId')}, not the platform account",
    )
    require(
        not subnet.get("MapPublicIpOnLaunch"),
        f"Subnet {cfg['private_subnet_id']} auto-assigns public IPs; this evaluation requires a private subnet",
    )
    record["subnet"] = {
        "id": cfg["private_subnet_id"],
        "vpc": subnet.get("VpcId"),
        "az": subnet.get("AvailabilityZone"),
    }
    return True


def check_cognito(pool, client, cfg, record):
    """The Cognito pool must be the configured one, in-region and in-account."""
    pool_id = str(pool.get("Id") or "")
    require(
        pool_id == str(cfg["cognito_user_pool_id"]),
        f"Cognito pool {pool_id!r} is not the configured {cfg['cognito_user_pool_id']!r}",
    )
    require(
        pool_id.startswith(cfg["region"] + "_"),
        f"Cognito pool {pool_id} is not in region {cfg['region']}",
    )
    arn = str(pool.get("Arn") or "")
    if arn:
        require(
            f":{cfg['platform_account']}:" in arn,
            "Cognito pool is not owned by the platform account",
        )
    require(
        client.get("ClientId"),
        "No Cognito app client was resolved for CLI authentication",
    )
    record["cognito"] = {"user_pool_id": pool_id, "client_id": client.get("ClientId")}
    return True


def check_deployment_bindings(cfg, record, *, fetch=None):
    """#5413: prove each configured deployment is its own live ADP gateway.

    Read-only, and it is what turns three configured URLs into a usable fixture.
    Each one must serve the CLI's own unauthenticated discovery document, because
    that is the first thing `adp login` fetches — an unreachable or non-ADP URL
    would otherwise surface as an unexplained login failure inside the journey,
    attributed to the product rather than to the binding.

    The Cognito pools are recorded and compared, not required to differ: three
    deployments MAY share an identity provider. What must differ is the gateway,
    and `config.validate()` has already refused a binding set that reuses a URL.

    Returns False rather than raising: an absent or broken deployment fixture
    blocks E16/E17 and leaves the rest of the matrix to run, exactly as a missing
    GitHub App does. A wrong TARGET aborts; a missing FIXTURE blocks.

    `fetch` is injectable for the same reason `check_served_cli_hashes` takes one:
    the production stage reads through the run's own HTTP transport, which refuses
    redirects before anything is sent, instead of a second urllib path here.
    """
    read = fetch or (lambda url: http_json(url, expect=200))
    bindings = cfg.get("deployments") or []
    if len(bindings) < config.REQUIRED_DEPLOYMENTS:
        record["deployments"] = {
            "configured": len(bindings),
            "required": config.REQUIRED_DEPLOYMENTS,
            "reachable": [],
        }
        return False
    reachable, problems = [], {}
    for entry in bindings:
        name = str(entry.get("name") or "")
        url = str(entry.get("gateway_url") or "").rstrip("/")
        try:
            discovery = read(url + DISCOVERY_PATH)
        except PreflightError as exc:
            problems[name] = str(exc)
            continue
        if not isinstance(discovery, dict) or [
            key for key in DISCOVERY_POPULATED if not str(discovery.get(key) or "")
        ]:
            problems[name] = (
                "served no usable Cognito discovery document, so `adp login` "
                "against this deployment could not work"
            )
            continue
        reachable.append(
            {
                "name": name,
                # Not secret: the pool and client id ship to every browser.
                "user_pool_id": discovery["user_pool_id"],
                "region": discovery["region"],
            }
        )
    record["deployments"] = {
        "configured": len(bindings),
        "required": config.REQUIRED_DEPLOYMENTS,
        "reachable": reachable,
        "problems": problems,
        # Evidence that these are three gateways and not three names for one.
        "distinct_pools": len({entry["user_pool_id"] for entry in reachable}),
    }
    return len(reachable) >= config.REQUIRED_DEPLOYMENTS


def check_superplane_domain(cfg, record, *, probe=None):
    """#5637: keep E18 blocked until durable mutation recovery is implemented.

    There is deliberately no configuration switch that claims this capability.
    An unauthenticated 401/403 only proves gateway authentication answered; it
    cannot establish domain readiness or the missing cleanup producer. Keep the
    existing probe argument for the stage's interface, but do not use network
    reachability as permission to execute this incomplete journey.
    """
    superplane = cfg.get("superplane") or {}
    base = str(superplane.get("base_path") or "").rstrip("/")
    record["superplane"] = {
        "configured": bool(base),
        "durable_recovery": False,
        "blocker": "superplane_durable_recovery_unimplemented",
        "problem": cleanup.SUPERPLANE_RECOVERY_BLOCKER,
    }
    return False


def evaluate_fixtures(
    cfg,
    *,
    github_available=None,
    hosted_available=None,
    deployments_available=None,
    superplane_available=None,
):
    """Decide which fixture classes are genuinely usable for this run.

    Config alone gives the candidate set; the caller passes live RESULTS for the
    classes that need proving. `None` means "not proven", which is treated as
    unavailable — an unproven fixture must block its cases, never be assumed.

    Results, not probes: `live.py` hands these out as callables and `stages.py`
    calls them, so passing the callable itself is an easy mistake — and every
    function object is truthy, so it would mark the fixture AVAILABLE on the
    strength of never having been run. That is the one direction this function
    must never fail in, so it is refused outright rather than trusted.
    """
    for label, value in (
        ("github_available", github_available),
        ("hosted_available", hosted_available),
        ("deployments_available", deployments_available),
        ("superplane_available", superplane_available),
    ):
        if callable(value):
            raise PreflightError(
                f"{label} was given a probe rather than its result; call it first. "
                "A callable is truthy, so this would have reported the fixture as "
                "available without ever checking it"
            )
    available = config.fixture_classes(cfg)
    if cases.GITHUB_APP in available and not github_available:
        available.discard(cases.GITHUB_APP)
        available.discard(cases.GITHUB_REPO)
    if cases.GITHUB_REPO in available and not github_available:
        available.discard(cases.GITHUB_REPO)
    if cases.HOSTED in available and not hosted_available:
        available.discard(cases.HOSTED)
    # #5413: configured is not reachable. Three URLs in a config prove nothing
    # until each one answers as an ADP gateway, so an unproven binding set blocks
    # E16/E17 rather than letting them fail inside the journey.
    if cases.THREE_DEPLOYMENTS in available and not deployments_available:
        available.discard(cases.THREE_DEPLOYMENTS)
    # No supplied boolean can manufacture the missing E18 recovery producer.
    # Remove this guard only alongside its implemented durable recovery path.
    available.discard(cases.SUPERPLANE_DOMAIN)
    return available


def missing_fixture_report(cfg, available):
    """Human-readable list of what is absent and which cases it blocks.

    This is what the runbook and the Actions summary show an operator so they can
    go and create the fixture, instead of a bare 'blocked'.
    """
    names = {
        cases.DESTINATION: "cross-account destination and provisioner roles (config destination_role_arn + provisioner_role_arn)",
        cases.GITHUB_APP: "an isolated GitHub App fixture (config github.org + github.app_fixture/existing_app_fixture)",
        cases.GITHUB_REPO: "a dedicated evaluation repository (config github.repo)",
        cases.SECOND_DESTINATION: "a second destination AWS account (config second_destination_account)",
        cases.HOSTED: "hosted dispatch configuration (config websocket_url + hosted_tasks_queue_url)",
        cases.THREE_DEPLOYMENTS: (
            "three separately reachable ADP deployments, each with its own sign-in "
            "fixture (config deployments: three entries with distinct name, "
            "gateway_url and credential_secret_name — never one URL under three "
            "names, which the CLI treats as aliases of a single deployment)"
        ),
        cases.MULTI_DEPLOYMENT_MODEL_LIMITS: (
            "implementation of hard Codex output limits (at most 256 tokens per "
            "request) and an aggregate 48-request ceiling before inference; "
            "E16/E17 model execution is disabled until these limits are enforced"
        ),
        cases.SUPERPLANE_DOMAIN: (
            cleanup.SUPERPLANE_RECOVERY_BLOCKER
            + " A deployed domain and an ordinary-session fixture are also required."
        ),
    }
    absent = {}
    for fixture, description in names.items():
        if fixture in available:
            continue
        blocked = sorted(case.id for case in cases.CASES if fixture in case.requires)
        if blocked:
            absent[fixture] = {"needs": description, "blocks": blocked}
    return absent
