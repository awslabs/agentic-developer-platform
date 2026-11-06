# Explicit owner execution delegation

Saving L5 configuration does not authorize a worker to use the selected AWS
connection. The configuration owner can explicitly grant a bounded execution
ceiling through the Product service API. This fills the missing parent grant
used by setup admission and by per-run child grants.

The path prefix is
`/v1/products/{product_id}/projects/{project_id}/services/{service_id}`.
Supported services are `security`, `devops`, `finops`, `transform` and
`well_architected`. The server derives each registered worker identity; callers
cannot choose a service principal, operations, sources or capabilities.

1. Read `GET /delegation-scope`. Review its exact Product, Project, connection
   version, capabilities, sources, visibility and binding generation. For an
   existing binding supply `binding_id`. Add `for_control=true` only when
   authorizing its next control generation. Initial setup uses generation one.
2. Send `POST /delegations` with the returned configuration `ETag` as
   `If-Match`, a fresh `Idempotency-Key`, and a JSON body containing the returned
   `scope_digest` and an explicit timezone-qualified `expires_at`. Repeat
   `binding_id` and `for_control` from the preview when supplied. The expiry
   must be more than one minute and no more than 24 hours in the future.
   A disabled configuration can be previewed, but issuance returns
   `configuration_disabled` until its prerequisites and configuration are ready.
   Enable it, review the new scope and then authorize; the API never enables it.
3. Retain the returned delegation ID and revision. Repeating the same request
   and key returns the same active grant. `GET /delegations` reads the owner's
   retained grants. Existing setup/run APIs resolve the active parent; the
   caller does not insert its ID into a queue or impersonate the worker.
4. To withdraw or replace consent, send `DELETE /delegations/{delegation_id}`
   with the quoted delegation revision in `If-Match`. Revocation retains the
   record and invalidates child grants. Review a fresh scope and use a new key
   for replacement consent. An expired or revoked grant is never resurrected.

Issuance requires the verified human configuration owner to remain an actual
member of the Product team and own the selected, currently verified personal
AWS connection. Platform administrator visibility does not substitute for
membership or connection ownership. Product, Project, relationship, source
rights, connection version and configuration ETag are rechecked. Source or
connection loss and Product/Project archive do not prevent a still-authorized
owner from reading and revoking the grant.

Only one live parent grant per owner and Product service is issued. A second
consent with a different key returns `delegation_already_active` with instructions
to read/reuse or revoke the existing grant. Scope changes return
`delegation_scope_changed`; changed reuse of a key returns
`idempotency_conflict`. Every creation and revocation leaves an identity audit
record with the disclosed scope and expiry.

The grant itself retains the complete disclosed execution scope. Setup admission,
child derivation and the credential broker independently compare the current
configuration ID, revision and hash, connection version, provider account, role,
region and selected binding against it. Children retain the same scope and remain
dependent on their parent. Removing an audit receipt does not change authority.
An explicit binding selection cannot be used for a sibling binding. A control
grant permits its disclosed next generation; retries reuse that generation.

Any configuration edit requires fresh consent, including enabling a disabled
configuration. Complete the configuration and prerequisites first, then review
the scope and authorize it. Saving or enabling configuration alone cannot use an
older scoped grant to dispatch provider work. Existing schedules still require
normal work admission and a currently valid grant.

This action does not enable a disabled configuration, enable a schedule, create
setup intent, dispatch work, create native resources or return credentials.
Every setup/run action still passes its normal admission checks. The broker
requires the exact durable operation, attempt, worker lease and current Product
authority before delivering a short-lived provider session. Native installation,
permissions and qualification remain separate prerequisites. Revocation cannot
undo a provider action that already completed or retroactively revoke an STS
session already issued; it denies subsequent admission and credential refresh.

Signed DevOps incidents derive an idempotent run child from the current owner's
grant. The run keeps the authenticated event source separately from its consent
owner. The broker requires the matching durable admitted event, run, source and
configuration before issuing a session. An event supplies no owner identity and
cannot create a parent grant. Replaying an admitted event returns the existing
run; it does not dispatch it again.

## Deployment and compatibility

