"""Go language helpers for the basic code-index builder.

Extracted into a standalone importable module (Issue #3300) so the detection
logic can be unit-tested without importing the full ingest-repo.py with its
heavy dependency tree (boto3, requests, etc.).
"""

from __future__ import annotations


def extract_go_func_name(line: str) -> str:
    """Extract function/method name from a Go func declaration.

    Handles both plain functions and methods with receivers:
      - "func main() {"           -> "main"
      - "func (s *Server) Run()" -> "Run"
      - "func (q Quote) String()" -> "String"

    Args:
        line: A stripped source line starting with "func ".

    Returns:
        The function/method name, or "" if it can't be parsed.
    """
    rest = line[5:]  # strip "func "
    # If starts with '(' it's a method with receiver - skip to after the receiver
    if rest.startswith("("):
        # Find the closing ')' of the receiver
        close_paren = rest.find(")", 1)
        if close_paren == -1:
            return ""
        rest = rest[close_paren + 1 :].strip()
    # Now `rest` starts with the function name, followed by '(' for params
    name = rest.split("(")[0].strip()
    # Strip any type parameters (Go 1.18+ generics): "Foo[T any]" -> "Foo"
    if "[" in name:
        name = name.split("[")[0].strip()
    return name


def extract_go_type(line: str) -> tuple[str, str]:
    """Extract type name and kind from a Go type declaration.

    Returns (name, kind) where kind is "struct", "interface", or "class" (fallback
    for type aliases and named types).
    Returns ("", "") if the line doesn't contain a valid type declaration.

    Args:
        line: A stripped source line starting with "type ".

    Examples:
      - "type frontendServer struct {"  -> ("frontendServer", "struct")
      - "type Quote struct {"           -> ("Quote", "struct")
      - "type Handler interface {"      -> ("Handler", "interface")
      - "type Duration int64"           -> ("Duration", "class")
      - "type ("                        -> ("", "")  (type block opener)
    """
    rest = line[5:].strip()  # strip "type "
    # Skip type-block openers: "type ("
    if not rest or rest.startswith("("):
        return ("", "")
    # The type name is the first word (may include type params like "Foo[T any]")
    parts = rest.split(None, 1)
    name = parts[0]
    # Handle type parameters: "Foo[T comparable]" - name has "[" but rest is in parts[1]
    # We need to find the full remainder after the type params are closed
    remainder = parts[1].strip() if len(parts) > 1 else ""
    if "[" in name:
        name = name.split("[")[0]
        # The remainder might start with "T comparable] struct {" — skip past "]"
        bracket_close = remainder.find("]")
        if bracket_close != -1:
            remainder = remainder[bracket_close + 1 :].strip()
    if not name or (not name[0].isalpha() and name[0] != "_"):
        return ("", "")
    # Determine kind from the remainder
    if remainder:
        if remainder.startswith("struct"):
            return (name, "struct")
        elif remainder.startswith("interface"):
            return (name, "interface")
        else:
            # Type alias or named type (e.g., "type Duration int64")
            return (name, "class")
    # Just "type Foo" with nothing else (incomplete line) - still capture it
    return (name, "class")
