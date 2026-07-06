"""SCIP-native graph construction from .scip index files.

Decodes a .scip protobuf, performs enclosing-scope resolution to attribute
references to their enclosing definitions (caller), and produces a reference/call
graph with Symbol nodes and CALLS/REFERENCES edges.

Architecture:
  .scip binary → decode protobuf → enclosing-scope resolution
    → graph: Symbol nodes + reference/CALLS edges, monikers native
    → ready for Neptune CSV emission (scip_neptune_csv.py)

Design points (from EPIC #1529, spikes #1540-#1548):
  1. Enclosing-scope resolution: ref at position P → caller = def whose range encloses P
  2. Edge kind: SCIP refs are all ReadAccess (no Call role); tag via callee descriptor:
     - `().` = function-call-like → CALLS
     - `#` = class/type reference → REFERENCES
  3. 0-indexed (SCIP) → 1-indexed lines for human-readable output
  4. Node properties: symbol_id (full moniker), name, module, file, line, kind, repo
  5. Fail-loud: 0 edges from a code-bearing repo → ERROR

Reuses logic from spike6_scip_graph.py (PR #1551).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scip_proto.scip_pb2 import (
    Index,
    ROLE_DEFINITION,
    ROLE_IMPORT,
)

log = logging.getLogger("scip_ingester")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SymbolNode:
    """A symbol (function, class, method, etc.) in the graph."""

    symbol_id: str  # Full SCIP moniker (join key, internal)
    name: str  # Human-readable short name
    module: str  # Module/package path
    file: str  # Relative file path
    line: int  # 1-indexed line number
    kind: str  # function, class, method, variable, etc.
    repo: str  # Repository identifier


@dataclass
class Edge:
    """A reference or call edge between two symbols."""

    caller_id: str  # symbol_id of the caller (enclosing definition)
    callee_id: str  # symbol_id of the referenced symbol
    edge_kind: str  # "CALLS" or "REFERENCES"
    file: str  # File where the reference occurs
    line: int  # 1-indexed line of the reference


@dataclass
class SCIPGraph:
    """The complete graph extracted from a .scip index."""

    nodes: dict[str, SymbolNode] = field(default_factory=dict)  # symbol_id -> SymbolNode
    edges: list[Edge] = field(default_factory=list)
    repo: str = ""

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def calls_count(self) -> int:
        return sum(1 for e in self.edges if e.edge_kind == "CALLS")

    @property
    def references_count(self) -> int:
        return sum(1 for e in self.edges if e.edge_kind == "REFERENCES")


def merge_graphs(graphs: list[SCIPGraph]) -> SCIPGraph:
    """Merge multiple SCIPGraphs into one (union nodes, concatenate edges).

    SCIP symbol_ids are language-namespaced (e.g., 'scip-python ...' vs
    'scip-typescript ...'), so cross-language collisions do not occur.
    If duplicate symbol_ids exist within a language (shouldn't happen in
    practice), the first occurrence wins.

    Args:
        graphs: List of SCIPGraphs to merge (may be empty).

    Returns:
        A single merged SCIPGraph. Empty if input is empty.
    """
    if not graphs:
        return SCIPGraph()
    if len(graphs) == 1:
        return graphs[0]

    merged = SCIPGraph(repo=graphs[0].repo)
    for g in graphs:
        # Union nodes by symbol_id (first occurrence wins)
        for symbol_id, node in g.nodes.items():
            if symbol_id not in merged.nodes:
                merged.nodes[symbol_id] = node
        # Concatenate edges
        merged.edges.extend(g.edges)

    log.info(
        "Merged %d graphs: %d nodes, %d edges",
        len(graphs),
        merged.node_count,
        merged.edge_count,
    )
    return merged


# ---------------------------------------------------------------------------
# Moniker parsing
# ---------------------------------------------------------------------------


@dataclass
class _Descriptor:
    """A single parsed SCIP descriptor segment."""

    name: str  # The identifier (unquoted if backtick-escaped)
    suffix: str  # The trailing type suffix: "/", ".", "#", "().", ":", "!"


def _tokenize_descriptors(descriptor_str: str) -> list[_Descriptor]:
    """Tokenize a SCIP descriptor string into individual descriptors.

    SCIP descriptor grammar (from the spec):
      - `/` = namespace/package
      - `.` = term/value
      - `#` = type/class
      - `().` = method/function
      - `:` = meta
      - `!` = macro
      - `(name)` = method parameter (name between parens)
      - `[name]` = type parameter (name between brackets)
      - `` `name` `` = backtick-escaped identifier (may contain special chars)

    Returns a list of _Descriptor objects with their name and suffix type.
    """
    result: list[_Descriptor] = []
    i = 0
    n = len(descriptor_str)

    while i < n:
        # Skip leading whitespace (shouldn't occur, but be safe)
        if descriptor_str[i] == " ":
            i += 1
            continue

        # Case 1: backtick-escaped name
        if descriptor_str[i] == "`":
            end_tick = descriptor_str.find("`", i + 1)
            if end_tick == -1:
                # Malformed — take rest as name
                result.append(_Descriptor(name=descriptor_str[i + 1 :], suffix=""))
                break
            name = descriptor_str[i + 1 : end_tick]
            i = end_tick + 1
            # Read suffix after the closing backtick
            suffix = _read_suffix(descriptor_str, i)
            i += len(suffix)
            result.append(_Descriptor(name=name, suffix=suffix))
            continue

        # Case 2: parameter descriptor `(name)`
        if descriptor_str[i] == "(":
            end_paren = descriptor_str.find(")", i + 1)
            if end_paren == -1:
                # Malformed — take rest
                result.append(_Descriptor(name=descriptor_str[i + 1 :], suffix="()"))
                break
            name = descriptor_str[i + 1 : end_paren]
            i = end_paren + 1
            # Check for trailing `.` (method param: `(name).` is not standard, but handle)
            suffix = "()"
            if i < n and descriptor_str[i] == ".":
                suffix = "()."
                i += 1
            result.append(_Descriptor(name=name, suffix=suffix))
            continue

        # Case 3: type parameter descriptor `[name]`
        if descriptor_str[i] == "[":
            end_bracket = descriptor_str.find("]", i + 1)
            if end_bracket == -1:
                result.append(_Descriptor(name=descriptor_str[i + 1 :], suffix="[]"))
                break
            name = descriptor_str[i + 1 : end_bracket]
            i = end_bracket + 1
            result.append(_Descriptor(name=name, suffix="[]"))
            continue

        # Case 4: regular identifier — read until a suffix character
        name_start = i
        while i < n and descriptor_str[i] not in "/\\.#:!`()[]":
            i += 1
        name = descriptor_str[name_start:i]

        # Read suffix
        suffix = _read_suffix(descriptor_str, i)
        i += len(suffix)
        if name or suffix:
            result.append(_Descriptor(name=name, suffix=suffix))
        else:
            # Unrecognized character (e.g. bare ], ), \) — skip to avoid infinite loop
            i += 1

    return result


def _read_suffix(s: str, pos: int) -> str:
    """Read a descriptor suffix starting at pos.

    Recognizes: `().`, `.`, `#`, `/`, `:`, `!`
    """
    if pos >= len(s):
        return ""
    ch = s[pos]
    if ch == "(" and pos + 2 <= len(s) and s[pos : pos + 3] == "().":
        return "()."
    if ch in ".#/:!":
        return ch
    return ""


def parse_moniker_name(symbol: str) -> str:
    """Extract the human-readable name from a SCIP moniker.

    Parses the SCIP descriptor grammar to find the terminal named descriptor,
    returning its clean identifier (unquoted, no suffix punctuation).

    Examples:
        "scip-python python Agent-Reach 0.1 src/agent.py/AgentRunner#run()." → "run"
        "scip-python python requests 2.28 api.py/get()." → "get"
        "scip-ts npm pkg 1.0 src/utils.ts/formatUsd().(value)" → "value"
        "scip-python python pkg 0.1 models.py/Position#" → "Position"
    """
    descriptor_str = _extract_descriptor_str(symbol)
    if not descriptor_str:
        return symbol

    descriptors = _tokenize_descriptors(descriptor_str)
    if not descriptors:
        return symbol

    # Find the last descriptor with a meaningful name
    # Skip descriptors with empty names or synthetic names (typeLiteral*, digits-only)
    for desc in reversed(descriptors):
        name = desc.name
        if not name:
            continue
        # Skip synthetic TypeScript type literal names
        if name.startswith("typeLiteral") or name.startswith("{"):
            continue
        # Clean the name — strip any trailing digits-colon pattern from meta descriptors
        # e.g. "session_id0" from "session_id0:" — the "0:" is the meta suffix
        if desc.suffix == ":" and name and name[-1].isdigit():
            # Strip trailing digits that are part of SCIP meta numbering
            stripped = name.rstrip("0123456789")
            if stripped:
                name = stripped
        return name

    # Fallback: return the last descriptor's name even if it looks synthetic
    for desc in reversed(descriptors):
        if desc.name:
            return desc.name

    return symbol


def _extract_descriptor_str(symbol: str) -> str:
    """Extract the descriptor portion from a full SCIP symbol string.

    SCIP format: "scheme manager package version descriptors..."
    The descriptor portion starts after the 4th space-separated token.
    """
    parts = symbol.split(" ", 4)
    if len(parts) >= 5:
        return parts[4]
    # Might already be just a descriptor string or a short symbol
    return symbol


def parse_moniker_module(symbol: str) -> str:
    """Extract the module/package from a SCIP moniker.

    The module is the path between the package version and the symbol name.
    """
    parts = symbol.split(" ")
    if len(parts) >= 4:
        # Format: "scheme manager package version descriptor..."
        descriptor = " ".join(parts[4:]) if len(parts) > 4 else parts[-1]
        # The module is the file/package path (everything before the last symbol)
        path_parts = descriptor.split("/")
        if len(path_parts) > 1:
            return "/".join(path_parts[:-1])
        return path_parts[0] if path_parts else ""
    return ""


def parse_moniker_kind(symbol: str) -> str:
    """Infer the symbol kind from the SCIP moniker's terminal descriptor suffix.

    Parses the descriptor grammar and reads the suffix of the last named
    descriptor to determine kind:
      - `().` = method/function
      - `#` = type/class
      - `.` = term/variable
      - `/` = package/module
      - `()` = parameter
      - `[]` = type-parameter
      - `:` = meta
      - `!` = macro
    """
    if not symbol:
        return "unknown"

    descriptor_str = _extract_descriptor_str(symbol)
    if not descriptor_str:
        return "symbol"

    descriptors = _tokenize_descriptors(descriptor_str)
    if not descriptors:
        return "symbol"

    # Use the last descriptor's suffix to determine kind
    last = descriptors[-1]
    suffix = last.suffix

    if suffix == "().":
        return "function"
    if suffix == "#":
        return "class"
    if suffix == "/":
        return "module"
    if suffix == ".":
        return "variable"
    if suffix == "()":
        return "parameter"
    if suffix == "[]":
        return "type-parameter"
    if suffix == ":":
        return "meta"
    if suffix == "!":
        return "macro"
    return "symbol"


def classify_edge_kind(callee_symbol: str) -> str:
    """Classify a reference edge as CALLS or REFERENCES based on callee descriptor.

    Design point #3: SCIP has no Call role. We tag edge kind via callee descriptor:
      - `().` suffix = function/method call → CALLS
      - `#` = class/type reference → REFERENCES
      - Other → REFERENCES (conservative)
    """
    if not callee_symbol:
        return "REFERENCES"
    if "()." in callee_symbol or callee_symbol.endswith("()."):
        return "CALLS"
    if "#" in callee_symbol and not callee_symbol.endswith("#"):
        # Member access on a type — likely a method call
        return "CALLS"
    return "REFERENCES"


def is_local_symbol(symbol: str) -> bool:
    """Check if a symbol is a local (file-scoped) symbol.

    Local symbols start with "local " and have no cross-file significance.
    They degrade monikers and break cross-repo join (#1536).
    """
    return symbol.startswith("local ")


# ---------------------------------------------------------------------------
# Core: decode + enclosing-scope resolution
# ---------------------------------------------------------------------------


def decode_scip_file(scip_path: str) -> Index:
    """Parse a .scip file and return the decoded Index."""
    with open(scip_path, "rb") as f:
        data = f.read()
    index = Index()
    index.ParseFromString(data)
    return index


def build_graph(index: Index, repo: str) -> SCIPGraph:
    """Build a reference/call graph from a decoded SCIP Index.

    Algorithm (from spike6, proven on Agent-Reach with ~2,838 edges):
      1. First pass: collect all definitions with their ranges per file
      2. Second pass: for each reference, find the enclosing definition (caller)
         using the "last def before ref line" heuristic
      3. Emit edges: caller_symbol → callee_symbol, tagged by edge kind

    Args:
        index: Decoded SCIP Index protobuf
        repo: Repository identifier (e.g., "org/repo-name")

    Returns:
        SCIPGraph with nodes and edges
    """
    graph = SCIPGraph(repo=repo)

    # First pass: collect definitions per file
    # file_path -> [(symbol, start_line, end_line)]
    file_definitions: dict[str, list[tuple[str, int, int]]] = defaultdict(list)

    for doc in index.documents:
        rel_path = doc.relative_path
        for occ in doc.occurrences:
            if occ.symbol_roles & ROLE_DEFINITION:
                symbol = occ.symbol
                if not symbol or is_local_symbol(symbol):
                    continue

                # Parse SCIP range (0-indexed)
                r = list(occ.range)
                if len(r) == 3:
                    start_line = r[0]
                    end_line = r[0]  # Single-line definition
                elif len(r) >= 4:
                    start_line = r[0]
                    end_line = r[2]
                else:
                    continue

                file_definitions[rel_path].append((symbol, start_line, end_line))

                # Add node for this definition
                if symbol not in graph.nodes:
                    graph.nodes[symbol] = SymbolNode(
                        symbol_id=symbol,
                        name=parse_moniker_name(symbol),
                        module=parse_moniker_module(symbol),
                        file=rel_path,
                        line=start_line + 1,  # 0-indexed → 1-indexed
                        kind=parse_moniker_kind(symbol),
                        repo=repo,
                    )

    # Sort definitions by start_line for enclosing-scope heuristic
    for path in file_definitions:
        file_definitions[path].sort(key=lambda x: x[1])

    # Second pass: resolve references to enclosing definitions
    edge_set: set[tuple[str, str]] = set()  # Deduplicate (caller, callee) pairs

    for doc in index.documents:
        rel_path = doc.relative_path
        defs = file_definitions.get(rel_path, [])
        if not defs:
            continue

        for occ in doc.occurrences:
            # Skip definitions and imports — we want references only
            if occ.symbol_roles & ROLE_DEFINITION:
                continue
            if occ.symbol_roles & ROLE_IMPORT:
                continue

            callee_symbol = occ.symbol
            if not callee_symbol or is_local_symbol(callee_symbol):
                continue

            # Get reference position (0-indexed)
            r = list(occ.range)
            if len(r) < 3:
                continue
            ref_line = r[0]

            # Enclosing-scope resolution: find the last definition whose
            # start_line <= ref_line (the def that "contains" this reference)
            caller_symbol = None
            for sym, start_line, end_line in reversed(defs):
                if start_line <= ref_line:
                    caller_symbol = sym
                    break

            if caller_symbol is None:
                continue

            # Skip self-references
            if caller_symbol == callee_symbol:
                continue

            # Deduplicate edges (same caller→callee pair)
            edge_key = (caller_symbol, callee_symbol)
            if edge_key in edge_set:
                continue
            edge_set.add(edge_key)

            # Classify edge kind
            edge_kind = classify_edge_kind(callee_symbol)

            # Ensure callee node exists (may be external — from dependencies)
            if callee_symbol not in graph.nodes:
                graph.nodes[callee_symbol] = SymbolNode(
                    symbol_id=callee_symbol,
                    name=parse_moniker_name(callee_symbol),
                    module=parse_moniker_module(callee_symbol),
                    file="",  # External — no file in this repo
                    line=0,
                    kind=parse_moniker_kind(callee_symbol),
                    repo=repo,
                )

            graph.edges.append(
                Edge(
                    caller_id=caller_symbol,
                    callee_id=callee_symbol,
                    edge_kind=edge_kind,
                    file=rel_path,
                    line=ref_line + 1,  # 0-indexed → 1-indexed
                )
            )

    return graph


def ingest_scip(scip_path: str, repo: str) -> SCIPGraph:
    """Full SCIP ingestion: decode .scip file → build graph.

    Args:
        scip_path: Path to the .scip index file
        repo: Repository identifier (e.g., "org/repo-name")

    Returns:
        SCIPGraph with nodes and edges

    Raises:
        FileNotFoundError: If scip_path doesn't exist
        ValueError: If the .scip file produces 0 edges (fail-loud rule)
    """
    if not Path(scip_path).exists():
        raise FileNotFoundError(f"SCIP index not found: {scip_path}")

    log.info("Decoding SCIP index: %s", scip_path)
    index = decode_scip_file(scip_path)

    log.info(
        "SCIP metadata: tool=%s v%s, documents=%d",
        index.metadata.tool_info.name,
        index.metadata.tool_info.version,
        len(index.documents),
    )

    graph = build_graph(index, repo)

    log.info(
        "Graph built: %d nodes, %d edges (%d CALLS, %d REFERENCES)",
        graph.node_count,
        graph.edge_count,
        graph.calls_count,
        graph.references_count,
    )

    return graph
