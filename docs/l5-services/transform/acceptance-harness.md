# Transform acceptance collection

The release driver evaluates the original criteria in
[`acceptance-catalog.json`](acceptance-catalog.json): eight epic criteria,
29 story criteria, 29 scenarios and 15 live checks. The catalog retains the
accepted backlog revision. A successful installed deployment or native job does
not by itself complete this catalog.

`qualify-transform-release.py` and `emit-transform-qualification.py` use the same
collector and evaluator. The release context must match the protected workflow
identity and exact source revision. A private evidence directory holds an
exclusive, append-only journal bound to that context, the original fixture hash
and catalog revision. A restart re-reads evidence for the original selected
results; a changed fixture or workflow attempt cannot reuse the journal.
Each emitted report is retained separately. `qualification.json` is the current
report for the trusted publisher, not an input that can supply pass flags.
Before staging metadata, the publisher reopens the private journal under its
lock and checks the latest preserved report bytes and every claimed collector
receipt. A report document without that journal cannot establish acceptance.

The optional fixture `release_acceptance.installed` contains the existing shared
installed collector's `selection` and `release` pins. It cannot contain verdicts,
collector names, executable code, exported UI facts or manual acceptance flags.
The installed collector records a baseline before original native intent, then
checks the installation after each selected case. An absent baseline cannot be
recreated retrospectively for retained work.

## Installed readbacks

| Observation | Actual reader | Acceptance limit |
| --- | --- | --- |
| Installed artifacts and runtime revisions | Shared manifest, archive, Lambda, Gateway, frontend and CLI collectors | Before/after checkpoints prove unchanged observed artifacts; they do not prove prior-schema compatibility or deployment history. |
| Native refactor and dependency upgrade | Original Product source, native checkpoint, patch reconstruction and detached-check evaluator | Two distinct selected projects, original sources, native sessions and patches are required for EC-03. |
| Durable recovery and publication progress | Scoped SQL run attempts, work rows and result publication receipts | Counters are reported as counters. They cannot prove injected faults, exactly one native effect or later consumer use. |
| Owned scratch absence | Actual installed worker configuration, exact EKS cluster and metadata-only reads for checkpoint-owned Pod, ConfigMap and Secret identities | Reads do not delete resources or fetch secret/configuration contents. A present resource, wrong namespace or unavailable metadata read remains incomplete. |
| Retention after cleanup | Current Product artifact access/content plus independent versioned S3 bytes, followed by the pinned result revision | Both selected cases must retain the original patch and every before/after test log after owned resources are absent. This can satisfy `retained-results`, while full cleanup remains incomplete without adopted-resource and SCM observations. |
| Portability progress | Verified case source references and test policy, paired with installed checkpoints | Repository identity and deployment continuity remain separately required. Unmeasured cost is `unknown`, never zero. |

The ordinary offline scenario validators remain useful regression tests. Their
`live_qualified: false` results cannot be promoted into live acceptance by wrapping
them in a reader interface. An API-shaped UI payload or an arbitrary Python
adapter cannot substitute for an authenticated human observation.

## Original evidence still required

The report retains every original incomplete row and gives an action for each
unobserved epic. These are implementation or observation tasks, not flags an
operator can override:

* EC-01 requires compatible schema history, a current qualified provider/executor
  and both ready and missing-input Product configuration observations.
* EC-02 requires observed hostile/foreign source and credential denials plus the
  installed isolation controls.
* EC-04 requires the bounded original lost-ack, restart, replay, stale-result and
  cancellation trace, with independently sampled native-effect identity/counts.
  Existing attempt and work snapshots are insufficient; they cannot reconstruct
  faults that were never recorded.
* EC-05 additionally requires independent adopted-resource preservation and SCM
  before/after observations. A Kubernetes absence receipt proves only the exact
  recorded scratch resources.
* EC-06 requires the actual UI, CLI and MCP journeys, a recurring occurrence and
  separate SCM authorization. UI/SSO observations must come from a human; this
  driver performs no browser or console automation. The current driver has no
  receipt-upload or external fact-supply bypass.
* EC-07 requires consumer acknowledgements, a later entitled task using the
  contribution, and foreign/revoked reuse denials.
