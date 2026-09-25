"""The production stage implementations, and the factory the entry point uses.

Before this module the runner was invoked with no `stages` mapping at all, so the
stage loop skipped every stage and a run reported fifteen `not_run` rows while
looking structurally healthy. Merging and supplying fixtures could not have made
that workflow provision an instance or authenticate anything. `build_stages()` is
what the entry point now calls, and a REQUIRED stage that is missing from the
assembled mapping is a hard failure rather than a silent skip.

Division of responsibility:

- `ports.py` moves bytes. It never decides anything.
- this module decides. Every case's pass condition is asserted here, against
  evidence that came back from a real command, so a double that returns partial
  or wrong evidence fails the case exactly as a broken deployment would.
- `cases.py` grades. It cannot be talked out of a `blocked` or `not_run`.

Where a case cannot yet be driven end to end, it is recorded FAILED with
`unimplemented: true` — never `blocked` (which means "a fixture is absent",
an operator-actionable state) and never skipped. An honest failing row keeps
full acceptance red, which is the correct state for work that does not exist.
"""

from __future__ import annotations

import shlex

from . import (
    bundle,
    cases,
    cleanup,
    config as config_module,
    contracts,
    ports as ports_module,
    preflight,
    release,
)

# Stages that must exist in any assembled mapping. `evidence` and `cleanup` are
# included because a run that skips them cannot prove correlation or teardown.
REQUIRED_STAGES = (
    "preflight",
    "ec2",
    "install_auth",
    "providers",
    "journeys",
    "evidence",
    "cleanup",
)

# Amazon Linux 2023, resolved through SSM public parameters rather than hardcoded.
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

# The run-owned 0700 directory on the instance. Aliased to the bundle's remote
# directory rather than repeated as a second literal: the session vault lives here
# and `live._journey_payload` must name the SAME directory the install stage
# created, or a journey would bound its session reference against a path that does
# not exist.
WORK_DIR = bundle.REMOTE_DIR


class StageError(RuntimeError):
    """A stage could not complete. Message carries no provider text."""


def require(condition, message):
    if not condition:
        raise StageError(message)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _deployment_discovery(http):
    """#5413: read a deployment's discovery document through the run's transport.

    Wrapped rather than passed raw for two reasons. `http.get` returns
    `(status, document)` while the check wants the document; and both a non-200 and
    an unreachable host must surface as the named problem for THAT deployment. A
    `PortError` escaping here would abort the whole run over an absent
    multi-deployment fixture, which is precisely the "a missing fixture blocks, a
    wrong target aborts" line this harness draws everywhere else.
    """

    def read(url):
        try:
            status, document = http.get(url, expect=None)
        except ports_module.PortError as exc:
            raise preflight.PreflightError(str(exc)) from None
        if status != 200:
            raise preflight.PreflightError(f"returned HTTP {status}")
        return document

    return read


def _superplane_probe(http):
    """#5637: the domain mount check, read through the run's own transport.

    Returns the STATUS, because an error status is the evidence here — 401 proves
    the route forwards, 404 proves nothing is behind it. `expect=None` is what makes
    `http.get` return a status instead of raising on a non-200.
    """

    def read(url):
        try:
            status, _document = http.get(url, expect=None)
        except ports_module.PortError as exc:
            raise preflight.PreflightError(str(exc)) from None
        return status

    return read


