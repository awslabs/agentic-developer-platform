"""The measurement primitives every wave-4 collector is built out of.

This module contains no wave-4 knowledge at all. It exists to make one property
structural rather than a matter of discipline: **a field that could not be measured
cannot be emitted.**

The kickoff names the anti-pattern precisely — "a dictionary of booleans or a
fixture example is not a producer". The failure it describes is not laziness, it is
subtler and much easier to commit by accident:

    frontend = {
        "revision": revision_from_git(),
        "served_asset_evidence": {"verified": verify() if assets else False},
    }

That `else False` is the bug. `verify()` failing and `verify()` never running
produce the same emitted value, so the artifact says "we checked the served assets
and they did not match" when the truth is "we could not reach the deployment". The
first is a deployment problem the operator should fix; the second is a collection
problem, and a run that reports the wrong one sends them to the wrong place.

So measurements here are three-valued, matching the evaluator's own three-valued
checks:

* a VALUE, when the measurement succeeded;
* a REFUSAL carrying why, when it could not be taken;
* and nothing else. There is no "default".

`Artifact.build` then drops refused fields entirely. The evaluator's
`REQUIRED_ARTIFACT_KEYS` sees a missing key and reports not_run naming it — which
is how a collection gap arrives as "the harness could not look" instead of as a
finding about the deployment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# A refused measurement is not an exception. It is a value, so a collector can
# gather eleven fields, have two refused, and still emit the nine it DID measure —
# an artifact with nine real fields and two named gaps is far more useful to the
# operator than an aborted run with nothing.
#
# It is also not falsy-by-accident: `Refused` is a distinct type, so
# `if measurement:` cannot silently treat a refusal as a `False` observation. That
# specific confusion is how a "not measured" becomes a "measured false".


@dataclass(frozen=True)
class Refused:
    """Why a measurement could not be taken.

    Carries the reason as text because the reason is the useful part. "could not
    measure served_asset_evidence" tells an operator nothing; "HEAD
    https://dash/assets/index-abc.js returned 403" tells them what to fix.
    """

    reason: str

    def __bool__(self) -> bool:
        # Explicitly false-y is tempting and wrong. A refusal must never be usable
        # in a boolean position, because the whole point is that it is not an
        # observation of falsity. Raising here turns `if refused:` into an immediate
        # crash in the collector rather than a wrong field in the artifact.
        raise TypeError(
            f"a refused measurement has no truth value ({self.reason!r}). A refusal means the "
            "measurement was not taken, which is not the same as taking it and getting False — "
            "check `is_refused()` or pass it to Artifact.build, which will omit the field"
        )


class MeasurementRefused(Exception):
    """Raised when a caller demands a value from a refused measurement.

    Distinct from `Refused` because they serve opposite purposes: `Refused` lets a
    collector continue and report the gap, while this exception is what stops a
    collector that tried to USE a gap as if it were data.
    """


Measured = Any  # a real value, or a Refused


def is_refused(value: Measured) -> bool:
    """Whether a measurement was refused rather than taken."""
    return isinstance(value, Refused)


def value_of(measurement: Measured) -> Any:
    """The measured value, or an exception naming the refusal.

    For the places that genuinely cannot proceed — a collector that needs the
    deployed revision in order to know what to measure next. Everywhere else,
    prefer passing the measurement to `Artifact.build` and letting the field be
    omitted.
    """
    if is_refused(measurement):
        raise MeasurementRefused(measurement.reason)
    return measurement


@dataclass
class CommandResult:
    """What running one command produced, recorded rather than interpreted."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    argv: Sequence[str],
    *,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
    timeout: float = 60.0,
) -> CommandResult:
    """Run one command and record what it produced.

    `runner` is injectable so the tests drive every collector through a controlled
    transport rather than a real cluster — which is what makes them tests of the
    collector's logic instead of tests of the environment they happen to run in.

    A missing executable is a CommandResult with a nonzero code, not an exception:
    "git is not installed" is a measurement refusal like any other, and the caller
    already knows how to turn a nonzero result into one.
    """
    argv = tuple(str(part) for part in argv)
    if runner is not None:
        return runner(argv)
    if shutil.which(argv[0]) is None:
        return CommandResult(argv=argv, returncode=127, stdout="", stderr=f"{argv[0]}: not found")
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(argv=argv, returncode=1, stdout="", stderr=str(exc))
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def measure_command(
    argv: Sequence[str],
    *,
    what: str,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
    parse: Callable[[str], Any] | None = None,
) -> Measured:
    """One measurement taken by running a command.

    Nonzero exit, empty output, or a parse failure all become refusals naming what
    was attempted. None of them become a value: a command that printed nothing has
    not told us the thing is absent, only that it printed nothing.
    """
    result = run_command(argv, runner=runner)
    if not result.ok:
        return Refused(
            f"{what}: `{' '.join(result.argv)}` exited {result.returncode}"
            + (f" ({result.stderr.strip()[:200]})" if result.stderr.strip() else "")
        )
    text = result.stdout.strip()
    if not text:
        return Refused(f"{what}: `{' '.join(result.argv)}` succeeded but printed nothing")
    if parse is None:
        return text
    try:
        return parse(text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
        return Refused(f"{what}: could not parse the output of `{' '.join(result.argv)}`: {exc}")


def measure_json_command(
    argv: Sequence[str],
    *,
    what: str,
    runner: Callable[[Sequence[str]], CommandResult] | None = None,
) -> Measured:
    """A measurement whose command emits JSON (`aws ... --output json`, `gh api`)."""
    return measure_command(argv, what=what, runner=runner, parse=json.loads)


@dataclass
class Artifact:
    """An artifact under construction, with its refusals tracked alongside it.

    `build` is where the package's central rule is applied: refused fields are
    OMITTED. Not nulled, not defaulted — omitted, so the evaluator's required-key
    check sees a gap and reports not_run naming it.

    `refusals` accompanies the artifact rather than being written into it. Two
    reasons, and the second is the important one:

    1. The evaluator validates keys against `REQUIRED_ARTIFACT_KEYS`, and an extra
       `_refusals` key in the payload would be a key no schema declares.
    2. A refusal list living INSIDE the artifact invites a reader to treat the
       artifact as complete-with-caveats. It is not: it is incomplete, and the
       operator needs to see that as a collection failure on stderr, not as a
       footnote in a file that otherwise looks like evidence.
    """

    name: str
    fields: dict[str, Measured] = field(default_factory=dict)

    def set(self, key: str, measurement: Measured) -> Artifact:
        self.fields[key] = measurement
        return self

    def update(self, values: Mapping[str, Measured]) -> Artifact:
        self.fields.update(values)
        return self

    @property
    def refusals(self) -> dict[str, str]:
        return {
            key: value.reason for key, value in self.fields.items() if is_refused(value)
        }

    def build(self) -> dict:
        """The payload, carrying only what was actually measured."""
        return {
            key: value for key, value in self.fields.items() if not is_refused(value)
        }

    def missing(self, required: Iterable[str]) -> list[str]:
        """Required keys this artifact cannot supply, refused or never attempted."""
        payload = self.build()
        return sorted(key for key in required if key not in payload)
