# Knowledge Layer — Storage & Indexing Design

**Date:** 2026-06-11
**Written for:** humans (and the next agent). Plain language, no acronym soup.
**Status:** Design agreed in discussion. No code written yet.
**Companion docs:** `context-platform-design-session-handover-20260611T134708Z.md` (the big-picture two-layer model). This doc zooms into ONE thing: how we store and index *code* in the Knowledge layer.

---

## 1. What this document is about (in one paragraph)

The platform indexes source code so agents can search it, understand it, and reason about it. Today that's done with two third-party tools (Sourcebot and OpenViking) that we want to **remove** — one has a licensing problem, and both store their data in fragile ways. This document describes what replaces them: a simpler design built on **plain AWS services** (S3, a new thing called S3 Vectors, and PostgreSQL) that is cheaper, more durable, and uses open file formats. It also adds a new capability: generating a **software bill of materials (SBOM)** for every repo, which turns ADP into a tool that can find security vulnerabilities and fix them automatically.

If you read nothing else, read §2 and §3.

---

## 2. The big picture (the one diagram that matters)

We index each repository three different ways, because agents ask three different kinds of question:

```
   A REPO ON GITHUB
        │
        │  (clone it once)
        ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  We read the code and produce FOUR things:                   │
   │                                                              │
   │   1. Exact search index   "find this exact text/regex"       │
   │   2. Meaning search index "find code ABOUT this idea"        │
   │   3. Structure map        "what calls what / what breaks"    │
   │   4. Bill of materials    "what dependencies does it use"    │
   └─────────────────────────────────────────────────────────────┘
        │
        ▼
   STORED IN PLAIN AWS SERVICES (S3, S3 Vectors, PostgreSQL)
        │
        ▼
   AGENTS ASK QUESTIONS through one MCP server, and every answer is
   permission-checked (you only see repos you're allowed to see).
```

That's the whole thing. The rest of this document explains each piece.

---

## 3. What we're removing and why

| Removing | What it did | Why it's going | Replaced by |
|----------|-------------|----------------|-------------|
| **Sourcebot** | Exact/keyword code search | Its license forbids commercial competition — a problem for a product we sell. (The engine *inside* it, Zoekt, is free and fine.) | **Zoekt** directly (open-source, Apache-2.0) |
| **OpenViking** | Meaning-based search + a file browser + agent memory | Stores everything inside one pod on one disk — if the pod dies, it's fragile; can't enforce who-sees-what; not an open format | **S3 Vectors** (meaning search) + **S3** (files) + our own small code |

Both removals make the system **simpler**, not more complex. We end up running only ONE third-party indexing tool (Zoekt), and everything else is plain AWS plus a few hundred lines of our own code.

---

## 4. The four AWS stores (where things actually live)

Think of it as four filing systems, each good at one job:

| Store | Plain-English description | What we keep here |
|-------|---------------------------|-------------------|
| **S3 bucket** (mounted as files) | A giant, durable folder in the cloud. We can read/write files in it like a normal disk, using a tool called **Mountpoint**. | Search index files, structure maps, dependency lists, wikis |
| **S3 Vectors** | A new AWS service that stores "meaning fingerprints" of text and finds similar ones. Like a search engine for *ideas*, not words. | The meaning-search index |
| **PostgreSQL** (RDS) | A standard relational database. Good at structured records and "find all rows where…" queries. | The catalog (what's indexed), permissions (who can see what), and the dependency lookup |
| **Worker scratch disk** | A temporary local disk on each working machine. Thrown away when the job finishes. | The cloned repo and half-built index files, while we're working |

> **Why Postgres and not DynamoDB?** ADP's pods already talk to a PostgreSQL database (the gateway uses it, with migrations and IAM auth) — it's the proven, supported way for our code to reach a database from inside the cluster. DynamoDB is technically reachable but isn't a path we rely on today. Postgres is also a *better* fit for the dependency lookup, which is a relational question ("join components to repos to permissions"). So we use the database we already trust.

> **Where does this Postgres live?** We reuse the **existing gateway RDS instance** (PostgreSQL 16.6) but give agent-context its **own separate database** on it — not the gateway's tables. One instance can host many independent databases; the gateway keeps its database, agent-context gets a fresh `agent_context` database with its own tables and its own low-privilege login (the indexing pods don't use the master user). This reuses the instance, security group, IAM-auth plumbing, and migration tooling we already trust, with no new infrastructure. The two databases share physical CPU/memory, so if heavy indexing ever competes with gateway traffic, the escape hatch is to move agent-context onto its own instance later — and because it's already a self-contained database, that's a dump-and-restore, not a redesign. *(Implementation note, not a design blocker.)*