def preflight_stage(cfg, ports):
    """Prove account, region, gateway, revision, served hashes and fixtures.

    Read-only, and first, because it is the only stage that can reject the target
    before a mutation exists to roll back.
    """

    def run(ctx):
        record = ctx["preflight"]
        http, aws = ports["http"], ports["aws"]

        # Only cross-account scenarios require a destination session. Installing
        # and logging in must work before any Bedrock setup exists.
        #
        # Gated on the destination fixture actually being bound, not on matrix
        # membership alone: when it is absent, these cases are blocked further
        # down for lacking exactly this access, so proving it first aborted the
        # run — a `full` dispatch died here rather than grading the cases that
        # needed no destination at all. The block itself stays below, after the
        # target checks, so a rejected deployment reports NOT_RUN and never
        # attributes a fixture verdict to a target it refused.
        destination_required = cases.DESTINATION in config_module.fixture_classes(
            cfg
        ) and any(
            cases.DESTINATION in cases.BY_ID[case_id].requires
            for case_id in ctx["matrix"]
        )
        identities = {}
        for name, key in (
            ("platform", "platform_account"),
            ("destination", "destination_account"),
        ):
            if name == "destination" and not destination_required:
                continue
            identities[name] = ports["identity"](name)
            require(
                identities[name].get("Account"),
                f"No STS identity resolved for the {name} account ({cfg[key]})",
            )
        preflight.check_accounts(
            identities, cfg, record, destination_required=destination_required
        )

        # Gateway reachability, then the deployed revision the results bind to.
        # R2: the canonical prefix-free discovery document the CLI itself fetches,
        # with its real contract validated — not `/cli/discovery`, which is not a
        # served path and could only ever have returned 404 or the SPA fallback.
        status, discovery = http.get(
            cfg["gateway_url"].rstrip("/") + preflight.DISCOVERY_PATH, expect=200
        )
        record["unauthenticated_discovery_status"] = status
        # Passed through as-is, including a non-dict: the SPA fallback answers 200
        # with HTML, and the contract check must reject that. Coercing it to None
        # here would instead send the helper off to fetch the URL itself with
        # urllib, outside this run's transport and its redirect refusal.
        preflight.check_unauthenticated_discovery(cfg, record, discovery=discovery)

        # The revision the results bind to. NOT from a public health field: the
        # product's `/health` returns `{"status": "healthy"}` and always has, so
        # requiring a revision there aborted every live run before it launched
        # anything. The capability prefers `/health` if a deployment ever reports
        # one and otherwise reads the deployment's own pinned image tag, which is
        # an IAM-authorized read the gateway cannot influence. A new public
        # health-metadata API is deliberately not a prerequisite.
        deployed = ports["deployed_revision"](record)
        record["deployed_revision"] = deployed
        record["expected_revision"] = cfg["expected_revision"]
        require(
            deployed,
            "No deployed revision could be established from the gateway or from the "
            "deployment evidence; results cannot be bound to a deployment",
        )
        require(
            deployed.startswith(cfg["expected_revision"][: len(deployed)])
            or cfg["expected_revision"].startswith(deployed),
            f"Deployed revision {deployed!r} is not the expected {cfg['expected_revision']!r}",
        )

        # R10: the expected release is derived from the revision under test, in
        # the git object store, BEFORE anything is downloaded. Previously this
        # hashed three of the release's ten files and then handed those same
        # observed values to the instance as `expected_hashes` — comparing the
        # download against itself, so a stale `adp-aws.py` (the file E04 and E05
        # depend on) could not be detected. The helper list comes from the
        # installer's own CLI_FILES at that revision, so the set cannot silently
        # shrink back to a subset.
        expected_hashes = release.manifest(cfg["expected_revision"])
        expected_cli_version = release.cli_version(cfg["expected_revision"])
        require(
            len(expected_hashes) >= 2,
            "The release manifest at the revision under test lists too few files to be a CLI release",
        )
        record["expected_release"] = {
            "revision": cfg["expected_revision"],
            "cli_version": expected_cli_version,
            "files": dict(sorted(expected_hashes.items())),
        }
        # One comparison, in the reviewed helper, against hashes the gateway
        # cannot influence. Aborts on any missing or mismatched file.
        preflight.check_served_cli_hashes(
            cfg, expected_hashes, record, fetch=http.get_bytes
        )
        # Carried into the instance payload so "the gateway serves the release"
        # and "the instance installed the release" are the same assertion.
        ctx["expected_hashes"] = expected_hashes
        ctx["expected_cli_version"] = expected_cli_version

        # E13's other consumer. Read from the same git object store, at the same
        # revision, for the same reason the hashes are: a contract the deployment
        # could influence would agree with whatever it served. Derived here, while
        # preflight is still read-only, so a renamed interface fails before an
        # instance exists rather than as an unexplained parity failure later.
        ui_contracts = (
            contracts.wire_contracts(cfg["expected_revision"])
            if "E13" in ctx["matrix"]
            else {}
        )
        record["ui_contracts"] = {
            name: sorted(fields) for name, fields in sorted(ui_contracts.items())
        }
        ctx["ui_contracts"] = ui_contracts

        # The BG_CONFIG_DIR isolation contract, from the assembled harness.
        preflight.check_harness_isolation(ports["harness_auth_helper"](), record)

        # Subnet and Cognito ownership.
        subnet = aws.call(
            "ec2", "describe_subnets", SubnetIds=[cfg["private_subnet_id"]]
        )["Subnets"][0]
        preflight.check_subnet(subnet, cfg, record)
        pool = aws.call(
            "cognito-idp",
            "describe_user_pool",
            UserPoolId=cfg["cognito_user_pool_id"],
        )["UserPool"]
        clients = aws.call(
            "cognito-idp",
            "list_user_pool_clients",
            UserPoolId=cfg["cognito_user_pool_id"],
            MaxResults=60,
        )["UserPoolClients"]
        preflight.check_cognito(
            pool, {"ClientId": (clients or [{}])[0].get("ClientId")}, cfg, record
        )

        # The instance profile the launch will reference. Checked here so a wrong
        # or absent name fails while preflight is still read-only, rather than as
        # a KeyError/AWS error at run_instances with a manifest already written.
        profile = aws.call(
            "iam",
            "get_instance_profile",
            InstanceProfileName=cfg["instance_profile"],
        )["InstanceProfile"]
        require(
            profile.get("InstanceProfileName") == cfg["instance_profile"],
            f"Instance profile {cfg['instance_profile']!r} did not resolve; "
            "the disposable instance could not be launched with it",
        )
        record["instance_profile"] = profile.get("InstanceProfileName")

        # SSM reachability, so an EC2 stage failure later is not a mystery.
        require(
            ports["ssm_reachable"](),
            "SSM is not reachable from this runner; the EC2 stages could not be driven",
        )

        # Fixture availability decides which cases block. Unproven means absent.
        available = preflight.evaluate_fixtures(
            cfg,
            github_available=ports["github_available"](),
            hosted_available=ports["hosted_available"](),
            # #5413. Probed only when a selected case needs it, because it is three
            # more gateway reads and every other suite runs one deployment. The
            # check itself is read-only and never aborts: an unreachable binding
            # blocks E16/E17 and leaves the rest of the matrix to run.
            capability_contrast_available=ports["capability_contrast_available"]()
            if any(
                cases.CAPABILITY_CONTRAST in cases.BY_ID[case_id].requires
                for case_id in ctx["matrix"]
            )
            else None,
            deployments_available=preflight.check_deployment_bindings(
                cfg, record, fetch=_deployment_discovery(http)
            )
            if any(
                cases.THREE_DEPLOYMENTS in cases.BY_ID[case_id].requires
                for case_id in ctx["matrix"]
            )
            else None,
            # #5637. Validate the recovery prerequisite before E18 can allocate
            # resources. A reachable gateway does not implement that producer.
            superplane_available=preflight.check_superplane_domain(
                cfg, record, probe=_superplane_probe(http)
            )
            if any(
                cases.SUPERPLANE_DOMAIN in cases.BY_ID[case_id].requires
                for case_id in ctx["matrix"]
            )
            else None,
        )
        record["fixture_classes"] = sorted(available)
        record["missing_fixtures"] = preflight.missing_fixture_report(cfg, available)
        blocked = cases.block_missing_fixtures(ctx["matrix"], available)
        record["blocked_cases"] = {k: v for k, v in sorted(blocked.items())}
        if "E18" in blocked and not any(
            selected(ctx, case_id) for case_id in ctx["matrix"]
        ):
            # The runner executes stages in order even when every case blocks.
            # Stop this otherwise idle attempt before ec2/install_auth mutate.
            raise StageError(cleanup.SUPERPLANE_RECOVERY_BLOCKER)

        if ctx["fault"] == "wrong_account":
            # Injection: prove a wrong-account run cannot go green.
            raise StageError(
                "Injected fault wrong_account: refusing to evaluate an unverified account"
            )

    return run


