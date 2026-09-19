# Learnings — issue #4529 (gate-aware proposal authoring: the amendment brief)

**Deliverable:** `agent/issue-4529` — commits `3137aa1c` (producer: export the brief),
`a0ea9ecc` (consumer: persona + skill Step 7g, bound by a contract test), `60a855a2`
(delivery: the brief survives into the agent process).
**Persona:** `agent-developer` — implementation

---

## 1. "A consumer with no producer" is a defect class, and it has three forms, not one

The finding that opened this work: `lib/engine_registration.py` read an authored
amendment from `aidlc/spaces/amendments/{request_id}/proposal.json`, and **nothing told
any author to write there**. Not the persona, not the skill, not the run's environment.
A correctly summoned, correctly authorized author received two opaque identifiers,
followed its ordinary planning instructions, opened a flow nobody asked for, and filed
nothing — while the human's `replan:` request stayed recorded and owed.

What took a second pass to see is that the same defect has three distinct forms, and
fixing one leaves the others invisible:

1. **Consumer, no producer** — code reads a path nothing is told to write. The original.
2. **Producer, no consumer** — an env var is exported that no instruction mentions. This
   is *harder* to spot, because the instruction text reads perfectly well right up until
   an author follows it and finds an unset variable.
3. **Producer and consumer, no delivery** — both halves exist and name the same things,
   but the value never reaches the process that reads it. This one is the worst: every
   test of form 1 and form 2 stays green.

Form 3 is what commit `60a855a2` pins. The chain is
`_export_authoring_assignment` (entrypoint.py:1463) → `agent_env = os.environ.copy()`
(2320) → `subprocess.run(command, env=agent_env)` → Node's `workerAwsEnvironment()`,
which spreads `process.env` and deletes only named AWS keys. It works today. But turn
`agent_env` into an allow-list, or widen a scrub from named variables to a prefix
(`ADP_*`), and the export still happens, the variables are still in `os.environ`, and
the author gets nothing.

**Generalizable:** when you fix a producer/consumer mismatch, ask what *transports* the
value between them, and whether anything on that path is allowed to filter. Assert the
transport, not just the two endpoints. Mine turned out to be a mediated-credential
scrub that an AIDLC author can legitimately be subject to.

## 2. Read for asymmetry between two code paths that "do the same thing"

The single most consequential line of instruction text in this change came from tracing
callers rather than reasoning by analogy.

Registering a **new** plan runs `transform_for_registration`, which synthesises an
acceptance gate and optionally wave gates. Accepting an **amendment** runs `amend_plan`,
which synthesises **nothing** — I confirmed this by enumerating every caller of
`transform_for_registration` and finding `amend_plan` absent, not by assuming symmetry.

Combined with the fact that an amendment document is a whole-plan replacement (absence
is supersession), this means an author carrying the new-plan habit over — "a gate is
always added for me" — files a document that **deletes the acceptance gate the original
registration inserted**. On an issue whose whole subject is gate placement, that is the
one way an amendment silently *reduces* human oversight. "I did not mention gates" is
not neutral on this path; it deletes them.

**Generalizable:** when two paths converge on the same data structure, the interesting
bug is rarely in either path. It is in what one does and the other does not, on a step
both callers assume is shared. Enumerate callers; do not reason from naming.

## 3. Assert against the real artifact the runtime loads, not the source file

The worker never reads `modules/agent-factory/`. It reads a flat tree assembled at image
build time by `stage-personas.sh` (Dockerfile stage 2) → `/app/personas/` →
`WORK_DIR/.adp-rules/personas/` → `loadRules()`. A test asserting on the source markdown
would pass for guidance the shipped image does not contain.

So the contract test runs the **real** staging script over the **real** source trees into
a tmp dir and asserts on the output. Similarly it imports the **real** constants from
`lib.engine_registration` rather than retyping `"ADP_AMENDMENT_OUTPUT_PATH"` — a test
carrying its own copy of the name under test passes happily after a rename has broken
both real sides.

The env-var coverage test is parametrised over the library's own tuple, so adding a
seventh brief variable fails the suite until some instruction mentions it. That is the
direction the original defect ran: code grew a name, instructions did not.

## 4. Two mutation probes failed because of how I designed the probe, not the fence

Worth recording because both wasted a cycle and both were my error:

- **Probe 1** mutated `entrypoint.py` but was paired with an instruction-*text* test.
  An entrypoint change cannot affect what the markdown says, so the test passed and the
  probe reported a missing fence that was not missing. Re-paired with the test that
  actually reads the exported path → failed as required.
- **Probe 6** replaced 1 of 3 occurrences of `ADP_AMENDMENT_OUTPUT_PATH` in the persona,
  so "is it named" still passed. Re-ran replacing all 3, with an
  `assert t.count(old) == 3` guard → failed as required.

**Generalizable:** a mutation probe has two halves, and "the test still passed" is
ambiguous between *the fence is untested* and *I pointed the probe at the wrong test*.
Before believing the first, check that the mutated file is one the named test can
observe, and that the mutation is total (assert the occurrence count).

## 5. Find the config that governs the directory you are in

I formatted a new test with `ruff format --line-length 150`, having found
`line-length = 150` in `modules/gateway/pyproject.toml`. The file I was editing is under
`modules/agent-factory/`, whose own `pyproject.toml` declares **100**. Two different
widths in one repo, and I had picked the one belonging to a different module.

Checking further: no workflow lints `agent-worker-image/` at all (`agent-worker-image.yml`
has no ruff or pytest step), and files already on `origin/main` in that directory are not
uniformly format-clean at 100 either. So the correct action was to format *my new file*
to 100 and deliberately **not** reformat the pre-existing spots in a sibling that 100
would also rewrap — unrelated churn in a behavioural diff makes review harder and is not
mine to land here.

**Generalizable:** resolve tool config from the edited file upward, not by grepping the
repo for the first match. And before "fixing" formatting across a file, check whether the
tool is actually enforced on that path and whether the existing code complies — if
neither, a reformat is churn wearing the costume of hygiene.

## 6. Two bogus green runs from wrong interpreter and wrong path

A gateway test run reported **exit code 0** while printing `No module named pytest`; a
second reported exit 0 with `no tests ran` because the file lives under
`tests/orchestration/`, not `tests/`. Both would have been recorded as passes by anything
reading only the exit status.

**Generalizable:** for any test invocation, read the *summary line*, not the exit code. A
run that collected zero tests is not a pass, and `python` is not necessarily the
interpreter that has your dev dependencies.

## 7. Pre-existing defect found, scoped out, and recorded rather than fixed

`main()` crashes with `AttributeError` on any truthy non-dict `payload`, at
`(envelope.get("payload") or {}).get("provider")` — three sites, present on `origin/main`
and unrelated to this story. Rather than widen the diff, I extracted
`_export_authoring_assignment` as a named module-level function and drove the shape-guard
tests directly against it, documenting in the test docstring why the whole-run probe
cannot reach that code. The alternative — a whole-run test for that case — would have
failed on somebody else's defect and proved nothing about my guard.

This extraction also fixed a **tautological test** I caught in myself: the original
version asserted on environment state without ever invoking my code.
