#!/usr/bin/env python3
"""Per-repo ingestion pipeline.

Pipeline steps:
  1. Clone repo to persistent storage (S3 Files mount)
  2. Run cgc analyze -> code-index.json -> filesystem + S3 markdown summary
  3. Call DeepWiki API -> wiki.md -> S3 + S3 Vectors (via wiki_store)
  4. GraphRAG extraction -> Neptune (with delete-before-reload for stale entity cleanup)
  5. Keep clone on persistent storage for downstream consumers

Usage:
  python ingest-repo.py --repo org/repo
  python ingest-repo.py --repo org/repo --skip-deepwiki
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from telemetry import configure_telemetry, get_logger

configure_telemetry(service_name="knowledge-layer-ingest-repo")
log = get_logger("ingest-repo")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

from config import settings
from scope import compute_s3_prefix, parse_scope_from_env
from scip_indexer import index_repo as scip_index_repo, detect_languages, cleanup_indexing_artifacts
from scip_ingester import ingest_scip
from scip_neptune_csv import (
    generate_csv as scip_generate_csv,
    generate_summary as scip_generate_summary,
)

DEEPWIKI_URL = settings.deepwiki_url
DEEPWIKI_ENABLED = settings.deepwiki_enabled
# Clone to persistent S3 Files mount instead of /tmp (shared across enrichment consumers)
CLONE_BASE = settings.clone_base
REQUEST_TIMEOUT = settings.request_timeout
CODE_INDEX_DIR = settings.code_index_dir

# Neptune TLS verification — Amazon CA bundle (Issue #2224)
# Override with NEPTUNE_CA_BUNDLE_PATH env var for local dev (set to "" to disable).
NEPTUNE_CA_BUNDLE = (
    os.environ.get("NEPTUNE_CA_BUNDLE_PATH", "/etc/ssl/certs/rds-global-bundle.pem") or False
)

# GraphRAG configuration
GRAPHRAG_ENABLED = settings.graphrag_enabled
NEPTUNE_ENDPOINT = settings.neptune_endpoint
NEPTUNE_PORT = settings.neptune_port
OPENSEARCH_ENDPOINT = settings.opensearch_endpoint
LLM_MODEL = settings.model_wiki
LLM_BASE_URL = settings.llm_base_url

# SCIP structural graph configuration
SCIP_ENABLED = os.environ.get("SCIP_ENABLED", "true").lower() in ("true", "1", "yes")

# DynamoDB configuration (for state tracking — replaces repo-state.json)
DYNAMO_TABLE = settings.dynamo_table
AWS_REGION = settings.aws_region

# S3 content store configuration
S3_BUCKET_NAME = settings.s3_bucket_name
S3_CONTENT_PREFIX = settings.s3_content_prefix
WIKI_S3_PREFIX = settings.wiki_s3_prefix
CODE_INDEX_S3_PREFIX = settings.code_index_s3_prefix
S3_VECTORS_BUCKET = settings.s3_vectors_bucket
S3_VECTORS_SHARD_COUNT = settings.s3_vectors_shard_count

# Zoekt indexing configuration (#2361)
ZOEKT_INDEX_ENABLED = settings.zoekt_index_enabled
ZOEKT_SHARDS_S3_PREFIX = settings.zoekt_shards_s3_prefix
ZOEKT_INDEX_TIMEOUT = settings.zoekt_index_timeout

# ---------------------------------------------------------------------------
# S3 content store + wiki store imports
# ---------------------------------------------------------------------------

from s3_store import S3ContentStore
from stage_tracker import StageTracker
from wiki_store import store_wiki, store_code_index_to_s3


# ---------------------------------------------------------------------------
# S3 Writer Adapter (bridges S3ContentStore to wiki_store.S3Writer protocol)
# ---------------------------------------------------------------------------


class _S3WriterAdapter:
    """Adapts S3ContentStore to satisfy the wiki_store.S3Writer protocol.

    The wiki_store module expects an object with a `put_object(bucket, key, body) -> bool`
    method. S3ContentStore uses `put_content(path, content) -> bool` which prepends its
    own prefix and always targets its configured bucket. This adapter bypasses the prefix
    logic and writes directly to the specified bucket/key.
    """

    def __init__(self, store: S3ContentStore):
        self._s3_client = store._s3

    def put_object(self, bucket: str, key: str, body: str) -> bool:
        """Write content to S3 at the given bucket/key."""
        try:
            if isinstance(body, str):
                encoded = body.encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            else:
                encoded = body
                content_type = "application/octet-stream"

            self._s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=encoded,
                ContentType=content_type,
            )
            log.info("S3 put_object: s3://%s/%s (%d bytes)", bucket, key, len(encoded))
            return True
        except Exception as e:
            log.error("S3 put_object failed for s3://%s/%s: %s", bucket, key, e)
            return False


# ---------------------------------------------------------------------------
# DynamoDB state helpers
# ---------------------------------------------------------------------------


def _get_dynamodb_table():
    """Lazy-init DynamoDB table resource."""
    try:
        import boto3

        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMO_TABLE)
    except Exception as e:
        log.debug("DynamoDB not available: %s", e)
        return None


def update_dynamo_state(org_repo: str, result: dict[str, Any], tags: dict[str, str] | None = None):
    """Update DynamoDB STATE record after ingestion."""
    table = _get_dynamodb_table()
    if not table:
        return

    pk = f"repo#{org_repo}"
    now = datetime.now(timezone.utc).isoformat()

    item = {
        "source": pk,
        "record_type": "STATE",
        "content_type": "repo",
        "updated_at": now,
        "s3_status": "complete"
        if result.get("s3_upload") in ("ok", "complete")
        else ("failed" if result.get("s3_upload") == "failed" else "skipped"),
        "code_index_status": "complete"
        if result.get("code_index") == "written"
        else ("failed" if result.get("code_index") in ("failed", "fs_write_failed") else "skipped"),
        "deepwiki_status": "complete"
        if result.get("deepwiki") in ("uploaded", "generated")
        else ("failed" if result.get("deepwiki") == "failed" else "skipped"),
        "graphrag_status": "complete"
        if result.get("graphrag") == "ok"
        else ("failed" if result.get("graphrag") == "failed" else "skipped"),
        "sbom_source_status": "complete"
        if result.get("sbom_source") == "complete"
        else (
            "failed"
            if result.get("sbom_source") in ("failed", "syft_failed", "syft_timeout")
            else "skipped"
        ),
        "last_error": result.get("error"),
    }

    if tags:
        item["user_tags"] = tags

    # Get current SHA for last_sha tracking
    clone_path = os.path.join(CLONE_BASE, org_repo)
    if os.path.exists(os.path.join(clone_path, ".git")):
        try:
            sha_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=clone_path,
                capture_output=True,
                timeout=10,
            )
            if sha_result.returncode == 0:
                item["last_sha"] = sha_result.stdout.decode().strip()
        except Exception:
            pass

    try:
        table.put_item(Item={k: v for k, v in item.items() if v is not None})
        log.info("DynamoDB state updated for %s", org_repo)
    except Exception as e:
        log.warning("DynamoDB state update failed for %s: %s", org_repo, e)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _sanitize_git_output(text: str) -> str:
    """Remove any credentials from git output.

    Git with GIT_ASKPASS shouldn't leak credentials in stderr,
    but defense-in-depth: redact anything that looks like a token.
    """
    import re

    # Redact x-access-token:xxx@ patterns (shouldn't appear, but just in case)
    text = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", text)
    # Redact GitHub token prefixes (ghp_=PAT, gho_=OAuth, ghu_=user, ghs_=App installation)
    text = re.sub(r"(ghp_|gho_|ghu_|ghs_|github_pat_)[A-Za-z0-9_]+", r"\1***", text)
    return text


def git_clone(repo_url: str, dest: str) -> bool:
    """Clone a repo using GIT_ASKPASS for auth. Returns True on success."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    # GIT_ASKPASS is already set by sqs-worker if token is available.
    # Ensure terminal prompt is disabled regardless.
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, dest],
            check=True,
            capture_output=True,
            timeout=300,
            env=env,
        )
        log.info("Cloned %s -> %s", repo_url, dest)
        return True
    except subprocess.CalledProcessError as e:
        stderr = _sanitize_git_output(e.stderr.decode()[:500])
        log.error("git clone failed for %s: %s", repo_url, stderr)
        return False
    except subprocess.TimeoutExpired:
        log.error("git clone timed out for %s", repo_url)
        return False


