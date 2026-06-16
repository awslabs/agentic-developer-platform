#!/usr/bin/env python3
"""
SPIKE-6: SCIP-only graph construction analysis.

Tests whether scip-python's .scip index contains reference occurrences with positions,
and whether we can build a usable call/reference graph via enclosing-scope resolution.

Compares against cgc's 611 CALLS edges from SPIKE-3 (Agent-Reach).
"""

import sys

sys.path.insert(0, "/workspace/proto")

import scip_pb2
from collections import Counter, defaultdict

# SCIP SymbolRole enum values (from scip.proto)
# These are bit flags
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE_ACCESS = 0x4
ROLE_READ_ACCESS = 0x8
ROLE_GENERATED = 0x10
ROLE_TEST = 0x20

ROLE_NAMES = {
    0x1: "Definition",
    0x2: "Import",
    0x4: "WriteAccess",
    0x8: "ReadAccess",
    0x10: "Generated",
    0x20: "Test",
}

CGC_BASELINE_EDGES = 611


def decode_roles(role_int):
    """Decode the bitmask symbol_roles into a list of role names."""
    roles = []
    for bit, name in ROLE_NAMES.items():
        if role_int & bit:
            roles.append(name)
    if not roles:
        roles.append("UnqualifiedReference")  # role=0 means just a reference
    return roles


def parse_scip(path):
    """Parse a .scip file and return the Index protobuf."""
    with open(path, "rb") as f:
        data = f.read()
    index = scip_pb2.Index()
    index.ParseFromString(data)
    return index


def analyze_occurrences(index):
    """
    Step 1: Report occurrence composition.
    Count Definition vs Reference occurrences and all symbol_roles values present.
    """
    print("=" * 70)
    print("STEP 1: OCCURRENCE COMPOSITION")
    print("=" * 70)

    total_occurrences = 0
    role_counter = Counter()
    role_combo_counter = Counter()
    definitions = 0
    references = 0
    references_with_position = 0

    for doc in index.documents:
        for occ in doc.occurrences:
            total_occurrences += 1
            roles = occ.symbol_roles
            role_list = decode_roles(roles)
            role_combo_counter[tuple(sorted(role_list))] += 1
            for r in role_list:
                role_counter[r] += 1

            if roles & ROLE_DEFINITION:
                definitions += 1
            else:
                references += 1
                # Check if it has a position (range field)
                if len(occ.range) >= 3:
                    references_with_position += 1

    print("")
    print("Total documents (files): {}".format(len(index.documents)))
    print("Total occurrences: {}".format(total_occurrences))
    print("  Definitions: {}".format(definitions))
    print("  References (non-definition): {}".format(references))
    print("  References WITH positions (range >= 3 ints): {}".format(references_with_position))
    print("")
    print("--- Role counts (individual flags) ---")
    for role, count in role_counter.most_common():
        print("  {}: {}".format(role, count))
    print("")
    print("--- Role combinations ---")
    for combo, count in role_combo_counter.most_common(20):
        print("  {}: {}".format(", ".join(combo), count))

    if references_with_position > 0:
        print("")
        print(
            "*** MAKE-OR-BREAK: References with positions exist? YES ({})".format(
                references_with_position
            )
        )
    else:
        print("")
        print("*** MAKE-OR-BREAK: References with positions exist? NO — SCIP-only is IMPOSSIBLE")

    return references_with_position > 0


