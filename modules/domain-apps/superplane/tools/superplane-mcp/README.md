# `superplane-mcp` — Superplane MCP tool surface

Unit **U4** (issue #5038), requirement **R9**, EPIC #4910.

Thin MCP adapters over the Superplane domain contract. The tools themselves are
small; the structure around them is the deliverable, because three of R9's five
acceptance criteria are invariants that either hold by construction or decay.

## Layout

The directory is `superplane-mcp/` (hyphen) because that path is fixed by design
note §3 line 155 and by U1's README, which other units reference. A hyphen is not
a legal Python identifier, so the importable package sits one level in:

```
superplane-mcp/
  superplane_mcp/        <- import this
    authz.py             <- the ONE authorization decision
    server.py            <- the ONE dispatch; handler table
    transports.py        <- thin shims (REST, MCP)
    contract.py          <- recorded MOCK of the domain contract
    redaction.py         <- secret boundary on the single way out
  tests/
```

## How the invariants are enforced

| Criterion | Mechanism |
|---|---|
| **acc. 3** — read-only discovery distinct from spending/mutation | Separate tools bound to separate contract methods. `superplane_discover_capacity` reaches `list_capacity` only; a tripwire contract in the tests fails if it ever touches `allocate`/`release`. Read and spend also require **different capabilities**, so a read grant cannot spend. |
| **acc. 4** — no provider secret in a tool result | Every result passes `redact()` on the single return path in `dispatch_tool()`. Two independent rules run (secret-shaped key names, secret-shaped values), because either alone has a known bypass. A handler cannot opt out. |
| **acc. 5** — authorization identical across every transport | `dispatch_tool()` is the only route to a handler and it calls `authorize()` itself. Transports pass **raw headers** — they cannot pass a principal or a decision, so there is no parameter through which a transport could weaken the check. |

### Where this tightens the precedent

R9 names the agent-context door as the shape to follow, and this copies it, with
one deliberate difference. The door calls `extract_caller_principal()` **in each
transport** (`door/server.py:446`, `door/mcp_app.py:226`) and passes the result
into `_dispatch_tool`. That centralizes dispatch but leaves principal
construction duplicated per transport, so the entry points can drift. Here
extraction lives *inside* the dispatch boundary, because acc. 5 asks for the
invariant to hold through every entry point A adds, and a duplicated extraction
is exactly where such an invariant decays.

Tests guard the structure, not just the behaviour: `test_transports_do_not_duplicate_authorization_logic`
reads `transports.py` and fails if it grows its own capability check, and
`test_every_transport_is_covered_by_parity_tests` fails if a transport is added
without being registered for parity comparison. Behavioural tests alone would
stay green in both cases.

## The contract is a recorded mock

`contract.py` is a **mock, and says so at run time** — discovery results carry
`"source": "mock"`. R9 permits this explicitly: "where a shared contract is
unavailable, mock it and record the mock."

What is unavailable:

- the versioned domain OpenAPI contract is owned by **U8** and has not landed;
- `modules/harness/mcp-hub/` has no running service and its `contracts/tool.schema.json`
  is unwritten — R9 states this is a fact about today's ADP and **not** a
  prerequisite this unit takes on;
- the pinned upstream source (`src/superplane-api/`) was not reachable from the
  runtime that wrote this, so even the request/response shapes could not be
  copied from it. They follow the design note's capacity model.

`CapacityContract` is the single seam to replace when U8 lands: swap the
in-memory data for HTTP calls against the generated client. Nothing above it
changes.

## Tests

```bash
cd modules/domain-apps/superplane/tools/superplane-mcp
python3 -m pytest tests/ -q
python3 -m pytest tests/ -q --cov=superplane_mcp --cov-report=term-missing   # >= 85% required
```
