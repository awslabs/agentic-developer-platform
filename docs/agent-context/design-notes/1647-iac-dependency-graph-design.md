# IAC-0 Design Authority: IaC Infrastructure Dependency Graph (Terraform-first → Neptune)

**Issue**: #1647
**Status**: Design authority (IAC-0) — no implementation yet
**Author**: @agent-architect
**Date**: 2026-06-19
**Parent EPIC**: #1345 (Knowledge Layer)
**Sibling**: #1529 (code graph EPIC — the pattern to mirror)
**Research**: #1545 (tool landscape — python-hcl2/MIT chosen for Terraform)

---

## 1. Summary of Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Graph model** | Distinct labels (`:InfraResource`, `:InfraModule`) + `iac:` prefix on `~id` | Coexistence without collision in shared Neptune cluster |
| **Door surface** | Extend existing `impact` verb with infra traversal | Single verb, lower cognitive load for agents; same ACL/fail-closed contract |
| **Pipeline shape** | `iac_terraform_parser.py → iac_neptune_csv.py → reuse scip_neptune_loader.py` | Mirrors SCIP path 1:1; reuse loader verbatim |
| **Neptune lessons** | collect(node) + Python projection; NeptuneQueryError (never silent []) | Baked into query function design from day 1 |

---

## 2. Normalized Graph Model

### 2.1 Node Labels and Properties

All infra nodes use distinct Neptune labels from code nodes (`:Symbol`). They coexist in the same Neptune cluster, joinable when needed but not colliding.

#### `:InfraResource` (primary node type)

| Property | Type | Description | Example |
|----------|------|-------------|---------|
| `~id` | String | **Prefix: `iac:`** + `{repo_safe}\|{address_hash}` | `iac:aws-e-adp\|a1b2c3d4e5f6g7h8` |
| `address` | String | Terraform resource address (type.name) | `aws_iam_role.agent_runner` |
| `resource_type` | String | Resource type | `aws_iam_role` |
| `name` | String | Resource local name | `agent_runner` |
| `provider` | String | Provider name | `aws` |
| `file` | String | Relative file path within repo | `platform/infra/modules/iam/main.tf` |
| `line` | Int | 1-indexed line where resource block starts | `42` |
| `repo` | String | Repository identifier (org/repo) | `aws-e/adp` |
| `module_path` | String | Terraform module path (empty for root) | `module.iam` |

#### `:InfraModule` (grouping node)

| Property | Type | Description | Example |
|----------|------|-------------|---------|
| `~id` | String | `iac:{repo_safe}\|mod\|{module_path_hash}` | `iac:aws-e-adp\|mod\|c3d4e5f6` |
| `module_path` | String | Full module address | `module.iam` |
| `source` | String | Module source (registry, local, git) | `./modules/iam` |
| `file` | String | File declaring the module call | `platform/infra/main.tf` |
| `line` | Int | Line of the `module "..."` block | `15` |
| `repo` | String | Repository identifier | `aws-e/adp` |

#### `:InfraProvider` (provider version tracking)

| Property | Type | Description | Example |
|----------|------|-------------|---------|
| `~id` | String | `iac:{repo_safe}\|prov\|{provider_hash}` | `iac:aws-e-adp\|prov\|d4e5f6g7` |
| `provider_name` | String | Provider source name | `hashicorp/aws` |
| `version_constraint` | String | Version constraint string | `~> 5.0` |
| `repo` | String | Repository identifier | `aws-e/adp` |

### 2.2 Edge Types

| Edge Label | From | To | Meaning | Source in HCL |
|------------|------|-----|---------|---------------|
| `DEPENDS_ON` | `:InfraResource` | `:InfraResource` | Resource depends on another | Explicit `depends_on` + implicit `${...}` / `.id` / `.arn` interpolation references |
| `DECLARED_IN` | `:InfraResource` | `:InfraModule` | Resource is declared inside this module | Module path prefix match |
| `USES_MODULE` | `:InfraModule` | `:InfraModule` | Module calls another module | `module "x" { source = ... }` |
| `USES_PROVIDER` | `:InfraResource` | `:InfraProvider` | Resource uses this provider | `provider` field or type prefix (`aws_*` → `hashicorp/aws`) |

#### Edge `~id` encoding