def build_reference_graph(index):
    """
    Step 2: Build reference graph via enclosing-scope resolution.

    For each reference at position P in a file:
      - Find the definition whose range ENCLOSES P (the caller)
      - Emit edge: caller_moniker -> callee_moniker (the referenced symbol)
    """
    print("")
    print("=" * 70)
    print("STEP 2: BUILD REFERENCE GRAPH (enclosing-scope resolution)")
    print("=" * 70)

    # First pass: collect all definitions with their ranges per file
    file_definitions = defaultdict(list)  # file_path -> [(symbol, start_line, end_line)]
    all_symbols = set()

    for doc in index.documents:
        rel_path = doc.relative_path
        for occ in doc.occurrences:
            if occ.symbol_roles & ROLE_DEFINITION:
                symbol = occ.symbol
                if not symbol or symbol.startswith("local "):
                    continue  # skip local symbols

                # Parse range
                r = list(occ.range)
                if len(r) == 3:
                    start_line = r[0]
                    end_line = r[0]  # single line
                elif len(r) >= 4:
                    start_line = r[0]
                    end_line = r[2]
                else:
                    continue

                file_definitions[rel_path].append((symbol, start_line, end_line))
                all_symbols.add(symbol)

    # Sort definitions by start_line for each file (for enclosing-scope heuristic)
    for path in file_definitions:
        file_definitions[path].sort(key=lambda x: x[1])

    # Second pass: for each reference, find enclosing definition
    # Strategy: for a reference at line L in file F, the enclosing definition is
    # the LAST definition in F whose start_line <= L
    # (This is a heuristic — works for Python where defs are declared before body)

    edges = []  # (caller_moniker, callee_moniker, file, line)
    edge_set = set()  # deduplicate
    references_resolved = 0
    references_unresolved = 0

    for doc in index.documents:
        rel_path = doc.relative_path
        defs = file_definitions.get(rel_path, [])
        if not defs:
            continue

        for occ in doc.occurrences:
            # Skip definitions and imports — we want references
            if occ.symbol_roles & ROLE_DEFINITION:
                continue
            if occ.symbol_roles & ROLE_IMPORT:
                continue

            callee_symbol = occ.symbol
            if not callee_symbol or callee_symbol.startswith("local "):
                continue

            # Get reference position
            r = list(occ.range)
            if len(r) >= 3:
                ref_line = r[0]
            else:
                continue

            # Find enclosing definition (last def whose start_line <= ref_line)
            caller_symbol = None
            for sym, start_line, end_line in reversed(defs):
                if start_line <= ref_line:
                    caller_symbol = sym
                    break

            if caller_symbol is None:
                references_unresolved += 1
                continue

            # Skip self-references
            if caller_symbol == callee_symbol:
                continue

            # Create edge (deduplicated)
            edge_key = (caller_symbol, callee_symbol)
            if edge_key not in edge_set:
                edge_set.add(edge_key)
                edges.append(
                    {
                        "caller": caller_symbol,
                        "callee": callee_symbol,
                        "file": rel_path,
                        "ref_line": ref_line,
                    }
                )
            references_resolved += 1

    # Collect unique nodes (symbols that appear as caller or callee)
    nodes = set()
    for e in edges:
        nodes.add(e["caller"])
        nodes.add(e["callee"])

    print("")
    print("Graph construction results:")
    print(
        "  Definitions found (potential callers): {}".format(
            sum(len(v) for v in file_definitions.values())
        )
    )
    print("  References resolved to enclosing scope: {}".format(references_resolved))
    print("  References with no enclosing definition: {}".format(references_unresolved))
    print("  Unique edges (caller -> callee): {}".format(len(edges)))
    print("  Unique nodes (symbols): {}".format(len(nodes)))
    print("")
    comparison = (
        "MORE"
        if len(edges) > CGC_BASELINE_EDGES
        else ("FEWER" if len(edges) < CGC_BASELINE_EDGES else "SAME")
    )
    print(
        "  *** vs cgc SPIKE-3 baseline (611 CALLS edges): {} edges ({})".format(
            len(edges), comparison
        )
    )

    return edges, nodes, file_definitions


def show_sample_edges(edges):
    """
    Step 3: Show 5 sample edges with monikers and source location.
    Prioritize cross-file edges.
    """
    print("")
    print("=" * 70)
    print("STEP 3: SAMPLE EDGES (5 edges, cross-file prioritized)")
    print("=" * 70)

    # Separate cross-file and same-file edges by comparing module paths in monikers
    cross_file = []
    same_file = []

    for e in edges:
        caller_parts = e["caller"].split("/")
        callee_parts = e["callee"].split("/")
        # If the module path differs, it's likely cross-file
        if len(caller_parts) > 1 and len(callee_parts) > 1:
            caller_module = "/".join(caller_parts[:-1])
            callee_module = "/".join(callee_parts[:-1])
            if caller_module != callee_module:
                cross_file.append(e)
            else:
                same_file.append(e)
        else:
            same_file.append(e)

    print("")
    print("  Cross-file edges (different modules): {}".format(len(cross_file)))
    print("  Same-module edges: {}".format(len(same_file)))

    # Show 3 cross-file + 2 same-file (or best available)
    samples = cross_file[:3] + same_file[:2]
    if len(cross_file) < 3:
        samples = cross_file + same_file[: 5 - len(cross_file)]

    print("")
    print("  --- Sample edges ---")
    for i, e in enumerate(samples[:5], 1):
        print("")
        print("  Edge {}:".format(i))
        print("    Caller moniker: {}".format(e["caller"]))
        print("    Callee moniker: {}".format(e["callee"]))
        print("    Source file:    {}".format(e["file"]))
        print("    Reference line: {}".format(e["ref_line"]))


