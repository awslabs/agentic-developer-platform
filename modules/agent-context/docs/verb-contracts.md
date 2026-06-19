# Knowledge Layer Verb Contracts

> **Status**: Active
> **Author**: @agent-architect
> **Date**: 2026-06-19
> **Related**: #1643 (understand contract decision), #1592 (agent guidance), EPIC #1529

This document defines the **input contract** for each Knowledge Layer MCP verb — what
each verb accepts, what it does NOT accept, and where to route queries that don't fit.

---

## `understand`

**Purpose**: Structural lookup — retrieve the definition, neighborhood, and topology
of a specific code entity (repo, directory, file, or symbol).

**Parameter**: `target` (string, required)

### Valid target formats

| Format | Example | Returns |
|--------|---------|---------|
| Repo-level | `"codegraph"` or `"org/repo"` | Module topology (top-level modules, file counts) |
| File path | `"CloakBrowser/cloakbrowser/browser.py"` | Symbols defined in that file (names, kinds, signatures) |
| Directory | `"CloakBrowser/cloakbrowser/human"` | Symbols/modules in that directory |
| Symbol (qualified) | `"codegraph::engine"` or `"org/repo::Class.method"` | Definition + callers + callees + signature |
| Symbol (file-qualified) | `"repo/src/db.py::connect"` | Same as above, scoped to file |

### What understand does NOT accept

- Natural-language questions: `"what does X do"`, `"how does Y work"`
- Concept queries: `"finding security vulnerabilities"`
- List requests: `"what agents are available"`

These belong to other verbs (see routing table below).

### Backend chain

1. **Primary**: Neptune openCypher queries (symbol neighborhood, module topology)
2. **Fallback**: S3 code-index.json (when Neptune unavailable or lacks data)
3. **Last resort**: Zoekt file search (for cross-repo when code-index lacks results)

### Scoring contract (eval)

A correct `understand` response contains:
- `definitions[]` with the expected `file` path (location match)
- Structural facts (symbol names, kinds, signatures, callers/callees) that match expected key facts

---

## `search` (exact)

**Purpose**: Find files containing a literal token, symbol name, or string.

**Parameter**: `query` (string), `scope` = `"code"` (default)

### Valid queries

- Literal strings known to exist in code: `"code review checklist"`
- Symbol names: `"compute_frozen_count"`
- Multi-token identifiers: `"hooks.json session-start"`

### What search_exact does NOT accept

- Vocabulary-mismatched concepts: `"how to write good tests"` (use search_semantic)
- Structural lookups: `"CloakBrowser/browser.py"` (use understand)

---

## `search` (semantic)

**Purpose**: Bridge vocabulary gaps — find files relevant to a concept query even when
the query terms don't literally appear in the code.

**Parameter**: `query` (string), `scope` = `"docs"` + `semantic_enabled` flag

### Valid queries

- Concept queries with vocabulary mismatch: `"how to write good tests"` → finds `test-driven-development/SKILL.md`
- Natural-language questions about capabilities: `"what does X do"`, `"how does Y work"`
- Synonym-bridging: `"finding security vulnerabilities"` → finds `security-and-hardening`

### Current status

Planned but not yet active (returns empty results). When implemented, backed by S3 Vectors embeddings.

---

## `impact`

**Purpose**: Call-graph / dependency analysis — who calls this symbol, what breaks if it changes.

**Parameter**: `target` (string, same format as understand), `cross_repo` (boolean)

### Valid targets

- Symbol references: `"codegraph::engine"`, `"last30days-skill/scripts/lib/schema.py"`
- File paths (finds all symbols in file, reports callers of each)

### What impact does NOT accept

- Natural-language questions
- Directory-level targets (use understand for topology)

---

## `browse`

**Purpose**: Navigate repository structure — list files and directories.

**Parameter**: `action` = `"ls"`, `uri` (path)

### Valid URIs

- Repo root: `"/agent-skills"`
- Directory: `"/agent-skills/skills"`
- Specific path: `"/agent-skills/hooks"`

---

## Routing Table (When to Use Which Verb)

| User intent | Correct verb | Example |
|-------------|--------------|---------|
| "Show me what's in this file" | `understand` | `understand("repo/path/file.py")` |
| "What symbols are in this directory" | `understand` | `understand("repo/src/")` |
| "What does function X do" (know the name) | `understand` | `understand("repo::functionX")` |
| "Find code about concept Y" (NL) | `search` (semantic) | `search("concept Y", scope="docs")` |
| "Find files containing literal Z" | `search` (exact) | `search("literal Z")` |
| "What calls this function" | `impact` | `impact("repo::function")` |
| "List files in this directory" | `browse` | `browse(uri="/repo/dir")` |
| "What would break if I change X" | `impact` | `impact("repo::X", cross_repo=true)` |

---

## Decision Record

**Issue #1643** (2026-06-19): The `understand` verb scored 15% in baseline eval because
25/65 golden questions were natural-language prose ("what does X do") sent to a
structural-target verb. The 10/10 passing entries were all file-path/directory targets.

**Decision**: The verb contract IS structural targeting. NL questions are re-scoped to
`search_semantic` (or rewritten as structural targets). No NL→symbol resolution layer
is needed — the verb works correctly for its intended purpose.

**Evidence**: `structural_backend.py:863` (`_parse_target()` — purely structural parser),
`server.py:50-55` (tool description says "specific repo, directory, or file"),
Neptune design doc D15 (bounded structural lookup).