# ---------------------------------------------------------------------------
# ec2
# ---------------------------------------------------------------------------


def user_data(cfg, evaluation_id):
    """Cloud-init that makes the instance terminate itself.

    This is the durable half of the cleanup guarantee. A cancelled workflow, a
    lost runner or Actions being unavailable entirely all skip every `always()`
    block and every dependent job, so nothing that lives in the workflow can
    promise termination. `shutdown -H` plus instance-initiated-shutdown-behavior
    of `terminate` means the instance removes itself on its own clock even if
    nothing ever polls it again.
    """
    ttl = int(cfg["instance_ttl_minutes"])
    return "\n".join(
        [
            "#!/bin/bash",
            "set -eu",
            # Self-destruct timer, armed before anything else can fail.
            f"shutdown -H +{ttl} 'cli-uplift-eval TTL reached' &",
            f"echo {shlex.quote(evaluation_id)} > /etc/cli-uplift-eval-id",
            "dnf install -y jq >/dev/null 2>&1",
            "dnf install -y nodejs22 nodejs22-npm >/dev/null 2>&1 || true",
            "ln -sf /usr/bin/node-22 /usr/local/bin/node || true",
            f"install -d -o ec2-user -g ec2-user -m 700 {WORK_DIR}",
        ]
    )


def ec2_stage(cfg, ports):
    """Launch exactly one disposable instance in the approved private subnet."""

    def run(ctx):
        aws, manifest = ports["aws"], ctx["manifest"]
        require(
            int(cfg["max_instances"]) >= 1,
            "max_instances must allow at least one disposable instance",
        )
        ami = aws.call("ssm", "get_parameter", Name=AMI_PARAMETER)["Parameter"]["Value"]

        # Record intent BEFORE the mutating call. A launch that succeeds while the
        # response is lost is otherwise an instance nothing can find.
        manifest.record(
            "ec2_instance",
            f"pending:{ctx['attempt_id']}",
            account=str(cfg["platform_account"]),
            region=cfg["region"],
            detail={"subnet": cfg["private_subnet_id"]},
        )
        reservation = aws.call(
            "ec2",
            "run_instances",
            ImageId=ami,
            InstanceType=cfg.get("instance_type", "t3.small"),
            MinCount=1,
            MaxCount=1,
            SubnetId=cfg["private_subnet_id"],
            IamInstanceProfile={"Name": cfg["instance_profile"]},
            UserData=user_data(cfg, ctx["evaluation_id"]),
            # Belt and braces with the in-guest timer: a stop from any cause
            # becomes a terminate rather than a stopped instance billing storage.
            InstanceInitiatedShutdownBehavior="terminate",
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": cleanup.OWNER_TAG, "Value": ctx["evaluation_id"]},
                        {
                            "Key": "Name",
                            "Value": f"cli-uplift-eval-{ctx['attempt_id']}",
                        },
                    ],
                }
            ],
        )
        instance_id = reservation["Instances"][0]["InstanceId"]
        manifest.record(
            "ec2_instance",
            instance_id,
            account=str(cfg["platform_account"]),
            region=cfg["region"],
            detail={"ttl_armed": True},
        )
        manifest.mark("ec2_instance", f"pending:{ctx['attempt_id']}", cleanup.DELETED)
        ctx["document"]["instance_id"] = instance_id
        ctx["correlation"]["instance_id"] = instance_id

        require(
            ports["wait_online"](instance_id),
            f"Instance {instance_id} did not register with SSM; cannot drive the CLI on it",
        )

        # R1: deliver the on-instance scripts, verified by digest, and self-check
        # them — HERE, so a delivery failure fails the EC2 stage rather than
        # surfacing as an inexplicable journey error. Previously every remote step
        # invoked `worker.py`, which nothing ever created.
        delivered = ports["install_bundle"](
            instance_id, ctx["evaluation_id"], ctx["manifest"]
        )
        ctx["document"]["remote_bundle"] = delivered
        ctx["correlation"]["remote_bundle_digest"] = (delivered or {}).get("digest")
        require(
            (delivered or {}).get("purposes"),
            "The remote script bundle installed but reported no runnable purposes",
        )

        if ctx["fault"] == "instance_loss":
            raise StageError(
                "Injected fault instance_loss: instance became unavailable mid-run"
            )

    return run


