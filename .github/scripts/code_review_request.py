#!/usr/bin/env python3
"""Drive one nightly whole-repo Security Agent code review (intent #4290,
unit U5, issue #4445).

What this does, in order: package the repo as a zip, upload it to the staging
bucket, register the service role and that bucket on the existing agent space,
create the review, start the job, poll it to a terminal state under a bounded
timeout, abort it if the timeout hits, and write the raw findings to a local
file the workflow publishes to the private rendezvous prefix.

Why the two pinned settings are constants and not parameters
------------------------------------------------------------
``validationMode`` and ``codeRemediationStrategy`` are each a single field that
changes what the service *does*:

  * ``validationMode=SIMULATED`` stops being a code review and starts
    exercising live endpoints -- it gives this half the blast radius of the
    pentest half, and it does so without any visible change to the workflow.
  * ``codeRemediationStrategy=AUTOMATIC`` makes the service open its own fix
    pull requests, which race the fix pipeline this EPIC is building: two
    competing fixes per finding, both plausible.

So they are module-level constants read from the validated profile's
``code_review.pinned_modes``, and **no function in this module accepts either
as an argument and no CLI flag sets either**. That distinction is the whole
safety property. A keyword argument defaulting to the safe value is still
reachable -- some later caller passes the other value and nothing here would
notice. A constant with no parameter is not reachable without editing this
file, which is a reviewable diff. ``test_code_review_request.py`` asserts the
*unreachability* structurally -- it parses this module with ``ast`` and checks
that no function parameter and no CLI flag can carry either setting -- rather
than grepping for the permissive literals. The literals DO appear in this file,
in the explanatory comments above and below; a textual-absence test would fail
on the very prose that records why they are dangerous.

Why every value comes from the profile
--------------------------------------
``.github/security/security-agent-profile.json`` (U0, #4439) is the interface,
and the U4 precedent is that its values are read and never retyped. That
matters more than usual here: the runbook and the API disagree in ways that
produce failures which read like something else entirely.

  * ``serviceRole`` is passed on every create call and treated as required.
    The published synopsis marks it optional; the API rejects the call
    without it.
  * ``UpdateAgentSpace`` must run BEFORE ``CreateCodeReview``, and must pass
    the existing ``name`` back even though only ``awsResources`` is changing.
    Skip the registration and the failure is "Service role ... not found in
    agent instance IAM roles" -- which reads like a broken IAM policy and is
    not one.
  * ``BatchGetCodeReviewJobs`` requires ``agentSpaceId`` as well as the job
    ids (the runbook's example omits it).
  * ``ListFindings`` requires job scope. Unscoped it returns the agent
    space's whole history rather than this night's findings.
  * The source archive must be a ``.zip``, despite the generic ``s3Location``
    field name.

This module never calls ``CreateAgentSpace``: the profile records
``agent_space.reuse`` and the space already carries the registrations.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

PROFILE_PATH = (
    Path(__file__).resolve().parent.parent / "security" / "security-agent-profile.json"
)

# --------------------------------------------------------------------------
# the two pinned settings
#
# Names, not values. The values are read from the profile's
# `code_review.pinned_modes` at load time so this file cannot drift from the
# artifact that recorded them. The permissive members of each enum
# (validationMode=SIMULATED, codeRemediationStrategy=AUTOMATIC) appear in this
# source ONLY in explanatory comments like this one -- never as a value that any
# code path can send. What the gate checks is that structural unreachability
# (no function parameter, no CLI flag), not the absence of the strings.
# --------------------------------------------------------------------------
VALIDATION_MODE_FIELD = "validationMode"
REMEDIATION_STRATEGY_FIELD = "codeRemediationStrategy"

# --------------------------------------------------------------------------
# packaging
# --------------------------------------------------------------------------

# One source of truth for the exclusions: the packer walks these and the gate
# asserts against these. Two lists would let the gate pass while the packer
# shipped node_modules.
#
# Directory names, matched on any path component.
EXCLUDED_DIRS = (
    ".venv",
    "node_modules",
    "dist",
    "__pycache__",
    ".terraform",
    "build",
    # Not in the unit's stated list, but excluded for the reason that list
    # exists: the impact analysis names "packaging includes dependencies or
    # build output -> review time and cost balloon". The object store is the
    # largest such directory in this repo by a wide margin, it is not source
    # the service can review, and shipping it would put findings on file
    # paths nobody here owns.
    ".git",
)

# Glob patterns, matched on the file name.
EXCLUDED_FILE_PATTERNS = (
    "*.pyc",
    "*.tfstate*",
)

ARCHIVE_SUFFIX = ".zip"

# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------

# Terminal job states. Asserted to be a subset of the profile's recorded
# `code_review.enums.job_status`, so a service that grows a new state fails
# the gate rather than being silently polled forever.
TERMINAL_JOB_STATUSES = ("COMPLETED", "FAILED", "STOPPED")

# Derived from measurement, not chosen. The profile's
# `code_review.observed_job_durations` records the only two code-review jobs
# ever run to a terminal state on this agent space:
#
#   whole repo    3h 30m 59s  (12659s)
#   gateway only  2h 28m 47s  ( 8927s)
#
# 5h is ~1.4x the slowest observed run. The previous value (3600) predates both
# measurements, and being below them was not a near miss: every night aborted a
# healthy job about 2.5 hours early via StopCodeReviewJob, having paid for an
# hour of metered review, and published an empty findings document -- with an
# error reading "did not reach a terminal state", which looks like a degraded
# service rather than an impatient caller (#4526).
#
# Two invariants bind this number, and `test_code_review_request.py` enforces
# both by parsing the profile and the workflow rather than trusting a literal:
#
#   1. bound > the longest duration in `observed_job_durations`. Dropping back
#      below observed reality reintroduces #4526.
#   2. bound < the review step's `timeout-minutes` in
#      security-agent-nightly.yml. The script's timeout must fire FIRST so the
#      job is *stopped*; a runner kill that lands first leaves a metered job
#      running with nothing holding its id -- unabortable in practice, which is
#      the failure the abort path exists to prevent.
#
# Raising this is cost-reducing, not cost-increasing: today's spend buys
# nothing. Append to the profile's list as further runs complete.
DEFAULT_POLL_TIMEOUT_SECONDS = 18000
DEFAULT_POLL_INTERVAL_SECONDS = 30

# Conservative request-size bound for BatchGetFindings. The spike did not
# record a server-side page ceiling, so this is a self-imposed chunk rather
# than an observed limit -- named as such so nobody reads it as an API fact.
BATCH_GET_CHUNK_SIZE = 25


class CodeReviewError(RuntimeError):
    """The review could not be driven to a terminal state, or the inputs the
    profile is required to supply are missing."""


# --------------------------------------------------------------------------
# profile access
# --------------------------------------------------------------------------


def load_profile(path: Path | str = PROFILE_PATH) -> dict:
    """Load the U0 validated profile. Absence is fatal: every request field
    below comes from it, and inventing them is how a night produces a
    rejected call that reads like a service outage."""
    profile_path = Path(path)
    if not profile_path.is_file():
        raise CodeReviewError(
            f"validated profile not found at {profile_path}. It is the source of "
            "the agent space, service role, staging bucket and pinned modes; "
            "refusing to invent any of them."
        )
    return json.loads(profile_path.read_text(encoding="utf-8"))


def _require(profile: dict, section: str, field: str) -> str:
    value = (profile.get(section) or {}).get(field)
    if not value:
        raise CodeReviewError(
            f"{section}.{field} is missing or empty in the validated profile; "
            "the nightly cannot proceed without it."
        )
    return value


def pinned_modes(profile: dict) -> dict[str, str]:
    """The two pinned settings, read from the profile.

    Returns the exact keyword fragment added to every create call. There is
    deliberately no argument that can influence the result: the only input is
    the profile, and the profile's own gate
    (``test_security_agent_profile.py``) holds those two values at their
    disabled members.
    """
    modes = (profile.get("code_review") or {}).get("pinned_modes") or {}
    validation = modes.get(VALIDATION_MODE_FIELD)
    remediation = modes.get(REMEDIATION_STRATEGY_FIELD)
    if not validation or not remediation:
        raise CodeReviewError(
            "code_review.pinned_modes must record both "
            f"{VALIDATION_MODE_FIELD} and {REMEDIATION_STRATEGY_FIELD}; "
            "refusing to send a create call that omits either, because the "
            "service-side default for both is the permissive member."
        )
    return {
        VALIDATION_MODE_FIELD: validation,
        REMEDIATION_STRATEGY_FIELD: remediation,
    }


def title_charset(profile: dict) -> str:
    """The recorded title charset regex. Read rather than retyped so a change
    to the recorded charset fails the generator instead of passing silently."""
    return _require(profile, "code_review", "title_charset")


# --------------------------------------------------------------------------
# title
# --------------------------------------------------------------------------


def nightly_title(run_date: date | str, prefix: str = "adp-dev-nightly-codereview") -> str:
    """Build the review title: ``<prefix>-YYYYMMDD``.

    The title accepts letters, digits, hyphen and underscore only, up to 100
    characters -- no spaces and no colons. That rules out the obvious
    convenience of naming a review after an issue title or an ISO timestamp,
    both of which are rejected by the API. A malformed title fails the create
    call, and the night then produces nothing with an error that reads like a
    service outage, so this is generated rather than passed in.

    Hyphen-separated date parts are avoided in favour of a compact ``YYYYMMDD``
    only because it is shorter; hyphens are legal.
    """
    if isinstance(run_date, str):
        stamp = run_date.replace("-", "")
    else:
        stamp = f"{run_date:%Y%m%d}"

    if not stamp.isdigit():
        raise CodeReviewError(
            f"run date {run_date!r} does not yield a digit-only stamp; the title "
            "charset forbids everything else."
        )
    return f"{prefix}-{stamp}"


def assert_title_is_legal(title: str, profile: dict) -> str:
    """Validate a title against the *recorded* charset. Called before the
    create request leaves this process, so a bad title is a local failure with
    a clear cause rather than a service-side rejection."""
    import re  # noqa: PLC0415 - only needed on this path

    pattern = title_charset(profile)
    if not re.match(pattern, title):
        raise CodeReviewError(
            f"title {title!r} violates the recorded charset {pattern}. The API "
            "rejects spaces and colons; a title built from an issue title or an "
            "ISO timestamp will always fail."
        )
    return title


# --------------------------------------------------------------------------
# packaging
# --------------------------------------------------------------------------


def is_excluded(relative_path: Path) -> bool:
    """True when a repo-relative path must stay out of the archive."""
    parts = relative_path.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = relative_path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_FILE_PATTERNS)


def build_source_archive(repo_root: Path | str, archive_path: Path | str) -> Path:
    """Package `repo_root` into a deflated **zip** at `archive_path`.

    Zip specifically, not tar: the generic ``s3Location`` field name suggests
    any archive works, and the service rejects everything else.

    Deps and build output are excluded because including them balloons review
    time and cost on vendored code and surfaces findings in files nobody in
    this repo owns.
    """
    root = Path(repo_root).resolve()
    destination = Path(archive_path)
    if destination.suffix != ARCHIVE_SUFFIX:
        raise CodeReviewError(
            f"archive path {destination} must end in {ARCHIVE_SUFFIX}: the "
            "service rejects any other archive format for sourceCode assets."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            # Prune in place so os.walk never descends into an excluded tree.
            # Filtering only at file level would still walk every object in
            # node_modules to discard each one.
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
            for filename in sorted(filenames):
                absolute = current / filename
                if absolute.is_symlink() or not absolute.is_file():
                    continue
                relative = absolute.relative_to(root)
                if is_excluded(relative):
                    continue
                archive.write(absolute, relative.as_posix())
                written += 1

    if written == 0:
        raise CodeReviewError(
            f"packaged 0 files from {root}; refusing to submit an empty review, "
            "which would report zero findings and look like a clean repo."
        )
    print(f"[package] {written} files -> {destination}", flush=True)
    return destination


def upload_archive(s3_client, archive_path: Path | str, bucket: str, key: str) -> str:
    """Upload the archive and return its ``s3://`` URI."""
    s3_client.upload_file(str(archive_path), bucket, key)
    uri = f"s3://{bucket}/{key}"
    print(f"[upload] {archive_path} -> {uri}", flush=True)
    return uri


