# Installation-owned provider authority

Refs #7135. Source implementation is separate from infrastructure review,
activation, image qualification and live demo acceptance.

Personal AWS connections continue to reject the platform account. The distinct
`spda1:<canonical-lowercase-UUIDv5>` handle names a protected installation-owned
record, never a `UserCredential`. Reserved-prefix aliases, missing records,
revocation and store outages never fall through to personal delivery. Generic
vault resolution cannot return these handles, even if a colliding row exists.

The protected deployment owner records exact installation, domain/ADP organization,
future workspace and create-request IDs; beneficiary subject, canonical user and
membership selectors; provider ARN/RoleId and policy generation; child boundary
identity; and Secret ARN/VersionId. Workspace identity uses the existing UUIDv5
derivation from the actual create request. Enrollment does not create a workspace,
approval or live identity proof. Gateway establishes current Cognito identity and
SQL membership on every consumption. The record expires and can be conditionally
revoked; revoked identities cannot be silently restored or re-enrolled.

Account admission constructs the existing domain account request from owner-held
material and redacts the role/ExternalId response. Evidence reports installation
ownership and delegation to exactly one workspace through the existing contract.
The refresh-only validation endpoint accepts no caller readings or profile. Its
fixed profile drives EC2 DryRun and quota observations; physical capacity remains
unknown and the report does not prove all native Terraform permissions.

Validation observations are immutable rows in a separate table. One DynamoDB
transaction checks the current authority revision and writes both digest-indexed
and current-generation observations. Gateway can condition/read authority but
cannot mutate it. Reports are bounded to six hours and the authority expiry;
revocation, secret rotation and generation changes invalidate their use.

The existing paid-operation broker alone releases AWS sessions. It retains sealed
target, live worker/run/pod identity, lease/fence, approval deadline and cleanup
restrictions. It re-establishes governed authority around STS, provider reads and
audit commit, checks the returned immutable RoleId and limits sessions to 900
seconds. Revocation cannot revoke an already-issued STS session before its expiry.

All new storage and Gateway IAM definitions live in the app-owned
`infra/provider-authority` child module. The existing webhook Terraform root only
composes it and publishes configuration through its maintained rollout. Enrollment
is a separate conditional owner command after the actual installation/binding is
prepared; storage existence is not enrollment or worker readiness. Tables and
authority history are retained and protected from deletion.

The provider-role policy and mandatory child-role boundary belong to the separate
app-owned provider root. Generated workspace IAM roles need explicit boundary
support in the packaged workspace module and lifecycle configuration. Both API
and worker therefore require genuine new builds and qualification. IAM controls
combine with sealed reviewed native source/plan checks; EKS creation and service
linked role effects cannot be represented as complete IAM-only VPC isolation.
No source test or empty-default infrastructure composition establishes live demo
readiness.

## Policy identity and operator procedure

AWS's inline-role aggregate policy limit cannot fit the fully scoped native
policy. The admitted recipe has exactly four same-account app-owned managed
policies (`network`, `identity`, `lifecycle`, `state-validation`) under the exact
provider role name, with no inline policy or provider boundary. Gateway hashes
the role trust and each policy ARN, current DefaultVersionId and canonical policy
document. Missing, extra or changed attachments/versions refuse. The child-role
boundary ARN and version/document digest are pinned independently. These digests
identify the reviewed policies; they do not replace review of their permissions.

The shared root's optional `domain_provider_authority` input composes storage and
exact Gateway grants. Its default is null. It publishes the verified account as
`ADP_DOMAIN_PROVIDER_ACCOUNT_ID`; the Bedrock service account setting does not
establish installation ownership. The configured public-route bucket must also
belong to this exact account. A real saved plan and owner authorization are
required before applying this module through the existing webhook state; any
active infrastructure deployment hold still applies. Do not create a second
state owner for the module's resources.

After that rollout, use the app command from a reviewed repository checkout:

```bash
PYTHONPATH=modules/domain-apps/superplane python -m installation.provider_authority --document "$PROVIDER_AUTHORITY_DOCUMENT"
```

