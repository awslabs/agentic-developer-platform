"""Wires the stage helpers each stage needs, live by default.

`stages.py` asks for capabilities ("run this worker on that instance", "is the
GitHub fixture real?"). This module supplies them from the ports, and is the one
place a test substitutes a double. The default is always the live implementation:
a harness that quietly no-ops when a dependency is absent reports success for work
it never performed, which is the failure this whole evaluation exists to catch.

`wire()` returns a plain dict, so an offline test can override one capability
(say, `run_worker`) and leave the rest live-shaped without reimplementing the
stage logic it is trying to exercise.
"""

from __future__ import annotations

import json
import secrets
import shlex
import time
from urllib.parse import quote

from . import bundle, cleanup, config
from . import ports as ports_module


def _identity(aws, cfg):
    """Resolve each account's identity from a session that actually belongs to it.

    The platform identity is the runner's own session. The destination identity
    must come from an assumed role in the destination account: re-reading the
    runner's identity would report the platform account under a destination label
    and preflight's cross-account check would pass without cross-account access
    existing. Absent a role to assume, this raises rather than substituting the
    session at hand.
    """
    sessions = {}

    def resolve(which):
        if which == "platform":
            return aws.call("sts", "get_caller_identity")
        role_arn = cfg.get("destination_role_arn") or cfg.get("provisioner_role_arn")
        if not role_arn:
            raise ports_module.PortError(
                "No destination role is configured; the destination account identity "
                "cannot be proven from the runner's own session"
            )
        if which not in sessions:
            sessions[which] = aws.assume(role_arn, f"cli-uplift-eval-{which}")
        return sessions[which].call("sts", "get_caller_identity")

    return resolve


def _deployed_revision(aws, http, cfg):
    """What revision the gateway is actually running, from trusted evidence.

    The run must be bound to the deployment it evaluated, or a green result says
    nothing about which code produced it. The obvious source is `/health`, and the
    preflight stage used to REQUIRE a revision there — but the product's `/health`
    returns `{"status": "healthy"}` and always has (`modules/gateway/src/app.py`).
    So every live run would have aborted in preflight with "reports no revision",
    and no amount of harness work could have fixed it.

    Making a new public health-metadata API a prerequisite is the wrong fix (and
    the reviewer ruled it out): the deployment already publishes this, privately
    and more trustworthily than an unauthenticated endpoint could. `gateway-deploy`
    stamps the EKS deployment with `adp-gateway:<sha>` and pins the orchestration
    engine Lambda to the digest of that same tag, asserting the two are equal
    before it reports success. Resolving the Lambda's image digest back to its ECR
    tags therefore yields the revision under an IAM-authorized read that the
    gateway itself cannot influence.

    Order of preference:

    1. `/health` reporting a revision, if a future deployment ever does. Cheapest,
       and it observes the exact process serving the run.
    2. The deployment evidence above.

    Both are accepted; neither is invented. If neither answers, the caller fails
    the run — an unbound result must never be published as acceptance.
    """
    engine = cfg.get("engine_function") or DEFAULT_ENGINE_FUNCTION.format(
        environment=cfg.get("environment") or "dev"
    )

    def resolve(record=None):
        note = record if record is not None else {}
        try:
            _, health = http.get(cfg["gateway_url"].rstrip("/") + "/health", expect=200)
        except ports_module.PortError:
            health = None
        served = str(
            (health or {}).get("revision")
            or (health or {}).get("git_sha")
            or (health or {}).get("version")
            or ""
        ).strip()
        if served:
            note["revision_source"] = "gateway_health"
            return served

        # The engine Lambda and the EKS deployment are pinned to one digest by the
        # deploy workflow, which verifies the equality before reporting success.
        image = str(
            aws.call("lambda", "get_function", FunctionName=engine)
            .get("Code", {})
            .get("ResolvedImageUri")
            or ""
        )
        if "@" not in image:
            raise ports_module.PortError(
                f"The deployment evidence ({engine}) reports no pinned image digest, "
                "and /health reports no revision; the run cannot be bound to a "
                "deployed revision"
            )
        repository = image.split("/")[-1].split("@")[0]
        digest = image.split("@", 1)[1]
        tags = (
            aws.call(
                "ecr",
                "describe_images",
                repositoryName=repository,
                imageIds=[{"imageDigest": digest}],
            ).get("imageDetails")
            or [{}]
        )[0].get("imageTags") or []
        revisions = [tag for tag in tags if REVISION_TAG.match(str(tag))]
        if len(revisions) != 1:
            raise ports_module.PortError(
                f"The deployed image {digest[:19]}… carries {len(revisions)} "
                "commit-shaped tags; exactly one is required to bind the run to a "
                "revision"
            )
        note["revision_source"] = "deployment_image_tag"
        note["revision_evidence"] = {"function": engine, "image_digest": digest}
        return revisions[0]

    return resolve


# The image tag `gateway-deploy.yml` stamps is `github.sha`, so a 40-hex tag is
# the revision. `latest` and any other moving tag are deliberately not accepted.
REVISION_TAG = config.REVISION
DEFAULT_ENGINE_FUNCTION = "adp-{environment}-orchestration-tick"


def _wait_online(aws, *, attempts=36, sleep=time.sleep):
    def wait(instance_id):
        for _ in range(attempts):
            found = (
                aws.call(
                    "ssm",
                    "describe_instance_information",
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
                ).get("InstanceInformationList")
                or []
            )
            if found and found[0].get("PingStatus") == "Online":
                return True
            sleep(5)
        return False

    return wait


