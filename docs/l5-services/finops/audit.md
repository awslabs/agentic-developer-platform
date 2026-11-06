# FinOps repository audit

## Boundary and method

Complete **tracked-tree inventory**, source baseline
`388584bdd80a4a5ff142abe3234e7d1eac1cfd64` (main, 2026-10-27). This is not a
representative sample and not a claim of exhaustive semantic testing/live health.
No AWS target operations, credentials, provider jobs or deployments were performed.

[The reproducer](audit_inventory.py) extends the existing shared audit, reads every
blob with `git ls-tree` / `git cat-file`, maps every path to a component/disposition,
classifies workflow/build/package/deployment/executable/handler/migration entry
points, parses every workflow and Terraform declaration, and topologically orders
both SQL migration graphs. Generated files contain paths, blob identities and
structural metadata, not private file contents or provider evidence.

| Inventory | Count / content |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | All 12,710 tracked artifacts; path, blob, component, disposition and roles |
| [entry-points.tsv](entry-points.tsv) | All 2,835 detected entry points, individually component-mapped; includes launchers, shell/CLI programs, handlers, Docker/buildspec/package/action definitions, infra and migrations |
| [workflows.tsv](workflows.tsv) | All 161 workflows, events/jobs/path filters/local dependency references; join on path for component/disposition |
| [resources.tsv](resources.tsv) | All 2,057 Terraform resource/data/module declarations with source line and logical name; join on path for ownership |
| [migrations.tsv](migrations.tsv) | All 162 gateway revisions in dependency order; head `145_conversation_list_indexes`; operation/source references including raw-SQL markers |
| [l5-migrations.tsv](l5-migrations.tsv) | All 24 L5 revisions in dependency order; head `l5_results_knowledge_merge` |
| [inventory-summary.json](inventory-summary.json) | Counts by component, empty structural/unmapped lists, explicit semantic coverage limits |

Tracked paths not implicated by FinOps are explicitly assigned to existing component
ownership/exclusion, including Cyber/Superplane/other domain runtimes, agent factory,
Context, Insights, harness, tools, release tooling, nightly Security, experiments,
private environment-input locations, legacy docs and repository maintenance. Their
presence is not permission to modify/deploy them. Source-control, identity, budget,
Task, artifact and broker components are reused only through named interfaces.
No pre-existing subsystem is silently promoted to FinOps runtime ownership.

New design artifacts are outside that pinned baseline: all files in this FinOps
subtree are `finops-design` (Markdown, draft payload contract/fixtures/backlog data,
audit tables and offline verification tools); the single added shared README link
is `l5-design-index`. This is the complete working-change overlay, not a runtime
extension. Reproduce from the reviewed design revision with its shared audit helper;
re-auditing a later main requires a new explicit baseline, not silently regenerating
historical counts.

## Storage and entry-point findings

Sources in this table are at the pinned baseline unless explicitly marked open PR.
Line numbers point to definitions/triggers, not live observations.