> **Postgres version:** Agent-context inherits whatever version the shared gateway instance runs — currently **PostgreSQL 16.6**, which has community support until **November 2028** (~2.5 years runway). PostgreSQL has no "LTS" tier; each major version gets a fixed 5-year support window. We don't pick a version independently — bumping it is a gateway-instance decision, and agent-context comes along when the gateway upgrades. 2.5 years is ample for this phase.

**Why a temporary scratch disk?** Some tools — `git` especially — need a real, fast, local disk with file-locking to work. Cloud file systems can't do that well. So each worker clones to its own local disk, builds everything there, then **uploads the finished results** to the durable stores and throws the scratch away. This is why removing OpenViking's "everything on one shared disk" approach is a win: nothing important lives on a disk that can disappear.

---

## 5. How indexing works, step by step (the write path)

This is what happens from "a repo needs indexing" to "it's searchable."

**Step 1 — Decide what needs work.**
A scheduler looks at the list of repos. For each one, it checks Postgres: "what was this repo's last version when we indexed it?" Then it checks GitHub: "what's its version now?" If nothing changed, skip it. If it changed, add it to a work queue (one message per repo).

> *Result: only repos that actually changed get re-indexed. For 500 repos, maybe only 10 changed today.*

**Step 2 — Spread the work across many machines.**
A system called **KEDA** watches the queue and starts one worker machine per repo, up to ~50–100 at once. Each worker handles exactly one repo and shuts down. This is how we index 500 repos quickly — they're done in parallel, not one after another.

> *Result: 500 repos finish in a few waves instead of taking hours in a line.*