def staging_object_key(title: str) -> str:
    """Archive key inside the staging bucket.

    Nested under the same ``security-agent/`` prefix the nightly's IAM role is
    bounded to (``securityagent_staging_prefix`` in
    platform/infra/securityagent-nightly-iam.tf). A key outside that prefix
    would upload fine with runner credentials and then be unreadable by the
    service role -- a failure that surfaces only once the job starts.
    """
    return f"security-agent/source/{title}{ARCHIVE_SUFFIX}"


# --------------------------------------------------------------------------
# the review job
# --------------------------------------------------------------------------


def register_agent_space(client, profile: dict) -> str:
    """Register the service role and staging bucket on the existing agent
    space. Returns the agent space id.

    Must complete BEFORE the review is created. The service resolves
    ``serviceRole`` against the space's ``awsResources.iamRoles``, so an
    unregistered role produces "Service role ... not found in agent instance
    IAM roles" at create time -- which reads like a permissions problem and is
    not one.

    ``name`` is passed back unchanged because the update call requires it even
    when only ``awsResources`` is changing.
    """
    agent_space_id = _require(profile, "agent_space", "existing_id")
    name = _require(profile, "agent_space", "existing_name")
    service_role = _require(profile, "agent_space", "service_role")
    bucket = _require(profile, "agent_space", "staging_bucket")

    client.update_agent_space(
        agentSpaceId=agent_space_id,
        name=name,
        awsResources={
            "iamRoles": [service_role],
            "s3Buckets": [f"arn:aws:s3:::{bucket}"],
        },
        codeReviewSettings={
            "controlsScanning": True,
            "generalPurposeScanning": True,
        },
    )
    print(f"[agent-space] registered role + bucket on {agent_space_id}", flush=True)
    return agent_space_id


