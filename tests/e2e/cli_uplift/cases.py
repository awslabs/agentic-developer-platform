"""The acceptance cases of #5199 (plus #5413's two), their owners, and grading.

Every function here is pure: no AWS, no gateway, no filesystem. The offline
suite exercises the whole grading contract with sockets disabled, because the
part that must never be wrong is the part that decides whether a run is allowed
to report success.

Two rules drive the design and are asserted directly in the offline tests:

1. A case that is BLOCKED or NOT_RUN is not a pass. `blocked` exists so a
   missing fixture is reported honestly instead of being silently skipped, and
   it still keeps full acceptance from succeeding.
2. A partial (named-suite) run can never satisfy full acceptance, however green
   it looks. `accept()` requires every case in the matrix to be present AND
   passed AND for the run to have declared itself a full run.

#5413 adds E16/E17 (the `multi-deployment` suite) rather than a second harness,
and adds them INSIDE the matrix rather than beside it as C01 is. That placement
is the point: three real deployment bindings do not exist yet, so these two grade
BLOCKED, and because BLOCKED is not PASSED a `full` run stays red until the live
multi-deployment evidence is actually collected. A checkpoint outside the matrix
would instead have let full acceptance go green with those two never run.

#5637 adds E18 (the `superplane` suite) on the same terms and for the same
reason. Its live acceptance needs a deployed domain service behind the gateway,
which no environment in this harness's config provides yet, so E18 grades BLOCKED
and keeps `full` red until that evidence exists. The offline contract tests in
`modules/gateway/tests/cli/test_superplane_contract.py` prove the requests match
the gateway allowlist and the domain's request models; they cannot prove a live
service accepts them, and E18 is where that distinction is recorded rather than
assumed away.
"""

from __future__ import annotations

PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
NOT_RUN = "not_run"

STATUSES = (PASSED, FAILED, BLOCKED, NOT_RUN)

# Suites are the dispatchable groupings of cases. `--suite full` is the only one
# that can satisfy full acceptance; the others exist so an operator can make
# progress while a fixture class is still blocked.
SUITES = (
    "full",
    "login",
    "install",
    "admin",
    "personal-aws",
    "routing",
    "inference",
    "github",
    "parity",
    "harness",
    # #5413. One installed CLI serving three deployments at once, which no other
    # suite can express: every suite above runs a single deployment, so a crossed
    # endpoint, token or proxy is invisible to all of them.
    "multi-deployment",
    # #5637. The served CLI's Superplane commands driven THROUGH the gateway to the
    # real domain service. `parity` covers the routes ADP itself serves; this suite
    # exists because the domain sits behind the gateway's forwarding allowlist, and
    # a request that satisfies the allowlist can still be rejected by the domain's
    # own schemas — which is exactly the class of defect #5637 repaired.
    "superplane",
)


class Case:
    """One acceptance row: a stable ID, the story that owns it, and its suite."""

    def __init__(self, case_id, owner, suite, summary, requires=()):
        self.id = case_id
        self.owner = owner
        self.suite = suite
        self.summary = summary
        # Fixture classes this case cannot run without. Preflight maps a missing
        # class to BLOCKED for exactly the cases that name it, so one absent
        # fixture does not mark the whole matrix blocked.
        self.requires = tuple(requires)

    def __repr__(self):
        return f"Case({self.id})"


