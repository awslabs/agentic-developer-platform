# Operation-bound vault delivery

The API's `build_vault_client(settings)` exposes control-plane evidence reads only.
It holds the internal evidence key and implements no delivery-channel methods.
Preflight and material delivery use a separate channel composed inside the trusted
executor for one admitted attempt:

```python
transport = ExecutorVaultTransport(
    endpoint=api_gateway_invoke_url,
    binding=admitted_run_binding,
    run_credential_file=projected_run_credential_path,
    workload_token_file=projected_workload_token_path,
)
channel = ExecutorVaultChannel(transport=transport)
```

The binding contains the admitted operation/job IDs, the current harness lease's
attempt ID, and the verified run principal as recipient. Operation IDs are not
invocation IDs. The transport refuses another binding, reloads projected tokens
on every delivery, obtains refreshed AWS credentials, and signs the actual request
bytes for API Gateway. It never supplies `X-Caller-Identity` or uses the shared key
for preflight or material delivery. The executor receives no internal evidence key,
and there are no application-wide run-token settings.

`ProviderExecutor` calls `channel.revocation_state()` before materialization. This
uses `POST /internal/v1/credential-delivery/preflight` with the same SigV4 identity,
projected run/pod tokens and complete operation binding as material delivery.
Gateway checks the lease holder/attempt/fence, budget, cancellation, capability,
approved credential/account, delegation and validated current version, then
refreshes authority after provider version I/O. Preflight never reads secret
material or marks a credential delivered. The subsequent delivery repeats these
checks; a successful preflight is not reusable delivery authority. The shared-key
control-plane revocation endpoint is not part of the executor channel.

Approval must include `credential_id`, `credential_service`, `credential_label`,
`provider` and `provider_account_id` in `OperationRequest.parameters`. The account
is the actual provider account ID, resolved before approval, not a display label.
The canonical harness digest covers both the credential and its intended account.
Gateway compares the executor's `RunBinding` provider/account with that approved
target and the independently authenticated account held in version-bound evidence
before and after secret I/O. Rotating a reference to a role in another account
cannot authorize its use for the old operation. Gateway loads that same decoder (staged from its canonical
source into the image at build time) and checks the consumed approval digest and
current lease, including its holder, attempt, fence, expiry, budget and cancellation.
Operations approved without an exact credential selection cannot fetch material;
they need a new approval rather than a retroactive change to an admitted request.

The selected credential must be delegated to the same tenant/workspace and have
provider-validation evidence for the unique `AWSCURRENT` version, with credential,
permission and quota readings all permitting provisioning. Gateway fetches exactly
that version and repeats the validation, version, approved-reference, lease,
run/pod/grant/flow and capability checks after provider I/O. Registry capability
checks strongly read the primary record; the IAM identity cache cannot retain a
revoked capability. Refusals contain no material or provider exception text.

Deployment composition remains owned by #5535/#5538: projected run identity must
be issued to the current executor attempt, its dedicated service account/image
must be recognized by workload verification, its IAM role must have the seeded
`credential:operation-delivery` capability, and Gateway must have scoped access to
the executor's authority tables. This implementation does not activate any of
those deployment resources or grant the capability to the shared worker fleet.
The paired PostgreSQL tests simulate AWS/Kubernetes boundaries, exercise the real
authorization decisions, and do not constitute a live deployment acceptance.

## Producing delegation and validation evidence

Migration 066 creates empty authority tables. Populate them through the Gateway
owner APIs below; database inserts and a submitted Superplane report are not
validation authority. These APIs use the ordinary authenticated ADP user session
and the vault's existing owner/admin mutation rules. Another tenant or credential
owner cannot grant access. Team and organization credentials require the existing
organization-admin check. The internal evidence key cannot write authority.

The initial provider verifier supports ADP `aws_role` credentials (`service=aws`).
The Gateway deployment must configure `BG_CREDENTIAL_VALIDATION_PROFILES` as a JSON
map of **tenant ID → workspace ID → EC2 profile**, for example:

```json
{
  "tenant-id": {
    "workspace-id": {
      "region": "eu-west-2",
      "image_id": "ami-0123456789abcdef0",
      "instance_type": "g5.xlarge",
      "subnet_id": "subnet-0123456789abcdef0",
      "security_group_ids": ["sg-0123456789abcdef0"]
    }
  }
}
```

