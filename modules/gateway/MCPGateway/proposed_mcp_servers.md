# Proposed MCP Servers for Development & Data Engineering Gateway

This document lists the MCP servers proposed for integration behind the AgentCore Gateway, organized by domain. Each server is categorized by priority for rollout.

- **P0**: Include in initial gateway deployment
- **P1**: Add in second phase
- **P2**: Evaluate and add based on team demand

---

## Software Development

### Version Control & Code (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [GitHub MCP Server](https://github.com/github/github-mcp-server) | GitHub (official) | ~26.7k | PRs, issues, commits, code search, branch management, CI workflows | GitHub PAT / OAuth |
| [Git MCP Server](https://github.com/modelcontextprotocol/servers) | Official MCP | ~7.5k | Local git operations, branch management, history, merge assistance | None (local) |
| [GitLab MCP Server](https://gitlab.com) | GitLab | — | Merge requests, CI/CD pipeline inspection, code review | OAuth 2.0 |

### Browser Automation & Testing (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Microsoft (official) | ~26.8k | Cross-browser automation (Chromium, Firefox, WebKit), accessibility snapshots, UI testing | None |
| [Puppeteer MCP](https://github.com/anthropics/mcp-servers) | Official MCP | ~27.2k | Browser automation, screenshots, web scraping, form interaction | None |

### File System & Local Dev (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Filesystem MCP](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Secure file read/write/search with configurable access controls | None (local) |
| [MarkItDown MCP](https://github.com/microsoft/markitdown) | Microsoft | ~86.6k | Converts PDF, DOCX, XLSX, etc. to Markdown for LLM consumption | None |
| [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) | Community | — | Terminal access, app launching, file management, window control | None (local) |

### Containers & Infrastructure (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Docker MCP Server](https://hub.docker.com) | Docker | — | Container lifecycle, image building, Compose orchestration | None (local) |
| [Terraform MCP Server](https://github.com/hashicorp/terraform-mcp-server) | HashiCorp (official) | ~575 | Registry providers/modules, IaC automation, plan/apply workflows | None |
| [Kubernetes MCP Server](https://github.com/modelcontextprotocol/servers) | Community | — | Cluster monitoring, pod/service ops, deployment automation, scaling | Kubeconfig |

### Cloud Platforms (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [AWS MCP Servers (45+)](https://github.com/awslabs/mcp) | AWS Labs (official) | ~8.1k | EC2, S3, Lambda, CloudWatch, CDK, documentation, billing | IAM / AWS credentials |
| [Azure MCP Server](https://github.com/microsoft/azure-mcp-server) | Microsoft (official) | — | 40+ Azure services, Cosmos DB, Storage, AI Search, Key Vault | Entra ID |
| [Cloudflare MCP Suite](https://github.com/cloudflare/mcp-server-cloudflare) | Cloudflare (official) | — | Workers, KV, R2, D1, DNS, browser rendering (13 servers) | API Token |

### Observability & Debugging (P1)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Sentry MCP](https://github.com/getsentry/sentry-mcp) | Sentry (official) | ~173 | Error tracking, performance telemetry, issue prioritization | API Token |
| [Netdata MCP](https://github.com/netdata/netdata) | Netdata | ~77.7k | Metrics, logs, containers, processes, root cause analysis | API Key |
| [Digma MCP](https://digma.ai) | Digma | — | Runtime observability, performance bottlenecks, test flakiness from APM | API Key |

### Project Management & Collaboration (P1)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Atlassian MCP (Jira + Confluence)](https://github.com/sooperset/mcp-atlassian) | Community + Atlassian | ~3.1k | Issues, boards, sprints, Confluence pages, CQL search | OAuth 2.0 / API Token |
| [Linear MCP Server](https://github.com/jerhadf/linear-mcp-server) | Community | — | Issues, projects, teams, cycles via natural language | API Key |
| [Notion MCP Server](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Pages, databases, docs, project management | API Token |
| [Slack MCP Server](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Messages, channels, threads, workflow automation | OAuth / Bot Token |

### AI-Assisted Coding & Context (P1)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Context7](https://github.com/upstash/context7) | Upstash | ~45k | Version-specific docs and code examples from official sources | None |
| [Sequential Thinking MCP](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Breaks complex tasks into structured logical steps | None |
| [Serena MCP](https://github.com/oraios/serena) | Community | ~19.8k | Symbolic code operations via language servers, refactoring | None |
| [Claude Task Master](https://github.com/eyaltoledano/claude-task-master) | Community | ~25.3k | PRD parsing, task expansion, multi-provider AI task management | None |
| [Memory MCP](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Persistent context, entity relationships, cross-session memory | None (local) |
| [Knowledge Graph Memory](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Graph-based memory, entity relationships, codebase navigation | None (local) |

### Search & Research (P1)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Brave Search MCP](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Privacy-first web search, real-time results | API Key |
| [Perplexity MCP](https://github.com/perplexity-ai) | Perplexity (official) | — | Deep research, citation-rich results, recency filtering | API Key |
| [Exa MCP](https://github.com/exa-labs/exa-mcp-server) | Exa AI | — | Web + GitHub code search, company research | API Key |
| [Tavily MCP](https://github.com/tavily-ai/tavily-mcp) | Tavily (official) | — | Real-time search, content extraction, site mapping | API Key |

---

## Data Engineering

### Relational Databases (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [PostgreSQL MCP](https://github.com/modelcontextprotocol/servers) | Official MCP | — | SQL queries, schema inspection, data analysis, connection pooling | Connection string |
| [SQLite MCP](https://github.com/modelcontextprotocol/servers) | Official MCP | — | Lightweight local DB operations, prototyping, embedded analytics | None (local) |
| [DBHub (MySQL, Postgres, SQLite, DuckDB)](https://github.com/bytebase/dbhub) | Bytebase | — | Universal database MCP connecting multiple DB engines | Connection string |
| [Google MCP Toolbox for Databases](https://github.com/googleapis/genai-toolbox) | Google (official) | ~12.8k | Cloud SQL, AlloyDB, Spanner, Firestore | GCP credentials |

### Data Warehouses & Analytics Engines (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Snowflake MCP](https://github.com/Snowflake-Labs/mcp) | Snowflake Labs (official) | — | Cortex AI, SQL orchestration, semantic views, object management | Snowflake credentials |
| [BigQuery MCP](https://cloud.google.com) | Google (official) | — | Petabyte-scale analytics, natural language queries | GCP credentials |
| [Databricks MCP](https://www.databricks.com) | Databricks (official) | — | Unity Catalog, notebooks, SQL warehouses, agent workflows | Databricks token |
| [ClickHouse MCP](https://github.com/ClickHouse) | ClickHouse (official) | — | Columnar analytics, high-concurrency read-only queries | Connection string |
| [DuckDB MCP](https://github.com/motherduck-ai/duckdb-mcp) | MotherDuck | ~370 | In-process OLAP, local analytics | None (local) |
| [StarRocks MCP](https://github.com/StarRocks/mcp-server-starrocks) | StarRocks | ~80 | BI and large-scale analytics on StarRocks SQL engine | Connection string |

### NoSQL & Document Stores (P1)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [MongoDB MCP](https://github.com/mongodb-js/mongodb-mcp-server) | MongoDB (official) | ~202 | Document queries, Atlas integration, schema exploration | Connection string |
| [Supabase MCP](https://github.com/supabase/mcp) | Supabase (official) | — | 20+ tools: migrations, SQL, branching, auth, TypeScript types | API Key |

### Data Transformation & Modeling (P0)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [dbt MCP Server](https://github.com/dbt-labs/dbt-mcp) | dbt Labs (official) | ~240 | Semantic layer, project graph, CLI commands, governed data models | dbt Cloud token |
| [MindsDB MCP](https://github.com/mindsdb/mindsdb) | MindsDB | ~38.4k | Federated queries across 200+ sources (DBs, SaaS) via SQL/NL | API Key |

### Vector Databases / RAG (P1)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant) | Qdrant (official) | — | Semantic memory, vector search, metadata filtering for LLMs | API Key |
| [Pinecone MCP](https://github.com/sirmews/mcp-pinecone) | Community | — | Vector index read/write, multi-tenant isolation | API Key |
| [Cognee MCP](https://github.com/topoteretes/cognee) | Community | ~12k | Memory manager using graph + vector stores, 30+ data sources | API Key |

### Data Integration & Federation (P2)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Pipedream MCP](https://github.com/PipedreamHQ/pipedream) | Pipedream | ~11.1k | 2,500 APIs with 8,000+ prebuilt tools | API Key |
| [Filestash MCP](https://github.com/mickael-kerjean/filestash) | Community | ~13.5k | SFTP, S3, FTP, SMB, NFS, WebDAV, Git, Azure Blob, SharePoint | Various |

### Cloud Cost & Ops (P2)

| MCP Server | Maintainer | GitHub Stars | Key Capabilities | Auth Method |
|---|---|---|---|---|
| [Vantage MCP](https://github.com/vantage-sh/vantage-mcp-server) | Vantage | ~57 | Cloud cost visibility, billing trends, cost-saving recommendations | API Key |
| [Spark History Server MCP](https://aws.amazon.com/blogs/big-data/) | AWS | — | Spark application debugging and optimization | IAM |

---

## Rollout Summary

| Phase | Target Count | Domains |
|---|---|---|
| P0 (Initial) | ~20 servers | Version control, testing, filesystem, containers, cloud, databases, data warehouses, dbt |
| P1 (Phase 2) | ~15 servers | Observability, project management, AI coding, search, NoSQL, vector DBs |
| P2 (On Demand) | ~5 servers | Data integration, cloud cost, federation tools |

Total: ~40 MCP servers unified behind a single AgentCore Gateway endpoint.

---

## AgentCore Gateway Target Group Mapping

```
AgentCore Gateway (single endpoint)
│
├── Target: github-mcp          → GitHub MCP Server
├── Target: git-mcp              → Git MCP Server
├── Target: gitlab-mcp           → GitLab MCP Server
├── Target: playwright-mcp       → Playwright MCP Server
├── Target: puppeteer-mcp        → Puppeteer MCP Server
├── Target: filesystem-mcp       → Filesystem MCP Server
├── Target: markitdown-mcp       → MarkItDown MCP Server
├── Target: docker-mcp           → Docker MCP Server
├── Target: terraform-mcp        → Terraform MCP Server
├── Target: kubernetes-mcp       → Kubernetes MCP Server
├── Target: aws-mcp              → AWS MCP Servers
├── Target: azure-mcp            → Azure MCP Server
├── Target: cloudflare-mcp       → Cloudflare MCP Suite
├── Target: postgres-mcp         → PostgreSQL MCP Server
├── Target: sqlite-mcp           → SQLite MCP Server
├── Target: snowflake-mcp        → Snowflake MCP Server
├── Target: bigquery-mcp         → BigQuery MCP Server
├── Target: databricks-mcp       → Databricks MCP Server
├── Target: clickhouse-mcp       → ClickHouse MCP Server
├── Target: duckdb-mcp           → DuckDB MCP Server
├── Target: dbt-mcp              → dbt MCP Server
├── Target: mindsdb-mcp          → MindsDB MCP Server
├── Target: sentry-mcp           → Sentry MCP Server
├── Target: atlassian-mcp        → Jira + Confluence MCP
├── Target: notion-mcp           → Notion MCP Server
├── Target: slack-mcp            → Slack MCP Server
├── Target: context7-mcp         → Context7 MCP Server
├── Target: brave-search-mcp     → Brave Search MCP Server
├── Target: mongodb-mcp          → MongoDB MCP Server
├── Target: supabase-mcp         → Supabase MCP Server
├── Target: qdrant-mcp           → Qdrant MCP Server
├── Target: memory-mcp           → Memory MCP Server
└── Target: sequential-think-mcp → Sequential Thinking MCP
```
