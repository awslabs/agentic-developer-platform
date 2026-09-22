# Learnings — issue #5616 (S17: cyber worker script execution + tenant-scoped object access)

2026-09-22 security work package S17. Two 2026-08-30 findings to revalidate and remediate:
#4729 (cyber worker runs caller-named scripts with no real validation or isolation) and
#4730 (cyber workers read arbitrary and cross-org stored objects).

Outcome: both confirmed still live on current main, not stale. Enforcement moved to the
consumer (a shared resolver plus a script guard), producers updated to carry identity, pods
hardened, IAM narrowed, unused credential grant removed. 141 tests pass, up from 66; the 7
remaining failures are pre-existing libmagic import errors on main.

## What generalises to other security work packages

**"Verify the finding is still live" is not a formality, and the useful output is a
reproduction, not a yes/no.** The issue explicitly framed the open state as "historical
evidence, not proof the same vulnerability exists today". Both were still live — but the
valuable artifact was proving *how*. For #4729 I ran the existing validator against
`requests`, `boto3`, `os.system`, `eval`, `exec` and `__import__` and watched it pass all of
them. That converted "there is a validator, is it enough?" into the precise claim: it checks
whether a tool is *present in the image*, which is a compatibility check, not a safety one.
That distinction determined the whole design — extend the validator with capability denial
rather than add a second parallel checker.

**A prompt instruction to the producer is not an enforcement point.** The pre-existing design
asked the agent to run `validate_script.py` before uploading, and the worker never re-checked.
Anything that could put a message on the queue got code execution. The general shape: when the
thing being validated and the thing doing the validating are the same untrusted party, there is
no boundary. Move the check to the consumer and let its verdict block the action. Keeping the
producer-side check as a *convenience* is fine — but the docs must say which one is the
boundary, or the next reader will assume the cheap one is load-bearing.

**Test the absence of the dangerous call, not the presence of an error.** My first-cut
no-leak tests asserted that a refusal did not echo the victim's key names. They passed against
the *unfixed* worker — because a worker that happily reads the victim's object also leaks no
key names in the response it never refuses. The assertion has to be "the refusal happened
first", and for download paths, "`s3.download_file` was never called". Asserting on the mock's
call list is what makes it real.

**Run new security tests against the pre-fix code before trusting them.** Every suite here was
checked this way by stashing the fix: 11/12 dispatch tests fail pre-fix, 22/24 pod-security
tests fail unhardened, 9/16 IAM tests fail against the old policy. That exercise found two
vacuous tests I would otherwise have shipped, and it identifies *which* passes are legitimate
(the happy-path regression guard must pass in both states; the pre-existing egress policy
should too). A security test that cannot fail is documentation with a misleading name.

**Enumerate dispatch modes explicitly — the bug will be in the path you didn't wire.** The
acceptance criteria demanded this and it was right. There were three download/execution paths
(triage, static Mode A, static Mode B). Guarding one and missing another leaves the exposure
open while the issue looks closed, so the tests are parametrised over all three and both
workers share one resolver rather than each having its own copy.

**`str.startswith` is not path containment, and segment comparison alone is not either.** Two
separate defects, and I introduced the second one myself. `o/acme` matching `o/acme-evil` is
the well-known one. The one I shipped and then caught with a probe: after comparing segments
correctly, `o/acme/t/t1/u/u1/../../../o/victim/...` still escapes, because traversal is
resolved by whoever consumes the key. Reject `.`/`..` segments at parse time *and* compare
segment-wise. My docstring had confidently claimed the segment comparison handled traversal —
the probe disproved my own comment, which is a good argument for probing your own claims
rather than re-reading them.

**A non-echoing refusal is part of the fix, not politeness.** If a blocked cross-tenant read
replies "cannot read `o/victim-org/t/secret-team/...`", the block has been converted into a
key-name disclosure oracle. Refusals return stable reason codes only, and a test asserts the
victim's identifiers appear nowhere in the response or the DDB row. Same reasoning for digest
mismatches: report the mismatch, not the two digests.

**Fail-closed changes are incomplete until a producer supplies the new input — check who
produces the message.** Making the worker refuse jobs without identity would have refused
*every* job: no producer sent `org_id`/`team_id`/`user_id`. Grepping for the producers found
exactly two dispatching skills (stage-1, stage-3) matching the two hardened workers, and an
existing test encoding the old contract that my change correctly broke. Ship the enforcement
and the producer update together, and separate "my regression" from "pre-existing failure" by
stashing and re-running rather than assuming.

**Where identity comes from is the actual security property.** Had I read `org_id` from the
issue body, an attacker could file an issue naming the victim's identity *and* a path inside
the victim's space — the two would agree, every check would pass, and the confinement would be
worthless. Identity has to arrive on a channel the requester doesn't control. Relatedly: no
`||` default in the workflow env, because a fallback becomes a shared identity any caller can
claim. Unset must mean refused.

