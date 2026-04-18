#!/usr/bin/env python3
"""Per-repo ingestion pipeline (Option C).

Pipeline steps:
  1. POST to OpenViking (GitHub URL) — it clones & indexes internally
  2. Clone repo to /tmp for enrichment
  3. Run cgc analyze -> code-index.json -> filesystem + markdown summary to OpenViking
  4. Call DeepWiki API -> wiki.md -> upload to OpenViking via temp_upload
  5. GraphRAG extraction -> Neptune (with delete-before-reload for stale entity cleanup)
  6. Cleanup temp clone

Usage:
  python ingest-repo.py --repo org/repo --ov-url http://openviking:1933 --ov-key ROOT_KEY
  python ingest-repo.py --repo org/repo --ov-url http://openviking:1933 --ov-key ROOT_KEY --skip-ov
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ingest-repo")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEEPWIKI_URL = os.getenv(
    "DEEPWIKI_URL", "http://deepwiki.agent-context.svc.cluster.local:8001"
)
DEEPWIKI_ENABLED = os.getenv("DEEPWIKI_ENABLED", "true").lower() == "true"
# Clone to persistent S3 Files mount instead of /tmp (shared across enrichment consumers)
CLONE_BASE = os.getenv("CLONE_BASE", "/platform-data/repos")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
CODE_INDEX_DIR = os.getenv("CODE_INDEX_DIR", "/platform-data/code-indexes")

# GraphRAG configuration
GRAPHRAG_ENABLED = os.getenv("GRAPHRAG_ENABLED", "false").lower() == "true"
NEPTUNE_ENDPOINT = os.getenv("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.getenv("NEPTUNE_PORT", "8182"))
OPENSEARCH_ENDPOINT = os.getenv("OPENSEARCH_ENDPOINT", "")
LLM_MODEL = os.getenv("WIKI_LLM_MODEL", "bedrock/global.anthropic.claude-opus-4-6-v1")
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL", "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"
)

# DynamoDB configuration (for state tracking — replaces repo-state.json)
DYNAMO_TABLE = os.getenv("DYNAMO_TABLE", "adp-context-service-state")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


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
        "openviking_status": "complete" if result.get("openviking") in ("submitted", "ok") else (
            "failed" if result.get("openviking") == "failed" else "skipped"
        ),
        "code_index_status": "complete" if result.get("code_index") == "written" else (
            "failed" if result.get("code_index") in ("failed", "fs_write_failed") else "skipped"
        ),
        "deepwiki_status": "complete" if result.get("deepwiki") in ("uploaded", "generated") else (
            "failed" if result.get("deepwiki") == "failed" else "skipped"
        ),
        "graphrag_status": "complete" if result.get("graphrag") == "ok" else (
            "failed" if result.get("graphrag") == "failed" else "skipped"
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
                ["git", "rev-parse", "HEAD"], cwd=clone_path,
                capture_output=True, timeout=10,
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
# OpenViking helpers
# ---------------------------------------------------------------------------


def ov_headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "default",
    }


def openviking_ingest(ov_url: str, headers: dict, org_repo: str) -> bool:
    """Submit a GitHub repo URL to OpenViking for ingestion (async)."""
    try:
        resp = requests.post(
            f"{ov_url}/api/v1/resources",
            headers={**headers, "Content-Type": "application/json"},
            json={"path": f"https://github.com/{org_repo}"},
            timeout=REQUEST_TIMEOUT,
        )
        log.info("OpenViking ingest %s: HTTP %d", org_repo, resp.status_code)
        return resp.status_code < 300
    except Exception as e:
        log.error("OpenViking ingest failed for %s: %s", org_repo, e)
        return False


def upload_to_openviking(
    ov_url: str,
    headers: dict,
    content: str | bytes,
    filename: str,
    target_uri: str,
) -> bool:
    """Upload a file to OpenViking.

    For enrichment files (.code-index.json, wiki.md) that belong inside an
    already-indexed repo, uses content/write (writes to the AGFS and triggers
    reindexing).  Falls back to temp_upload + resource add for new resources.
    """
    if isinstance(content, str):
        content_str = content
    else:
        content_str = content.decode("utf-8", errors="replace")

    # --- Primary path: content/write (works for files inside existing resources) ---
    try:
        resp = requests.post(
            f"{ov_url}/api/v1/content/write",
            headers={**headers, "Content-Type": "application/json"},
            json={"uri": target_uri, "content": content_str, "wait": False},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            log.info("Wrote %s -> %s via content/write", filename, target_uri)
            return True
        log.debug(
            "content/write returned %d for %s — trying temp_upload fallback",
            resp.status_code,
            target_uri,
        )
    except Exception as e:
        log.debug("content/write failed for %s: %s — trying temp_upload", target_uri, e)

    # --- Fallback: temp_upload + resource add (for new top-level resources) ---
    try:
        content_bytes = content_str.encode("utf-8")
        files = {"file": (filename, content_bytes, "application/octet-stream")}
        resp = requests.post(
            f"{ov_url}/api/v1/resources/temp_upload",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            files=files,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 300:
            log.warning(
                "temp_upload failed for %s: HTTP %d — %s",
                filename,
                resp.status_code,
                resp.text[:200],
            )
            return False

        temp_id = resp.json().get("result", {}).get("temp_file_id")
        if not temp_id:
            log.warning("temp_upload returned no temp_file_id for %s", filename)
            return False

        resp = requests.post(
            f"{ov_url}/api/v1/resources",
            headers={**headers, "Content-Type": "application/json"},
            json={"temp_file_id": temp_id, "to": target_uri, "wait": True, "timeout": REQUEST_TIMEOUT},
            timeout=REQUEST_TIMEOUT + 10,
        )
        if resp.status_code < 300:
            log.info("Uploaded %s -> %s via temp_upload", filename, target_uri)
            return True
        else:
            log.warning(
                "add resource failed for %s: HTTP %d — %s",
                target_uri,
                resp.status_code,
                resp.text[:200],
            )
            return False

    except Exception as e:
        log.error("Upload failed for %s: %s", filename, e)
        return False


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git_clone(repo_url: str, dest: str) -> bool:
    """Clone a repo. Returns True on success."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, dest],
            check=True,
            capture_output=True,
            timeout=300,
        )
        log.info("Cloned %s -> %s", repo_url, dest)
        return True
    except subprocess.CalledProcessError as e:
        log.error("git clone failed: %s", e.stderr.decode()[:300])
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
                    if "function " in stripped and ("export" in stripped or stripped.startswith("function ")):
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