def selected(ctx, case_id):
    """Whether this run may record a result for `case_id`.

    R9: `install_auth` recorded E01-E03 and `evidence` recorded E15 unconditionally,
    so `--suite install` (E01 only) raised "Case E02 is not in the selected matrix"
    and the suite could not run at all. The matrix is the selection, so consulting
    it is the fix — and a case that is present but already decided (passed on a
    previous attempt, or blocked by a missing fixture) is likewise not re-recorded.

    Note what this deliberately does NOT do: skip the WORK. A stage still installs
    the CLI and logs in even when only E01 is selected, because E01 cannot be
    proven without them. Selection filters what is recorded, not what runs.
    """
    entry = ctx["matrix"].get(case_id)
    return entry is not None and entry.get("status") == cases.NOT_RUN


def record_selected(ctx, case_id, status, detail=None):
    """Record a result only for a case this run actually selected."""
    if selected(ctx, case_id):
        ctx["record"](case_id, status, detail)
        return True
    return False


def install_auth_stage(cfg, ports):
    """Install the served CLI on the instance and prove a real admin login.

    E01 (install) and E02/E03 (native Cognito login and setup) are decided here.
    A seeded or imported token cannot satisfy E02 — the whole point is the
    challenge flow — so the worker performs the actual login and the assertions
    below require challenge evidence, not merely a usable token.

    Install-only runs do not authenticate. The login checkpoint performs native
    authentication and refresh; E02 challenge checks and E03 setup run only when
    selected. Other journeys establish a basic session as their prerequisite.
    """

    def run(ctx):
        instance = ctx["document"].get("instance_id")
        require(instance, "install_auth ran before an instance existed")
        # R10: the hashes come from the release manifest preflight derived at the
        # revision under test, not from anything observed in a download.
        expected_hashes = ctx.get("expected_hashes") or (
            ctx["preflight"].get("expected_release") or {}
        ).get("files")
        require(
            expected_hashes,
            "install_auth ran before preflight derived the expected release hashes",
        )
        # E02 needs a challenge identity and a non-admin identity that the shared
        # login-regression fixture cannot provide: that fixture is CONFIRMED (so
        # it issues no NEW_PASSWORD_REQUIRED) and it is an admin (so it cannot be
        # the negative). Rotating it would break the suite that depends on it, so
        # when E02 is selected the run provisions its own and points the worker at
        # them. Every other suite keeps using the configured shared fixture.
        #
        # Selected, not merely present in the matrix: a BLOCKED or already-passed
        # E02 must not create identities nothing will use.
        challenges = selected(ctx, "E02")
        fixtures = ports["admin_fixtures"](ctx) if challenges else {}
        if fixtures.get("created_username"):
            # Published before the worker runs so a failed login still reports
            # which identity it was attempted against, and so cleanup has it.
            ctx["document"]["admin_fixtures"] = {
                key: value
                for key, value in fixtures.items()
                if key != "credential_secret"
            }
        evidence = ports["run_worker"](
            instance,
            "install_auth",
            {
                **ports["worker_config"](cfg, ctx, "install"),
                **{
                    key: value
                    for key, value in fixtures.items()
                    if key in ("credential_secret", "created_username")
                },
                "expected_hashes": expected_hashes,
                "cognito_user_pool_id": cfg["cognito_user_pool_id"],
                "work_dir": WORK_DIR,
                "login_required": any(key != "E01" for key in ctx["matrix"]),
                "admin_challenges_required": challenges,
                "admin_setup_required": "E03" in ctx["matrix"],
            },
        )
        require(isinstance(evidence, dict), "install/auth worker returned no evidence")
        ctx["transcript"].extend(evidence.get("transcript") or [])

        # The session every later journey reuses. Carried in the run document so a
        # routing or inference journey does not log in again — a second login would
        # turn a login failure into a routing failure and hide which one broke.
        session = evidence.get("session") or {}
        if session:
            ctx["document"]["session"] = {
                **session,
                "prefix": ctx["evaluation_id"],
            }
        # A Cognito identity the run created is this run's to remove. When the
        # fixtures above created it, this is a no-op: `Manifest.record` is
        # idempotent per (kind, id) and the identity was already recorded BEFORE
        # the create, which is the ordering that survives an interruption. This
        # remains for the identity a worker creates on its own, where the
        # orchestrator only learns the name from the returned evidence and so
        # cannot record it any earlier.
        if session.get("created_username"):
            ctx["manifest"].record(
                "cognito_user",
                f"{cfg['cognito_user_pool_id']}/{session['created_username']}",
                account=str(cfg["platform_account"]),
                region=cfg["region"],
            )
        # Records the journey itself reports — the ADP account it registered for
        # the run's own login. Unlike the Cognito identity above, the orchestrator
        # cannot know this id in advance: the product assigns it. Recorded here,
        # immediately on return, which is the earliest moment it is knowable.
        for kind, identifier in evidence.get("resources") or []:
            ctx["manifest"].record(kind, identifier, detail={"case": "E02"})

        # E01: discovery/download unauthenticated, executable install, hashes match.
        install = evidence.get("install") or {}
        if (
            install.get("success")
            and install.get("hashes_match")
            and install.get("executable")
        ):
            record_selected(
                ctx,
                "E01",
                cases.PASSED,
                {
                    "unauthenticated_status": install.get("download_status"),
                    "installed_hashes_match_release": True,
                    "helpers": sorted(install.get("helpers") or []),
                    "expected_helper_count": install.get("expected_helper_count"),
                },
            )
        else:
            record_selected(ctx, "E01", cases.FAILED, {"install": install})

        # E02: real login, including the challenge flow and the negatives.
        login = evidence.get("login") or {}
        record_selected(
            ctx,
            "C01",
            cases.PASSED
            if (
                evidence.get("success")
                and login.get("authenticated")
                and login.get("bad_credentials_rejected")
                and login.get("refresh_succeeded")
                and login.get("user_id")
            )
            else cases.FAILED,
            {
                "authenticated": login.get("authenticated"),
                "refresh_succeeded": login.get("refresh_succeeded"),
                "user_id": login.get("user_id"),
                "error": evidence.get("error"),
            },
        )
        proved = (
            login.get("challenge_completed")
            and login.get("authenticated")
            and login.get("bad_credentials_rejected")
            and login.get("non_admin_denied")
            and login.get("refresh_succeeded")
        )
        record_selected(
            ctx,
            "E02",
            cases.PASSED if proved else cases.FAILED,
            {
                "challenges": login.get("challenges"),
                "bad_credentials_rejected": login.get("bad_credentials_rejected"),
                "non_admin_denied": login.get("non_admin_denied"),
                "refresh_succeeded": login.get("refresh_succeeded"),
                # Named explicitly so a reader can see this was not a seeded token.
                "seeded_token_used": False,
            },
        )

        # E03: setup reports accurately and a rerun completes without duplicates.
        setup = evidence.get("setup") or {}
        record_selected(
            ctx,
            "E03",
            cases.PASSED
            if (
                setup.get("statuses_accurate")
                and setup.get("rerun_completed_missing_only")
                and not setup.get("duplicates")
            )
            else cases.FAILED,
            {
                "reported_states": setup.get("reported_states"),
                "rerun_completed_missing_only": setup.get(
                    "rerun_completed_missing_only"
                ),
                "duplicates": setup.get("duplicates"),
            },
        )

    return run