| Entry/component | Current observation | FinOps disposition / owner |
|---|---|---|
| `modules/L5-services/src/l5_services/api/router.py:10`, `api/runs.py:11`, `api/results.py:11` | Allowlist/discriminators restrict existing services; cost run/result unavailable | FO01/FO03/FO04 coordinate shared service variants; FO05 consumes them |
| `modules/L5-services/src/l5_services/run_input.py:31`, `admission.py:42`, `configuration_population.py:11` | Security source assumptions; DevOps selection branches; no FinOps typed input | FO01 extends strict variants without loosening existing validation; coordinate DA09 |
| `modules/gateway/src/l5_integration.py:66`, `src/app.py` | Gateway hosts shared backend/router; authority/action-specific host integration needed | FO01–FO03 integrate scoped FinOps action authorization, not just router allowlist |
| `modules/gateway/src/products/configuration.py:74`, `modules/L5-services/src/l5_services/product_client.py:10` | Environment refs, financial proposals/policy refs and API client exist | FO02 uses authorized API candidates; no Profile SQL or invented billing-view Profile field |
| `modules/gateway/src/internal/l5_product_routes.py:1`, `l5_credential_routes.py:133`, `modules/gateway/src/shared/identity/product_scope.py` | Delegated Product and credential-session verification paths exist | FO01/FO02 qualify FinOps work permits/selected data-role scope; no ambient credentials |
| `modules/L5-services/src/l5_services/models/`, `migrations/versions/` | Existing configuration/binding/setup/run/work/attempt/observation/result/event state; both full migration graphs inventoried | FO02 adds retained-agent scope guard; FO04 adds checkpointed CE collection; common publication integration remains FO06/SA11 dependency |
| `modules/L5-services/src/l5_services/workers/execution.py:319`, `workers/recovery.py`, `workers/bindings.py`, `workers/relay.py` | Bounded execution/setup/repair handlers and runtime factory pattern exist | FO03 adds qualified adapter/executor; FO07 checks deployed handler/factory wiring rather than assuming availability |
| `modules/L5-services/src/l5_services/events/schedules.py:15` | Receiver freezes a Security tick; not a generic FinOps scheduler | FO06 adds scoped cost-query variant and FO07 verifies real ingress/repair; no second scheduler |
| `modules/L5-services/scripts/build-package.sh:7`, `stage-gateway.sh:7`, `modules/gateway/Dockerfile:84` | Wheel copies shared contract JSON, staged into gateway image with digest verification | FO01 schema compatibility and FO07 package identity; ordinary onboarding does not rebuild platform |
| `modules/L5-services/scripts/build-workers.sh:4` | Builds archive from wheel with `devops-worker` dependencies and release metadata | FO03/FO07 qualify FinOps browser/provider dependencies; this script is not a FinOps publisher |
| `modules/L5-services/infra/devops-events.tf:1` | Parameterized DevOps event/queue/repair infrastructure requires worker identities | Preserve sibling ownership; integrate shared transport in FO07, not a copied per-project stack |
| `.github/workflows/l5-services-ci.yml:3` | PR/main-path checks, schema and module tests; not deployment | Extend appropriate tests in FO01–FO07; a successful check cannot prove native inquiry |
| `.github/workflows/gateway-deploy.yml:40` | Main pushes include L5 source/package/build scripts and shared contract JSON; prose excluded | Retain host auto-deploys; no claims that this deploys private FinOps execution |
| `.github/workflows/gateway-frontend-deploy.yml:4` | Main frontend-path pushes and manual dispatch; docs excluded | FO05 UI auto-deploy; FO07 installed-client verification |
| `codebuild/bs-gateway-build.yml`, `.github/workflows/gateway-ci.yml`, `modules/gateway/pyproject.toml` | Gateway build/test entry points exist in full inventory | FO07 integrates exact-case L5 wheel/package staging and contract dependency paths |
| `modules/gateway/alembic/env.py`, `alembic.ini`, `scripts/migrate-before-rollout.py` | Host migration runner is present; searched configuration does not register L5 version locations | Do not treat package migrations as applied; FO07 coordinates SA15 runner/metadata/multi-head compatibility |
| `deploy.sh`, `platform/scripts/deploy-all.sh`, `.github/workflows/adp-release.yml`, platform CodeBuild/infra | Existing launcher/release orchestration; source search does not establish FinOps worker deployment | FO07 verifies/extends release integration, preserving existing deployment authority/order |
| `modules/gateway/frontend/src/pages/products/ProductOverview.tsx`, `frontend/src/services/products.ts`, `cli/command-manifest.json`, `modules/tools/facade/` | Product/CLI/tool hosts exist; not proof of FinOps tab, installed commands or registered MCP tools | FO05 adds thin shared-API surfaces, tests real installation/registration |
| `docs/l5-services/contracts/v1/peer-contracts.md`, `modules/agent-context/`, `modules/insights/` | Shared contribution contracts and peer runtimes; no FinOps consumer acknowledgment verified | FO06 coordinates producer/consumer capability; peer-owned storage/runtime not forked |

All other tracked build/deployment entries are individually retained in
[entry-points.tsv](entry-points.tsv); the table above explains FinOps-relevant
findings, not a substitute for that inventory. No deployment workflow was run.
IAM/resource policies, Secrets Manager paths, S3/DynamoDB definitions are included
in the full Terraform/source inventory; no new credential namespace, database,
billing table outside L5 or role permission has been provisioned/proposed by name.
Runtime policy/target values remain private configuration.

## Open and historical overlays