# ---------------------------------------------------------------------------
# cgc (CodeGraphContext) analysis
# ---------------------------------------------------------------------------


def cgc_analyze(clone_path: str, org_repo: str) -> dict[str, Any] | None:
    """Run codegraphcontext analysis on a cloned repo, return code-index dict."""
    try:
        import codegraphcontext  # noqa: F401

        # Try using cgc CLI first
        result = subprocess.run(
            ["cgc", "analyze", clone_path, "--json"],
            capture_output=True,
            timeout=300,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            data["repo"] = org_repo
            data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            return data
    except (ImportError, FileNotFoundError):
        log.info("cgc CLI not found, trying Python API")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log.warning("cgc CLI failed: %s", e)

    # Fallback: build a basic code-index from tree-sitter
    try:
        return _build_basic_code_index(clone_path, org_repo)
    except Exception as e:
        log.warning("Basic code-index generation failed: %s", e)
        return None


def _build_basic_code_index(clone_path: str, org_repo: str) -> dict[str, Any]:
    """Build a basic code-index.json using file analysis (no tree-sitter required)."""
    symbols: list[dict] = []
    imports: dict[str, list[str]] = {}
    language_stats: dict[str, int] = {}
    dependencies_external: set[str] = set()
    dependencies_internal: dict[str, list[str]] = {}

    ext_to_lang = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
    }

    clone = Path(clone_path)
    for ext, lang in ext_to_lang.items():
        files = list(clone.rglob(f"*{ext}"))
        # Skip node_modules, .git, vendor, etc.
        files = [
            f
            for f in files
            if not any(
                p in f.parts
                for p in ("node_modules", ".git", "vendor", "__pycache__", ".venv", "dist")
            )
        ]
        if files:
            language_stats[lang] = len(files)

        for fpath in files[:500]:  # Cap to prevent huge repos from hanging
            rel = str(fpath.relative_to(clone))
            try:
                content = fpath.read_text(errors="replace")
                lines = content.split("\n")
            except Exception:
                continue

            file_imports: list[str] = []
            file_internal_deps: list[str] = []

            for i, line in enumerate(lines[:2000]):
                stripped = line.strip()

                # Python imports
                if lang == "python":
                    if stripped.startswith("import ") or stripped.startswith("from "):
                        file_imports.append(stripped)
                        # Extract package name
                        parts = stripped.split()
                        if len(parts) >= 2:
                            pkg = parts[1].split(".")[0]
                            if not pkg.startswith("."):
                                dependencies_external.add(pkg)
                    if stripped.startswith("def "):
                        name = stripped[4:].split("(")[0].strip()
                        if name:
                            symbols.append(
                                {
                                    "name": name,
                                    "type": "function",
                                    "file": rel,
                                    "line": i + 1,
                                }
                            )
                    elif stripped.startswith("class "):
                        name = stripped[6:].split("(")[0].split(":")[0].strip()
                        if name:
                            symbols.append(
                                {
                                    "name": name,
                                    "type": "class",
                                    "file": rel,
                                    "line": i + 1,
                                }
                            )

                # TypeScript/JavaScript imports
                elif lang in ("typescript", "javascript"):
                    if stripped.startswith("import "):
                        file_imports.append(stripped)
                        if "from " in stripped:
                            mod = stripped.split("from")[-1].strip().strip("\"';")
                            if not mod.startswith("."):
                                dependencies_external.add(mod.split("/")[0])
                    if "function " in stripped and (
                        "export" in stripped or stripped.startswith("function ")
                    ):
                        # Extract function name
                        idx = stripped.find("function ") + 9
                        name = stripped[idx:].split("(")[0].strip()
                        if name:
                            symbols.append(
                                {
                                    "name": name,
                                    "type": "function",
                                    "file": rel,
                                    "line": i + 1,
                                }
                            )

                # Go imports
                elif lang == "go":
                    if stripped.startswith("import "):
                        file_imports.append(stripped)
                    if stripped.startswith("func "):
                        name = stripped[5:].split("(")[0].strip()
                        if name:
                            symbols.append(
                                {
                                    "name": name,
                                    "type": "function",
                                    "file": rel,
                                    "line": i + 1,
                                }
                            )

            if file_imports:
                imports[rel] = file_imports

    return {
        "repo": org_repo,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "language_stats": language_stats,
        "symbols": symbols[:5000],  # Cap total symbols
        "imports": imports,
        "call_graph": {},  # Basic analysis doesn't produce call graphs
        "dependencies": {
            "external": sorted(dependencies_external),
            "internal": dependencies_internal,
        },
    }


def _write_code_index_to_filesystem(code_index_json: str, safe_name: str, org_repo: str) -> bool:
    """Write code-index JSON to the shared filesystem (platform-data PVC).

    This is the primary storage for structured code-index data, read by the
    MCP server's understand and impact tools.
    """
    try:
        os.makedirs(CODE_INDEX_DIR, exist_ok=True)
        path = os.path.join(CODE_INDEX_DIR, f"{safe_name}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code_index_json)
        log.info("Wrote code-index to filesystem: %s", path)
        return True
    except OSError as e:
        log.warning(
            "Failed to write code-index to filesystem for %s: %s — falling back to S3-only upload",
            org_repo,
            e,
        )
        return False


def _code_index_to_markdown(code_index: dict[str, Any]) -> str:
    """Convert code-index JSON to a markdown summary for S3 semantic search."""
    lines = [f"# Code Index: {code_index.get('repo', 'unknown')}\n"]
    lines.append(f"Analyzed at: {code_index.get('analyzed_at', 'unknown')}\n")

    lang_stats = code_index.get("language_stats", {})
    if lang_stats:
        lines.append("## Language Statistics\n")
        for lang, count in sorted(lang_stats.items(), key=lambda x: -x[1]):
            lines.append(f"- **{lang}**: {count} files")
        lines.append("")

    deps = code_index.get("dependencies", {})
    ext_deps = deps.get("external", [])
    if ext_deps:
        lines.append("## External Dependencies\n")
        for dep in ext_deps[:100]:
            lines.append(f"- {dep}")
        lines.append("")

    symbols = code_index.get("symbols", [])
    classes = [s for s in symbols if s.get("type") == "class"]
    functions = [s for s in symbols if s.get("type") == "function"]

    if classes:
        lines.append(f"## Classes ({len(classes)} total)\n")
        for cls in classes[:50]:
            lines.append(f"- `{cls['name']}` in `{cls.get('file', '')}`:{cls.get('line', 0)}")
        if len(classes) > 50:
            lines.append(f"- ... and {len(classes) - 50} more classes")
        lines.append("")

    if functions:
        lines.append(f"## Functions ({len(functions)} total)\n")
        for fn in functions[:100]:
            lines.append(f"- `{fn['name']}` in `{fn.get('file', '')}`:{fn.get('line', 0)}")
        if len(functions) > 100:
            lines.append(f"- ... and {len(functions) - 100} more functions")
        lines.append("")

    call_graph = code_index.get("call_graph", {})
    if call_graph:
        lines.append("## Call Graph\n")
        for caller, callees in list(call_graph.items())[:50]:
            lines.append(f"- `{caller}` calls: {', '.join(f'`{c}`' for c in callees[:10])}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DeepWiki generation
# ---------------------------------------------------------------------------


def deepwiki_generate(org_repo: str) -> str | None:
    """Call DeepWiki streaming API to generate a wiki for a repo.

    Uses /chat/completions/stream — the correct DeepWiki endpoint.
    The old /api/wiki/generate endpoint does not exist (404).
    """
    try:
        resp = requests.post(
            f"{DEEPWIKI_URL}/chat/completions/stream",
            json={
                "repo_url": f"https://github.com/{org_repo}",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Generate a comprehensive architecture wiki. "
                            "Cover: overview, architecture, key components, "
                            "code organization, patterns, dependencies."
                        ),
                    }
                ],
                "provider": "openai",
                "model": LLM_MODEL,
                "language": "en",
                "type": "github",
            },
            timeout=900,  # Wiki generation can take 5-15 minutes
            stream=False,  # Get full response (not SSE chunks)
        )
        if resp.status_code < 300:
            # The streaming endpoint returns the wiki content directly
            wiki_text = resp.text.strip()
            if len(wiki_text) < 500:
                log.warning(
                    "DeepWiki wiki too short for %s (%d chars)",
                    org_repo,
                    len(wiki_text),
                )
                return None
            log.info(
                "DeepWiki generated wiki for %s (%d chars)",
                org_repo,
                len(wiki_text),
            )
            return wiki_text
        else:
            log.warning(
                "DeepWiki returned HTTP %d for %s: %s",
                resp.status_code,
                org_repo,
                resp.text[:200],
            )
            return None
    except requests.Timeout:
        log.warning("DeepWiki timed out for %s (15 min limit)", org_repo)
        return None
    except Exception as e:
        log.warning("DeepWiki failed for %s: %s", org_repo, e)
        return None