The private, owner-readable JSON document has exactly `version: 1`, `environment`,
`operator_role_arn`, `operator_role_id`, `gateway_namespace`, `management_cluster`,
`registry_table` and `authority`. The last object uses the closed fields in
`modules/gateway/src/shared/domain_provider_contract.py`; generate its handle and
digests with that maintained contract, using actual independently read metadata.
Do not put SecretString, ExternalId or session credentials in this document.
Preserve the genuine create request and use its original operation ID when later
calling workspace preview/create. Preserve the exact reviewed enrollment document
for recovery and revocation.

The default command checks without writing. It verifies selected caller ARN and
RoleId, current provider/boundary generations, current Secret VersionId, the live
installation route, selected EKS endpoint/CA, the maintained Gateway ConfigMap and
the SSM-selected protected registry pair. It does not claim executable readiness
or validate the beneficiary selectors as a current human. Gateway performs that
separate proof at consumption. Following independent review, `--write` performs
only a conditional insert and exact readback. Retry a lost response with the same
document; differing records are refused rather than adopted.

`--revoke --write` withdraws only that exact original record with a conditional
generation change. It remains usable when the provider or installation has
broken, because withdrawal requires the protected owner and existing record,
not healthy runtime dependencies. Revocation is idempotent and irreversible
through this command. General rotation/re-enrollment of an existing handle remains unsupported. The
restricted owner binding-update procedure below supports reviewed runtime upgrades
without changing the beneficiary, provider scope or lifetime.

Tests use fake transports and Terraform mocks. They cover real API account-schema
and credential-evidence consumers, exact workspace delegation, current identity
and policy drift, conditional evidence writes, revocation during provider/audit
I/O, and a valid minted session near its deadline. No test result here claims
cloud provisioning, spend approval, deployed readiness or a working demo. The
existing approval contract also requires a second distinct, genuine human with
current authority; installing provider infrastructure cannot manufacture that
approval.

## Genuine user draft and enrollment sequence

Use the maintained browser flow for this scoped installation. The existing CLI
`workspace create` does not yet provide the region, reviewed-plan and approval
steps needed by this lifecycle path.

1. Sign in as the intended beneficiary and select the actual organization. In
   **Create a workspace**, enter the final name, managed mode, isolation, exact
   target account ID (or eventual registered account name), region and budget.
   Click **Review plan**. The browser's maintained `claimPreviewIdentity` stores
   the scoped draft and its operation UUID before calling preview. An absent
   account or authority may refuse preview; this does not erase the real draft.
2. Retain that real same-user browser receipt and request body. Inspect the
   persisted `create-workspace` receipt or the actual preview network request;
   the draft is not currently displayed as a receipt panel. Derive the future
   workspace UUID with the existing `workspace_id_for(org_id, operation_id)`
   contract. Do not insert an operation/workspace database row or manufacture an
   approval. This is a persisted user intent, not an accepted server operation.
3. The protected owner reviews the actual provider/storage plans and selected
   installation, then applies only through their maintained state owners. Build,
   qualify and install the API/worker versions that understand the child boundary.
   Configure the exact lifecycle target, opaque credential reference and boundary,
   and conditionally enroll the authority using the original draft operation ID
   and its derived workspace ID. Complete the owner checks described above.
4. In the beneficiary's genuine authenticated session, call
   `POST /superplane/v1/accounts` with `name`, `provider: "aws"`, `account_id` and
   `adp_credential_id` equal to the enrolled opaque handle. The Gateway establishes
   current Cognito and organization membership against the enrolled beneficiary,
   checks the installation and provider generations, and constructs the domain
   account from protected material. Caller-supplied role ARN or ExternalId is not
   accepted. A tenant administrator cannot substitute another beneficiary.
5. In that same genuine session, refresh validation with an empty-body
   `POST /superplane/v1/provider-authorities/{credential_id}/validation`.
   The server uses the enrolled profile; no client readings or profile are input.
6. Retry **Review plan** in the original browser with identical inputs. The draft
   UUID is reused. The API resolves the registered account by exact name/ID and
   matches its credential reference to the installed lifecycle policy; it does
   not look for this handle in the personal credential list. Changing draft
   inputs generates a different request UUID and therefore a different workspace;
   stop and review a new authority scope instead of reusing the old enrollment.
