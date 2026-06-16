# Design Research: Live AWS Account Resource Graph

**Issue:** #1546 (sub of EPIC #1345)
**Author:** @agent-architect
**Date:** 2026-06-16
**Status:** Research complete — feeds future design-authority story
**Siblings:** #1529 (code call graph), #1545 (declared IaC graph)

---

## 1. Executive Summary

This document evaluates approaches for building a **live AWS account resource graph** — what actually exists in an account and how resources are wired — stored in the same Neptune instance as the code call graph (#1529) and the declared IaC graph (#1545), cost-enriched via CUR.

**Recommended starting point:** A **custom AWS Config snapshot → Neptune CSV pipeline** (Option C below), reusing the proven extract→CSV→Neptune load pattern from #1529/#1532, with the archived `aws-samples/amazon-neptune-aws-config-visualization` repo as a reference implementation. Supplement with the existing `discover-infra.py` Resource Explorer data for breadth.

**Key finding:** CUR is confirmed as a cost-enrichment layer ONLY (flat ledger, no relationships). AWS Config is the correct relationship source. No off-the-shelf tool produces a Neptune-compatible live resource graph without significant adaptation work.

---

## 2. Evaluated Options Landscape

### 2.1 Comparison Table

| Tool/Approach | Resource Type Coverage | Relationship Coverage | Output Format | License | Neptune Fit | Maturity | Reuse Effort |
|---|---|---|---|---|---|---|---|
| **A. Cartography (Lyft/CNCF)** | ~33 AWS services, extensive | Rich (explicit edges: instance→SG, subnet→VPC, role→policy) | Neo4j property graph (Bolt/Cypher) | **Apache-2.0** ✅ | ❌ Not directly. Neo4j-locked (APOC, LOAD CSV, stored procs). Open issue #1658 for Neptune support. | v0.137.0 (Jun 2026), 1861 commits, CNCF Sandbox | **High** — requires rewriting data-loading layer + Neo4j-specific Cypher |
| **B. Workload Discovery on AWS** | Broad (Config + supplemental SDK) | Rich (Config-derived + SDK-enriched) | Neptune (Gremlin) + OpenSearch | **Apache-2.0** ✅ | ✅ Already uses Neptune! But Gremlin, not openCypher | v2.3.20 (Jun 2026), actively maintained. **⚠️ RETIRING Aug 14, 2026** | **Medium** — could fork Neptune data model, but Gremlin→openCypher translation needed; retirement risk |
| **C. AWS Config snapshots → custom parser → Neptune** | Whatever Config records (hundreds of resource types) | 4 relationship types from Config (`contains`, `is_contained_in`, `is_associated_with`, `is_attached_to`) | Custom CSV (we control schema) | **N/A** (our code) | ✅ Perfect — we define the openCypher CSV | Proven pattern (aws-samples PoC, MIT-0) | **Medium** — parser from Config JSON is straightforward; relationship extraction is built-in to Config CIs |
| **D. Steampipe (+ AWS plugin)** | **Best-in-class: 586 tables** | ❌ None — flat relational tables only | SQL results (PostgreSQL wire protocol) | **Apache-2.0** ✅ | ❌ No graph output; requires custom ETL for relationships | Production-grade, massive coverage | **High** — must write ALL relationship-extraction logic (JOINs between tables → edges) from scratch |
| **E. CloudMapper (Duo/Cisco)** | Limited (SGs, EIPs, ENIs, ELBs, IAM) | Limited (network-diagram focus) | File-based JSON + cytoscape.js | **BSD-3-Clause** ✅ | ❌ No graph DB support | **Abandoned** (last release Nov 2021) | ❌ Not viable |
| **F. Resource Explorer (existing in-repo)** | Broad (hundreds of services, auto-enabled) | ❌ None — inventory/search only, no relationship data | JSON (ARN, type, region, tags) | **N/A** (AWS API) | ❌ No relationships | GA, already used by `discover-infra.py` | ✅ Already implemented — gives us the NODE inventory; not edges |

### 2.2 Detailed Evaluations

#### A. Cartography (Lyft / CNCF Sandbox)

**What it is:** A Python tool that crawls AWS APIs (and GCP, Azure, GitHub, Okta, etc.) and writes a property graph into Neo4j. CNCF Sandbox project, Apache-2.0 licensed.

**Strengths:**
- Richest open-source AWS asset/relationship data model available
- Explicit schema with documented node types and relationship types
- Active community; wide service coverage (~33 AWS modules)
- Time-based "update tags" for finding stale/drifted resources
- Multi-cloud support (could extend to GCP/Azure accounts later)

**Neptune compatibility (CRITICAL BLOCKER):**
- Cartography's data-loading layer uses Neo4j-specific features:
  - `CALL[YIELD...]` stored procedures — NOT supported in Neptune
  - APOC library usage — NOT available in Neptune
  - `LOAD CSV` — Neptune has no equivalent (uses Bulk Loader instead)
  - `id()` returns integers (Neo4j) vs. strings (Neptune)
  - Some queries use `shortestPath()` — NOT supported in Neptune
- GitHub issue #1658 (opened Jun 2025, labeled "long-term-improvement") explicitly requests Neptune/openCypher support — not yet addressed
- **Verdict:** Porting Cartography's core `sync` logic to Neptune would be a multi-month effort. The DATA MODEL (node types + relationships) is excellent reference material, but the CODE is not reusable.

**Recommendation:** Study Cartography's AWS module schemas for graph model inspiration; do NOT attempt to port the sync engine.

#### B. Workload Discovery on AWS (formerly AWS Perspective)

**What it is:** An AWS-maintained solution that deploys a full stack (Neptune, OpenSearch, ECS Fargate, AppSync, CodeBuild) to discover and visualize AWS account architecture.

**Strengths:**
- Already uses Neptune as its graph store
- Rich relationship model (Config-derived + supplemental SDK calls)
- Cost integration via CUR + Athena
- Exposed via AppSync GraphQL API (not just a UI)
- Apache-2.0 license

**Critical issues:**
- **⚠️ RETIRING August 14, 2026** (less than 2 months from now). AWS recommends migrating to CloudWatch Application Map and AWS DevOps Agent.
- Uses Gremlin query language, NOT openCypher — our Neptune pipeline uses openCypher
- Heavy infrastructure footprint (Neptune, OpenSearch, ECS, Cognito, AppSync, CodeBuild — massive CloudFormation stack)
- 15-minute refresh cycle (not real-time, but acceptable for our use case)
- Closed-loop design makes extracting just the data model non-trivial

**Recommendation:** Do NOT adopt — retiring in 2 months makes this a dead-end. Study its data model and the ECS Fargate → Neptune pattern for reference, but build independently.

#### C. AWS Config Snapshots → Custom Parser → Neptune (RECOMMENDED)

**What it is:** Use AWS Config's Configuration Items (CIs) as the authoritative source of resource state and relationships, parse them into Neptune-compatible CSV, and load via the existing bulk-load pipeline.

**How Config relationships work:**
- Each Configuration Item includes a `relationships` array with entries like:
  ```json
  {
    "resourceType": "AWS::EC2::SecurityGroup",
    "resourceId": "sg-0abc1234",
    "relationshipName": "Is associated with SecurityGroup"
  }
  ```
- Four relationship predicates: `contains`, `is_contained_in`, `is_associated_with`, `is_attached_to`
- Coverage: EC2, VPC, RDS, Lambda, IAM, CloudFront, Auto Scaling, ECS, ALB, and hundreds more
- Access methods:
  - **Snapshot delivery to S3** — full CI dump as JSON, periodic (best for batch)
  - **Config Aggregator** — cross-account/cross-region single query endpoint
  - **Advanced Queries** — SQL-like `SELECT` with relationship filtering
  - **API** — `GetResourceConfigHistory`, `BatchGetResourceConfig`

**Reference implementation:** `aws-samples/amazon-neptune-aws-config-visualization` (MIT-0, archived Jan 2026):
- Architecture: S3 (Config JSON snapshots) → Lambda parser → Neptune (Gremlin)
- 11 commits, PoC quality — useful as a pattern reference, not production code
- Lambda parses Config CI JSON, extracts resource + relationship arrays, writes to Neptune

**Strengths:**
- AWS Config IS the authoritative source for "what exists and how it's connected"
- Relationship data comes for free with Config CIs (no additional API crawling needed for the core 4 relationship types)
- Snapshot-to-S3 gives us batch processing (aligns with our bulk-load pipeline)
- Cross-account via Config Aggregator (matches our assume-role pattern in `discover-infra.py`)
- We control the graph schema → openCypher CSV exactly like #1529
- Same pipeline pattern: source → extract → CSV → S3 → Neptune Bulk Loader

**Limitations:**
- Config's 4 relationship predicates are coarse (no "routes_to", "encrypts", etc.)
- Supplementary API calls (describe-* APIs) needed for richer edge semantics
- Config recording has per-item cost (~$0.003/CI recorded per month)
- Not all resource types have relationship data (some have empty `relationships` arrays)

**Cost estimate:**
- Config recording: ~$0.003/CI/month. An account with 5,000 recorded resources ≈ $15/month
- No additional Neptune cost (reuses existing Serverless cluster)
- S3 snapshot storage: negligible (JSON, compressed)

#### D. Steampipe

**Strengths:** Best coverage (586 resource types), Apache-2.0, SQL interface.
**Fatal flaw for this use case:** No relationship data whatsoever. Every table is flat. You'd need to write custom JOIN logic for every relationship type (instance→SG, subnet→VPC, etc.) — essentially rebuilding what Config already provides for free. The breadth is impressive but it's an INVENTORY tool, not a TOPOLOGY tool.

**Recommendation:** Not suitable as primary source. Could supplement Config for resource types Config doesn't record (niche services).

#### E. CloudMapper — **REJECTED** (abandoned, limited coverage, no graph support)

#### F. Resource Explorer (already in use) — **COMPLEMENTARY** (node inventory only, no edges)

Already implemented in `modules/agent-context/images/ingestion/discover-infra.py`. Useful for discovering ALL resources (including those Config doesn't record), but provides zero relationship data. Use as a supplemental node source alongside Config.

---

## 3. Recommendation: Option C — AWS Config → Custom Parser → Neptune

### 3.1 Rationale

| Decision Factor | Why Config → Neptune wins |
|---|---|
| **Relationship data source** | Config CIs include relationships natively — no API crawling needed for core topology |
| **Graph schema control** | We define the openCypher CSV → full compatibility with existing #1529 pipeline |
| **Pipeline reuse** | Same pattern: source → extract → CSV → S3 → Neptune Bulk Loader. Reuse `load_csv_to_neptune.py` directly |
| **License** | Our code (Apache-2.0 if open-sourced) — no third-party copyleft risk |
| **Cost** | Marginal: Config recording ($15/mo for 5K resources) + existing Neptune Serverless |
| **Cross-account** | Config Aggregator provides single-pane view across accounts |
| **Maturity** | AWS Config is GA since 2015; relationships are documented and stable |

### 3.2 What We Study (but don't adopt) From Others

| Source | What We Take |
|---|---|
| Cartography's AWS data model | Node type taxonomy, relationship type naming, the concept of "update tags" for staleness detection |
| Workload Discovery | The ECS Fargate → Neptune pattern for periodic refresh; the concept of supplementing Config with SDK calls for richer metadata |
| aws-samples/amazon-neptune-aws-config-visualization | The Config CI JSON → graph transformation algorithm (lambda parser logic) |
| Steampipe | Nothing directly — but its 586-table coverage list is useful as a checklist of resource types we might want to enrich beyond Config |

---

## 4. Proposed Neptune Graph Schema (Live AWS Resources)

### 4.1 Node Labels

Distinct from code graph (#1529: `Symbol`, `File`, `Module`) and IaC graph (#1545: `IaCResource`, `IaCModule`, `IaCStack`, `IaCFile`):

| Label | Description | `~id` Encoding | Required Properties |
|---|---|---|---|
| `AwsResource` | A live deployed AWS resource | `live\|{account_id}\|{resource_type}\|{resource_id}` | `arn`, `account_id`, `region`, `service`, `resource_type`, `resource_id`, `name`, `discovered_at`, `monthly_cost` |
| `AwsAccount` | An AWS account in scope | `live\|account\|{account_id}` | `account_id`, `alias` |
| `AwsRegion` | A region (per-account) | `live\|region\|{account_id}\|{region}` | `account_id`, `region` |

**Namespace isolation:** All live-resource node IDs are prefixed with `live|` — guaranteed not to collide with code graph nodes (`{repo}|...`) or IaC nodes (TBD #1545, likely `iac|...`).

### 4.2 Edge Types

| Type | Source → Target | Maps To Config Relationship | Description |
|---|---|---|---|
| `CONTAINS` | AwsResource → AwsResource | `contains` | VPC contains subnet, account contains VPC |
| `CONTAINED_IN` | AwsResource → AwsResource | `is_contained_in` | Inverse of CONTAINS (for directional traversal) |
| `ASSOCIATED_WITH` | AwsResource → AwsResource | `is_associated_with` | EC2↔SecurityGroup, Lambda↔IAM Role |
| `ATTACHED_TO` | AwsResource → AwsResource | `is_attached_to` | EBS volume attached to EC2, ENI attached to instance |
| `IN_ACCOUNT` | AwsResource → AwsAccount | (structural) | Resource lives in account |
| `IN_REGION` | AwsResource → AwsRegion | (structural) | Resource lives in region |

**Cross-graph linkage edges (live ↔ IaC reconciliation):**

| Type | Source → Target | Description |
|---|---|---|
| `REALIZED_BY` | IaCResource (#1545) → AwsResource (live) | The declared IaC resource maps to this live resource |
| `MANAGED_BY` | AwsResource (live) → IaCResource (#1545) | Inverse: live resource is managed by this IaC |
| `UNMANAGED` | AwsResource (self-edge marker property) | Live resource with no matching IaC declaration (drift candidate) |

### 4.3 Cost Enrichment (CUR Join)

- **Join key:** `line_item_resource_id` in CUR → `resource_id` property on `AwsResource` node
- **Stored as node properties:** `monthly_cost` (Float), `daily_cost` (Float), `cost_updated_at` (String/datetime)
- **Gap acknowledgment:** ~30-50% of CUR line items have blank `resource_id` (aggregate charges, taxes, data transfer, API requests). These cannot be attributed to specific graph nodes. Cost is best-effort enrichment, not complete.
- **Update cadence:** CUR is delivered daily (or hourly for hourly reports). Cost properties updated on each graph refresh via a post-load enrichment step.

### 4.4 Node ID Encoding

```
~id format: live|{account_id}|{resource_type}|{resource_id}

Examples:
  live|123456789012|AWS::EC2::Instance|i-0abc1234def567890
  live|123456789012|AWS::EC2::VPC|vpc-0abc1234
  live|123456789012|AWS::IAM::Role|MyServiceRole
  live|account|123456789012
  live|region|123456789012|us-east-1
```

This encoding:
- Guarantees uniqueness (account + type + ID is unique within AWS)
- Enables scoped deletion: `MATCH (n:AwsResource {account_id: $acct}) DETACH DELETE n`
- Avoids collision with code graph (`{repo}|...`) and IaC graph (`iac|...`)

---

## 5. Pipeline Architecture (Reusing #1529 Infrastructure)

```
┌─────────────────────────────────────────────────────────────────┐
│ Ingestion (Periodic — CronJob or SQS-triggered)                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. AWS Config Snapshot (S3)                                    │
│     └── OR: Config Aggregator Advanced Queries                  │
│     └── OR: BatchGetResourceConfig API (per-resource-type)      │
│                                                                 │
│  2. Parse Config CIs → Extract:                                 │
│     - Resource nodes (AwsResource, AwsAccount, AwsRegion)       │
│     - Relationship edges (CONTAINS, ASSOCIATED_WITH, etc.)      │
│                                                                 │
│  3. Enrich with supplemental describe-* APIs (optional):        │
│     - ec2:DescribeSecurityGroups (for SG rule detail)           │
│     - elasticloadbalancing:DescribeTargetGroups (for routing)   │
│     - rds:DescribeDBClusters (for cluster membership)           │
│                                                                 │
│  4. Generate Neptune CSV (vertices.csv + edges.csv)             │
│     └── Same format as #1529: openCypher with ~id encoding      │
│     └── Reuse DictWriter pattern from extract_falkordb_to_csv.py│
│                                                                 │
│  5. Upload CSV to S3 staging path:                              │
│     s3://{bucket}/neptune-bulk-load/aws-resources/{acct}/{ts}/  │
│                                                                 │
│  6. Scoped delete (clear stale state):                          │
│     MATCH (n:AwsResource {account_id: $acct}) DETACH DELETE n   │
│                                                                 │
│  7. Neptune Bulk Load (or batched UNWIND MERGE via HTTP):       │
│     └── Reuse load_csv_to_neptune.py directly                   │
│                                                                 │
│  8. CUR Cost Enrichment (post-load):                            │
│     - Read CUR parquet from costvisibility11 bucket              │
│     - Match line_item_resource_id → graph resource_id           │
│     - UPDATE node properties: monthly_cost, daily_cost          │
│     - Via batched MERGE SET over openCypher HTTP                 │
│                                                                 │
│  9. Cross-link to IaC graph (#1545):                            │
│     - Match live ARN/name → IaC resource declarations           │
│     - Create REALIZED_BY/MANAGED_BY edges                       │
│     - Flag UNMANAGED resources (no IaC match)                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 5.1 Existing Code Reuse

| Component | Path | Reuse Strategy |
|---|---|---|
| `discover-infra.py` | `images/ingestion/discover-infra.py` | Reuse cross-account assume-role logic (lines 84-105) and Resource Explorer queries for supplemental node discovery |
| `correlate.py` | `images/ingestion/correlate.py` | Lift `correlate_resources()` pattern (lines 39-96) for REALIZED_BY edge generation |
| `extract_falkordb_to_csv.py` | `pipeline/neptune_ingestion/extract_falkordb_to_csv.py` | Reuse CSV DictWriter pattern, `~id` encoding logic, validation gates (unique ID check, dangling endpoint check) |
| `load_csv_to_neptune.py` | `pipeline/neptune_ingestion/load_csv_to_neptune.py` | **Directly reusable** — SigV4-authenticated openCypher HTTP loader, batched UNWIND MERGE. Proven in SPIKE-3 |
| `config.py` Settings | `images/ingestion/config.py` | Add new settings: `aws_config_enabled`, `cur_bucket`, `cur_prefix` |
| Neptune Terraform module | `terraform/modules/neptune-serverless/` | No changes — same cluster serves all graph domains |
| IAM module | `terraform/modules/iam/main.tf` | Add Config/CUR read permissions (see §6) |
| S3ContentStore | `images/ingestion/s3_store.py` | Reuse for reading Config snapshots from S3 |

### 5.2 New Code Required

| Component | Description | Complexity |
|---|---|---|
| Config CI parser | Parse Config JSON → extract resources + relationships | Medium (JSON parsing, well-documented format) |
| Neptune CSV generator (live resources) | Transform parsed CIs into vertices.csv + edges.csv with `live|...` IDs | Low (adapts existing CSV writer pattern) |
| CUR enrichment step | Read CUR parquet, match resource_ids, generate MERGE SET statements | Medium (parquet parsing + batched openCypher writes) |
| Live↔IaC cross-linker | Match live resources to IaC declarations, create edges | Low (extends existing `correlate.py` pattern) |
| New config settings | `aws_config_enabled`, `config_snapshot_bucket`, `cur_bucket` | Trivial |
| IAM additions | Config:Get*/Batch*, cur:GetObject, ce:GetCostAndUsage | Low (Terraform) |

---

## 6. IAM Permissions Required

### 6.1 Already Available (no changes needed)

From `modules/agent-context/terraform/modules/iam/main.tf`:
- Neptune access: `neptune-db:*` (conditional on `neptune_enabled`) — lines 198-214
- Resource Explorer: `resource-explorer-2:Search/GetView/ListViews` — lines 152-174
- S3 CRUD on platform data bucket — lines 78-102
- STS:AssumeRole for cross-account — used by `discover-infra.py` (runtime, not in Terraform policy)

### 6.2 New Permissions Required

| Permission | Purpose | Resource Scope |
|---|---|---|
| `config:BatchGetResourceConfig` | Read Config CIs in bulk | `*` (Config has no resource-level ARN scoping) |
| `config:GetResourceConfigHistory` | Read resource history/relationships | `*` |
| `config:SelectAggregateResourceConfig` | Advanced Queries on Aggregator | `arn:aws:config:*:{account}:config-aggregator/*` |
| `config:DescribeConfigurationRecorders` | Verify Config is recording | `*` |
| `s3:GetObject` on Config snapshot bucket | Read Config delivery S3 bucket | `arn:aws:s3:::config-bucket-{account}/*` |
| `s3:GetObject` on CUR bucket | Read CUR parquet files | `arn:aws:s3:::costvisibility11/*` |
| `ce:GetCostAndUsage` | Fallback cost enrichment via Cost Explorer API | `*` |
| `sts:AssumeRole` (explicit) | Cross-account discovery | `arn:aws:iam::*:role/AgentContextReadOnly` (or scoped list) |

**Note:** The STS:AssumeRole permission is already used at RUNTIME by `discover-infra.py` (line 92) but is NOT explicitly granted in the Terraform IAM module. This is a pre-existing gap — the pod likely gets it from the broader IRSA role or node instance profile. Should be made explicit.

---

## 7. AWS Config Relationship Details

### 7.1 Relationship Types and Coverage

Config records four relationship predicates. Examples from real Config CIs:

| Relationship Name | Example | Direction |
|---|---|---|
| `contains` | VPC contains Subnet, Subnet contains ENI | Parent → Child |
| `is_contained_in` | Subnet is_contained_in VPC | Child → Parent |
| `is_associated_with` | EC2 Instance is_associated_with SecurityGroup | Peer ↔ Peer |
| `is_attached_to` | EBS Volume is_attached_to EC2 Instance | Component → Host |

### 7.2 Resource Types with Rich Relationships (sampled)

| Resource Type | Typical Relationships |
|---|---|
| `AWS::EC2::Instance` | SGs, Subnet, VPC, EBS volumes, ENIs, IAM instance profile |
| `AWS::EC2::SecurityGroup` | VPC, instances, ENIs, RDS, Lambda, ELB |
| `AWS::EC2::VPC` | Subnets, IGWs, route tables, NACLs, SGs |
| `AWS::EC2::Subnet` | VPC, route table, NACL, instances, ENIs |
| `AWS::RDS::DBInstance` | Subnet group, SGs, VPC, parameter groups |
| `AWS::Lambda::Function` | IAM role, VPC, SGs, subnets, layers |
| `AWS::ECS::Service` | Cluster, task definition, target groups, SGs |
| `AWS::ElasticLoadBalancingV2::LoadBalancer` | VPC, subnets, SGs, target groups |
| `AWS::IAM::Role` | Policies (attached/inline) |

### 7.3 Access Method Recommendation

**For periodic batch processing (our use case):**

1. **Primary: Config Snapshot delivery to S3** (preferred for scale)
   - Config delivers full snapshots to an S3 bucket (configurable frequency)
   - Each snapshot is a JSON file per resource type per region
   - Parse offline — no rate-limiting, no API throttling
   - Best for: full-account refresh every 6-24 hours

2. **Alternative: `BatchGetResourceConfig` API** (for targeted refresh)
   - Pull specific resource types on demand
   - Good for: delta updates when Config SNS notifications fire
   - Rate-limited; use with backoff

3. **For cross-account: Config Aggregator**
   - Centralized query across all member accounts
   - `SelectAggregateResourceConfig` for Advanced Queries
   - Eliminates per-account assume-role complexity for the query layer

---

## 8. CUR Cost Enrichment

### 8.1 Join Key Behavior

| Fact | Detail |
|---|---|
| CUR column | `line_item_resource_id` |
| Format | Varies by service: instance ID, ARN, bucket name, function name |
| Blank rate | ~30-50% of line items (taxes, support, aggregate data transfer, API requests, account fees) |
| Report location | S3 bucket `costvisibility11` (already known to exist per issue body) |
| Format | Parquet (compressed columnar — efficient for resource_id aggregation) |
| Update frequency | Daily (or hourly if configured) |

### 8.2 Enrichment Algorithm

```python
# Pseudocode for CUR cost enrichment
for each resource_id in CUR where resource_id != '':
    monthly_cost = SUM(line_item_unblended_cost) for last 30 days
    daily_cost = SUM(line_item_unblended_cost) for yesterday
    
    # Match to graph node
    node_id = f"live|{account_id}|{resource_type}|{resource_id}"
    # Note: resource_type must be derived from CUR's product/servicecode + resource_id format
    
    # Write to Neptune via MERGE SET
    MERGE (n:AwsResource {resource_id: $resource_id, account_id: $account_id})
    SET n.monthly_cost = $monthly_cost, n.daily_cost = $daily_cost, n.cost_updated_at = $now
```

### 8.3 Resource ID Mapping Challenge

CUR `line_item_resource_id` and Config `resourceId` don't always use the same format:
- EC2: both use `i-0abc...` ✅ (direct match)
- S3: CUR uses bucket ARN, Config uses bucket name → need normalization
- Lambda: CUR uses function name, Config uses function ARN → extract name from ARN
- RDS: both use DB instance identifier ✅

**Recommendation:** Build a normalization layer that extracts the bare resource identifier from both CUR and Config formats, then joins on that. Accept that ~30-50% of cost will be "unattributable" (account-level charges).

---

## 9. Live-vs-Declared Reconciliation (Drift Detection)

### 9.1 Linkage Strategy

The key question: how do live-asset nodes (this issue) link to #1545's IaC-declared nodes?

**Three matching strategies (in priority order):**

1. **ARN/ID matching** (highest confidence)
   - Terraform state (`terraform.tfstate`) stores the ARN of every created resource
   - If #1545 extracts resource ARNs from state, the join to live nodes is trivial: `IaCResource.arn == AwsResource.arn`
   - Requires access to Terraform state (may not always be available)

2. **Tag-based matching** (high confidence)
   - Resources tagged with `ManagedBy=terraform` or carrying `aws:cloudformation:stack-name`
   - Config CIs include resource tags
   - Works even without state access

3. **Name/type heuristic matching** (medium confidence)
   - The existing `correlate.py` (lines 64-74) already does this: extract resource name from ARN, match against IaC resource declarations by name
   - Produces false positives for common names; use as fallback only

### 9.2 Drift Detection (falls out naturally)

Once live↔declared edges exist:
- **Managed resources:** Have a `REALIZED_BY`/`MANAGED_BY` edge to an IaC node → Config-actual matches IaC-declared
- **Unmanaged resources:** Live resources with NO IaC edge → drift candidates (created manually or by another tool)
- **Orphaned declarations:** IaC nodes with no live resource → either not yet applied, or resource was deleted outside IaC

A simple query:
```cypher
// Find unmanaged resources in an account (drift candidates)
MATCH (r:AwsResource {account_id: $acct})
WHERE NOT (r)<-[:REALIZED_BY]-()
RETURN r.arn, r.resource_type, r.name, r.monthly_cost
ORDER BY r.monthly_cost DESC
```

### 9.3 Prior Art

- The existing `correlate.py` in this repo is the closest prior art — it already produces `iac-creates` relationships between repos and live resources (lines 68-73)
- Terraform Cloud/Enterprise has a "drift detection" feature (but closed-source)
- `driftctl` (now part of Snyk) compares Terraform state to live resources — similar concept, different execution

---

## 10. Cross-Account Considerations

### 10.1 Current Pattern

`discover-infra.py` (lines 84-105) already implements cross-account discovery:
- Reads `accounts.txt` (format: `account_id:role_name:regions`)
- Calls `sts:AssumeRole` on each target account
- Uses temporary credentials for Resource Explorer queries

### 10.2 Extensions Needed

| Requirement | Current State | Needed |
|---|---|---|
| Assume role for Config reads | Pattern exists (Resource Explorer) | Same pattern; target role needs `config:*` read permissions |
| Config Aggregator (alternative) | Not used | Set up an org-wide aggregator in the platform account; eliminates per-account assume-role for queries |
| CUR access | CUR bucket likely in payer account | Cross-account S3 read OR use Cost Explorer API (supports linked accounts natively) |
| Target account role permissions | `AgentContextReadOnly` (assumed in discover-infra.py) | Must include `config:BatchGetResourceConfig`, `config:GetResourceConfigHistory` |

### 10.3 Recommendation

For Phase 1: Use the same assume-role pattern as `discover-infra.py` — read Config data per-account with temporary credentials. This is proven and simple.

For Phase 2 (scale): Evaluate deploying a Config Aggregator in the platform account. This provides a single query endpoint for all member accounts, eliminating the per-account role-assumption loop. Requires org-level Config setup.

---

## 11. Gaps and Risks

| Gap/Risk | Severity | Mitigation |
|---|---|---|
| Config's 4 relationship types are coarse (no routing/encryption/delegation semantics) | Medium | Supplement with describe-* API calls for richer edges in Phase 2. Phase 1: accept the 4 Config predicates as-is |
| CUR resource_id blank for ~30-50% of charges | Medium | Accept incomplete cost attribution. Document which resource types have reliable cost. Flag unattributable cost at the account level |
| Config recording must be enabled in target accounts | Low (usually already on) | Preflight check: verify Config recorder is active before attempting ingestion. Fail gracefully if not |
| No good OSS exists for "Config → Neptune" at production quality | Noted | The aws-samples PoC (MIT-0) is a reference, not production code. We build our own parser — straightforward given Config's well-documented CI format |
| Neptune Bulk Loader IAM role not yet implemented | High (blocks loading) | The S3 loader role (`adp-dev-neptune-s3-loader`) is specified in the design doc (D21) but NOT yet in Terraform. Must be added as part of #1529 Bulk Loader setup — this issue depends on it |
| Stale data between refreshes | Low | 6-24 hour refresh acceptable for Phase 1. Config change notifications (SNS) can trigger incremental updates in Phase 2 |
| Cross-graph edge explosion | Medium | Bound: only create REALIZED_BY edges where confidence > threshold. Don't create speculative edges |

---

## 12. Deployment Considerations (Future Implementation)

### 12.1 New Infrastructure

- **No new Neptune cluster** — reuses existing `adp-dev-agent-context-graphrag` cluster
- **Config Recorder** — must be enabled in target accounts (usually already is for compliance)
- **Config delivery channel** — snapshots to S3 (may need setup if not configured)
- **IAM policy additions** — Config read + CUR bucket read (Terraform change to IAM module)
- **New ingestion CronJob** — periodic Config parse + Neptune load (similar to existing `discover-infra.py` schedule)

### 12.2 Feature Flag

Add to `config.py`:
```python
live_resource_graph_enabled: bool = False
config_snapshot_bucket: str = ""
config_snapshot_prefix: str = "AWSLogs/"
cur_bucket: str = "costvisibility11"
cur_prefix: str = ""
```

Gate all new code behind `live_resource_graph_enabled` — mirrors the existing `graphrag_enabled` / `neptune_enabled` pattern.

### 12.3 Dependency Chain

```
#1529 (code call graph) — Neptune store + pipeline proven
  └── #1532 (bulk loader infra) — S3 loader role created
       └── #1545 (IaC graph) — IaC node schema defined
            └── #1546 (THIS — live resource graph) — builds on all above
```

**Do not start implementation until:** #1532 delivers the Neptune S3 Bulk Loader role, AND #1545 defines the IaC node schema (needed for cross-graph linkage).

---

## 13. Validation Strategy (Future Implementation)

### 13.1 Smoke Test

Index a known account (e.g., the ADP dev account itself), then query:
```cypher
// Count live resources
MATCH (n:AwsResource {account_id: '123456789012'}) RETURN count(n)

// Verify relationships exist
MATCH (v:AwsResource {resource_type: 'AWS::EC2::VPC'})-[:CONTAINS]->(s:AwsResource {resource_type: 'AWS::EC2::Subnet'})
WHERE v.account_id = '123456789012'
RETURN v.resource_id, collect(s.resource_id)

// Cost enrichment check
MATCH (n:AwsResource {account_id: '123456789012'})
WHERE n.monthly_cost > 0
RETURN n.resource_type, n.name, n.monthly_cost
ORDER BY n.monthly_cost DESC LIMIT 10
```

### 13.2 Regression Checks

- Existing code graph queries (#1529) must still work unchanged
- `understand` and `impact` verbs must not see AwsResource nodes (different label, filtered by verb logic)
- ACL enforcement: live resources must be scoped by account/tenant, not globally visible

---

## 14. Summary of Recommendations

1. **Adopt Option C:** AWS Config snapshots → custom parser → Neptune CSV → Bulk Loader
2. **Study but don't adopt:** Cartography's data model (reference), Workload Discovery (retiring), Steampipe (no relationships)
3. **Reuse extensively:** `load_csv_to_neptune.py`, CSV writer pattern, `discover-infra.py` cross-account logic, `correlate.py` matching logic
4. **Graph schema:** `AwsResource`/`AwsAccount`/`AwsRegion` nodes with `live|` prefix IDs; Config's 4 relationship predicates as edge types
5. **Cost enrichment:** CUR join on `line_item_resource_id` → node properties, accepting ~30-50% unattributable gap
6. **Sequence:** Wait for #1532 (Bulk Loader role) and #1545 (IaC schema) before implementation
7. **Phase 1 scope:** Single-account, Config snapshot → Neptune, CUR cost join, basic live↔IaC linkage
8. **Phase 2 scope:** Multi-account via Config Aggregator, supplemental describe-* enrichment, Config change notification for incremental updates