# ---------------------------------------------------------------------------
# GraphRAG extraction — entities and relationships into Neptune
# ---------------------------------------------------------------------------


def graphrag_extract(
    clone_path: str,
    org_repo: str,
    code_index: dict[str, Any] | None,
    wiki: str | None,
) -> dict[str, Any] | None:
    """Extract entities and relationships for the knowledge graph.

    Loads structured data from code-index directly (no LLM needed) and uses
    LLM for higher-level entity extraction from wiki + source code.
    """
    if not GRAPHRAG_ENABLED or not NEPTUNE_ENDPOINT:
        return None

    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []

    # 1. Load code-index directly into graph (free — no LLM extraction needed)
    #    Delete stale code-index entities first so removed symbols don't persist
    if code_index:
        _clear_repo_structural_graph(org_repo)
        symbols = code_index.get("symbols", [])
        # Build a lookup of entity_ids for matching relationships later
        entity_id_set: set[str] = set()
        # Also track file-level entities we create
        file_entities_created: set[str] = set()

        for sym in symbols[:500]:
            eid = f"{org_repo}:{sym.get('file', '')}:{sym.get('name', '')}"
            entity_id_set.add(eid)
            entities.append(
                {
                    "entity_id": eid,
                    "name": sym.get("name", ""),
                    "type": sym.get("type", "symbol"),
                    "file": sym.get("file", ""),
                    "line": sym.get("line", 0),
                    "repo": org_repo,
                    "source": "code-index",
                }
            )

        # Import relationships from code-index
        # Create file-level entities so import edges have valid source vertices
        imports_data = code_index.get("imports", {})
        for file, file_imports in imports_data.items():
            file_eid = f"{org_repo}:{file}:__module__"
            if file_eid not in file_entities_created:
                file_entities_created.add(file_eid)
                entity_id_set.add(file_eid)
                entities.append(
                    {
                        "entity_id": file_eid,
                        "name": file.rsplit("/", 1)[-1] if "/" in file else file,
                        "type": "module",
                        "file": file,
                        "repo": org_repo,
                        "source": "code-index",
                    }
                )
            for imp in file_imports:
                # Create a package entity for the import target
                pkg_eid = f"pkg:{imp}"
                if pkg_eid not in entity_id_set:
                    entity_id_set.add(pkg_eid)
                    entities.append(
                        {
                            "entity_id": pkg_eid,
                            "name": imp,
                            "type": "package",
                            "repo": "__external__",
                            "source": "code-index",
                        }
                    )
                relationships.append(
                    {
                        "from": file_eid,
                        "to": pkg_eid,
                        "type": "imports",
                        "repo": org_repo,
                    }
                )

        # Call graph edges — normalize to entity_id format
        call_graph = code_index.get("call_graph", {})
        for caller, callees in call_graph.items():
            if isinstance(callees, list):
                # caller may be "file:func" or just "func" — prefix with org_repo
                caller_eid = f"{org_repo}:{caller}"
                # If caller_eid isn't in entity set, try to find a matching entity
                if caller_eid not in entity_id_set:
                    # Look for any entity whose id ends with the caller
                    matches = [
                        e
                        for e in entity_id_set
                        if e.endswith(f":{caller}") or e.endswith(f":{caller.split(':')[-1]}")
                    ]
                    if matches:
                        caller_eid = matches[0]
                for callee in callees:
                    callee_eid = f"{org_repo}:{callee}"
                    if callee_eid not in entity_id_set:
                        matches = [
                            e
                            for e in entity_id_set
                            if e.endswith(f":{callee}") or e.endswith(f":{callee.split(':')[-1]}")
                        ]
                        if matches:
                            callee_eid = matches[0]
                    relationships.append(
                        {
                            "from": caller_eid,
                            "to": callee_eid,
                            "type": "calls",
                            "repo": org_repo,
                        }
                    )

        # External dependency edges — create repo-level and package entities
        repo_eid = f"{org_repo}:__repo__"
        if repo_eid not in entity_id_set:
            entity_id_set.add(repo_eid)
            entities.append(
                {
                    "entity_id": repo_eid,
                    "name": org_repo.split("/")[-1],
                    "type": "repository",
                    "repo": org_repo,
                    "source": "code-index",
                }
            )
        ext_deps = code_index.get("dependencies", {}).get("external", [])
        for dep in ext_deps:
            pkg_eid = f"pkg:{dep}"
            if pkg_eid not in entity_id_set:
                entity_id_set.add(pkg_eid)
                entities.append(
                    {
                        "entity_id": pkg_eid,
                        "name": dep,
                        "type": "package",
                        "repo": "__external__",
                        "source": "code-index",
                    }
                )
            relationships.append(
                {
                    "from": repo_eid,
                    "to": pkg_eid,
                    "type": "depends_on",
                    "repo": org_repo,
                }
            )

    # 2. Extract higher-level entities from wiki (architecture components, patterns)
    if wiki:
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Content-Type": "application/json"},
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"Extract entities and relationships from this architecture wiki for {org_repo}.\n\n"
                                f"{wiki[:4000]}\n\n"
                                "Return JSON with two arrays:\n"
                                '- "entities": [{{"name": "...", "type": "component|pattern|service|api", "description": "..."}}]\n'
                                '- "relationships": [{{"from": "...", "to": "...", "type": "uses|implements|communicates_with|part_of"}}]\n'
                                "Return ONLY valid JSON."
                            ),
                        }
                    ],
                    "max_tokens": 3000,
                    "temperature": 0.1,
                },
                timeout=120,
            )
            if resp.status_code < 300:
                import re

                content = resp.json()["choices"][0]["message"]["content"]
                # Try to parse JSON from response
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
                    data = json.loads(match.group(1)) if match else {}

                for ent in data.get("entities", []):
                    ent["repo"] = org_repo
                    ent["entity_id"] = f"{org_repo}:wiki:{ent.get('name', '')}"
                    ent["source"] = "wiki"
                    entities.append(ent)

                for rel in data.get("relationships", []):
                    rel["repo"] = org_repo
                    # Normalize from/to to use entity_id format
                    if rel.get("from") and ":" not in rel["from"]:
                        rel["from"] = f"{org_repo}:wiki:{rel['from']}"
                    if rel.get("to") and ":" not in rel["to"]:
                        rel["to"] = f"{org_repo}:wiki:{rel['to']}"
                    relationships.append(rel)
        except Exception as e:
            log.warning("Wiki entity extraction failed for %s: %s", org_repo, e)

    # 3. Write to Neptune via HTTP API
    if entities or relationships:
        _write_to_neptune(entities, relationships, org_repo)

    return {
        "entities": len(entities),
        "relationships": len(relationships),
    }


def _get_neptune_signed_headers(method: str, url: str, body: str | None = None) -> dict:
    """Sign Neptune request with IAM SigV4. Falls back to plain headers."""
    headers = {"Content-Type": "application/json"}
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.session import Session as BotocoreSession

        session = BotocoreSession()
        creds = session.get_credentials()
        if creds:
            creds = creds.get_frozen_credentials()
            region = settings.aws_region
            request = AWSRequest(method=method, url=url, headers=headers, data=body)
            SigV4Auth(creds, "neptune-db", region).add_auth(request)
            return dict(request.headers)
    except ImportError:
        log.debug("botocore not available — sending unsigned Neptune request")
    except Exception as e:
        log.warning("SigV4 signing failed for Neptune: %s", e)
    return headers