def create_review(client, profile: dict, agent_space_id: str, title: str, source_uri: str) -> str:
    """Create the review and return its id.

    The two pinned settings are added from :func:`pinned_modes`. This function
    has no parameter that can influence them.
    """
    response = client.create_code_review(
        title=assert_title_is_legal(title, profile),
        agentSpaceId=agent_space_id,
        assets={"sourceCode": [{"s3Location": source_uri}]},
        # Required in practice even though the synopsis marks it optional.
        serviceRole=_require(profile, "agent_space", "service_role"),
        **pinned_modes(profile),
    )
    code_review_id = response["codeReviewId"]
    print(f"[create] codeReviewId={code_review_id} title={title}", flush=True)
    return code_review_id


def start_job(client, agent_space_id: str, code_review_id: str) -> str:
    """Start the (metered) job over the whole zip and return its id."""
    response = client.start_code_review_job(
        agentSpaceId=agent_space_id,
        codeReviewId=code_review_id,
    )
    job_id = response["codeReviewJobId"]
    print(f"[start] codeReviewJobId={job_id}", flush=True)
    return job_id


def job_status(client, agent_space_id: str, job_id: str) -> str:
    """Read one job's status. ``agentSpaceId`` is required here as well as the
    job ids -- the runbook's example omits it and the call fails."""
    response = client.batch_get_code_review_jobs(
        agentSpaceId=agent_space_id,
        codeReviewJobIds=[job_id],
    )
    jobs = response.get("codeReviewJobs") or []
    if not jobs:
        raise CodeReviewError(
            f"job {job_id} not returned by BatchGetCodeReviewJobs; cannot "
            "determine whether it is still running."
        )
    return jobs[0]["status"]