# Fixture classes. Preflight proves each of these independently; a case is
# blocked when any class it requires is unavailable.
PLATFORM = "platform"
DESTINATION = "destination"
SECOND_DESTINATION = "second_destination"
EC2 = "ec2"
COGNITO = "cognito"
GITHUB_APP = "github_app"
GITHUB_REPO = "github_repo"
HOSTED = "hosted"
# #5413: three separately-reachable ADP deployments and a sign-in fixture for
# each. Deliberately its own class rather than a count on DESTINATION — those are
# AWS accounts a rule routes TO, whereas these are three gateways the CLI signs in
# to independently, and one cannot stand in for the other. Absent, E16/E17 BLOCK:
# approximating three deployments with one URL registered under three names would
# pass while every crossed-endpoint defect the story exists to prevent survived.
THREE_DEPLOYMENTS = "three_deployments"
# A capability requirement, deliberately never granted by current preflight.
# Gateway availability does not prove enforcement of inference spend limits.
MULTI_DEPLOYMENT_MODEL_LIMITS = "multi_deployment_model_limits"
# #5621 (CLI-08-AC-04): a deployment where one module is deliberately DISABLED
# and one is enabled, plus an ordinary non-admin identity alongside the admin one.
#
# Its own class rather than a flag on PLATFORM, because the criterion is a
# CONTRAST: proving the CLI distinguishes "switched off" from "not permitted" from
# "available" needs a deployment configured to exhibit all three at once, and a
# second identity that genuinely lacks a permission the first holds. A single
# admin on a fully-enabled platform can demonstrate none of them — every answer
# would be "available", so the four axes could all be collapsed into one boolean
# and the case would still pass. Absent, E18 BLOCKS rather than testing the one
# state that proves nothing.
CAPABILITY_CONTRAST = "capability_contrast"
# #5637: a deployed Superplane domain service reachable through the gateway's
# forwarding allowlist, plus an ordinary and an admin identity in it.
#
# Its own class, not an extension of PLATFORM: the gateway is deployed in dev and
# still forwards to a domain service that may not be. Folding this into PLATFORM
# would mark E18 runnable whenever the gateway answers, and the case would then
# fail mid-journey on a 502 from the proxy — reporting "the product is broken" for
# what is actually an absent fixture. And it cannot be inferred from the allowlist
# either: the allowlist is checked-in source, so it is present on every revision
# whether or not anything is listening behind it.
SUPERPLANE_DOMAIN = "superplane_domain"

