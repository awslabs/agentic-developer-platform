"""Zoekt search backend for the Door (MCP query layer).

Queries the zoekt-webserver /api/search JSON API and returns results in the
MCP search contract shape (repo_id, file, line, content). The Door's ACL
filter (acl.py) runs post-query on the returned SearchHits.

See: docs/design-notes/1346-zoekt-direct-replacement.md for full design.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

import httpx

from .acl import SearchHit

log = logging.getLogger(__name__)

# Default timeout for Zoekt API requests (seconds)
DEFAULT_TIMEOUT = 10.0
DEFAULT_LIMIT = 50


class SearchBackend(Protocol):
    """Protocol for code-search backends.

    The Door dispatches to any backend satisfying this interface.
    Implementations: ZoektSearchBackend (production), FakeZoektIndex (tests).
    """

    async def search(
        self,
        query: str,
        *,
        repo_ids: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[SearchHit]:
        """Search for code matching the query.

        Parameters
        ----------
        query:
            Literal text or regex pattern to search for.
        repo_ids:
            Optional list of repo names (org/repo) to restrict search scope.
            If None, searches all indexed repos.
        limit:
            Maximum number of results to return.

        Returns
        -------
        List of SearchHit objects (with repo_name and data dict).
        Empty list on error (fail-safe for search availability).
        """
        ...


class ZoektSearchBackend:
    """Production Zoekt search backend querying zoekt-webserver's /api/search.

    The webserver is expected to be running at the configured URL (default:
    http://zoekt.agent-context.svc.cluster.local:6070).
    """

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize with the zoekt-webserver base URL.

        Parameters
        ----------
        base_url:
            Full URL to the zoekt-webserver (e.g., "http://zoekt:6070").
        timeout:
            HTTP request timeout in seconds.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def search(
        self,
        query: str,
        *,
        repo_ids: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[SearchHit]:
        """Query Zoekt and return results as SearchHit objects.

        Returns empty list on timeout/error (fail-safe: search unavailability
        should not crash the Door — it degrades to no exact-search results).
        """
        params: dict[str, Any] = {"q": query, "num": str(limit)}

        # Scope to specific repos using Zoekt's repo filter syntax
        if repo_ids:
            repo_filter = _build_repo_filter(repo_ids)
            params["repos"] = repo_filter

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._base_url}/api/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            log.warning("zoekt search timed out after %.1fs for query: %s", self._timeout, query)
            return []
        except httpx.HTTPStatusError as e:
            log.warning("zoekt returned HTTP %d for query: %s", e.response.status_code, query)
            return []
        except Exception:
            log.warning("zoekt search failed for query: %s", query, exc_info=True)
            return []

        return _parse_zoekt_response(data)

    async def health_check(self) -> bool:
        """Check if zoekt-webserver is reachable and responding."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/")
                return resp.status_code == 200
        except Exception:
            return False


def _build_repo_filter(repo_ids: list[str]) -> str:
    """Convert a list of repo names to a Zoekt repos regex filter.

    Zoekt's `repos` parameter accepts a regex matched against repository names.
    We anchor each name and join with `|` for an exact-match-any filter.

    Example:
        ["org/repo-a", "org/repo-b"] -> "^(org/repo\\-a|org/repo\\-b)$"
    """
    escaped = [re.escape(r) for r in repo_ids]
    return f"^({'|'.join(escaped)})$"


def _parse_zoekt_response(data: dict[str, Any]) -> list[SearchHit]:
    """Parse Zoekt's /api/search JSON response into SearchHit objects.

    Zoekt response structure:
    {
      "Result": {
        "FileMatches": [
          {
            "FileName": "src/handler.py",
            "Repository": "org/my-repo",
            "LineMatches": [
              {"LineNumber": 42, "Line": "base64-encoded-line", ...}
            ],
            "ChunkMatches": [
              {"Content": "base64-encoded", "ContentStart": {"LineNumber": 42}, ...}
            ]
          }
        ]
      }
    }

    Note: Zoekt may use either LineMatches (legacy) or ChunkMatches (newer).
    We support both for forward compatibility.
    """
    results: list[SearchHit] = []

    result_obj = data.get("Result") or data.get("result", {})
    file_matches = result_obj.get("FileMatches") or result_obj.get("Files") or []

    for file_match in file_matches:
        repo = file_match.get("Repository", "")
        filename = file_match.get("FileName", "")

        # Try LineMatches first (legacy format)
        line_matches = file_match.get("LineMatches") or []
        for lm in line_matches:
            line_num = lm.get("LineNumber", 0)
            line_text = _decode_line(lm.get("Line", ""))

            results.append(
                SearchHit(
                    repo_name=repo,
                    data={
                        "repo_id": repo,
                        "file": filename,
                        "line": line_num,
                        "content": line_text,
                        "match_type": "exact",
                    },
                )
            )

        # Try ChunkMatches (newer format)
        chunk_matches = file_match.get("ChunkMatches") or []
        for cm in chunk_matches:
            content_start = cm.get("ContentStart", {})
            line_num = content_start.get("LineNumber", 0)
            content_raw = _decode_line(cm.get("Content", ""))

            # ChunkMatches may contain multiple lines; emit one hit per chunk
            first_line = content_raw.split("\n")[0] if content_raw else ""
            results.append(
                SearchHit(
                    repo_name=repo,
                    data={
                        "repo_id": repo,
                        "file": filename,
                        "line": line_num,
                        "content": first_line,
                        "match_type": "exact",
                    },
                )
            )

        # If neither LineMatches nor ChunkMatches exist, emit a file-level hit
        if not line_matches and not chunk_matches:
            results.append(
                SearchHit(
                    repo_name=repo,
                    data={
                        "repo_id": repo,
                        "file": filename,
                        "line": 0,
                        "content": "",
                        "match_type": "exact",
                    },
                )
            )

    return results


def _decode_line(value: str | bytes) -> str:
    """Decode a line from Zoekt response.

    Zoekt may return line content as:
    - A plain string (when JSON-encoded directly)
    - A base64-encoded bytes field (in some protobuf-to-JSON serializations)

    We handle both gracefully.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        # Check if it looks like base64 (Zoekt's Go JSON encoder uses base64 for []byte)
        # Heuristic: if it contains only base64 chars and length is a multiple of 4
        import base64

        if value and len(value) % 4 == 0 and re.match(r"^[A-Za-z0-9+/=]+$", value):
            try:
                decoded = base64.b64decode(value).decode("utf-8", errors="replace")
                # Only accept if decoding produces printable text
                if decoded.isprintable() or "\n" in decoded or "\t" in decoded:
                    return decoded.rstrip("\n")
            except Exception:
                pass
        return value
    return str(value)