**Step 3 — Clone the repo (once).**
The worker downloads the repo to its scratch disk. For private repos it uses a GitHub App token. **This is the only download** — everything below reads from this one copy. (Today, three different tools each download the same repo separately; we're fixing that.)

**Step 4 — Figure out who's allowed to see this repo.**
Using GitHub, the worker asks "who has access to this repo?" (which teams, which people) and saves that list. This is the permission list. We **mirror whatever GitHub already says** — we don't invent our own rules. Saved into Postgres.

**Step 5 — Build the four indexes from the one clone:**

- **5a. Exact search (Zoekt).** Build a searchable index of the literal text, upload it to S3 as a finished file.
- **5b. Structure map.** Analyze the code to list every function/class and what calls what. Save as a `.json` file in S3.
- **5c. Meaning search.** Break the code into pieces (ideally one function per piece), turn each piece into a "meaning fingerprint" using Amazon Bedrock, and store those fingerprints in S3 Vectors — each tagged with which repo it came from and who's allowed to see it.
- **5d. Bill of materials (SBOM).** List every outside dependency the repo uses (see §7). Save the list to S3 and record each dependency in Postgres so we can look it up later.

**Step 6 — Update the catalog.**
The worker records in Postgres: this repo's new version, what got indexed successfully, the permission list, and a log entry saying "indexed at this time, took this long." This catalog is used for three things: deciding what to re-index next time, listing what exists, and checking permissions.

**Step 7 — Clean up.**
The scratch disk is discarded. Everything valuable is now safely in S3, S3 Vectors, and Postgres.

---

## 6. How agents ask questions (the read path)

When an agent searches, here's what happens:

**Step 1 — The agent asks** (e.g. "how does the database connection get set up?"), and its identity comes along with the question.

**Step 2 — We search the right places:**
- For exact words → ask Zoekt.
- For meaning/concepts → turn the question into a fingerprint and ask S3 Vectors for similar code.

**Step 3 — We filter by permission.**
Every result says which repo it came from. We check: "is this agent allowed to see that repo?" If not, we drop the result. **If we can't tell who the agent is, we show nothing** — better safe than leaking.

**Step 4 — Return the allowed results,** with links back to the exact file and line, so the agent can read the real source.

The same permission check happens on *every* kind of question, always.

---

## 7. The bill of materials (SBOM) — and why it makes ADP a security product

### 7.1 What an SBOM is
A "software bill of materials" is just a list of all the outside code a project depends on — like an ingredients label on food. Example: "this repo uses `requests` version 2.28, `numpy` version 1.24," and so on.

### 7.2 Two ways to make one (we do both)
There are two layers of ingredients, and you need both to be safe:

| Type | What it sees | What it misses |
|------|--------------|----------------|
| **Source SBOM** (read the project's dependency files) | The app's own dependencies, in every repo | The operating-system packages inside a built container |
| **Image SBOM** (build the container, then inspect it) | OS packages + the base image + what's *actually installed* | Only works for repos that build into a container |

**Decision made:** We generate a **source SBOM for every indexed repo** (it's cheap and works everywhere), AND an **image SBOM for every repo that has a Dockerfile** (best-effort — if the build fails, we log it and move on). This gives the broadest possible coverage. Many open-source study repos won't build successfully, and that's fine — we record "couldn't build this one" honestly rather than pretending we scanned it.

We already use the right tool for this (**Syft**), so we're reusing it, not inventing.

### 7.2.5 The tools — and why their licenses matter
Because we intend to sell this platform, every tool in the security chain must be under a *permissive* license (Apache-2.0 or MIT) — so we can package and distribute it without legal strings. That rules out some popular tools (e.g. Grype, which we used before) in favour of clean equivalents:

| Job | Tool | License | Notes |
|-----|------|---------|-------|
| Make the SBOM | **Syft** (Anchore) | Apache-2.0 | already in use |
| Match SBOM → known vulnerabilities (packages) | **OSV-Scanner** (Google) | Apache-2.0 | precise, commit-level matching against the open OSV database; low false positives. *This replaces Grype.* |
| Match the OS/base-image layer | **Trivy** | Apache-2.0 | covers operating-system packages inside containers, which OSV-Scanner doesn't focus on |

**Why OSV-Scanner instead of running our own vulnerability database:** OSV-Scanner queries Google's open vulnerability database directly, so we don't maintain heavyweight threat-intelligence data ourselves. It's strongest for open-source ecosystem packages (npm, PyPI, Go, Cargo, etc.). It does *not* deeply cover OS/base-image CVEs — that's why we pair it with **Trivy** for the image layer. Both are Apache-2.0, so the whole chain stays commercially clean.

> **Note on a competing design:** another AI proposed a from-scratch version of this loop (a new FastAPI webhook + GitPython clone, a PostgreSQL dependency graph, and Llama-3 for fixes). We took TWO ideas from it — **OSV-Scanner for vulnerability matching** and **PostgreSQL for the dependency graph** (which matches both our existing pod-to-database pattern and the relational shape of the lookup). We declined the rest, because ADP already has better-suited pieces: the SQS/KEDA parallel pipeline (vs. a single clone service), a full repo clone reused across four indexers (vs. a manifest-only clone that would break code search), and Claude-based developer agents that fix-and-test with full repo context (vs. feeding a code snippet to a local model). See §9.

### 7.3 The clever part: a reverse lookup
A normal SBOM answers "what does repo X use?" But the question you actually want answered is the **reverse**: "**which repos use dependency Y?**" — and you want it answered *instantly*.

So we build a reverse index in Postgres. We record every (repo, dependency, version) combination as rows in a `dependencies` table, indexed on the dependency's coordinate. Then the question is one fast SQL lookup:

```
Ask:  "which repos use requests version 2.28?"
SQL:  SELECT repo FROM dependencies WHERE package = 'pkg:pypi/requests@2.28'
Get:  instantly → [repo-A, repo-B, repo-C, ... 14 repos]
```

This is a natural fit for a relational database: it's a join across components, repos, and permissions. The full SBOM files live in S3 (the official record); the Postgres index makes the question instant.

**The simple schema** (mirrors what the dependency engine needs — three tables):
- `repositories` — (id, repo_name, git_url, owner, allowed_principals)
- `dependencies` — (id, repo_id, package_coordinate, version, is_transitive, source: code|image)
- `vulnerabilities` — (id, cve_id, package, affected_versions, safe_version, details)

A global advisory ("lodash 4.17.20 is vulnerable") becomes one indexed query joining `vulnerabilities` → `dependencies` → `repositories`, returning every affected repo and who owns it.

### 7.4 The loop that makes ADP a vulnerability-management product
Once we know which repos use which dependencies, ADP can run the whole security cycle by itself:

```
1. DETECT   OSV-Scanner (packages) and Trivy (OS/image layer) match our
              SBOMs against known vulnerabilities.
              → Look the affected package up in the reverse index:
                "which of our repos use it?"
              → Instant answer: these 14 repos, these specific files.

2. CHECK    Confirm it's actually exploitable (not just present-but-unused),
              using the structure map to see if the vulnerable code is reached.

3. FIX      File a fix task for each affected repo.
              → ADP's existing developer agents pick it up, make the fix,
                and run the tests. (This part ALREADY EXISTS in the platform.)

4. VERIFY   Tests pass → open a pull request. Tests fail → the agent retries.

5. REMEMBER Record "we fixed CVE-X across 14 repos, tests passed" as a
              verified lesson (this feeds the Experience layer).
```

**Why this is a big deal:** steps 3–5 already exist in ADP (the agent-factory developer loop). The SBOM is the missing piece that points those agents at the right work. With it, ADP isn't just a search tool — it **finds vulnerabilities and fixes them automatically across your whole codebase.** That's a product, not a feature.

---

## 8. How permissions work (who can see what)

Short version: **the search engines don't enforce permissions — we do, at the MCP server (the single query surface).**

None of the storage tools (Zoekt, S3 Vectors) have built-in "this user can see this document" rules. So instead:

1. **When indexing:** we record each repo's permission list (copied from GitHub) in Postgres.
2. **When answering:** after the search returns results, we drop any result from a repo the asker isn't allowed to see.
3. **If we can't identify the asker:** we return nothing. (Fail safe, not fail open.)

For **personal data** (a specific user's private context, later), S3 Vectors offers something stronger: a *separate index per user*, so one user's data physically can't appear in another user's search. We'll use that when we build personal context.

> **Note:** Personal context is currently blocked on a separate problem — connecting a GitHub action back to a specific user account (tracked as issue #1319). Enterprise/shared code indexing does NOT need that fix and can ship first.

---

## 9. What we reuse vs. what we build new

Most of this already exists. We are mostly *rewiring*, not building from scratch.

| Piece | Reuse or new? |
|-------|---------------|
| Parallel work queue (SQS + KEDA) | **Reuse** — already built for ingestion |
| Cloning repos to disk | **Reuse** — already happens |
| Structure-map analysis | **Reuse** — already runs |
| Bedrock connection for fingerprints | **Reuse** — already wired |
| The catalog + dependency tables (Postgres) | **New tables, reused database** — Postgres + IAM auth is already how pods reach a DB; we add tables via the existing migration tooling |
| Syft (SBOM tool) | **Reuse** — already used for container scans |
| OSV-Scanner + Trivy (vuln matching) | **New** — both Apache-2.0; replace Grype for license cleanliness |
| Developer agents that fix code | **Reuse** — the whole agent-factory |
| Zoekt search (instead of Sourcebot) | **New** — but it's a simpler, smaller deployment |
| Storing meaning-fingerprints in S3 Vectors | **New** — replaces OpenViking's fragile storage |
| Breaking code into pieces + fingerprinting | **New** — a few hundred lines (was OpenViking's job) |
| Reverse dependency lookup | **New** — new Postgres tables + an index on the package coordinate |
| Permission filtering at the MCP server | **New** — the security gate |

---

## 10. Honest open questions (things we haven't decided)

1. **Is S3 Vectors fully released and available in our region?** It's documented in the main AWS guide and lists "navigating large code bases" as a use case, but we must confirm general-availability and region before committing. *(Action: verify before building.)*
2. **How do we break code into pieces for meaning-search?** Best option: one function/class per piece (we already compute those boundaries). Simpler fallback: fixed-size chunks. We lean toward per-function for quality. *(Decide at build time.)*
3. **Do we keep the `browse` feature** (walking the file tree) now that OpenViking is gone? It can be rebuilt cheaply from the catalog + S3, or dropped for v1 if agents don't really use it. *(Recommend: rebuild cheaply, low priority.)*
4. **Image-build cost control.** Building containers for every repo with a Dockerfile costs CI time, and many open-source repos won't build. We cap concurrency, set per-build timeouts, only build when the repo changed, and record failures as honest "coverage gaps." *(Guardrails agreed; tune the caps when we see real numbers.)*
5. **Consolidated SBOM scope** — do we merge a single corpus-wide bill, and does it mix our repos with the open-source study repos, or keep them separate? *(Still open — the reverse index works regardless; this is about the rolled-up report.)*
6. **Write speed limits on S3 Vectors.** One index accepts ~2,500 new fingerprints/second. With 50–100 workers running at once, we may need to split the index into shards so they don't bottleneck. *(Design the sharding in; it composes with the parallel workers.)*

---

## 12. Where we sit in the 2026 agentic-search spectrum (positioning + tier-priority revision)

By 2026 "agent-as-retriever" has fragmented into five recognised flavors. Placing ourselves honestly against them changes which tiers we should prioritise.

| Flavor (poster product) | Us? |
|---|---|
| **8.1 Pure agentic** — LLM drives grep/read/bash in a loop, no index (Claude Code, Devin) | ✅ This is our **agent-factory** (developer/ops agents). Already built and running. |
| **8.2 Hybrid lexical + semantic** — grep for symbols, embeddings for concepts, agent picks (Cursor, Sourcegraph Amp) | ✅ Our Knowledge layer as designed: **Zoekt + S3 Vectors**. "The path most enterprise tools converge on." |
| **8.3 Structural / AST-aware** — tree-sitter parses code, agent searches by structure; returns whole functions, not broken chunks (Cline, Probe, ast-grep) | ✅ Our **structural tier** (cgc/tree-sitter `code-index.json` + `understand`/`impact`). Cline's exact three-tier stack (ripgrep + fuzzy + AST) is what we designed. |
| **8.4 Specialised retrieval models** (Windsurf SWE-grep, Chroma Context-1) | ❌ Not us — general agents + indexes, no trained retrieval model. |
| **8.5 RL-trained retrieval policies** (Search-R1, CoSearch) | ❌ Not us — but our **Experience layer is the nearest cousin**: it learns *what worked* over time, via verified events + golden paths rather than RL. |

So our architecture is a **2 + 3 hybrid with an 8.1 loop on top** — the mainstream-convergent shape, not an outlier.

### 12.1 Course-correction: structural is under-built, semantic-for-code is over-bet

Two honest revisions to the tier priorities in §2/§5, prompted by the spectrum **and** by our own first deploy:

1. **Double down on the structural/AST tier (8.3) — it's our strongest, least-built bet.** The frontier consensus is that AST-aware structural search is where the leverage is for *code* (it returns whole functions/classes, not chunks that break mid-function). We designed it but it is the **least deployed** part — the first eval skipped `understand`/`impact` entirely because no MCP server exists yet. We over-invested in semantic and under-invested in structural. Build the MCP server + the structural backends next.

2. **Re-examine the semantic/embedding tier for code — don't reflexively repair it.** Probe's production argument: *embeddings exist to solve a vocabulary-mismatch problem that an LLM-driven agent already handles* — the agent translates intent into precise boolean/structural queries, so a capable agent + grep + AST may make the embedding tier redundant for code (no index, no embed cost, millisecond results). Our own first eval is direct evidence: **S3 Vectors came up empty/broken, yet exact search scored 63% and browse 93% — the system worked without the semantic tier.** Before filing "fix the empty S3 Vectors index," answer the prior question: *should it exist for code at all, or is it better reserved for docs/wikis/NL queries (the genuine vocabulary-mismatch cases) — see §5c's "embed summaries, not raw code"?* This may be a tier to de-scope for code, not repair.

### 12.2 The moat is outside the spectrum

All five flavors compete on **retrieval quality** — how well the agent *finds* things. None of them (Probe, Cline, SWE-grep, even the RL policies) have a **verified Experience layer**: an agent that remembers what actually *worked*, with substrate proof (CI/tests/deploy outcomes), and matures proven procedures into materialised workflows. That dimension is orthogonal to the whole taxonomy. Our defensible position is therefore **not** "best code retrieval flavor" (Cursor/Probe are ahead on raw retrieval) but **"outcome-verified experience on top of a competent hybrid+structural retriever."** Retrieval flavor is becoming commodity; verified experience is not. Prioritise accordingly.

---

## 13. Compute platform: serverless target + portability posture

Two related strategic stances, recorded so future work preserves them rather than accidentally welding the solution to EKS or to the ADP platform.

### 13.1 We are already ~70% serverless

The **storage + data plane is entirely serverless** (a deliberate choice in the redesign): S3, **S3 Vectors**, SQS, DynamoDB, Bedrock — all pay-per-use, no idle. RDS is the one provisioned store (and is serverless-capable). Ingestion already runs as **KEDA scale-to-zero** jobs. What is *not* serverless is the standing compute: the MCP server, litellm-proxy, deepwiki, and zoekt.

### 13.2 Per-component serverless verdict

| Component | Fit | Target form |
|---|---|---|
| **MCP server** (the verb surface, #1417) | ✅ excellent — stateless request/response | **Build serverless from the start** — Lambda + API Gateway, or Fargate. Not built yet, so nothing to migrate. |
| **litellm-proxy** | ✅ good — stateless Bedrock passthrough | Fargate, or drop it and call Bedrock directly (it is just routing). |
| **ingestion** | ✅ already scale-to-zero (KEDA) | Fargate tasks (NOT Lambda — big repos exceed the 15-min limit; the cloned tree is large). |
| **deepwiki** | ⚠️ borderline — bursty LLM generation | Fargate task, scale-to-zero. Workable. |
| **zoekt** | ❌ **the one real misfit** — serves a search index from a mounted volume as a persistent daemon; cold-start-per-query reloads the index = bad latency | **The one deliberate decision** — see 13.3. |

### 13.3 The zoekt decision = the §12 decision

The single obstacle to "fully serverless" is zoekt-as-a-daemon. But §12 already surfaced the alternative: **Probe's model — no index server at all** (a binary the agent invokes per-query: ripgrep + AST over the cloned tree, milliseconds, no daemon). That is *inherently* serverless-compatible (a Lambda/Fargate invocation, not a standing pod). So **"go fully serverless" and §12's "structural/AST over a persistent semantic index" are the same move** — the thing blocking serverless (the zoekt daemon) is the thing §12 questioned. Resolve them together: keep zoekt as the one standing service, or adopt per-query structural execution. A real evaluation, not a default.

### 13.4 Portability posture (standalone-able, don't fork yet)

The context solution is **architecturally separable** from the ADP platform and should stay that way — serverless reinforces this (handing someone "Lambdas + Fargate + S3/DynamoDB via one Terraform module" beats "stand up an EKS cluster"). Clean seams to **protect**:
- **Identity is header-based and pluggable** (`acl.py` trusts `X-GitHub-Login`/`X-GitHub-Teams` set by *a* dispatch layer — ADP's webhook or any other caller). Do not hard-wire ADP Cognito into the verbs.
- **Runtime talks to AWS services, not the platform** (S3 / S3 Vectors / Bedrock / Postgres — not the gateway or agent-factory). Keep it that way.

Explicit couplings to keep **swappable** (config inputs, not assumptions):
- Terraform **remote-state** lookups (VPC/EKS/ECR) → accept direct inputs for a standalone deploy.
- The `agent_context` DB on the **shared gateway RDS** → a toggle for its own instance (already noted as a dump/restore, not a redesign).
- The **vuln-remediation loop needs an agent fleet** — standalone, this is "bring your own agent": expose the SBOM inventory + MCP verbs; the auto-fix is an ADP integration, not a core dependency.

### 13.5 Sequencing — don't re-platform before it works once

**Target serverless as the end state; do not migrate compute now.** It is not deployed cleanly even once (empty S3, the verify-after-write gap #1423). Fix the logic on the current stack first; *then* the serverless migration is mechanical, because the storage/data plane is already serverless and will not change. Concretely: build the MCP server (#1417) serverless from day one; migrate litellm/deepwiki/ingestion to Fargate after the pipeline works; make the zoekt-vs-Probe call (13.3) as its own decision. Preserve the portability seams (13.4) in everything built between now and then.

---

## 11. One-paragraph summary

We're replacing two fragile, partly-license-encumbered tools (Sourcebot, OpenViking) with a simpler design on plain AWS services. Each repo is cloned once on a temporary disk, then indexed four ways — exact search (Zoekt), meaning search (S3 Vectors), a structure map, and a dependency bill-of-materials — with the finished results stored durably in S3, S3 Vectors, and PostgreSQL. Work is spread across many machines in parallel so hundreds of repos index quickly. Every answer an agent gets is filtered by GitHub-mirrored permissions at the MCP server. The bill-of-materials is the standout new capability: by recording which repos use which dependencies (and making that instantly searchable), ADP can detect a new vulnerability, find every affected repo, hand the fix to its existing developer agents, and verify the fix with tests — turning ADP from a code-search tool into an autonomous vulnerability-management-and-remediation platform.
