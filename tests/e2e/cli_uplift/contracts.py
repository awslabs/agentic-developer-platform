"""The browser's wire contract, extracted from the revision under test.

E13 asserts that live API responses satisfy both of their real consumers. The CLI
consumer can be executed — the on-instance script imports the installed helpers
and calls their own reader functions, so a dropped field makes the product's code
raise. The browser consumer cannot: it is TypeScript, its checks happen at compile
time, and by the time a response reaches a component a missing field is already
`undefined`.

What the browser DOES have is a declaration, and a declaration is exactly what an
evaluation needs: `account_id: string` and `account_id: string | null` are
different promises, and a server that starts returning null for the first is a
rendered "undefined" or a thrown component. So the interfaces are read out of the
git object store at `expected_revision` — the same source `release.py` derives the
expected CLI hashes from, for the same reason: a commit SHA cannot be edited
without becoming a different SHA, so the contract cannot drift to match whatever
the deployment happens to serve.

This is deliberately a small, total parser rather than a TypeScript one. It reads
`export interface Name { field: type; }` and `export type Name = 'a' | 'b';`, and
it FAILS on anything it does not understand rather than skipping it — a silently
dropped field is a check that quietly stops existing, which is the failure mode
this whole harness is built against.
"""

from __future__ import annotations

import re

from . import release

# Where each wire type is declared. Both files are the real consumers: the service
# module the credentials page calls, and the admin surface's wire types.
SOURCES = {
    "CredentialItem": "modules/gateway/frontend/src/services/credentials.ts",
    "DestinationSummary": "modules/gateway/frontend/src/types/bedrockRouting.ts",
    "MappingSummary": "modules/gateway/frontend/src/types/bedrockRouting.ts",
}

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")
INTERFACE = re.compile(
    r"^export\s+interface\s+(?P<name>\w+)\s*\{(?P<body>[^{}]*)\}", re.M
)
ALIAS = re.compile(r"^export\s+type\s+(?P<name>\w+)\s*=\s*(?P<value>[^;]+);", re.M)
# `field?: type;` — one member per statement, which is the house style in both
# files. A member this does not match is an error, not a skip.
MEMBER = re.compile(r"^(?P<name>\w+)(?P<optional>\??)\s*:\s*(?P<type>.+)$")


class ContractError(RuntimeError):
    """The browser's contract could not be derived. Nothing was evaluated."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def _strip(text):
    """Remove comments. JSDoc on this surface carries prose, not declarations."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def _aliases(text):
    """Exported string-union aliases, so a field's declared type is self-contained.

    `scope_type: MappingScopeType` tells an evaluation nothing on its own; resolved
    to `'user' | 'team' | 'org'` it constrains the VALUE, which is what catches a
    server that grew a state the UI has no branch for.
    """
    found = {}
    for match in ALIAS.finditer(text):
        value = " ".join(match.group("value").split())
        found[match.group("name")] = value
    return found


def _resolve(declared, aliases, *, depth=0):
    """Substitute alias names until the type is spelled in primitives and literals."""
    require(depth < 8, f"Type aliases nest too deeply to resolve: {declared}")
    parts = []
    for part in declared.split("|"):
        item = part.strip()
        if item in aliases:
            parts.append(_resolve(aliases[item], aliases, depth=depth + 1))
        else:
            parts.append(item)
    return " | ".join(parts)


def interfaces(text, *, names=None):
    """Every exported interface in one source file, as `{name: {field: type}}`.

    Optional members keep their `?`, because the on-instance check treats an absent
    optional field as acceptable and an absent required one as a contract breach.
    """
    stripped = _strip(text)
    aliases = _aliases(stripped)
    found = {}
    for match in INTERFACE.finditer(stripped):
        name = match.group("name")
        if names is not None and name not in names:
            continue
        fields = {}
        for statement in match.group("body").split(";"):
            line = " ".join(statement.split())
            if not line:
                continue
            member = MEMBER.match(line)
            require(
                member,
                f"{name} declares a member this evaluation cannot read: {line!r}. "
                "Extend contracts.py rather than letting the field go unchecked",
            )
            fields[member.group("name") + member.group("optional")] = _resolve(
                member.group("type"), aliases
            )
        require(fields, f"{name} declares no fields")
        found[name] = fields
    return found


def wire_contracts(revision, *, repo_root=None, read=release.git_blob):
    """The declared wire contract for every type E13 checks, at one revision.

    `read` is injectable so the offline tests can exercise this against fixture
    sources without a git repository, and so a failure to read one file names that
    file rather than surfacing as an empty contract.
    """
    wanted = {}
    for name, path in SOURCES.items():
        wanted.setdefault(path, set()).add(name)

    contracts = {}
    for path, names in sorted(wanted.items()):
        try:
            raw = read(revision, path, repo_root=repo_root)
        except release.ReleaseError as exc:
            raise ContractError(
                f"The browser's wire types could not be read from {path}: {exc}"
            ) from None
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        found = interfaces(text, names=names)
        missing = sorted(names - set(found))
        require(
            not missing,
            f"{path} at revision {revision[:12]} no longer declares: "
            + ", ".join(missing)
            + ". The UI contract E13 checks has moved or been renamed",
        )
        contracts.update(found)
    return contracts