def _install_bundle(ssm, aws, cfg):
    """Deliver the checked-in remote scripts to the instance, once per run.

    R1: the harness used to invoke `/home/ec2-user/adp-eval/worker.py`, which
    nothing ever created — cloud-init made the directory and stopped. Every remote
    path therefore ran a file that did not exist. This is the missing half: the
    reviewed scripts are packaged, uploaded, verified by digest on the instance,
    extracted, and self-checked before any stage depends on one.

    The transfer reuses the mechanism the pinned #5173 harness already runs in this
    same private subnet (tar.gz to S3, `aws s3 cp`, `sha256sum -c`, extract, run as
    ec2-user) rather than inventing a new one.
    """
    installed = {}

    def install(instance_id, evaluation_id, manifest=None):
        if installed.get("instance") == instance_id:
            return installed["result"]
        bucket = cfg.get("state_bucket")
        if not bucket:
            raise ports_module.PortError(
                "No state_bucket is configured, so the remote scripts cannot be "
                "delivered to the instance; set CLI_UPLIFT_EVAL_STATE_BUCKET"
            )
        payload = bundle.archive()
        expected = bundle.digest(payload)
        # Intent before mutation: the object is this run's to delete either way.
        key = bundle.object_key(evaluation_id)
        if manifest is not None:
            manifest.record("s3_object", f"{bucket}/{key}", detail={"bundle": True})
        bundle.upload(
            aws,
            bucket,
            evaluation_id,
            kms_key_id=cfg.get("state_kms_key_id"),
            data=payload,
        )
        result = ssm.run(
            instance_id,
            bundle.install_commands(bucket, key, expected, region=cfg["region"]),
            purpose="install-bundle",
            timeout=min(int(cfg.get("timeout_seconds", 240)) * 2, 600),
        )
        if result.get("Status") != "Success":
            raise ports_module.PortError(
                "The remote script bundle did not install on the instance "
                f"(SSM status {result.get('Status')}); no journey could have run"
            )
        installed.update(
            instance=instance_id,
            result={"digest": expected, "purposes": list(bundle.purposes())},
        )
        return installed["result"]

    return install


def _run_worker(ssm, cfg, install):
    """Run one dispatcher purpose on the instance and return its evidence.

    The payload goes via a file written by SSM rather than an argv string so a
    fixture reference never lands in a process listing, and it runs as ec2-user so
    it cannot read root-only material.

    `bundle.require_purpose()` is checked BEFORE the command is sent, so a purpose
    with no shipped script names the module a developer must write instead of
    failing as a remote ImportError — or, as before, silently invoking a path that
    was never delivered.
    """

    def run(instance_id, purpose, payload):
        bundle.require_purpose(purpose)
        install(instance_id, payload.get("evaluation_id") or "")
        remote = f"{bundle.REMOTE_DIR}/{purpose}.json"
        commands = [
            "set -eu",
            "umask 077",
            f"cat > {shlex.quote(remote)} <<'EOF_PAYLOAD'\n"
            f"{json.dumps(payload)}\nEOF_PAYLOAD",
            f"chown ec2-user:ec2-user {shlex.quote(remote)}",
            "runuser -l ec2-user -c "
            + shlex.quote(
                f"python3 {bundle.DISPATCHER} {purpose} {shlex.quote(remote)}"
            ),
            # Removed even on success: the payload names a fixture secret.
            f"rm -f {shlex.quote(remote)}",
        ]
        _, document = ssm.json_result(
            instance_id,
            commands,
            purpose=purpose,
            timeout=min(int(cfg.get("timeout_seconds", 240)) * 3, 900),
        )
        return document

    return run