* EC-08 requires two independently identified non-ADP repositories with different
  test configurations, continuous deployment evidence, and complete retention,
  cleanup and actual/unknown cost observations on the same artifacts.

Per-case read failures preserve successful native verification and continue
collecting the other original case. Safe actions identify the failing collection
stage. Raw provider responses and credentials are not copied into public report
diagnostics. No failed read authorizes replacement native work.

## Installed executor readback enforcement

The actual provider and check controls construct and verify Pods from one
source-owned manifest shape. On initial read, restart and after reading logs,
they verify the immutable command and image, exact allowed identity sources,
input projections and mounts, node, security contexts, resource bounds and
deadline. Provider deadline bounds come from the current durable Work admission,
not a Pod annotation or caller-supplied Task identity. Ordinary Kubernetes
defaults are recognized explicitly; extra containers, execution hooks, host
access, credentials or security overrides prevent results from being retained.
Diagnostics name only the affected source-owned field, never untrusted values.
The exact owned Pod UID can still be stopped if its isolation shape has drifted.

The initial provider response must match the exact launch request, including
its projected part count and credential-bounded deadline. A resumed provider
read verifies ordered owned projection names, a bounded part count and the
current admitted deadline bound. Original launch count and deadline are not
separately retained for recovery; resumed readback alone cannot prove those
exact original values.

These runtime checks and offline mutation regressions do not establish live
executor qualification or mark TF01 AC-02 complete. An admission controller can
mutate a Pod before the first readback; installed admission and node isolation
controls remain necessary to prevent such execution. The controls refuse to
accept evidence from that changed workload.

## Credential-free executor component observations

An original private fixture may opt in with
`release_acceptance.executor_isolation: true` alongside its existing installed
selection. The driver reads the protected infrastructure configuration and the
actual installed worker and EKS cluster before creating any probe. Worker
revision, source artifact, region, role, node UID, namespaces and immutable image
references must agree. The caller must supply `L5_INFRA_CONFIG_JSON` from the
protected infrastructure configuration and use the existing scoped identity.
The accepted release workflow currently exposes that secret to its infrastructure
step, but not to native run/emission steps. Its definition remains unchanged;
this component therefore remains an opt-in driver capability pending an
authorized protected configuration path. The fixture cannot supply or override
the infrastructure configuration. Missing configuration or Kubernetes authority
is an incomplete collection; the collector
does not add access entries, RBAC, policies or permissions.

For a separately authorized protected execution context, the component-only
entry point is `qualification_transform_isolation.py run SOURCE_SHA FIXTURE
EVIDENCE_DIRECTORY`; `emit` only revalidates retained observations. It enforces
the same original release identity, engine authorization and private journal.
It does not call the native run driver, change the conventional qualification
report, or enable consumers. A successful command means component observations
were collected, while the original criterion remains incomplete.

The source-owned program runs in the two installed image digests, each in a
credential-free Pod in the installed deny-all **check namespace**. It observes
credential absence, read-only root, capabilities, seccomp, metadata and private
endpoint denial, and the installed provider library's filesystem/write/shell
boundary. The collector independently reaches the private EKS TLS endpoint
before and after the Pod probes. A refused connection is inconclusive rather
than an isolation pass. Pod UID, actual image digest, immutable command/source
hash, original output and termination evidence remain in the private journal.

Cleanup removes only the original Pod UID after termination was observed. A
lost create response resumes observation of the same named Pod; an absent
possibly-executed Pod never authorizes replacement. Emission verifies retained
original output, current installed pins and original Pod absence without
launching another probe. Failed or unknown probes retain their evidence.

These are **partial TF01 AC-02 / SCN-02 observations**. They do not mark that
story criterion, its scenario, EC-02, or a live check passed. Both images running
credential-free in the check namespace does not qualify a credential-bearing
provider operation. Completion still requires the actual repository source
projection and admitted check command, actual provider tool mediation,
independent cross-Pod controls, installed Task-identity rejection and the
unqualified bridge's admission denial. The broader EC-02 additionally requires
the owner/foreign Product, source and real broker controls. The broker's positive
credential-session path needs current L5 work authority; a canary cannot replace
that dependency.
