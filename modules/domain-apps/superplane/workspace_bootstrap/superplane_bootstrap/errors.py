"""The single refusal type for workspace bootstrap.

Issue #5533 (w6-10), EPIC #4910.

## Why one exception type rather than a hierarchy

Every gate in this package has the same safe response: stop, install nothing
further, register nothing as usable. A caller that needs to branch on *which*
gate refused reads the message; a caller that needs to branch on *whether* to
proceed needs exactly one type. A hierarchy would invite
`except WrongAccount: pass`, and there is no gate here where continuing past a
refusal is correct.

This mirrors `installation/config.py::Refusal`, which makes the same choice for
the same reason in the management installer. It is deliberately NOT that class:
importing it would make this package depend on the installer's module layout, and
the two packages are separate precisely so their failure domains are separate.

## Messages name the check, never the secret

A refusal message identifies what was being verified and what disagreed. It does
not carry certificate bytes, tokens, kubeconfig contents or a provider key — a
refusal is frequently the thing that ends up in a log or an issue comment, so it
is the last place secret material should be interpolated. `registration.py`
enforces the same rule structurally via the contract's
`assert_no_secret_material`.

That rule covers the values this package CHOOSES to mention. `failure_kind` below
covers the values it would otherwise mention by accident — the text of an exception
raised by somebody else's code.
"""

from __future__ import annotations


def failure_kind(error: BaseException) -> str:
    """The only part of a foreign exception safe to put in an operator-facing message.

    Review finding F9. A wrapper that says `f"... failed: {error}"` reproduces a
    message this package did not write and cannot bound. The exceptions reaching these
    wrappers come from a cloud SDK, a database driver or a subprocess, and those put a
    bearer token, a request body, a DSN with a password, or an endpoint's full URL into
    their `str()` as a matter of course. `cli.py::_report` serializes a refusal's string
    to stdout, so interpolating one is a disclosure to the operator's terminal and to
    every CI log that retains it — which is the same defect as printing a credential,
    arriving through a message nobody read as a credential path.

    The type name is kept because it is the one part that is this-package-adjacent
    rather than payload: `TimeoutError` versus `PermissionError` is the distinction an
    operator actually acts on, and a class name cannot carry a secret. Everything else
    is reachable through `__cause__`, which chaining preserves — a debugger with the
    traceback loses nothing, while stdout gains nothing it should not have.

    Deliberately not a "scrub the string" filter. A denylist of secret shapes passes
    anything it was not taught, and the failure mode of a missed pattern is silent
    disclosure. Naming only the type cannot miss a pattern because it never looks at
    one. `tests/test_components.py` pins this structurally, by refusing any raw
    exception interpolation anywhere in the package source.
    """
    return type(error).__name__


class BootstrapRefused(Exception):
    """A bootstrap gate did not verify. Nothing further may be installed or registered.

    Raised for a failed identity check, a missing isolation proof, an absent CRD,
    a competing controller, a replay that would duplicate a record, and a cleanup
    whose ownership could not be established.

    ## Why a refusal can carry partial installation state

    `installation` is the partial `ComponentInstallation` at the moment of refusal, or
    None when nothing had been created yet. It exists because of review finding F6: a
    CRD failure happens AFTER the namespace was created, and a refusal that carried
    nothing left `bootstrap_workspace` building `cleanup=None` for a cluster that has
    an owned namespace on it. An owned object with no rollback plan is worse than the
    original failure, because nothing downstream knows to remove it.

    Typed as `object` to keep this module importable by every other module in the
    package without a cycle — `components.py` imports `errors`, so `errors` cannot
    import `components`. The attribute is always either None or a
    `ComponentInstallation`; `workspace.py` is the only reader and passes it straight
    to `plan_cleanup`, which validates it.
    """

    def __init__(self, *args: object, installation: object | None = None) -> None:
        super().__init__(*args)
        self.installation = installation
