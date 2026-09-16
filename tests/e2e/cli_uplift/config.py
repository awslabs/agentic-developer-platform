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
SECRET_KEYS = re.compile(
    r"password|token|secret|access.?key|external.?id|private.?key|cookie|credential",
    re.I,
)

# Keys that legitimately contain a matched word but name an ENDPOINT or an ARN
# reference rather than carrying a credential value. Everything else matching
# SECRET_KEYS is refused outright.
SECRET_KEY_ALLOWED = frozenset({"secrets_endpoint", "credential_secret_name"})

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
    # The instance profile the disposable EC2 instance runs under. It must grant
    # only SSM plus the evaluation's own read access; the CLI journeys obtain AWS
    # access through the product's own connect flow, which is the thing under
    # test. Defaulted rather than required so a dispatch cannot fail at the
    # launch call with a bare KeyError -- preflight verifies the name exists.
    "instance_profile": "adp-cli-uplift-eval-instance",
    "instance_type": "t3.small",
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


def validate(config):
    """Validate and default a config dict, returning the normalized copy."""
    require(isinstance(config, dict), "Config must be a JSON object")
    no_secrets(config)

    missing = [key for key in REQUIRED if not config.get(key)]
    require(not missing, "Config is missing required keys: " + ", ".join(missing))

    result = {**DEFAULTS, **config}

    url = str(result["gateway_url"]).rstrip("/")
    require(url.startswith("https://"), "gateway_url must be HTTPS")
    require(
        "@" not in url and "?" not in url and "#" not in url,
        "gateway_url must not carry credentials or a query",
    )
    result["gateway_url"] = url

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
    ):
        value = result[key]
        require(type(value) is int and value > 0, f"{key} must be a positive integer")
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

    return result


def require_bindings(config, suites):
    """Require only the credentials used by the selected scenarios.

    Install/login are the first checkpoint and must not depend on Bedrock roles.
    Fixture values stay in Secrets Manager; this config carries only references.
    """
    from . import cases

    selected = cases.resolve_suites(suites)
    required = []
    if any(cases.DESTINATION in case.requires for case in selected):
        required.extend(("destination_role_arn", "provisioner_role_arn"))
    if any(case.id not in ("E01", "E15") for case in selected):
        required.append("credential_secret_name")
    missing = [key for key in required if not config.get(key)]
    require(
        not missing,
        "Config is missing the bindings this suite needs: "
        + ", ".join(missing)
        + ". Supply the destination role ARN, the provisioner role ARN and the "
        "fixture secret NAME (never a credential value) via the "
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
}


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

    `CLI_UPLIFT_EVAL_BINDINGS` may name a reviewed non-secret binding file
    (tests/e2e/cli_uplift/bindings.dev.json) that layers between the example and
    the environment overlay. It exists because the identifiers below are carried
    by repository VARIABLES that the executing identity cannot write (HTTP 403 on
    the Actions variables API), and blocking the evaluation on a privileged
    GitHub write is worse than carrying the same non-secret references in review.
    It is layered UNDER the overlay, so a repository variable always wins once
    set and this file never has to be removed to hand control back. It is passed
    through the same validate()/no_secrets() gate as every other config, so it
    cannot introduce a credential.
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
    return validate(document)


def fixture_classes(config):
    """Which fixture classes the config alone makes possible.

    Preflight narrows this further with live checks. Cases requiring a class not
    returned here are blocked before any mutation happens.
    """
    from . import cases

    available = {cases.PLATFORM, cases.DESTINATION, cases.EC2, cases.COGNITO}
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
    return available
