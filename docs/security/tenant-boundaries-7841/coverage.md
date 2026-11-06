# Source inventory, evidence and coverage limits

Reviewed source: `c958000f0829b1462c2f4ce23bfb743676b97306` (`main` observed on
2026-10-16 UTC). This record accompanies the [design](README.md), not a claim of
live vulnerability reproduction. Source content was inspected locally and GitHub
issue/PR states were read through `gh`. Private scan evidence and AWS deployment
state were not accessed. Public documents retain only finding IDs, not the private
ledger address or its deployment identity.

## Exhaustive enumeration within the declared source boundary

[artifact-inventory.csv](artifact-inventory.csv) contains **3,093 distinct tracked
paths**, each mapped to a component and disposition. The source tree contains
10,472 tracked paths. The inventory includes, without sampling:

1. Every tracked artifact in the named authentication/internal-plane, Cognito,
   relevant schema, Superplane API/auth/installation, Door and Context test roots
   listed in [audit_inventory.py](audit_inventory.py).
2. Every repository-wide tracked text match for the declared boundary symbols and
   route names, including callers outside the three affected modules.
3. Every tracked GitHub workflow/action, CodeBuild directory, scripts/releases
   directory, shell script, Terraform/HCL file, YAML manifest/configuration,
   Dockerfile, Makefile, Python/Node package declaration and conventionally named
   build/deploy/release/install/bootstrap/migration entry script. This yields
   **1,914 entrypoint/configuration candidates**, including explicit exclusions;
   a candidate is not necessarily an executable production entry point.

The other 7,379 tracked paths are excluded by rule: outside all declared component
roots, no boundary-symbol match, and not a selected delivery/configuration
artifact. That exclusion does **not** mean their behavior is secure. It limits
this audit to #7841's dependency and deployment surface instead of implying a
security review of every product in the repository. Binary/non-text inputs do not
match the symbol search; conventionally named build artifacts are still included.
Generated/untracked assets and dynamically supplied configuration cannot be
enumerated from this revision. These are coverage limits, not deferred inventory
tasks. The static inventory is delivered now, not assigned to implementation.

| Disposition | Count | Meaning |
|---|---:|---|
| boundary-or-dependency | 1,204 | In bounded component roots or references the boundary. Includes tests; not every row is a defect or proposed edit. |
| delivery-or-config-surface | 1,261 | Build/deploy/config artifact in a relevant/shared owner; retained for delivery audit even without a direct boundary symbol. |
| excluded-no-boundary-reference | 564 | Other component/entrypoint or caller build surface without a detected boundary dependency; no change proposed. |
| reference-only | 64 | Documentation/evidence reference, not runtime or build authority. |

| Component | Paths |
|---|---:|
| Gateway | 730 |
| Superplane | 689 |
| Agent Context | 327 |
| Shared delivery/configuration | 747 |
| Agent Factory caller surface | 274 |
| Tools caller surface | 68 |
| Harness caller surface | 12 |
| Other, explicitly excluded/reference-only | 246 |

Reproduction uses Git objects at the immutable reviewed revision, not a changing
working directory. Each CSV row records the selection rule. `--check` rebuilds
and compares the whole inventory, so an omitted candidate or edited count is
detectable. It does not download dependencies or execute application code.

## Internal-route inventory and existing boundaries

[internal-route-candidates.csv](internal-route-candidates.csv) enumerates **219
decorated route candidates** across Gateway source with internal-plane path or
symbol references, including handler, local decorator path, method and line.
This deliberately retains false-positive public routes instead of discarding
ambiguous candidates. The 14 route-bearing files directly under `src/internal`
are all represented. Router prefixes/composition determine final URLs; the CSV
does not pretend local decorator strings are the mounted route graph.

| Internal family | Existing boundary / design disposition |
|---|---|
| `vault_evidence_routes.py` | Four routes: repair evidence and revocation; retain run-bound delivery/preflight. |
| `credential_routes.py`, `assume_role_routes.py` | Broker worker tenant, selected-user credential and egress/binding checks; preserve, include in shared-dependency regressions. An old shared-key docstring is not evidence that the dependency accepts shared keys. |
| `routes.py`, `admin_routes.py` | Capability-specific `require_service_operation` / `service_tenant`, with explicit ingress cross-tenant handling; reuse without globally changing optional-target discovery semantics. |
| `controller_execution_routes.py`, `domain_operation_routes.py`, `domain_operation_binding_proof_routes.py` | Producer binding and authenticated domain/run/lease authority; the controller router appends domain-router routes. Cover composition rather than counting these as independent deployed copies. |
| `provenance_routes.py`, `status_callback_routes.py`, `task_credentials.py` | Run/workload/server-origin or task-specific authority. Tenant authority is not a generic body org. Preserve distinct verifier and regressions. |
| `persona_model_selection.py`, `persona_model_probe_routes.py` | Signed producer selection and restricted probe IAM/registry admission respectively; service-specific control paths, not an excuse to grant every internal caller metadata access. |
| `tools_authorize.py` | Tools facade plus run transport; assertions must not be treated as ordinary tenant-service registration. |

