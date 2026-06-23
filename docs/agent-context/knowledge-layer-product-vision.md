# Knowledge Layer — Product Vision

**Status:** Product vision. The "why" and "for whom" above the architecture.
**Last updated:** 2026-06-23
**Companion docs:** `knowledge-layer-design.md` (the *how* — design of record) · `vision-federated-code-intelligence.md` (the *technical* north-star architecture). This document sits **above** both: it states who the Knowledge Layer is for, the problem it solves, and why ADP wins — and gives the E1–E7 child EPICs of #1345 a "why" to hang off.

---

## 1. Vision statement

> **Every agent and engineer working in our codebase should be able to ask any question about it — "what does this do?", "what breaks if I change this?", "which repos are exposed to this CVE?" — and get a correct, permission-safe answer in seconds, across every repository, without reading the code first.**
>
> And beyond answering: the platform should *act* on what it knows — finding vulnerabilities across the whole corpus and fixing them with its own agents, then remembering what worked.

The Knowledge Layer turns a pile of repositories into a **queryable, reasoning substrate** that both humans and ADP's autonomous agents share. It is the difference between agents that grep blindly and agents that *understand* the code they're changing.

---

## 2. The problem

As a codebase grows past what one person can hold in their head — dozens to hundreds of repos, millions of lines, many languages — three things break:

1. **Comprehension doesn't scale.** Answering "how does auth work here?" or "what calls this function?" means reading code across repos. Humans can't; agents that only grep get shallow, often wrong answers and waste tokens re-deriving structure every task.
2. **Change is blind.** "What breaks if I touch this?" has no fast answer. Blast-radius is discovered in production, not before the PR.
3. **Security is reactive and manual.** A new CVE lands. *Which of our repos use it?* Today that's a frantic manual audit, repo by repo, often missing transitive dependencies entirely — and then every fix is hand-rolled.

The common root cause: **the knowledge is latent in the code but not queryable.** Existing tools either don't scale (grep), are license-encumbered or fragile (the tools we removed), or stop at *retrieval* and never *act*.

---

## 3. Who it's for

| Persona | What they need | What the Knowledge Layer gives them |
|---|---|---|
| **ADP's autonomous agents** (developer, ops, reviewer) | Accurate context before editing code they didn't write | One MCP endpoint: exact search, structural `understand`/`impact`, permission-safe — so they edit with understanding, not guesswork |
| **Platform / security owners** | "Which repos are exposed to advisory X, right now?" | Instant reverse-dependency answer + an autonomous loop that files, fixes, and verifies the remediation |
| **Engineers** (human) | Fast comprehension + blast-radius before a change | The same verbs, plus browsable wikis and architecture views |
| **Open-source / commercial adopters** | A code-intelligence platform they can run and ship | A cleanly **permissive-licensed** stack (no AGPL/GPL/BSL), portable beyond ADP's own platform |

The primary customer is **the agent fleet** — every other persona benefits, but the Knowledge Layer's first job is to make ADP's own agents materially better at their work.

---

## 4. The outcome we're selling

**ADP is not a code-search tool. It is an autonomous code-intelligence and vulnerability-management platform.**

Code search is table stakes — a means, not the product. The product is what becomes possible once the codebase is queryable:

- **Agents that understand before they act** — fewer wrong edits, less wasted reasoning, higher first-pass PR quality.
- **Blast-radius before the PR, not after the incident.**
- **A vulnerability that fixes itself**: detect a CVE → reverse-index finds every affected repo and file → reachability triage drops false positives → a fix issue is filed → ADP's existing developer agents fix it and run the tests → PR opened (never auto-merged) → the verified outcome is remembered. Across the whole corpus, autonomously.

That last loop is the headline: it reuses ADP's existing developer-agent fleet, so the SBOM is the one missing piece that converts a search index into a security product.

---

## 5. Why us — the defensible moat

The 2026 market for "agent-as-retriever" has fragmented into five flavors (pure-agentic loops, hybrid lexical+semantic, structural/AST, specialised retrieval models, RL-trained retrieval policies). **All five compete on retrieval quality — how well an agent *finds* things. Retrieval flavor is becoming a commodity; Cursor and Probe are already ahead on raw retrieval, and we will not win there.**

Our defensible position is **orthogonal to that whole taxonomy**:

> **Outcome-verified experience on top of a competent hybrid + structural retriever.**

No competitor pairs a good-enough retriever with an **Experience layer** that remembers what actually *worked* — backed by substrate proof (tests passed, deploy succeeded, CVE closed) — and matures proven procedures into reusable workflows. Retrieval tells an agent *where to look*; verified experience tells it *what has actually worked here before*. That dimension is the moat, because:

- It **compounds** — every fixed CVE, every green deploy, every successful change makes the next one better. A retriever doesn't get smarter from being used; our Experience layer does.
- It's **proprietary by construction** — it's built from *our* outcomes on *our* corpus; it can't be downloaded or replicated by a better embedding model.
- It's **the bridge from "answers questions" to "does work"** — and doing work, verified, is the product.

So we prioritise accordingly: be a **competent** hybrid + structural retriever (not the best — competent), and invest the differentiation budget in the autonomous act-and-remember loop.

---

## 6. Principles (constraints that shape the product)

1. **Permission-safe or it doesn't ship.** Every answer is filtered against GitHub-mirrored ACLs at the single query surface; an unknown caller sees nothing (fail-closed). A code-intelligence platform that can leak across tenants is not a product.
2. **Permissively licensed, end to end.** We intend to distribute this. Every component is Apache-2.0 / MIT (which is why Sourcebot, OpenViking, Redis, Neo4j, and Grype were all replaced). No AGPL/GPL/BSL/SSPL anywhere in the chain.
3. **Portable — not welded to ADP.** The Knowledge Layer is architecturally separable: header-based pluggable identity, talks to AWS services (not the ADP gateway), serverless-leaning. Someone should be able to run it as "a few Lambdas + Fargate + S3 via one Terraform module," not "stand up our whole platform."
4. **Structural over semantic for code.** A capable agent already turns intent into precise structural queries; we bet on AST/structural understanding (returns whole functions, not broken chunks) over semantic-for-code, and reserve embeddings for the genuine vocabulary-mismatch cases (wiki/doc prose, NL questions).
5. **Act, don't just answer.** Retrieval that stops at a result list is half a product. The reverse-dependency index, the vuln loop, and the Experience layer exist to close the gap from *knowing* to *doing*.

---

## 7. What "winning" looks like (success measures)

These are product outcomes, not engineering metrics:

- **Agents reach for the Knowledge Layer by default** — context verbs are part of how every developer/ops agent works, not an opt-in.
- **Mean "what breaks if I change this?" latency: seconds**, answered correctly across repos.
- **CVE-to-PR is autonomous** — a published advisory results in fixing PRs across all affected repos with no human triage, only human merge approval.
- **The Experience layer measurably improves outcomes over time** — later remediations and changes succeed faster / first-try more often than earlier ones on the same corpus.
- **Runnable standalone** — a third party can deploy the Knowledge Layer without the rest of ADP.

---

## 8. Roadmap — vision to execution

The product vision decomposes into the child EPICs of #1345 (the same ones nested under it on the project board). This is the "why" behind each:

| Product capability | EPIC / issue | Vision tie |
|---|---|---|
| Comprehension that's reliably useful (`understand`, structural) | **E1 #1665** | §2 problem 1 — comprehension at scale |
| Framework-aware routing (endpoints → handlers) | **E2 #1666** | deeper "what does this do" across web frameworks |
| Analytical verbs — blast-radius, architecture clusters, dead-code | **E4 #1667** | §2 problem 2 — change isn't blind |
| Local edge daemon (hybrid topology) — *decision-gated* | **E5 #1668** | latency/portability frontier (vision §2 Edge) |
| Operational/runtime intelligence (`diagnose_pod_failure`) — *separate pillar, decision-gated* | **E6 #1669** | act-on-runtime, beyond code |
| Self-serve & team-level indexing (multi-tenant, ACL-scoped) | **E7 #1672** | §3 adopters — make it usable by teams, not just the platform |
| Autonomous vuln-management loop | §7 of design-of-record (#1358/#1359/#1360) | §4 the headline outcome |
| Cross-repo deep graph (Neptune) | **#1529** | §2 problems 1 & 2 — the substrate |
| Permissive-license mandate | parent **#1345** | §6 principle 2 |

The technical architecture for all of this is `vision-federated-code-intelligence.md`; the concrete build design is `knowledge-layer-design.md`.

---

## 9. Explicit non-goals

- **Being the best raw retriever.** We aim for *competent* hybrid + structural retrieval and win on verified experience instead (§5). Chasing Cursor/Probe on retrieval benchmarks is not the game.
- **Semantic embeddings for code in v1.** Gated off; reserved for wiki/NL. (See design-of-record §6.)
- **Operational/runtime intelligence as a core pillar.** It's a separate, decision-gated pillar (E6), not part of the core code Knowledge Layer.
- **A general-purpose graph database product.** Neptune is an implementation detail, not the offering.
