# Security worker runtime

The installed Security worker uses `l5_services.runtime:factory`. The Security
L5 infrastructure root supplies the selected regional API Gateway stage,
RDS IAM database identity, versioned private archive bucket and owned work
queue. It registers the worker's exact IAM role as the internal `l5-worker`
service, with only `l5:credential-session` as its credential capability. The
repair function uses the same installed assembly but only has database and
queue permissions beyond logging and VPC networking; it cannot invoke the Gateway or access source archives.

The worker signs internal Product and credential-broker requests with its
Lambda execution role. That role is **transport authority only**: it does not
have Security Agent permissions or read provider secrets. Before source
materialization, the worker checks each pinned receipt through the current
Product API under its claimed work lease. Before each provider step, it asks
the broker for a selected-connection session using the run's durable grant,
work ID, lease owner and fence. The broker checks current Product rights and
bounds the session policy by the work lease, even though STS issues a longer
underlying session. Missing registration, lease, binding, readback or broker
response stops the provider step. A queue message never grants authority.

For setup, the broker checks the persisted intent and operation, the selected
configuration, current parent grant and Product rights, and the setup work's
lease owner and fence. It does not bind the reusable parent grant to one setup
attempt. The installed binding adapter requests a scoped session and rechecks
the current work before each native setup call; a revoked grant or replaced
fence cannot authorize another effect. The repair sweep re-enqueues owed setup
and run work from SQL rather than inferring authority from a queue delivery.

The worker uses an IAM-authenticated, TLS-verified RDS connection and places
sealed source archives under an owned prefix in the private, versioned release
bucket. It verifies the object version and digest on read. The regional API
Gateway URL uses a NAT path; an execute-api interface endpoint must not replace
that public regional route. The deployed environment must also permit the
worker's actual source IP through any configured internal-route source filter.
Neither offline fixtures nor artifact publication establish that network
readback or a live provider result.

To check code changes locally, run the Gateway mounted Security fixture, the
L5 runtime and deployment tests, and the mocked Terraform scope test. These
checks substitute offline STS, native provider, storage and Gateway transport
boundaries; installation and live qualification remain separate release work.

## Full repository code scanning

Security code scanning captures the complete selected Git commit into one ZIP.
Saved exclusions remain explicit; the capture never drops files to fit a limit.
ADP permits up to 1 GiB and 100,000 files, subject to complete Git-tree metadata,
archive overhead and the original assessment deadline. These are ADP limits,
not a published provider quota. Threat modeling retains its separate pinned
architecture-document input and 6 MiB boundary.

Product admission first records the immutable request and its authorization.
Repository capture runs as durable Product work, with a database lease, at most
three bounded capture attempts and no deadline extension. Gateway replicas
recover interrupted work. The original submission key is reused to complete
admission after capture, including when the initiating page has closed. HTTP
`503` with `source_capture_preparing` and `Retry-After` indicates preparation;
clients retain the same input and key. It does not indicate native failure.
Scheduled preparation must have begun in the original five-minute arrival
window and still satisfy current consent and the original deadline.

Capture checks every path, size and Git blob hash against the pinned tree.
Archives and file manifests are written to private temporary files and immutable
versioned storage. Worker transfers use authenticated ranges of at most 1 MiB,
with authorization rechecked for each range and the final digest verified. Code
scanning verifies and packages files on disk instead of retaining all source
contents in memory. Native staging uses a conditional write and byte readback;
a conflicting object is rejected.

The Security worker has a 900-second timeout, 1,536 MiB memory and 4,096 MiB
temporary storage; its queue visibility is 5,400 seconds. Materialization and
submission hold a 960-second fenced work lease and finish within an 840-second
application bound. The separate repair worker keeps its ordinary resource
limits. Install the reviewed Security infrastructure changes before qualifying
large scans; publishing code alone does not update these resources.

Complete submitted snapshot coverage is distinct from provider-reported
analysis coverage. An upload, accepted native job or green local test does not
prove every file was analyzed. Retain provider results and coverage evidence
from the authorized qualification run. Native validation and remediation remain
disabled for code review.

## Collecting an assessment after its deadline

An expired ADP run does not prove that AWS stopped the assessment. Reconcile an
existing Security run to open a 15-minute observation window for its saved native
job. This preserves the original repository snapshot, configuration, provider
attempt and execution deadline. An active worker lease prevents takeover;
undelivered observation work is fenced before recovery is queued.

Recovery requires current owner consent, parent delegation, source access,
configuration and binding generation. Its broker session permits only job,
task and finding reads in the admitted agent space. It cannot start or stop a
job, upload source, update a model or pass a role. Expired source bytes are not
reread: findings are checked against the sealed manifest. AWS checkout paths
are mapped to repository-relative paths only for the known checkout prefix and
files present in that manifest; the original native locations remain in the
retained provider result.

ADP refreshes progress while recovery is pending. Verified completion and result
collection move the existing run to succeeded. If the window closes or access is
unavailable, the run retains an actionable recovery message. Reconciliation
never submits a replacement assessment.