7. Review the returned plan and request approval. A second distinct, genuine
   currently authorized human must approve that exact plan. Only then submit
   **Create this workspace**, follow its durable operation and lifecycle proposal
   approvals, and verify live readiness. Enrollment, registration, successful
   preview and validation alone do not prove workspace creation or demo success.


## Restricted owner binding update

A reviewed runtime upgrade can change the installed binding digest, including its
qualified worker image allowlist. The protected owner may advance an existing
active authority using `--rotate-binding-from PREVIOUS_DOCUMENT` with the exact
new `--document`. Default invocation is read-only; `--write` is the final step
following review of both documents and the separate shared-owner deployment plan.

The new authority must preserve every field except `binding_sha256` and a
single-step `generation` increment. Both documents must select the same operator,
cluster, registry and namespace. Identity selectors, original request/workspace,
role and policy generations, secret version, validation profile and expiry cannot
change. Expired, revoked, absent or mismatched predecessors are refused. This is
not account re-enrollment, restoration, expiry extension or workspace approval.

Use the maintained installation flow to quiesce execution, deploy and verify the
reviewed runtime before this command. The owner command independently repeats its
normal live role/policy/secret, enabled installation route, exact current binding
and protected registry checks. It does not claim worker readiness or quiescence.
Gateway still establishes those runtime and human facts at consumption.

The write condition pins the complete previous authority revision. Concurrent
revocation or rotation fails instead of overwriting another record. If a response
is lost, replay the same old/new document pair; an exact committed successor is a
verified no-op. Preserve both private documents and review/readback evidence in
the existing versioned artifact store. Use the successor document for subsequent
revocation; revocation advances that actual generation and remains irreversible.
Generation changes invalidate prior provider validation evidence; the genuine
beneficiary must refresh it. Already-issued STS sessions retain their existing
maximum 900-second lifetime; the update does not revoke those credentials.

```bash
PYTHONPATH=modules/domain-apps/superplane python -m installation.provider_authority \
  --document "$NEW_PROVIDER_AUTHORITY_DOCUMENT" \
  --rotate-binding-from "$PREVIOUS_PROVIDER_AUTHORITY_DOCUMENT"
# After review, repeat the exact command with --write.
```


## Restricted session-tag trust repair

