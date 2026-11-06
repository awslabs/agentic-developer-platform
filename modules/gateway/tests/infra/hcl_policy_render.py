"""Render IAM policy documents straight from Terraform source, without Terraform.

The infra tests in this directory pin facts about ``.tf`` files with text and
structure assertions (no AWS, no terraform binary). Policy *size* is one fact
that text matching cannot establish: IAM measures the rendered JSON, after every
``${...}`` interpolation, ``concat`` and conditional has been resolved. This
module evaluates exactly the HCL subset the role-policy files use -- object and
tuple literals, string templates, traversals, ``concat``/``sort``/``tolist``/
``length``/``jsonencode``/``jsondecode``/``contains``/``keys``/``values``/
``distinct``/``try``/``coalesce``, ``? :``, ``==``/``!=``/``&&``/``||``/``!``,
``>``/``<`` and list ``for`` expressions -- against a fixture scope of realistic
values, and refuses anything else loudly so a new construct is a visible test
failure rather than a silently wrong byte count.

``local.*`` references not supplied by the fixture are resolved lazily from the
``locals`` blocks of the same directory, so a statement list assembled across
several files renders the way Terraform would assemble it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Character-level scanner: comments, strings, heredocs and nesting depth
# ---------------------------------------------------------------------------


def _skip_string(text: str, i: int) -> int:
    """``text[i]`` is the opening quote; return the index just past the closing quote."""
    assert text[i] == '"'
    i += 1
    depth = 0  # ${ ... } nesting inside the template
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if depth == 0 and ch == '"':
            return i + 1
        if ch == "$" and text.startswith("${", i) and not text.startswith("$${", i - 1 if i else 0):
            depth += 1
            i += 2
            continue
        if depth and ch == '"':
            i = _skip_string(text, i)
            continue
        if depth and ch == "{":
            depth += 1
        elif depth and ch == "}":
            depth -= 1
        i += 1
    raise ValueError("unterminated string")


def _skip_comment(text: str, i: int) -> int | None:
    if text.startswith("#", i) or text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        if end < 0:
            raise ValueError("unterminated block comment")
        return end + 2
    return None


def _skip_heredoc(text: str, i: int) -> int | None:
    match = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\r?\n").match(text, i)
    if not match:
        return None
    marker = match.group(1)
    pattern = re.compile(rf"^\s*{re.escape(marker)}\s*$", re.MULTILINE)
    end = pattern.search(text, match.end())
    if not end:
        raise ValueError(f"unterminated heredoc {marker}")
    return end.end()


def _matching_close(text: str, open_index: int) -> int:
    """Index of the ``}``/``]``/``)`` that closes the bracket at ``open_index``."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = [pairs[text[open_index]]]
    i = open_index + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        skipped = _skip_comment(text, i)
        if skipped is not None:
            i = skipped
            continue
        skipped = _skip_heredoc(text, i)
        if skipped is not None:
            i = skipped
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in "}])":
            if ch != stack[-1]:
                raise ValueError(f"mismatched bracket {ch!r} at {i}")
            stack.pop()
            if not stack:
                return i
        i += 1
    raise ValueError("unterminated bracket")