Same pattern as SCIP edges but with `iac:` prefix:
```
iac:e|{from_hash}|{edge_label}|{to_hash}
```
Where `from_hash` and `to_hash` are the first 10 chars of SHA-256 of the source/target `~id`.

### 2.3 Isolation from Code Graph

| Mechanism | Code Graph | Infra Graph |
|-----------|-----------|-------------|
| **Node labels** | `:Symbol` | `:InfraResource`, `:InfraModule`, `:InfraProvider` |
| **`~id` prefix** | `{repo_safe}\|{moniker_hash}` | `iac:{repo_safe}\|{address_hash}` |
| **Edge labels** | `CALLS`, `REFERENCES` | `DEPENDS_ON`, `DECLARED_IN`, `USES_MODULE`, `USES_PROVIDER` |
| **Edge `~id` prefix** | `e\|...` | `iac:e\|...` |
| **repo property** | Same format (`org/repo`) | Same format (`org/repo`) — enables future cross-graph join |

**No label collision is possible**: Neptune labels are disjoint sets, `~id` prefixes prevent ID collision, edge labels are entirely different vocabularies. A single openCypher query can still join across both graphs via `repo` + `file` properties (e.g., "which code symbols are in the same file as this infra resource") — this is intentional for future blast-radius integration.

### 2.4 Per-repo Isolation (Scoped Delete)

Mirrors the SCIP pipeline's `MATCH (n {repo: $r}) DETACH DELETE n` pattern (design D16):
```cypher
MATCH (n:InfraResource {repo: $repo}) DETACH DELETE n
MATCH (n:InfraModule {repo: $repo}) DETACH DELETE n
MATCH (n:InfraProvider {repo: $repo}) DETACH DELETE n
```
Run before re-ingestion to ensure idempotent re-indexing.

---

## 3. Door Surface: Extend `impact` Verb

### 3.1 Decision: Extend, Do Not Fork

**Choice**: Extend the existing `impact` verb to handle infra resources.
**Rejected alternative**: A separate `infra_impact` verb.