def personal_aws_stage(cfg, ports):
    """E04/E05: the real `adp aws connect` journeys on the instance.

    `test-cli-routing.py --provision direct|handoff` drives
    `adp admin bedrock connect`, a different command against a different endpoint
    set, so this is a genuinely new adapter rather than a renamed state directory.
    """

    def run(ctx):
        instance = ctx["document"].get("instance_id")
        require(instance, "providers ran before an instance existed")
        for case_id, mode in (("E04", "provision"), ("E05", "handoff")):
            if not selected(ctx, case_id):
                continue  # blocked by a missing fixture, or already passed
            payload = {"mode": mode, **ports["worker_config"](cfg, ctx, mode)}

            # R6/R8: intent BEFORE the mutating call. The worker creates a stack
            # and a connection whose names are derived here, so they are knowable
            # in advance — and a worker that dies after creating them but before
            # reporting must still leave a record cleanup can act on. Recording
            # only what came BACK is how an interrupted journey leaked silently.
            ctx["manifest"].record(
                "cloudformation_stack",
                payload["stack_name"],
                account=str(cfg["destination_account"]),
                region=cfg["region"],
                detail={"case": case_id, "intent": True},
            )
            evidence = ports["run_worker"](instance, f"personal_aws_{mode}", payload)
            evidence = evidence or {}
            ctx["transcript"].extend(evidence.get("transcript") or [])

            # The stack the worker actually created, if it reported a different ID
            # than the name we recorded (a stack ID is the durable handle).
            if evidence.get("stack_id"):
                ctx["manifest"].record(
                    "cloudformation_stack",
                    evidence["stack_id"],
                    account=str(cfg["destination_account"]),
                    region=cfg["region"],
                    detail={"case": case_id},
                )
            connection = evidence.get("connection_id")
            if connection:
                ctx["manifest"].record(
                    "adp_connection", connection, detail={"case": case_id}
                )
                # R8: the journey's own final act is `adp aws disconnect`, and it
                # asserts the connection is absent from the live API afterwards.
                # Leaving the record pending made a SUCCESSFUL E04/E05 fail
                # cleanup — the record was for a resource the run had already
                # proved gone. A proved disconnect is a completed deletion.
                if "disconnect_removes_adp_record_preserves_aws_role" in (
                    evidence.get("checks") or []
                ):
                    ctx["manifest"].mark("adp_connection", connection, cleanup.DELETED)
            # A handoff journey that only downloaded a template never created the
            # stack; drop the intent record so cleanup does not chase a stack that
            # was never made. Marked deleted rather than removed, so the record of
            # the intent survives for the audit.
            if evidence.get("stack_created") is False:
                ctx["manifest"].mark(
                    "cloudformation_stack", payload["stack_name"], cleanup.DELETED
                )

            detail = {
                "checks": evidence.get("checks") or [],
                "provisioner_caller_arn": evidence.get("provisioner_caller_arn"),
                "stage_reached": evidence.get("stage"),
            }
            if evidence.get("error"):
                detail["error"] = evidence["error"]
            record_selected(
                ctx,
                case_id,
                cases.PASSED if evidence.get("success") else cases.FAILED,
                detail,
            )
            if evidence.get("provisioner_caller_arn"):
                ctx["correlation"][f"{case_id}_provisioner_arn"] = evidence[
                    "provisioner_caller_arn"
                ]

    return run


