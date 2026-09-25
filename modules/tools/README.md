# Shared tool service infrastructure

`adp_tools` contains domain-independent building blocks for separately deployed
services behind the platform API Gateway. Domain tools and their deployment
packages belong to their application, for example
`modules/domain-apps/cyber/tools/`.

The shared library provides:

- Strict Task attempt and verified identity contracts.
- Validation of API Gateway's IAM caller context against configured worker roles.
- IAM-signed calls to the platform's generic Task authorization and artifact APIs,
  forwarding workload proof and the opaque Task run credential.
- Verification of returned Task/attempt/principal bindings and deterministic
  artifact receipts.
- Domain-owned DynamoDB operation storage with monotonic Task-version mirrors.

It does not authenticate a client-supplied tenant header or decode a run token as
proof. The platform validates the actual workload, run and current attempt, then
resolves the original client principal from the stored Task owner. The tools
service never receives the external client's OAuth secret.

## Permission model

Administrators set `allowed_tools` on the canonical principal's Task service
policy. Tool names are exact `domain.operation` strings. No wildcard grants are
supported. `ADP_TASK_PERSONA_TOOLS` maps persona names to lists of permitted tools.
Admission freezes the intersection into the Task's protected `tool_grants`.

Every start/use authorization requires membership in all three sets:

1. The principal's current `allowed_tools`.
2. The Task's immutable `tool_grants`.
3. The persona's current configured tools.

Removing a live permission stops subsequent tool use. Adding a permission does
not expand an existing Task; submit a new Task to receive a new grant. Legacy
policies without `allowed_tools` grant no tools and continue supporting model-only
Tasks. Submit bodies cannot supply tool grants.

Cleanup is a narrow exception: a verified Task settlement identity may invoke
`<domain>.cancel_jobs` for that Task after expiry, cancellation or permission
revocation, including when it never received any tools. The service must restrict
cleanup to that Task's existing jobs; this exception cannot authorize new work.

The initial transport supports Task-worker calls, not direct OAuth calls to tool
endpoints. The permissions originate from the client principal behind the Task.
Direct client tool invocation would need its own authenticated admission contract.

## Deployment contract

API Gateway enforces AWS_IAM on the domain route; its Lambda invocation policy
must restrict the API, method, resource, account and stage. No Function URL is
supported. The domain service's IAM identity must be explicitly registered for
the platform internal authorization plane and restricted to required generic
endpoints. IAM reachability is not client tool permission.

Domain runtime images copy this library and their own application package only;
they must not copy/import the gateway source tree. Deployment and provider
credentials remain domain-owned. Normal execution defaults disabled until the
operator configures and qualifies the service.