Source paths and route line numbers for all these files are in the CSV. Additional
candidates under `agentauth`, `crew`, `tasks`, `orchestration`, `auth` and `app.py`
remain explicitly mapped to Gateway, not silently omitted because they are outside
`src/internal`. Their feature-specific authorization is not rewritten by #7841.
S-05 must use mounted routes and data-helper instrumentation to test the invariant;
the current static enumeration cannot establish transitive runtime calls, optional
composition behavior or every dynamically generated route. Those are explicit
verification gaps, not a claim that 219 routes passed authorization tests.

## Delivery/build entry points traced

The CSV is the complete candidate listing. The following table traces the paths
that can actually deliver these components; it is a control-flow explanation,
not a representative substitute for the inventory.

| Entry surface | Component / relationship / explicit exclusion |
|---|---|
| Root `deploy.sh` → `platform/scripts/deploy-all.sh` | Source/release launcher composes module deployment. Includes Gateway, separate factory, optional Context and domain installation. Existing target verification and approvals remain mandatory; not run here. |
| `.github/workflows/gateway-deploy.yml`, `codebuild/bs-gateway-build.yml`, `modules/gateway/Dockerfile`, Gateway scripts/Kubernetes manifests | Gateway application build and rollout; source changes can auto-deploy on main. Stage shared contracts before image build. These do not publish the Cognito pre-token Lambda. |
| `.github/workflows/gateway-infra-{plan,apply}.yml`, Gateway infra root and `modules/cognito/{main,lambda}.tf` | Terraform pool/client/role and Lambda archive delivery. Apply is a separate manual control. `run-gateway-migrations.yml` and migration-before-rollout scripts govern Gateway DB upgrades. No new Gateway migration is proposed. |
| `.github/workflows/superplane-api-build.yml`, `modules/domain-apps/superplane/releases/{superplane.lock.yaml,resolve_lock.py,build-image.sh,buildspecs/api.yml}`, app Dockerfile | Maintained source and lock-driven build; the domain auth package is also an image input. An image build is not deployment. |
| `.github/workflows/superplane-{k8s-deploy,migrate,infra-apply}.yml`, Superplane `deploy.sh`, `installation/`, `infra/`, `k8s/`, CodeBuild project declarations | App-owned manual rollout/migration/infra and installation/recovery paths. Additive SSO provider binding belongs in the existing domain database, not platform state. |
| Superplane controller/executor/paid-worker builds and releases | Compatibility consumers of domain bindings, registry scope and current identity. No unrelated controller implementation is proposed, but their exact producer/worker authority checks cannot be ignored. |
| `.github/workflows/agent-context-images-build.yml` → `agent-context-deploy.yml`, `codebuild/bs-agent-context-image.yml`, Context images/manifests, module `deploy.sh` | Door image build then successful-main-build rollout; module launcher is an alternative. Keep ready/dependency checks and ACL DB composition. Context infra apply is separate if configuration ever changes. |
| `.github/workflows/adp-release*.yml`, platform release/deployment scripts, shared workflow templates | Alternative packaged-release delivery and promotion paths retained in the inventory; no release is made by a documentation PR. |
| Agent Factory, tools/harness build paths and registry provisioning | Related caller identity/transport only where symbol matches show dependencies. No platform-wide capability grant or worker role expansion. Agent Factory infra apply remains manual; this design requires no new factory resources. |
| Other domain applications, research apps, frontend-only/static-site builds and unrelated maintenance images | Explicitly excluded when no boundary reference is present; not delivery paths for this repair. Shared infra/config candidates remain mapped for review, without proposing changes to them. |

## Data-store and schema alignment

The migration graphs were enumerated from every tracked revision, using
`revision`/`down_revision` assignments rather than assuming filename ordering:

