"""Collect the `wave4_preflight` artifact by asking the systems that hold the facts.

What this collects, and from where — each field is one measurement, and the source is
chosen so the answer is not the operator's to write:

  deployed_components  git + `aws ecr describe-images` + `aws codebuild
                       batch-get-builds`, via the shared component collector
  frontend             git + the SERVED assets, fetched from the deployment
  prior_waves          each earlier wave's own `result.json`, read as written
  merged_revisions     `git rev-parse` + `git merge-base --is-ancestor`
  ci_gates             `gh run view --json ...`, archived verbatim
  browser_identity     the gateway's own answer to "who is this token"
  ordinary_*           the live flag surface, read rather than asserted

The one field that most needs its provenance defended is `served_asset_evidence`.
"Is the deployed bundle the revision we think?" cannot be answered from git: git says
what the revision CONTAINS, and the question is what the deployment is SERVING. So
this fetches the SPA's entry point from the gateway, extracts the hashed asset names
the HTML references, and compares them against the assets a build of the claimed
revision produces. A match is evidence; a mismatch names which side is stale; an
unreachable deployment is a refusal, never a `verified: false`.

That last distinction is the module's reason for existing. `verified: false` says "we
looked at the served bundle and it was not the claimed revision", which is a
deployment defect. A refusal says "we could not reach the deployment", which is a
collection problem. Emitting the first when the second is true sends an operator to
redeploy something that was fine.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .collector import (
    Artifact,
    CommandResult,
    Measured,
    Refused,
    is_refused,
    measure_command,
    measure_json_command,
    run_command,
    value_of,
)

# The assets a Vite build emits are content-hashed, which is what makes this
# comparison possible at all: `index-a1b2c3d4.js` changes name whenever its content
# changes, so the set of names the served HTML references IS a fingerprint of the
# bundle. Matched loosely on purpose — the hash length and the separator are Vite's
# to change, and a pattern pinned to today's exact format would start silently
# matching nothing after an upgrade, which would make this check vacuous rather than
# failing.
_ASSET_REFERENCE = re.compile(r"""["'(]([^"'()\s]*/assets/[^"'()\s]+\.(?:js|css))["')]""")

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(
    argv: Sequence[str],
    *,
    what: str,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
    repo: str = ".",
) -> Measured:
    return measure_command(["git", "-C", repo, *argv], what=what, runner=runner)


def measure_revision(
    ref: str,
    *,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
    repo: str = ".",
) -> Measured:
    """One ref resolved to a full 40-character SHA, or a refusal.

    `rev-parse` on an unknown ref exits nonzero, so an invented ref is a refusal
    rather than a value. A ref that resolves to something that is not a full SHA is
    also refused: `rev-parse` can print an abbreviated hash under some configs, and a
    short SHA names whatever that prefix happened to match.
    """
    measured = _git(["rev-parse", ref], what=f"revision of {ref!r}", runner=runner, repo=repo)
    if is_refused(measured):
        return measured
    if not _GIT_SHA.match(measured):
        return Refused(
            f"revision of {ref!r}: `git rev-parse` returned {measured!r}, which is not a full "
            "40-character SHA. A short SHA names whatever that prefix matched"
        )
    return measured