**Say what a narrowing does and does not accomplish.** Anchoring the IAM pattern to
`o/*/t/*/u/*/s/*/*/in/*` removes the any-depth reach of `o/*/in/*` (an IAM `*` matches `/`, so
one wildcard spanned every org). It does not make the role tenant-scoped: all tenants share one
role, so there is no per-tenant value for IAM to substitute, and no static pattern separates
org A from org B. That needs session-tagged or per-job credentials. Writing this in the policy
comment matters more than the change itself — an over-claimed control is worse than a
documented partial one, because the next person stops looking.

**Mapping IAM against the code path finds gaps the findings didn't mention.** Evaluating the
grants with `fnmatch` (same wildcard semantics as IAM) showed there was *no* grant for any
`scripts/` prefix — Mode B's script download was never permitted. Which means Mode B could only
ever have worked by staging the script where samples go: the old path depended on treating an
uploaded input as executable code, i.e. the shape of #4729 itself. A test now asserts the
sample and script grants do not overlap.

**Unused privilege is the cheapest thing to remove and the easiest to leave.** The worker role
held `secretsmanager:GetSecretValue`; no worker makes a Secrets Manager call anywhere. Removal
cannot break a working path, and on a pod that parses hostile binaries and executes generated
scripts, an unused credential is exactly what an escape reaches for. Verify by searching for
the call, not by reading the comment that says why the grant exists.

**Hardening must be checked against what the workload actually does, including the
sidecars.** `readOnlyRootFilesystem` was safe only because every write goes through
`tempfile.TemporaryDirectory()` — verified, not assumed. Two things that would have broken in
production and neither is obvious: `runAsNonRoot` propagates to the initContainer, and
`amazon/aws-cli` defaults `HOME=/root`, which uid 1001 cannot write — so the YARA rule fetch
fails and the static worker starts with *no rules*, a silent loss of detection rather than a
crash. And any library caching under `HOME` (magika's model) fails on a read-only root. The
failure mode to fear in hardening work is the silent degradation, not the crash.

**Infrastructure invariants need tests because they regress without failing.** Nothing breaks
when a `securityContext` is dropped or a wildcard widens — the pods just run with more
privilege. Rendering the manifests exactly as the deploy pipeline renders them also caught that
my new placeholder wasn't substituted anywhere, which would have shipped
`CYBER_ALLOWED_BUCKETS=REPLACE_WITH_...` and denied every read, failing the pipeline closed
with no obvious cause. Added a deploy step that fails on any surviving placeholder.

**Pin contracts you cannot execute in this environment.** Docker was unavailable, so the image
build is unverified. The two paths that would break Mode B entirely if they drifted (validator
destination, manifest location) are now asserted statically against the Dockerfile, and I
mutation-tested that assertion to confirm it fails on a moved path. Because the guard fails
closed, a drifted path would otherwise surface as `worker_manifest_unavailable` on every Mode B
job rather than announcing itself as a packaging bug.

**Call-time env reads beat import-time constants.** A test failed because `script_guard` read
its paths at import, so `monkeypatch.setenv` came too late. The fix — read `os.environ` inside
a function — was also a genuine operability improvement, since the running process now reflects
its current environment. When a module is awkward to test, that is often a real design signal
rather than a testing inconvenience.

## Repo-specific notes

- **Two divergent copies of `bs-cyber-worker.yml`.** The live one is repo-root
  `codebuild/bs-cyber-worker.yml` (referenced by `.github/workflows/cyber-worker-build.yml` and
  `platform/infra/modules/codebuild/main.tf`). `modules/domain-apps/cyber/codebuild/` holds an
  unreferenced duplicate. Confirm which is wired before editing.
- **`.claude/skills/` and `.adp-rules/personas/` at the repo root are untracked staging.**
  `malware-analysis-agent.yml` regenerates them from
  `modules/domain-apps/cyber/agent/{skills,personas}/`, which is where edits belong.
- **Canonical multi-tenant key layout** is built in
  `modules/agent-factory/gateway/lambdas/ingest/handler.py::_build_upload_s3_key`:
  `o/<org>/t/<team>/u/<user>/s/<session>/<task>/in/<file>`, from Cognito claims. Ground
  tenant-scoping work on that function rather than on a pattern seen in an IAM policy.
- **No CI runs `modules/domain-apps/cyber/workers/tests/`.** These suites (including the new
  security ones) currently only run locally — flagged in the PR; wiring it is outside this
  package's scope.
- **libmagic is not installable in this sandbox** (no root, no distro package), so 7 triage
  tests fail on main and continue to. For a *security* test this matters: an authorization
  regression test must not be unrunnable because an unrelated analysis dependency is missing, so
  `test_dispatch_enforcement.py` stubs `magic` only when genuinely absent, only for the import,
  and removes it afterwards so other test files still bind the real library.
- **gVisor RuntimeClass and its nodegroup bind to `module.eks` (the main cluster), not the
  cyber cluster.** Setting `runtimeClassName` on these pods would make every job unschedulable.
  Reported as a platform-owned prerequisite rather than attempted here.