def _write_code_index_to_filesystem(
    code_index_json: str, safe_name: str, org_repo: str
) -> bool:
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
            "Failed to write code-index to filesystem for %s: %s — "
            "falling back to OpenViking-only upload",
            org_repo,
            e,
        )
        return False


def _code_index_to_markdown(code_index: dict[str, Any]) -> str:
    """Convert code-index JSON to a markdown summary for OpenViking indexing."""
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
            entities.append({
                "entity_id": eid,
                "name": sym.get("name", ""),
                "type": sym.get("type", "symbol"),
                "file": sym.get("file", ""),
                "line": sym.get("line", 0),
                "repo": org_repo,
                "source": "code-index",
            })

        # Import relationships from code-index
        # Create file-level entities so import edges have valid source vertices
        imports_data = code_index.get("imports", {})
        for file, file_imports in imports_data.items():
            file_eid = f"{org_repo}:{file}:__module__"
            if file_eid not in file_entities_created:
                file_entities_created.add(file_eid)
                entity_id_set.add(file_eid)
                entities.append({
                    "entity_id": file_eid,
                    "name": file.rsplit("/", 1)[-1] if "/" in file else file,
                    "type": "module",
                    "file": file,
                    "repo": org_repo,
                    "source": "code-index",
                })
            for imp in file_imports:
                # Create a package entity for the import target
                pkg_eid = f"pkg:{imp}"
                if pkg_eid not in entity_id_set:
                    entity_id_set.add(pkg_eid)
                    entities.append({
                        "entity_id": pkg_eid,
                        "name": imp,
                        "type": "package",
                        "repo": "__external__",
                        "source": "code-index",
                    })
                relationships.append({
                    "from": file_eid,
                    "to": pkg_eid,
                    "type": "imports",
                    "repo": org_repo,
                })

        # Call graph edges — normalize to entity_id format
        call_graph = code_index.get("call_graph", {})
        for caller, callees in call_graph.items():
            if isinstance(callees, list):
                # caller may be "file:func" or just "func" — prefix with org_repo
                caller_eid = f"{org_repo}:{caller}"
                # If caller_eid isn't in entity set, try to find a matching entity
                if caller_eid not in entity_id_set:
                    # Look for any entity whose id ends with the caller
                    matches = [e for e in entity_id_set if e.endswith(f":{caller}") or e.endswith(f":{caller.split(':')[-1]}")]
                    if matches:
                        caller_eid = matches[0]
                for callee in callees:
                    callee_eid = f"{org_repo}:{callee}"
                    if callee_eid not in entity_id_set:
                        matches = [e for e in entity_id_set if e.endswith(f":{callee}") or e.endswith(f":{callee.split(':')[-1]}")]
                        if matches:
                            callee_eid = matches[0]
                    relationships.append({
                        "from": caller_eid,
                        "to": callee_eid,
                        "type": "calls",
                        "repo": org_repo,
                    })

        # External dependency edges — create repo-level and package entities
        repo_eid = f"{org_repo}:__repo__"
        if repo_eid not in entity_id_set:
            entity_id_set.add(repo_eid)
            entities.append({
                "entity_id": repo_eid,
                "name": org_repo.split("/")[-1],
                "type": "repository",
                "repo": org_repo,
                "source": "code-index",
            })
        ext_deps = code_index.get("dependencies", {}).get("external", [])
        for dep in ext_deps:
            pkg_eid = f"pkg:{dep}"
            if pkg_eid not in entity_id_set:
                entity_id_set.add(pkg_eid)
                entities.append({
                    "entity_id": pkg_eid,
                    "name": dep,
                    "type": "package",
                    "repo": "__external__",
                    "source": "code-index",
                })
            relationships.append({
                "from": repo_eid,
                "to": pkg_eid,
                "type": "depends_on",
                "repo": org_repo,
            })

    # 2. Extract higher-level entities from wiki (architecture components, patterns)
    if wiki:
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Content-Type": "application/json"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Extract entities and relationships from this architecture wiki for {org_repo}.\n\n"
                            f"{wiki[:4000]}\n\n"
                            "Return JSON with two arrays:\n"
                            '- "entities": [{{"name": "...", "type": "component|pattern|service|api", "description": "..."}}]\n'
                            '- "relationships": [{{"from": "...", "to": "...", "type": "uses|implements|communicates_with|part_of"}}]\n'
                            "Return ONLY valid JSON."
                        ),
                    }],
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
            region = os.getenv("AWS_REGION", "us-east-1")
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
    query = (
        f"g.V().has('repo', '{org_repo}')"
        f".has('source', 'code-index')"
        f".drop()"
    )
    body = json.dumps({"gremlin": query})
    headers = _get_neptune_signed_headers("POST", neptune_url, body)
    try:
        resp = requests.post(neptune_url, data=body, headers=headers, timeout=60, verify=False)
        if resp.status_code < 400:
            log.info("Cleared structural graph for %s", org_repo)
            return True
        log.warning("Clear structural graph failed for %s: HTTP %d", org_repo, resp.status_code)
        return False
    except Exception as e:
        log.warning("Clear structural graph failed for %s: %s", org_repo, e)
        return False


