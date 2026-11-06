# B01 Product API evidence entrypoint

The maintained `.github/workflows/gateway-live-tests.yml` workflow is the Product V01
live entrypoint. Its `workflow_call` or `workflow_dispatch` selects explicit
`product_cases` and the **full served source SHA** as `expected_revision` on the
approved protected target. Selected Product runs use the existing main-only
`dev` CLI evaluator GitHub environment and its scoped OIDC role from
`AWS_CLI_UPLIFT_EVAL_ROLE_ARN`; the workflow checks the assumed role's account
and name. Ordinary Gateway regression keeps its separate `adp-checks-dev`
checks identity and target configuration. `secrets: inherit` does not make
secrets in `dev` available in `adp-checks-dev`. The combined B01 selection is
`api_product_lifecycle,api_denial_oracle,api_recovery_cleanup`; partial selections
are diagnostic and cannot yield a complete B01 packet. The workflow runs only
on its reviewed default branch.

Selected runs require the private `CLI_UPLIFT_EVAL_BINDINGS_JSON` and
`CLI_UPLIFT_EVAL_GATEWAY_CATALOG` already used by the evaluator. The protected
preflight also requires the evaluator's credential-secret **name**, disposable
instance profile and durable state-bucket **name** as reviewed references.
Those names grant no credential access to Actions: the orchestrator must not
read the secret value. `PRODUCT_API_FIXTURES_JSON` containing bearer tokens is
an obsolete static fixture and is rejected before the selected Product step.
The driver creates a disposable workspace and fresh actors. Before Product writes,
it requires matched native runtime proof and readback of encrypted private
bootstrap and Product-owner session backups. A missing capability or mismatched binding still refuses
writes; merely selecting a case does not establish installed permissions.
The [native runtime proof contract](native-runtime-proof.md) describes the new
read-only process probe, signed deployment-release binding and exact rollout
prerequisites. Its supported parent is the matching Gateway deployment workflow;
a standalone dispatch cannot supply the required signed release by assertion.

The owned setup runs on the disposable evaluator instance, using its
existing narrowly scoped secret-read permission and the installed CLI to log in
freshly for each workflow attempt. It must create or verify two isolated test
tenants and distinct owner, foreign and revoked actor sessions without copying
tokens into command text, reports or public files. Actor tokens stay in private
instance-local files. The original bootstrap administrator's and Product owner's
access/refresh tokens also cross the protected SSM response channel into orchestrator memory and the
existing KMS-encrypted, bucket-owner-only evaluator store. They are never included
in the run document or uploaded artifacts. Use a stable attempt ID (workflow run ID plus attempt
number) through setup, resumed execution, and cleanup. Persist the owned intent,
original write key, actor and tenant provenance and every attempt before writes;
resuming a lost response must reconcile the original operation, not create a
second fixture. Recovery deliberately interrupts the original create response,
reopens the private ownership store, and retries the same key through the lifecycle
driver. It checks the two expected history revisions and compares the other tenant's
Product inventory and profiles before and after retirement. An incomplete paginated
inventory refuses the check rather than silently omitting shared assets.
The protected cleanup caller
must invoke guarded retirement for only the manifest-owned Product, check
current and historical API absence and shared-asset preservation, and retain
audit history. Interrupted cleanup must resume from the durable manifest and
refuse unknown ownership rather than mark the run clean. The independent recovery
job restores the original administrator session, renews an expired access token
through the existing CLI refresh API, and checks the actor and tenant again. It
stops the original evaluator instance and verifies termination before invoking
Product retirement with the distinct recovery role. Failed API cleanup preserves
the bootstrap account and login resources for retry; successful cleanup clears
the private session backup. An expired or revoked refresh token, lost refresh
response, unavailable store or failed ownership check remains incomplete rather
than authorizing a substitute actor.

If cancellation occurred before the create receipt existed, recovery first tries
the unchanged protected retirement contract. On an ownership conflict it rechecks
the served source, restores the original Product owner and durably records a retry
of the original create request and key. The public API resolves that request once,
then the same protected retirement path removes the owned fixture. A different
actor, target, source, body or key cannot substitute for the original intent.

The scoped evaluator role must be verified for this exact target and the required
read-only observation and run-owned state/cleanup actions; do not substitute the
Gateway checks role or grant broad IAM or Kubernetes execution privileges. The
selected driver writes
`test-results/product-v01-dispatch.json`
with selected cases, run ID and observed source revision, but not actor tokens.
An observation denial, stale source or missing authorized run-owned cleanup
leaves dispatch incomplete and the write refused. This report **does not**
claim independent schema, serving-pod flags or installed CLI identity on its own.