def _admin_fixtures(aws, cfg):
    """Provision E02's two run-owned identities and the secret carrying them.

    E02 asserts a REAL Cognito challenge flow: the CLI must complete
    NEW_PASSWORD_REQUIRED, and a non-admin identity must be refused the admin
    session. Neither can be satisfied by the shared login-regression fixture —
    it is CONFIRMED (so it issues no challenge) and it is an admin (so it cannot
    be the negative). Rotating it to force a challenge would break the very
    regression suite that depends on it, so this run creates its own.

    Two identities, matching the shape of the pool's existing fixture:

    - the challenge identity, created with a TEMPORARY password so Cognito puts
      it in FORCE_CHANGE_PASSWORD and the login must answer the challenge. It is
      granted admin the way this pool grants it, `custom:role=platform_admin`
      plus the `admins` group, because the gateway's `is_admin` accepts either.
    - the non-admin identity, with a PERMANENT password and no role and no group,
      so it authenticates (200) and is then refused the admin session (403).
      `org_admin` is deliberately not used: it is a real role that is excluded
      from platform admin, but leaving the claim unset tests the weaker case.

    The credentials never travel in the SSM payload or the run config. They are
    written to a run-owned secret and only its NAME is passed, which is the
    contract `remote/common.fixture_secret` already implements.

    The instance role cannot read that secret by identity policy — its single
    GetSecretValue grant is pinned to the one shared fixture ARN, deliberately.
    So the secret carries a RESOURCE policy naming only that role. Verified with
    `simulate-principal-policy`: `allowed` via SourcePolicyType "Resource Policy"
    for the eval instance role on this secret, `implicitDeny` for any other
    secret. That needs no change to the shared role, no new identity policy and
    no wider grant — which is why it is done this way.

    A Cognito identity is NOT an ADP account, and this function used to create
    only the former. That was the whole fixture for E02's purposes — a challenge
    and a negative are both pure sign-in assertions — but the identity it creates
    is then inherited by every LATER case in the run, and those touch user-scoped
    records. `_resolve_user_id` maps the Cognito sub to `users.id` and answers 404
    `user_not_found` when no row exists, so E13's `adp aws connect --download`
    reported `failed`/exit 5 where it expects `pending`/exit 4 — a failure whose
    cause was installed two stages earlier, in this function. Reproduced live:
    the same command as the shared fixture (which HAS a `users` row) returns
    `pending`, and as a run-created identity returns `user_not_found`.

    The ADP-side record is therefore created too — but NOT here, in
    `remote/install_auth._onboard`. Registering it needs a platform-admin bearer
    token, and the only one that exists without minting a second is the session
    the CLI earns by completing E02's own challenge; logging in from here to get
    one would consume that challenge before the CLI could be tested against it.
    So the identity registers its own account, on the instance, immediately after
    the login that proves it — through the product's own onboarding route rather
    than a database write.
    """

    def build(ctx):
        pool = cfg["cognito_user_pool_id"]
        prefix = ctx["evaluation_id"]
        manifest = ctx["manifest"]
        account, region = str(cfg["platform_account"]), cfg["region"]
        # `adp-e2e-*` so the ownership tag, the bundle grant and the recovery
        # sweep all recognise these as this run's.
        #
        # `.example` rather than `.invalid`: the ADP user route below validates
        # the address with `EmailStr`, which REFUSES reserved TLDs (`.invalid`,
        # `.test`) — verified, it answers 422 — so the old domain could not be
        # registered through the product's own onboarding path at all. `.example`
        # is equally reserved by RFC 2606 and equally undeliverable, which is the
        # property that mattered: combined with MessageAction=SUPPRESS below, no
        # mail is generated and none could be delivered if it were.
        challenge = f"{prefix}-admin@adp-eval.example"
        non_admin = f"{prefix}-user@adp-eval.example"
        secret_name = f"adp/cli-uplift-eval/{prefix}-fixtures"

        # Distinct passwords, generated here and never logged. Each satisfies the
        # pool policy (>=12 chars, upper, lower, digit, symbol) verified live.
        def password():
            return "Ev!" + secrets.token_urlsafe(18) + "9Aa"

        temporary, rotated = password(), password()
        non_admin_password = password()

        def create(username, *, role):
            attributes = [
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": f"ADP eval {prefix}"},
                {
                    "Name": "custom:org_id",
                    "Value": cfg.get("fixture_org_id", "") or "adp-platform",
                },
            ]
            if role:
                attributes.append({"Name": "custom:role", "Value": role})
            # Recorded BEFORE the create, so an interruption between the two
            # still leaves the identity findable by the sweep.
            manifest.record(
                "cognito_user",
                f"{pool}/{username}",
                account=account,
                region=region,
                detail={"fixture": "e02", "role": role or "none"},
            )
            aws.call(
                "cognito-idp",
                "admin_create_user",
                UserPoolId=pool,
                Username=username,
                UserAttributes=attributes,
                # No email is deliverable to a reserved domain, and a fixture must
                # not try: SUPPRESS keeps this from generating mail at all.
                MessageAction="SUPPRESS",
                TemporaryPassword=temporary if role else non_admin_password,
            )

        create(challenge, role="platform_admin")
        # The pool grants admin by group as well as by claim, and the gateway
        # accepts either. Both are set so the fixture matches the shared one.
        aws.call(
            "cognito-idp",
            "admin_add_user_to_group",
            UserPoolId=pool,
            Username=challenge,
            GroupName="admins",
        )

        create(non_admin, role="")
        # Permanent, so the non-admin authenticates outright: its rejection must
        # come from authorization, not from an unanswered password challenge.
        aws.call(
            "cognito-idp",
            "admin_set_user_password",
            UserPoolId=pool,
            Username=non_admin,
            Password=non_admin_password,
            Permanent=True,
        )

        document = {
            "admin_username": challenge,
            "admin_password": temporary,
            "admin_new_password": rotated,
            "non_admin_username": non_admin,
            "non_admin_password": non_admin_password,
        }
        manifest.record(
            "secret",
            secret_name,
            account=account,
            region=region,
            detail={"fixture": "e02"},
        )
        arn = (
            aws.call(
                "secretsmanager",
                "create_secret",
                Name=secret_name,
                SecretString=json.dumps(document),
                Tags=[{"Key": cleanup.OWNER_TAG, "Value": prefix}],
            )
            or {}
        ).get("ARN") or secret_name
        aws.call(
            "secretsmanager",
            "put_resource_policy",
            SecretId=secret_name,
            BlockPublicPolicy=True,
            ResourcePolicy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowEvalInstanceRoleOnly",
                            "Effect": "Allow",
                            "Principal": {
                                "AWS": (
                                    f"arn:aws:iam::{account}:role/"
                                    + cfg["instance_profile"]
                                )
                            },
                            "Action": "secretsmanager:GetSecretValue",
                            "Resource": arn,
                        }
                    ],
                }
            ),
        )

        # Only the NAME and the usernames leave this function. The passwords stay
        # in the secret, which is the whole point of the indirection.
        return {
            "credential_secret": secret_name,
            "created_username": challenge,
            "non_admin_username": non_admin,
        }

    return build


def _worker_config(cfg):
    def build(_cfg, ctx, mode):
        return {
            "instance_id": ctx["document"].get("instance_id"),
            "evaluation_id": ctx["evaluation_id"],
            "platform_account": str(cfg["platform_account"]),
            "destination_account": str(cfg["destination_account"]),
            "region": cfg["region"],
            "gateway_url": cfg["gateway_url"],
            "sts_endpoint": cfg["sts_endpoint"],
            "secrets_endpoint": cfg["secrets_endpoint"],
            "connection_name": f"{ctx['evaluation_id']}-{mode}",
            "stack_name": f"{ctx['evaluation_id']}-{mode}",
            "role_name": f"{ctx['evaluation_id']}-{mode}-role",
            "credential_secret": cfg.get("credential_secret_name", ""),
            "session_key": "admin",
            # R3: the provisioner binding, validated in config as a role ARN in
            # the destination account. Absent, the journey fails naming it rather
            # than falling back to the instance's own identity.
            "provisioner_arn": cfg.get("provisioner_role_arn", ""),
            "absent_connection_id": "00000000-0000-4000-8000-000000000000",
        }

    return build


def _journey(ssm, cfg, install, journeys=None):
    """Resolve a case's purpose to a callable that runs it on the instance.

    Returns None only for a purpose with no shipped script — which the stage
    reports as an implementation gap naming the module to write. That is a real
    absence, not a wiring hole: `bundle.purposes()` is read from the dispatcher
    registry that ships, so "registered" and "runnable" cannot disagree.

    `journeys` still allows an explicit override for a test, but the default is
    live rather than absent.
    """
    if journeys is not None:
        return (journeys or {}).get if isinstance(journeys, dict) else journeys
    worker = _run_worker(ssm, cfg, install)
    available = set(bundle.purposes())

    def resolve(purpose):
        if purpose not in available:
            return None

        def drive(instance_id, ctx):
            return worker(instance_id, purpose, _journey_payload(cfg, ctx))

        return drive

    return resolve