Replace these example identifiers with the deployment's reviewed placement. No
profile, placement, validation flags, ARN or material is accepted from the
validation HTTP body. Missing profiles, unsupported providers and unsupported EC2
quota families fail closed. The profile establishes permission/quota observations
for **one instance of that type in that region and placement**; it does not certify
all provisioning plans or reserve quota. Run admission still needs its own resource,
permission and budget checks. Update the server profile and revalidate when the
intended placement changes.

Gateway needs its existing scoped Secrets Manager read/describe and KMS access,
plus `sts:AssumeRole`/`sts:TagSession` to the registered role. The role must trust
Gateway, with its configured external ID and ADP user session tags. Validation
uses that role to call STS GetCallerIdentity, EC2 RunInstances **with DryRun=true**,
DescribeInstances, DescribeInstanceTypes, DescribeCapacityReservations, and Service
Quotas GetServiceQuota. Missing read permissions produce no passing evidence.
Permission is proven by AWS's `DryRunOperation` response; no instance is launched.
Quota compares regional On-Demand vCPU limits against running/pending non-Spot
instances and unused owned active capacity reservations in the relevant Standard,
G/VT or P family. Physical fleet capacity remains `null`, explicitly unmeasured.
SDK retries and inventory pagination are bounded. No live validation is performed
by installing the code or configuring a profile.

Supported onboarding sequence (paths are relative to the indicated application's
API base; all calls use the caller's authenticated session):

1. Register the AWS role through ADP's existing AWS connection flow or Gateway
   `POST /auth/credentials` (`service=aws`, `credential_type=aws_role`). Raw material
   is stored only in ADP. Retain the returned credential ID and label.
2. Call Gateway `PUT /auth/credentials/{id}/workspaces/{workspace_id}` with no body.
   This records owner-authorized delegation to that workspace in the caller's tenant.
3. Call Gateway `POST /auth/credentials/{id}/workspaces/{workspace_id}/validation`
   with no body. Gateway pins the unique `AWSCURRENT` VersionId, fetches exactly
   that version, performs the provider checks and persists their separate readings.
   The response contains `validated_version_id`, the STS-observed
   `provider_account_id`, `validation` and `report_digest`;
   it contains no secret material. HTTP 200 can contain failed readings: check all
   three booleans before expecting delivery to succeed.
4. Register the reference with Superplane's existing `POST /vault/credentials`
   (`adp_credential_id`, provider, credential type and display name), then
   `POST /workspaces/{workspace_id}/provider-connections` using the exact
   `credential_id`, `service`, `label` and `provider`. The caller also needs the
   workspace's `workspace:renew_credential` grant.
5. Submit the returned `validation` object unchanged to Superplane
   `POST /workspaces/{workspace_id}/provider-connections/{connection_id}/validation`.
   Superplane asks Gateway to independently attest its digest and observation time
   before activation. This no longer depends on an unpopulated evidence table.

Revalidation immediately replaces old positive evidence with a failed generation
before any provider I/O. A failed call cannot leave that old approval usable.
Concurrent validation, withdrawal, deletion, metadata changes or version rotation
cannot be overwritten by a late result. Version and authority are checked again
when material is delivered, so the response is evidence, not a delivery capability.

To withdraw a workspace, call Gateway
`DELETE /auth/credentials/{id}/workspaces/{workspace_id}`. It revokes delegation and
deletes validation in one transaction. Regranting with PUT requires validation again.
To revoke the credential globally, use the existing Gateway credential DELETE;
the database removes its delegation/evidence through foreign-key cascades before
the Secrets Manager deletion. Metadata PATCH also invalidates validation.

For replacement credentials, register a new ADP credential, delegate and validate
it with steps 1–3, register its Superplane reference, then use the existing
Superplane connection `/rotation` API with `replacement` and the new `validation`.
Withdraw/delete the old credential when its authorized uses are finished. Existing
Gateway value rotation uses re-registration, not a value PATCH. An externally
rotated Secrets Manager version requires a new Gateway validation; old-version
evidence cannot authorize delivery. An admitted operation selecting the old
credential ID must receive a new approval to select its replacement.

The paired tests create delegation and validation through these production APIs
and run the full `ProviderExecutor` preflight/materialization/cleanup path with no
internal key in the executor channel, real PostgreSQL and simulated AWS boundaries.
They also refuse revoked run/capability/lease/budget/account/delegation before secret
I/O and recheck authority changes during preflight. Onboarding UI and deployment
configuration remain with #5730 and #5535/#5538 respectively.