def poll_until_terminal(
    client,
    agent_space_id: str,
    job_id: str,
    timeout_seconds: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    clock=time.monotonic,
    sleeper=time.sleep,
) -> str:
    """Poll to a terminal state, or abort the job and raise on timeout.

    On timeout this calls ``StopCodeReviewJob``. Simply returning would leave
    the workflow to hang to its runner timeout while a metered job kept
    running with nothing left holding its id -- unabortable in practice. The
    abort is the point of the bound, not a tidy-up.

    ``clock`` and ``sleeper`` are injected so the timeout path is testable
    without a wall-clock wait.
    """
    deadline = clock() + timeout_seconds
    while True:
        status = job_status(client, agent_space_id, job_id)
        if status in TERMINAL_JOB_STATUSES:
            print(f"[poll] terminal status={status}", flush=True)
            return status

        if clock() >= deadline:
            print(
                f"[poll] timeout after {timeout_seconds}s with status={status}; "
                "stopping the job",
                flush=True,
            )
            client.stop_code_review_job(
                agentSpaceId=agent_space_id,
                codeReviewJobId=job_id,
            )
            raise CodeReviewError(
                f"code review job {job_id} did not reach a terminal state within "
                f"{timeout_seconds}s (last status {status}). StopCodeReviewJob was "
                "called, so the job is aborted rather than left running."
            )

        print(f"[poll] status={status}; sleeping {interval_seconds}s", flush=True)
        sleeper(interval_seconds)


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