def _strip_comments(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
            continue
        skipped = _skip_comment(text, i)
        if skipped is not None:
            i = skipped
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Block extraction
# ---------------------------------------------------------------------------

_BLOCK_HEADER = re.compile(
    r'^(?P<kind>resource|data|locals|variable|module|moved|output)(?:\s+"(?P<a>[^"]+)")?(?:\s+"(?P<b>[^"]+)")?\s*\{',
    re.MULTILINE,
)


@dataclass
class Block:
    kind: str
    labels: tuple[str, ...]
    body: str
    path: Path
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return ".".join(self.labels)


def _split_attributes(body: str) -> dict[str, str]:
    """Top-level ``name = expression`` pairs of a block body (nested blocks are skipped)."""
    attributes: dict[str, str] = {}
    i = 0
    length = len(body)
    attr = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_-]*)\s*(=|\{)")
    while i < length:
        skipped = _skip_comment(body, i)
        if skipped is not None:
            i = skipped
            continue
        if body[i].isspace():
            i += 1
            continue
        match = attr.match(body, i)
        if not match:
            raise ValueError(f"cannot parse block body near: {body[i : i + 60]!r}")
        name, kind = match.group(1), match.group(2)
        if kind == "{":  # nested block such as lifecycle / validation
            i = _matching_close(body, match.end() - 1) + 1
            continue
        # Nested block with a label: `provisioner "local-exec" {`
        labelled = re.compile(r'\s*[A-Za-z_][A-Za-z0-9_-]*\s+"[^"]*"\s*\{').match(body, i)
        if labelled:
            i = _matching_close(body, labelled.end() - 1) + 1
            continue
        start = match.end()
        end = _expression_end(body, start)
        attributes[name] = body[start:end].strip()
        i = end
    return attributes


def _expression_end(text: str, start: int) -> int:
    """End of the expression starting at ``start``: the first newline at depth 0 that is
    not inside brackets, strings or comments -- unless the line continues with an
    operator, ``?``, ``:`` or ``.``."""
    i = start
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        skipped = _skip_comment(text, i)
        if skipped is not None:
            i = skipped
            continue
        skipped = _skip_heredoc(text, i)
        if skipped is not None:
            i = skipped
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "\n" and depth == 0:
            rest = text[i + 1 :].lstrip()
            if rest[:1] in ("?", ":", ".") or rest[:2] in ("&&", "||", "==", "!="):
                i += 1
                continue
            return i
        i += 1
    return len(text)


def blocks(path: Path) -> list[Block]:
    text = path.read_text()
    found: list[Block] = []
    pos = 0
    while True:
        match = _BLOCK_HEADER.search(text, pos)
        if not match:
            break
        open_index = match.end() - 1
        close = _matching_close(text, open_index)
        labels = tuple(label for label in (match.group("a"), match.group("b")) if label)
        body = text[open_index + 1 : close]
        found.append(Block(match.group("kind"), labels, body, path))
        pos = close + 1
    for block in found:
        try:
            block.attributes = _split_attributes(block.body)
        except ValueError:
            block.attributes = {}  # only needed for the blocks we evaluate; see Scope
    return found


# ---------------------------------------------------------------------------
# Expression parser
# ---------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*)
  | (?P<op>=>|\.\.\.|==|!=|<=|>=|&&|\|\||[{}\[\]()\,=:?.!<>+\-*/%])
    """,
    re.VERBOSE,
)


class _Tokens:
    def __init__(self, source: str):
        self.tokens: list[tuple[str, str]] = []
        i = 0
        source = _strip_comments(source)
        while i < len(source):
            if source[i] == '"':
                end = _skip_string(source, i)
                self.tokens.append(("str", source[i + 1 : end - 1]))
                i = end
                continue
            match = _TOKEN.match(source, i)
            if not match:
                raise ValueError(f"unexpected character {source[i]!r} in {source!r}")
            kind = match.lastgroup
            if kind != "ws":
                self.tokens.append((kind, match.group(kind)))
            i = match.end()
        self.pos = 0

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def accept(self, kind: str, value: str | None = None) -> bool:
        token = self.peek()
        if token and token[0] == kind and (value is None or token[1] == value):
            self.pos += 1
            return True
        return False

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if not token or token[0] != kind or (value is not None and token[1] != value):
            raise ValueError(f"expected {value or kind}, got {token}")
        return self.next()


# AST nodes are tuples: (tag, ...)


def parse(source: str) -> tuple:
    tokens = _Tokens(source)
    node = _parse_conditional(tokens)
    if tokens.peek() is not None:
        raise ValueError(f"trailing tokens in {source!r}: {tokens.tokens[tokens.pos :]}")
    return node


def _parse_conditional(t: _Tokens) -> tuple:
    cond = _parse_or(t)
    if t.accept("op", "?"):
        yes = _parse_conditional(t)
        t.expect("op", ":")
        no = _parse_conditional(t)
        return ("cond", cond, yes, no)
    return cond


def _parse_or(t: _Tokens) -> tuple:
    left = _parse_and(t)
    while t.accept("op", "||"):
        left = ("or", left, _parse_and(t))
    return left


def _parse_and(t: _Tokens) -> tuple:
    left = _parse_equality(t)
    while t.accept("op", "&&"):
        left = ("and", left, _parse_equality(t))
    return left


def _parse_equality(t: _Tokens) -> tuple:
    left = _parse_comparison(t)
    while True:
        token = t.peek()
        if token and token[0] == "op" and token[1] in ("==", "!="):
            t.next()
            left = ("binop", token[1], left, _parse_comparison(t))
        else:
            return left


def _parse_comparison(t: _Tokens) -> tuple:
    left = _parse_additive(t)
    while True:
        token = t.peek()
        if token and token[0] == "op" and token[1] in ("<", ">", "<=", ">="):
            t.next()
            left = ("binop", token[1], left, _parse_additive(t))
        else:
            return left


def _parse_additive(t: _Tokens) -> tuple:
    left = _parse_unary(t)
    while True:
        token = t.peek()
        if token and token[0] == "op" and token[1] in ("+", "-"):
            t.next()
            left = ("binop", token[1], left, _parse_unary(t))
        else:
            return left


def _parse_unary(t: _Tokens) -> tuple:
    if t.accept("op", "!"):
        return ("not", _parse_unary(t))
    if t.accept("op", "-"):
        return ("neg", _parse_unary(t))
    return _parse_postfix(t)


def _parse_postfix(t: _Tokens) -> tuple:
    node = _parse_primary(t)
    while True:
        if t.accept("op", "."):
            token = t.next()
            if token[0] == "op" and token[1] == "*":
                node = ("splat", node)
            else:
                node = ("attr", node, token[1])
        elif t.peek() == ("op", "["):
            t.next()
            if t.accept("op", "*"):
                t.expect("op", "]")
                node = ("splat", node)
            else:
                index = _parse_conditional(t)
                t.expect("op", "]")
                node = ("index", node, index)
        else:
            return node


def _parse_primary(t: _Tokens) -> tuple:
    token = t.next()
    kind, value = token
    if kind == "num":
        return ("lit", float(value) if "." in value else int(value))
    if kind == "str":
        return ("template", value)
    if kind == "ident":
        if value in ("true", "false"):
            return ("lit", value == "true")
        if value == "null":
            return ("lit", None)
        if t.peek() == ("op", "("):
            t.next()
            args = []
            expand = False
            while not t.accept("op", ")"):
                args.append(_parse_conditional(t))
                if t.accept("op", "..."):
                    expand = True
                t.accept("op", ",")
            return ("call", value, args, expand)
        return ("var", value)
    if kind == "op" and value == "(":
        inner = _parse_conditional(t)
        t.expect("op", ")")
        return inner
    if kind == "op" and value == "[":
        if t.peek() == ("ident", "for"):
            return _parse_for(t, "]")
        items = []
        while not t.accept("op", "]"):
            items.append(_parse_conditional(t))
            t.accept("op", ",")
        return ("tuple", items)
    if kind == "op" and value == "{":
        if t.peek() == ("ident", "for"):
            return _parse_for(t, "}")
        entries = []
        while not t.accept("op", "}"):
            key_token = t.next()
            if key_token[0] == "op" and key_token[1] == "(":
                key: tuple = _parse_conditional(t)
                t.expect("op", ")")
            elif key_token[0] == "str":
                key = ("template", key_token[1])
            else:
                key = ("lit", key_token[1])
            if not (t.accept("op", "=") or t.accept("op", ":")):
                raise ValueError(f"expected = after object key {key_token}")
            entries.append((key, _parse_conditional(t)))
            t.accept("op", ",")
        return ("object", entries)
    raise ValueError(f"unexpected token {token}")


def _parse_for(t: _Tokens, closer: str) -> tuple:
    t.expect("ident", "for")
    names = [t.expect("ident")[1]]
    if t.accept("op", ","):
        names.append(t.expect("ident")[1])
    t.expect("ident", "in")
    collection = _parse_conditional(t)
    t.expect("op", ":")
    value = _parse_conditional(t)
    key = None
    if closer == "}":
        key = value
        t.expect("op", "=>")
        value = _parse_conditional(t)
    condition = None
    if t.accept("ident", "if"):
        condition = _parse_conditional(t)
    t.expect("op", closer)
    return ("for", names, collection, key, value, condition, closer)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class UnknownReferenceError(KeyError):
    pass


class Scope:
    """Fixture values keyed by dotted path, plus lazily evaluated ``locals``.

    ``values`` maps e.g. ``"var.environment"``, ``"module.redis[0].port"`` or
    ``"aws_kms_key.dynamodb.arn"`` to the value Terraform would see. Any
    ``local.<name>`` not in ``values`` is parsed from the ``locals`` blocks of
    ``local_files`` and evaluated on first use.
    """

    def __init__(self, values: dict[str, Any], local_files: list[Path] = ()):
        self.values = dict(values)
        self.locals_source: dict[str, str] = {}
        for path in local_files:
            for block in blocks(path):
                if block.kind == "locals":
                    self.locals_source.update(_split_attributes(block.body))
        self._cache: dict[str, Any] = {}
        self._resolving: set[str] = set()

    def local(self, name: str) -> Any:
        key = f"local.{name}"
        if key in self.values:
            return self.values[key]
        if key in self._cache:
            return self._cache[key]
        if name not in self.locals_source:
            raise UnknownReferenceError(key)
        if key in self._resolving:
            raise ValueError(f"cycle evaluating {key}")
        self._resolving.add(key)
        try:
            value = evaluate(parse(self.locals_source[name]), self)
        finally:
            self._resolving.discard(key)
        self._cache[key] = value
        return value


def _path_lookup(node: tuple, scope: Scope, bindings: dict[str, Any]) -> Any:
    """Resolve a traversal by progressively matching the dotted path against the scope."""
    parts: list[str] = []
    cursor = node
    while True:
        if cursor[0] == "attr":
            parts.insert(0, "." + cursor[2])
            cursor = cursor[1]
        elif cursor[0] == "index":
            index = evaluate(cursor[2], scope, bindings)
            parts.insert(0, f"[{json.dumps(index) if isinstance(index, str) else index}]")
            cursor = cursor[1]
        elif cursor[0] == "var":
            root = cursor[1]
            break
        else:
            return None
    if root in bindings:
        value = bindings[root]
        for part in parts:
            value = _step(value, part)
        return value
    path = root
    remaining = parts
    if root == "local":
        if not remaining:
            raise UnknownReferenceError("local")
        value = scope.local(remaining[0][1:])
        for part in remaining[1:]:
            value = _step(value, part)
        return value
    # Longest fixture prefix wins; the rest are attribute/index steps on it.
    for cut in range(len(remaining), -1, -1):
        candidate = path + "".join(remaining[:cut])
        if candidate in scope.values:
            value = scope.values[candidate]
            for part in remaining[cut:]:
                value = _step(value, part)
            return value
    raise UnknownReferenceError(path + "".join(remaining))


def _step(value: Any, part: str) -> Any:
    if part.startswith("."):
        return value[part[1:]]
    key = json.loads(part[1:-1])
    return value[key]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"condition is not a bool: {value!r}")


def _to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    raise ValueError(f"cannot interpolate {value!r}")


def _render_template(raw: str, scope: Scope, bindings: dict[str, Any]) -> str:
    out = []
    i = 0
    while i < len(raw):
        if raw.startswith("$${", i):
            out.append("${")
            i += 3
            continue
        if raw.startswith("%{", i):
            raise ValueError("template directives are not supported")
        if raw.startswith("${", i):
            close = _matching_close(raw, i + 1)
            out.append(_to_text(evaluate(parse(raw[i + 2 : close]), scope, bindings)))
            i = close + 1
            continue
        if raw[i] == "\\":
            escape = raw[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}[escape])
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _call(name: str, args: list[Any]) -> Any:
    if name == "concat":
        result: list[Any] = []
        for item in args:
            result.extend(item)
        return result
    if name == "sort":
        return sorted(args[0])
    if name in ("tolist", "toset"):
        return list(args[0])
    if name == "tostring":
        return _to_text(args[0])
    if name == "length":
        return len(args[0])
    if name == "jsonencode":
        return json.dumps(args[0], separators=(",", ":"), sort_keys=True)
    if name == "jsondecode":
        return json.loads(args[0])
    if name == "contains":
        return args[1] in args[0]
    if name == "keys":
        return sorted(args[0])
    if name == "values":
        return [args[0][key] for key in sorted(args[0])]
    if name == "distinct":
        seen: list[Any] = []
        for item in args[0]:
            if item not in seen:
                seen.append(item)
        return seen
    if name == "coalesce":
        for item in args:
            if item not in (None, ""):
                return item
        raise ValueError("coalesce: no non-empty argument")
    if name == "lookup":
        return args[0].get(args[1], args[2]) if len(args) > 2 else args[0][args[1]]
    if name == "format":
        return args[0] % tuple(args[1:])
    if name == "basename":
        return args[0].rsplit("/", 1)[-1]
    if name == "trimprefix":
        return args[0][len(args[1]) :] if args[0].startswith(args[1]) else args[0]
    if name == "flatten":
        flat: list[Any] = []
        for item in args[0]:
            flat.extend(item if isinstance(item, list) else [item])
        return flat
    if name == "merge":
        merged: dict[str, Any] = {}
        for item in args:
            merged.update(item)
        return merged
    raise ValueError(f"unsupported function {name}")


def evaluate(node: tuple, scope: Scope, bindings: dict[str, Any] | None = None) -> Any:
    bindings = bindings or {}
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "template":
        return _render_template(node[1], scope, bindings)
    if tag == "tuple":
        return [evaluate(item, scope, bindings) for item in node[1]]
    if tag == "object":
        result = {}
        for key_node, value_node in node[1]:
            result[str(evaluate(key_node, scope, bindings))] = evaluate(value_node, scope, bindings)
        return result
    if tag in ("var", "attr", "index"):
        return _path_lookup(node, scope, bindings)
    if tag == "cond":
        return evaluate(node[2] if _truthy(evaluate(node[1], scope, bindings)) else node[3], scope, bindings)
    if tag == "or":
        return _truthy(evaluate(node[1], scope, bindings)) or _truthy(evaluate(node[2], scope, bindings))
    if tag == "and":
        return _truthy(evaluate(node[1], scope, bindings)) and _truthy(evaluate(node[2], scope, bindings))
    if tag == "not":
        return not _truthy(evaluate(node[1], scope, bindings))
    if tag == "neg":
        return -evaluate(node[1], scope, bindings)
    if tag == "binop":
        left, right = evaluate(node[2], scope, bindings), evaluate(node[3], scope, bindings)
        op = node[1]
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        if op == "+":
            return left + right
        if op == "-":
            return left - right
    if tag == "call":
        name, arg_nodes, expand = node[1], node[2], node[3]
        if name == "try":
            for arg in arg_nodes:
                try:
                    return evaluate(arg, scope, bindings)
                except (UnknownReferenceError, KeyError, IndexError, TypeError, ValueError):
                    continue
            raise ValueError("try: every argument failed")
        args = [evaluate(arg, scope, bindings) for arg in arg_nodes]
        if expand:
            args = args[:-1] + list(args[-1])
        return _call(name, args)
    if tag == "for":
        names, collection_node, key_node, value_node, condition_node, closer = node[1:]
        collection = evaluate(collection_node, scope, bindings)
        items = list(collection.items()) if isinstance(collection, dict) else list(enumerate(collection))
        out_list: list[Any] = []
        out_map: dict[str, Any] = {}
        for index, item in items:
            inner = dict(bindings)
            if len(names) == 2:
                inner[names[0]], inner[names[1]] = index, item
            else:
                inner[names[0]] = item
            if condition_node is not None and not _truthy(evaluate(condition_node, scope, inner)):
                continue
            if closer == "}":
                out_map[str(evaluate(key_node, scope, inner))] = evaluate(value_node, scope, inner)
            else:
                out_list.append(evaluate(value_node, scope, inner))
        return out_map if closer == "}" else out_list
    if tag == "splat":
        return evaluate(node[1], scope, bindings)
    raise ValueError(f"cannot evaluate {tag}")


# ---------------------------------------------------------------------------
# Convenience: role policies declared in a Terraform directory
# ---------------------------------------------------------------------------


@dataclass
class RenderedPolicy:
    address: str
    name: str
    document: dict
    path: Path

    @property
    def size(self) -> int:
        """Characters IAM counts: the compact JSON Terraform's jsonencode emits."""
        return len(json.dumps(self.document, separators=(",", ":"), sort_keys=True))


def policy_size(document: dict) -> int:
    return len(json.dumps(document, separators=(",", ":"), sort_keys=True))


def render_role_policies(
    files: list[Path],
    scope: Scope,
    kind: str = "aws_iam_role_policy",
    role_matches=lambda role_expression: True,
) -> list[RenderedPolicy]:
    """Evaluate every ``kind`` resource whose ``role`` expression satisfies ``role_matches``
    and whose ``count`` (if any) evaluates to a non-zero number in ``scope``."""
    rendered: list[RenderedPolicy] = []
    for path in files:
        for block in blocks(path):
            if block.kind != "resource" or block.labels[0] != kind:
                continue
            attributes = block.attributes or _split_attributes(block.body)
            if not role_matches(attributes.get("role", "")):
                continue
            if "count" in attributes and not evaluate(parse(attributes["count"]), scope):
                continue
            if "for_each" in attributes and not evaluate(parse(attributes["for_each"]), scope):
                continue
            policy = evaluate(parse(attributes["policy"]), scope)
            document = json.loads(policy) if isinstance(policy, str) else policy
            name = evaluate(parse(attributes["name"]), scope)
            rendered.append(RenderedPolicy(block.address, name, document, path))
    return rendered