**Rationale**:
1. **Single mental model for agents**: "impact" means "what depends on this thing" regardless of whether "this thing" is a function, a class, or a Terraform resource. Agents shouldn't need to know which verb to call.
2. **Same ACL contract**: Both code and infra are gated by the same `X-GitHub-Login` + Postgres `allowed_principals` check — no new auth surface.
3. **Same fail-closed behavior**: If Neptune is unreachable, infra impact returns no data (same as code impact). No silent fallback to a different data source.
4. **Future joinability**: When code+infra graphs coexist, a single `impact("aws_iam_role.agent_runner")` can traverse both `DEPENDS_ON` (infra) and `CALLS` (code that references this role's ARN) in one query.
5. **Lower tool count**: MCP tool proliferation degrades agent performance. Six tools is already the ceiling stated in `server.py`.

### 3.2 Target Resolution

The `impact` verb's existing target format is `repo_id::symbol` or `repo_id/file`. For infra:

| Target format | Resolves to | Example |
|---------------|-------------|---------|
| `repo_id::aws_iam_role.agent_runner` | Match `:InfraResource` by `address` | `aws-e/adp::aws_iam_role.agent_runner` |
| `repo_id::module.iam` | Match `:InfraModule` by `module_path` | `aws-e/adp::module.iam` |
| `repo_id/platform/infra/main.tf` | All `:InfraResource` nodes with `file` match | File-level infra impact |

**Resolution logic** (added to `_impact_via_neptune` in `structural_backend.py`):
1. Try existing code-graph resolution (`:Symbol` match by name/file).
2. If no code symbol found, try infra resolution: `MATCH (t:InfraResource {repo: $repo, address: $target})`.
3. If still no match, try module resolution: `MATCH (t:InfraModule {repo: $repo, module_path: $target})`.
4. Return empty + `verdict: "symbol_not_found"` if neither resolves.

This "code-first, infra-fallback" order preserves backward compatibility — existing impact queries continue to work exactly as before.

### 3.3 Infra Impact Query

```cypher
MATCH (target:InfraResource {repo: $repo, address: $address})
WITH target
MATCH path = (dependent:InfraResource)-[:DEPENDS_ON*1..4]->(target)
WHERE dependent <> target
RETURN dependent.repo AS dep_repo,
       dependent.file AS dep_file,
       dependent.address AS dep_address,
       dependent.resource_type AS dep_type,
       dependent.name AS dep_name,
       length(path) AS distance
ORDER BY distance ASC, dep_repo, dep_file
LIMIT 100
```

**Key constraints** (mirroring code impact):
- Max traversal depth: `*1..4` (same as code's `[:CALLS*1..4]`)
- Result cap: `LIMIT 100` (same as code)
- Ordered by distance (nearest dependents first)

### 3.4 Response Shape

Infra impact results use a distinct `source` field to distinguish from code results:

```python
{
    "source": "neptune-infra",
    "verdict": "has_dependents",  # or "no_dependents" | "target_not_found"
    "target": "aws_iam_role.agent_runner",
    "target_type": "infra_resource",
    "repo": "aws-e/adp",
    "dependents": [
        {
            "address": "aws_iam_role_policy_attachment.agent_runner_ecr",
            "resource_type": "aws_iam_role_policy_attachment",
            "file": "platform/infra/modules/iam/main.tf",
            "distance": 1
        },
        ...
    ],
    "count": 7,
    "bounded": true  # true = LIMIT 100 hit; actual blast radius may be larger
}
```

**`source: "neptune-infra"`** distinguishes from `source: "neptune"` (code graph) in the same verb response, making it trivial for agents and UIs to branch on provenance.

---

## 4. Pipeline Fit

### 4.1 Pipeline Shape (Mirroring SCIP)

```
Code path (proven):
  scip_indexer.py → scip_ingester.py → scip_neptune_csv.py → scip_neptune_loader.py → Neptune

Infra path (new, mirrors 1:1):
  iac_terraform_parser.py → iac_neptune_csv.py → scip_neptune_loader.py → Neptune
  ^^^^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^
  NEW (per-format)           NEW (normalize)     REUSE (verbatim)
```

### 4.2 File Layout

```
modules/agent-context/images/ingestion/
├── iac_terraform_parser.py    # NEW: python-hcl2 → IaCGraph dataclass
├── iac_neptune_csv.py         # NEW: IaCGraph → vertices.csv + edges.csv
├── scip_neptune_loader.py     # REUSE: loads any CSV into Neptune (unchanged)
├── discover-infra.py          # EXTEND: invoke iac_terraform_parser for repos
└── ...
```

### 4.3 `iac_terraform_parser.py` — Responsibilities

**Input**: Repository clone path (same as `scip_indexer.py` receives).
**Output**: `IaCGraph` dataclass (analogous to `SCIPGraph`).

```python
@dataclass
class IaCNode:
    """Single infrastructure resource/module/provider."""
    node_id: str       # Terraform address (e.g., "aws_iam_role.agent_runner")
    label: str         # "InfraResource" | "InfraModule" | "InfraProvider"
    resource_type: str # e.g., "aws_iam_role" (empty for modules/providers)
    name: str          # Local name (e.g., "agent_runner")
    provider: str      # Provider name (e.g., "aws")
    file: str          # Relative file path
    line: int          # 1-indexed line number
    repo: str          # org/repo
    module_path: str   # Module path context (empty for root module)
    # Module-specific
    source: str        # Module source (for InfraModule nodes)
    # Provider-specific
    version_constraint: str  # For InfraProvider nodes


@dataclass
class IaCEdge:
    """Directed dependency edge."""
    from_id: str       # Source node address
    to_id: str         # Target node address
    edge_label: str    # "DEPENDS_ON" | "DECLARED_IN" | "USES_MODULE" | "USES_PROVIDER"
    file: str          # File where reference occurs
    line: int          # Line of the reference


@dataclass
class IaCGraph:
    """Complete IaC dependency graph for one repository."""
    nodes: dict[str, IaCNode]  # address → node
    edges: list[IaCEdge]
    repo: str
    
    @property
    def node_count(self) -> int:
        return len(self.nodes)
    
    @property
    def edge_count(self) -> int:
        return len(self.edges)
```

**Parsing strategy** (python-hcl2):

1. **Discover `.tf` files**: Walk the repo, skip `.terraform/` directories.
2. **Parse each file**: `hcl2.load(open(f))` → dict. python-hcl2 v8.x returns a dict with keys like `"resource"`, `"module"`, `"data"`, `"variable"`, `"output"`, `"provider"`.
3. **Extract resources**: For each `resource[type][name]` block → create `:InfraResource` node.
4. **Extract modules**: For each `module[name]` block → create `:InfraModule` node.
5. **Extract providers**: For `required_providers` blocks → create `:InfraProvider` node.
6. **Resolve dependencies**:
   - **Explicit `depends_on`**: Direct edge creation.
   - **Implicit interpolation**: Regex scan of all string values for patterns matching `resource_type.resource_name` references (e.g., `aws_iam_role.agent_runner.arn`). Each match → `DEPENDS_ON` edge.
   - **Module references**: `module.X.output_name` → `DEPENDS_ON` from consuming resource to `:InfraModule`.
   - **`var.*` and `local.*`**: Trace through variables/locals to ultimate resource references where possible (best-effort; may miss complex expressions).
7. **Create DECLARED_IN edges**: Every resource → its enclosing module.
8. **Create USES_PROVIDER edges**: Every resource → its provider (inferred from type prefix).

**Fail-loud rule** (matching SCIP's behavior in `scip_ingester.py`): If a `.tf` file exists but the parser produces **zero nodes**, raise `ValueError` — distinguish "no TF files found" (skip) from "parse failure" (error).

### 4.4 `iac_neptune_csv.py` — Responsibilities

**Input**: `IaCGraph` dataclass.
**Output**: `CSVOutput` (same dataclass as `scip_neptune_csv.py` uses — reusable by loader).

CSV column format:

**vertices.csv:**
```
~id,~label,address:String,resource_type:String,name:String,provider:String,file:String,line:Int,repo:String,module_path:String,source:String,version_constraint:String
```

**edges.csv:**
```
~id,~from,~to,~label,file:String,line:Int,repo:String
```

**`~id` generation:**
```python
def _make_infra_node_id(address: str, repo: str) -> str:
    """iac:{repo_safe}|{address_hash}"""
    repo_safe = repo.replace("/", "-")
    address_hash = hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]
    return f"iac:{repo_safe}|{address_hash}"

def _make_infra_edge_id(from_id: str, to_id: str, edge_label: str) -> str:
    """iac:e|{from_hash}|{edge_label}|{to_hash}"""
    from_hash = hashlib.sha256(from_id.encode("utf-8")).hexdigest()[:10]
    to_hash = hashlib.sha256(to_id.encode("utf-8")).hexdigest()[:10]
    return f"iac:e|{from_hash}|{edge_label}|{to_hash}"
```

### 4.5 Integration into Orchestration

**How it slots in** (no forking existing pipeline):

1. **`discover-infra.py`** gains a new step: after AWS Resource Explorer discovery, invoke `iac_terraform_parser` for each repo that contains `.tf` files.
2. **`publish-ingestion.py`** already has content_type `"infra"` with steps `["discovery", "graphrag"]`. The "discovery" step will call the parser.
3. **`sqs-worker.py`** routes `content_type="infra"` to invoke the IaC pipeline (parser → CSV → loader).
4. **No change to SCIP pipeline**: Infra parsing runs independently for repos that have `.tf` files. A repo can have both code graph AND infra graph (they coexist via distinct labels/IDs).

**Trigger condition**: Parse IaC if `glob("**/*.tf", repo_path)` returns ≥1 file (excluding `.terraform/`).

### 4.6 Reuse of `scip_neptune_loader.py`

The loader already:
- Accepts `CSVOutput` with paths to vertex/edge CSVs
- Runs batched `UNWIND` → `MERGE` queries
- Handles IAM SigV4 auth
- Tests connectivity
- Tracks error counts

**No modification needed** for the infra path because:
- The loader uses generic `MERGE (n:{label} ...)` from the CSV `~label` column — `:InfraResource` etc. is just a different label value.
- Edge loading groups by `~label` column — `DEPENDS_ON` etc. works identically to `CALLS`.
- The `~id` prefix (`iac:`) is transparent to the loader — it's just a string.

**One pre-load step is added**: The scoped-delete query (Section 2.4) must run before loading infra CSVs for a repo. This mirrors how the SCIP loader clears `:Symbol` nodes for a repo before re-loading. Implement as a function in `iac_neptune_csv.py` that emits the delete queries, called by the orchestration before loader invocation.

---

## 5. Neptune Lessons Baked In

### 5.1 Bug #1611: No `collect({inline map})` in openCypher

**Constraint**: Neptune's openCypher implementation crashes (terminates connection with internal error) when `collect(DISTINCT { key: node.prop, ... })` is used inside aggregation.

**Infra design mitigation**: All infra queries that need to collect related nodes will:
1. Use `collect(DISTINCT dependent)` to collect node references.
2. Project properties in Python via `_nodes_to_dicts(nodes, keys)` — the same helper already in `neptune_client.py` (line 360).

**Example — infra "understand" query (module topology):**
```cypher
MATCH (m:InfraModule {repo: $repo, module_path: $module_path})
OPTIONAL MATCH (r:InfraResource)-[:DECLARED_IN]->(m)
RETURN m.module_path AS module_path,
       m.source AS source,
       m.file AS file,
       collect(DISTINCT r) AS resources
```
Then in Python:
```python
resources = _nodes_to_dicts(record["resources"], ["address", "resource_type", "name", "file", "line"])
```

### 5.2 Error Handling: NeptuneQueryError, Never Silent []

**Constraint**: The Door's structural_backend.py uses a pattern where `NeptuneQueryError` is raised on server errors, caught by the verb handler, and triggers S3 fallback. An empty `[]` means "query succeeded, no data" — NOT "query failed silently."

**Infra design mitigation**: All infra query functions in `neptune_client.py` will:
1. **Raise `NeptuneQueryError`** on any Neptune server/connection error.
2. **Return `[]`** only when the query succeeds but finds no matching nodes/edges.
3. **Never return `[]` on connection failure** — that would look like "no dependencies" and could lead agents to conclude a resource is safe to delete.

**Pattern** (infra query function):
```python
def query_infra_impact(repo: str, address: str) -> list[dict[str, Any]]:
    """Query transitive dependents of an infra resource.
    
    Returns list of dependent records on success (may be empty = no dependents).
    Raises NeptuneQueryError on server/connection failure.
    """
    driver = get_neptune_driver()
    if driver is None:
        raise NeptuneQueryError("Neptune driver not configured")
    
    cypher = """
        MATCH (target:InfraResource {repo: $repo, address: $address})
        WITH target
        MATCH path = (dep:InfraResource)-[:DEPENDS_ON*1..4]->(target)
        WHERE dep <> target
        RETURN dep.repo AS dep_repo, dep.file AS dep_file,
               dep.address AS dep_address, dep.resource_type AS dep_type,
               dep.name AS dep_name, length(path) AS distance
        ORDER BY distance ASC, dep_repo, dep_file
        LIMIT 100
    """
    try:
        with driver.session() as session:
            result = session.run(cypher, {"repo": repo, "address": address})
            return [dict(record) for record in result]
    except Exception as exc:
        log.error("Neptune infra impact query FAILED for %s in %s", address, repo, exc_info=True)
        raise NeptuneQueryError(f"Infra impact query failed for {address} in {repo}") from exc
```

### 5.3 `symbol_exists` Equivalent for Infra

The code graph distinguishes "symbol not found" from "no callers" (Bug #1587 fix). Infra must do the same:

```python
def infra_resource_exists(repo: str, address: str) -> bool:
    """Check if an infra resource exists in Neptune (without querying dependents)."""
    cypher = "MATCH (n:InfraResource {repo: $repo, address: $address}) RETURN count(n) AS cnt"
    ...
```

This enables the Door to return `verdict: "target_not_found"` vs `verdict: "no_dependents"`.

### 5.4 Connection Reuse

Infra queries reuse the same Neptune driver instance (`get_neptune_driver()`) as code queries. No separate connection pool — the existing pool (`max_pool=25`) is shared. This is intentional: infra and code queries are unlikely to contend because they're called from the same verb handler sequentially (code-first, then infra-fallback).

---

## 6. Sequencing Constraint

**IAC-1 implementation should be sequenced after the code arm reaches "agents actually use it"** (#1592 agent-wiring done). Rationale:
- Adding the infra arm on an unfinished code arm spreads effort across two partially-working systems.
- The code arm proves the patterns (loader, ACL, verb dispatch, error handling) end-to-end first.
- The infra arm then reuses those proven patterns with confidence.

**This design CAN proceed now** (and has). The first corpus (ADP's own 218 `.tf` files) is ready. Implementation waits.

---

## 7. First Corpus: ADP's Own Terraform

IAC-1 will parse ADP's own infrastructure as proof:

| Directory | Modules | Resource Count (est.) |
|-----------|---------|----------------------|
| `platform/infra/` | networking, iam, eks, ecr, codebuild, security_scans | ~50 resources |
| `modules/gateway/infra/` | ~12 modules (RDS, ALB, CloudFront, S3, etc.) | ~80 resources |
| `modules/agent-factory/infra/` | ~8 modules (Lambda, SQS, KEDA, etc.) | ~40 resources |
| `modules/agent-context/terraform/` | ~11 modules (Neptune, OpenSearch, S3, etc.) | ~35 resources |
| `modules/agent-factory/webhook-ingress/terraform/` | Lambda, API GW, SQS | ~15 resources |

**Expected graph size**: ~220 nodes (resources) + ~50 module nodes + ~10 provider nodes ≈ **280 nodes**, with an estimated 3-5 edges per resource → **~800-1200 edges**. This is smaller than the code graph (~7,500 edges for a single repo) but meaningful for blast-radius queries.

---

## 8. Verify Gate

The existing verify gate (per #1534) asserts `edges > 0` for the code graph. For infra:

**Assertion**: After IaC ingestion for a repo with `.tf` files, assert:
- `node_count > 0` (at least one `:InfraResource` node exists)
- `edge_count > 0` (at least one `DEPENDS_ON` edge exists)

**Where**: Same verify gate as code graph. Extended with:
```python
# After infra ingestion
infra_nodes = count_nodes_by_label(repo, "InfraResource")
infra_edges = count_edges_by_label(repo, "DEPENDS_ON")
if has_tf_files and infra_nodes == 0:
    raise VerifyGateError(f"Infra parse produced 0 nodes for {repo} (has .tf files)")
if has_tf_files and infra_edges == 0:
    log.warning("Infra parse produced 0 edges for %s — may be leaf resources", repo)
    # Warning, not error: a repo with only data sources / standalone resources may have 0 edges legitimately
```

---

## 9. Child Stories (Sequenced)

### IAC-0 ✅ Design Authority (this document)
- Finalize graph model, Door surface, pipeline fit, Neptune lessons.
- **Done**: This document.

### IAC-1: Terraform Parser → Neptune (implementation)
- **Scope**: `iac_terraform_parser.py` + `iac_neptune_csv.py` + orchestration wiring + first corpus (ADP's TF).
- **Depends on**: #1592 (agent-wiring) reaching "done" state — code arm proven.
- **Acceptance**: ADP's own TF parsed into `:InfraResource` nodes with `DEPENDS_ON` edges in Neptune. Verify gate passes.
- **New dependency**: `python-hcl2>=8.0.0` added to ingestion image.

### IAC-2: Door Infra Query + Verb Extension
- **Scope**: `query_infra_impact()` + `infra_resource_exists()` in `neptune_client.py`; resolution logic in `structural_backend.py`; ACL enforcement (same Postgres check); tests.
- **Depends on**: IAC-1 (data must exist in Neptune to query).
- **Acceptance**: `impact("aws-e/adp::aws_iam_role.agent_runner")` returns graph data with `source: "neptune-infra"`.

### IAC-3: Kubernetes via kubectl-graph (Phase 2 format)
- **Scope**: Second parser proving multi-format model. kubectl-graph (Apache-2.0) emits Cypher natively → adapt to our CSV format.
- **Depends on**: IAC-1 proven.
- **Acceptance**: K8s manifests parsed into `:InfraResource` (type=`kubernetes_deployment` etc.) with `DEPENDS_ON` edges.

### IAC-4+: CloudFormation / CDK / Pulumi (Phase 3)
- **Scope**: Remaining formats, demand-justified.
- **Not yet scoped**: Only proceed if real agent usage of IAC-1/2 demonstrates value.

---

## 10. Non-Goals

- **No unified IaC IR**: Research (#1545) confirmed none exists. We normalize per-format into our model, not build a standard.
- **No live cloud-asset graph**: That's #1546 (separate research, separate design note). This EPIC is IaC-source graph, not running-account graph. They're complementary — may later join in Neptune via resource ARN.
- **No policy/security scanning**: This is a dependency graph for comprehension and blast-radius, not a replacement for Checkov/tfsec.
- **No Terraform state parsing in Phase 1**: `terraform.tfstate` contains actual resource IDs but is a security-sensitive artifact. Phase 1 uses only `.tf` source files (HCL). State-file enrichment is a future enhancement.
- **No cross-repo infra joins in Phase 1**: Unlike code (where SCIP monikers enable cross-repo edges), infra resources are typically repo-local. Cross-repo infra relationships (e.g., "repo A's Lambda calls repo B's API Gateway") require runtime correlation (#1546), not source analysis.

---

## 11. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| python-hcl2 can't parse all HCL features (dynamic blocks, complex expressions) | Medium | Low — worst case: some edges missed | Fall back to regex extraction for complex expressions; log coverage metrics |
| `${...}` interpolation regex produces false-positive edges | Medium | Medium — phantom dependencies | Validate extracted references against known resource addresses in the same graph; discard unresolved refs |
| Large monorepo TF (500+ resources) hits Neptune UNWIND batch limits | Low | Low — slower ingestion | Batch size already configurable in `scip_neptune_loader.py` (default 50); can tune |
| Agents trust "no dependents" verdict from parse-failure (silent empty) | High if not mitigated | High — incorrect "safe to delete" | Mitigated: fail-loud parser + NeptuneQueryError + verify gate (Section 5.2, 8) |

---

## 12. Design Coverage Audit (Self-Check)

| Section | Status |
|---------|--------|
| Description | ✅ Covered in EPIC body |
| Impact Analysis | ✅ Covered in EPIC body |
| Design | ✅ This document |
| Deployment | ⚠️ Deferred to IAC-1 child story (EPIC-level: "see child issues") |
| Validation | ✅ Verify gate defined; acceptance criteria per child story |

---

## Appendix A: Example — ADP `platform/infra/main.tf`

Given ADP's root module:
```hcl
module "networking" { source = "./modules/networking" ... }
module "iam"        { source = "./modules/iam" ... }
module "eks"        { source = "./modules/eks" vpc_id = module.networking.vpc_id ... }
module "ecr"        { source = "./modules/ecr" ... }
```

Expected graph output:
- **Nodes**: `:InfraModule` for each (networking, iam, eks, ecr)
- **Edges**: `module.eks` → `DEPENDS_ON` → `module.networking` (via `module.networking.vpc_id` interpolation)
- **Edges**: `module.eks` → `USES_MODULE` → (itself, representing the source)

Inside `modules/iam/main.tf`:
```hcl
resource "aws_iam_role" "agent_runner" { ... }
resource "aws_iam_role_policy_attachment" "agent_runner_ecr" {
  role = aws_iam_role.agent_runner.name
  ...
}
```

Expected:
- **Nodes**: `:InfraResource` for both
- **Edges**: `aws_iam_role_policy_attachment.agent_runner_ecr` → `DEPENDS_ON` → `aws_iam_role.agent_runner` (via `.name` reference)

---

## Appendix B: python-hcl2 Output Shape

```python
import hcl2

with open("main.tf") as f:
    parsed = hcl2.load(f)

# parsed = {
#   "resource": [
#     {"aws_iam_role": [{"agent_runner": {...}}]},
#     {"aws_iam_role_policy_attachment": [{"agent_runner_ecr": {...}}]}
#   ],
#   "module": [
#     {"networking": [{"source": "./modules/networking", ...}]},
#     {"eks": [{"source": "./modules/eks", "vpc_id": "${module.networking.vpc_id}", ...}]}
#   ],
#   "terraform": [...],
#   "variable": [...],
#   "output": [...]
# }
```

The parser walks this structure to extract nodes and resolve interpolation references to edges. Line numbers come from python-hcl2's `__start_line__` / `__end_line__` metadata (available in v8.x with `hcl2.load(f, with_meta=True)`).