CASES = (
    Case(
        "E01",
        "#5185",
        "install",
        "Unauthenticated discovery/download return 200; fresh EC2 install is immediately executable with matching hashes",
        (EC2, PLATFORM),
    ),
    Case(
        "E02",
        "#5185",
        "admin",
        "Real adp admin login completes fresh-password and MFA challenges; bad credentials and non-admin operations fail; refresh works",
        (EC2, COGNITO),
    ),
    Case(
        "E03",
        "#5185",
        "admin",
        "adp admin setup reports ready/pending/failed accurately; interruption and rerun complete missing steps without duplicates",
        (EC2, COGNITO),
    ),
    Case(
        "E04",
        "#5182",
        "personal-aws",
        "adp aws connect provisions/imports compatible roles; list/verify match live records; mismatch fails; disconnect keeps the AWS role",
        (EC2, DESTINATION),
    ),
    Case(
        "E05",
        "#5182",
        "personal-aws",
        "Download then separate AWS-admin apply then resume connects with AWS access disabled in ADP; CloudTrail identifies the EC2 provisioner",
        (EC2, DESTINATION),
    ),
    Case(
        "E06",
        "#5181",
        "routing",
        "Bedrock CLI direct/reuse/download/apply/resume verifies before assignment; failure prevents assignment; rerun is idempotent",
        (EC2, DESTINATION),
    ),
    Case(
        "E07",
        "#5181",
        "routing",
        "User beats team beats org beats platform default; removing overrides exposes the next rung without affecting another user",
        (EC2, DESTINATION, SECOND_DESTINATION),
    ),
    Case(
        "E08",
        "#5181",
        "inference",
        "Real personal Claude/Codex inference at each rung returns the unique marker with matching AWS and ADP usage evidence",
        (EC2, DESTINATION),
    ),
    Case(
        "E09",
        "#5181",
        "inference",
        "Real hosted Claude via authenticated ingress yields owner/tenant/task/run IDs with matching routing and usage at each rung",
        (EC2, HOSTED),
    ),
    Case(
        "E10",
        "#5183",
        "github",
        "Native-admin journey creates a fresh GitHub App and reuses an existing App in separate fixtures with real OAuth/webhook readiness",
        (EC2, GITHUB_APP),
    ),
    Case(
        "E11",
        "#5184",
        "github",
        "Real adp login OAuth/browser approval connects user and repository; wrong repo, cross-tenant and nonce replay fail",
        (EC2, GITHUB_APP, GITHUB_REPO),
    ),
    Case(
        "E12",
        "#5183 #5184",
        "github",
        "Dedicated repo completes one bounded existing agent-development task after CLI onboarding with webhook/run and commit/PR evidence",
        (EC2, GITHUB_APP, GITHUB_REPO),
    ),
    Case(
        "E13",
        "all",
        "parity",
        "Live API field/type/ownership assertions derive from actual CLI/UI consumers; UI-created and CLI-created resources are equivalently usable",
        (EC2, PLATFORM),
    ),
    Case(
        "E14",
        "all",
        "parity",
        "Update/rollback/interrupted download preserve a usable installation; adp codex/claude forwarding, setup and launch pass",
        (EC2, PLATFORM),
    ),
    Case(
        "E15",
        "harness",
        "harness",
        "Two fresh full runs pass on one deployed revision; interrupt, resume and repeat cleanup leave no duplicates or unowned mutations",
        (EC2, PLATFORM),
    ),
    Case(
        "E16",
        "#5413",
        "multi-deployment",
        "One install, three deployments, three concurrent tool sessions (two Codex + Claude and the reverse mix): every marker has an authenticated request and usage receipt at its own deployment for its own user, and none at the other two",
        (EC2, PLATFORM, THREE_DEPLOYMENTS, MULTI_DEPLOYMENT_MODEL_LIMITS),
    ),
    Case(
        "E17",
        "#5413",
        "multi-deployment",
        "Live default switch, refresh, and logout of one deployment leave the other two correctly routed; the logged-out one fails labelled without borrowing a session; teardown leaves no deployment state",
        (EC2, PLATFORM, THREE_DEPLOYMENTS, MULTI_DEPLOYMENT_MODEL_LIMITS),
    ),
    Case(
        "E18",
        "#5637",
        "superplane",
        "Served CLI traverses the gateway to the real domain: workspace create/read/kubeconfig/cost/events/quota/deploy and the provider credential handoff carry both identifiers; a failed second-stage registration compensates only its own credential; account registration reports unavailable without writing",
        (EC2, PLATFORM, SUPERPLANE_DOMAIN),
    ),
    Case(
        "E19",
        "#5621",
        "parity",
        "Freshly served CLI on EC2: adp capabilities distinguishes an enabled operation from an intentionally disabled one and from one the caller may not perform; adp doctor reports read-only bounded findings with no mutation and no paid inference; a foreign request ID is indistinguishable from an absent one",
        (EC2, PLATFORM, CAPABILITY_CONTRAST),
    ),
)

# A small execution checkpoint, deliberately outside the E01–E15 acceptance
# matrix. A basic login must never be presented as the full E02 challenge test.
LOGIN_CHECKPOINT = Case(
    "C01",
    "#5199",
    "login",
    "Native Cognito admin login and refresh work on fresh EC2; no seeded session",
    (EC2, COGNITO),
)
BY_ID = {case.id: case for case in (*CASES, LOGIN_CHECKPOINT)}


def suite_cases(suite):
    """Resolve a suite name to its ordered cases. 'full' is every case."""
    if suite not in SUITES:
        raise ValueError(f"Unknown suite {suite!r}; choose from {', '.join(SUITES)}")
    if suite == "full":
        return CASES
    if suite == "login":
        return (BY_ID["E01"], LOGIN_CHECKPOINT)
    return tuple(case for case in CASES if case.suite == suite)


def resolve_suites(names):
    """Union of several suites, in canonical matrix order, deduplicated."""
    if not names:
        raise ValueError("Select at least one suite")
    selected = set()
    for name in names:
        selected.update(case.id for case in suite_cases(name))
    return tuple(case for case in BY_ID.values() if case.id in selected)


def is_full(names):
    """Only an explicit 'full' selection may satisfy full acceptance.

    Deliberately not 'the union happens to cover every case': a run assembled
    from named suites has not proven it ran as one uninterrupted matrix on one
    revision, which is what E15 is about.
    """
    return "full" in tuple(names)