| Overlay | Inspected revision and material overlap |
|---|---|
| [PR #9080](https://github.com/aws-e/adp/pull/9080) | `35e00861a273b4a6492e05c57917c0272947255d`; six documentation files: shared index, capability expansion, POC evidence, FinOps/Transform/Well-Architected READMEs. FinOps references them immutably; does not import sibling files or assume merge. Resolve the FinOps README overlap by keeping both historical links and canonical architecture link. |
| [DevOps DA09 PR #9081](https://github.com/aws-e/adp/pull/9081) | `25e62f04bf750a7bcb36663e94105edfab587218`; file list and RunInput patch inspected: strict DevOps investigation variant added beside Security. Overlap in `run_input.py`, admission, execution, run API, event sources and gateway host. FO01 integrates FinOps as another explicit variant, not a replacement. |
| [Security SA15 PR #8932](https://github.com/aws-e/adp/pull/8932) | `1477aed6761d4f7e09be64344fe3c27ddeb605fd`; complete changed-path list and release workflow patch inspected. Proposed `l5-services-release.yml` uses manual dispatch, reviewed main SHA and protected target; worker lifecycle workflow, security infra, migrations, packaging, launcher and gateway integrations remain unmerged evidence. FO07 reuses/extends compatible mechanisms. |
| [#7923 original report](https://github.com/aws-e/adp/blob/46bbf63486b3af677bddd4a694de9a1c793cfd5a/docs/spikes/aws-agent-setup/finops.md) | Successful native console inquiry, view policy and cleanup reported; not repeated in this design run |
| [#7981 follow-up](https://github.com/aws-e/adp/blob/573062809251f240634f998f045d9716834c6f79/docs/spikes/aws-agent-setup/finops-adp.md) | Later billing limitation per extraction; not proof that #7923 failed |
| [#8015 helper/report](https://github.com/aws-e/adp/blob/83673b8808bc266ee52f1d1a2d97c6b3a55add1a/docs/frontier-agents/spikes/finops-cost-inquiry.md) | Script function/source inspection covers independent CE paging/Decimal/query/view checks. Historical 41-row result is not a native answer or durable integration. |

Parent [#8150](https://github.com/aws-e/adp/issues/8150), Security
[#8682](https://github.com/aws-e/adp/issues/8682), DevOps
[#8865](https://github.com/aws-e/adp/issues/8865) and catalog
[#8683](https://github.com/aws-e/adp/issues/8683) were read. Their EC IDs and sibling
content are unchanged. FinOps implementation children are new native children of
#9079, not replacement children of #8150.

## Coverage gaps and review limits

- **No tracked-artifact mapping gaps:** every pinned path has a component and
  disposition, and both migration graphs resolve. This is structural completeness,
  not a finding that every function/resource is correct.
- Dynamic shell dispatch, external actions/packages, generated deployment values,
  remote provider UI implementation and raw migration SQL semantics are not fully
  expanded by the parser. Their tracked entry points are enumerated; this audit
  does not claim runtime reachability/execution coverage.
- No live catalog, provider eligibility, billing role/view access, current deployment,
  pricing, consumer indexing/retrieval or public API availability was established.
  FO01 owns provider qualification; FO06 consumers; FO07 live release proof. The
  requested repository inventory is complete here, not deferred to those stories.
- Open PR branches are not main and may change. Exact inspected revisions are above;
  FO01/FO07 revalidate overlapping shared changes at integration.
- `gh project view 4 --owner aws-e` returned “Could not resolve to a ProjectV2 with
  the number 4”; project listing exposed no project to this integration. This does
  not prove the roadmap is absent. Coordinator later verified all seven children in project 4 (2026-10-27).
- Branch-protection read returned HTTP 403 “Resource not accessible by integration”.
  Ruleset read was also denied. Required approvals/checks cannot be inferred;
  normal repository PR review remains required, and author approval is not evidence.

## Design coverage

| Epic section | Design coverage / remaining acceptance |
|---|---|
| Description | Generic fixed-period inquiries, explicit scope/exclusions, seven native implementation outcomes and live completion boundary in architecture/backlog |
| Impact analysis | Billing/tenant/project isolation, duplicate-effect avoidance, signed precision, missing/freshness semantics, private sessions and existing budgets |
| Design | Components, API variants, keys/states, qualification, setup/observe/recovery/results/publication and client parity; provider executor support remains FO01 qualification, not invented implementation |
| Deployment | Actual/open entry matrix, grouped compatibility rollout, additive migrations, rollback and owned/adopted retirement; no deployment or installed worker claim |
| Validation | Every EC-01–EC-08 maps to stable child AC/SCN and independent assertions; docs checks are separate from planned provider/client/reuse acceptance |

See [verification](verification.md) for executed checks, reproducibility and remaining
review/project-placement limits. Public records contain synthetic examples and source
references only; operational receipts belong in authorized private stores.