def characterize_edge_quality(edges):
    """
    Step 4: Characterize edge quality.
    Since SCIP has no "Call" role, analyze what kinds of references form our edges.
    """
    print("")
    print("=" * 70)
    print("STEP 4: EDGE QUALITY CHARACTERIZATION")
    print("=" * 70)

    # Categorize edges by callee moniker patterns
    likely_calls = 0
    type_refs = 0
    import_refs = 0
    other_refs = 0

    for e in edges:
        callee = e["callee"]
        # Heuristics for categorization:
        # - Methods/functions: monikers with () or ending in ().
        # - Module refs: ending with /
        # - Type refs: contain Type/Class pattern

        if "()." in callee or callee.endswith("()."):
            likely_calls += 1
        elif "#" in callee:
            likely_calls += 1  # member access = likely method call
        elif callee.endswith("/"):
            import_refs += 1  # module reference
        else:
            other_refs += 1

    total = len(edges)

    def pct(x):
        return 100 * x // max(total, 1)

    print("")
    print("  Edge categories (heuristic):")
    print(
        "    Likely true calls (function/method refs): {} ({}%)".format(
            likely_calls, pct(likely_calls)
        )
    )
    print("    Module/import references: {} ({}%)".format(import_refs, pct(import_refs)))
    print("    Type/class annotations: {} ({}%)".format(type_refs, pct(type_refs)))
    print("    Other/unclassified: {} ({}%)".format(other_refs, pct(other_refs)))

    print("")
    print("  Filtering feasibility:")
    print('    If we only keep "likely calls": {} edges'.format(likely_calls))
    if abs(likely_calls - CGC_BASELINE_EDGES) < 200:
        print("    vs cgc 611: COMPARABLE (within 200)")
    elif likely_calls > CGC_BASELINE_EDGES:
        print("    vs cgc 611: RICHER (more edges, broader coverage)")
    else:
        print("    vs cgc 611: SPARSER (fewer edges)")

    return likely_calls


def main():
    print("SPIKE-6: SCIP-only graph construction analysis")
    print("Target: Panniantong/Agent-Reach")
    print("Baseline: cgc SPIKE-3 = 611 CALLS edges")
    print("=" * 70)

    # Parse SCIP index
    print("")
    print("Parsing /workspace/Agent-Reach/index.scip...")
    index = parse_scip("/workspace/Agent-Reach/index.scip")
    print(
        "  Metadata: tool={} v{}".format(
            index.metadata.tool_info.name, index.metadata.tool_info.version
        )
    )
    print("  Documents: {}".format(len(index.documents)))

    # Step 1: Occurrence composition
    refs_exist = analyze_occurrences(index)

    if not refs_exist:
        print("")
        print("*** FAIL: No reference occurrences found. SCIP-only graph NOT viable.")
        return

    # Step 2: Build reference graph
    edges, nodes, file_defs = build_reference_graph(index)

    # Step 3: Sample edges
    show_sample_edges(edges)

    # Step 4: Edge quality
    characterize_edge_quality(edges)

    # Final verdict
    print("")
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)

    edge_count = len(edges)
    if edge_count >= 400 and refs_exist:
        verdict = "PASS"
        reasoning = (
            "References present ({} unique edges), "
            "comparable to or exceeding cgc 611. "
            "Monikers native on every node/edge."
        ).format(edge_count)
    elif edge_count >= 100 and refs_exist:
        verdict = "PARTIAL"
        reasoning = (
            "References present but graph is different from cgc "
            "({} edges vs 611). Reference graph may still be "
            "acceptable for impact analysis."
        ).format(edge_count)
    else:
        verdict = "FAIL"
        reasoning = "Graph too sparse ({} edges) or no references.".format(edge_count)

    print("")
    print("  Verdict: {}".format(verdict))
    print("  Reasoning: {}".format(reasoning))
    print("")
    print("  Architecture recommendation:")
    if verdict == "PASS":
        print("    RECOMMEND SCIP-ONLY - drop cgc + FalkorDB + reconciliation.")
        print(
            "    Pipeline: scip-python -> decode .scip -> enclosing-scope graph -> CSV -> Neptune"
        )
        print("    Supersedes SPIKE-5.")
    elif verdict == "PARTIAL":
        print("    SCIP-only produces a reference graph (broader than call-graph).")
        print("    Decision needed: is reference graph acceptable for impact analysis?")
        print('    (Arguably MORE useful - "what references X" = real blast radius)')
    else:
        print("    SCIP-only NOT viable. Stick with cgc+SCIP+reconciliation (SPIKE-5 path).")


if __name__ == "__main__":
    main()