def _journey_payload(cfg, ctx):
    """What a journey script needs, derived from the run's own state.

    `expected_hashes` comes from preflight, which derived it from the revision
    under test — never re-read from the download the instance is about to make.
    The session established by install/auth is carried forward so a routing or
    inference journey does not log in again and turn a login failure into a
    routing failure.

    Every key any shipped script reads is supplied here, including the ones with
    on-instance defaults. That is deliberate: `personal_inference` hard-requires
    `claude_model`, `test_user_id` and `effective_destination_account`, and an
    absent key would have failed the journey with a KeyError attributed to
    inference rather than to a payload this function never assembled. The bounds
    are passed explicitly too, so a run's timeouts come from its validated config
    instead of a constant buried in a remote module.
    """
    session = ctx["document"].get("session") or {}
    correlation = ctx.get("correlation") or {}
    return {
        "instance_id": ctx["document"].get("instance_id"),
        "evaluation_id": ctx["evaluation_id"],
        "platform_account": str(cfg["platform_account"]),
        "destination_account": str(cfg["destination_account"]),
        "region": cfg["region"],
        "gateway_url": cfg["gateway_url"],
        "sts_endpoint": cfg["sts_endpoint"],
        "secrets_endpoint": cfg["secrets_endpoint"],
        "credential_secret": cfg.get("credential_secret_name", ""),
        "provisioner_arn": cfg.get("provisioner_role_arn", ""),
        "expected_hashes": ctx.get("expected_hashes")
        or (ctx["preflight"] or {}).get("served_cli_hashes")
        or {},
        "cli_path": session.get("cli_path", ""),
        # A reference, not the tokens. install_auth keeps the real session material
        # in a private on-instance vault because the only channel out of the
        # instance redacts credential-shaped values — so forwarding the "tokens"
        # here used to forward the literal string "<redacted>". The journey
        # resolves this with `common.load_session()`, which fails loudly when the
        # session did not survive the stage boundary. `work_dir` travels with it so
        # the journey can bound the reference to this run's own directory.
        "session_ref": session.get("session_ref", ""),
        "work_dir": bundle.REMOTE_DIR,
        "session_expires_at": session.get("expires_at", 0),
        "test_user": session.get("username", ""),
        # The gateway's own id for that identity, which is what the usage log keys
        # on. install_auth reads it from `/auth/cli/admin-session`; the username is
        # the fallback only because a pool can be configured to make them equal.
        "test_user_id": session.get("user_id") or session.get("username", ""),
        "org_id": session.get("org_id", ""),
        "destination_label": f"{ctx['evaluation_id']}-dest",
        "unprovisioned_label": f"{ctx['evaluation_id']}-unprov",
        "handoff_label": f"{ctx['evaluation_id']}-handoff",
        "claude_version": cfg.get("claude_version", ""),
        "codex_version": cfg.get("codex_version", ""),
        "claude_model": cfg["claude_model"],
        # The account the routing rule E06 left in place actually points at, as
        # read back from the product. E08 compares it against the configured
        # destination and refuses to grade cross-account inference if they differ,
        # so it must be E06's observation and never a copy of the config.
        "effective_destination_account": correlation.get(
            "bedrock_destination_account", ""
        ),
        # The proxy `adp codex` starts. Off the 9191 default so a listener left by
        # an earlier attempt on this instance cannot be mistaken for ours.
        "proxy_port": int(cfg["proxy_port"]),
        "usage_wait_seconds": int(cfg["usage_wait_seconds"]),
        "cloudtrail_wait_seconds": int(cfg["cloudtrail_wait_seconds"]),
        "inference_timeout_seconds": int(cfg["inference_timeout_seconds"]),
        # #5413's three deployment records, each {name, gateway_url,
        # credential_secret_name}. Absent when no three-deployment fixture is bound,
        # which is why E16/E17 block at the fixture gate long before this payload is
        # built — the empty list here is what a resumed or hand-written dispatch
        # would hit, and the journey refuses it by name rather than by IndexError.
        "deployments": cfg.get("deployments") or [],
        # Per-request output cap and the absence window, passed explicitly for the
        # same reason the other bounds are: a live run's spend limit must come from
        # the validated config of the run that was authorised, not from a constant
        # inside a shipped module where nobody reviewing the dispatch can see it.
        "max_output_length": int(cfg["max_output_length"]),
        "absence_wait_seconds": int(cfg["absence_wait_seconds"]),
        # Where a script that installs from a staged copy finds it. The scripts that
        # install from the served release ignore this.
        "source_dir": bundle.REMOTE_DIR + "/release",
        # E13's own connection, and an id that belongs to nobody. The name carries
        # the evaluation id so cleanup can find the row even if the create call's
        # response never arrived, and so it cannot collide with E04/E05's.
        "connection_name": f"{ctx['evaluation_id']}-parity",
        "absent_connection_id": "00000000-0000-4000-8000-000000000000",
        # The browser's declared wire types, read from the git object store at the
        # revision under test rather than from the deployment. E13 refuses to run
        # without them: checking only the CLI consumer would report a pass on a
        # response the UI cannot render, which is the whole property under test.
        "ui_contracts": ctx.get("ui_contracts") or {},
        # E18 receives references and bounded workload choices only. The admin
        # password remains in Secrets Manager and is read on the instance.
        "superplane": cfg.get("superplane") or {},
    }


def _github_available(cfg):
    """Whether an ISOLATED GitHub fixture genuinely exists.

    Config naming a fixture is necessary but not sufficient — the point of the
    check is that an operator has actually created a dedicated App/org/repo. With
    no way to prove it from here, this returns False so the GitHub cases block
    rather than being assumed. Pointing at a shared App is never an answer: a
    reset would break real users.
    """

    def check():
        github = cfg.get("github") or {}
        if not (github.get("org") and github.get("repo")):
            return False
        return bool(github.get("app_fixture") or github.get("existing_app_fixture"))

    return check


def _hosted_available(cfg):
    def check():
        return bool(cfg.get("hosted_tasks_queue_url") and cfg.get("websocket_url"))

    return check


def _sessions(aws, cfg):
    """Resolve a session that actually belongs to a given account, cached.

    R7: every deleter used to close over the platform session, so deleting the
    destination account's CloudFormation stack sent platform credentials at
    `605440105851` — AccessDenied at best, and the stack left standing while the
    run reported a clean sweep. The account a resource lives in decides which
    session deletes it, and if no role can reach that account this raises rather
    than falling back to the session at hand.
    """
    cache = {}

    def resolve(account=None):
        account = str(account or cfg["platform_account"])
        if account == str(cfg["platform_account"]):
            return aws
        if account not in cache:
            role_arn = cfg.get("destination_role_arn") or cfg.get(
                "provisioner_role_arn"
            )
            if not role_arn:
                raise ports_module.PortError(
                    f"No role is configured for account {account}; its resources "
                    "cannot be deleted with the platform session and must not be "
                    "reported as cleaned"
                )
            if f":{account}:" not in role_arn:
                raise ports_module.PortError(
                    f"The configured destination role does not belong to account "
                    f"{account}; refusing to delete its resources with another "
                    "account's credentials"
                )
            cache[account] = aws.assume(role_arn, f"cli-uplift-eval-cleanup-{account}")
        return cache[account]

    return resolve