# The dispatcher purpose each case is driven by. A purpose with no shipped script
# is an implementation gap: the case fails naming the module that must be written.
JOURNEY_DRIVERS = {
    "E06": "bedrock_routing",
    "E07": "bedrock_rungs",
    "E08": "personal_inference",
    "E09": "hosted_inference",
    "E10": "github_app",
    "E11": "github_login",
    "E12": "agent_task",
    "E13": "api_parity",
    "E14": "update_rollback",
    # #5413. Two purposes, one module: E16 is the concurrent-overlap proof and E17
    # is what a default switch, a refresh and one logout do to the other two. Split
    # because they fail for different reasons and a single row would report "the
    # multi-deployment case failed" without saying which half.
    "E16": "multi_deployment_concurrency",
    "E17": "multi_deployment_lifecycle",
    # #5637. One purpose: the workspace/deploy/cost/events half and the credential
    # half are one journey because the credential is registered INTO a workspace
    # this run created, and splitting them would mean either creating two
    # workspaces or making one case depend on the other's leftovers.
    "E18": "superplane_domain",
    "E19": "capability_contrast",
    "E20": "story_capabilities",
    "E21": "story_usage",
    "E22": "story_activity",
    "E24": "story_vault",
    "E30": "story_gitlab",
    "E26": "story_budget",
}