The existing private evidence-upload step retains `test-results/product-b01.json`
after the packet producer runs, even if an earlier selected step failed. The
producer requires `test-results/product-b01-inputs.json` in the same protected
workspace, containing the source/requirements/workflow revisions, deployment
receipt, independently observed schema/image/CLI/pod capabilities, per-case
attempt and cleanup receipts, and V03's installed-CLI report references. The
V01 driver does not invent those observations or provision that peer artifact;
the assigned verification owner must verify them against the actual reviewed
deployment and peer runs. A static actor-token secret is not a substitute for
protected fresh-actor setup. Missing input, mismatched run,
target or revision, skipped cases, unknown observation or failed cleanup writes
an **incomplete** packet and fails
the workflow. The packet's content hash identifies the exact revision for one
later human B01 decision; a successful code-only run does not establish acceptance.

The protected observer must verify the served source, schema, image digest,
per-serving-pod core flags and installed CLI identity. If its read capability is
unavailable, stop and retain an incomplete packet; do not infer access from a
separate connection or grant broad pod execution or wildcard cloud permissions.
The internal retirement route retains immutable audit history and is restricted
to the configured evaluator and independent recovery identities. It is not a
public deletion/purge route. No new IAM, registry or cluster permissions are
implied by this implementation. Maintained deployment qualification must verify
the existing exact-role grants and private-store access before live execution.

Each supported case can be selected alone or in canonical lifecycle/denial/recovery
order. Denial and recovery selections create their own run-owned lifecycle fixture
as a prerequisite; they report only the requested cases. The complete B01 packet
still requires all three cases, the installed V03 report and D01 release evidence.
Offline checks exercise the actual Product routes and protected retirement with
local database and identity fixtures; they do not establish live acceptance.

## D01 release grading and compatible recovery

The native operations dispatcher selects the exact combined `product_cases`
inventory above on the maintained Gateway deployment. That accepted selection
also selects compatible recovery on reviewed main in the protected dev scope;
there is no separate recovery-enable input or per-release setup gate. Ordinary
deployments with `product_cases=none` do not run it. The dispatcher supplies only
the admitted selection and `adp_correlation`, `adp_source_revision`, and
`adp_definition_revision`; arbitrary workflow inputs remain rejected. Matching-run
adoption checks those same reviewed inputs. This is not authority for a different
target or an arbitrary rollback artifact.

The preliminary grader succeeds only when C01/C02 pass against the independently
observed signed release and installed CLI. Its report still says `incomplete`,
with C03/C04 `NOT_RUN`. After independent owned cleanup, a private binding makes
that exact source, workflow revision, run attempt, image and target eligible for
the separate held fixture. The callable recovery workflow requalifies the signed
baseline, immutable source tags, reviewed ancestry floors and actual schema before
any switch. It quiesces Product admissions, switches the compatible image, verifies
the original Product/history/idempotency receipt, then restores the candidate and
verifies idempotency replay. Unsupported onboarding stays disabled; no down
migration or database mutation shortcut is used.

The maintained restoration job makes at most three attempts with five-second
delays, within its 30-minute job bound. Independent readback jobs are bounded to
20 minutes; the final cleanup job to 35 minutes. A failed intermediate switch or
readback still enters the restoration path after a retained restore intent, and
owned cleanup runs regardless. Restoration or cleanup failure fails the workflow;
compensating restoration alone cannot turn an interrupted rollback into a pass.
Do not start concurrent out-of-band recovery against the held owner.

Final grading consumes the original private C01/C02 report, ordered quiesced,
baseline and restored-candidate observations, the exact owned retirement receipt,
and the completed cleanup manifest. Only that complete same-attempt chain makes
C03/C04 pass. Missing, stale, wrong-target or failed evidence exits nonzero. The
required final step runs even after cleanup failure and retains an incomplete
result rather than suppressing the failure.

The private state prefix is `cli-uplift-eval/state/product-runs/<run-id>/`, where
`<run-id>` is `product-<workflow-run-id>-<attempt>`. It retains
`preliminary-grades.json`, `accepted-release.json`, `recovery.json` (including
successful owned cleanup), and `final-grades.json`. The final report is also
published through the existing private evidence uploader as artifact
`product-d01-final`, file `test-results/product-d01-final.json`. Owner sessions
and raw recovery state are not uploaded. Local synthetic composition tests prove
the grading and refusal paths, not live rollback acceptance; actual qualification
remains with the post-merge deployment owner.

## One human B01 decision

After the assigned verification owner has published the **actual** combined
V01/V03 packet and D01 deployment receipt, give the B01 human reviewer the
private artifact reference, its `packet_revision` content hash and its
`plan_revision`. The reviewer checks that the exact packet covers AC-01, AC-02,
AC-03 and AC-04 with expected versus actual results, every attempt, installed
CLI/API evidence, release identities and verified run-owned cleanup. The owner
records **one** accept or reject decision in ADP against that exact packet and
plan revision, and links the decision on the existing B01 verification stories.
The packet's `pass` status means agent checks are complete; it is **not** a
human acceptance decision, and the evidence producer never sets one.

If any required evidence or cleanup is incomplete, leave B01 unaccepted; the
assigned owner repairs or reruns the affected checks and publishes a new packet
revision, retaining earlier attempts and the rejected revision. A reviewer
rerun is optional. This instruction is a future handoff, not a record of a
human decision or a claim that Product acceptance has run on a deployed target.