| Store | Revisions | Observed head | Relevant existing state |
|---|---:|---|---|
| Gateway PostgreSQL | 111 | `103_dev_box_consolidation` | User/tenant membership, canonical identity links, credentials and operation/delegation/evidence records. `066_credential_evidence_and_delegation.py` adds credential validation and workspace delegation. No new table proposed. |
| Superplane PostgreSQL | 68 | `054_unstarted_bootstrap` | `007_add_org_settings_sso.py` adds SSO fields; `Organization.sso_provider_name` is display text. Current model already has explicit unique `adp_org_id`; membership uniqueness is `(org_id, cognito_sub)`, not global sub. Add only the exact provider resource-binding column. |
| Agent Context PostgreSQL | 15 | `014_verified_public_acl` | Repository/ACL/catalog state already exists. Query scoping requires no new table. |

All three enumerated graphs have no missing parent revision. This checks source
graph closure, not which migrations are applied live. Inspect the actual head
again when allocating the implementation migration number; do not branch from a
stale guessed sequence.

Existing Gateway agent registry and domain-provider authority stores remain their
respective authority sources. Existing secret paths and storage roots are reused;
the design introduces no new DynamoDB table, Secrets Manager convention or S3
bucket. The pre-token Lambda currently reads M2M metadata from its configured
DynamoDB table; its new human-record read needs only pool-scoped Cognito permission.
No production IAM role/account was assumed or checked in this run.

## External contract checks

AWS documentation retrieved on 2026-10-16:

- [Cognito attribute mapping](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)
  confirms that mapping values name IdP attributes, and writable mapped attributes
  are required. The text says a non-writable mapped attribute is not set and
  authentication proceeds; do not claim every such sign-in fails or that current
  restrictive Terraform proves a live tenant overwrite.
- [UpdateUserPoolClient](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_UpdateUserPoolClient.html)
  documents mutable client settings and `WriteAttributes`. Unspecified write
  attributes default to standard attributes; omission alone is **not evidence**
  that custom tenant attributes become writable. The security design still
  explicitly preserves restrictive settings and verifies installed state.

## Checks performed and remaining uncertainty

Pre-publication results, 2026-10-16 UTC, from `/work/repo`:

| Check | Observed result |
|---|---|
| `audit_inventory.py --check` | Pass: all 3,093 rows reproduce from the reviewed Git tree. |
| `audit_inventory.py --routes --check` | Pass: all 219 static route candidate rows reproduce. |
| CSV path and duplicate checks | Pass: every listed artifact/route file exists; artifact paths are unique. |
| Markdown local-link/line-target check | Pass: 44 relative link targets resolve; cited source line numbers exist at the reviewed SHA. |
| Public-document scan after staging | `python3 scripts/check-public-docs.py`: 1,544 public files checked, zero findings. This pattern check is supplemented by manual review; it is not a credential scanner proof. |
| Whitespace check | `git diff --cached --check`: pass. |
| Synthetic URL example | Reserved `.test` host; HTTPS; no userinfo, query or fragment. |
| Source freshness | GitHub `main` still equals the reviewed SHA at pre-publication check. |

Completed source investigation: exact issue/comments and native parent; repository
instructions; module/identity/deployment documentation; real evidence consumer and
reserved-provider paths; relevant policy/ACL siblings; related issue/PR states;
repository-wide symbol search; all declared inventory candidates; migration graph
enumeration; actual build/rollout controls; AWS mapping/client documentation.

The validation commands in [validation.md](validation.md) reproduce inventory and
public-doc checks. Documentation link/line targets are checked against the reviewed
tree before publication. These are documentation checks only; no implementation
suite, live Cognito sign-in, AWS identity, deployed route graph, staging probe or
private scan ledger was tested. Pipeline definitions were inspected, but workflow
run success and live service health were not asserted.

Unresolved deployment evidence: external callers of revocation; current registry
and domain binding enrollment; complete installed provider/client inventory;
legacy provider ownership and subject-link reconciliation; permitted real issuer
hosts and OIDC configuration; target access and current migrations. Operations
and identity owners resolve these in B8/B9 and S-12. They must not be hidden by an
unconditional "merge deploys everything" statement or by counting unit tests as
live acceptance. No blocker here grants permission to read private resources.

The source issue has no GitHub Project item and no existing module/feature catalog
mapping. Do not invent them. The issue owner can attach the established project
and catalog mapping as bookkeeping; native issue/parent identity and the concrete
component owners above are preserved. This design does not claim Insights result
ingestion or story acceptance has occurred.
