# Product Context storage

Product Context stores uploaded originals and derived S3 content under
`tenants/{tenant_id}/products/{product_id}/` in the deployment's Context bucket.
Projects in the same Product share this boundary. Product team members can
upload and read originals; a Product owner with current access or a verified
platform administrator can select encryption. Archived Products are read-only.

## Identity and isolation

The Gateway authorizes the human through the Product API's current tenant/team
membership rules. It then assumes the Context storage service role using
`adp_tenant_id` and `adp_product_id` session tags. The role's S3 permissions restrict
access to that Product prefix; each session policy further restricts access to
one object, operation, encryption key, and (for original reads) object version.
The source identity is `knowledge-ingestion`. No per-user AWS access key is used.

The Gateway remains a trusted authorization broker: its AWS identity can issue
sessions for authorized Products. The bucket is shared; a prefix and policy
boundary is not a separate AWS account or bucket. Platform administrators retain
administrative authority. Application authorization protects search results;
this feature does not create separate Product vector indexes or change MCP's
existing caller authorization.

Workers and clients receive short-lived object links, never STS credentials.
New links require current authorization. Already issued links can remain usable
for up to five minutes after membership changes. Archived or unassigned Products
cannot continue ingestion operations. External S3 sources retain their existing
explicit source allowlists and external ownership; selecting a Product key does
not change encryption in a source bucket.

## Encryption choices

| Choice | Provisioning | Configuration |
| --- | --- | --- |
| Shared platform key | Terraform creates one Context KMS key | Default when storage is enabled |
| Dedicated ADP-managed Product key | Platform Terraform supplies a tenant/Product pair | Product owner selects **Dedicated ADP-managed product key** |
| Customer-supplied Product key | Administrator configures an existing KMS key and its key policy | Product owner enters the key ARN |

Dedicated keys must be enabled, symmetric `ENCRYPT_DECRYPT` keys in the Context
bucket's account and region. They must carry `adp:tenant_id` and `adp:product_id`
tags matching the Product. Customer key policies must permit the storage session
role to decrypt and generate data keys through S3, and permit the Gateway role
to describe the key and read its tags. APIs never create IAM roles or KMS keys.

Configuration uses optimistic concurrency (`revision`). Changes apply to new
uploads and newly derived objects. Existing files retain their keys, and the
configuration retains previously selected key ARNs for reads. Legacy objects
encrypted with `alias/aws/s3` remain readable. Disabling or deleting a retained
key can make those files unreadable. Re-encrypting existing content is a separate
migration. This feature does not re-encrypt search vectors or other backends.

## UI and CLI

Open a Product's project workspace → **Context** → **Storage** → **Open storage**.
Current members see the storage location and upload control. Authorized owners
and administrators also see the encryption selector. Uploads support PDF, DOCX,
PPTX, Markdown, text, RST, AsciiDoc, HTML, CSV and XLSX, up to 50 MiB.

The existing `knowledge` CLI command and API namespace remain compatible with
the Context display name:

```bash
adp knowledge storage show --product "$PRODUCT_ID"
adp knowledge storage configure --product "$PRODUCT_ID" \
  --encryption managed --revision 0 --yes
adp knowledge storage upload --product "$PRODUCT_ID" --file ./architecture.md --yes
adp knowledge storage complete --product "$PRODUCT_ID" --upload-id "$UPLOAD_ID" --yes
adp knowledge storage download --product "$PRODUCT_ID" \
  --asset-id "$ASSET_ID" --output ./original.md
```

Use the current revision returned by `show`. For customer encryption, add
`--encryption customer --kms-key-arn "$PRODUCT_KEY_ARN"`. Mutations preview without
`--yes`; `--dry-run` always wins. Signed URLs and credentials are not printed.
Downloads use a new file with private permissions and refuse overwrites.

## Upload and download contract

All paths below are relative to the deployment API base (normally `/api`).

| Method and path | Behavior |
| --- | --- |
| `GET /agent-context/products/{product_id}/storage` | Location, encryption, revision, limits and caller permissions |
| `PUT /agent-context/products/{product_id}/storage` | Owner/admin changes encryption using the current revision |
| `POST .../storage/uploads` | Filename, byte length and SHA-256 reserve a server-generated object key; returns a five-minute PUT link |
| `POST .../storage/uploads/{upload_id}/complete` | Contributor or authorized owner verifies and registers the original; retries return the existing asset |
| `POST .../storage/assets/{asset_id}/download` | Current member receives a five-minute link to the verified original version |

