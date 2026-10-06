# CPython email parser maintenance

This overlay removes nesting-dependent Python recursion from the exact CPython
3.10.21 legacy address parser used by `email.utils.parseaddr` and `getaddresses`.
It changes only `email/_parseaddr.py` and removes that module's old bytecode.
Python, SkyPilot and dependency versions remain unchanged.

The readable patch replaces recursive group parsing with explicit continuation
phases, a group-depth counter and one result list. It replaces nested comment
recursion with a delimiter-depth counter and one text accumulator. This removes
both recursive cycles; changing comment handling alone would leave the group
recursion discussed in CVE-2023-36632. It introduces no arbitrary input/depth cap
and does not catch or suppress errors to classify input as safe.

`source-lock.json` pins the official source archive, original module, exact patch,
resulting module and PSF license. The installer requires that exact Python version
and module input, applies the patch offline with zero fuzz to a temporary copy,
and verifies the result before writing it. It invalidates only that module's
legacy/current-interpreter bytecode paths. A bind mount keeps installer and test
files out of the runtime image; `-B` prevents incidental new bytecode during build.

## Compatibility and progress invariants

Public signatures, strict defaults, tuple/list return shapes, ordering and the
existing acceptance policy remain unchanged. The group loop preserves the old
sequence of comment resets, cursor rewinds, whitespace/comment scans, semicolon
consumption, and trailing comma consumption. In particular, a group scan that
reaches EOF through `gotonext` still performs the original empty child parse.
Group names remain discarded and empty-group placeholder behavior is retained.

The group continuation either consumes a delimiter, schedules a leaf parse that
consumes input, or finishes one previously opened group. The finish phase returns
or resumes a containing group. Its finite continuation transitions cannot grow
the Python call stack. The delimiter loop consumes a character on every iteration;
inner carriage returns terminate only the inner comment. Escaping precedes closing
delimiters, which precede recognizing a nested comment, matching the original code.
One accumulator avoids repeated nested string/list copying.

Private/deprecated `AddrlistClass` subclasses overriding `getaddress` or
`getcomment` may observe different callback counts because internal recursive
dispatch is removed. That private subclass behavior is not a preserved public
`email.utils` contract. The repair is not a general resource bound or security
assessment for the entire email package or arbitrary application input handling.

## Verification

`ordinary-baseline.json` records the original module's results for 15 small ordinary
RFC address examples, public strict/compatibility modes, intermediate cursor and
comment states, and seven ordinary comment/quote/domain-literal fragments. This
includes two-level comments, groups, escaping and obsolete syntax with existing
parser quirks. `verify.py` compares the actual installed implementation to those
results, checks loaded function bytecode against the reviewed source, and checks
that the parser's source-level call graph is acyclic.

Build from the `images/skypilot` directory with a reviewed immutable parent:

```sh
docker build --network=none \
  --build-arg BASE_IMAGE=repository@sha256:reviewed-platform-digest \
  -f email-parser-maintenance/Dockerfile -t local-email-parser .
```

Run the ordinary verifier without network or writable runtime storage:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 1000:1000 \
  --mount type=bind,src="$PWD/email-parser-maintenance",dst=/maintenance,readonly \
  --entrypoint python local-email-parser -B /maintenance/verify.py
```

Do not run deep/oversized/malformed nesting probes or vulnerability suites as part
of this qualification. Static progress review and ordinary compatibility checks
are the intended checks. An independent reviewer must inspect the production
patch, package/provenance boundary and actual image filesystem delta. Raw scanner
findings and verdicts are unchanged; no vendor dispute or source-only review is
an automatic disposition, whole-image approval or release clearance.
