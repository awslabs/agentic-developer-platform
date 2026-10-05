"""Configuration and the pinned revisions this evaluation trusts.

Nothing here reaches the network. `validate()` is strict on purpose: an
evaluation that silently defaults a target account or region can produce a
confident green result about the wrong environment, which is worse than failing.

Secrets never live in this file. Credentials arrive as short-lived Actions/EC2
role sessions, and any fixture password or token is fetched at run time from
Secrets Manager by reference. `no_secrets()` enforces that structurally.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

# The reviewed, immutable revision of the #5173 EC2 tenant-validation harness.
# It is NOT on main (verified at implementation time), so the workflow fetches
# this exact object rather than trusting a branch or a local checkout. Changing
# this constant is a reviewable diff, which is the point.
HARNESS_COMMIT = "62b03d343181978aeb54ef1b29634204d050637d"

# The harness imports as `tests.e2e.tenant_validation` and derives the CLI
# directory as parents[3] / "modules/gateway/cli", so it must be unpacked at a
# repository root, at exactly that depth.
HARNESS_MODULE_PATH = "tests/e2e/tenant_validation"
HARNESS_SENTINEL = HARNESS_MODULE_PATH + "/regression.py"

# The one-line CLI change the harness depends on. At HARNESS_COMMIT,
# bg-cognito-auth.sh honours BG_CONFIG_DIR; on main it still hardcodes
# ${HOME}/.bedrock-gateway. Without it the on-instance worker writes into the
# invoking user's real config directory instead of the per-run isolated one,
# which silently substitutes another identity's session for the one under test.
# Preflight asserts this and blocks rather than running contaminated.
REQUIRED_CLI_CONFIG_DIR_LINE = 'CONFIG_DIR="${BG_CONFIG_DIR:-${HOME}/.bedrock-gateway}"'
CLI_AUTH_HELPER = "modules/gateway/cli/bg-cognito-auth.sh"

ACCOUNT = re.compile(r"^[0-9]{12}$")
REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::([0-9]{12}):role/.+$")

# #5413: the name a deployment is registered under by `adp deployment add`. The
# same character class the CLI's own registry accepts, restated here so an
# unusable name is refused while the config is still being validated rather than
# by a failing `adp deployment add` an hour into a live run.
DEPLOYMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# How many independent deployments E16/E17 need. Three, because the product
# requirement is three concurrent terminals and because two cannot distinguish
# "each command reaches its own deployment" from "commands alternate between the
# two". It is a floor, not a cap: a binding set with more is still usable.
REQUIRED_DEPLOYMENTS = 3

# R3: the bindings the destination account and the fixture credentials are reached
# through. Nothing supplied them before — not the example config, not the workflow
# overlay — so `_identity("destination")` raised "No destination role is
# configured" and the worker had no `credential_secret_name` to read a fixture
# password from. They are ARNs and a secret NAME: references, never values. The
# credentials themselves are only ever a short-lived assumed-role session.
#
# Required for every suite that touches the destination account or logs in, which
# is all of them except `harness`. Declared per-suite rather than globally so a
# harness-only self-check does not demand live fixture plumbing it never uses.
BINDINGS = ("destination_role_arn", "provisioner_role_arn", "credential_secret_name")
# Separate from `BINDINGS` because it is a different kind of thing: every name in
# `BINDINGS` is one identifier for one AWS resource the run assumes into, whereas
# this is a list of three deployment records (#5413). Keeping it out of `BINDINGS`
# leaves the places that iterate that tuple expecting a scalar identifier — the
# summary line, the overlay guard — correct without a special case, while
# `require_bindings()` still reports both together.
FIXTURE_BINDINGS = ("deployments",)
SECRET_KEYS = re.compile(
    r"password|token|secret|access.?key|external.?id|private.?key|cookie|credential",
    re.I,
)

# Keys that legitimately contain a matched word but name an ENDPOINT or an ARN
# reference rather than carrying a credential value. Everything else matching
# SECRET_KEYS is refused outright.
SECRET_KEY_ALLOWED = frozenset(
    {
        "secrets_endpoint",
        "credential_secret_name",
        # #5637. A Secrets Manager NAME carrying E18's separately onboarded
        # ordinary session. The inherited run session is the administrator;
        # `validate()` additionally refuses an ARN, URL or inline value here.
        "ordinary_session_secret_name",
    }
)

REQUIRED = (
    "gateway_url",
    "region",
    "platform_account",
    "destination_account",
    "expected_revision",
    "vpc_id",
    "private_subnet_id",
    "cognito_user_pool_id",
)

DEFAULTS = {
    # Bounds. Every one of these caps a real cost or a real hang; the offline
    # tests assert they are enforced rather than merely present.
    "timeout_seconds": 240,
    "evidence_wait_seconds": 180,
    "cleanup_wait_seconds": 120,
    "max_instances": 1,
    "max_run_minutes": 180,
    "daily_budget_usd": 5,
    # Instance TTL for the independent recovery sweep. Cancellation and runner
    # loss both skip `always()` blocks, so the manifest plus this TTL is what
    # actually guarantees termination.
    "instance_ttl_minutes": 240,
    "claude_version": "2.1.236",
    "codex_version": "0.154.0",
    # The model E08 asks for a completion from. Configurable because the model a
    # destination account has Bedrock access to is a property of that account, not
    # of this harness.
    "claude_model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    # The local auth proxy `adp codex` starts. Deliberately not the product's 9191
    # default: a listener a previous attempt on this instance left behind must not
    # be mistakable for the one this run started.
    "proxy_port": 9273,
    # How long a journey waits for an asynchronous record of a request it made.
    # ADP's own usage log is written on the request path; CloudTrail is not, and
    # its delivery delay is why these differ by an order of magnitude.
    "usage_wait_seconds": 180,
    "cloudtrail_wait_seconds": 900,
    # One model call, including a cold provider start.
    "inference_timeout_seconds": 300,
    # #5413's per-request output cap, measured in output TOKENS. The issue bounds a
    # live multi-deployment run at 256 of them per request, and the concurrency case
    # makes nine calls (three deployments x two arrangements, plus the lifecycle
    # re-checks), so the bound has to be enforced on the call rather than trusted to
    # a short prompt. Configurable, but capped below, because raising it is a spend
    # decision.
    #
    # Named `..._length` rather than `..._tokens` because `no_secrets()` refuses any
    # key matching /token/ anywhere in the tree, and that guard is worth more than
    # the more natural name: widening the allowlist to admit one integer would admit
    # every future key that happened to match it too.
    "max_output_length": 256,
    # How long to wait before concluding a request did NOT reach a deployment.
    # Shorter than `usage_wait_seconds` on purpose: proving an absence is the
    # cheap half of the crossed-request check and it runs six times per
    # arrangement, but it must still be long enough that a slow-but-correct write
    # is not read as a clean miss.
    "absence_wait_seconds": 60,
    # The instance profile the disposable EC2 instance runs under. It must grant
    # only SSM plus the evaluation's own read access; the CLI journeys obtain AWS
    # access through the product's own connect flow, which is the thing under
    # test. Defaulted rather than required so a dispatch cannot fail at the
    # launch call with a bare KeyError -- preflight verifies the name exists.
    "instance_profile": "adp-cli-uplift-eval-instance",
    "instance_type": "t3.small",
    "instance_security_group_id": "",
}


class ConfigError(ValueError):
    """Configuration is unusable. Raised before any mutation."""


def require(condition, message):
    if not condition:
        raise ConfigError(message)


def no_secrets(value, path="config"):
    """Refuse any credential-shaped key anywhere in the tree."""
    if isinstance(value, dict):
        for key, item in value.items():
            require(
                str(key) in SECRET_KEY_ALLOWED or not SECRET_KEYS.search(str(key)),
                f"{path}.{key} looks like a secret; pass a Secrets Manager reference instead",
            )
            no_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            no_secrets(item, f"{path}[{index}]")


def validate_assistant_websocket_url(value):
    message = (
        "assistant_users requires a clean wss:// websocket_url with a hostname "
        "and optional port from 1 to 65535; credentials, query, fragment and "
        "whitespace/control characters are not allowed"
    )
    require(
        isinstance(value, str)
        and bool(value)
        and not any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        and not any(marker in value for marker in ("?", "#")),
        message,
    )
    try:
        websocket = urlsplit(value)
        port = websocket.port
        hostname = websocket.hostname or ""
    except ValueError:
        raise ConfigError(message) from None
    authority_host = (
        websocket.netloc.rsplit(":", 1)[0] if port is not None else websocket.netloc
    )
    expected_host = f"[{hostname}]" if ":" in hostname else hostname
    require(
        websocket.scheme == "wss"
        and bool(hostname)
        and websocket.username is None
        and websocket.password is None
        and authority_host.lower() == expected_host.lower()
        and (port is None or 1 <= port <= 65535),
        message,
    )


def validate(config):
    """Validate and default a config dict, returning the normalized copy."""
    require(isinstance(config, dict), "Config must be a JSON object")
    no_secrets(config)

    missing = [key for key in REQUIRED if not config.get(key)]
    require(not missing, "Config is missing required keys: " + ", ".join(missing))

    result = {**DEFAULTS, **config}

    if result.get("gateway_deployment") is not None:
        require(
            result["gateway_deployment"]
            in ("dev", "pre-production", "customer-demo", "example-demo"),
            "Unknown gateway_deployment binding",
        )
    url = str(result["gateway_url"]).rstrip("/")
    require(url.startswith("https://"), "gateway_url must be HTTPS")
    require(
        "@" not in url and "?" not in url and "#" not in url,
        "gateway_url must not carry credentials or a query",
    )
    result["gateway_url"] = url

    if result.get("assistant_users") and result.get("websocket_url") not in (None, ""):
        validate_assistant_websocket_url(result["websocket_url"])

    require(REGION.match(str(result["region"])), "region is not a valid AWS region")
    for key in ("platform_account", "destination_account"):
        require(
            ACCOUNT.match(str(result[key])),
            f"{key} must be a 12-digit account ID string",
        )
    require(
        result["platform_account"] != result["destination_account"],
        "destination_account must differ from platform_account so cross-account routing is actually proven",
    )
    require(
        REVISION.match(str(result["expected_revision"])),
        "expected_revision must be a full 40-character commit SHA",
    )

    require(str(result["vpc_id"]).startswith("vpc-"), "vpc_id must be a VPC ID")
    require(
        str(result["private_subnet_id"]).startswith("subnet-"),
        "private_subnet_id must be a subnet ID",
    )

    # A second destination account is optional, but E07 needs it to prove that
    # distinct rungs resolve to genuinely distinct accounts. Absent means E07 is
    # blocked, not silently downgraded to a one-account approximation.
    second = result.get("second_destination_account")
    if second:
        require(
            ACCOUNT.match(str(second)),
            "second_destination_account must be a 12-digit account ID string",
        )
        require(
            second not in (result["platform_account"], result["destination_account"]),
            "second_destination_account must differ from the platform and first destination accounts",
        )

    for key in (
        "timeout_seconds",
        "evidence_wait_seconds",
        "cleanup_wait_seconds",
        "max_instances",
        "max_run_minutes",
        "instance_ttl_minutes",
        "max_output_length",
        "absence_wait_seconds",
    ):
        value = result[key]
        require(type(value) is int and value > 0, f"{key} must be a positive integer")
    # #5413's stated live bound. A run that quietly asked for more output per
    # request than the issue authorised would be spending outside its approval,
    # and the number is small enough that no legitimate marker reply needs more.
    require(
        result["max_output_length"] <= 256,
        "max_output_length is capped at 256, the per-request output bound the "
        "multi-deployment live run is authorised for",
    )
    require(
        result["absence_wait_seconds"] <= result["usage_wait_seconds"],
        "absence_wait_seconds must not exceed usage_wait_seconds: an absence "
        "proven in longer than a presence takes to appear proves nothing",
    )
    require(
        result["max_instances"] <= 4,
        "max_instances is capped at 4; this evaluation needs one disposable instance per journey",
    )
    require(
        result["max_run_minutes"] <= 360,
        "max_run_minutes is capped at 360 to bound spend on a stuck run",
    )
    require(
        10 <= result["timeout_seconds"] <= 900,
        "timeout_seconds must be between 10 and 900",
    )
    require(
        result["instance_ttl_minutes"] >= result["max_run_minutes"],
        "instance_ttl_minutes must not expire before max_run_minutes",
    )

    budget = result["daily_budget_usd"]
    require(
        type(budget) in (int, float) and type(budget) is not bool and 0 < budget <= 100,
        "daily_budget_usd must be between 0 and 100",
    )

    # The private subnet in the approved test VPC has previously required
    # regional FIPS endpoints for STS and Secrets Manager. Default to them so a
    # run in that subnet works without per-run tuning.
    result.setdefault(
        "sts_endpoint", f"https://sts-fips.{result['region']}.amazonaws.com"
    )
    result.setdefault(
        "secrets_endpoint",
        f"https://secretsmanager-fips.{result['region']}.amazonaws.com",
    )
    for key in ("sts_endpoint", "secrets_endpoint"):
        require(
            str(result[key]).startswith("https://"), f"{key} must be an HTTPS endpoint"
        )

    group = result.get("instance_security_group_id")
    require(
        not group
        or (isinstance(group, str) and re.fullmatch(r"sg-[0-9a-f]{8,17}", group)),
        "instance_security_group_id must be a security group ID",
    )

    for key in ("instance_profile", "instance_type"):
        require(
            isinstance(result[key], str) and result[key],
            f"{key} must be a non-empty string",
        )

    # Durable run state, keyed by evaluation ID. Optional, because a local run
    # can use its own state directory; without it, resume/status/cleanup cannot
    # work across Actions runs, since each job's /tmp dies with its runner.
    # NEVER an ordinary Actions artifact: this state holds disposable passwords,
    # session tokens and ExternalIds until cleanup, so it goes to encrypted,
    # access-scoped storage or nowhere.
    bucket = result.get("state_bucket")
    if bucket:
        require(
            isinstance(bucket, str) and not str(bucket).startswith("s3://"),
            "state_bucket must be a bare bucket name, not an s3:// URL",
        )
        key_id = result.get("state_kms_key_id")
        if key_id:
            require(
                isinstance(key_id, str) and key_id,
                "state_kms_key_id must be a non-empty string",
            )

    # R3: the destination/provisioner bindings. Validated for SHAPE here whenever
    # present — an ARN in the wrong account is the mistake that would otherwise
    # surface as a cross-account test passing against the platform account itself.
    # Whether a given run REQUIRES them depends on the selected suites, so that
    # decision lives in `require_bindings()` below.
    for key in ("destination_role_arn", "provisioner_role_arn"):
        value = result.get(key)
        if value:
            found = ROLE_ARN.match(str(value))
            require(found, f"{key} must be an IAM role ARN")
            require(
                found.group(1) == str(result["destination_account"]),
                f"{key} names account {found.group(1)}, not the configured destination "
                f"account {result['destination_account']}; cross-account access would not be proven",
            )
    secret = result.get("credential_secret_name")
    if secret:
        require(
            isinstance(secret, str)
            and not secret.startswith("arn:")
            and "://" not in secret,
            "credential_secret_name must be a Secrets Manager secret NAME, not an ARN or URL",
        )

    # GitHub fixtures are named here or the GitHub cases block. There is
    # deliberately no fallback to a shared App: resetting one would break real
    # users, and a seeded token is not a login.
    github = result.get("github") or {}
    require(isinstance(github, dict), "github must be an object")
    if github:
        for key in ("org", "app_fixture", "existing_app_fixture", "repo"):
            if github.get(key):
                require(
                    isinstance(github[key], str) and github[key],
                    f"github.{key} must be a non-empty string",
                )
    result["github"] = github

    contrast = result.get("capability_contrast") or {}
    require(isinstance(contrast, dict), "capability_contrast must be an object")
    if contrast:
        required = (
            "disabled_feature",
            "enabled_feature",
            "disabled_operation",
            "enabled_operation",
            "denied_operation",
            "foreign_request_id",
            "ordinary_fixture_name",
        )
        missing_contrast = [
            key
            for key in required
            if not isinstance(contrast.get(key), str) or not contrast.get(key)
        ]
        require(
            not missing_contrast,
            "capability_contrast is missing: " + ", ".join(missing_contrast),
        )
        for key in ("disabled_operation", "enabled_operation", "denied_operation"):
            require(
                re.fullmatch(r"[a-z][a-z0-9_.]{2,127}", contrast[key]),
                f"capability_contrast.{key} is not an operation ID",
            )
        require(
            re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", contrast["disabled_feature"]),
            "capability_contrast.disabled_feature is not a feature name",
        )
        require(
            re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", contrast["enabled_feature"]),
            "capability_contrast.enabled_feature is not a feature name",
        )
        require(
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", contrast["foreign_request_id"]),
            "capability_contrast.foreign_request_id is not a request ID",
        )
        require(
            not contrast["ordinary_fixture_name"].startswith("arn:")
            and "://" not in contrast["ordinary_fixture_name"],
            "capability_contrast.ordinary_fixture_name must be a Secrets Manager name",
        )
    result["capability_contrast"] = contrast
    tenant_fixture = result.get("tenant_isolation") or {}
    require(isinstance(tenant_fixture, dict), "tenant_isolation must be an object")
    if tenant_fixture:
        tenant_ids = tenant_fixture.get("tenant_ids")
        require(
            isinstance(tenant_ids, list)
            and len(tenant_ids) == 2
            and all(isinstance(t, str) and 0 < len(t) <= 255 for t in tenant_ids)
            and len(set(tenant_ids)) == 2,
            "tenant_isolation.tenant_ids must name two distinct existing memberships",
        )
    result["tenant_isolation"] = tenant_fixture

    # #5413: the three deployment bindings E16/E17 run against. Absent means those
    # two cases BLOCK (see `fixture_classes`), which is the honest state until a
    # coordinator supplies real integration and pre-production URLs.
    #
    # Validated for SHAPE whenever present, and the two rules below are the ones
    # that stop a weaker fixture from passing as three deployments:
    #
    # * distinct gateway URLs. Three names pointing at one URL are ALIASES — one
    #   session and one stable id by design — so registering them would satisfy a
    #   count while proving nothing about isolation.
    # * a distinct credential reference per deployment. Independent logins are the
    #   subject of AC-03/AC-11; one shared identity could not demonstrate that
    #   logging out of one leaves the others signed in.
    deployments = result.get("deployments") or []
    require(
        isinstance(deployments, list),
        "deployments must be a list of {name, gateway_url, credential_secret_name} objects",
    )
    if deployments:
        require(
            len(deployments) == REQUIRED_DEPLOYMENTS,
            f"deployments needs exactly {REQUIRED_DEPLOYMENTS} entries for the "
            "multi-deployment cases; fewer cannot prove three concurrent sessions "
            "stay independent",
        )
        names, urls, secrets = [], [], []
        for index, entry in enumerate(deployments):
            where = f"deployments[{index}]"
            require(isinstance(entry, dict), f"{where} must be an object")
            name = str(entry.get("name") or "")
            require(
                DEPLOYMENT_NAME.match(name),
                f"{where}.name must be a deployment name `adp deployment add` accepts "
                "(lower-case letters, digits and hyphens, starting with a letter)",
            )
            url = str(entry.get("gateway_url") or "").rstrip("/")
            require(url.startswith("https://"), f"{where}.gateway_url must be HTTPS")
            require(
                "@" not in url and "?" not in url and "#" not in url,
                f"{where}.gateway_url must not carry credentials or a query",
            )
            secret = str(entry.get("credential_secret_name") or "")
            require(
                secret and not secret.startswith("arn:") and "://" not in secret,
                f"{where}.credential_secret_name must be a Secrets Manager secret "
                "NAME, not an ARN, a URL or a credential value",
            )
            names.append(name)
            urls.append(url)
            secrets.append(secret)
            entry["gateway_url"] = url
        require(
            len(set(names)) == len(names),
            "Each deployment binding needs its own name; `adp deployment add` treats "
            "a repeated name as the same record",
        )
        require(
            len(set(urls)) == len(urls),
            "Each deployment binding needs its own gateway URL. Two names for one URL "
            "are aliases — one session and one stable id — so they cannot demonstrate "
            "that three deployments stay isolated",
        )
        require(
            len(set(secrets)) == len(secrets),
            "Each deployment binding needs its own credential reference. A shared "
            "identity cannot show that logging out of one deployment leaves the "
            "others signed in",
        )
    result["deployments"] = deployments

    # #5637: the Superplane domain fixture E18 runs against. Absent means E18
    # BLOCKS, which is the honest state until a domain service is actually deployed
    # behind the gateway in a reachable environment.
    #
    # The inherited run session is the already-verified administrator. E18 needs
    # a separately onboarded ordinary session because quota-setting is admin work,
    # and using one identity for both cannot prove the ordinary-user negative path.
    superplane = result.get("superplane") or {}
    require(isinstance(superplane, dict), "superplane must be an object")
    if superplane:
        for key in (
            "base_path",
            "ordinary_session_secret_name",
            "model_name",
            "aws_connection_id",
        ):
            value = superplane.get(key)
            require(
                isinstance(value, str) and value,
                f"superplane.{key} must be a non-empty string",
            )
        require(
            superplane["base_path"].startswith("/"),
            "superplane.base_path must be the gateway-relative API base, for "
            "example /superplane/v1",
        )
        secret = superplane["ordinary_session_secret_name"]
        require(
            not secret.startswith("arn:") and "://" not in secret,
            "superplane.ordinary_session_secret_name must be a Secrets Manager "
            "secret NAME, not an ARN, a URL or a credential value",
        )
        connection_id = superplane["aws_connection_id"]
        require(
            not connection_id.startswith("arn:") and "://" not in connection_id,
            "superplane.aws_connection_id must be an opaque ADP credential ID, not an ARN or URL",
        )
    result["superplane"] = superplane
    research = result.get("research_readback", False)
    require(
        type(research) is bool,
        "research_readback must be a boolean fixture declaration",
    )
    result["research_readback"] = research

    return result


def require_bindings(config, suites):
    """Require only the credentials used by the selected scenarios.

    Install/login are the first checkpoint and must not depend on Bedrock roles.
    Fixture values stay in Secrets Manager; this config carries only references.
    """
    from . import cases

    selected = cases.resolve_suites(suites)
    required = []
    destination_keys = ("destination_role_arn", "provisioner_role_arn")
    if any(cases.DESTINATION in case.requires for case in selected):
        required.extend(destination_keys)
    if any(case.id not in ("E01", "E15") for case in selected):
        required.append("credential_secret_name")
    # #5413: the same treatment as the destination roles. An operator who asked
    # for `multi-deployment` by name is told immediately that the three bindings
    # are absent; a `full` dispatch blocks E16/E17 further down and still grades
    # everything else, because a report of what DID run is more useful than no
    # report at all.
    fixture_keys = FIXTURE_BINDINGS
    if any(cases.THREE_DEPLOYMENTS in case.requires for case in selected):
        required.extend(fixture_keys)

    # `full` is the only selection that can grant acceptance, and it must always
    # produce a graded report: absent destination roles block E04-E08 (and, via
    # the fixture gate, only those), leaving the rest of the matrix to run and
    # `full_acceptance` false because BLOCKED is not PASSED. Aborting instead
    # produced no report at all, so a fixture gap was indistinguishable from a
    # harness crash and the cases that *were* runnable never ran.
    #
    # An explicitly named destination suite still refuses: the operator asked for
    # exactly those cases, so "none of them can run" is the useful answer, not a
    # report of nothing but blocks.
    enforced = required
    if cases.is_full(suites):
        exempt = set(destination_keys) | set(fixture_keys)
        enforced = [key for key in required if key not in exempt]

    missing = [key for key in enforced if not config.get(key)]
    require(
        not missing,
        "Config is missing the bindings this suite needs: "
        + ", ".join(missing)
        + ". Supply the destination role ARN, the provisioner role ARN, the "
        "fixture secret NAME and (for the multi-deployment cases) the three "
        "deployment bindings — never a credential value — via the "
        "CLI_UPLIFT_EVAL_* environment variables. The login checkpoint needs "
        "only the credential reference, not destination roles.",
    )
    return tuple(required)


def load(path):
    """Load and validate a config file."""
    text = Path(path).read_text()
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"Config is not valid JSON: {exc}") from None
    return validate(raw)


# The example file is the non-secret base every run starts from; environment
# overlays supply the revision under test and the fixture REFERENCES for the
# environment. Every name is a repository variable, never a secret, because every
# value is an identifier — an account number, a role ARN, a bucket, a secret NAME.
EXAMPLE_PATH = Path(__file__).resolve().parent / "config.example.json"

# env var -> config key. A dotted key lands inside `github`.
OVERLAY = {
    "CLI_UPLIFT_EVAL_INSTANCE_PROFILE": "instance_profile",
    "CLI_UPLIFT_EVAL_SECURITY_GROUP_ID": "instance_security_group_id",
    "CLI_UPLIFT_EVAL_EXPECTED_REVISION": "expected_revision",
    "CLI_UPLIFT_EVAL_GITHUB_ORG": "github.org",
    "CLI_UPLIFT_EVAL_GITHUB_REPO": "github.repo",
    "CLI_UPLIFT_EVAL_GITHUB_APP": "github.app_fixture",
    "CLI_UPLIFT_EVAL_SECOND_DESTINATION_ACCOUNT": "second_destination_account",
    "CLI_UPLIFT_EVAL_STATE_BUCKET": "state_bucket",
    "CLI_UPLIFT_EVAL_STATE_KMS_KEY_ID": "state_kms_key_id",
    "CLI_UPLIFT_EVAL_DESTINATION_ROLE_ARN": "destination_role_arn",
    "CLI_UPLIFT_EVAL_PROVISIONER_ROLE_ARN": "provisioner_role_arn",
    "CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME": "credential_secret_name",
    "CLI_UPLIFT_EVAL_CAP_DISABLED_FEATURE": "capability_contrast.disabled_feature",
    "CLI_UPLIFT_EVAL_CAP_ENABLED_FEATURE": "capability_contrast.enabled_feature",
    "CLI_UPLIFT_EVAL_CAP_DISABLED_OPERATION": "capability_contrast.disabled_operation",
    "CLI_UPLIFT_EVAL_CAP_ENABLED_OPERATION": "capability_contrast.enabled_operation",
    "CLI_UPLIFT_EVAL_CAP_DENIED_OPERATION": "capability_contrast.denied_operation",
    "CLI_UPLIFT_EVAL_CAP_FOREIGN_REQUEST_ID": "capability_contrast.foreign_request_id",
    "CLI_UPLIFT_EVAL_CAP_ORDINARY_FIXTURE": "capability_contrast.ordinary_fixture_name",
}

# #5413. The three deployment bindings, as a JSON array, because they are a list
# of objects and every other overlay entry is a single scalar. Kept out of
# `OVERLAY` rather than bolted onto it so the scalar path stays a plain string
# assignment and cannot start silently parsing JSON out of an account number.
#
# A repository VARIABLE, never a secret: each entry carries a name, an HTTPS URL
# and a Secrets Manager secret NAME. `validate()`/`no_secrets()` refuse a
# credential in it, so a pasted password fails the offline guards.
DEPLOYMENTS_VARIABLE = "CLI_UPLIFT_EVAL_DEPLOYMENTS"


def from_environment(env, *, base=None):
    """The run config, built from the example plus the environment overlay.

    R5: this used to be an inline Python heredoc in the evaluate job, so the
    RECOVERY job — a separate job, on a fresh runner — had no way to obtain the
    same config. It fell back to the checked-in example, which carries no
    `state_bucket`, so `--restore` raised "needs a durable state store" on every
    run; the workflow turned that into a warning and the recovery gate passed
    green while the run's IAM roles, stacks, secrets and Cognito users were still
    live. One function both jobs call is what makes the two configs the same
    config, and makes that property testable offline.

    An absent overlay value is left ABSENT rather than defaulted: a placeholder
    role ARN would produce a cross-account test that silently ran against one
    account, and an absent GitHub fixture must block its cases rather than
    downgrade them to something weaker that passes.

    `CLI_UPLIFT_EVAL_BINDINGS` can name a local binding example. Real target
    identifiers belong in private configuration: CLI_UPLIFT_EVAL_BINDINGS_JSON
    overlays the example before individual environment overrides. Both forms
    pass the same structural credential guard; only fixture references belong
    in bindings, never passwords, tokens, or credential values.
    """
    document = dict(base) if base is not None else json.loads(EXAMPLE_PATH.read_text())
    if base is None:
        bindings_path = str(env.get("CLI_UPLIFT_EVAL_BINDINGS") or "").strip()
        if bindings_path:
            try:
                overlay = json.loads(Path(bindings_path).read_text())
            except OSError as exc:
                raise ConfigError(
                    f"CLI_UPLIFT_EVAL_BINDINGS names {bindings_path!r}, "
                    f"which could not be read: {exc}"
                ) from None
            except ValueError as exc:
                raise ConfigError(
                    f"CLI_UPLIFT_EVAL_BINDINGS file is not valid JSON: {exc}"
                ) from None
            require(
                isinstance(overlay, dict),
                "CLI_UPLIFT_EVAL_BINDINGS file must be a JSON object",
            )
            # `_`-prefixed keys are this file's own documentation and are dropped
            # BEFORE the guard runs, not after: they never reach the run config,
            # and a prose key explaining the fixture reference would otherwise be
            # refused for merely containing a credential-shaped word.
            bindings = {
                key: value
                for key, value in overlay.items()
                if not str(key).startswith("_")
            }
            # Refused here as well as in validate(), so a credential in this file
            # fails before it is merged into the run config.
            no_secrets(bindings, "bindings")
            document.update(bindings)
    raw_bindings = str(env.get("CLI_UPLIFT_EVAL_BINDINGS_JSON") or "").strip()
    if base is None and raw_bindings:
        try:
            private_bindings = json.loads(raw_bindings)
        except ValueError:
            raise ConfigError(
                "CLI_UPLIFT_EVAL_BINDINGS_JSON is not valid JSON"
            ) from None
        require(
            isinstance(private_bindings, dict),
            "CLI_UPLIFT_EVAL_BINDINGS_JSON must be a JSON object",
        )
        no_secrets(private_bindings, "bindings")
        document.update(private_bindings)
    for name, key in OVERLAY.items():
        value = str(env.get(name) or "").strip()
        if not value:
            continue
        if "." in key:
            parent, child = key.split(".", 1)
            document.setdefault(parent, {})
            if not isinstance(document[parent], dict):
                raise ConfigError(f"{parent} must be an object to overlay {child!r}")
            document[parent][child] = value
        else:
            document[key] = value
    raw_deployments = str(env.get(DEPLOYMENTS_VARIABLE) or "").strip()
    if raw_deployments:
        try:
            parsed = json.loads(raw_deployments)
        except ValueError as exc:
            raise ConfigError(
                f"{DEPLOYMENTS_VARIABLE} is not valid JSON: {exc}. It must be an "
                "array of {name, gateway_url, credential_secret_name} objects"
            ) from None
        require(
            isinstance(parsed, list),
            f"{DEPLOYMENTS_VARIABLE} must be a JSON array of deployment bindings",
        )
        # Guarded before it is merged, like the bindings file, so a credential
        # pasted into the variable fails here rather than inside the run config.
        no_secrets(parsed, DEPLOYMENTS_VARIABLE)
        document["deployments"] = parsed
    raw_fixtures = str(env.get("CLI_UPLIFT_EVAL_FIXTURES") or "").strip()
    if raw_fixtures:
        from .fixtures import parse

        document.update(parse(raw_fixtures))
    return validate(document)


def fixture_classes(config):
    """Which fixture classes the config alone makes possible.

    Preflight narrows this further with live checks. Cases requiring a class not
    returned here are blocked before any mutation happens.
    """
    from . import cases

    available = {cases.PLATFORM, cases.EC2, cases.COGNITO}
    # Explicitly identifies an existing domain for read-only regression. This
    # does not assert readiness: actual authenticated CLI reads decide that.
    if config.get("research_readback") is True:
        available.add(cases.SUPERPLANE_RESEARCH)
    # The destination class is exactly "we hold a cross-account session", so it
    # depends on both role bindings the same way SECOND_DESTINATION depends on
    # its account. Claiming it unconditionally made the cross-account cases
    # attempt a destination they had no way to reach: they failed on a missing
    # ARN mid-journey instead of grading BLOCKED before any mutation, which is
    # the distinction between "the fixture is absent" and "the product is broken".
    if config.get("destination_role_arn") and config.get("provisioner_role_arn"):
        available.add(cases.DESTINATION)
    if config.get("second_destination_account"):
        available.add(cases.SECOND_DESTINATION)
    github = config.get("github") or {}
    if github.get("org") and (
        github.get("app_fixture") or github.get("existing_app_fixture")
    ):
        available.add(cases.GITHUB_APP)
    if github.get("repo"):
        available.add(cases.GITHUB_REPO)
    if config.get("hosted_tasks_queue_url") and config.get("websocket_url"):
        available.add(cases.HOSTED)
    from .fixtures import validate_fixture

    for key, fixture_class in (
        ("human_task_coding", cases.HUMAN_TASK_CODING),
        ("human_task_chat", cases.HUMAN_TASK_CHAT),
        ("vault_lifecycle", cases.VAULT_LIFECYCLE),
        ("hierarchy_lifecycle", cases.HIERARCHY_LIFECYCLE),
        ("knowledge_lifecycle", cases.KNOWLEDGE_LIFECYCLE),
        ("machine_lifecycle", cases.MACHINE_LIFECYCLE),
        ("budget_lifecycle", cases.BUDGET_LIFECYCLE),
    ):
        if config.get(key):
            try:
                validate_fixture(key, config[key])
            except ConfigError:
                continue
            available.add(fixture_class)
    if config.get("assistant_users"):
        try:
            validate_assistant_websocket_url(config.get("websocket_url"))
            validate_fixture("assistant_users", config["assistant_users"])
        except ConfigError:
            pass
        else:
            available.add(cases.ASSISTANT_USERS)
    # #5413. `validate()` has already refused a binding set that is too small, or
    # that reuses a URL or a credential reference, so reaching the required count
    # here means three genuinely distinct deployments were configured.
    if len(config.get("deployments") or []) >= REQUIRED_DEPLOYMENTS:
        available.add(cases.THREE_DEPLOYMENTS)
    if config.get("capability_contrast"):
        available.add(cases.CAPABILITY_CONTRAST)
    # #5637. Configured is not the same as reachable here either, and preflight
    # discards this class again if the domain does not answer through the gateway —
    # a 404 from the proxy means nothing is mounted behind the allowlist, which
    # must block E18 before it mutates anything rather than fail mid-journey.
    superplane = config.get("superplane") or {}
    if all(
        superplane.get(key)
        for key in (
            "base_path",
            "ordinary_session_secret_name",
            "model_name",
            "aws_connection_id",
        )
    ):
        available.add(cases.SUPERPLANE_DOMAIN)
    return available