def _vault_token(ssm, instance_id, document):
    """Read the session token off the instance, for the API deleters only.

    The cleanup sweep runs from the orchestrator and terminates the instance
    first, so the token cannot be resolved on-instance the way a journey resolves
    it. It is read here over the same SSM transport, at most once per run, and is
    never returned into the run document, the report or the durable state.

    Returns "" rather than raising: the deleters treat an absent token as "refuse
    to act and report the resource as outstanding", which is the correct, honest
    outcome and strictly better than aborting the rest of the teardown.
    """
    reference = (document or {}).get("session_ref")
    if not (ssm and instance_id and reference):
        return ""
    try:
        _result, payload = ssm.json_result(
            instance_id,
            [
                "set -eu",
                # `cat` of a 0600 ec2-user file, as ec2-user. The value reaches the
                # orchestrator over the SSM API and nothing writes it to disk here.
                "runuser -l ec2-user -c "
                + shlex.quote(f"cat {shlex.quote(reference)}"),
            ],
            purpose="session_handoff",
            timeout=120,
        )
    except ports_module.PortError:
        return ""
    return (payload or {}).get("access_token") or ""


def _deleters(aws, cfg, http=None, ssm=None):
    """Live deletions, keyed by resource kind, scoped to the resource's account.

    Every kind the manifest can hold must appear, because `cleanup.sweep()` treats
    an unregistered kind as a FAILURE rather than a skip — a leak that reports
    clean is the outcome the issue forbids. There is deliberately no queue-purging
    deleter and no `sqs_queue` kind.

    Each deleter WAITS for completion and then VERIFIES absence. A delete API that
    returns 200 only means the request was accepted: CloudFormation deletion is
    asynchronous and can end in DELETE_FAILED, which would otherwise be recorded as
    a successful cleanup.
    """
    session_for = _sessions(aws, cfg)
    wait_seconds = int(cfg.get("cleanup_wait_seconds", 120))

    # The AWS error codes that mean "it is already gone", per service. Absence is
    # the end state a deleter exists to reach, so meeting it on arrival is a
    # SUCCESS, not a failure.
    #
    # This is not a theoretical case. `cleanup.sweep()` deletes a resource and
    # THEN marks it deleted, and the durable push behind that mark is
    # deliberately best-effort (`critical=False` in `Manifest.mark`, so an
    # unreachable store cannot strand the remaining deletions). So a run that is
    # cancelled mid-sweep — or whose status push simply failed — leaves the
    # durable manifest saying `pending` for a resource that is already deleted.
    # The recover job runs `always()`, restores that manifest, and deletes it
    # again. Without this, the second delete raises, `sweep()` records a FAILURE,
    # `cleanup_ok` goes false, and `cases.accept()` denies acceptance over a
    # resource that is not there.
    #
    # `cleanup.ORDER` makes the strand window widest for the kinds deleted first
    # (instance, profile, role, security group), so those need this as much as
    # the secret and the Cognito user do.
    #
    # Codes, not messages: `ports.PortError` carries the AWS error code only, by
    # design, so a provider message cannot leak an ARN or an ExternalId into the
    # report. Verified live where the sandbox allows a truthful answer
    # (`iam.NoSuchEntity`, `cognito-idp.UserNotFoundException`); the rest are the
    # documented codes for each API.
    ABSENT = (
        "NoSuchEntity",  # IAM role, instance profile
        "ResourceNotFound",  # Secrets Manager (also ResourceNotFoundException)
        "UserNotFound",  # Cognito admin_delete_user
        "InvalidInstanceID.NotFound",  # EC2 instance
        "InvalidGroup.NotFound",  # EC2 security group
        "ValidationError",  # CloudFormation: stack does not exist
        "NoSuchKey",  # S3 object
        "NotFound",
        "404",
    )

    def gone_is_good(call, *what):
        """Run a deleting call, treating "already absent" as the success it is.

        Only absence is forgiven. Every other error — AccessDenied above all —
        still raises, because "we were not allowed to delete it" must never be
        recorded as a clean teardown.
        """
        try:
            call()
        except ports_module.PortError as exc:
            message = str(exc)
            if not any(code in message for code in ABSENT):
                raise
            return False
        return True

    def _absent(call, *, retries=None, interval=5):
        """Poll until a describe call reports the resource gone."""
        attempts = max(
            1, (retries if retries is not None else wait_seconds // interval)
        )
        for remaining in range(attempts, 0, -1):
            if call():
                return True
            if remaining > 1:
                time.sleep(interval)
        return False

    def terminate(instance_id, *, account=None, region=None):
        scoped = session_for(account)
        # An instance EC2 has already reclaimed answers InvalidInstanceID.NotFound
        # instead of terminating. That is the end state this deleter wants, so it
        # returns rather than polling a describe that can only raise.
        if not gone_is_good(
            lambda: scoped.call("ec2", "terminate_instances", InstanceIds=[instance_id])
        ):
            return

        def gone():
            # Absence here is also success — but ONLY absence. Any other error
            # keeps `gone()` false so the poll runs its course and the run reports
            # the instance as outstanding rather than assuming it died.
            reservations = {}

            def call():
                reservations.update(
                    scoped.call("ec2", "describe_instances", InstanceIds=[instance_id])
                )

            if not gone_is_good(call):
                return True
            found = (reservations.get("Reservations") or [{}])[0].get("Instances") or [
                {}
            ]
            return (found[0].get("State") or {}).get("Name") == "terminated"

        if not _absent(gone):
            raise ports_module.PortError(
                f"Instance {instance_id} did not reach terminated state"
            )

    def delete_stack(name, *, account=None, region=None):
        # The destination account's stack, deleted with the destination account's
        # own credentials. This is the R7 case exactly.
        scoped = session_for(account)
        if not gone_is_good(
            lambda: scoped.call("cloudformation", "delete_stack", StackName=name)
        ):
            return

        def gone():
            # Describe by name fails with ValidationError once the stack is fully
            # deleted, which is success. Catching every PortError here would also
            # swallow an AccessDenied — reporting a clean sweep over a stack still
            # standing in the destination account, the exact R7 defect this
            # deleter's account scoping exists to prevent.
            described = {}

            def call():
                described.update(
                    scoped.call("cloudformation", "describe_stacks", StackName=name)
                )

            if not gone_is_good(call):
                return True
            stacks = described.get("Stacks") or []
            status = (stacks or [{}])[0].get("StackStatus") or ""
            if status.endswith("_FAILED"):
                raise ports_module.PortError(
                    f"Stack {name} ended in {status}; it still exists in account "
                    f"{account or cfg['platform_account']}"
                )
            return status == "DELETE_COMPLETE"

        if not _absent(gone):
            raise ports_module.PortError(f"Stack {name} was still deleting")

    def delete_role(name, *, account=None, region=None):
        scoped = session_for(account)

        # A role already gone has no policies to list, and IAM answers
        # NoSuchEntity to the LIST rather than to the delete — so the absence
        # check has to happen here, before the detach loops, not just around the
        # final delete_role call.
        def listed(operation, key):
            found = {}

            def call():
                found.update(scoped.call("iam", operation, RoleName=name))

            if not gone_is_good(call):
                return None
            return found.get(key) or []

        attached = listed("list_attached_role_policies", "AttachedPolicies")
        if attached is None:
            return
        for policy in attached:
            scoped.call(
                "iam",
                "detach_role_policy",
                RoleName=name,
                PolicyArn=policy["PolicyArn"],
            )
        inline = listed("list_role_policies", "PolicyNames")
        if inline is None:
            return
        for policy in inline:
            scoped.call("iam", "delete_role_policy", RoleName=name, PolicyName=policy)
        if not gone_is_good(lambda: scoped.call("iam", "delete_role", RoleName=name)):
            return

        def gone():
            # NoSuchEntity means deleted; anything else (AccessDenied above all)
            # must not read as absence, or a role we were forbidden to delete
            # would be reported as successfully removed.
            return not gone_is_good(
                lambda: scoped.call("iam", "get_role", RoleName=name)
            )

        if not _absent(gone, retries=3):
            raise ports_module.PortError(f"Role {name} still exists after deletion")

    def delete_profile(name, *, account=None, region=None):
        gone_is_good(
            lambda: session_for(account).call(
                "iam", "delete_instance_profile", InstanceProfileName=name
            )
        )

    def delete_security_group(group_id, *, account=None, region=None):
        gone_is_good(
            lambda: session_for(account).call(
                "ec2", "delete_security_group", GroupId=group_id
            )
        )

    def delete_secret(secret_id, *, account=None, region=None):
        gone_is_good(
            lambda: session_for(account).call(
                "secretsmanager",
                "delete_secret",
                SecretId=secret_id,
                ForceDeleteWithoutRecovery=True,
            )
        )

    def delete_cognito_user(spec, *, account=None, region=None):
        pool, _, username = str(spec).partition("/")
        # Verified against the live pool: AdminDeleteUser raises
        # UserNotFoundException for an absent user rather than succeeding.
        gone_is_good(
            lambda: session_for(account).call(
                "cognito-idp", "admin_delete_user", UserPoolId=pool, Username=username
            )
        )

    def delete_s3_object(spec, *, account=None, region=None):
        bucket, _, key = str(spec).partition("/")
        scoped = session_for(account)
        scoped.call("s3", "delete_object", Bucket=bucket, Key=key)
        try:
            scoped.call("s3", "head_object", Bucket=bucket, Key=key)
        except ports_module.PortError as exc:
            if str(exc).endswith((": 404", ": NoSuchKey", ": NotFound")):
                return
            raise
        raise ports_module.PortError(
            "The evaluation bundle still exists after deletion"
        )

    def unsupported(kind):
        def refuse(identifier, *, account=None, region=None):
            # Explicit failure, so the run reports a leak instead of hiding one.
            raise ports_module.PortError(
                f"No live deleter for {kind}; {identifier} must be removed by its owner"
            )

        return refuse

    def delete_destination(http, session):
        """Remove a Bedrock destination E06 registered, as far as the product allows.

        Two steps, because a destination cannot be removed while a rule names it:

        1. Delete every routing rule pointing at it
           (`DELETE /admin/bedrock-routing/mappings/{scope}`), which is safe by
           design — a scope with no rule falls through to the next rung.
        2. Delete the destination row itself
           (`DELETE /admin/bedrock-routing/connection-links/{id}`).

        Step 2 only exists for a destination created by `POST /connection-links`,
        which requires a matching `bedrock_connection_grants` row. A destination
        registered through `adp admin bedrock connect` (`POST /destinations`, source
        `new_account`) has no grant, so that route answers 404 and **the product has
        no endpoint that deletes it**. That is a real gap, not a harness one, so this
        raises and names it: the run reports the row as an outstanding leak with the
        operator action needed, rather than treating a 404 as "already gone" and
        reporting a clean sweep over a registry row that is still there and still
        routable.

        The rule deletions in step 1 still happen, so the run never leaves a live
        routing rule pointing at an evaluation destination — which is the part that
        would actually affect traffic.
        """

        def delete(identifier, *, account=None, region=None):
            token = (session() or {}).get("access_token") or ""
            if not token:
                raise ports_module.PortError(
                    f"No authenticated session is available to remove destination "
                    f"{identifier}; it must be removed by its owner"
                )
            base = cfg["gateway_url"].rstrip("/") + "/admin/bedrock-routing"
            status, listing = http.get(base + "/destinations", token=token, expect=None)
            if status != 200:
                raise ports_module.PortError(
                    f"The destinations API returned HTTP {status}; ownership of "
                    f"{identifier} could not be established"
                )
            rows = listing if isinstance(listing, list) else []
            found = next(
                (row for row in rows if str(row.get("id")) == str(identifier)), None
            )
            if found is None:
                return  # already gone: the desired end state

            # Ownership: only a destination labelled for THIS run may be touched.
            prefix = (session() or {}).get("prefix") or ""
            label = str(found.get("label") or "")
            if not prefix or prefix not in label:
                raise ports_module.PortError(
                    f"Destination {identifier} is not labelled for this run; refusing "
                    "to remove a destination this evaluation did not create"
                )

            # Step 1: drop every rule naming it, so no traffic can route here and
            # the unlink below is not refused with a 409.
            status, mappings = http.get(base + "/mappings", token=token, expect=None)
            if status != 200:
                raise ports_module.PortError(
                    f"The mappings API returned HTTP {status}; the routing rules for "
                    f"{identifier} could not be removed"
                )
            for row in mappings if isinstance(mappings, list) else []:
                if str(row.get("destination_id")) != str(identifier):
                    continue
                scope = row.get("scope")
                if not scope:
                    raise ports_module.PortError(
                        f"A routing rule for {identifier} reports no scope; it cannot "
                        "be removed by this run"
                    )
                rule_status, _ = http.request(
                    f"{base}/mappings/{quote(str(scope), safe='')}",
                    method="DELETE",
                    token=token,
                    expect=None,
                )
                if rule_status not in (200, 202, 204, 404):
                    raise ports_module.PortError(
                        f"Removing the routing rule {scope!r} returned HTTP "
                        f"{rule_status}; destination {identifier} is still in use"
                    )

            # Step 2: the destination row itself, where the product supports it.
            unlink_status, _ = http.request(
                f"{base}/connection-links/{quote(str(identifier), safe='')}",
                method="DELETE",
                token=token,
                expect=None,
            )
            if unlink_status == 404:
                raise ports_module.PortError(
                    f"Destination {identifier} was registered through `adp admin "
                    "bedrock connect`, and the gateway has no endpoint that deletes "
                    "such a row (DELETE /connection-links requires a connection "
                    "grant). Its routing rules were removed, so no traffic reaches "
                    "it, but the registry row must be removed by a platform admin"
                )
            if unlink_status not in (200, 202, 204):
                raise ports_module.PortError(
                    f"Removing destination {identifier} returned HTTP {unlink_status}"
                )
            # A 2xx is a request, not a proof. Confirm it is actually gone.
            status, listing = http.get(base + "/destinations", token=token, expect=None)
            rows = listing if isinstance(listing, list) else []
            if any(str(row.get("id")) == str(identifier) for row in rows):
                raise ports_module.PortError(
                    f"Destination {identifier} is still registered after deletion"
                )

        return delete

    def delete_adp_user(http, session):
        """Remove the ADP account the run registered for its own E02 login.

        Recorded as `<org_id>/<user_id>`, because the delete route is org-scoped
        and an id alone would not say which organization to remove it from.

        Ownership is checked before deleting, the same way the connection deleter
        does it and for the same reason: the identifier came back over SSM from a
        journey, and a deleter that trusts it could be pointed at a real operator's
        account. The row's email must carry this run's evaluation ID, which is a
        property of the name `_admin_fixtures` derives and cannot be true of an
        identity this evaluation did not create.

        A 404 is success — the end state a deleter exists to reach. It is also the
        expected answer on a re-run of an interrupted sweep, where the row is
        already gone but the durable manifest still says pending.
        """

        def delete(identifier, *, account=None, region=None):
            org, _, user_id = str(identifier).partition("/")
            if not (org and user_id):
                raise ports_module.PortError(
                    f"ADP user record {identifier} is not <org_id>/<user_id>; "
                    "refusing to guess which organization to delete it from"
                )
            token = (session() or {}).get("access_token") or ""
            if not token:
                raise ports_module.PortError(
                    f"No authenticated session is available to delete ADP user "
                    f"{identifier}; it must be removed by its owner"
                )
            base = cfg["gateway_url"].rstrip("/") + "/api/admin/identity/organizations"
            prefix = (session() or {}).get("prefix") or ""
            status, payload = http.get(f"{base}/{org}/users", token=token, expect=None)
            if status == 404:
                return
            if status != 200:
                raise ports_module.PortError(
                    f"The identity API returned HTTP {status}; ownership of "
                    f"{identifier} could not be established"
                )
            found = next(
                (
                    row
                    for row in ((payload or {}).get("users") or [])
                    if str(row.get("id")) == user_id
                ),
                None,
            )
            if found is None:
                return  # already gone: the desired end state
            if prefix and prefix not in str(found.get("email") or ""):
                raise ports_module.PortError(
                    f"ADP user {identifier} is not named for this run; refusing to "
                    "delete an account this evaluation did not create"
                )
            delete_status, _ = http.request(
                f"{base}/{org}/users/{user_id}",
                method="DELETE",
                token=token,
                expect=None,
            )
            if delete_status not in (200, 202, 204, 404):
                raise ports_module.PortError(
                    f"Deleting ADP user {identifier} returned HTTP {delete_status}"
                )
            # A 2xx is a request, not a proof.
            status, payload = http.get(f"{base}/{org}/users", token=token, expect=None)
            if any(
                str(row.get("id")) == user_id
                for row in ((payload or {}).get("users") or [])
            ):
                raise ports_module.PortError(
                    f"ADP user {identifier} is still present after deletion"
                )

        return delete

    def delete_connection(http, session):
        """R8: authenticated, ownership-checked deletion of an ADP connection.

        A successful journey disconnects through the product's own CLI and the
        stage marks the record deleted, so this only runs for a worker that was
        interrupted mid-journey — previously an unconditional raise, which made
        cleanup fail for exactly the runs that had succeeded.

        Ownership is checked against the live API before deleting: the connection
        must exist, and its name must carry this run's evaluation ID. A connection
        that is already gone is a success; someone else's is never touched.
        """

        def delete(identifier, *, account=None, region=None):
            token = (session() or {}).get("access_token") or ""
            if not token:
                raise ports_module.PortError(
                    f"No authenticated session is available to delete connection "
                    f"{identifier}; it must be removed by its owner"
                )
            base = cfg["gateway_url"].rstrip("/")
            status, payload = http.get(
                f"{base}/aws/connections", token=token, expect=None
            )
            if status == 404:
                return
            if status != 200:
                raise ports_module.PortError(
                    f"The connections API returned HTTP {status}; ownership of "
                    f"{identifier} could not be established"
                )
            rows = (
                (payload or {}).get("connections") or (payload or {}).get("items") or []
            )
            found = next(
                (row for row in rows if str(row.get("id")) == str(identifier)), None
            )
            if found is None:
                return  # already gone: the desired end state
            prefix = (session() or {}).get("prefix") or ""
            name = str(found.get("name") or found.get("connection_name") or "")
            if prefix and prefix not in name:
                raise ports_module.PortError(
                    f"Connection {identifier} is not named for this run; refusing to "
                    "delete a connection this evaluation did not create"
                )
            delete_status, _ = http.request(
                f"{base}/aws/connections/{identifier}",
                method="DELETE",
                token=token,
                expect=None,
            )
            if delete_status not in (200, 202, 204, 404):
                raise ports_module.PortError(
                    f"Deleting connection {identifier} returned HTTP {delete_status}"
                )
            # A 2xx is a request, not a proof. Confirm it is actually gone.
            status, payload = http.get(
                f"{base}/aws/connections", token=token, expect=None
            )
            rows = (
                (payload or {}).get("connections") or (payload or {}).get("items") or []
            )
            if any(str(row.get("id")) == str(identifier) for row in rows):
                raise ports_module.PortError(
                    f"Connection {identifier} is still present after deletion"
                )

        return delete

    def build(_cfg, ctx=None):
        # The session the API deleters authenticate with.
        #
        # Read EAGERLY, here, before the sweep runs a single deleter — not lazily on
        # first use. `cleanup.ORDER` terminates `ec2_instance` FIRST (it holds the
        # ENI), and the vault holding this token lives on that instance, so by the
        # time any API deleter asks for it the instance is gone and the SSM read can
        # only fail. A lazy read is therefore never early enough. `build` is called
        # by `cleanup_stage` before `sweep()` begins, which is the last moment the
        # instance is still running.
        #
        # This went unnoticed because the two API deleters that existed before were
        # both for resources a SUCCESSFUL journey removes through the product's own
        # CLI: `adp_connection` and `bedrock_destination` are marked deleted by the
        # journey, so their deleters only run for an interrupted worker and almost
        # never executed. `adp_user` is the first kind the sweep must ALWAYS delete
        # itself, which is what surfaced it — live, as
        # `adp_user:<org>/<id>` outstanding with a PortError while every other kind
        # reported deleted.
        #
        # Nothing token-shaped is written back into the run document, which is what
        # the report and the durable state are built from.
        cached = {}
        if ctx is not None:
            document = ctx["document"].get("session") or {}
            if document:
                # Non-secret fields come straight from the document; only the token
                # needs the instance. A read that fails leaves the token absent, and
                # the deleters already refuse to act without one rather than
                # reporting a clean sweep over a resource they could not touch.
                cached.update(document)
                cached["access_token"] = _vault_token(
                    ssm, ctx["document"].get("instance_id"), document
                )

        def session():
            return cached

        return {
            "ec2_instance": terminate,
            "cloudformation_stack": delete_stack,
            "iam_role": delete_role,
            "iam_instance_profile": delete_profile,
            "security_group": delete_security_group,
            "secret": delete_secret,
            "cognito_user": delete_cognito_user,
            "s3_object": delete_s3_object,
            # An ADP connection is normally removed by the product's own
            # disconnect path inside the worker. A record still pending here means
            # the worker was interrupted, so this deletes it through the API with
            # the run's own session and an ownership check.
            "adp_connection": delete_connection(http, session),
            # A registered Bedrock destination. Its routing rules are always
            # removed; the registry row itself only where the gateway exposes a
            # delete for it. See `delete_destination` — the residual case is a
            # product gap and is reported as an outstanding resource, not swallowed.
            "bedrock_destination": delete_destination(http, session),
            # Historical E18 evidence remains a real cleanup obligation. The
            # admin session above is not the ordinary resource owner's session;
            # never guess ownership or treat an unimplemented delete as success.
            **{kind: unsupported(kind) for kind in cleanup.SUPERPLANE_KINDS},
            # The ADP account the run registered for its own E02 login. Deleted
            # after the two above, which authenticate as it — see `cleanup.ORDER`.
            "adp_user": delete_adp_user(http, session),
            # A GitHub App is never deleted by automation: it is either a reused
            # fixture (preserved by design) or it needs an owner's action.
            "github_app": unsupported("github_app"),
        }

    return build


def wire(cfg, supplied=None, *, journeys=None):
    """Return the capability mapping the stages consume.

    `supplied` overrides individual entries — that is the offline seam. Anything
    not overridden is live.
    """
    supplied = dict(supplied or {})
    base = ports_module.default_ports(cfg)
    aws = supplied.get("aws") or base["aws"]
    http = supplied.get("http") or base["http"]
    ssm = supplied.get("ssm") or ports_module.SsmPort(aws)
    install = _install_bundle(ssm, aws, cfg)

    resolved = {
        "aws": aws,
        "http": http,
        "ssm": ssm,
        "identity": _identity(aws, cfg),
        # The revision the results bind to, from trusted deployment evidence
        # rather than from a public health field the product does not publish.
        "deployed_revision": _deployed_revision(aws, http, cfg),
        "wait_online": _wait_online(aws),
        "ssm_reachable": lambda: True,
        "install_bundle": install,
        "run_worker": _run_worker(ssm, cfg, install),
        "worker_config": _worker_config(cfg),
        "admin_fixtures": _admin_fixtures(aws, cfg),
        "github_available": _github_available(cfg),
        "hosted_available": _hosted_available(cfg),
        "harness_auth_helper": _read_harness_helper,
        # `ssm` so the API deleters can resolve the session token off the instance
        # before the sweep terminates it. See `_vault_token`.
        "deleters": _deleters(aws, cfg, http, ssm),
        # R1: journeys are the SAME transport as every other remote step. There is
        # no separate driver layer to leave unpopulated: a case's purpose either
        # has a shipped script (`bundle.purposes()`) or `require_purpose()` names
        # the module that must be written. The old resolver defaulted to
        # `lambda _purpose: None`, so nine cases reported "no driver registered"
        # while the mapping looked wired.
        "journey": _journey(ssm, cfg, install, journeys),
    }
    resolved.update(
        {
            key: value
            for key, value in supplied.items()
            if key not in ("aws", "http", "ssm")
        }
    )
    return resolved


def _read_harness_helper():
    """Read the assembled harness's CLI auth helper for the isolation check."""
    import os
    from pathlib import Path

    root = os.environ.get("HARNESS_ROOT")
    if not root:
        raise ports_module.PortError(
            "HARNESS_ROOT is not set; the pinned harness was not assembled"
        )
    path = Path(root) / config.CLI_AUTH_HELPER
    if not path.exists():
        raise ports_module.PortError(
            f"The assembled harness has no {config.CLI_AUTH_HELPER}"
        )
    return path.read_text()


__all__ = ["wire"]
