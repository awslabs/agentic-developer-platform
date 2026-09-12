"""Build-time verification that a deploy artifact really carries the pricing policy.

Run as ``python -m pricing_policy.selfcheck`` inside a built image or an unpacked
Lambda zip. Exits 0 when the package imports and its pinned snapshots load; exits
1 with the reason on stderr otherwise (design §4.1, S1).

Why this is a build gate rather than a runtime check
---------------------------------------------------
``pricing_policy`` is a plain directory copied into three separate artifacts — the
gateway image, the budget-usage-tracker Lambda zip and the pricing-refresh Lambda
zip. Nothing in ``pip install`` puts it there. If a ``COPY`` line or a zip's file
list is dropped, ``pip install .`` still succeeds and the image still builds; the
failure surfaces later as an ``ImportError`` on the first priced request, or — worse
— as a Lambda that falls back and bills at default rates.

The ``snapshots/*.json`` data files fail the same way independently: the package can
import perfectly while its rate data is missing, because the snapshot is read lazily
on first use. So this checks that the pinned versions actually *load and parse*, not
merely that the module resolves.

It deliberately imports nothing from ``src/`` or any handler. This must be runnable
in an artifact that contains only the package, and it must not be able to pass
because some unrelated dependency happened to pull the rates in.
"""

from __future__ import annotations

import sys


def run() -> list[str]:
    """Verify the package and its pinned snapshots. Returns a list of failures."""
    failures: list[str] = []

    try:
        from pricing_policy import (
            COMPATIBILITY_SNAPSHOT_VERSION,
            CURRENT_SNAPSHOT_VERSION,
            load_snapshot,
        )
    except Exception as exc:  # pragma: no cover - exercised by the build, not tests
        return [f"cannot import pricing_policy: {exc!r}"]

    # Both pins are checked, not just the current one. The compatibility version
    # must keep resolving for as long as settlement events written under it are
    # retained, so a retried old event reproduces its original cost exactly
    # (design §4.3) — dropping its snapshot file would silently reprice replays.
    for label, version in (
        ("current", CURRENT_SNAPSHOT_VERSION),
        ("compatibility", COMPATIBILITY_SNAPSHOT_VERSION),
    ):
        try:
            snapshot = load_snapshot(version)
        except Exception as exc:
            failures.append(f"{label} snapshot {version} does not load: {exc!r}")
            continue
        if not snapshot.rates:
            failures.append(f"{label} snapshot {version} loaded with zero rate rows")
        if not snapshot.curated_non_openai.get("rates"):
            failures.append(f"{label} snapshot {version} has no curated non-OpenAI rates")

    # A rate lookup, not just a load: proves the flat adapters the gateway's
    # estimator and the tracker's fallback both call are wired end to end.
    try:
        from pricing_policy import legacy_flat_rates

        rates, known = legacy_flat_rates("anthropic.claude-sonnet-4-6-v1")
        if not known or rates.get("input") is None:
            failures.append("legacy_flat_rates could not price a known curated model")
    except Exception as exc:
        failures.append(f"legacy_flat_rates raised: {exc!r}")

    return failures


def main() -> int:
    failures = run()
    if failures:
        print("pricing_policy selfcheck FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nThe artifact is missing the shared pricing policy or its rate snapshots.\n"
            "Check that pricing_policy/ (including snapshots/*.json) is copied into\n"
            "this artifact — see modules/gateway/Dockerfile and the budget-lambda\n"
            "Terraform packaging.",
            file=sys.stderr,
        )
        return 1

    from pricing_policy import CURRENT_SNAPSHOT_VERSION, load_snapshot

    snapshot = load_snapshot()
    print(f"pricing_policy selfcheck OK — snapshot {CURRENT_SNAPSHOT_VERSION}, {len(snapshot.rates)} rate rows, {len(snapshot.models)} OpenAI models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