# Which account a journey's resources live in, by kind. A journey reports
# `["cloudformation_stack", id]`; the stack is in the DESTINATION account, and
# recording it without saying so would have it deleted with platform credentials
# (R7). Kinds absent here are platform-account resources.
JOURNEY_RESOURCE_ACCOUNT = {
    "cloudformation_stack": "destination_account",
    "iam_role": "destination_account",
}


def journeys_stage(cfg, ports):
    """Bedrock routing, real inference, GitHub onboarding and parity.

    Each case is driven by one dispatcher purpose on the instance, through the same
    transport as every other remote step. A purpose with no shipped script is
    recorded FAILED with `unimplemented: true` and the module path to write, which
    keeps full acceptance red and names precisely what is missing — the honest
    state for absent work.
    """

    def run(ctx):
        instance = ctx["document"].get("instance_id")
        require(instance, "journeys ran before an instance existed")
        for case_id, purpose in JOURNEY_DRIVERS.items():
            if not selected(ctx, case_id):
                continue
            driver = ports["journey"](purpose)
            if driver is None:
                record_selected(
                    ctx,
                    case_id,
                    cases.FAILED,
                    {
                        "unimplemented": True,
                        "purpose": purpose,
                        "detail": "no on-instance script implements this case. It "
                        f"must be written as tests/e2e/cli_uplift/remote/{purpose}.py "
                        "and registered in remote/dispatcher.py PURPOSES; until then "
                        "this case cannot pass.",
                    },
                )
                continue
            evidence = driver(instance, ctx) or {}
            ctx["transcript"].extend(evidence.get("transcript") or [])
            # Resources the journey itself removed AND asserted absent. A pair, so
            # a bare id cannot mark the wrong kind's record deleted.
            proved_removed = {
                (str(kind), str(identifier))
                for kind, identifier in evidence.get("removed") or []
            }
            for kind, identifier in evidence.get("resources") or []:
                account_key = JOURNEY_RESOURCE_ACCOUNT.get(kind)
                ctx["manifest"].record(
                    kind,
                    identifier,
                    account=str(cfg[account_key]) if account_key else None,
                    region=cfg["region"] if account_key else None,
                    detail={"case": case_id},
                )
                # R8 again, for journeys: a case whose own final act removes a
                # resource — and which asserted its absence from the live API — has
                # already completed that deletion. Leaving the record pending made
                # a SUCCESSFUL case fail cleanup, because cleanup would be chasing
                # something the run had proved gone. The journey must SAY it proved
                # removal; the mere absence of an error is not that claim.
                if (str(kind), str(identifier)) in proved_removed:
                    ctx["manifest"].mark(kind, identifier, cleanup.DELETED)
            ctx["correlation"].update(evidence.get("correlation") or {})
            if ctx["fault"] == "missing_usage" and case_id in ("E08", "E09"):
                # Injection: usage evidence absent must fail the case, never pass.
                evidence = {**evidence, "success": False, "usage": None}
            detail = evidence.get("detail") or {
                "success": bool(evidence.get("success")),
                "checks": evidence.get("checks") or [],
                "stage_reached": evidence.get("stage"),
            }
            if evidence.get("error"):
                detail = {**detail, "error": evidence["error"]}
            record_selected(
                ctx,
                case_id,
                cases.PASSED if evidence.get("success") else cases.FAILED,
                detail,
            )

    return run