Apply Gateway migration `173_l5_execution_scope` before rolling out Gateway or
worker code that reads the mapped delegation model. The migration adds nullable
scope data, preserves existing records and does not issue grants, enable services,
or change schedules. Roll out admission and broker enforcement together before
using the new owner API. Do not deploy an intermediate owner API version that
records scope only in an audit receipt.

Pre-existing grants with no retained scope keep their original authorization
mechanism; they are not retrospectively represented as owner consent. Operators
must revoke and replace legacy authority deliberately if migrating it to this
API. Keep disabled installations and schedules disabled until their independent
acceptance prerequisites are met. A new API grant never falls back to a legacy
grant because its configuration changed or its audit record was removed.

## Separate consent for owned cleanup

Security and Well-Architected support a separate `owned_cleanup` purpose. This
allows a current owner to clean an exact ADP-created resource after disabling
execution or revoking its content sources. It grants no content access. Other
services keep their existing setup limitations; this does not install missing
cleanup drivers.

Read `/delegation-scope` with the exact `binding_id`, `for_control=true` and
`control_action=cleanup`. Review the disclosed native reference, provider identity,
original durable create receipt and next binding generation. Revoke any previous
live grant, then send `/delegations` with those same three fields, the returned
scope digest, configuration ETag and explicit expiry. This records consent only.
To request cleanup, use the binding operation API with `action=cleanup`, the
current configuration revision and binding ETag, and `owned_cleanup_confirmed=true`.

`GET /bindings/{binding_id}/cleanup-status` returns only owned-resource metadata,
current state and the binding ETag needed for cleanup. The exact cleanup operation
can be polled through `GET /binding-operations/{operation_id}`. These reads require
the actual current owner and team membership, but remain available after content
revocation, connection expiry or cleanup-consent expiry/revocation, including
failed and completed cleanup. They do not restore source access or authorize
provider actions; ordinary binding lists and content-bearing binding reads retain
their usual source checks.

Cleanup consent has only `binding:cleanup` and `credential:session`, no execution
capabilities, and only the selected credential reference. General Product reads,
source reads/pins, run admission, child derivation, create, update and resume
cannot use it. A new control generation or configuration change requires new
consent. Changed owner membership, connection version, native reference, original
receipt or provider identity denies the operation. Adopted resources cannot use
this path. The actual current human owner and currently verified personal AWS
connection remain mandatory; administrator visibility is insufficient.

The setup broker checks the exact cleanup intent, proof hash, lease owner/fence
and deadline before issuing bounded credentials and again after provider calls.
Security receives only exact-space read/delete and bounded region list reads.
Well-Architected receives only exact-Profile read/delete, context metadata and
region list reads. Neither policy grants source content access, IAM role passing,
creation, updates or new generation starts. Well-Architected retains the original
Profile identity without treating it as current source-role qualification; its
cleanup-only driver still requires the retained exclusive claim, no unresolved
or active runs/shared references, matching native ownership/snapshot, disabled
deletion protection and independent absence readback. Uncertain cleanup remains
unresolved; an attempted deletion is not reported as proven cleanup.

Deploy Gateway admission/broker enforcement and the corresponding service worker
together before using cleanup consent. This uses the retained scope column from
`173_l5_execution_scope` and requires no additional migration. No live grant,
native deletion or fixture execution is performed by deployment.

## Transform credential policy

Transform setup permits linking the exact selected definition and grants only
`transform-custom:GetTransformationPackageUrl` for its package ARN. Work adds
`transform-custom:ExecuteTransformation` on that same package and
`transform-custom:ConverseStream`. AWS documents no resource type for
`ConverseStream`, so that single action uses `Resource: "*"`. Every statement
is bounded by the selected region and the active work lease expiry. See the
[AWS Transform custom service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_transform-custom.html).

The broker checks pinned definition revision, qualified executor, the expected
NDJSON CLI interface and binary digest, source visibility, snapshots, and the
durable `transform_provider_submit` attempt. The session grants no source-store,
SCM, IAM or executor management permissions. These checks do not install a
Transform setup worker or prove native provider qualification; those remain
separate prerequisites.
