# ruff: noqa: N999 - the hyphen is the point; see "On the directory name" below.
"""Wave 4 operator tooling: the collectors that produce #3970's artifacts.

**On the directory name.** The kickoff asks for "operator/wave4 tooling", and this is
`operator-wave4/` instead. Not a liberty taken lightly — a nested `operator/wave4/`
needs `platform/scripts/operator/__init__.py`, and that file SHADOWS the standard
library's `operator` module for anything importing with `platform/scripts` on the
path. Python says so itself: "consider renaming ... since it has the same name as the
standard library module named 'operator' and prevents importing that standard library
module". Anything in the tree that does `from operator import itemgetter` would break,
and it would break by silently importing this package instead. A hyphen cannot appear
in an importable package name, which makes the collision impossible to recreate by
accident.


Split from the evaluator on purpose. `agent-control-eval.py` READS artifacts and
refuses the ones that cannot be trusted; the modules here PRODUCE them, by asking
the systems that hold the answers. Keeping the two apart is what stops the
evaluation from being circular — a collector that could also relax a predicate
would let one commit do both halves of a false green.

The rule this package exists to enforce, in the kickoff's words: *a dictionary of
booleans or a fixture example is not a producer.* Every field emitted here is
derived from a command that was run or a response that was received, and a field
that could not be measured is ABSENT rather than defaulted — see
`collector.Measured` and `collector.MeasurementRefused`.

The consequence is deliberate: an incomplete collection produces an incomplete
artifact, the evaluator reports not_run or failed for the affected checks, and the
run exits nonzero. That is the correct outcome. A collector that filled a gap with
`False` would be converting "we did not look" into "we looked and it was fine",
which is the single failure mode both this package and the evaluator are built to
prevent.
"""