def collect_findings(client, profile: dict, agent_space_id: str, job_id: str) -> list[dict]:
    """Fetch this job's findings, in full.

    ``ListFindings`` is scoped to ``codeReviewJobId``: the profile records that
    job scope is required, and an unscoped list returns the agent space's whole
    history rather than this night's results. The summaries carry ids only, so
    each page is expanded through ``BatchGetFindings``.
    """
    list_key = (profile.get("findings") or {}).get(
        "list_response_key", "findingsSummaries"
    )
    batch_key = (profile.get("findings") or {}).get("batch_get_response_key", "findings")

    finding_ids: list[str] = []
    next_token: str | None = None
    while True:
        kwargs = {"agentSpaceId": agent_space_id, "codeReviewJobId": job_id}
        if next_token:
            kwargs["nextToken"] = next_token
        page = client.list_findings(**kwargs)
        for summary in page.get(list_key) or []:
            finding_id = summary.get("findingId")
            if finding_id:
                finding_ids.append(finding_id)
        next_token = page.get("nextToken")
        if not next_token:
            break

    findings: list[dict] = []
    for start in range(0, len(finding_ids), BATCH_GET_CHUNK_SIZE):
        chunk = finding_ids[start : start + BATCH_GET_CHUNK_SIZE]
        response = client.batch_get_findings(
            agentSpaceId=agent_space_id,
            findingIds=chunk,
        )
        findings.extend(response.get(batch_key) or [])

    print(f"[findings] {len(findings)} finding(s) for job {job_id}", flush=True)
    return findings


