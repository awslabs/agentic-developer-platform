# Security source storage

Security uses two independent storage boundaries:

| Storage | Terraform root | Access |
| --- | --- | --- |
| Immutable Product snapshots | `modules/L5-services/infra/source-snapshots` | Gateway captures and reads exact versioned objects under `products/source-snapshots/`. Workers read them through the authorized Product API. |
| Native Security source archives | `modules/L5-services/infra/security-sources` | A dedicated service role trusts the exact ADP-created agent space and reads its dedicated source prefix. |

Both roots enable S3 versioning, encryption, public access blocks and retained
buckets. Keep their state and deployment-specific input files private and
separate from the gateway/platform state. Do not commit generated plans or state.
The native root consumes the original ADP setup identity; it does not create or
replace the agent space.

The Product snapshot root publishes the bucket name to
`/adp/<environment>/gateway/product-source-snapshot-bucket`. Gateway Deploy reads
that setting into `PRODUCT_SOURCE_SNAPSHOT_BUCKET`. Provision storage before
rolling out the setting, and wait for every gateway replica to finish rolling
before submitting repository captures. Background capture workers run in each
replica and require the same storage configuration.

The selected connection role needs `s3:PutObject`, `s3:GetObject` and
`s3:GetObjectVersion` on the dedicated `security-agent/source/*` prefix. Upload
verification reads the exact S3 version; current-object read permission alone
is insufficient. Scope `iam:PassRole` to the registered execution role and
`securityagent.amazonaws.com`.

After provisioning, verify versioning and gateway get/put permissions, complete
the gateway rollout, and submit through the Product Security API. Admission
captures sources before native execution. A first worker read atomically binds
the child delegation only after the gateway verifies the exact admitted run,
current parent authorization, selected source and live work lease. Subsequent
reads continue to check the same authorization and fencing rules.

Check the retained preparation and run state after an interruption. Reuse the
original idempotency key while preparation is active. A failed preparation does
not establish that a native assessment ran; inspect its cause and any run record
before creating a new submission.

Threat-model setup reads the one architecture document selected in the saved
Security configuration through a fenced internal Product API. It retains a
versioned source snapshot, uploads its verified bytes as a native artifact, and
passes that artifact as `scopeDocs` when creating the persistent model. Later
assessments update `scopeDocs` to the run's captured revision and include the
selected model's existing `serviceRole`. Documents must not be sent under
`assets.documents`.

The setup session permits artifact upload only into the selected, ADP-owned
space. It permits passing the previously registered source role, or the exact
role identity created by `infra/security-sources` before registration has been
retained. Both paths constrain the target service to Security Agent. They do not
grant wildcard role passing or change the selected connection's own permissions.

An authorization failure retains the failed worker stage, an allowlisted gateway
cause when available, and a recovery action. Provider response bodies, credentials
and arbitrary exception messages are excluded from that diagnostic. A run blocked
before any provider attempt can be reconciled using its original input and source
snapshots; the worker must pass current authorization checks again.