def _clear_repo_structural_graph(org_repo: str) -> bool:
    """Delete all code-index-sourced entities and relationships for a repo.

    Only deletes nodes with source='code-index', preserving LLM-extracted
    wiki entities (source='wiki') in the same graph.
    """
    neptune_url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
    query = f"g.V().has('repo', '{org_repo}').has('source', 'code-index').drop()"
    body = json.dumps({"gremlin": query})
    headers = _get_neptune_signed_headers("POST", neptune_url, body)
    try:
        resp = requests.post(
            neptune_url, data=body, headers=headers, timeout=60, verify=NEPTUNE_CA_BUNDLE
        )
        if resp.status_code < 400:
            log.info("Cleared structural graph for %s", org_repo)
            return True
        log.warning("Clear structural graph failed for %s: HTTP %d", org_repo, resp.status_code)
        return False
    except Exception as e:
        log.warning("Clear structural graph failed for %s: %s", org_repo, e)
        return False


def _write_to_neptune(entities: list[dict], relationships: list[dict], org_repo: str) -> bool:
    """Write entities and relationships to Neptune via Gremlin HTTP API with IAM auth."""
    neptune_url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"

    try:
        # Batch upsert vertices
        for ent in entities:
            props = {k: v for k, v in ent.items() if v and isinstance(v, (str, int, float))}
            prop_steps = "".join(
                f".property('{k}', '{str(v).replace(chr(39), chr(92) + chr(39))}')"
                for k, v in props.items()
                if k != "entity_id"
            )
            query = (
                f"g.V().has('entity_id', '{ent['entity_id']}').fold()"
                f".coalesce(unfold(), addV('entity').property('entity_id', '{ent['entity_id']}'))"
                f"{prop_steps}"
            )
            body = json.dumps({"gremlin": query})
            signed_headers = _get_neptune_signed_headers("POST", neptune_url, body)
            resp = requests.post(
                neptune_url,
                data=body,
                headers=signed_headers,
                timeout=30,
                verify=NEPTUNE_CA_BUNDLE,
            )
            if resp.status_code >= 400:
                log.warning(
                    "Neptune vertex write failed for %s: HTTP %d — %s",
                    ent.get("entity_id", "?"),
                    resp.status_code,
                    resp.text[:200],
                )

        # Batch upsert edges
        for rel in relationships:
            from_id = rel.get("from", "")
            to_id = rel.get("to", "")
            rel_type = rel.get("type", "related_to")
            query = (
                f"g.V().has('entity_id', '{from_id}').as('a')"
                f".V().has('entity_id', '{to_id}').as('b')"
                f".select('a').coalesce("
                f"  outE('{rel_type}').where(inV().as('b')),"
                f"  addE('{rel_type}').to('b')"
                f")"
            )
            body = json.dumps({"gremlin": query})
            signed_headers = _get_neptune_signed_headers("POST", neptune_url, body)
            resp = requests.post(
                neptune_url,
                data=body,
                headers=signed_headers,
                timeout=30,
                verify=NEPTUNE_CA_BUNDLE,
            )
            if resp.status_code >= 400:
                log.debug(
                    "Neptune edge write failed: HTTP %d for %s->%s",
                    resp.status_code,
                    from_id[:50],
                    to_id[:50],
                )

        log.info(
            "GraphRAG: wrote %d entities + %d relationships for %s",
            len(entities),
            len(relationships),
            org_repo,
        )
        return True
    except Exception as e:
        log.warning("Neptune write failed for %s: %s", org_repo, e)
        return False


# ---------------------------------------------------------------------------
# SCIP structural graph ingestion (#1532 — Neptune deep graph)
# ---------------------------------------------------------------------------


def scip_structural_ingest(
    clone_path: str,
    org_repo: str,
    s3_store: S3ContentStore,
) -> dict[str, Any]:
    """Run SCIP-native structural graph ingestion for a repository.

    Pipeline: detect languages → resolve deps → scip-<lang> index → .scip
      → decode protobuf → enclosing-scope resolution → graph → Neptune CSV
      → S3 upload → Neptune load (openCypher UNWIND batch)

    Fail-loud: a code-bearing repo producing 0 edges → ERROR.

    Returns:
        Dict with status and metrics:
        - status: "complete", "indexing_failed", "no_languages", "no_edges"
        - nodes, edges, calls, references counts
    """
    import tempfile

    result: dict[str, Any] = {"status": "pending"}

    # Step 1: Detect languages
    lang_counts = detect_languages(clone_path)
    if not lang_counts:
        log.info("No SCIP-supported languages in %s — skipping SCIP indexing", org_repo)
        result["status"] = "no_languages"
        return result

    result["languages"] = lang_counts
    log.info("SCIP indexing %s — languages: %s", org_repo, lang_counts)

    # Step 2: Index repo (dep resolution + scip-<lang>)
    try:
        indexing_report = scip_index_repo(clone_path, org_repo)
    except Exception as e:
        log.error("SCIP indexing failed for %s: %s", org_repo, e)
        result["status"] = "indexing_failed"
        result["error"] = str(e)
        return result

    if not indexing_report.any_success:
        errors = [r.error for r in indexing_report.results if r.error]
        log.error("SCIP indexing produced no .scip for %s: %s", org_repo, errors)
        result["status"] = "indexing_failed"
        result["errors"] = errors
        return result

    scip_path = indexing_report.combined_scip_path
    result["indexed_language"] = indexing_report.successful_languages[0]
    result["dep_resolution"] = indexing_report.results[0].dep_resolution

    # Step 3: Decode .scip and build graph
    try:
        graph = ingest_scip(scip_path, org_repo)
    except FileNotFoundError as e:
        log.error("SCIP file not found for %s: %s", org_repo, e)
        result["status"] = "indexing_failed"
        result["error"] = str(e)
        return result

    # Fail-loud: code-bearing repo with 0 edges → ERROR
    if graph.edge_count == 0:
        log.error(
            "FAIL-LOUD: SCIP produced 0 edges for %s (languages: %s). "
            "This indicates failed dep resolution or indexer issue.",
            org_repo,
            lang_counts,
        )
        result["status"] = "no_edges"
        result["nodes"] = graph.node_count
        result["edges"] = 0
        return result

    result["nodes"] = graph.node_count
    result["edges"] = graph.edge_count
    result["calls"] = graph.calls_count
    result["references"] = graph.references_count

    # Step 4: Generate Neptune CSV
    csv_output_dir = tempfile.mkdtemp(prefix="scip-neptune-")
    try:
        csv_output = scip_generate_csv(graph, csv_output_dir)

        # Generate summary
        summary_path = os.path.join(csv_output_dir, "extraction_summary.json")
        scip_generate_summary(graph, csv_output, summary_path)

        # Step 5: Upload CSV to S3
        if S3_BUCKET_NAME:
            from scip_neptune_loader import upload_csv_to_s3

            s3_result = upload_csv_to_s3(csv_output, S3_BUCKET_NAME, org_repo, AWS_REGION)
            result["s3_upload"] = s3_result.get("s3_prefix", "")

        # Step 6: Load into Neptune (if endpoint configured)
        if NEPTUNE_ENDPOINT:
            from scip_neptune_loader import load_to_neptune

            neptune_ep = f"{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}"
            load_result = load_to_neptune(csv_output, neptune_ep, AWS_REGION)
            result["neptune_load"] = load_result
            if load_result.get("success"):
                result["status"] = "complete"
            else:
                result["status"] = "load_partial"
                log.warning(
                    "Neptune load had errors for %s: %s",
                    org_repo,
                    load_result.get("total_errors", 0),
                )
        else:
            # No Neptune endpoint — CSV + S3 only
            result["status"] = "complete"
            log.info(
                "SCIP graph for %s: %d nodes, %d edges (Neptune not configured — CSV only)",
                org_repo,
                graph.node_count,
                graph.edge_count,
            )
    finally:
        # Clean up temp CSV directory
        import shutil as _shutil

        _shutil.rmtree(csv_output_dir, ignore_errors=True)

    # Clean up indexing artifacts from the clone
    cleanup_indexing_artifacts(clone_path)

    log.info(
        "SCIP structural ingest complete for %s: %d nodes, %d edges (%d CALLS, %d REFERENCES)",
        org_repo,
        graph.node_count,
        graph.edge_count,
        graph.calls_count,
        graph.references_count,
    )
    return result