S3 enforces the upload's signed checksum, length and encryption headers. Completion
checks stored size, checksum, KMS ARN and versioning before registration. Originals
disable S3 bucket keys. Workers read the pinned original version, so overwriting
the upload key cannot change the bytes indexed by the registration. Derived
objects use the selected Product key and existing attempt-bound worker grants.
Single and bulk imports cannot register originals or enclosing upload folders
without verification. Worker reads enforce the same boundary, including older
folder registrations; tenant-wide folder grants cannot access Product objects.

There are at most 20 active, unfinished receipts per Product. Receipts expire
after 24 hours; upload links expire after five minutes. If transfer succeeds but
completion fails, use **Finish import** or the CLI `complete` command. Indexing
readiness remains visible through the existing asset status UI and CLI. A saved
asset can require an indexing retry if queue dispatch was unavailable.

Removing a Context entry revokes new original-download links. It does not delete
S3 versions, upload receipts or encryption history. Expired/uncompleted uploads
also remain stored: this change introduces no automatic data deletion. Retention
and storage cost management remain administrator responsibilities.

## Infrastructure and rollout

Implementation lives in:

- `modules/agent-context/terraform/modules/product-context-storage/`: role, policies, KMS keys and exact-origin CORS.
- `modules/agent-context/terraform/product-context-storage.tf`: optional integration with the existing Context deployment.
- `modules/agent-context/alembic/versions/018_product_context_storage.py`: configuration, receipts and version references.
- `modules/gateway/src/knowledge/product_storage*.py`: service sessions and authorized APIs.
- `modules/gateway/cli/adp_context_storage.py`: CLI parity.

The feature defaults off. A rollout uses existing deployment procedures and
approval gates; this document does not authorize applying Terraform.

1. Apply Context database migration `018_product_context_storage`.
2. Provision with `product_context_storage_enabled=true`, exact dashboard HTTPS
   origins in `product_context_upload_origins`, and optional dedicated keys in
   `product_context_managed_keys`. Keep `product_context_deny_gateway_direct_access=false`.
   The existing bucket must have versioning enabled. CORS configuration is owned
   by this module when origins are supplied; review existing bucket CORS first.
3. Update all Gateway replicas with the new code and `adp-ingestion-control`
   ConfigMap values: `PRODUCT_CONTEXT_STORAGE_ROLE_ARN`,
   `PRODUCT_CONTEXT_DEFAULT_KMS_KEY_ARN`, `PRODUCT_CONTEXT_KMS_ALIAS_PREFIX`.
   Preserve `AGENT_CONTEXT_S3_BUCKET` and enable `SCOPED_KNOWLEDGE_IMPORTS_ENABLED`.
4. Publish the frontend. Verify an upload, indexing, original download and a
   cross-product refusal using a non-admin member. Existing workers use the same
   signed GET/POST protocol and do not require a rebuild for this feature.
5. Optionally enable `product_context_deny_gateway_direct_access` after auditing
   all Gateway consumers of the bucket. This denies direct reads/writes of
   Product objects and **all direct bucket listing**, including parent-prefix
   listing that could reveal Product object names. Legacy folder imports using
   ambient Gateway listing must be migrated before enabling this final guard.

The role variable activates the new storage path. Do not enable it on replicas
without migration 018. During rollback, retain KMS keys, settings and upload
receipts, and retain application support for encrypted Product objects. The
migration refuses a downgrade when retained storage records exist; Terraform
protects KMS keys from accidental destruction.

## Verification

Backend tests exercise real PostgreSQL migrations with mocked AWS, cross-tenant
and Product access, uploads, checksums, key history, versions, membership removal,
archival, retries and old-schema refusal. SDK signing tests inspect actual
SigV4 PUT/POST contracts. Frontend and CLI tests cover configuration revisions,
retry behavior and transfer credential separation. Terraform tests use a mocked
provider and never apply cloud resources. These checks do not replace the live
rollout verification in step 4.