def write_findings(
    path: Path | str,
    *,
    findings: list[dict],
    run_date: str,
    title: str,
    agent_space_id: str,
    code_review_id: str,
    job_id: str,
    status: str,
) -> Path:
    """Write the raw findings document the dedup unit consumes.

    Findings are written verbatim -- no normalisation, no dedup, no severity
    remapping. That is the consuming unit's job, and doing any of it here would
    mean the dedup unit could never see what the service actually said.

    ``default=str`` covers the timestamp fields boto3 returns as datetimes.
    """
    document = {
        "runDate": run_date,
        "title": title,
        "agentSpaceId": agent_space_id,
        "codeReviewId": code_review_id,
        "codeReviewJobId": job_id,
        "jobStatus": status,
        "findingCount": len(findings),
        "findings": findings,
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")
    print(f"[write] {len(findings)} finding(s) -> {output}", flush=True)
    return output


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def run_code_review(
    client,
    s3_client,
    profile: dict,
    *,
    repo_root: Path | str,
    archive_path: Path | str,
    output_path: Path | str,
    run_date: str,
    timeout_seconds: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    clock=time.monotonic,
    sleeper=time.sleep,
) -> dict:
    """Run one whole-repo review end to end and return a result summary.

    Note the absent parameters: there is no way for a caller to reach
    ``validationMode`` or ``codeRemediationStrategy``. Both come from
    :func:`pinned_modes`, which takes only the profile.
    """
    title = nightly_title(run_date)
    assert_title_is_legal(title, profile)

    build_source_archive(repo_root, archive_path)
    bucket = _require(profile, "agent_space", "staging_bucket")
    source_uri = upload_archive(
        s3_client, archive_path, bucket, staging_object_key(title)
    )

    # Ordering requirement, not a preference: registration first, then create.
    agent_space_id = register_agent_space(client, profile)
    code_review_id = create_review(client, profile, agent_space_id, title, source_uri)
    job_id = start_job(client, agent_space_id, code_review_id)

    status = poll_until_terminal(
        client,
        agent_space_id,
        job_id,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        clock=clock,
        sleeper=sleeper,
    )

    findings = collect_findings(client, profile, agent_space_id, job_id)
    write_findings(
        output_path,
        findings=findings,
        run_date=run_date,
        title=title,
        agent_space_id=agent_space_id,
        code_review_id=code_review_id,
        job_id=job_id,
        status=status,
    )

    return {
        "title": title,
        "agentSpaceId": agent_space_id,
        "codeReviewId": code_review_id,
        "codeReviewJobId": job_id,
        "jobStatus": status,
        "findingCount": len(findings),
        "sourceUri": source_uri,
        "outputPath": str(output_path),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _default_run_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_parser() -> argparse.ArgumentParser:
    """Note what is NOT here: no --validation-mode and no --remediation-strategy.
    A flag for either would make the permissive value reachable from a workflow
    edit, which is precisely what this unit pins shut."""
    parser = argparse.ArgumentParser(
        description="Run one nightly whole-repo Security Agent code review."
    )
    parser.add_argument("--profile", default=str(PROFILE_PATH))
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[2]),
        help="Directory to package and review.",
    )
    parser.add_argument("--archive", default="/tmp/adp-code-review-source.zip")  # nosec B108
    parser.add_argument(
        "--output",
        default="code-review-findings.json",
        help="Where to write the raw findings document.",
    )
    parser.add_argument("--run-date", default=None, help="YYYY-MM-DD; defaults to today (UTC).")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_POLL_TIMEOUT_SECONDS)
    parser.add_argument(
        "--poll-interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        profile = load_profile(args.profile)

        import boto3  # noqa: PLC0415 - imported late so --help works unprovisioned

        result = run_code_review(
            boto3.client("securityagent"),
            boto3.client("s3"),
            profile,
            repo_root=args.repo_root,
            archive_path=args.archive,
            output_path=args.output,
            run_date=args.run_date or _default_run_date(),
            timeout_seconds=args.timeout_seconds,
            interval_seconds=args.poll_interval_seconds,
        )
    except CodeReviewError as exc:
        print(f"::error title=Security Agent code review::{exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2), flush=True)
    if result["jobStatus"] != "COMPLETED":
        print(
            f"::error title=Security Agent code review::job reached terminal state "
            f"{result['jobStatus']}, not COMPLETED",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