def new_matrix(names):
    """Start every selected case at NOT_RUN so omissions stay visible.

    Pre-seeding is what makes a crashed or cancelled run report honestly: a case
    that never executed is NOT_RUN in the report rather than absent from it.
    """
    return {
        case.id: {
            "status": NOT_RUN,
            "owner": case.owner,
            "suite": case.suite,
            "detail": {},
        }
        for case in resolve_suites(names)
    }


def record(matrix, case_id, status, detail=None):
    """Set one case's outcome. Unknown IDs and statuses are programming errors."""
    if case_id not in BY_ID:
        raise ValueError(f"Unknown case {case_id!r}")
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status!r}")
    if case_id not in matrix:
        raise ValueError(f"Case {case_id} is not in the selected matrix")
    matrix[case_id] = {**matrix[case_id], "status": status, "detail": detail or {}}
    return matrix[case_id]


def block_missing_fixtures(matrix, available):
    """Mark every not-yet-run case BLOCKED when a fixture class it needs is absent.

    Called by preflight before any mutation. Returns the mapping of case ID to
    the sorted fixture classes that blocked it, for the report and the summary.
    """
    available = set(available)
    blocked = {}
    for case_id, entry in matrix.items():
        if entry["status"] != NOT_RUN:
            continue
        missing = sorted(set(BY_ID[case_id].requires) - available)
        if missing:
            record(matrix, case_id, BLOCKED, {"missing_fixtures": missing})
            blocked[case_id] = missing
    return blocked


def tally(matrix):
    """Count cases per status. Keys are always present, so a report never omits one."""
    counts = dict.fromkeys(STATUSES, 0)
    for entry in matrix.values():
        counts[entry["status"]] += 1
    return counts


def stage_problems(stages):
    """Stages that did not complete, as sorted `name: state` strings.

    Acceptance requires every stage to have completed, not merely that the cases
    look green. The distinction matters most on resume: a passed case is
    preserved across attempts, so if a later attempt's preflight rejects the
    target — a changed or unreachable deployment — the matrix still reads
    fifteen passes. Grading on the matrix alone certifies revision B using
    results collected against revision A.

    The rule is deliberately "not complete" rather than a list of bad states.
    `failed` and `timed_out` are the obvious ones, but `skipped` (a stage with no
    implementation registered), `pending` (never reached) and `running` (the
    process died mid-stage) must all veto too, and enumerating the bad states
    means a state added later defaults to being treated as success.
    """
    return sorted(
        f"{name}: {state}"
        for name, state in (stages or {}).items()
        if state != "complete"
    )


def accept(matrix, names, *, cleanup_ok=True, stages=None):
    """Decide the run's overall verdict.

    Returns (status, reasons). `status` is 'passed' when all selected cases
    passed, every stage completed and cleanup succeeded. Full acceptance also
    requires an explicit full selection, as reported by report.build(). Reasons are
    stable, sorted strings so the offline tests can assert on them and the
    summary can print them.

    `stages` is the run document's stage map. It is keyword-optional so a caller
    grading a bare matrix (the schema check, unit tests) still works, but the
    runner always passes it — see test_publish_carries_stage_state_into_the_verdict.
    """
    reasons = []
    if not matrix:
        reasons.append("no cases were selected")

    incomplete = stage_problems(stages)
    if incomplete:
        # Named individually so the report says which stage broke and how,
        # rather than a bare "something failed".
        reasons.append("stages did not complete: " + ", ".join(incomplete))

    expected = {case.id for case in resolve_suites(names)} if names else set()
    missing = sorted(expected - set(matrix))
    if missing:
        reasons.append("cases missing from the matrix: " + ", ".join(missing))

    for status, label in (
        (FAILED, "failed"),
        (BLOCKED, "blocked"),
        (NOT_RUN, "did not run"),
    ):
        offenders = sorted(
            case_id for case_id, entry in matrix.items() if entry["status"] == status
        )
        if offenders:
            reasons.append(f"{label}: " + ", ".join(offenders))

    if not cleanup_ok:
        reasons.append("cleanup did not complete")

    if reasons:
        return FAILED, sorted(reasons)
    return PASSED, []