def _write_to_neptune(
    entities: list[dict], relationships: list[dict], org_repo: str
) -> bool:
    """Write entities and relationships to Neptune via Gremlin HTTP API with IAM auth."""
    neptune_url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"

    try:
        # Batch upsert vertices
        for ent in entities:
            props = {k: v for k, v in ent.items() if v and isinstance(v, (str, int, float))}
            prop_steps = "".join(
                f".property('{k}', '{str(v).replace(chr(39), chr(92)+chr(39))}')"
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
                verify=False,
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
                verify=False,
            )
            if resp.status_code >= 400:
                log.debug(
                    "Neptune edge write failed: HTTP %d for %s->%s",
                    resp.status_code, from_id[:50], to_id[:50],
                )

        log.info(
            "GraphRAG: wrote %d entities + %d relationships for %s",
            len(entities), len(relationships), org_repo,
        )
        return True
    except Exception as e:
        log.warning("Neptune write failed for %s: %s", org_repo, e)
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def ingest_repo(
    org_repo: str,
    ov_url: str,
    ov_key: str,
    skip_ov: bool = False,
    skip_cgc: bool = False,
    skip_deepwiki: bool = False,
) -> dict[str, Any]:
    """Full ingestion pipeline for one repo.

    Returns a result dict with status for each step.
    """
    headers = ov_headers(ov_key)
    result: dict[str, Any] = {
        "repo": org_repo,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "openviking": "skipped",
        "code_index": "skipped",
        "deepwiki": "skipped",
        "graphrag": "skipped",
    }

    # Step 1: Submit to OpenViking (async — it clones from GitHub internally)
    if not skip_ov:
        if openviking_ingest(ov_url, headers, org_repo):
            result["openviking"] = "submitted"
        else:
            result["openviking"] = "failed"

    # Step 2: Clone to persistent storage (S3 Files mount) — shared across enrichment consumers
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
        return result
    result["clone"] = "ok"

    # Step 3: Run cgc → code-index.json → upload to OpenViking
    if not skip_cgc:
        try:
            code_index = cgc_analyze(clone_path, org_repo)
            if code_index:
                code_index_json = json.dumps(code_index, indent=2)
                safe_name = org_repo.replace("/", "-")

                # Write to filesystem (primary — for programmatic access by MCP server)
                fs_written = _write_code_index_to_filesystem(
                    code_index_json, safe_name, org_repo
                )

                # Upload as markdown summary (for semantic search/understand)
                code_index_md = _code_index_to_markdown(code_index)
                upload_to_openviking(
                    ov_url,
                    headers,
                    code_index_md,
                    f"{safe_name}-code-index.md",
                    f"viking://resources/{org_repo}/.code-index.md",
                )

                # Filesystem is the primary storage for structured code-index data;
                # Neptune holds the graph representation; OpenViking gets the markdown summary only.
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
            else:
                result["code_index"] = "analysis_failed"
        except Exception as e:
            log.warning("cgc failed for %s: %s — continuing without code-index", org_repo, e)
            result["code_index"] = f"error: {e}"

    # Step 4: Generate DeepWiki wiki → upload to OpenViking
    if not skip_deepwiki and DEEPWIKI_ENABLED:
        try:
            wiki = deepwiki_generate(org_repo)
            if wiki:
                safe_name = org_repo.replace("/", "-")
                uploaded = upload_to_openviking(
                    ov_url,
                    headers,
                    wiki,
                    f"{safe_name}-wiki.md",
                    f"viking://resources/deepwiki/{safe_name}-wiki.md",
                )
                result["deepwiki"] = "uploaded" if uploaded else "upload_failed"
            else:
                result["deepwiki"] = "generation_failed"
        except Exception as e:
            log.warning("DeepWiki failed for %s: %s — continuing without wiki", org_repo, e)
            result["deepwiki"] = f"error: {e}"

    # Step 5: GraphRAG extraction — entities and relationships into Neptune
    if GRAPHRAG_ENABLED:
        try:
            # Get code_index for GraphRAG if we have it
            ci_data = None
            if result.get("code_index") == "written":
                ci_path = os.path.join(CODE_INDEX_DIR, f"{org_repo.replace('/', '-')}.json")
                if os.path.isfile(ci_path):
                    with open(ci_path) as f:
                        ci_data = json.load(f)

            # Get wiki content if available (re-read from filesystem if DeepWiki succeeded)
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
                result["graphrag"] = f"{graphrag_result['entities']} entities, {graphrag_result['relationships']} relationships"
            else:
                result["graphrag"] = "skipped"
        except Exception as e:
            log.warning("GraphRAG failed for %s: %s — continuing", org_repo, e)
            result["graphrag"] = f"error: {e}"

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
    parser.add_argument("--repo", required=True, help="org/repo to ingest (e.g., aws-samples/bedrock-chat)")
    parser.add_argument("--ov-url", default=os.getenv("OV_URL", "http://openviking.agent-context.svc.cluster.local:1933"))
    parser.add_argument("--ov-key", default=os.getenv("OPENVIKING_ROOT_KEY", os.getenv("ROOT_KEY", "")))
    parser.add_argument("--skip-ov", action="store_true", help="Skip OpenViking ingestion (enrichment only)")
    parser.add_argument("--skip-cgc", action="store_true", help="Skip code-index generation")
    parser.add_argument("--skip-deepwiki", action="store_true", help="Skip DeepWiki generation")
    parser.add_argument("--skip-graphrag", action="store_true", help="Skip GraphRAG extraction")
    parser.add_argument("--tags", default="{}", help="JSON tags object for metadata")
    args = parser.parse_args()

    if not args.ov_key:
        log.error("No OpenViking API key. Set --ov-key or OPENVIKING_ROOT_KEY env var.")
        sys.exit(1)

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError:
        tags = {}

    result = ingest_repo(
        org_repo=args.repo,
        ov_url=args.ov_url,
        ov_key=args.ov_key,
        skip_ov=args.skip_ov,
        skip_cgc=args.skip_cgc,
        skip_deepwiki=args.skip_deepwiki,
    )

    # Update DynamoDB state (replaces repo-state.json)
    update_dynamo_state(args.repo, result, tags=tags)

    print(json.dumps(result, indent=2))

    # Exit non-zero only if OpenViking ingestion failed (enrichment failures are non-blocking)
    if result.get("openviking") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