def measure_containment(
    revision: str,
    descendant: str,
    *,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
    repo: str = ".",
) -> Measured:
    """Whether `revision` is an ancestor of `descendant`, from the commit graph.

    Collected even though the evaluator recomputes it. That is not redundancy: the
    evaluator computes it so the artifact cannot assert it, and the collector computes
    it so an operator finds out BEFORE publishing evidence that their deployment does
    not contain the story they are evidencing. The evaluator's answer is the one that
    counts, and this one never reaches an artifact field the evaluator reads.

    `--is-ancestor` exits 0 for yes and 1 for no, so the two are distinguishable from
    a real failure (128 for an unknown object) only by the code. A 1 is a measured
    False; anything else is a refusal.
    """
    result = run_command(
        ["git", "-C", repo, "merge-base", "--is-ancestor", revision, descendant],
        runner=runner,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return Refused(
        f"containment of {revision} in {descendant}: `git merge-base --is-ancestor` exited "
        f"{result.returncode} ({result.stderr.strip()[:200]}). An unknown object cannot be placed in "
        "the graph, which is not the same as being absent from it"
    )


def measure_served_assets(
    base_url: str,
    *,
    fetch: Callable[[str], tuple[int, str]],
) -> Measured:
    """The hashed asset names the DEPLOYED SPA's entry point references.

    `fetch` returns `(status, body)` and is injected, so this is testable against a
    controlled transport and so the collector has exactly one place where a timeout or
    a redirect policy is set.

    Every non-200, every unparseable body and every asset-free page is a refusal. The
    last one matters most and is the easiest to get wrong: a gateway that answers the
    SPA route with its own HTML error page returns 200 with a body containing no
    asset references at all. Treating that as "no assets served" would compare an
    empty set against an empty set and report a match — a green field produced by a
    broken deployment, which is precisely the false pass this whole exercise is
    about.
    """
    url = base_url.rstrip("/") + "/"
    try:
        status, body = fetch(url)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a refusal
        return Refused(f"served assets: GET {url} failed: {exc}")
    if status != 200:
        return Refused(f"served assets: GET {url} returned {status}, expected 200")
    references = sorted({match.group(1).rsplit("/", 1)[-1] for match in _ASSET_REFERENCE.finditer(body)})
    if not references:
        return Refused(
            f"served assets: GET {url} returned 200 but its body references no hashed assets. An "
            "error page served at the SPA route looks exactly like this, and an empty set would "
            "trivially match any other empty set"
        )
    return references


def measure_built_assets(
    revision: str,
    *,
    dist_listing: Callable[[str], Sequence[str]],
) -> Measured:
    """The hashed asset names a build of `revision` produces.

    `dist_listing` is injected rather than this module running `npm run build`: the
    build is the operator's step (it needs the node toolchain, the env, and several
    minutes), and a collector that silently rebuilt would be doing something very
    expensive as a side effect of gathering evidence. What this module owns is the
    COMPARISON and the refusal semantics around it.
    """
    try:
        names = sorted({str(name).rsplit("/", 1)[-1] for name in dist_listing(revision)})
    except Exception as exc:  # noqa: BLE001
        return Refused(f"built assets for {revision}: {exc}")
    if not names:
        return Refused(
            f"built assets for {revision}: the build produced no hashed assets, so there is nothing "
            "to compare the served bundle against"
        )
    return names


def measure_served_asset_evidence(
    revision: str,
    *,
    served: Measured,
    built: Measured,
) -> Measured:
    """Whether the deployment is serving a build of `revision`.

    The three outcomes are kept genuinely distinct:

    * `verified: True` with both asset sets recorded — they matched.
    * `verified: False` with the symmetric difference recorded — they did not, and the
      record says which names differ so an operator can see whether the served bundle
      is older or newer.
    * a refusal — one side could not be measured. NOT `verified: False`: "we could not
      look" and "we looked and it did not match" send an operator to different places,
      and only the second is a deployment defect.
    """
    if is_refused(served):
        return Refused(f"served_asset_evidence: {served.reason}")
    if is_refused(built):
        return Refused(f"served_asset_evidence: {built.reason}")
    served_set, built_set = set(served), set(built)
    return {
        "verified": served_set == built_set,
        "revision": revision,
        "served_assets": sorted(served_set),
        "built_assets": sorted(built_set),
        "only_served": sorted(served_set - built_set),
        "only_built": sorted(built_set - served_set),
    }


def measure_prior_wave(
    wave: int,
    *,
    read_result: Callable[[int], Mapping[str, Any]],
) -> Measured:
    """One earlier wave's acceptance, read out of the report that wave produced.

    `read_result` returns the wave's own `result.json`. Reading the report rather than
    asking the operator is the whole point: the counts, the run identity and the
    cleanup outcome are all already written there by the run that produced them, and
    re-typing them is how a 9/10 becomes a 10/10.

    Note what is deliberately NOT emitted: `compatible`. The evaluator computes
    containment from the commit graph, and a collector supplying its own answer would
    be handing over the conclusion the check exists to reach. `measure_containment`
    exists for the operator's benefit, not for this artifact.
    """
    try:
        report = read_result(wave)
    except Exception as exc:  # noqa: BLE001
        return Refused(f"wave {wave} acceptance: cannot read its result.json: {exc}")
    if not isinstance(report, Mapping):
        return Refused(f"wave {wave} acceptance: its result.json is {type(report).__name__}, not an object")

    missing = [
        key for key in ("evaluation", "run_id", "revision", "passed", "required") if key not in report
    ]
    if missing:
        return Refused(
            f"wave {wave} acceptance: its result.json omits {sorted(missing)}, so the acceptance cannot "
            "be summarised from it without inventing the missing fields"
        )
    # `cleanup_ok` lives under `fixture_cleanup.ok` in a real report, with the flat
    # key kept for older ones. Both are read; neither is defaulted, because a report
    # that records no cleanup outcome has not told us cleanup succeeded.
    cleanup = report.get("fixture_cleanup")
    if isinstance(cleanup, Mapping) and "ok" in cleanup:
        cleanup_ok = cleanup["ok"]
    elif "cleanup_ok" in report:
        cleanup_ok = report["cleanup_ok"]
    else:
        return Refused(
            f"wave {wave} acceptance: its result.json records no cleanup outcome. An accepted wave whose "
            "fixture was left enabled is the DP-INV-1 state, so a missing outcome cannot be read as a "
            "successful one"
        )

    passed, required = report["passed"], report["required"]
    return {
        "accepted": bool(passed == required and required > 0 and cleanup_ok is True),
        "evaluation": str(report["evaluation"]),
        "run_id": str(report["run_id"]),
        "revision": str(report["revision"]),
        "passed": passed,
        "required": required,
        "cleanup_ok": cleanup_ok,
    }


def measure_gate(
    gate: str,
    *,
    run_lookup: Callable[[str], Measured],
    retrieved_at: str,
) -> Measured:
    """One CI gate, with the `gh run view` response archived verbatim.

    The archive is the point. A summary of what CI said is written by the operator,
    whereas the response is written by GitHub — and the evaluator parses the response
    at its field locations rather than searching it, so a red run cannot pass by
    containing the right strings somewhere.

    The summary fields ARE emitted alongside it, and that is not duplication: the
    evaluator cross-checks them against the archive, so a disagreement between the
    two is itself a finding. What matters is that the archive is the source and the
    summary is derived from it here, rather than both being typed.
    """
    response = run_lookup(gate)
    if is_refused(response):
        return Refused(f"the {gate!r} gate: {response.reason}")
    if not isinstance(response, Mapping):
        return Refused(f"the {gate!r} gate: `gh run view` returned {type(response).__name__}, not an object")

    run_id = response.get("databaseId")
    head = response.get("headSha")
    if not run_id:
        return Refused(f"the {gate!r} gate: the run response carries no databaseId")
    if not isinstance(head, str) or not _GIT_SHA.match(head):
        return Refused(
            f"the {gate!r} gate: the run response records headSha {head!r}, which is not a full "
            "40-character SHA"
        )
    jobs = response.get("jobs")
    named = None
    if isinstance(jobs, Sequence):
        named = next(
            (job for job in jobs if isinstance(job, Mapping) and job.get("name") == gate), None
        )
    if named is None:
        return Refused(
            f"the {gate!r} gate: no job of that exact name appears in run {run_id}. 'Some job in this "
            "run passed' is a different and much weaker claim than 'this gate passed'"
        )
    conclusion = named.get("conclusion")
    status = "passed" if conclusion == "success" else "failed"

    return {
        "status": status,
        "run_id": str(run_id),
        "run_url": str(response.get("url") or f"https://github.com/aws-e/adp/actions/runs/{run_id}"),
        "tested_revision": head,
        "raw": {
            "run": {
                "command": (
                    f"gh run view {run_id} --json databaseId,headSha,attempt,event,conclusion,jobs,url"
                ),
                "retrieved_at": retrieved_at,
                "body": dict(response),
            }
        },
    }


def collect(
    *,
    config: Mapping[str, Any],
    retrieved_at: str,
    frontend_ref: str,
    story_refs: Mapping[str, str],
    prior_waves: Sequence[int],
    gates: Sequence[str],
    fetch: Callable[[str], tuple[int, str]],
    dist_listing: Callable[[str], Sequence[str]],
    read_result: Callable[[int], Mapping[str, Any]],
    run_lookup: Callable[[str], Measured],
    identity_lookup: Callable[[], Measured],
    flag_lookup: Callable[[], Measured],
    deployed_components: Mapping[str, Any] | Refused,
    git_runner: Callable[[Sequence[str]], CommandResult] | None = None,
    repo: str = ".",
) -> Artifact:
    """Assemble `wave4_preflight` from measurements, omitting what was refused.

    Every collaborator is injected. That is what makes the end-to-end tests real
    tests: they drive this function through controlled transports and then feed its
    output to the actual evaluator, so what is exercised is the true
    collector → artifact → evaluator path rather than a hand-written fixture that
    happens to resemble one.
    """
    artifact = Artifact("wave4_preflight")

    frontend_revision = measure_revision(frontend_ref, runner=git_runner, repo=repo)
    if is_refused(frontend_revision):
        # Several fields below are measured RELATIVE to this revision, so without it
        # they cannot be attempted at all. Each is refused naming this cause rather
        # than left absent, so the operator sees one root cause instead of four
        # unexplained gaps.
        artifact.set("frontend", Refused(f"frontend: {frontend_revision.reason}"))
    else:
        served = measure_served_assets(str(config.get("frontend_url") or config["gateway_url"]), fetch=fetch)
        built = measure_built_assets(frontend_revision, dist_listing=dist_listing)
        evidence = measure_served_asset_evidence(frontend_revision, served=served, built=built)
        if is_refused(evidence):
            artifact.set("frontend", Refused(f"frontend: {evidence.reason}"))
        else:
            artifact.set(
                "frontend",
                {
                    "revision": frontend_revision,
                    # The digest of the asset set actually being served, derived from
                    # the names rather than supplied. Content-hashed names make this a
                    # real fingerprint.
                    "asset_digest": _digest(evidence["served_assets"]),
                    "served_asset_evidence": evidence,
                },
            )

    artifact.set("deployed_components", deployed_components)

    # Prior waves: each read from its own report, each refusal kept separate so one
    # unreadable wave does not hide the others.
    wave_records: dict[str, Any] = {}
    wave_refusals: list[str] = []
    for wave in prior_waves:
        record = measure_prior_wave(wave, read_result=read_result)
        if is_refused(record):
            wave_refusals.append(record.reason)
        else:
            wave_records[str(wave)] = record
    if wave_refusals and not wave_records:
        artifact.set("prior_waves", Refused("prior_waves: " + "; ".join(wave_refusals)))
    else:
        # A PARTIAL map is emitted deliberately. The evaluator requires an entry for
        # every wave in WAVE4_PRIOR_WAVES and names the missing one, which is a better
        # failure than omitting the whole field: "wave 3's acceptance is unreadable" is
        # actionable, "prior_waves is absent" is not.
        artifact.set("prior_waves", wave_records)

    merged: dict[str, Any] = {}
    for story, ref in story_refs.items():
        revision = measure_revision(ref, runner=git_runner, repo=repo)
        if is_refused(revision):
            continue
        # "Merged" is a graph relation, not a claim: is this story's revision contained
        # in the default branch? Asked of git, so an unmerged branch cannot be recorded
        # as merged.
        contained = measure_containment(revision, "origin/main", runner=git_runner, repo=repo)
        if is_refused(contained):
            continue
        merged[story] = {"merged": contained, "revision": revision}
    artifact.set("merged_revisions", merged if merged else Refused(
        "merged_revisions: no story revision could be resolved and placed in the commit graph"
    ))

    artifact.set(
        "ci_gates",
        {
            gate: measured
            for gate in gates
            if not is_refused(measured := measure_gate(gate, run_lookup=run_lookup, retrieved_at=retrieved_at))
        }
        or Refused("ci_gates: no gate's run response could be retrieved"),
    )

    artifact.set("browser_identity", _measure_identity(identity_lookup))
    flags = flag_lookup()
    if is_refused(flags):
        artifact.set("ordinary_users_gated", Refused(f"ordinary_users_gated: {flags.reason}"))
        artifact.set("ordinary_flags_off", Refused(f"ordinary_flags_off: {flags.reason}"))
    else:
        artifact.set("ordinary_users_gated", flags.get("ordinary_users_gated"))
        artifact.set("ordinary_flags_off", flags.get("ordinary_flags_off"))

    artifact.set(
        "fixture_identity",
        {
            "account_id": str(config.get("account_id") or ""),
            "environment": config.get("environment"),
            "run_id": config.get("live_run_id"),
        },
    )
    return artifact


def _measure_identity(identity_lookup: Callable[[], Measured]) -> Measured:
    """Who the browser drove, as the GATEWAY reports it.

    Asked of the deployment rather than recorded by the operator, because "this token
    owns this run" is the gateway's answer to give. An operator labelling their own
    token `owner` is the assertion this replaces.
    """
    answer = identity_lookup()
    if is_refused(answer):
        return Refused(f"browser_identity: {answer.reason}")
    if not isinstance(answer, Mapping):
        return Refused(f"browser_identity: the identity lookup returned {type(answer).__name__}")
    for key in ("role", "is_run_owner"):
        if key not in answer:
            return Refused(
                f"browser_identity: the identity lookup did not report {key!r}, which is the field that "
                "distinguishes the run's owner from any other authenticated identity"
            )
    return {"role": answer["role"], "is_run_owner": answer["is_run_owner"]}


def _digest(names: Sequence[str]) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(
        json.dumps(sorted(names), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def measure_component_digest(
    repository: str,
    tag: str,
    *,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
) -> Measured:
    """One component's registry digest for a tag, from ECR.

    Kept here rather than hand-written into the artifact because the question the
    evaluator asks is "what is the registry serving for this tag TODAY" — which a
    recorded digest answers only as of whenever it was recorded.
    """
    measured = measure_json_command(
        [
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            repository,
            "--image-ids",
            f"imageTag={tag}",
            "--query",
            "imageDetails[0].imageDigest",
            "--output",
            "json",
        ],
        what=f"registry digest of {repository}:{tag}",
        runner=runner,
    )
    if is_refused(measured):
        return measured
    if not isinstance(measured, str) or not measured.startswith("sha256:"):
        return Refused(
            f"registry digest of {repository}:{tag}: ECR returned {measured!r}, which is not a sha256 "
            "digest"
        )
    return measured


__all__ = [
    "collect",
    "measure_built_assets",
    "measure_component_digest",
    "measure_containment",
    "measure_gate",
    "measure_prior_wave",
    "measure_revision",
    "measure_served_asset_evidence",
    "measure_served_assets",
    "value_of",
]