def evidence_stage(cfg, ports):
    """E15 plus the correlation the report publishes.

    E15 is a property of the harness across runs: two fresh full runs on one
    revision, an interrupt/resume, and a cleanup that leaves nothing behind. One
    run can only contribute its own half.

    R4: this used to require `counts[NOT_RUN] == 0` and an EMPTY manifest, both
    evaluated while E15 itself was still `not_run` and while cleanup had not yet
    executed. So E15 counted its own un-run status against itself and demanded that
    a run which had just created an instance, a stack and a connection be holding
    no resources — two conditions that cannot both hold on a fresh full run, which
    made E15 unpassable by construction.

    The fix separates the two questions:

    - THIS RUN's contribution, decided here: every OTHER selected case passed, and
      nothing is outstanding beyond what cleanup is about to remove. Cleanup's own
      success is a separate gate (`cases.accept(cleanup_ok=...)`), so E15 does not
      need to pre-judge it — and by not requiring an empty manifest before cleanup
      runs, it stops asserting something false.
    - The CROSS-RUN requirement, which one run cannot satisfy: reported as
      `second_fresh_run_required` and enforced outside a single run. Nothing here
      weakens it; a passing E15 on one run is explicitly not full acceptance.
    """

    def run(ctx):
        document = ctx["document"]
        attempts = document.get("attempts") or []

        # E15's own row is excluded: counting it while it is still `not_run` made
        # the case fail on itself. Every other SELECTED case must have a verdict.
        others = {
            case_id: entry
            for case_id, entry in ctx["matrix"].items()
            if case_id != "E15"
        }
        failed = sorted(
            case_id
            for case_id, entry in others.items()
            if entry.get("status") == cases.FAILED
        )
        undecided = sorted(
            case_id
            for case_id, entry in others.items()
            if entry.get("status") == cases.NOT_RUN
        )

        # Resources still pending are the ones cleanup is ABOUT to remove, which is
        # the normal state here — every full run has an instance and a stack at this
        # point. What would be wrong is a resource already marked FAILED, i.e. one a
        # previous cleanup attempt could not delete.
        outstanding = ctx["manifest"].outstanding()
        unrecoverable = [
            entry for entry in outstanding if entry.get("status") == cleanup.FAILED
        ]
        contributed = not failed and not undecided and not unrecoverable

        ctx["correlation"].update(
            {
                "evaluation_id": ctx["evaluation_id"],
                "attempt_id": ctx["attempt_id"],
                "attempts": len(attempts),
                "deployed_revision": (ctx["preflight"] or {}).get("deployed_revision"),
            }
        )
        record_selected(
            ctx,
            "E15",
            cases.PASSED if contributed else cases.FAILED,
            {
                "attempts": len(attempts),
                "resumed": len(attempts) > 1,
                "failed_cases": failed,
                "undecided_cases": undecided,
                # Pending-at-this-point is expected; FAILED is not.
                "pending_before_cleanup": [
                    f"{e['kind']}:{e['id']}"
                    for e in outstanding
                    if e.get("status") != cleanup.FAILED
                ],
                "unrecoverable_resources": [
                    f"{e['kind']}:{e['id']}" for e in unrecoverable
                ],
                "single_revision": (ctx["preflight"] or {}).get("deployed_revision")
                == document.get("expected_revision")
                or None,
                # Stated rather than implied: closing #5199 needs a SECOND fresh
                # full run on the same revision plus an interruption/resume proof.
                # One run passing this row is not that, and must not read as it.
                "second_fresh_run_required": True,
                "this_run_contributes_only": True,
            },
        )

    return run


def cleanup_stage(cfg, ports):
    """Delete exactly what this run created, by recorded ID."""

    def run(ctx):
        if ctx["fault"] == "cleanup_failure":
            return False
        # `ctx` is passed so the connection deleter can use this run's own
        # authenticated session and check ownership before deleting (R8).
        ok, results = cleanup.sweep(
            ctx["manifest"], ports["deleters"](cfg, ctx), prefix=ctx["evaluation_id"]
        )
        ctx["document"]["cleanup_results"] = [
            {"kind": r["kind"], "status": r["status"]} for r in results
        ]
        return ok

    return run


def build_stages(cfg, ports=None, *, journeys=None):
    """Assemble the production stage mapping the entry point runs.

    Missing ports are a programming error surfaced immediately, not at the moment
    a live stage first reaches for one.
    """
    from . import live

    resolved = live.wire(cfg, ports, journeys=journeys)
    mapping = {
        "preflight": preflight_stage(cfg, resolved),
        "ec2": ec2_stage(cfg, resolved),
        "install_auth": install_auth_stage(cfg, resolved),
        "providers": personal_aws_stage(cfg, resolved),
        "journeys": journeys_stage(cfg, resolved),
        "evidence": evidence_stage(cfg, resolved),
        "cleanup": cleanup_stage(cfg, resolved),
    }
    missing = [name for name in REQUIRED_STAGES if name not in mapping]
    if missing:
        raise StageError("Required stages are not implemented: " + ", ".join(missing))
    return mapping


__all__ = ["REQUIRED_STAGES", "StageError", "build_stages"]