# ---------------------------------------------------------------------------
# Zoekt indexing — build .zoekt shards and upload to S3 (#2361)
# ---------------------------------------------------------------------------


def _run_zoekt_index(
    clone_path: str,
    org_repo: str,
    s3_store: S3ContentStore,
) -> dict[str, Any]:
    """Run zoekt-git-index on a cloned repo and upload shards to S3.

    Steps:
      1. Run `zoekt-git-index -index <tmpdir> <clone_path>` -> .zoekt shard files
      2. Upload each .zoekt shard to s3://<bucket>/zoekt-shards/<org>/<repo>/
      3. Verify at least one shard via head_object

    Returns dict with keys: status, shards, shard_bytes, artifact_key, error
    """
    import glob
    import tempfile

    zoekt_timeout = ZOEKT_INDEX_TIMEOUT
    shards_prefix = ZOEKT_SHARDS_S3_PREFIX

    # Create a temp directory for index output
    index_dir = tempfile.mkdtemp(prefix="zoekt-index-")

    try:
        # Step 1: Run zoekt-git-index
        try:
            subprocess.run(
                ["zoekt-git-index", "-index", index_dir, clone_path],
                check=True,
                capture_output=True,
                timeout=zoekt_timeout,
            )
        except FileNotFoundError:
            log.warning("zoekt-git-index binary not found — skipping for %s", org_repo)
            return {"status": "binary_not_found", "error": "zoekt-git-index not installed"}
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode()[:500] if e.stderr else ""
            log.warning("zoekt-git-index exited %d for %s: %s", e.returncode, org_repo, stderr)
            return {"status": "index_failed", "error": f"exit code {e.returncode}: {stderr}"}
        except subprocess.TimeoutExpired:
            log.warning("zoekt-git-index timed out (%ds) for %s", zoekt_timeout, org_repo)
            return {"status": "timeout", "error": f"timed out after {zoekt_timeout}s"}

        # Step 2: Find and upload .zoekt shard files
        shard_files = glob.glob(os.path.join(index_dir, "*.zoekt"))
        if not shard_files:
            log.warning("zoekt-git-index produced no shards for %s", org_repo)
            return {"status": "no_shards", "error": "no .zoekt files produced"}

        total_bytes = 0
        uploaded_keys = []
        s3_key_prefix = f"{shards_prefix}/{org_repo}"

        for shard_path in shard_files:
            shard_name = os.path.basename(shard_path)
            s3_key = f"{s3_key_prefix}/{shard_name}"
            shard_size = os.path.getsize(shard_path)
            total_bytes += shard_size

            try:
                # Use upload_file for large shards (auto multipart for files > 8MB)
                s3_store._s3.upload_file(
                    Filename=shard_path,
                    Bucket=s3_store.bucket_name,
                    Key=s3_key,
                    ExtraArgs={
                        "ContentType": "application/octet-stream",
                        "Metadata": {
                            "org_repo": org_repo,
                            "shard_name": shard_name,
                        },
                    },
                )
                uploaded_keys.append(s3_key)
                log.info(
                    "Zoekt shard uploaded: s3://%s/%s (%d bytes)",
                    s3_store.bucket_name,
                    s3_key,
                    shard_size,
                )
            except Exception as e:
                log.error("S3 upload failed for zoekt shard %s of %s: %s", shard_name, org_repo, e)
                return {
                    "status": "upload_failed",
                    "error": f"S3 upload failed for {shard_name}: {e}",
                    "shards": len(uploaded_keys),
                    "shard_bytes": total_bytes,
                }

        # Step 3: Verify first uploaded shard via head_object
        if uploaded_keys:
            try:
                s3_store._s3.head_object(Bucket=s3_store.bucket_name, Key=uploaded_keys[0])
            except Exception as e:
                log.warning("Zoekt shard verification failed for %s: %s", uploaded_keys[0], e)
                return {
                    "status": "verify_failed",
                    "error": f"head_object failed: {e}",
                    "shards": len(uploaded_keys),
                    "shard_bytes": total_bytes,
                    "artifact_key": uploaded_keys[0],
                }

        log.info(
            "Zoekt indexing complete for %s: %d shards, %d bytes total",
            org_repo,
            len(uploaded_keys),
            total_bytes,
        )
        return {
            "status": "complete",
            "shards": len(uploaded_keys),
            "shard_bytes": total_bytes,
            "artifact_key": uploaded_keys[0] if uploaded_keys else "",
        }

    finally:
        # Clean up temp index directory
        shutil.rmtree(index_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Source SBOM generation (Rail 1 — #1358)
# ---------------------------------------------------------------------------


def _generate_source_sbom(
    clone_path: str,
    org_repo: str,
    s3_store: S3ContentStore,
    sbom_s3_prefix: str | None = None,
) -> str:
    """Run Syft against a cloned repo directory and store the CycloneDX SBOM.

    Steps:
      1. Run `syft dir:{clone_path} -o cyclonedx-json` -> /tmp SBOM file
      2. Upload CycloneDX JSON to S3
      3. Parse dependencies and upsert into Postgres (best-effort)

    Returns: "complete", "syft_failed", or "failed"
    """
    import tempfile

    from sbom_parser import parse_cyclonedx

    if sbom_s3_prefix is None:
        sbom_s3_prefix = settings.sbom_s3_prefix
    syft_timeout = settings.syft_timeout

    # Generate a safe slug for the S3 key
    safe_name = org_repo.replace("/", "-")
    sbom_filename = f"sbom-source-{safe_name}.cdx.json"
    sbom_path = os.path.join(tempfile.gettempdir(), sbom_filename)

    # Step 1: Run Syft
    try:
        syft_result = subprocess.run(
            [
                "syft",
                f"dir:{clone_path}",
                "-o",
                f"cyclonedx-json={sbom_path}",
                "--quiet",
            ],
            capture_output=True,
            timeout=syft_timeout,
        )
        if syft_result.returncode != 0:
            stderr = syft_result.stderr.decode()[:500] if syft_result.stderr else ""
            log.warning("Syft exited %d for %s: %s", syft_result.returncode, org_repo, stderr)
            return "syft_failed"
    except FileNotFoundError:
        log.warning("Syft binary not found — skipping source SBOM for %s", org_repo)
        return "syft_not_installed"
    except subprocess.TimeoutExpired:
        log.warning("Syft timed out (%ds) for %s", syft_timeout, org_repo)
        return "syft_timeout"

    # Verify output file exists and is non-empty
    if not os.path.isfile(sbom_path) or os.path.getsize(sbom_path) == 0:
        log.warning("Syft produced no output for %s", org_repo)
        return "syft_failed"

    # Step 2: Upload to S3
    s3_key = f"{sbom_s3_prefix}/repos/{org_repo}/source.cdx.json"
    try:
        with open(sbom_path, "rb") as f:
            sbom_bytes = f.read()

        s3_store._s3.put_object(
            Bucket=s3_store.bucket_name,
            Key=s3_key,
            Body=sbom_bytes,
            ContentType="application/json",
            Metadata={
                "org_repo": org_repo,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sbom_type": "source",
            },
        )
        log.info(
            "Source SBOM uploaded: s3://%s/%s (%d bytes)",
            s3_store.bucket_name,
            s3_key,
            len(sbom_bytes),
        )
    except Exception as e:
        log.error("S3 upload failed for source SBOM of %s: %s", org_repo, e)
        # S3 is the durable record — if it fails, report failure
        return "failed"

    # Step 3: Parse and upsert to Postgres (best-effort — non-blocking)
    if settings.sbom_db_enabled:
        try:
            records = parse_cyclonedx(sbom_bytes.decode("utf-8"), source="code")
            if records:
                import db as sbom_db

                conn = sbom_db.get_connection()
                try:
                    git_url = f"https://github.com/{org_repo}"
                    repo_id = sbom_db.ensure_repo_exists(conn, org_repo, git_url)
                    sbom_db.upsert_dependencies(conn, repo_id, records)
                    # Get current SHA for status update
                    sha = None
                    git_head = os.path.join(clone_path, ".git")
                    if os.path.exists(git_head):
                        sha_result = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=clone_path,
                            capture_output=True,
                            timeout=10,
                        )
                        if sha_result.returncode == 0:
                            sha = sha_result.stdout.decode().strip()
                    sbom_db.update_repo_sbom_status(
                        conn, repo_id, source_status="complete", last_source_sha=sha
                    )
                finally:
                    conn.close()
                log.info("Wrote %d dependency rows to Postgres for %s", len(records), org_repo)
        except Exception as e:
            # Postgres insert is best-effort — S3 is the durable record
            log.warning("Postgres upsert failed for %s (non-blocking): %s", org_repo, e)

    # Detect Dockerfiles for Rail 2 metadata
    _detect_dockerfiles(clone_path, org_repo)

    # Clean up temp file
    try:
        os.unlink(sbom_path)
    except OSError:
        pass

    return "complete"


def _detect_dockerfiles(clone_path: str, org_repo: str) -> None:
    """Detect Dockerfiles in the repo and record in DynamoDB for Rail 2 triggering."""
    dockerfiles = []
    for root, _dirs, files in os.walk(clone_path):
        # Skip common vendor/build directories
        rel_root = os.path.relpath(root, clone_path)
        if any(skip in rel_root for skip in (".git", "node_modules", ".terraform", "vendor")):
            continue
        for f in files:
            if f == "Dockerfile" or f.endswith(".Dockerfile"):
                rel_path = os.path.join(rel_root, f) if rel_root != "." else f
                dockerfiles.append(rel_path)

    if dockerfiles:
        log.info("Found %d Dockerfile(s) in %s: %s", len(dockerfiles), org_repo, dockerfiles[:5])
        # Record in DynamoDB for Rail 2 image SBOM triggering
        table = _get_dynamodb_table()
        if table:
            try:
                pk = f"repo#{org_repo}"
                table.update_item(
                    Key={"source": pk, "record_type": "STATE"},
                    UpdateExpression="SET has_dockerfile = :hd, dockerfiles = :dfs",
                    ExpressionAttributeValues={
                        ":hd": True,
                        ":dfs": dockerfiles[:20],  # Cap to avoid DynamoDB item size limits
                    },
                )
            except Exception as e:
                log.debug("DynamoDB dockerfile update failed for %s: %s", org_repo, e)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _get_commit_sha(clone_path: str) -> str | None:
    """Get the HEAD commit SHA from a clone. Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.decode().strip()
    except Exception:
        pass
    return None


def _verify_s3_object(s3_store: S3ContentStore, key: str) -> bool:
    """Read-back verify an S3 object exists via head_object."""
    try:
        s3_store._s3.head_object(Bucket=s3_store.bucket_name, Key=key)
        return True
    except Exception:
        return False


def ingest_repo(
    org_repo: str,
    skip_cgc: bool = False,
    skip_deepwiki: bool = False,
    skip_scip: bool = False,
) -> dict[str, Any]:
    """Full ingestion pipeline for one repo.

    Returns a result dict with status for each step.
    Each producer stage follows the verify-after-write contract:
    attempt -> write row -> produce -> read-back verify -> only-then record verified.
    """
    result: dict[str, Any] = {
        "repo": org_repo,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zoekt_index": "skipped",
        "s3_upload": "skipped",
        "code_index": "skipped",
        "deepwiki": "skipped",
        "graphrag": "skipped",
        "scip_structural": "skipped",
        "sbom_source": "skipped",
    }

    # Read scope from environment (propagated by sqs-worker for tenant isolation)
    scope = parse_scope_from_env()
    scoped_content_prefix = compute_s3_prefix(scope, S3_CONTENT_PREFIX)
    scoped_wiki_prefix = compute_s3_prefix(scope, WIKI_S3_PREFIX)
    scoped_code_index_prefix = compute_s3_prefix(scope, CODE_INDEX_S3_PREFIX)
    scoped_sbom_prefix = compute_s3_prefix(scope, settings.sbom_s3_prefix)

    if not scope.is_shared:
        log.info(
            "Scoped ingestion: visibility=%s tenant_id=%s owner_sub=%s",
            scope.visibility,
            scope.tenant_id,
            scope.owner_sub,
        )

    # Initialize the S3 content store and writer adapter
    s3_store = S3ContentStore(
        bucket_name=S3_BUCKET_NAME,
        prefix=scoped_content_prefix,
        region_name=AWS_REGION,
    )
    s3_writer = _S3WriterAdapter(s3_store)

    # --- Stage tracking setup (Postgres) ---
    # Best-effort: if DB is unavailable, fall back to legacy behavior
    tracker = None
    db_conn = None
    try:
        import db as stage_db

        db_conn = stage_db.get_connection()
        repo_id = stage_db.ensure_repo_exists(db_conn, org_repo, f"https://github.com/{org_repo}")
    except Exception as e:
        log.warning("DB unavailable for stage tracking — legacy mode: %s", e)
        db_conn = None
        repo_id = None

    # Step 1: Clone to persistent storage (S3 Files mount) — shared across enrichment consumers
    # If clone exists, do git fetch instead of full re-clone
    clone_path = os.path.join(CLONE_BASE, org_repo)
    if os.path.exists(os.path.join(clone_path, ".git")):
        try:
            subprocess.run(
                ["git", "fetch", "--depth=1"],
                cwd=clone_path,
                check=True,
                capture_output=True,
                timeout=120,
            )
            subprocess.run(
                ["git", "reset", "--hard", "FETCH_HEAD"],
                cwd=clone_path,
                check=True,
                capture_output=True,
                timeout=30,
            )
            log.info("Updated existing clone: %s", clone_path)
            clone_ok = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("git fetch failed, re-cloning: %s", e)
            shutil.rmtree(clone_path, ignore_errors=True)
            clone_ok = git_clone(f"https://github.com/{org_repo}", clone_path)
    else:
        if os.path.exists(clone_path):
            shutil.rmtree(clone_path, ignore_errors=True)
        clone_ok = git_clone(f"https://github.com/{org_repo}", clone_path)
    if not clone_ok:
        log.warning("Clone failed for %s — enrichment steps skipped", org_repo)
        result["clone"] = "failed"
        if db_conn:
            db_conn.close()
        return result
    result["clone"] = "ok"

    # Get commit SHA for skip logic (must be after clone)
    commit_sha = _get_commit_sha(clone_path)

    # Initialize the stage tracker now that we have commit_sha
    if db_conn and repo_id:
        try:
            tracker = StageTracker(db_conn, org_repo, repo_id, commit_sha)
            log.info("Stage tracker initialized: run_id=%s sha=%s", tracker.run_id, commit_sha)
        except Exception as e:
            log.warning("Failed to create stage tracker: %s — legacy mode", e)
            tracker = None

    # Record clone stage as verified (clone_path exists)
    if tracker:
        try:
            with tracker.stage("clone") as ctx:
                ctx.set_artifact(clone_path)
                ctx.verify(lambda: os.path.exists(os.path.join(clone_path, ".git")))
        except Exception as e:
            log.warning("Clone stage tracking failed: %s", e)

    # Step 1b: Zoekt indexing — build .zoekt shards and upload to S3 (#2361)
    if ZOEKT_INDEX_ENABLED:
        skip_zoekt_stage = tracker and tracker.should_skip("zoekt_index")
        if skip_zoekt_stage:
            log.info("Skipping zoekt_index for %s — already verified at %s", org_repo, commit_sha)
            tracker.mark_skipped("zoekt_index", "already verified at current SHA")
            result["zoekt_index"] = "skipped_verified"
        else:
            try:
                zoekt_result = _run_zoekt_index(clone_path, org_repo, s3_store)
                result["zoekt_index"] = zoekt_result.get("status", "unknown")

                # Stage tracking
                if tracker:
                    try:
                        zoekt_status = zoekt_result.get("status", "")
                        if zoekt_status == "complete":
                            with tracker.stage("zoekt_index") as ctx:
                                shard_count = zoekt_result.get("shards", 0)
                                shard_bytes = zoekt_result.get("shard_bytes", 0)
                                artifact_key = zoekt_result.get("artifact_key", "")
                                ctx.set_artifact(artifact_key)
                                ctx.set_metrics(
                                    {
                                        "shards": shard_count,
                                        "shard_bytes": shard_bytes,
                                    }
                                )
                                ctx.verify(lambda: _verify_s3_object(s3_store, artifact_key))
                        else:
                            with tracker.stage("zoekt_index") as ctx:
                                ctx.fail(zoekt_result.get("error", f"status={zoekt_status}"))
                    except Exception as e:
                        log.warning("zoekt_index stage tracking failed: %s", e)
            except Exception as e:
                log.warning("Zoekt indexing failed for %s: %s — continuing", org_repo, e)
                result["zoekt_index"] = f"error: {e}"
                if tracker:
                    try:
                        with tracker.stage("zoekt_index") as ctx:
                            ctx.fail(str(e))
                    except Exception:
                        pass
    elif tracker:
        tracker.mark_skipped("zoekt_index", "zoekt_index disabled")

    # Step 2: Run cgc -> code-index.json -> filesystem + S3 markdown summary
    if not skip_cgc:
        # Check if we can skip (already verified at this SHA)
        skip_cgc_stage = tracker and tracker.should_skip("cgc_structural")
        if skip_cgc_stage:
            log.info(
                "Skipping cgc_structural for %s — already verified at %s", org_repo, commit_sha
            )
            tracker.mark_skipped("cgc_structural", "already verified at current SHA")
            result["code_index"] = "skipped_verified"
            result["s3_upload"] = "skipped_verified"
        else:
            try:
                code_index = cgc_analyze(clone_path, org_repo)
                if code_index:
                    code_index_json = json.dumps(code_index, indent=2)
                    safe_name = org_repo.replace("/", "-")

                    # Write to filesystem (primary — for programmatic access by MCP server)
                    fs_written = _write_code_index_to_filesystem(
                        code_index_json, safe_name, org_repo
                    )

                    # Upload as markdown summary to S3 (for semantic search/understand)
                    code_index_md = _code_index_to_markdown(code_index)
                    s3_key = store_code_index_to_s3(
                        code_index_md,
                        org_repo,
                        s3_writer=s3_writer,
                        s3_bucket=S3_BUCKET_NAME,
                        code_index_s3_prefix=scoped_code_index_prefix,
                    )

                    if s3_key:
                        result["s3_upload"] = "ok"
                        log.info("Code-index markdown uploaded to S3: %s", s3_key)
                    else:
                        result["s3_upload"] = "failed"

                    if fs_written:
                        result["code_index"] = "written"
                    else:
                        result["code_index"] = "fs_write_failed"
                    log.info(
                        "Code index for %s: %d symbols, %d files with imports",
                        org_repo,
                        len(code_index.get("symbols", [])),
                        len(code_index.get("imports", {})),
                    )

                    # Stage tracking: verify the artifact was written
                    if tracker and s3_key:
                        try:
                            with tracker.stage("cgc_structural") as ctx:
                                ctx.set_artifact(s3_key)
                                ctx.set_metrics(
                                    {
                                        "symbols": len(code_index.get("symbols", [])),
                                        "files": len(code_index.get("imports", {})),
                                    }
                                )
                                ctx.verify(lambda: _verify_s3_object(s3_store, s3_key))
                        except Exception as e:
                            log.warning("cgc_structural stage tracking failed: %s", e)
                    elif tracker:
                        try:
                            with tracker.stage("cgc_structural") as ctx:
                                ctx.fail("S3 upload returned no key")
                        except Exception as e:
                            log.warning("cgc_structural stage tracking failed: %s", e)
                else:
                    result["code_index"] = "analysis_failed"
                    if tracker:
                        try:
                            with tracker.stage("cgc_structural") as ctx:
                                ctx.fail("cgc analysis returned no data")
                        except Exception as e:
                            log.warning("cgc_structural stage tracking failed: %s", e)
            except Exception as e:
                log.warning("cgc failed for %s: %s — continuing without code-index", org_repo, e)
                result["code_index"] = f"error: {e}"
                if tracker:
                    try:
                        with tracker.stage("cgc_structural") as ctx:
                            ctx.fail(str(e))
                    except Exception:
                        pass
    elif tracker:
        tracker.mark_skipped("cgc_structural", "skip_cgc flag set")

    # Step 3: Generate DeepWiki wiki -> upload to S3 + S3 Vectors
    if not skip_deepwiki and DEEPWIKI_ENABLED:
        skip_deepwiki_stage = tracker and tracker.should_skip("deepwiki")
        if skip_deepwiki_stage:
            log.info("Skipping deepwiki for %s — already verified at %s", org_repo, commit_sha)
            tracker.mark_skipped("deepwiki", "already verified at current SHA")
            result["deepwiki"] = "skipped_verified"
        else:
            try:
                wiki = deepwiki_generate(org_repo)
                if wiki:
                    org_id = org_repo.split("/")[0]
                    allowed_principals = ["*"]

                    wiki_result = store_wiki(
                        wiki_text=wiki,
                        org_repo=org_repo,
                        org_id=org_id,
                        allowed_principals=allowed_principals,
                        s3_writer=s3_writer,
                        s3_bucket=S3_BUCKET_NAME,
                        wiki_s3_prefix=scoped_wiki_prefix,
                        shard_count=S3_VECTORS_SHARD_COUNT,
                    )

                    if wiki_result.s3_success:
                        result["deepwiki"] = "uploaded"
                        if result["s3_upload"] != "ok":
                            result["s3_upload"] = "ok"

                        # Stage tracking: verify wiki artifact
                        if tracker and wiki_result.s3_key:
                            wiki_s3_key = wiki_result.s3_key
                            try:
                                with tracker.stage("deepwiki") as ctx:
                                    ctx.set_artifact(wiki_s3_key)
                                    ctx.set_metrics({"chars": len(wiki)})
                                    ctx.verify(lambda: _verify_s3_object(s3_store, wiki_s3_key))
                            except Exception as e:
                                log.warning("deepwiki stage tracking failed: %s", e)
                    else:
                        result["deepwiki"] = "upload_failed"
                        if tracker:
                            try:
                                with tracker.stage("deepwiki") as ctx:
                                    ctx.fail("wiki S3 upload failed")
                            except Exception as e:
                                log.warning("deepwiki stage tracking failed: %s", e)
                else:
                    result["deepwiki"] = "generation_failed"
                    if tracker:
                        try:
                            with tracker.stage("deepwiki") as ctx:
                                ctx.fail("DeepWiki generation returned no content")
                        except Exception as e:
                            log.warning("deepwiki stage tracking failed: %s", e)
            except Exception as e:
                log.warning("DeepWiki failed for %s: %s — continuing without wiki", org_repo, e)
                result["deepwiki"] = f"error: {e}"
                if tracker:
                    try:
                        with tracker.stage("deepwiki") as ctx:
                            ctx.fail(str(e))
                    except Exception:
                        pass
    else:
        if tracker:
            reason = "skip_deepwiki flag" if skip_deepwiki else "deepwiki disabled"
            tracker.mark_skipped("deepwiki", reason)

    # Step 4: GraphRAG extraction — entities and relationships into Neptune
    if GRAPHRAG_ENABLED:
        skip_graphrag_stage = tracker and tracker.should_skip("graphrag")
        if skip_graphrag_stage:
            log.info("Skipping graphrag for %s — already verified at %s", org_repo, commit_sha)
            tracker.mark_skipped("graphrag", "already verified at current SHA")
            result["graphrag"] = "skipped_verified"
        else:
            try:
                ci_data = None
                if result.get("code_index") == "written":
                    ci_path = os.path.join(CODE_INDEX_DIR, f"{org_repo.replace('/', '-')}.json")
                    if os.path.isfile(ci_path):
                        with open(ci_path) as f:
                            ci_data = json.load(f)

                wiki_content = None
                if result.get("deepwiki") == "uploaded":
                    wiki_safe = org_repo.replace("/", "-")
                    wiki_path = os.path.join("/platform-data/wikis", f"{wiki_safe}-wiki.md")
                    if os.path.isfile(wiki_path):
                        try:
                            with open(wiki_path, "r", encoding="utf-8") as wf:
                                wiki_content = wf.read()
                        except OSError:
                            pass

                graphrag_result = graphrag_extract(clone_path, org_repo, ci_data, wiki_content)
                if graphrag_result:
                    result["graphrag"] = (
                        f"{graphrag_result['entities']} entities,"
                        f" {graphrag_result['relationships']} relationships"
                    )
                    # Stage tracking: GraphRAG doesn't have a simple S3 artifact,
                    # but we can verify entities were written
                    if tracker:
                        try:
                            with tracker.stage("graphrag") as ctx:
                                entity_count = graphrag_result.get("entities", 0)
                                rel_count = graphrag_result.get("relationships", 0)
                                ctx.set_artifact(f"neptune:{org_repo}:entities={entity_count}")
                                ctx.set_metrics(
                                    {
                                        "entities": entity_count,
                                        "relationships": rel_count,
                                    }
                                )
                                ctx.verify(lambda: entity_count > 0)
                        except Exception as e:
                            log.warning("graphrag stage tracking failed: %s", e)
                else:
                    result["graphrag"] = "skipped"
                    if tracker:
                        tracker.mark_skipped("graphrag", "no data to extract")
            except Exception as e:
                log.warning("GraphRAG failed for %s: %s — continuing", org_repo, e)
                result["graphrag"] = f"error: {e}"
                if tracker:
                    try:
                        with tracker.stage("graphrag") as ctx:
                            ctx.fail(str(e))
                    except Exception:
                        pass
    elif tracker:
        tracker.mark_skipped("graphrag", "graphrag disabled")

    # Step 5a: SCIP structural graph ingestion (#1532 — Neptune deep graph)
    if SCIP_ENABLED and not skip_scip:
        skip_scip_stage = tracker and tracker.should_skip("scip_structural")
        if skip_scip_stage:
            log.info(
                "Skipping scip_structural for %s — already verified at %s", org_repo, commit_sha
            )
            tracker.mark_skipped("scip_structural", "already verified at current SHA")
            result["scip_structural"] = "skipped_verified"
        else:
            try:
                scip_result = scip_structural_ingest(clone_path, org_repo, s3_store)
                result["scip_structural"] = scip_result.get("status", "unknown")

                # Stage tracking
                if tracker:
                    try:
                        scip_status = scip_result.get("status", "")
                        if scip_status == "complete":
                            with tracker.stage("scip_structural") as ctx:
                                edge_count = scip_result.get("edges", 0)
                                node_count = scip_result.get("nodes", 0)
                                ctx.set_artifact(f"neptune:{org_repo}:edges={edge_count}")
                                ctx.set_metrics(
                                    {
                                        "nodes": node_count,
                                        "edges": edge_count,
                                    }
                                )
                                ctx.verify(lambda: edge_count > 0)
                        elif scip_status == "no_languages":
                            tracker.mark_skipped("scip_structural", "no SCIP-supported languages")
                        elif scip_status == "no_edges":
                            with tracker.stage("scip_structural") as ctx:
                                ctx.fail(
                                    "FAIL-LOUD: code-bearing repo produced 0 edges "
                                    f"(languages: {scip_result.get('languages', {})})"
                                )
                        else:
                            with tracker.stage("scip_structural") as ctx:
                                ctx.fail(scip_result.get("error", f"status={scip_status}"))
                    except Exception as e:
                        log.warning("scip_structural stage tracking failed: %s", e)
            except Exception as e:
                log.warning("SCIP structural ingest failed for %s: %s — continuing", org_repo, e)
                result["scip_structural"] = f"error: {e}"
                if tracker:
                    try:
                        with tracker.stage("scip_structural") as ctx:
                            ctx.fail(str(e))
                    except Exception:
                        pass
    elif tracker:
        reason = "skip_scip flag set" if skip_scip else "scip disabled"
        tracker.mark_skipped("scip_structural", reason)

    # Step 5b: Source SBOM generation (Rail 1 — #1358)
    if settings.sbom_enabled:
        skip_sbom_stage = tracker and tracker.should_skip("sbom_source")
        if skip_sbom_stage:
            log.info("Skipping sbom_source for %s — already verified at %s", org_repo, commit_sha)
            tracker.mark_skipped("sbom_source", "already verified at current SHA")
            result["sbom_source"] = "skipped_verified"
        else:
            try:
                sbom_result = _generate_source_sbom(
                    clone_path, org_repo, s3_store, sbom_s3_prefix=scoped_sbom_prefix
                )
                result["sbom_source"] = sbom_result

                # Stage tracking: verify the SBOM artifact in S3
                if tracker:
                    sbom_s3_key = f"{scoped_sbom_prefix}/repos/{org_repo}/source.cdx.json"
                    try:
                        if sbom_result == "complete":
                            with tracker.stage("sbom_source") as ctx:
                                ctx.set_artifact(sbom_s3_key)
                                # Count dependencies from the uploaded SBOM
                                dep_count = 0
                                try:
                                    obj = s3_store._s3.get_object(
                                        Bucket=s3_store.bucket_name, Key=sbom_s3_key
                                    )
                                    sbom_data = json.loads(obj["Body"].read())
                                    dep_count = len(sbom_data.get("components", []))
                                except Exception:
                                    pass  # best-effort count
                                ctx.set_metrics({"dependencies": dep_count})
                                ctx.verify(lambda: _verify_s3_object(s3_store, sbom_s3_key))
                        else:
                            with tracker.stage("sbom_source") as ctx:
                                ctx.fail(f"SBOM generation result: {sbom_result}")
                    except Exception as e:
                        log.warning("sbom_source stage tracking failed: %s", e)
            except Exception as e:
                log.warning("Source SBOM failed for %s: %s — continuing", org_repo, e)
                result["sbom_source"] = "failed"
                if tracker:
                    try:
                        with tracker.stage("sbom_source") as ctx:
                            ctx.fail(str(e))
                    except Exception:
                        pass
    elif tracker:
        tracker.mark_skipped("sbom_source", "sbom disabled")

    # Finalize the stage tracker (derive overall run status)
    if tracker:
        try:
            tracker.finalize()
            result["run_id"] = tracker.run_id
            log.info(
                "Index run %s finalized for %s (%d stages tracked)",
                tracker.run_id,
                org_repo,
                len(tracker.results),
            )
        except Exception as e:
            log.warning("Stage tracker finalize failed: %s", e)

    # Close DB connection
    if db_conn:
        try:
            db_conn.close()
        except Exception:
            pass

    # Step 6: Keep clone on persistent storage (S3 Files) — don't delete
    # Clone is reused by GraphRAG, learning artifacts, and daily refresh.
    # Only /tmp clones should be cleaned up.
    if CLONE_BASE.startswith("/tmp"):
        shutil.rmtree(clone_path, ignore_errors=True)
        log.info("Cleaned up temp clone %s", clone_path)
    else:
        log.info("Kept persistent clone at %s", clone_path)

    return result


def main():
    parser = argparse.ArgumentParser(description="Ingest a GitHub repo into the platform")
    parser.add_argument(
        "--repo", required=True, help="org/repo to ingest (e.g., aws-samples/bedrock-chat)"
    )
    parser.add_argument("--skip-cgc", action="store_true", help="Skip code-index generation")
    parser.add_argument("--skip-deepwiki", action="store_true", help="Skip DeepWiki generation")
    parser.add_argument("--skip-graphrag", action="store_true", help="Skip GraphRAG extraction")
    parser.add_argument(
        "--skip-scip", action="store_true", help="Skip SCIP structural graph ingestion"
    )
    parser.add_argument("--tags", default="{}", help="JSON tags object for metadata")
    args = parser.parse_args()

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError:
        tags = {}

    result = ingest_repo(
        org_repo=args.repo,
        skip_cgc=args.skip_cgc,
        skip_deepwiki=args.skip_deepwiki,
        skip_scip=args.skip_scip,
    )

    # Update DynamoDB state (replaces repo-state.json)
    update_dynamo_state(args.repo, result, tags=tags)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