AWS supports `sts:ExternalId` for `sts:AssumeRole`, but not `sts:TagSession`
([AWS STS authorization reference](https://servicereference.us-east-1.amazonaws.com/v1/sts/sts.json)).
The provider Terraform root uses separate statements. Both keep the same exact
Gateway principal, beneficiary and required operation/validator session tags;
only AssumeRole carries the unchanged ExternalId requirement.

For an already enrolled provider with the original combined statement, capture
the original private policy identity snapshot before applying a separately
reviewed saved plan through the existing app-owned provider state. The snapshot
has exactly `trust` (the role trust document) and `managed` (each of the four
policy ARNs mapped to its `version` and `document`), as hashed by `policy_identity`.
**It contains the ExternalId: mode 0600, never commit, print or archive it.**
Preserve all existing authority documents and immutable deployment inputs.

Prepare a successor authority changing only `policy_sha256` and incrementing
`generation` once. The reviewed plan must only split the original trust, with no
role replacement, grant/attachment/boundary/secret changes. During the gap between
trust apply and enrollment advancement, Gateway rejects the old policy digest.
This procedure does not claim to revoke already-issued sessions (maximum 900s).

After Terraform apply, run the maintained owner command read-only:

```bash
PYTHONPATH=modules/domain-apps/superplane python -m installation.provider_authority \
  --document "$SUCCESSOR_AUTHORITY_DOCUMENT" \
  --repair-session-tag-trust-from "$PREVIOUS_AUTHORITY_DOCUMENT" \
  --previous-policy-document "$PRIVATE_ORIGINAL_POLICY_SNAPSHOT"
# Following review, repeat the exact command with --write.
```

The predecessor's enrolled hash authenticates the full original snapshot. The
command only accepts the original closed trust recipe and computes its exact
split, preserving all tag restrictions, principal and managed policy versions.
Its successor digest must match the live provider. All ordinary operator,
installation, registry, secret, binding and boundary checks still apply. The
conditional write refuses absent, revoked, expired or concurrently changed
predecessors; lost responses reconcile only the exact successor. General policy
rotation, re-enrollment and expiry extension remain unsupported. The genuine
beneficiary must refresh provider validation after advancement. This repair
creates no human approval, workspace or operation authority.


## Restricted beneficiary-tag repair

Set Terraform `beneficiary_user_id` to the Gateway internal user ID returned by
the authenticated identity, not the Cognito login subject. Gateway tags provider
sessions with that enrolled internal `user_id`. A provider
role mistakenly provisioned with that same beneficiary's login `subject` can be
corrected through `--repair-beneficiary-tag-from PREVIOUS_DOCUMENT`, together
with `--document SUCCESSOR_DOCUMENT` and `--previous-policy-document SNAPSHOT`.
The snapshot has the same private format and handling requirements as the trust
repair above. Capture it before applying the separately reviewed saved Terraform
plan through the existing provider state, while execution is quiesced.

The plan must change only `aws:RequestTag/adp:user_id` in the existing AssumeRole
and TagSession statements, from the enrolled subject to the enrolled internal
user ID. The owner command authenticates the complete previous policy snapshot,
requires the exact split trust recipe, and verifies the resulting live digest.
It preserves the principal, ExternalId, other tags, grants, role ID, secret,
boundary, beneficiary identity, request, workspace and expiry. The successor
changes only `policy_sha256` and increments `generation` once.

Run the command read-only first, review the result, then repeat with `--write`.
The same conditional predecessor and idempotent retry checks apply. This is not
a general policy update or a change of beneficiary. Refresh normal authenticated
provider validation after advancement; the repair grants no workspace approval.

## Restricted native network read repair

The pinned Terraform AWS provider reads EIP address attributes and EC2 prefix
lists while reading newly created EIPs and interface endpoints. Existing enrolled
providers missing those reads can use `--repair-network-reads-from
PREVIOUS_DOCUMENT`, with `--previous-policy-document PRIVATE_SNAPSHOT`,
`--repaired-policy-version VERSION` and `--document SUCCESSOR_DOCUMENT`.

Capture the complete enrolled policy snapshot privately before applying a reviewed
saved Terraform plan to the existing provider state under installation maintenance.
That plan must add only `ec2:DescribeAddressesAttribute` and
`ec2:DescribePrefixLists` to the existing region-scoped read statement in the
`state-validation` shard. Record its actual new managed-policy version. Keep the
role trust, other shards and child boundary unchanged.

The owner entrypoint authenticates the old snapshot, derives this exact change,
and verifies the complete live policy digest. The successor increments the
generation and changes only `policy_sha256`. All ordinary operator, installation,
identity, secret, expiry, binding and conditional-write checks remain required.
Review the read-only result before repeating with `--write`. This does not resume
any operation or approve a replacement workspace plan.

## Restricted EKS nodegroup service-role read repair

`CreateNodegroup` checks whether the account's existing
`AWSServiceRoleForAmazonEKSNodegroup` is present. The native provider must be able
to `iam:GetRole` on its exact `eks-nodegroup.amazonaws.com` service-role ARN.
This grants no service-role creation, modification or deletion.

For an enrolled provider missing that read, capture its complete pinned policy
snapshot before applying the reviewed Terraform plan. The plan must append only
that exact allow statement to the `identity` shard, preserving the role trust,
other shards and child boundary. Use `--repair-nodegroup-role-read-from
PREVIOUS_DOCUMENT`, `--previous-policy-document PRIVATE_SNAPSHOT`,
`--repaired-policy-version VERSION` and `--document SUCCESSOR_DOCUMENT`.
The owner validates the previous digest and derives the exact successor policy;
only its digest and generation may advance. Review before repeating with `--write`.

Refresh normal authenticated provider validation after advancement. Existing
validation evidence is generation-bound. An uncertain apply still requires the
maintained partial-apply inspection, new repair plan and exact-plan approval;
this policy repair grants no permission to replay a failed saved plan.

`--repair-inventory-read-from PREVIOUS_DOCUMENT` with the authenticated `--previous-policy-document` and exact `--repaired-policy-version` permits only regional `tag:GetResources` on the existing read statement. Preserve all other fields and revalidate the new provider generation before execution resumes.
