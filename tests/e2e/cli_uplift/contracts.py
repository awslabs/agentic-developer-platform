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
from pathlib import Path

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


# ---------------------------------------------------------------------------
# The gateway's own mounted route table
# ---------------------------------------------------------------------------
#
# A request path is only right relative to the routes the app actually mounts,
# and a double that matches the harness's own spelling cannot tell a right path
# from a wrong one. Issue #8242 spent two live deployments proving that: the
# team-inventory request was asserted as a literal in mocks, agreed with itself,
# and was answered 404 by the deployment twice (runs 39733378117 and
# 39767400131, two different wrong prefixes).
#
# So the admin paths this harness requests are derived the same way the browser
# contract above is derived — from the declaration, which cannot agree with a
# guess by accident. FastAPI composes a mounted path from three things and all
# three are read here: the app's include (`app.py` registers every UNIT_MODULES
# router with `app.include_router(module.router)`, with no prefix of its own),
# the router's own `prefix=`, and the route decorator's path. That is why
# `/admin/organizations/<org>/teams` and
# `/api/admin/identity/organizations/<org>/users` are both correct at the same
# time: two routers, two prefixes, one app.
#
# Sub-routers a module includes into its own router are deliberately NOT walked.
# This answers "is the path the harness sends a route of the router that owns
# this surface", and a missing route fails loudly here rather than being
# approximated.

APP_SOURCE = "modules/gateway/src/app.py"

ROUTER_SOURCES = {
    "src.admin.routes": "modules/gateway/src/admin/routes.py",
    "src.admin.identity.router": "modules/gateway/src/admin/identity/router.py",
}

MODULE_INCLUDE = re.compile(r"app\.include_router\(\s*module\.router\s*\)")
ROUTER_DECLARATION = re.compile(
    r"^router\s*=\s*APIRouter\((?P<args>.*?)\)\s*$", re.M | re.S
)
ROUTER_PREFIX = re.compile(r"prefix\s*=\s*\"(?P<prefix>[^\"]*)\"")
ROUTE_DECORATOR = re.compile(
    r"@router\.(?P<method>get|post|put|patch|delete)\(\s*\"(?P<path>/[^\"]*)\""
)
PATH_PARAMETER = re.compile(r"\{[^{}/]+\}")


def repo_root():
    """The checkout this module lives in: tests/e2e/cli_uplift/contracts.py."""
    return Path(__file__).resolve().parents[3]


def _source(path, *, root=None):
    location = (Path(root) if root else repo_root()) / path
    require(
        location.is_file(),
        f"{path} is not present in this checkout, so the mounted route table "
        "cannot be derived and nothing would be validated",
    )
    return location.read_text(encoding="utf-8")


def mounted_routes(module, *, root=None):
    """Every `(METHOD, path)` the app mounts for one unit module's own router."""
    require(
        module in ROUTER_SOURCES,
        f"{module} is not a router source this evaluation knows how to read",
    )
    app_source = _source(APP_SOURCE, root=root)
    require(
        MODULE_INCLUDE.search(app_source),
        "app.py no longer mounts unit-module routers with "
        "`app.include_router(module.router)`, so a mounted path is no longer the "
        "router's own prefix plus its route and this derivation would be fiction",
    )
    require(
        f'"{module}"' in app_source,
        f"{module} is not registered in app.py, so none of its routes are mounted",
    )
    source = _source(ROUTER_SOURCES[module], root=root)
    declaration = ROUTER_DECLARATION.search(source)
    require(
        declaration,
        f"{ROUTER_SOURCES[module]} declares no module-level `router = APIRouter(...)`",
    )
    prefix = ROUTER_PREFIX.search(declaration.group("args"))
    mount = prefix.group("prefix") if prefix else ""
    routes = {
        (match.group("method").upper(), mount + match.group("path"))
        for match in ROUTE_DECORATOR.finditer(source)
    }
    require(routes, f"{ROUTER_SOURCES[module]} declares no routes on its own router")
    return routes


def admin_routes(*, root=None):
    """The mounted admin surface this harness calls, across both admin routers."""
    found = set()
    for module in ROUTER_SOURCES:
        found |= mounted_routes(module, root=root)
    return found


def mounted_route(method, request_path, routes):
    """The mounted template answering `request_path`, or None if none does.

    Takes the request exactly as the harness sends it, query string included, so
    a caller asserts the real wire path rather than a tidied-up version of it.
    """
    path = request_path.split("?", 1)[0]
    for mounted_method, template in sorted(routes):
        if mounted_method != method.upper():
            continue
        pattern = "".join(
            "[^/]+" if PATH_PARAMETER.fullmatch(part) else re.escape(part)
            for part in re.split(r"(\{[^{}/]+\})", template)
        )
        if re.fullmatch(pattern, path):
            return template
    return None
